"""Synthetic in-memory backend for setup and protocol checks; no phone I/O."""
from __future__ import annotations

import threading
import uuid

from .adapter import AdapterError
from .models import Node, State


class DemoAdapter:
    device_id = "b683d45a-e298-40b3-90c2-3bcd8f59cb84"

    def __init__(self, package="demo.app"):
        self.package, self.page, self.sequence = package, "home", 0
        self._lease = None
        self._lock = threading.RLock()

    def capabilities(self):
        return {"ok": True, "backend": "synthetic-demo", "physical_device": False}

    def acquire(self, owner, ttl_ms, package, previous=None):
        with self._lock:
            if package != self.package:
                raise AdapterError("foreground_changed")
            if self._lease and (not previous or previous.get("lease_id") != self._lease["lease_id"]):
                raise AdapterError("motor_busy")
            if not self._lease:
                self._lease = {"lease_id": str(uuid.uuid4()), "owner": owner, "generation": 1}
            return dict(self._lease)

    def _check(self, lease):
        if not self._lease or not lease or lease.get("lease_id") != self._lease["lease_id"]:
            raise AdapterError("lease_lost")

    def release(self, lease):
        with self._lock:
            if self._lease and lease.get("lease_id") == self._lease["lease_id"]:
                self._lease = None

    def cancel(self, reason="task_cancelled", lease=None):
        with self._lock:
            if lease:
                self.release(lease)
            else:
                self._lease = None
            return {"ok": True}

    def read(self, lease=None, package="", context=None):
        with self._lock:
            self._check(lease)
            nodes = [Node("title", text="Demo home" if self.page == "home" else "Demo details",
                          bounds=(0, 0, 400, 80))]
            if self.page == "home":
                nodes.append(Node("details", text="Details", role="button", clickable=True,
                                  bounds=(20, 100, 200, 160)))
            return State(self.package, self.device_id, "1", str(self.sequence), tuple(nodes),
                         width=400, height=800, event_sequence=self.sequence)

    def events(self, sequence, timeout_ms=500):
        return {"generation": 1, "sequence": self.sequence}

    def perform(self, kind, args, ident, lease=None, context=None):
        with self._lock:
            self._check(lease)
            if kind == "tap_node" and args.get("nodeId") == "details" and self.page == "home":
                self.page = "details"
            elif kind == "back":
                self.page = "home"
            else:
                return {"ok": False, "error": {"code": "unsupported_demo_action"}}
            self.sequence += 1
            return {"ok": True, "result": {"performed": True}}


def create_adapter(**config):
    return DemoAdapter(**config)
