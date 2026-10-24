"""Device-independent public state/action contracts for rules and future scorers."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
from typing import Protocol


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Selector:
    resource_id: str = ""
    text: str = ""
    description: str = ""
    role: str = ""

    def public(self):
        return {k: v for k, v in asdict(self).items() if v}


@dataclass(frozen=True)
class Node:
    id: str
    parent_id: str = ""
    resource_id: str = ""
    text: str = ""
    description: str = ""
    role: str = "text"
    class_name: str = ""
    bounds: tuple = (0, 0, 0, 0)
    clickable: bool = False
    scrollable: bool = False
    editable: bool = False
    enabled: bool = True
    visible: bool = True
    selected: bool = False
    checked: bool = False
    checkable: bool = False
    actions: tuple = ()

    @property
    def label(self) -> str:
        return self.text or self.description

    def selector(self) -> Selector:
        # A text discriminator is required when an adapter repeats resource IDs.
        return Selector(self.resource_id, self.text, self.description if not self.text else "", self.role)


@dataclass(frozen=True)
class State:
    package: str
    device_id: str
    version: str
    snapshot_id: str
    nodes: tuple[Node, ...]
    width: int = 0
    height: int = 0
    event_sequence: int = 0
    activity: str = ""
    truncated: bool = False
    excluded: bool = False
    window: str = ""

    @property
    def visible(self) -> tuple[Node, ...]:
        return tuple(n for n in self.nodes if n.visible and n.bounds[2] > n.bounds[0]
                     and n.bounds[3] > n.bounds[1] and n.bounds[2] > 0 and n.bounds[3] > 0
                     and (not self.width or n.bounds[0] < self.width)
                     and (not self.height or n.bounds[1] < self.height))

    def under_scroll(self, node: Node) -> bool:
        by_id = {n.id: n for n in self.nodes}
        parent = node.parent_id
        visited = set()
        while parent and parent not in visited:
            visited.add(parent)
            ancestor = by_id.get(parent)
            if ancestor is None:
                break
            if ancestor.scrollable:
                return True
            parent = ancestor.parent_id
        return False

    @property
    def fingerprint(self) -> str:
        return digest([self.package, self.window, [(n.resource_id, n.role, n.label,
                       n.selected, n.checked, n.enabled, n.bounds) for n in self.visible]])

    @property
    def page_fingerprint(self) -> str:
        # No coordinates or device IDs. List rows are content, not new pages.
        fixed = [n for n in self.visible if not self.under_scroll(n)]
        structure = sorted(set((n.resource_id or (re.sub(r"\d+", "{n}", n.label) if n.clickable else ""), n.class_name, n.role, n.clickable,
                                n.scrollable) for n in self.nodes if not self.under_scroll(n)))
        labels = [(n.role, n.label if n.selected or n.checked else re.sub(r"\d+", "{n}", n.label)) for n in fixed if n.label and
                  (n.selected or n.checked or (not n.clickable and not n.scrollable and n.role != "container"))]
        return digest([self.package, self.window, structure, labels])

    @property
    def content(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(n.label for n in self.visible
                                   if n.label and not n.editable and self.under_scroll(n)))

    @property
    def title(self) -> str:
        return next((n.label for n in self.visible if n.label and not n.clickable
                     and not n.scrollable and not self.under_scroll(n)), self.package)

    def summary(self, max_labels=24):
        return {"schema_version": "daxqo.motor.state/0.1", "package": self.package,
                "version": self.version, "device_id": self.device_id,
                "fingerprint": self.fingerprint, "page_fingerprint": self.page_fingerprint,
                "title": self.title, "labels": list(dict.fromkeys(n.label for n in self.visible if n.label))[:max_labels],
                "controls": [n.selector().public() for n in self.visible if n.clickable or n.scrollable][:24],
                "truncated": self.truncated, "excluded": self.excluded}


@dataclass(frozen=True)
class Action:
    kind: str  # CLICK, SCROLL, BACK, WAIT
    selector: Selector | None = None
    direction: str = "forward"
    purpose: str = "navigation"  # navigation, observation, task
    expected_text: str = ""
    expected_page: str = ""
    wait_ms: int = 500
    id: str = ""

    def public(self):
        value = asdict(self)
        value["selector"] = self.selector.public() if self.selector else None
        return value


@dataclass(frozen=True)
class Candidate:
    action: Action
    score: float | None = None
    source: str = "reflex"
    confidence: float | None = None

    def public(self):
        return {"action": self.action.public(), "score": self.score,
                "source": self.source, "confidence": self.confidence}


@dataclass
class Outcome:
    action_id: str
    status: str  # verified, failed, uncertain, unchanged, cancelled, denied
    before: State
    after: State
    reason: str = ""
    performed: bool = False
    elapsed_ms: float = 0.0
    source: str = "reflex"

    @property
    def verified(self):
        return self.status == "verified"


@dataclass(frozen=True)
class Budget:
    seconds: float = 30
    max_scrolls: int = 8
    max_pages: int = 12
    max_depth: int = 2
    max_actions: int = 48
    no_progress_limit: int = 2
    restore_seconds: float = 15

    def __post_init__(self):
        if not 0 < self.seconds <= 110 or not 0 <= self.restore_seconds <= 30:
            raise ValueError("Exploration budget must be 0..110 seconds; restore 0..30 seconds")
        if not 0 <= self.max_scrolls <= 100 or not 1 <= self.max_pages <= 100 or not 0 <= self.max_depth <= 10:
            raise ValueError("Invalid exploration limits")
        if not 1 <= self.max_actions <= 500 or not 1 <= self.no_progress_limit <= 5:
            raise ValueError("Invalid action/progress limits")


@dataclass
class Metrics:
    elapsed_ms: float = 0
    cpu_ms: float = 0
    cloud_calls: int = 0
    tree_reads: int = 0
    actions: int = 0
    scrolls: int = 0
    pages: int = 0
    discovered_tabs: int = 0
    visited_tabs: int = 0
    unique_content: int = 0
    restored: bool = False
    restore_attempted: bool = False
    max_rss_kb: int | None = None

    def public(self):
        value = asdict(self)
        value["tab_coverage"] = (self.visited_tabs / self.discovered_tabs if self.discovered_tabs else None)
        value["restoration_success"] = self.restored if self.restore_attempted else None
        return value


class ActionScorer(Protocol):
    """Future System One: state + candidates -> ranked candidates; no side effects."""
    def score(self, state: State, candidates: tuple[Candidate, ...]) -> tuple[Candidate, ...]: ...
