"""Public Motor facade. Planning clients share the same guarded execution path."""
from __future__ import annotations

import os
from contextvars import ContextVar
import threading
import time
import uuid

from .arbiter import ActionArbiter, Stopped
from .models import Action, Budget, Metrics, Selector
from .perception import scrollables
from .reflex import ReflexEngine
from .scout import UIScout

REQUEST_CANCEL = ContextVar("motor_request_cancel", default=None)
REQUEST_GUARD = ContextVar("motor_request_guard", default=None)


def action_from_dict(value):
    value = dict(value)
    selector = value.get("selector")
    if isinstance(selector, dict):
        value["selector"] = Selector(**selector)
    value["id"] = ""  # Learned routes always get a new transport identity.
    return Action(**value)


def _rss():
    try:
        import resource
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except ImportError:
        if os.name == "nt":
            try:
                import ctypes
                from ctypes import wintypes
                class Memory(ctypes.Structure):
                    _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [(name, ctypes.c_size_t) for name in
                        ("peak", "working", "paged_peak", "paged", "nonpaged_peak", "nonpaged", "pagefile", "pagefile_peak")]
                memory = Memory()
                memory.cb = ctypes.sizeof(memory)
                ctypes.windll.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
                info = ctypes.windll.psapi.GetProcessMemoryInfo
                info.argtypes = [wintypes.HANDLE, ctypes.POINTER(Memory), wintypes.DWORD]
                if info(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(memory), memory.cb):
                    return int(memory.peak // 1024)
                return None
            except Exception:
                return None
        return None


class Motor:
    def __init__(self, adapter, store, sink=None):
        self.adapter, self.store, self.sink = adapter, store, sink
        self._active = None
        self._active_lock = threading.Lock()
        self._task_lock = threading.Lock()

    def _run(self, package, seconds, work):
        if not self._task_lock.acquire(blocking=False):
            return {"ok": False, "escalation": {"required": True, "reason": "motor_busy"}}
        metrics = Metrics()
        start, cpu = time.monotonic(), time.thread_time()
        try:
            arbiter = ActionArbiter(self.adapter, package, seconds=seconds, cancel_event=REQUEST_CANCEL.get(),
                                    task_guard=REQUEST_GUARD.get())
            with self._active_lock:
                self._active = arbiter
            with arbiter:
                reflex = ReflexEngine(arbiter, self.store, self.sink, metrics)
                result = work(reflex)
                result.setdefault("ok", result.get("escalation") is None)
        except Stopped as exc:
            result = {"ok": False, "escalation": {"required": True, "reason": exc.reason}}
        finally:
            with self._active_lock:
                self._active = None
            self._task_lock.release()
        metrics.elapsed_ms = round((time.monotonic() - start) * 1000, 3)
        metrics.cpu_ms = round((time.thread_time() - cpu) * 1000, 3)
        metrics.max_rss_kb = _rss()
        result["metrics"] = metrics.public()
        if self.sink:
            result["collection"] = self.sink.report()
        return result

    def scout(self, package, mode="quick", goal="", budget=None, follow_tabs=True):
        budget = budget or Budget()
        result = self._run(package, budget.seconds + budget.restore_seconds + 2,
            lambda reflex: UIScout(reflex, budget, mode=mode, goal=goal, follow_tabs=follow_tabs).run())
        result["app_map"] = self.brief(package, query=goal or None)
        return result

    def brief(self, package, query=None, limit=8):
        return self.store.summary(package, query=query, limit=min(max(int(limit), 1), 12))

    def act(self, package, action: Action, source="external_harness", grant=None):
        def run(reflex):
            outcome = reflex.execute(action, source=source, grant=grant)
            return {"ok": outcome.verified or (action.kind == "WAIT" and outcome.status == "unchanged"),
                    "action_id": outcome.action_id, "status": outcome.status, "reason": outcome.reason,
                    "current": outcome.after.summary(),
                    "escalation": None if outcome.verified or action.kind == "WAIT" else {"required": True, "reason": outcome.reason}}
        return self._run(package, 12, run)

    def navigate(self, package, query, *, open_target=False, budget=None):
        budget = budget or Budget(seconds=35)
        def run(reflex):
            current = reflex.observe()
            start_page = self.store.observe(current)
            route = self.store.route(package, start_page, query, device_id=current.device_id)
            shared = False
            if not route:
                route = self.store.route(package, start_page, query)
                shared = bool(route)
            steps = []
            for edge in route[:budget.max_depth + 8]:
                action = action_from_dict(edge["action"])
                outcome = reflex.execute(action, state=current)
                steps.append({"action": action.public(), "status": outcome.status})
                if not outcome.verified:
                    self.store.invalidate(package, self.store._page_id(current), current.device_id, outcome.reason)
                    return {"ok": False, "steps": steps, "escalation": {"required": True, "reason": outcome.reason}}
                current = outcome.after
                if self.store._page_id(current) != edge["to"]:
                    self.store.invalidate(package, edge["from"], current.device_id, "route_diverged")
                    return {"ok": False, "steps": steps, "escalation": {"required": True, "reason": "map_route_diverged"}}
            query_normal = "".join(query.casefold().split())
            for _ in range(budget.max_scrolls + 1):
                matches = [n for n in current.visible if query_normal in "".join(n.label.casefold().split())]
                if matches:
                    if open_target:
                        targets = [n for n in matches if n.clickable]
                        if len(targets) != 1:
                            return {"ok": False, "current": current.summary(), "escalation": {"required": True, "reason": "ambiguous_or_nonactionable_match"}}
                        outcome = reflex.execute(Action("CLICK", targets[0].selector()), state=current)
                        steps.append({"action": "open_match", "status": outcome.status})
                        current = outcome.after
                        if not outcome.verified:
                            return {"ok": False, "current": current.summary(), "escalation": {"required": True, "reason": outcome.reason}}
                    return {"ok": True, "reused_map": bool(route), "shared_semantics": shared,
                            "steps": steps, "current": current.summary(), "escalation": None}
                regions = scrollables(current)
                if not regions or reflex.metrics.scrolls >= budget.max_scrolls:
                    break
                outcome = reflex.execute(Action("SCROLL", regions[0].selector(), purpose="observation"), state=current)
                current = outcome.after
                if outcome.status != "verified":
                    break
            return {"ok": False, "reused_map": bool(route), "steps": steps, "current": current.summary(),
                    "escalation": {"required": True, "reason": "target_not_observed_locally"}}
        return self._run(package, budget.seconds, run)

    def cancel(self, reason="user_cancelled"):
        with self._active_lock:
            active = self._active
        if active:
            active.stop(reason)
            return {"ok": True, "reason": reason}
        return self.adapter.cancel(reason)

    def cancel_request(self, event, reason="request_cancelled"):
        event.set()
        with self._active_lock:
            active = self._active
        if active is not None and active.cancelled is event:
            active.stop(reason)  # Never use an idle/global cancellation for a notification.
