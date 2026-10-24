import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("agent_benchmark", SCRIPTS / "agent_benchmark.py")
agent_benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent_benchmark)


class AgentBenchmarkTests(unittest.TestCase):
    def test_old_motor_attempt_is_archived_with_task_id(self):
        with tempfile.TemporaryDirectory() as temp:
            report = Path(temp) / "benchmark-device-motor.json"
            report.write_text(json.dumps({"task_id": "task-previous", "failed": True}), encoding="utf-8")
            archived = agent_benchmark._archive_old_motor(report)
            self.assertEqual(archived.name, "benchmark-device-motor-task-previous.json")
            self.assertEqual(json.loads(archived.read_text(encoding="utf-8"))["task_id"], "task-previous")
            self.assertTrue(report.exists())

    @contextmanager
    def _install_fake_collector(self, temp):
        collector_root = Path(temp) / "collector"
        (collector_root / "trajectory").mkdir(parents=True)
        (collector_root / "trajectory" / "transport.py").write_text("", encoding="utf-8")

        class Dataset:
            def __init__(self, *_args): pass
            def __enter__(self): return self
            def __exit__(self, *_args): return False

        class Adb:
            def __init__(self, *_args): pass

        package = types.ModuleType("trajectory")
        package.__path__ = []
        dataset_module = types.ModuleType("trajectory.dataset")
        dataset_module.Dataset = Dataset
        transport_module = types.ModuleType("trajectory.transport")
        transport_module.Adb = Adb
        with patch.dict(sys.modules, {"trajectory": package,
                                     "trajectory.dataset": dataset_module,
                                     "trajectory.transport": transport_module}):
            yield collector_root

    def test_motor_only_run_includes_saved_baseline_in_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "dist"
            output.mkdir()
            baseline = {"serial": "device-1", "mode": "baseline", "task_id": "base-1",
                        "metrics": {"success": {"value": True},
                                    "duration_seconds": {"value": 12}}}
            (output / "benchmark-device-1-baseline.json").write_text(json.dumps(baseline), encoding="utf-8")
            with self._install_fake_collector(temp) as collector_root:
                def run_serial(_serial, _adb, _dataset, _root, out, modes):
                    self.assertEqual(modes, ("motor",))
                    report = {"serial": "device-1", "mode": "motor", "task_id": "motor-1",
                              "metrics": {"success": {"value": True},
                                          "duration_seconds": {"value": 8}}}
                    agent_benchmark._json_write(out / "benchmark-device-1-motor.json", report)
                    return [report]

                with patch.object(agent_benchmark, "run_serial", side_effect=run_serial):
                    code = agent_benchmark.main(["--serial", "device-1", "--mode", "motor",
                                                 "--collector-root", str(collector_root),
                                                 "--trajectory-data", str(Path(temp) / "data"),
                                                 "--output-dir", str(output)])
            self.assertEqual(code, 0)
            summary = json.loads((output / "benchmark.json").read_text(encoding="utf-8"))
            self.assertEqual({(m["serial"], m["mode"]) for m in summary["measurements"]},
                             {("device-1", "baseline"), ("device-1", "motor")})

    def test_warm_only_reuses_baseline_and_cold_reports_and_compares_both(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "dist"
            output.mkdir()
            for mode, duration in (("baseline", 20), ("motor", 25)):
                report = {"serial": "device-1", "mode": mode, "task_id": mode + "-1",
                          "metrics": {"success": {"value": True},
                                      "duration_seconds": {"value": duration}}}
                (output / f"benchmark-device-1-{mode}.json").write_text(json.dumps(report), encoding="utf-8")
            with self._install_fake_collector(temp) as collector_root:
                def run_serial(_serial, _adb, _dataset, _root, out, modes):
                    self.assertEqual(modes, ("warm",))
                    report = {"serial": "device-1", "mode": "warm", "task_id": "warm-1",
                              "metrics": {"success": {"value": True},
                                          "duration_seconds": {"value": 22}}}
                    agent_benchmark._json_write(out / "benchmark-device-1-warm.json", report)
                    return [report]

                with patch.object(agent_benchmark, "run_serial", side_effect=run_serial):
                    code = agent_benchmark.main(["--serial", "device-1", "--mode", "warm",
                                                 "--collector-root", str(collector_root),
                                                 "--trajectory-data", str(Path(temp) / "data"),
                                                 "--output-dir", str(output)])
            self.assertEqual(code, 0)
            summary = json.loads((output / "benchmark.json").read_text(encoding="utf-8"))
            modes = {m["mode"] for m in summary["measurements"]}
            self.assertEqual(modes, {"baseline", "motor", "warm"})
            deltas = {item["comparison_mode"]: item["motor_minus_baseline_seconds"]
                      for item in summary["paired_duration_deltas"]}
            self.assertEqual(deltas["motor"], 5)
            self.assertEqual(deltas["warm"], 2)

    def test_warm_task_prompt_reads_saved_page_then_target_without_scout(self):
        class Client:
            def __init__(self): self.calls = []
            def service_request(self, _target, path, body=None):
                self.calls.append((path, body))
                if path == "/api/tasks":
                    return {"task": {"id": "warm-task"}}
                return {"task": {"id": "warm-task", "status": "completed"}}

        client = Client()
        created = []
        detail, timed_out = agent_benchmark._run_task(client, "warm", created.append)
        prompt = client.calls[0][1]["prompt"]
        self.assertIn('query="收藏", open_target=false', prompt)
        self.assertIn("query 使用刚读到的编号", prompt)
        self.assertNotIn("motor_scout(", prompt)
        self.assertEqual(created, ["warm-task"])
        self.assertFalse(timed_out)
        self.assertEqual(detail["task"]["status"], "completed")

    def test_started_task_with_failed_oracle_returns_one(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "dist"
            with self._install_fake_collector(temp) as collector_root:
                report = {"serial": "device-1", "mode": "motor", "task_id": "started-task",
                          "metrics": {"success": {"value": False},
                                      "duration_seconds": {"value": 3}}}
                with patch.object(agent_benchmark, "run_serial", return_value=[report]):
                    code = agent_benchmark.main(["--serial", "device-1", "--mode", "motor",
                                                 "--collector-root", str(collector_root),
                                                 "--trajectory-data", str(Path(temp) / "data"),
                                                 "--output-dir", str(output)])
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
