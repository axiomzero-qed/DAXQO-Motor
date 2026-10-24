import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "benchmark.py"
SPEC = importlib.util.spec_from_file_location("motor_benchmark", SCRIPT)
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


class BenchmarkTests(unittest.TestCase):
    def test_missing_model_calls_are_null_not_action_or_token_counts(self):
        result = benchmark.task_measurement({
            "task": {"id": "t1", "status": "completed"},
            "activity": {"elapsedSeconds": 4},
            "context": {"totalTokens": 1200},
            "progress": [{"kind": "tool", "summary": "操作手机", "status": "started"}],
            "metrics": {"actions": 18, "cloud_calls": 0, "tool_calls": 9},
        })
        self.assertEqual(result["android_mcp_calls"]["value"], 1)
        self.assertIsNone(result["model_api_calls"]["value"])
        self.assertIn("no provider request counter", result["model_api_calls"]["reason"])
        self.assertIsNone(result["motor_tool_calls"]["value"])

    def test_provider_metrics_duration_and_task_error(self):
        payload = {
            "task": {"id": "t2", "status": "completed", "createdAt": "2026-01-01T00:00:00Z",
                     "updatedAt": "2026-01-01T00:00:08Z", "provider_metrics": {"model_api_calls": 3}},
            "progress": [{"kind": "phase", "phase": "running", "createdAt": "2026-01-01T00:00:02Z"},
                         {"kind": "tool", "summary": "读取手机界面", "status": "started"},
                         {"kind": "tool", "summary": "读取手机界面", "status": "completed"}],
        }
        result = benchmark.task_measurement(payload)
        self.assertEqual(result["success"]["value"], True)
        self.assertEqual(result["duration_seconds"]["value"], 6)
        self.assertEqual(result["model_api_calls"]["value"], 3)
        self.assertEqual(result["android_mcp_calls"]["value"], 1)
        failed = benchmark.task_measurement({"task": {"id": "bad", "status": "completed", "error": "engine failed"}})
        self.assertFalse(failed["success"]["value"])
        self.assertIn("engine failed", failed["success"]["reason"])

    def test_nonterminal_task_and_missing_duration_are_not_success(self):
        result = benchmark.task_measurement({"task": {"status": "running"}, "progress": []})
        self.assertIsNone(result["success"]["value"])
        self.assertIsNone(result["duration_seconds"]["value"])
        self.assertIsNotNone(result["duration_seconds"]["reason"])
        self.assertEqual(result["android_mcp_calls"]["value"], 0)

    def test_reports_preserve_fixture_origin_and_only_compare_real_shared_metrics(self):
        baseline = benchmark.report_measurement({"source": "fixture_script", "ok": True,
                                                 "metrics": {"elapsed_ms": 9000, "actions": 50, "tool_calls": 10}}, "baseline")
        motor = benchmark.report_measurement({"ok": True, "metrics": {"elapsed_ms": 7000,
                                                "model_api_calls": 2, "tool_calls": 4}}, "motor")
        result = benchmark.build_benchmark(baseline, motor)
        self.assertEqual(baseline["source"], "fixture_script")
        self.assertNotIn("DeepSeek", str(baseline))
        self.assertEqual(motor["source"], "local_motor")
        self.assertEqual(result["comparison"]["metrics"]["duration_seconds"]["motor_minus_baseline"], -2)
        self.assertIsNone(result["comparison"]["metrics"]["model_api_calls"]["motor_minus_baseline"])
        self.assertIsNone(baseline["model_api_calls"]["value"])
        self.assertIsNotNone(baseline["model_api_calls"]["reason"])
        self.assertIsNone(baseline["motor_tool_calls"]["value"])
        self.assertEqual(motor["motor_tool_calls"]["value"], 4)

    def test_task_fetch_uses_get_and_only_temporary_forward(self):
        token = "x" * 40
        response = io.BytesIO(b'{"task":{"id":"task-7","status":"completed"},"progress":[]}')
        response.__enter__ = lambda: response
        response.__exit__ = lambda *args: None
        opener = MagicMock()
        opener.open.return_value = response

        def run(command, **kwargs):
            if command[-5:] == ["exec-out", "run-as", "io.daxqo.app", "cat", "shared_prefs/pairing_private.xml"]:
                return MagicMock(returncode=0, stdout=f'<map><string name="capability">{token}</string></map>'.encode())
            if "forward" in command and "--remove" not in command:
                return MagicMock(returncode=0, stdout=b"55234")
            return MagicMock(returncode=0, stdout=b"")

        with patch.object(benchmark.subprocess, "run", side_effect=run) as adb_run, \
             patch.object(benchmark.urllib.request, "build_opener", return_value=opener):
            payload = benchmark.fetch_harness_task("task-7", serial="device-1", pairing_source="android-debug")
        self.assertEqual(payload["task"]["id"], "task-7")
        self.assertEqual(opener.open.call_args.args[0].get_method(), "GET")
        self.assertTrue(any("--remove" in call.args[0] for call in adb_run.call_args_list))
        self.assertEqual(adb_run.call_count, 3)

    def test_cli_rejects_mixed_input_modes(self):
        with self.assertRaises(SystemExit) as err:
            benchmark.main(["--baseline-report", "base.json", "--motor-task-id", "t2"])
        self.assertEqual(err.exception.code, 2)

    def test_cli_writes_report_pair_and_short_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "base.json"
            local = Path(temp) / "motor.json"
            output = Path(temp) / "dist" / "benchmark.json"
            base.write_text(json.dumps({"source": "fixture_script", "ok": True,
                                        "metrics": {"elapsed_ms": 1000}}), encoding="utf-8")
            local.write_text(json.dumps({"ok": True, "metrics": {"elapsed_ms": 800}}), encoding="utf-8")
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                code = benchmark.main(["--baseline-report", str(base), "--motor-report", str(local),
                                       "--output", str(output)])
            self.assertEqual(code, 0)
            self.assertIn("fixture_script", stdout.getvalue())
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertIsNone(result["baseline"]["model_api_calls"]["value"])
            self.assertEqual(result["motor"]["source"], "local_motor")

    def test_report_accepts_explicit_value_metrics(self):
        result = benchmark.report_measurement({"success": {"value": True, "reason": None},
                                               "metrics": {"duration_seconds": {"value": 1.25},
                                                           "model_api_calls": {"value": 2},
                                                           "motor_tool_calls": {"value": 3}}}, "motor")
        self.assertTrue(result["success"]["value"])
        self.assertEqual(result["duration_seconds"]["value"], 1.25)
        self.assertEqual(result["model_api_calls"]["value"], 2)
        self.assertEqual(result["motor_tool_calls"]["value"], 3)

    def test_request_count_requires_provider_context(self):
        payload = {"task": {"status": "completed", "requestCount": 7,
                            "provider_metrics": {"requestCount": 2}}}
        self.assertEqual(benchmark.task_measurement(payload)["model_api_calls"]["value"], 2)

    def test_structured_motor_progress_counts_started_named_tools(self):
        payload = {"progress": [
            {"kind": "tool", "status": "started", "label": "motor_scout"},
            {"kind": "tool", "status": "completed", "label": "motor_navigate"},
            {"kind": "tool", "status": "started", "tool": "motor_reflex"},
            {"kind": "agentMessage", "status": "started", "text": "motor_cancel"},
        ]}
        self.assertEqual(benchmark._motor_call_count(payload), 2)


if __name__ == "__main__":
    unittest.main()
