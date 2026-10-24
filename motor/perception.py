"""Bounded accessibility perception. No pixels, OCR or cloud inference on hot paths."""
from __future__ import annotations

import re
from .models import Node, Selector, State


def from_tree(tree: dict, device_id: str, version: str = "") -> State:
    # A versioned, attributed policy snapshot keeps standalone operation safe
    # without requiring the Collector package or a second source checkout.
    from ._vendor.collector_privacy import has_sensitive_ui, is_sensitive_app, sanitize_nodes
    raw = tree.get("nodes", [])
    if not isinstance(raw, list):
        raise ValueError("Invalid accessibility tree")
    package = str(tree.get("packageName", ""))
    excluded = is_sensitive_app(package) or has_sensitive_ui(raw)
    safe = [] if excluded else sanitize_nodes(raw[:400])
    nodes = []
    for item in safe:
        cls = str(item.get("className", ""))
        role = str(item.get("role", item.get("roleDescription", ""))).casefold()
        if not role:
            role = ("input" if item.get("editable") else "scroll" if item.get("scrollable")
                    else "tab" if "tab" in cls.casefold() or "radiobutton" in cls.casefold()
                    else "button" if item.get("clickable") else "container" if "layout" in cls.casefold()
                    else "text")
        bounds = item.get("bounds", [0, 0, 0, 0])
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
            bounds = (0, 0, 0, 0)
        try:
            bounds = tuple(int(x) for x in bounds)
        except (ValueError, TypeError):
            bounds = (0, 0, 0, 0)
        nodes.append(Node(id=str(item.get("id", "")), parent_id=str(item.get("parentId", "")),
                     resource_id=str(item.get("viewId", "")), text=str(item.get("text", "")),
                     description=str(item.get("description", "")), role=role, class_name=cls,
                     bounds=bounds, clickable=item.get("clickable") is True,
                     scrollable=item.get("scrollable") is True, editable=item.get("editable") is True,
                     enabled=item.get("enabled", True) is True, visible=item.get("visible", True) is True,
                     selected=item.get("selected") is True, checked=item.get("checked") is True,
                     checkable=item.get("checkable") is True, actions=tuple(item.get("actions", []))))
    return State(package=package, device_id=device_id, version=str(tree.get("packageVersion", version)),
                 snapshot_id=str(tree.get("snapshotId", "")), nodes=tuple(nodes),
                 width=int(tree.get("width", 0)), height=int(tree.get("height", 0)),
                 event_sequence=int(tree.get("eventSequence", 0)), truncated=bool(tree.get("truncated")),
                 excluded=excluded, window=str(tree.get("windowType", "")))


def resolve(state: State, selector: Selector, kind="CLICK") -> Node:
    if not any((selector.resource_id, selector.text, selector.description, selector.role)):
        raise LookupError("empty_selector")
    candidates = []
    for node in state.visible:
        if not node.enabled or node.editable:
            continue
        if kind == "CLICK" and not node.clickable:
            continue
        if kind == "SCROLL" and not node.scrollable:
            continue
        if selector.resource_id and node.resource_id != selector.resource_id:
            continue
        if selector.text and node.text != selector.text:
            continue
        if selector.description and node.description != selector.description:
            continue
        if selector.role and node.role != selector.role:
            continue
        candidates.append(node)
    if len(candidates) != 1:
        raise LookupError("target_missing" if not candidates else "ambiguous_target")
    return candidates[0]


def tabs(state: State) -> tuple[Node, ...]:
    candidates = []
    by_id = {n.id: n for n in state.nodes}
    for n in state.visible:
        if not n.clickable or not n.enabled or not n.label or n.editable or state.under_scroll(n):
            continue
        parent = by_id.get(n.parent_id)
        semantics = " ".join((n.resource_id, n.role, n.description,
                              parent.resource_id if parent else "", parent.description if parent else "")).casefold()
        if (n.role == "tab" or re.search(r"(?:tab[_:/-]|[_:/-]tab|nav[_:/-]|navigation|导航|标签)", semantics)
                or (n.checkable and n.bounds[1] >= state.height * .65)):
            candidates.append(n)
    # No inferred click from merely being at the bottom of a screen.
    return tuple(candidates[:12]) if len(candidates) >= 2 else ()


def scrollables(state: State) -> tuple[Node, ...]:
    return tuple(sorted((n for n in state.visible if n.scrollable and n.enabled),
                        key=lambda n: (n.bounds[2] - n.bounds[0]) * (n.bounds[3] - n.bounds[1]), reverse=True))


def popup_close(state: State) -> Node | None:
    return next((n for n in state.visible if n.clickable and n.enabled and
                 n.label.strip().casefold() in {"关闭", "取消", "稍后", "close", "cancel", "not now"}), None)
