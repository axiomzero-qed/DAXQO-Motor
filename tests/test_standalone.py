"""Offline backend, installed CLI and USB setup remain independent of Collector."""
import contextlib
import importlib.abc
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from motor import cli
from motor.app_map import MapStore
from motor.engine import Motor
from motor.models import Action, Selector
from motor.perception import from_tree
from motor.plugins import load_adapter
from motor.transport import BridgeTransport, linux_transport, usb_transport


class BlockCollector(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "trajectory" or fullname.startswith("trajectory."):
            raise ImportError("Collector deliberately absent")


class StandaloneTests(unittest.TestCase):
    def test_standalone_android_perception_preserves_privacy_without_collector(self):
        blocker = BlockCollector()
        sys.meta_path.insert(0, blocker)
        try:
            state = from_tree({"packageName": "demo.app", "nodes": [
                {"id": "field", "editable": True, "text": "PRIVATE_INPUT", "bounds": [0, 0, 100, 100]},
                {"id": "title", "text": "Details", "bounds": [0, 100, 100, 160]}]}, "device")
            self.assertFalse(state.excluded)
            self.assertEqual(state.nodes[0].text, "")
            self.assertEqual(state.nodes[1].text, "Details")
            private = from_tree({"packageName": "com.tencent.mm", "nodes": [{"text": "PRIVATE_BODY"}]}, "device")
            self.assertTrue(private.excluded)
            self.assertEqual(private.nodes, ())
        finally:
            sys.meta_path.remove(blocker)

    def test_cli_and_custom_adapter_work_without_collector_or_bridge(self):
        blocker = BlockCollector()
        sys.meta_path.insert(0, blocker)
        try:
            with tempfile.TemporaryDirectory() as temporary:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    result = cli.main(["--adapter", "motor.demo:create_adapter", "--data", temporary, "doctor"])
                self.assertEqual(result, 0)
                self.assertFalse(json.loads(output.getvalue())["physical_device"])
                adapter = load_adapter("motor.demo:create_adapter")
                with MapStore(Path(temporary) / "map.sqlite3") as store:
                    motor = Motor(adapter, store)
                    result = motor.act("demo.app", Action("CLICK", Selector(text="Details")), source="foreign-harness")
                    self.assertTrue(result["ok"], result)
                    self.assertEqual(adapter.page, "details")
                self.assertFalse((Path(temporary) / "trajectories").exists())
        finally:
            sys.meta_path.remove(blocker)

    def test_usb_pairing_does_not_import_collector(self):
        class FakeAdb:
            def __init__(self, *_): self.calls = []
            def run(self, serial, *args):
                self.calls.append(args)
                return "tcp:unused" if "--remove" in args else ("package:io.daxqo.app" if "packages" in args else "32123")
        with patch("motor.transport.Adb", FakeAdb), patch.dict("os.environ", {"DAXQO_CAPABILITY_demo": "x" * 32}):
            with usb_transport("demo") as transport:
                self.assertEqual(transport.serial, "demo")
                self.assertEqual(transport.port, 32123)

    def test_explicit_stable_identity_needs_no_collector_status_endpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capability = root / "capability"
            capability.write_text("c" * 32)
            capability.chmod(0o600)
            with patch.object(BridgeTransport, "request") as request:
                with linux_transport(root, device_id="b683d45a-e298-40b3-90c2-3bcd8f59cb84"):
                    request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
