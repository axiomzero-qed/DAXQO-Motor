"""Budgeted local exploration, incremental list reading and bounded restoration."""
from __future__ import annotations

from dataclasses import asdict
import re
import time

from .arbiter import Stopped
from .models import Action, Budget, Metrics
from .perception import popup_close, resolve, scrollables, tabs


class UIScout:
    def __init__(self, reflex, budget=Budget(), *, mode="quick", goal="", follow_tabs=True):
        if mode not in {"quick", "deep"}:
            raise ValueError("Scout mode must be quick or deep")
        self.reflex, self.budget, self.mode, self.goal = reflex, budget, mode, goal
        self.follow_tabs = follow_tabs
        self.metrics = reflex.metrics
        self.pages = {}
        self.edges = []
        self._seen_targets = set()
        self._nav_history = []
        self._start = time.monotonic()
        self._deadline = self._start + budget.seconds
        self._main_tabs = ()
        self._tab_seen = set()
        self._tab_visited = set()
        self._stop = "completed"
        self._last = None

    def _allowed(self):
        if time.monotonic() >= self._deadline:
            self._stop = "time_budget"
            return False
        if self.metrics.actions >= self.budget.max_actions:
            self._stop = "action_budget"
            return False
        self.reflex.arbiter.check()
        return True

    @staticmethod
    def _key(node):
        return str(node.selector().public())

    def _remember(self, state, end=""):
        self._last = state
        key = state.page_fingerprint
        page = self.pages.setdefault(key, {"page_fingerprint": key, "title": state.title,
                    "labels": [], "content": [], "controls": [], "stop_reason": "", "task_matches": []})
        for label in dict.fromkeys(n.label for n in state.visible if n.label and not n.editable):
            if label not in page["labels"] and len(page["labels"]) < 48:
                page["labels"].append(label)
        for label in state.content:
            if label not in page["content"] and len(page["content"]) < 200:
                page["content"].append(label)
        for n in state.visible:
            if not (n.clickable or n.scrollable) or n.editable:
                continue
            selector = n.selector().public()
            if selector not in page["controls"] and len(page["controls"]) < 16:
                page["controls"].append(selector)
            if self._relevant(n.label) and n.label not in page["task_matches"]:
                page["task_matches"].append(n.label)
        if end:
            page["stop_reason"] = end
        if self.reflex.store and hasattr(self.reflex.store, "remember_content"):
            self.reflex.store.remember_content(state, state.content)

    def _relevant(self, label):
        if not self.goal or not label:
            return False
        normalize = lambda s: re.sub(r"[\s，,。.:：;；·]+", "", s).casefold()
        goal, text = normalize(self.goal), normalize(label)
        return len(text) >= 2 and (text in goal or (len(goal) >= 2 and goal in text))

    def _move(self, action, state):
        if not self._allowed():
            return None
        if action.kind == "CLICK" and len(self.pages) >= self.budget.max_pages:
            self._stop = "page_budget"
            return None
        outcome = self.reflex.execute(action, state=state, source="scout")
        self._last = outcome.after
        if outcome.status in {"cancelled", "uncertain", "failed"}:
            raise Stopped(outcome.reason)
        if outcome.status == "denied":
            return None
        if outcome.verified:
            self.edges.append({"from": state.page_fingerprint, "to": outcome.after.page_fingerprint,
                               "action": action.public(), "verified": True})
        return outcome

    def _scan(self, state):
        self._remember(state)
        local_scrolls, repeats = 0, 0
        seen = set(state.content)
        while self._allowed():
            areas = scrollables(state)
            if not areas:
                self._remember(state, "no_scrollable_region")
                break
            if self.metrics.scrolls >= self.budget.max_scrolls or (self.mode == "quick" and local_scrolls >= 1):
                self._remember(state, "scroll_budget")
                break
            action = Action("SCROLL", areas[0].selector(), purpose="observation")
            outcome = self._move(action, state)
            if outcome is None:
                break
            state = outcome.after
            local_scrolls += 1
            new = set(state.content) - seen
            seen.update(new)
            self._remember(state)
            repeats = repeats + 1 if not new else 0
            if outcome.reason == "scroll_boundary":
                # A deferred/lazy list may append after its first end response.
                self.reflex.arbiter.wait(state.event_sequence, 350)
                later = self.reflex.observe("scout")
                if set(later.content) - seen:
                    state = later
                    seen.update(later.content)
                    self._remember(state)
                    repeats = 0
                    continue
                self._remember(later, "observed_boundary")
                state = later
                break
            if repeats >= self.budget.no_progress_limit:
                self._remember(state, "no_new_content")
                break
        return state

    def _visit(self, state, depth=0):
        state = self._scan(state)
        if self.mode != "deep" or depth >= self.budget.max_depth:
            return state
        candidates = [n for n in state.visible if n.clickable and self._relevant(n.label) and
                      n not in tabs(state) and self.reflex.policy.allows(state, Action("CLICK", n.selector()), n)[0]]
        for node in candidates[:4]:
            ident = (state.page_fingerprint, self._key(node))
            if ident in self._seen_targets or not self._allowed():
                continue
            self._seen_targets.add(ident)
            original = state
            outcome = self._move(Action("CLICK", node.selector()), state)
            if not outcome or not outcome.verified:
                continue
            self._nav_history.append(original)
            state = self._visit(outcome.after, depth + 1)
            back = self._move(Action("BACK", expected_page=original.page_fingerprint), state)
            if back is None or not back.verified:
                # Leave the entry on the stack for final restoration.
                break
            self._nav_history.pop()
            state = back.after
            self._remember(state)
        return state

    def _subtabs(self, state):
        main_keys = {self._key(n) for n in self._main_tabs}
        children = [n for n in tabs(state) if self._key(n) not in main_keys]
        if len(children) < 2:
            return state
        original = next((n for n in children if n.selected or n.checked), None)
        if original is None:
            return state
        for tab in children:
            self._tab_seen.add(self._key(tab))
            if tab.selected or tab.checked:
                self._tab_visited.add(self._key(tab))
                continue
            if not self._allowed():
                break
            try:
                node = resolve(state, tab.selector())
            except LookupError:
                break
            outcome = self._move(Action("CLICK", node.selector()), state)
            if outcome and outcome.verified:
                self._tab_visited.add(self._key(tab))
                state = self._visit(outcome.after)
        if self._allowed():
            try:
                node = resolve(state, original.selector())
                if not (node.selected or node.checked):
                    outcome = self._move(Action("CLICK", node.selector()), state)
                    if outcome:
                        state = outcome.after
            except LookupError:
                pass
        return state

    def _restore(self, initial, state):
        self.metrics.restore_attempted = True
        deadline = min(self.reflex.arbiter.deadline, time.monotonic() + self.budget.restore_seconds)
        for ancestor in reversed(self._nav_history):
            if time.monotonic() >= deadline:
                break
            outcome = self.reflex.execute(Action("BACK", expected_page=ancestor.page_fingerprint),
                                           state=state, source="reflex")
            state = outcome.after
            if not outcome.verified:
                return state, outcome.reason
        initial_selected = [n for n in tabs(initial) if n.selected or n.checked]
        # Outer navigation first; nested tabs only exist on their own parent page.
        initial_selected.sort(key=lambda n: n.bounds[1], reverse=True)
        for tab in initial_selected:
            if time.monotonic() >= deadline:
                break
            try:
                node = resolve(state, tab.selector())
            except LookupError:
                continue
            if not (node.selected or node.checked):
                outcome = self.reflex.execute(Action("CLICK", tab.selector()), state=state, source="reflex")
                state = outcome.after
                if not outcome.verified:
                    return state, outcome.reason
        seen = set()
        while state.fingerprint != initial.fingerprint and time.monotonic() < deadline:
            self.reflex.arbiter.check()
            if state.page_fingerprint != initial.page_fingerprint or state.fingerprint in seen:
                break
            seen.add(state.fingerprint)
            areas = scrollables(state)
            if not areas:
                break
            outcome = self.reflex.execute(Action("SCROLL", areas[0].selector(), direction="backward", purpose="observation"),
                                           state=state, source="reflex")
            state = outcome.after
            if outcome.status not in {"verified", "unchanged"}:
                return state, outcome.reason
            if outcome.status == "unchanged":
                break
        restored = state.fingerprint == initial.fingerprint
        self.metrics.restored = restored
        return state, "restored" if restored else "exact_scroll_or_page_not_restored"

    def run(self):
        initial, current = None, None
        restoration = "not_started"
        escalation = None
        try:
            initial = current = self.reflex.observe("scout", stable=True)
            self._last = initial
            close = popup_close(current)
            if close and ("dialog" in current.window.casefold() or any("Dialog" in n.class_name for n in current.nodes)):
                outcome = self._move(Action("CLICK", close.selector()), current)
                if outcome:
                    current = outcome.after
            self._main_tabs = tabs(current) if self.follow_tabs else ()
            self._tab_seen.update(self._key(n) for n in self._main_tabs)
            self._tab_visited.update(self._key(n) for n in self._main_tabs if n.selected or n.checked)
            current = self._visit(current)
            current = self._subtabs(current) if self.follow_tabs else current
            for tab in self._main_tabs:
                if self._key(tab) in self._tab_visited or not self._allowed():
                    continue
                try:
                    target = resolve(current, tab.selector())
                except LookupError:
                    raise Stopped("navigation_controls_disappeared") from None
                outcome = self._move(Action("CLICK", target.selector()), current)
                if outcome and outcome.verified:
                    self._tab_visited.add(self._key(tab))
                    current = self._visit(outcome.after)
                    current = self._subtabs(current)
        except Stopped as exc:
            self._stop = exc.reason
            escalation = {"reason": exc.reason, "required": True}
        except (KeyboardInterrupt, SystemExit):
            self.reflex.arbiter.stop("user_cancelled")
            raise
        finally:
            current = self._last or current
            if initial and current and not self.reflex.arbiter.cancelled.is_set():
                try:
                    current, restoration = self._restore(initial, current)
                except Stopped as exc:
                    restoration = exc.reason
            elif initial:
                restoration = "stopped_for_takeover_or_uncertainty"
            self.metrics.pages = len(self.pages)
            self.metrics.discovered_tabs = len(self._tab_seen)
            self.metrics.visited_tabs = len(self._tab_visited)
            self.metrics.unique_content = len({v for p in self.pages.values() for v in p["content"]})
        if restoration != "restored" and initial:
            escalation = escalation or {"reason": restoration, "required": True}
        differences = []
        if initial and current and not self.metrics.restored:
            original_nodes = {n.id: n for n in initial.nodes}
            current_nodes = {n.id: n for n in current.nodes}
            for ident in original_nodes.keys() | current_nodes.keys():
                before, after = original_nodes.get(ident), current_nodes.get(ident)
                fields = ("label", "bounds", "visible", "enabled", "selected", "checked")
                changed = {k: {"before": getattr(before, k, None), "after": getattr(after, k, None)}
                           for k in fields if getattr(before, k, None) != getattr(after, k, None)}
                if changed:
                    differences.append({"node": ident, "changes": changed})
                if len(differences) >= 8:
                    break
        return {"schema_version": "daxqo.motor.scout/0.1", "mode": self.mode, "goal": self.goal, "stop_reason": self._stop,
                "pages": list(self.pages.values()), "edges": self.edges[:48],
                "restoration": {"status": restoration, "exact": self.metrics.restored,
                                "initial_page": initial.page_fingerprint if initial else None,
                                "current_page": current.page_fingerprint if current else None,
                                "differences": differences},
                "current": current.summary() if current else None, "escalation": escalation,
                "metrics": self.metrics.public()}
