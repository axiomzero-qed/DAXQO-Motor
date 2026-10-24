"""Sanitized policy input and validated ranking boundary for future scorers."""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import math
from typing import Iterable

from .models import Action, Candidate, Node, State


SCHEMA_VERSION = "daxqo.motor.policy/0.1"


def _action_data(action: Action) -> dict:
    value = action.public()
    value.pop("id", None)  # Per-execution transport identity is not an action identity.
    return value


def _action_key(action: Action) -> str:
    return json.dumps(_action_data(action), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _editable_target(state: State, action: Action) -> bool:
    selector = action.selector
    if selector is None:
        return False
    for node in state.nodes:
        if not node.editable:
            continue
        if selector.resource_id and selector.resource_id == node.resource_id:
            return True
        if not selector.resource_id and selector.role == node.role and selector.text == node.text:
            return True
    return False


def _safe_action(state: State, action: Action) -> Action:
    if not _editable_target(state, action):
        return action
    selector = action.selector
    return replace(action, selector=replace(selector, text="", description=""), expected_text="")


def _safe_state(state: State) -> State:
    nodes = tuple(replace(node, text="", description="") if node.editable else node for node in state.nodes)
    return replace(state, nodes=nodes)


def _normalized_bounds(node: Node, state: State):
    if state.width <= 0 or state.height <= 0:
        return None
    l, t, r, b = node.bounds
    return [max(0.0, min(1.0, l / state.width)),
            max(0.0, min(1.0, t / state.height)),
            max(0.0, min(1.0, r / state.width)),
            max(0.0, min(1.0, b / state.height))]


def prepare_input(state: State, candidates: Iterable[Candidate]) -> dict:
    """Build bounded, device-independent features; never expose editable values."""
    if state.excluded:
        raise ValueError("Excluded states cannot be sent to a scorer")
    controls = []
    for node in state.visible:
        if not (node.clickable or node.scrollable or node.checkable or node.selected or node.checked):
            continue
        controls.append({
            "resource_id": node.resource_id or "",
            "role": node.role or "",
            "text": "" if node.editable else node.label,
            "bounds": _normalized_bounds(node, state),
            "enabled": bool(node.enabled), "selected": bool(node.selected),
            "scrollable": bool(node.scrollable), "checkable": bool(node.checkable),
            "checked": bool(node.checked), "editable": bool(node.editable),
        })
    result = []
    seen = set()
    for candidate in candidates:
        if not isinstance(candidate, Candidate) or not isinstance(candidate.action, Action):
            raise TypeError("Candidates must contain Action values")
        _validate_candidate(candidate)
        key = _action_key(candidate.action)
        if key in seen:
            continue
        seen.add(key)
        action = _safe_action(state, candidate.action).public()
        action.pop("id", None)
        result.append({"action": action,
                       "source": candidate.source, "score": candidate.score,
                       "confidence": candidate.confidence})
    return {"schema_version": SCHEMA_VERSION,
            "page": {"package": state.package, "window": state.window,
                     "title": next((n.label for n in state.visible if n.label and not n.editable
                                    and not n.clickable and not n.scrollable), state.package)},
            "controls": controls, "candidates": result}


def _validate_candidate(candidate):
    if not isinstance(candidate, Candidate) or not isinstance(candidate.action, Action):
        raise TypeError("Scorer must return Candidate values")
    if candidate.score is not None:
        if type(candidate.score) not in (int, float) or not math.isfinite(candidate.score):
            raise ValueError("Candidate score must be finite")
    if candidate.confidence is not None:
        if (type(candidate.confidence) not in (int, float) or not math.isfinite(candidate.confidence)
                or not 0 <= candidate.confidence <= 1):
            raise ValueError("Candidate confidence must be finite and between 0 and 1")
    if not isinstance(candidate.source, str) or not candidate.source.strip():
        raise ValueError("Candidate source must be a non-empty string")


def validate_scores(original: Iterable[Candidate], ranked: Iterable[Candidate]) -> tuple[Candidate, ...]:
    """Require an exact permutation of original actions, with valid optional scores."""
    original, ranked = tuple(original), tuple(ranked)
    if len(original) != len(ranked):
        raise ValueError("Scorer must return every candidate exactly once")
    originals = {}
    for candidate in original:
        _validate_candidate(candidate)
        key = _action_key(candidate.action)
        if key in originals:
            raise ValueError("Original candidates contain duplicate actions")
        originals[key] = candidate
    seen = set()
    for candidate in ranked:
        _validate_candidate(candidate)
        key = _action_key(candidate.action)
        if key not in originals:
            raise ValueError("Scorer introduced an unknown action")
        if key in seen:
            raise ValueError("Scorer returned a duplicate action")
        seen.add(key)
    if seen != set(originals):
        raise ValueError("Scorer omitted an original candidate")
    return ranked


@dataclass(frozen=True)
class Decision:
    source: str
    chosen_action: dict | None
    candidate_actions: tuple[dict, ...]

    def public(self) -> dict:
        return {"source": self.source, "chosen_action": self.chosen_action,
                "candidate_actions": list(self.candidate_actions)}


def rank(state: State, candidates: Iterable[Candidate], scorer=None) -> Decision:
    """Return a policy decision only; execution remains owned by the arbiter."""
    candidates = tuple(candidates)
    if state.excluded:
        raise ValueError("Excluded states cannot be scored")
    if scorer is None:
        ranked = candidates
        source = "reflex"
    else:
        validate_scores(candidates, candidates)
        safe_state = _safe_state(state)
        safe_candidates = tuple(replace(candidate, action=_safe_action(state, candidate.action))
                                for candidate in candidates)
        scored = scorer.score(safe_state, safe_candidates)
        scored = validate_scores(safe_candidates, scored)
        by_key = {_action_key(safe.action): original for safe, original in zip(safe_candidates, candidates)}
        ranked = tuple(replace(by_key[_action_key(item.action)], score=item.score,
                               confidence=item.confidence, source=item.source) for item in scored)
        source = "system_one"
    if scorer is None:
        # Even the local path should not publish duplicate or sensitive action values.
        seen = set()
        for candidate in ranked:
            _validate_candidate(candidate)
            key = _action_key(candidate.action)
            if key in seen:
                raise ValueError("Original candidates contain duplicate actions")
            seen.add(key)
    public_candidates = tuple({"action": _safe_action(state, item.action).public(),
                               "score": item.score, "source": item.source,
                               "confidence": item.confidence} for item in ranked)
    chosen = public_candidates[0]["action"] if public_candidates else None
    return Decision(source=source, chosen_action=chosen, candidate_actions=public_candidates)
