"""One device, one lease, one executor for planners, scouts and reflex rules."""
from __future__ import annotations

from dataclasses import dataclass
import re
import threading
import time
import uuid

from .models import Action, State, digest
from .perception import tabs


class Stopped(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


_DANGEROUS = re.compile(r"支付|付款|购买|下单|结算|删除|清空|提交|发送|发布|授权|允许|安装|卸载|确认|登录|注册|订阅|转账|充值|购买|"
                        r"\b(?:pay|buy|purchase|checkout|delete|remove|clear|submit|send|post|publish|authorize|allow|install|uninstall|confirm|login|subscribe|transfer)\b", re.I)


def authorization_key(action: Action):
    return digest([action.kind, action.selector.public() if action.selector else None, action.direction])


@dataclass(frozen=True)
class TaskAuthorization:
    """Created by an explicitly authorized task flow, scoped to concrete actions."""
    package: str
    action_keys: frozenset[str]
    reason: str


class SafetyPolicy:
    def allows(self, state: State, action: Action, target=None, grant: TaskAuthorization | None = None):
        if state.excluded:
            return False, "sensitive_screen"
        if action.kind not in {"CLICK", "SCROLL", "BACK", "WAIT"}:
            return False, "unsupported_action"
        authorized = bool(grant and grant.package == state.package and grant.reason and
                          authorization_key(action) in grant.action_keys)
        if action.kind in {"BACK", "WAIT"}:
            return True, ""
        if action.kind == "SCROLL":
            return (action.direction in {"forward", "backward"}, "invalid_scroll_direction")
        if target is None:
            return False, "target_missing"
        semantics = " ".join((target.label, target.description, target.resource_id))
        if _DANGEROUS.search(semantics):
            return (True, "") if authorized and action.purpose == "task" else (False, "explicit_task_authorization_required")
        if authorized and action.purpose == "task":
            return True, ""
        if target in tabs(state):
            return True, ""
        if target.label.strip().casefold() in {"关闭", "取消", "返回", "返回首页", "close", "cancel", "back", "稍后"}:
            return True, ""
        if re.search(r"返回|详情|查看|\b(?:back|details|view)\b", target.label + " " + target.description, re.I):
            return True, ""
        # A read-only list entry is safe only when it carries explicit item/detail semantics.
        if state.under_scroll(target) and re.search(r"(?:item|product|detail|商品|条目)", semantics, re.I):
            return True, ""
        return False, "uncertain_navigation"


class ActionArbiter:
    def __init__(self, adapter, package: str, *, seconds=60, owner=None, cancel_event=None, task_guard=None):
        self.adapter, self.package = adapter, package
        self.owner = owner or "motor:" + str(uuid.uuid4())
        self.deadline = time.monotonic() + seconds
        self.cancelled = cancel_event if cancel_event is not None else threading.Event()
        self.task_guard = task_guard
        self.reason = ""
        self.lease = None
        self._mutex = threading.RLock()
        self._renew_at = 0.0
        self._closed = False

    def __enter__(self):
        if self.cancelled.is_set():
            raise Stopped(self.reason or "request_cancelled")
        self._check_task()
        try:
            self.lease = self.adapter.acquire(self.owner, 120000, self.package)
            self._renew_at = time.monotonic() + 45
        except Exception as exc:
            raise Stopped(getattr(exc, "code", "motor_bridge_unavailable")) from None
        if self.cancelled.is_set():
            self.stop(self.reason or "request_cancelled")
            self.__exit__()
            raise Stopped(self.reason)
        try:
            self._check_task()
        except Stopped:
            self.__exit__()
            raise
        return self

    def _check_task(self):
        if self.task_guard is not None:
            try:
                reason = self.task_guard()
            except Exception:
                reason = 'harness_guard_unavailable'
            if reason:
                self.stop(reason)
                raise Stopped(reason)

    def check(self):
        if self.cancelled.is_set() or self._closed:
            raise Stopped(self.reason or "task_cancelled")
        self._check_task()
        if time.monotonic() >= self.deadline:
            self.stop("task_timeout")
            raise Stopped(self.reason)
        if self.lease is None:
            raise Stopped("no_action_lease")
        if time.monotonic() >= self._renew_at:
            try:
                self.lease = self.adapter.acquire(self.owner, 120000, self.package, self.lease)
                self._renew_at = time.monotonic() + 45
                if self.cancelled.is_set():
                    self.stop(self.reason or "request_cancelled")
                    raise Stopped(self.reason)
                self._check_task()
            except Stopped:
                raise
            except Exception as exc:
                self.stop(getattr(exc, "code", "lease_lost"), notify=False)
                raise Stopped(self.reason) from None

    def read(self, context=None):
        with self._mutex:
            self.check()
            try:
                state = self.adapter.read(self.lease, self.package, context)
            except Exception as exc:
                code = getattr(exc, "code", "observation_failed")
                self.stop(code, notify=True)
                raise Stopped(code) from None
            if state.package != self.package:
                self.stop("foreground_changed")
                raise Stopped(self.reason)
            return state

    def execute(self, kind, args, ident, context=None):
        with self._mutex:
            self.check()
            try:
                response = self.adapter.perform(kind, {"expectedPackage": self.package, **args}, ident, self.lease, context)
            except Exception:
                self.stop("delivery_unknown")
                raise Stopped(self.reason) from None
            return response

    def wait(self, sequence, timeout_ms=500):
        self.check()
        try:
            value = self.adapter.events(sequence, min(timeout_ms, max(0, int((self.deadline - time.monotonic()) * 1000))))
        except Exception as exc:
            self.stop(getattr(exc, "code", "events_unavailable"))
            raise Stopped(self.reason) from None
        self.check()
        if value.get("generation") is not None and value["generation"] != self.lease["generation"]:
            self.stop(value.get("reason") or "lease_lost", notify=False)
            raise Stopped(self.reason)
        return value

    def stop(self, reason="task_cancelled", notify=True):
        self.reason = str(reason)
        self.cancelled.set()
        if notify and self.lease is not None and not self._closed:
            try:
                self.adapter.cancel(self.reason, self.lease)
            except Exception:
                pass

    def __exit__(self, *_):
        with self._mutex:
            try:
                if self.lease is not None:
                    self.adapter.release(self.lease)
            except Exception:
                pass
            finally:
                self._closed = True
                self.lease = None
