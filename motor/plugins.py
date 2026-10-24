"""Public device adapter contract and explicit, local plugin loading.

Factories are trusted Python code configured by the operator, never by a tool
call. The default Android adapter and external backends use the same arbiter.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import State


@runtime_checkable
class DeviceAdapter(Protocol):
    device_id: str

    def capabilities(self) -> dict: ...
    def acquire(self, owner: str, ttl_ms: int, package: str, previous=None) -> dict: ...
    def release(self, lease: dict) -> None: ...
    def cancel(self, reason: str = "task_cancelled", lease=None) -> dict: ...
    def read(self, lease=None, package: str = "", context=None) -> State: ...
    def events(self, sequence: int, timeout_ms: int = 500) -> dict: ...
    def perform(self, kind: str, args: dict, ident: str, lease=None, context=None) -> dict: ...


def load_adapter(factory: str, config_file: str | Path | None = None) -> DeviceAdapter:
    """Load ``module:factory`` without inspecting any sibling checkout."""
    module, separator, name = factory.partition(":")
    if not separator or not module or not name.isidentifier():
        raise ValueError("Adapter must be a trusted local module:factory")
    config = json.loads(Path(config_file).read_text(encoding="utf-8")) if config_file else {}
    if not isinstance(config, dict):
        raise ValueError("Adapter configuration must be a JSON object")
    builder = getattr(importlib.import_module(module), name)
    adapter = builder(**config)
    methods = ("capabilities", "acquire", "release", "cancel", "read", "events", "perform")
    if (not isinstance(getattr(adapter, "device_id", None), str) or not adapter.device_id
            or any(not callable(getattr(adapter, method, None)) for method in methods)):
        raise TypeError("Adapter must implement motor.plugins.DeviceAdapter")
    return adapter
