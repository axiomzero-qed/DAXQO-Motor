import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from motor.trajectory import CollectorSink, MissingCollector, load_collector
from motor.models import Action, Candidate, Outcome, Selector, State


class Client:
    def __init__(self, fail=False, response=None):
        self.calls = []
        self.fail = fail
        self.response = response

    def request(self, path, body=None):
        self.calls.append((path, body))
        if self.fail:
            raise OSError("offline")
        return self.response or {"ok": True}


def outcome(action_id="action-1", status="verified"):
    state = State("pkg", "device", "1", "snapshot", ())
    return Outcome(action_id, status, state, state, performed=True)


class CollectorSinkTests(unittest.TestCase):
    def test_context_and_feedback_link_action_id_and_default_claim(self):
        client = Client()
        sink = CollectorSink(client, {"id": "task-1", "user_task": "browse"})
        candidate = Candidate(Action("CLICK", Selector(text="Continue")))
        context = sink.context("reflex", (candidate,), "open detail")
        self.assertEqual(context["task"]["subgoal"], "open detail")
        self.assertEqual(context["decision"]["source"], "reflex")
        self.assertIsNone(context["decision"]["model"])
        self.assertEqual(context["decision"]["candidate_actions"], [candidate.public()])
        sink.bind("action-1", context)
        sink.context("reflex", (), "later observation")
        sink.verified(outcome())
        path, body = client.calls[-1]
        self.assertEqual(path, "/api/trajectory/feedback")
        self.assertEqual(body["action_id"], "action-1")
        self.assertEqual(body["verification"], {"status": "verified", "source": "agent_claim"})

    def test_failed_outcome_is_preserved_as_unverified_not_success(self):
        client = Client()
        CollectorSink(client, {"id": "t"}).verified(outcome(status="failed"))
        self.assertEqual(client.calls[0][1]["verification"]["status"], "unverified")

    def test_explicit_enable_and_request_failures_are_reported(self):
        client = Client(fail=True)
        sink = CollectorSink(client, {"id": "t"})
        self.assertIsNone(sink.enable(["example.app"]))
        self.assertIn("/api/trajectory/config", sink.report()["errors"][0])

    def test_http_success_with_application_error_is_reported_and_disable_exists(self):
        client = Client(response={"ok": False, "error": {"code": "collector_offline"}})
        sink = CollectorSink(client, {"id": "t"})
        self.assertIsNone(sink.disable())
        self.assertIn("collector_offline", sink.report()["errors"][0])

    def test_sync_without_explicit_storage_does_not_create_cwd_data(self):
        sink = CollectorSink(Client(), {"id": "t"})
        self.assertIsNone(sink.sync())
        self.assertIn("provide dataset or data_dir", sink.report()["errors"][0])

    def test_close_only_closes_owned_dataset(self):
        external = type("External", (), {"close": lambda self: setattr(self, "closed", True)})()
        sink = CollectorSink(Client(), {"id": "t"}, external)
        sink.close()
        self.assertFalse(hasattr(external, "closed"))
        owned = type("Owned", (), {"close": lambda self: setattr(self, "closed", True)})()
        sink = CollectorSink(Client(), {"id": "t"}, owned)
        sink._owns_dataset = True
        sink.close()
        self.assertTrue(owned.closed)

    def test_sync_delegates_to_collector_sync_device(self):
        client, dataset = Client(), object()
        sink = CollectorSink(client, {"id": "t"}, dataset)
        with patch("motor.trajectory.load_collector"), patch(
                "motor.trajectory.importlib.import_module") as imported:
            sync = imported.return_value.sync_device
            sync.return_value = {"through": 4}
            self.assertEqual(sink.sync(), {"through": 4})
            sync.assert_called_once_with(client, dataset)

    def test_load_collector_reports_missing_path(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch("motor.trajectory.importlib.import_module", side_effect=ImportError):
                with self.assertRaises(MissingCollector):
                    load_collector(Path(temp))

    def test_load_collector_prefers_installed_import(self):
        marker = object()
        with patch("motor.trajectory.importlib.import_module", return_value=marker) as imported:
            self.assertIs(load_collector(Path("/does/not/exist")), marker)
            imported.assert_called_once_with("trajectory")


if __name__ == "__main__":
    unittest.main()
