"""Optional adapter to the separately installed DAXQO trajectory collector."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path


class MissingCollector(ImportError):
    """Raised when optional trajectory collection is requested but unavailable."""


def load_collector(path=None):
    """Use an installed collector or an explicitly configured source root."""
    try:
        return importlib.import_module("trajectory")
    except ImportError:
        pass
    if path is None:
        raise MissingCollector("Install daxqo-trajectory-collector to enable collection, or pass --collector-root")
    root = Path(path).expanduser().resolve()
    if not (root / "trajectory" / "__init__.py").is_file():
        raise MissingCollector(f"Trajectory Collector not found at {root}; install it or pass --collector-root")
    value = str(root)
    if value not in sys.path:
        sys.path.insert(0, value)
    sys.modules.pop("trajectory", None)
    try:
        return importlib.import_module("trajectory")
    except ImportError as exc:
        raise MissingCollector(f"Cannot import trajectory from {root}: {type(exc).__name__}") from None


class CollectorSink:
    """Best-effort Motor-to-Collector bridge; failures are retained for inspection."""

    def __init__(self, client, task: dict, dataset=None, collector_root=None, data_dir=None):
        self.client = client
        self.task = dict(task)
        self.dataset = dataset
        self.collector_root = collector_root
        self.data_dir = Path(data_dir) if data_dir is not None else None
        self.errors: list[str] = []
        self._decisions: dict[str, dict] = {}

    @staticmethod
    def _candidate(candidate):
        public = getattr(candidate, "public", None)
        return public() if callable(public) else candidate

    def context(self, source: str, candidates: tuple = (), subgoal="") -> dict:
        task = {**self.task, "subgoal": subgoal}
        decision = {"source": source, "model": None, "tool_call_id": None,
                    "candidate_actions": [self._candidate(c) for c in candidates], "confidence": None}
        return {"task": task, "decision": decision}

    def bind(self, action_id, context):
        if not isinstance(action_id, str) or not action_id:
            raise ValueError("action_id is required")
        self._decisions[action_id] = context if isinstance(context, dict) else {"task": self.task, "decision": {}}

    def enable(self, allowed_packages, screenshots=False):
        body = {"enabled": True, "allowed_packages": list(allowed_packages),
                "screenshots": bool(screenshots), "task": self.task}
        return self._request("/api/trajectory/config", body)

    def disable(self):
        return self._request("/api/trajectory/config", {"enabled": False})

    def verified(self, outcome, verification_source="agent_claim") -> None:
        context = self._decisions.pop(outcome.action_id, {"task": self.task, "decision": {}})
        payload = {"action_id": outcome.action_id, "task": context.get("task", self.task),
                   "decision": context.get("decision", {}),
                   "verification": {"status": "verified" if outcome.verified else "unverified",
                                    "source": verification_source}}
        self._request("/api/trajectory/feedback", payload)

    def _request(self, path, body):
        try:
            result = self.client.request(path, body)
            if isinstance(result, dict) and result.get("ok") is False:
                error = result.get("error")
                code = error.get("code", "request_rejected") if isinstance(error, dict) else "request_rejected"
                self.errors.append(f"{path}: {code}")
                return None
            return result
        except Exception as exc:
            self.errors.append(f"{path}: {type(exc).__name__}")
            return None

    def sync(self):
        if self.dataset is None:
            if self.data_dir is None:
                self.errors.append("collector sync unavailable: provide dataset or data_dir")
                return None
            try:
                load_collector(self.collector_root)
                dataset_module = importlib.import_module("trajectory.dataset")
                self.dataset = dataset_module.Dataset(self.data_dir)
                self._owns_dataset = True
            except Exception as exc:
                self.errors.append(f"collector sync unavailable: {type(exc).__name__}")
                return None
        try:
            load_collector(self.collector_root)
            sync_device = importlib.import_module("trajectory.collector").sync_device
            return sync_device(self.client, self.dataset)
        except Exception as exc:
            self.errors.append(f"collector sync failed: {type(exc).__name__}")
            return None

    def report(self):
        return {"errors": list(self.errors)}

    def close(self):
        if getattr(self, "_owns_dataset", False) and self.dataset is not None:
            self.dataset.close()
            self.dataset = None
            self._owns_dataset = False
