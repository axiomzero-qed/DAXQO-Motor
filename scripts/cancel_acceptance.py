"""Verify the real Harness Stop button path fences an in-flight Motor scout."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from motor.trajectory import load_collector
from motor.transport import usb_transport

APP = "io.daxqo.motorfixture"


def run(serial, dataset):
    from trajectory.transport import Adb, DeviceClient
    from trajectory.collector import sync_device
    report = {"serial": serial, "passed": False}
    with DeviceClient(Adb(), serial) as client, usb_transport(serial) as native:
        status = client.service_request("agent", "/api/status")
        if status.get("activeTaskId") or status.get("activeTaskIds"):
            raise RuntimeError("An existing task is active")
        if client.request("/api/trajectory/status").get("enabled"):
            raise RuntimeError("An existing collection session is active")
        wake = client.action("wake")
        if not wake.get("ok"):
            raise RuntimeError("Wake was not verified")
        Adb().run(serial, "shell", "am", "start", "-S", "-W", "-n", APP + "/.MainActivity")
        task = {"id": str(uuid.uuid4()), "user_task": "MCP任务取消边界验收", "stage": "cancel_test",
                "source": "fixture", "subgoal": ""}
        client.request("/api/trajectory/config", {"enabled": True, "screenshots": False, "allowed_packages": [APP], "task": task})
        tid = None
        try:
            created = client.service_request("agent", "/api/tasks", {"title": "Motor取消验收",
                "prompt": "请立即只调用一次motor_scout：package=\"io.daxqo.motorfixture\",mode=\"deep\",goal=\"商品\",seconds=100,max_scrolls=50,max_depth=1。不要用其他工具，调用后简短报告。",
                "clientRequestId": str(uuid.uuid4())})
            tid = created["task"]["id"]
            report["task_id"] = tid
            task["id"] = tid
            client.request("/api/trajectory/config", {"task": task})
            deadline = time.monotonic() + 30
            before = {}
            while time.monotonic() < deadline:
                before = native.request("/api/motor/status")
                if before.get("lease_active"):
                    break
                time.sleep(.1)
            if not before.get("lease_active"):
                raise RuntimeError("Motor did not acquire its lease")
            started = time.monotonic()
            report["owner_before_stop"] = before["owner"]
            with ThreadPoolExecutor(max_workers=1) as pool:
                request = pool.submit(client.service_request, "agent", "/api/tasks/" + tid + "/stop", {})
                for delay in (.5, 1, 2):
                    while time.monotonic() - started < delay:
                        time.sleep(.03)
                    state = native.request("/api/motor/status")
                    report.setdefault("samples", []).append({"seconds": round(time.monotonic() - started, 3),
                        "lease_active": state["lease_active"], "reason": state["reason"]})
                report["released_within_2s"] = not state["lease_active"]
                if state["lease_active"] and state.get("owner") == before["owner"]:
                    # Isolated test cleanup is an explicit device-level stop, not an old task's request.
                    report["test_cleanup_required"] = True
                    native.request("/api/motor/cancel", {"reason": "isolated_cancel_test_cleanup"})
                try:
                    report["stop_ack_status"] = request.result(timeout=15).get("task", {}).get("status")
                except Exception as exc:
                    report["stop_error"] = type(exc).__name__
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = client.service_request("agent", "/api/tasks/" + tid)["task"]["status"]
                if state not in {"running", "queued"}:
                    break
                time.sleep(.1)
            report["final_task_status"] = state
            report["passed"] = report["released_within_2s"] and state == "stopped" and not report.get("stop_error")
        except Exception as exc:
            report["error"] = str(exc)[:250]
            report["error_type"] = type(exc).__name__
            if tid:
                try:
                    client.service_request("agent", "/api/tasks/" + tid + "/stop", {})
                except Exception:
                    pass
        finally:
            client.request("/api/trajectory/config", {"enabled": False})
            report["transfer"] = sync_device(client, dataset)
    (ROOT / "dist" / ("cancel-acceptance-" + serial + ".json")).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--serial", action="append", required=True)
    p.add_argument("--trajectory-data", type=Path, required=True)
    args = p.parse_args()
    load_collector()
    from trajectory.dataset import Dataset
    reports = []
    with Dataset(args.trajectory_data) as dataset:
        for serial in args.serial:
            report = run(serial, dataset)
            reports.append(report)
            print(json.dumps(report, ensure_ascii=True), flush=True)
    (ROOT / "dist/cancel-acceptance.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all(report["passed"] for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
