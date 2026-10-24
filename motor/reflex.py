"""Deterministic actions with fresh semantic targets and observed postconditions."""
from __future__ import annotations

import time
import uuid
from .arbiter import SafetyPolicy, Stopped
from .models import Action, Candidate, Metrics, Outcome
from .perception import resolve


_STOP_ERRORS = {"motor_cancelled", "motor_busy", "foreground_changed", "paused", "screen_off", "locked",
                "timeout", "outcome_unknown", "owner_confirmation_ui", "control_ui_local_only"}


def _loading(state):
    return any("ProgressBar" in n.class_name or n.label.strip().casefold() in
               {"加载中", "正在加载", "加载中…", "loading", "loading...", "please wait"} for n in state.visible)


def _observable_window(state):
    roots = [n for n in state.nodes if not n.parent_id]
    return not roots or not state.width or not state.height or all(
        n.bounds[0] >= 0 and n.bounds[1] >= 0 and n.bounds[2] <= state.width and n.bounds[3] <= state.height
        for n in roots)


class ReflexEngine:
    def __init__(self, arbiter, store=None, sink=None, metrics=None, policy=None):
        self.arbiter, self.store, self.sink = arbiter, store, sink
        self.metrics = metrics or Metrics()
        self.policy = policy or SafetyPolicy()
        self._attempted = set()
        self._native_actions = set()

    def observe(self, source="reflex", stable=False):
        context = self.sink.context(source, subgoal="observe") if self.sink else None
        value = self.arbiter.read(context)
        self.metrics.tree_reads += 1
        if stable:
            deadline = min(self.arbiter.deadline, time.monotonic() + 2)
            while time.monotonic() < deadline:
                events = self.arbiter.wait(value.event_sequence, 180)
                newer = self.arbiter.read(context)
                self.metrics.tree_reads += 1
                if newer.fingerprint == value.fingerprint and _observable_window(newer) and not events.get("changed", False):
                    value = newer
                    break
                value = newer
            else:
                self.arbiter.stop("unstable_accessibility_tree")
                raise Stopped("unstable_accessibility_tree")
        if value.excluded:
            self.arbiter.stop("sensitive_screen")
            raise Stopped("sensitive_screen")
        if not value.nodes or value.truncated:
            self.arbiter.stop("incomplete_accessibility_tree")
            raise Stopped("incomplete_accessibility_tree")
        if self.store:
            self.store.observe(value, source=source)
        return value

    def execute(self, action: Action, *, state=None, source="reflex", candidates=(), grant=None, verify_seconds=3):
        before = state or self.observe(source)
        started = time.monotonic()
        ident = action.id or str(uuid.uuid4())
        after = before
        performed = False
        status, reason = "uncertain", "unverified_effect"
        target = None
        try:
            self.arbiter.check()
            if action.kind in {"CLICK", "SCROLL"}:
                if action.selector is None:
                    raise LookupError("selector_required")
                target = resolve(before, action.selector, action.kind)
            allowed, reason = self.policy.allows(before, action, target, grant)
            if not allowed:
                return self._outcome(ident, "denied", before, before, reason, False, started, source, action)
            key = (before.fingerprint, str(action.selector), action.kind)
            if action.kind not in {"WAIT", "SCROLL"} and key in self._attempted:
                return self._outcome(ident, "uncertain", before, before, "repeated_operation", False, started, source, action)
            self._attempted.add(key)
            context = self.sink.context(source, tuple(candidates) or (Candidate(action, source=source),),
                                        subgoal=action.expected_text or action.kind) if self.sink else None
            if self.sink:
                self.sink.bind(ident, context)
            if action.kind == "WAIT":
                self.arbiter.wait(before.event_sequence, max(0, min(action.wait_ms, 2000)))
                after = self.observe(source, stable=True)
                status = "verified" if self._expect(action, after) and (action.expected_text or action.expected_page or after.fingerprint != before.fingerprint) else "unchanged"
                return self._outcome(ident, status, before, after, "wait_completed", False, started, source, action)
            kinds = {"CLICK": "tap_node", "SCROLL": "scroll_node", "BACK": "back"}
            args = {"nodeId": target.id, "snapshotId": before.snapshot_id} if target else {}
            if action.kind == "SCROLL":
                args["direction"] = action.direction
            if self.sink:
                self.sink.bind(ident, context)
            self._native_actions.add(ident)
            response = self.arbiter.execute(kinds[action.kind], args, ident, context)
            self.metrics.actions += 1
            if action.kind == "SCROLL":
                self.metrics.scrolls += 1
            if not response.get("ok"):
                reason = str(response.get("error", {}).get("code", "action_failed"))
                status = "uncertain" if reason in {"timeout", "outcome_unknown"} else "failed"
                if reason in _STOP_ERRORS:
                    self.arbiter.stop(reason)
                else:
                    after = self.observe(source)
                return self._outcome(ident, status, before, after, reason, False, started, source, action)
            performed = response.get("result", {}).get("performed", True) is not False
            deadline = min(self.arbiter.deadline, started + max(.1, min(verify_seconds, 8)))
            sequence = before.event_sequence
            while time.monotonic() < deadline:
                self.arbiter.wait(sequence, min(500, max(0, int((deadline - time.monotonic()) * 1000))))
                after = self.observe(source, stable=True)
                sequence = after.event_sequence
                if not _loading(after):
                    if action.kind == "SCROLL" and not performed and after.fingerprint == before.fingerprint:
                        status, reason = "unchanged", "scroll_boundary"
                        break
                    if after.fingerprint != before.fingerprint and self._expect(action, after):
                        status, reason = "verified", "observed_state_change"
                        break
                    if action.kind == "SCROLL" and not _loading(after) and not (action.expected_page or action.expected_text):
                        status, reason = "unchanged", "no_new_content"
                        break
                if not performed:
                    status, reason = "unchanged", "no_effect"
                    break
            else:
                status, reason = "uncertain", "verification_timeout"
        except LookupError as exc:
            status, reason = "uncertain", str(exc)
        except Stopped as exc:
            status, reason = "cancelled", exc.reason
        outcome = self._outcome(ident, status, before, after, reason, performed, started, source, action)
        if action.kind in {"CLICK", "BACK"} and status in {"uncertain", "failed"}:
            self.arbiter.stop(reason)
        return outcome

    @staticmethod
    def _expect(action, state):
        return (not action.expected_text or any(action.expected_text in n.label for n in state.visible)) and (
                not action.expected_page or state.page_fingerprint == action.expected_page)

    def _outcome(self, ident, status, before, after, reason, performed, started, source, action):
        result = Outcome(ident, status, before, after, reason, performed,
                         round((time.monotonic() - started) * 1000, 3), source)
        if status in {"uncertain", "failed", "cancelled"} and not self.arbiter.cancelled.is_set():
            self.arbiter.stop(reason)
        if self.store and action.kind in {"CLICK", "BACK"}:
            self.store.connect(before, after, action, result.verified, source)
        if self.sink and ident in self._native_actions:
            self.sink.verified(result)
        self._native_actions.discard(ident)
        return result
