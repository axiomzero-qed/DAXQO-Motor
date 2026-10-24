import io
import json
import unittest
from unittest.mock import patch

from motor import mcp_server
from motor.mcp_server import MotorMCP


class MockMotor:
    def __init__(self):
        self.calls = []
        self.scout_result = None

    def scout(self, package, mode="quick", goal="", budget=None):
        self.calls.append(("scout", package, mode, goal, budget))
        return self.scout_result or {"ok": True, "package": package, "steps": 2, "reasoning": "secret"}

    def brief(self, package, query=None, limit=8):
        self.calls.append(("brief", package, query, limit))
        return {"pages": [{"title": "Home"}], "nodes": ["private"]}

    def navigate(self, package, query, open_target=False):
        self.calls.append(("navigate", package, query, open_target))
        return {"ok": True, "reused_map": True, "query": query}

    def act(self, package, action, source="deepseek"):
        self.calls.append(("act", package, action, source))
        return {"ok": True, "action_id": action.id}

    def cancel(self, reason):
        self.calls.append(("cancel", reason))
        return {"cancelled": True}


class MCPTests(unittest.TestCase):
    def test_task_facts_survive_harness_budget_when_relevant_page_is_last(self):
        pages = [{"title": "Page " + str(i), "labels": ["首页", "商品", "收藏", "unrelated " * 200],
                  "content": ["row " + str(j) for j in range(100)], "controls": [], "task_matches": []}
                 for i in range(15)]
        pages.append({"title": "DAXQO Motor Fixture\n收藏", "labels": ["首页", "商品", "收藏", "跨标签目标：商品 19"],
                      "content": [], "controls": [], "task_matches": []})
        report = {"schema_version": "daxqo.motor.scout/0.1", "goal": "收藏页提示目标", "pages": pages,
                  "metrics": {"cloud_calls": 0, "pages": len(pages)}, "restoration": {"status": "restored", "exact": True}}
        result = mcp_server._tool_text(report)["content"][0]["text"]
        public = json.loads(result)
        self.assertLessEqual(len(result), 3000)
        self.assertEqual(public["pages"][0]["title"], "收藏")
        self.assertIn("跨标签目标：商品 19", public["pages"][0]["findings"])
        self.assertLess(result.index("商品 19"), 500)

    def setUp(self):
        self.motor = MockMotor()
        self.server = MotorMCP(self.motor)

    def call(self, name, arguments):
        response = self.server.dispatch({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                         "params": {"name": name, "arguments": arguments}})
        return response["result"]

    @staticmethod
    def text(result):
        return json.loads(result["content"][0]["text"])

    def test_tools_list_and_scout_call(self):
        listed = self.server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual({x["name"] for x in listed["result"]["tools"]},
                         {"motor_scout", "motor_map", "motor_navigate", "motor_reflex", "motor_cancel"})
        result = self.text(self.call("motor_scout", {"package": "demo.app", "mode": "deep", "seconds": 12}))
        self.assertEqual(result["steps"], 2)
        self.assertNotIn("reasoning", result)
        self.assertEqual(self.motor.calls[0][0:4], ("scout", "demo.app", "deep", ""))
        self.assertEqual(self.motor.calls[0][4].seconds, 12)

    def test_map_reflex_and_cancel_methods(self):
        self.call("motor_map", {"package": "demo.app", "query": "cart", "limit": 4})
        self.assertNotIn("nodes", self.text(self.call("motor_map", {"package": "demo.app"})))
        self.call("motor_reflex", {"package": "demo.app", "action": {
            "kind": "CLICK", "selector": {"resource_id": "go"}, "id": "a1"}})
        act = self.motor.calls[-1]
        self.assertEqual((act[0], act[1], act[2].kind, act[2].selector.resource_id, act[3]),
                         ("act", "demo.app", "CLICK", "go", "mcp"))
        self.call("motor_cancel", {"reason": "user"})
        self.assertEqual(self.motor.calls[-1], ("cancel", "user"))

    def test_navigate_validates_and_calls_core(self):
        result = self.text(self.call("motor_navigate", {"package": "demo.app", "query": "detail"}))
        self.assertTrue(result["reused_map"])
        self.assertEqual(self.motor.calls[-1], ("navigate", "demo.app", "detail", False))
        self.call("motor_navigate", {"package": "demo.app", "query": "cart", "open_target": True})
        self.assertEqual(self.motor.calls[-1], ("navigate", "demo.app", "cart", True))
        for bad in (
            {"package": "demo.app"},
            {"package": "demo.app", "query": ""},
            {"package": "demo.app", "query": "detail", "open_target": "yes"},
            {"package": "demo.app", "query": "detail", "dangerous": True},
        ):
            result = self.call("motor_navigate", bad)
            self.assertTrue(result["isError"])
        self.assertEqual(len([c for c in self.motor.calls if c[0] == "navigate"]), 2)

    def test_bad_arguments_and_unknown_tool_have_no_side_effect(self):
        bad = self.call("motor_reflex", {"package": "demo.app", "action": {"kind": "DELETE"}})
        self.assertTrue(bad["isError"])
        unknown = self.call("motor_danger", {"package": "demo.app"})
        self.assertTrue(unknown["isError"])
        bad_limit = self.call("motor_map", {"package": "demo.app", "limit": 1000})
        self.assertTrue(bad_limit["isError"])
        self.assertEqual(self.motor.calls, [])

    def test_scroll_direction_matches_motor_safety_policy(self):
        result = self.call("motor_reflex", {"package": "demo.app", "action": {
            "kind": "SCROLL", "direction": "down", "selector": {"resource_id": "list"}}})
        self.assertTrue(result["isError"])
        self.assertIn("forward or backward", self.text(result)["error"])
        self.assertEqual(self.motor.calls, [])

    def test_large_scout_report_is_compacted_not_discarded(self):
        page = {"title": "Fixture page", "page_fingerprint": "f" * 64,
                "task_matches": ["matching control " + str(i) for i in range(50)],
                "content": ["visible item " + str(i) + " x" * 300 for i in range(80)],
                "labels": ["label " + str(i) for i in range(60)],
                "controls": [{"resource_id": "button." + str(i), "text": "Open"} for i in range(24)]}
        self.motor.scout_result = {"ok": True, "mode": "deep", "stop_reason": "page_budget",
            "pages": [dict(page, title=f"Fixture page {i}") for i in range(60)],
            "edges": [{"from": str(i), "to": str(i + 1), "action": {"kind": "CLICK",
                       "selector": {"resource_id": "open"}}} for i in range(80)],
            "metrics": {"pages": 60, "actions": 90, "scrolls": 12, "elapsed_ms": 1234},
            "restoration": {"status": "restored", "exact": True},
            "escalation": None, "current": {"title": "Current", "labels": ["x"] * 80,
                                                  "controls": [{"role": "button"}] * 80},
            "app_map": {"total_pages": 60, "coverage": {"shown_pages": 8, "total_pages": 60},
                        "pages": [page] * 60, "edges": [{"from": "x", "to": "y"}] * 80}}
        result = self.call("motor_scout", {"package": "demo.app"})
        raw = result["content"][0]["text"]
        self.assertLessEqual(len(raw), 16000)
        compact = json.loads(raw)
        self.assertTrue(compact["truncated"])
        self.assertEqual(compact["metrics"]["pages"], 60)
        self.assertTrue(compact["restoration"]["exact"])
        self.assertIn("Fixture page 0", [p["title"] for p in compact["pages"]])
        self.assertIn("task_matches", compact["pages"][0])
        self.assertIn("content", compact["pages"][0])
        self.assertIn("controls", compact["pages"][0])
        self.assertEqual(compact["app_map"]["coverage"]["total_pages"], 60)
        self.assertTrue(compact["app_map"]["pages"])
        self.assertEqual(compact["mcp_request_id"], 4)

    def test_task_context_refresh_is_read_only_and_records_rpc_id_separately(self):
        class Client:
            def __init__(self):
                self.calls = []
            def request(self, path, body=None):
                self.calls.append((path, body))
                return {"task": {"id": "configured-task", "user_task": "fixture walk",
                                 "subgoal": "", "stage": "running", "source": "fixture"}}
        class Sink:
            def __init__(self):
                self.client = Client()
                self.task = {}
                self.errors = []
        self.motor.sink = Sink()
        result = self.call("motor_scout", {"package": "demo.app"})
        value = self.text(result)
        self.assertEqual(value["mcp_request_id"], 4)
        self.assertEqual(self.motor.sink.task["id"], "configured-task")
        self.assertEqual(self.motor.sink.client.calls, [("/api/trajectory/status", None)])
        self.assertNotIn("tool_call_id", value)
        self.assertFalse(any(path.endswith("/ack") or path.endswith("/config")
                             for path, _body in self.motor.sink.client.calls))

    def test_task_refresh_failure_is_visible_and_sinkless_motor_is_unchanged(self):
        class Client:
            def request(self, path, body=None):
                raise TimeoutError("do not echo request details")
        class Sink:
            def __init__(self):
                self.client, self.task, self.errors = Client(), {}, []
        self.motor.sink = Sink()
        result = self.text(self.call("motor_scout", {"package": "demo.app"}))
        self.assertEqual(result["mcp_request_id"], 4)
        self.assertEqual(self.motor.sink.errors, ["/api/trajectory/status: TimeoutError"])
        self.assertEqual(self.motor.sink.task, {})
        self.motor.sink = None
        calls_before = len(self.motor.calls)
        self.text(self.call("motor_scout", {"package": "demo.app"}))
        self.assertEqual(len(self.motor.calls), calls_before + 1)

    def test_main_attaches_sink_without_starting_collection_or_sync(self):
        class Transport:
            def __init__(self):
                self.calls = []
                self.serial = "fake"
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def request(self, path, body=None):
                self.calls.append((path, body))
                return {"ok": True}
        transport = Transport()
        created = {}
        class Store:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def bind_local_device(self, device_id):
                created["device_id"] = device_id
        class Engine:
            def __init__(self, adapter, store, sink=None):
                created["sink"] = sink
        with patch("motor.transport.linux_transport", return_value=transport), \
             patch("motor.app_map.MapStore", return_value=Store()), \
             patch("motor.engine.Motor", Engine), \
             patch.object(mcp_server.MotorMCP, "serve", return_value=0):
            self.assertEqual(mcp_server.main(["--state-dir", "unused", "--trajectory-feedback"]), 0)
        self.assertEqual(created["sink"].client, transport)
        self.assertEqual(created["sink"].task, {})
        self.assertEqual(created["device_id"], "fake")
        self.assertEqual(transport.calls, [])

    def test_stdio_initialize_tools_list_and_notifications(self):
        source = io.BytesIO((
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
            '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
            '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n').encode())
        sink = io.BytesIO()
        self.server.serve(source, sink)
        messages = [json.loads(line) for line in sink.getvalue().splitlines()]
        self.assertEqual([m["id"] for m in messages], [1, 2])
        self.assertEqual(messages[0]["result"]["serverInfo"]["name"], "daxqo-motor")
        self.assertEqual(len(messages[1]["result"]["tools"]), 5)


if __name__ == "__main__":
    unittest.main()
