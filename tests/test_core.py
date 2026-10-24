"""Core safety, bounded exploration, cancellation, and shared-map regressions."""
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from motor.app_map import MapStore
from motor.arbiter import ActionArbiter, Stopped
from motor.engine import Motor
from motor.models import Action, Budget, Metrics, Selector
from motor.reflex import ReflexEngine
from tests.fakes import FakeAdapter


class MotorCoreTests(unittest.TestCase):
    def test_cli_interrupt_does_not_restore_or_click_again(self):
        adapter = FakeAdapter(tabs=True)
        original_events = adapter.events
        def interrupt_after_two_actions(sequence, timeout_ms=500):
            if len(adapter.perform_calls) >= 2:
                raise KeyboardInterrupt()
            return original_events(sequence, timeout_ms)
        adapter.events = interrupt_after_two_actions
        with MapStore(":memory:") as store:
            with self.assertRaises(KeyboardInterrupt):
                Motor(adapter, store).scout("fixture.app", budget=Budget(seconds=4, max_scrolls=0))
        self.assertEqual(len(adapter.perform_calls), 2)
        self.assertIsNone(adapter._lease)

    def test_timeout_releases_native_ownership_immediately(self):
        adapter = FakeAdapter()
        with ActionArbiter(adapter, "fixture.app", seconds=.001) as arbiter:
            with patch("motor.arbiter.time.monotonic", return_value=arbiter.deadline + 1):
                with self.assertRaises(Stopped):
                    arbiter.check()
        with ActionArbiter(adapter, "fixture.app", seconds=2) as next_task:
            self.assertIsNotNone(next_task.lease)

    def test_quick_and_deep_visit_four_tabs_and_restore(self):
        for mode in ("quick", "deep"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                adapter = FakeAdapter(tabs=True)
                store = MapStore(Path(temp) / "map.sqlite3")
                motor = Motor(adapter, store)
                result = motor.scout("fixture.app", mode=mode, budget=Budget(
                    seconds=3, restore_seconds=2, max_scrolls=0, max_depth=1, max_pages=12))
                self.assertEqual(result["metrics"]["visited_tabs"], 4)
                self.assertTrue(result["restoration"]["exact"])
                self.assertEqual(adapter.tab_index, 0)
                store.close()

    def test_deep_safe_detail_visit_returns_home(self):
        with tempfile.TemporaryDirectory() as temp:
            adapter = FakeAdapter(details=True)
            store = MapStore(Path(temp) / "map.sqlite3")
            result = Motor(adapter, store).scout(
                "fixture.app", mode="deep", goal="details",
                budget=Budget(seconds=3, restore_seconds=2, max_scrolls=0, max_depth=1))
            self.assertTrue(result["restoration"]["exact"])
            self.assertTrue(any(call[0] == "tap_node" and call[1]["nodeId"] == "details"
                                for call in adapter.perform_calls))
            self.assertEqual(adapter.screen, "home")
            store.close()

    def test_long_list_deduplicates_content_and_stops_at_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            adapter = FakeAdapter(chunks=[["one", "two"], ["two", "three"], ["three", "four"]])
            store = MapStore(Path(temp) / "map.sqlite3")
            result = Motor(adapter, store).scout(
                "fixture.app", mode="deep", budget=Budget(seconds=3, restore_seconds=1,
                    max_scrolls=8, max_depth=0, max_pages=12))
            store.close()
            self.assertEqual(result["metrics"]["unique_content"], 4)
            forward_scrolls = [call for call in adapter.perform_calls
                               if call[0] == "scroll_node" and call[1].get("direction") == "forward"]
            self.assertEqual(len(forward_scrolls), 3)  # two pages plus one boundary probe
            self.assertTrue(result["restoration"]["exact"])

    def test_repeated_content_is_bounded_by_scroll_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            adapter = FakeAdapter(repeat_content=True)
            store = MapStore(Path(temp) / "map.sqlite3")
            result = Motor(adapter, store).scout(
                "fixture.app", mode="deep", budget=Budget(seconds=3, restore_seconds=1,
                    max_scrolls=2, max_depth=0, max_pages=12, no_progress_limit=2))
            forward_scrolls = [call for call in adapter.perform_calls
                               if call[0] == "scroll_node" and call[1].get("direction") == "forward"]
            self.assertEqual(len(forward_scrolls), 2)
            self.assertEqual(result["metrics"]["unique_content"], 1)
            store.close()

    def test_dangerous_and_ambiguous_actions_are_never_executed(self):
        for adapter, selector in (
            (FakeAdapter(dangerous=True), Selector(text="Delete")),
            (FakeAdapter(ambiguous=True), Selector(resource_id="duplicate")),
        ):
            with self.subTest(selector=selector), tempfile.TemporaryDirectory() as temp:
                store = MapStore(Path(temp) / "map.sqlite3")
                result = Motor(adapter, store).act(
                    "fixture.app", Action("CLICK", selector))
                self.assertFalse(result["ok"])
                self.assertEqual(adapter.perform_calls, [])
                store.close()

    def test_stale_and_timed_out_actions_are_not_retried(self):
        stale = FakeAdapter()
        with ActionArbiter(stale, "fixture.app", seconds=2) as arbiter:
            reflex = ReflexEngine(arbiter)
            state = reflex.observe()
            result = reflex.execute(Action("CLICK", Selector(text="Missing")), state=state)
            self.assertEqual(result.status, "uncertain")
            self.assertEqual(stale.perform_calls, [])

        no_effect = FakeAdapter(details=True, no_effect=True)
        with ActionArbiter(no_effect, "fixture.app", seconds=2) as arbiter:
            reflex = ReflexEngine(arbiter)
            state = reflex.observe()
            action = Action("CLICK", Selector(text="Details"))
            result = reflex.execute(action, state=state, verify_seconds=.03)
            self.assertEqual(result.status, "uncertain")
            self.assertEqual(len(no_effect.perform_calls), 1)
            retry = reflex.execute(action, state=state, verify_seconds=.03)
            self.assertEqual(retry.status, "cancelled")
            self.assertEqual(len(no_effect.perform_calls), 1)

    def test_cancel_does_not_resume_navigation_or_restore(self):
        adapter = FakeAdapter(tabs=True, block_events=True)
        store = MapStore(":memory:")
        motor = Motor(adapter, store)
        result = {}
        worker = threading.Thread(target=lambda: result.setdefault("value", motor.scout(
            "fixture.app", mode="quick", budget=Budget(seconds=4, restore_seconds=2, max_scrolls=0))))
        worker.start()
        self.assertTrue(adapter.wait_entered.wait(1))
        actions_before_cancel = len(adapter.perform_calls)
        motor.cancel("test_cancel")
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(adapter.perform_calls), actions_before_cancel)
        self.assertFalse(result["value"]["restoration"]["exact"])
        store.close()

    def test_two_motor_instances_share_native_exclusive_lease(self):
        adapter = FakeAdapter(block_events=True)
        store1, store2 = MapStore(":memory:"), MapStore(":memory:")
        motor1, motor2 = Motor(adapter, store1), Motor(adapter, store2)
        first = {}
        worker = threading.Thread(target=lambda: first.setdefault("value", motor1.act(
            "fixture.app", Action("WAIT", wait_ms=1000))))
        worker.start()
        self.assertTrue(adapter.wait_entered.wait(1))
        second = motor2.act("fixture.app", Action("WAIT", wait_ms=100))
        self.assertEqual(second["escalation"]["reason"], "motor_busy")
        motor1.cancel("release_test")
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(adapter.cancel_calls)

    def test_reopened_map_reuses_verified_route(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "shared.sqlite3"
            adapter = FakeAdapter(details=True)
            before = adapter.state()
            adapter.perform("tap_node", {"nodeId": "details"}, "seed", {"lease_id": "seed"})
            after = adapter.state()
            action = Action("CLICK", Selector(text="Details"))
            first = MapStore(path)
            first.observe(before)
            first.observe(after)
            first.connect(before, after, action, True, source="fixture")
            first.close()
            adapter.screen = "home"
            reopened = MapStore(path)
            result = Motor(adapter, reopened).navigate("fixture.app", "Detail page")
            self.assertTrue(result["ok"])
            self.assertTrue(result["reused_map"])
            self.assertTrue(any(call[1].get("nodeId") == "details" for call in adapter.perform_calls))
            reopened.close()


if __name__ == "__main__":
    unittest.main()
