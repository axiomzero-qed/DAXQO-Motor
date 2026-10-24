"""Thin shared Standard/XM adapter, driven by reported Android capabilities."""
from __future__ import annotations

from .models import State
from .perception import from_tree


class AdapterError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class AndroidAdapter:
    def __init__(self, client):
        self.client = client
        self.device_id = client.serial

    def capabilities(self):
        device = self.client.request("/api/device/status")
        try:
            motor = self.client.request("/api/motor/status")
        except Exception:
            motor = {"ok": False, "error": {"code": "motor_bridge_not_installed"}}
        return {"device": device, "motor": motor}

    def acquire(self, owner, ttl_ms, package, previous=None):
        body = {"owner": owner, "ttl_ms": ttl_ms, "expected_package": package}
        if previous:
            body.update(previous)
        value = self.client.request("/api/motor/lease", body)
        self.check(value)
        return {"lease_id": value["lease_id"], "generation": value["generation"], "owner": owner}

    def release(self, lease):
        value = self.client.request("/api/motor/release", {"lease_id": lease["lease_id"]})
        self.check(value)

    def cancel(self, reason="task_cancelled", lease=None):
        return self.client.request("/api/motor/cancel", {"reason": reason,
            **({"lease_id": lease["lease_id"]} if lease else {})})

    def read(self, lease=None, package="", context=None) -> State:
        import uuid
        response = self.perform("tree", {"fresh": True, **({"expectedPackage": package} if package else {})},
                                str(uuid.uuid4()), lease, context)
        self.check(response)
        return from_tree(response["result"], self.device_id)

    def events(self, sequence, timeout_ms=500):
        value = self.client.request("/api/motor/events?after=" + str(int(sequence)) +
                                    "&timeoutMs=" + str(max(0, min(2000, int(timeout_ms)))), timeout=4)
        self.check(value)
        return value

    def perform(self, kind, args, ident, lease=None, context=None):
        return self.client.request("/api/device/execute", {"id": ident, "action": kind, "args": args,
            **({"motor": lease} if lease else {}), **({"trajectory": context} if context else {})})

    @staticmethod
    def check(value):
        if not value.get("ok", False):
            raise AdapterError(str(value.get("error", {}).get("code", "bridge_rejected")))
