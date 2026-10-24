"""Small deterministic accessibility adapter for Motor core regression tests."""
from __future__ import annotations

import threading

from motor.models import Node, State


class FakeError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class FakeAdapter:
    def __init__(self, *, tabs=False, chunks=None, repeat_content=False, details=False,
                 dangerous=False, ambiguous=False, no_effect=False, block_events=False):
        self.tabs = tabs
        self.chunks = chunks or []
        self.repeat_content = repeat_content
        self.details = details
        self.dangerous = dangerous
        self.ambiguous = ambiguous
        self.no_effect = no_effect
        self.block_events = block_events
        self.tab_index = 0
        self.screen = "home"
        self.stack = []
        self.scroll_index = 0
        self.event_sequence = 0
        self.perform_calls = []
        self.cancel_calls = []
        self._lock = threading.Lock()
        self._lease = None
        self.wait_entered = threading.Event()
        self.release_wait = threading.Event()

    def capabilities(self):
        return {"ok": True}

    def acquire(self, owner, ttl_ms, package, previous=None):
        with self._lock:
            if self._lease is not None and (not previous or previous.get("lease_id") != self._lease["lease_id"]):
                raise FakeError("motor_busy")
            if self._lease is None:
                self._lease = {"lease_id": owner, "generation": 1, "owner": owner}
            return dict(self._lease)

    def release(self, lease):
        with self._lock:
            if self._lease and self._lease["lease_id"] == lease.get("lease_id"):
                self._lease = None

    def cancel(self, reason, lease=None):
        self.cancel_calls.append(reason)
        self.release_wait.set()
        if lease is not None:
            self.release(lease)
        return {"ok": True}

    def read(self, lease, package, context=None):
        return self.state()

    def events(self, sequence, timeout_ms=500):
        self.wait_entered.set()
        if self.block_events:
            self.release_wait.wait(3)
        self.event_sequence += 1
        return {"generation": 1, "sequence": self.event_sequence}

    def perform(self, kind, args, ident, lease, context=None):
        self.perform_calls.append((kind, dict(args), ident, context))
        performed = True
        if kind == "tap_node" and not self.no_effect:
            node = next((n for n in self.state().nodes if n.id == args.get("nodeId")), None)
            if node and node.role == "tab":
                self.tab_index = int(node.id.split("_")[-1])
                self.scroll_index = 0
            elif node and node.label == "Details":
                self.stack.append((self.screen, self.tab_index))
                self.screen = "detail"
            elif node and node.label == "Delete":
                self.screen = "deleted"
        elif kind == "scroll_node" and not self.no_effect:
            if args.get("direction") == "backward" and self.scroll_index > 0:
                self.scroll_index -= 1
            elif args.get("direction") == "backward":
                performed = False
            elif self.repeat_content:
                self.scroll_index += 1
            elif self.chunks and self.scroll_index + 1 < len(self.chunks):
                self.scroll_index += 1
            else:
                performed = False
        elif kind == "back" and self.stack:
            self.screen, self.tab_index = self.stack.pop()
        elif kind == "back" and self.tabs:
            self.tab_index = 0
        self.event_sequence += 1
        return {"ok": True, "result": {"performed": performed}}

    def state(self):
        nodes = []
        if self.screen != "detail" and self.tabs:
            for index in range(4):
                nodes.append(Node(f"tab_{index}", resource_id=f"tab_{index}", text=f"Tab {index + 1}",
                                  role="tab", bounds=(index * 100, 900, index * 100 + 90, 980),
                                  clickable=True, checkable=True, checked=index == self.tab_index))
        title = "Detail page" if self.screen == "detail" else f"Home {self.tab_index}"
        nodes.append(Node("title", text=title, role="text", bounds=(10, 10, 400, 60)))
        if self.chunks or self.repeat_content:
            nodes.append(Node("list", resource_id="list", role="scroll", class_name="ScrollView",
                              bounds=(0, 100, 500, 800), scrollable=True))
            labels = (["same", "same"] if self.repeat_content else
                      self.chunks[min(self.scroll_index, len(self.chunks) - 1)])
            for index, label in enumerate(labels):
                nodes.append(Node(f"item_{index}", parent_id="list", text=label,
                                  role="text", bounds=(10, 120 + index * 50, 450, 160 + index * 50)))
            if self.repeat_content:
                nodes.append(Node("phase", text=f"phase {self.scroll_index}", bounds=(5, 70, 200, 95)))
        if self.details and self.screen == "home":
            nodes.append(Node("details", text="Details", role="button", clickable=True,
                              bounds=(10, 200, 200, 250)))
        if self.dangerous:
            nodes.append(Node("delete", text="Delete", role="button", clickable=True,
                              bounds=(10, 300, 200, 350)))
        if self.ambiguous:
            nodes.extend((Node("dup1", resource_id="duplicate", text="Details", role="button", clickable=True,
                               bounds=(10, 400, 200, 450)),
                          Node("dup2", resource_id="duplicate", text="Details", role="button", clickable=True,
                               bounds=(10, 460, 200, 510))))
        return State("fixture.app", "fake-device", "1", f"snapshot-{self.event_sequence}", tuple(nodes),
                     width=500, height=1000, event_sequence=self.event_sequence)
