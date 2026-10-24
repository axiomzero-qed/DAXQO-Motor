"""Run one paired, read-only-observed Harness benchmark per requested Android device."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
import uuid
from pathlib import Path

from benchmark import task_measurement


ROOT = Path(__file__).resolve().parents[1]
APP = "io.daxqo.motorfixture"
HOME_VIEW_ID = APP + ":id/motor_tab_home"
TARGET_LABEL = "商品 19"
MAX_SECONDS = 180
POLL_SECONDS = 2
TERMINAL = {"completed", "succeeded", "finished", "failed", "error", "stopped", "interrupted", "cancelled", "canceled"}
MODES = ("baseline", "motor")
RUN_MODES = ("baseline", "motor", "warm")

BASELINE_PROMPT = """请在 DAXQO Motor Fixture 中完成一个只读浏览任务：先查看“收藏”页，读取页面提示的目标商品编号；然后到“商品”页找到该编号对应的商品，打开详情并读取商品编号与说明；最后回到首页。

目标编号必须从收藏页的“跨标签目标”提示中读取，不要猜测或使用提示以外的编号。只使用 DAXQO Android 工具（android_status、android_observe 读取界面树、android_action；需要唤醒时 action 为 wake）。不要调用任何 Motor 工具、终端/命令工具、截图工具或子智能体。不要点击“删除全部”。"""

MOTOR_PROMPT = """请在 DAXQO Motor Fixture 中完成一个只读浏览任务：目标商品编号必须从“收藏”页的“跨标签目标”提示中读取；提示没有给出编号时，说明无法继续，不要猜。

第一步只调用一次 motor_scout，参数必须是 package="io.daxqo.motorfixture", mode="deep", goal="收藏页提示目标", seconds=45, max_scrolls=8, max_depth=0。只根据该调用返回的摘要读取目标编号。然后只调用一次 motor_navigate，query 使用摘要中实际读取到的编号，open_target=true。确认详情中的编号与说明后，调用 motor_reflex 点击首页标签返回首页：CLICK selector resource_id="io.daxqo.motorfixture:id/motor_tab_home"、text="首页"、role="button"。

允许使用 android_status 或 android_action(action="wake")；不要调用 android_observe，不要额外调用 motor_scout/motor_map，不要使用终端、命令、截图或子智能体。不要点击“删除全部”。"""

WARM_PROMPT = """请在 DAXQO Motor Fixture 中完成一个只读浏览任务：目标商品编号必须从“收藏”页的“跨标签目标”提示中读取；提示没有给出编号时，说明无法继续，不要猜。

第一步调用一次 motor_navigate(package="io.daxqo.motorfixture", query="收藏", open_target=false)，只根据返回摘要读取收藏页提示中的实际目标编号。然后调用一次 motor_navigate，query 使用刚读到的编号，open_target=true。确认详情中的编号与说明后，调用 motor_reflex 点击首页标签返回首页：CLICK selector resource_id="io.daxqo.motorfixture:id/motor_tab_home"、text="首页"、role="button"。

允许使用 android_status 或 android_action(action="wake")；不要调用 motor_scout、motor_map、android_observe，不要使用终端、命令、截图或子智能体。不要点击“删除全部”。"""


def _json_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _archive_old_motor(path: Path) -> Path | None:
    if not path.is_file():
        return None
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    task_id = old.get("task_id") if isinstance(old, dict) else None
    suffix = _safe_serial(str(task_id)) if task_id else str(time.time_ns())
    archive_dir = path.parent / "attempts"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived = archive_dir / f"{path.stem}-{suffix}.json"
    if archived.exists():
        archived = archive_dir / f"{path.stem}-{suffix}-{uuid.uuid4().hex[:8]}.json"
    shutil.copy2(path, archived)
    return archived


def _safe_serial(serial: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", serial).strip("._")
    return value or "device"


def _active_ids(status: dict) -> list[str]:
    values = status.get("activeTaskIds")
    ids = [str(item) for item in values if item] if isinstance(values, list) else []
    if status.get("activeTaskId") and str(status["activeTaskId"]) not in ids:
        ids.append(str(status["activeTaskId"]))
    return ids


def _harness_status(client) -> dict:
    status = client.service_request("agent", "/api/status")
    if not isinstance(status, dict):
        raise RuntimeError("Harness returned an invalid status")
    active = _active_ids(status)
    if active:
        raise RuntimeError("Harness has active task(s): " + ", ".join(active))
    model = (status.get("provider") or {}).get("model")
    if not isinstance(model, str) or not model.strip():
        raise RuntimeError("Harness status did not expose provider.model")
    if "deepseek" not in model.casefold():
        raise RuntimeError("Harness provider model is not DeepSeek: " + model)
    return status


def _collector_status(client) -> dict:
    status = client.request("/api/trajectory/status")
    if not isinstance(status, dict) or type(status.get("enabled")) is not bool:
        raise RuntimeError("Collector status is unavailable")
    if status["enabled"]:
        raise RuntimeError("Collector is already enabled; refusing to overwrite its session")
    return status


def _task_detail(client, task_id: str) -> dict:
    value = client.service_request("agent", "/api/tasks/" + task_id)
    if not isinstance(value, dict) or not isinstance(value.get("task"), dict):
        raise RuntimeError("Harness returned invalid task detail")
    return value


def _stop_once(client, task_id: str, already_sent: bool) -> bool:
    if already_sent:
        return True
    client.service_request("agent", "/api/tasks/" + task_id + "/stop", body={})
    return True


def _run_task(client, mode: str, on_created) -> tuple[dict | None, bool]:
    prompt = {"baseline": BASELINE_PROMPT, "motor": MOTOR_PROMPT,
              "warm": WARM_PROMPT}[mode]
    body = {"title": "Motor fixture benchmark · " + mode,
            "prompt": prompt, "clientRequestId": str(uuid.uuid4())}
    started = time.monotonic()
    result = client.service_request("agent", "/api/tasks", body=body)
    task = result.get("task") if isinstance(result, dict) else None
    task_id = task.get("id") if isinstance(task, dict) else None
    if not isinstance(task_id, str) or not task_id:
        raise RuntimeError("Harness did not return a task ID")
    on_created(task_id)
    detail = None
    deadline = started + MAX_SECONDS
    while time.monotonic() < deadline:
        detail = _task_detail(client, task_id)
        status = str(detail["task"].get("status", "")).casefold()
        if status in TERMINAL:
            return detail, False
        time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
    return detail, True


def _state_texts(state) -> list[str]:
    if not isinstance(state, dict):
        return []
    values = []
    for key in ("title", "labels", "nodes"):
        item = state.get(key)
        if isinstance(item, str):
            values.append(item)
        elif isinstance(item, list):
            for entry in item:
                if isinstance(entry, str):
                    values.append(entry)
                elif isinstance(entry, dict):
                    for field in ("text", "description", "contentDescription"):
                        if isinstance(entry.get(field), str):
                            values.append(entry[field])
    return values


def _detail_oracle(records: list[dict]) -> dict:
    found = []
    for record in records:
        if record.get("record_type") != "action":
            continue
        text = "\n".join(_state_texts(record.get("after")))
        if "商品详情" in text and TARGET_LABEL in text:
            found.append(record.get("action_id"))
    passed = bool(found)
    return {"value": passed, "detail_after_action_ids": found,
            "reason": None if passed else "no native action.after contained both 商品详情 and 商品 19"}


def _home_oracle(tree_response) -> dict:
    if not isinstance(tree_response, dict) or not tree_response.get("ok"):
        return {"value": None, "reason": "final native tree read failed"}
    tree = tree_response.get("result")
    nodes = tree.get("nodes") if isinstance(tree, dict) else None
    if not isinstance(nodes, list):
        return {"value": None, "reason": "final native tree contained no node list"}
    home = next((node for node in nodes if isinstance(node, dict)
                 and node.get("viewId") == HOME_VIEW_ID and node.get("visible") is True), None)
    if home is None:
        return {"value": False, "reason": "home tab was not visible in final native tree"}
    if type(home.get("selected")) is not bool:
        return {"value": None, "reason": "native tree schema did not expose selected state"}
    return {"value": home["selected"],
            "reason": None if home["selected"] else "home tab was not selected"}


def _native_metrics(dataset, task_id: str | None) -> tuple[list[dict], dict]:
    if not task_id:
        return [], {"action_count": None, "decision_sources": [],
                    "reason": "Harness task was not created"}
    records = dataset.records(task_id=task_id)
    actions = [row for row in records if row.get("record_type") == "action"
               and (row.get("task") or {}).get("id") == task_id]
    sources = sorted({decision.get("source") for row in actions
                      if isinstance((decision := row.get("decision")), dict)
                      and isinstance(decision.get("source"), str)})
    return records, {"action_count": len(actions), "decision_sources": sources,
                     "reason": None if actions else "no native action records were synced for this task"}


def _tokens_and_responses(detail) -> tuple[dict, dict]:
    context = detail.get("context") if isinstance(detail, dict) else None
    total = context.get("total") if isinstance(context, dict) else None
    tokens = total.get("totalTokens") if isinstance(total, dict) else None
    token_metric = {"value": tokens, "reason": None if type(tokens) is int and tokens >= 0
                    else "Harness detail did not contain context.total.totalTokens"}
    events = detail.get("progress") if isinstance(detail, dict) else None
    usage = [event for event in events if isinstance(event, dict) and event.get("kind") == "usage"] if isinstance(events, list) else []
    snapshots = set()
    for event in usage:
        usage_context = event.get("context")
        cumulative = usage_context.get("total") if isinstance(usage_context, dict) else None
        if isinstance(cumulative, dict) and cumulative:
            snapshots.add(json.dumps(cumulative, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    response_metric = {"value": len(snapshots) if usage else None,
                       "reason": None if usage else "task progress contained no usage events; this is not an API-call count"}
    return token_metric, response_metric


def run_mode(client, adb, dataset, serial: str, mode: str, model: str) -> dict:
    task_id = None
    detail = None
    timed_out = False
    stop_count = 0
    collector_attempted = False
    collector_disabled = False
    sync = None
    wake_result = None
    tree_response = None
    report = {"schema_version": "daxqo.motor.agent-benchmark/0.1", "source": "real_deepseek_harness",
              "serial": serial, "mode": mode, "provider_model": model,
              "task_id": None, "harness_detail": None, "native_actions": None,
              "oracle": None, "metrics": None, "collector_sync": None,
              "stop_requests": 0}
    collector_task = {"id": str(uuid.uuid4()), "user_task": "从收藏提示读取目标商品并查看详情，最后回首页",
                      "subgoal": "Fixture benchmark; synthetic app data", "stage": mode, "source": "fixture"}
    try:
        _harness_status(client)
        _collector_status(client)
        wake_result = client.action("wake")
        if not wake_result.get("ok"):
            raise RuntimeError("Device wake failed; needs owner unlock/control")
        adb.run(serial, "shell", "am", "start", "-S", "-W", "-n", APP + "/.MainActivity")

        collector_attempted = True
        config = client.request("/api/trajectory/config", {
            "enabled": True, "screenshots": False, "task": collector_task,
            "allowed_packages": [APP]})
        if not isinstance(config, dict) or config.get("enabled") is not True:
            raise RuntimeError("Collector did not enable the fixture-only session")

        def remember_task(ident):
            nonlocal task_id
            task_id = ident
            updated_task = dict(collector_task, id=ident)
            updated = client.request("/api/trajectory/config", {
                "enabled": True, "screenshots": False, "task": updated_task,
                "allowed_packages": [APP]})
            if not isinstance(updated, dict) or updated.get("enabled") is not True:
                raise RuntimeError("Collector did not bind the Harness task ID")
        detail, timed_out = _run_task(client, mode, remember_task)
        if detail and isinstance(detail.get("task"), dict):
            task_id = detail["task"].get("id") or task_id
        if timed_out and task_id:
            stop_count += 1
            try:
                _stop_once(client, task_id, False)
                stop_deadline = time.monotonic() + 30
                while time.monotonic() < stop_deadline:
                    detail = _task_detail(client, task_id)
                    if str(detail["task"].get("status", "")).casefold() in TERMINAL:
                        break
                    time.sleep(POLL_SECONDS)
            except Exception as exc:
                report["stop_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
        if task_id:
            try:
                current = _task_detail(client, task_id)
                if str(current["task"].get("status", "")).casefold() not in TERMINAL and stop_count == 0:
                    stop_count += 1
                    _stop_once(client, task_id, False)
                    current = _task_detail(client, task_id)
                detail = current
            except Exception as stop_exc:
                report["stop_error"] = f"{type(stop_exc).__name__}: {str(stop_exc)[:200]}"
    finally:
        if collector_attempted:
            try:
                disabled = client.request("/api/trajectory/config", {"enabled": False, "screenshots": False})
                collector_disabled = isinstance(disabled, dict) and disabled.get("enabled") is False
            except Exception as exc:
                report["collector_disable_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            try:
                from trajectory.collector import sync_device
                sync = sync_device(client, dataset)
            except Exception as exc:
                report["collector_sync_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        try:
            tree_response = client.action("tree", {"expectedPackage": APP})
        except Exception as exc:
            report["final_tree_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"

    # The collector session is rebound to the created Harness task ID before calls start.
    if detail and isinstance(detail.get("task"), dict):
        task_id = detail["task"].get("id") or task_id
    records, native = _native_metrics(dataset, task_id)
    detail_result = _detail_oracle(records)
    home_result = _home_oracle(tree_response)
    passed = detail_result["value"] is True and home_result["value"] is True
    report.update({"task_id": task_id, "harness_detail": detail,
                   "native_actions": native,
                   "oracle": {"detail_observed": detail_result, "final_home_selected": home_result,
                              "success": passed,
                              "reason": None if passed else "independent native action/tree checks did not both pass"},
                   "collector_sync": sync, "collector_disabled_in_finally": collector_disabled,
                   "wake": {"ok": bool(wake_result and wake_result.get("ok"))},
                   "timed_out": timed_out, "stop_requests": stop_count})
    if detail is not None:
        metrics = task_measurement(detail, task_id)
        tokens, responses = _tokens_and_responses(detail)
        report["metrics"] = {
            "success": {"value": passed, "reason": report["oracle"]["reason"]},
            "duration_seconds": metrics["duration_seconds"], "tokens": tokens,
            "android_mcp_calls": metrics["android_mcp_calls"],
            "motor_tool_calls": metrics["motor_tool_calls"],
            "observed_model_responses": responses,
            "model_api_calls": metrics["model_api_calls"],
            "harness_status": (detail.get("task") or {}).get("status"),
            "task_error": (detail.get("task") or {}).get("error"),
        }
    else:
        report["metrics"] = {
            "success": {"value": False, "reason": report.get("error", "task detail unavailable")},
            "duration_seconds": {"value": None, "reason": "Harness task detail unavailable"},
            "tokens": {"value": None, "reason": "Harness task detail unavailable"},
            "android_mcp_calls": {"value": None, "reason": "Harness task detail unavailable"},
            "motor_tool_calls": {"value": None, "reason": "Harness task detail unavailable"},
            "observed_model_responses": {"value": None, "reason": "Harness task detail unavailable"},
            "model_api_calls": {"value": None, "reason": "No explicit provider API-call counter"},
            "harness_status": None, "task_error": report.get("error"),
        }
    return report


def _summary_entry(report: dict) -> dict:
    metrics = report.get("metrics") or {}
    return {"serial": report.get("serial"), "mode": report.get("mode"),
            "task_id": report.get("task_id"), "provider_model": report.get("provider_model"),
            "success": metrics.get("success"),
            "duration_seconds": metrics.get("duration_seconds"),
            "tokens": metrics.get("tokens"),
            "android_mcp_calls": metrics.get("android_mcp_calls"),
            "motor_tool_calls": metrics.get("motor_tool_calls"),
            "observed_model_responses": metrics.get("observed_model_responses"),
            "model_api_calls": metrics.get("model_api_calls")}


def build_summary(reports: list[dict]) -> dict:
    entries = [_summary_entry(report) for report in reports]
    by_serial = {}
    for entry in entries:
        by_serial.setdefault(entry.get("serial"), {})[entry.get("mode")] = entry
    paired = []
    for serial, modes in by_serial.items():
        baseline = ((modes.get("baseline") or {}).get("duration_seconds") or {}).get("value")
        for comparison_mode, run_label in (("motor", "cold"), ("warm", "warm")):
            observed = ((modes.get(comparison_mode) or {}).get("duration_seconds") or {}).get("value")
            paired.append({"serial": serial, "comparison_mode": comparison_mode,
                           "run_label": run_label,
                           "motor_minus_baseline_seconds":
                           round(observed - baseline, 3) if isinstance(observed, (int, float)) and isinstance(baseline, (int, float)) else None,
                           "reason": None if isinstance(observed, (int, float)) and isinstance(baseline, (int, float)) else "paired measured baseline and run durations are required"})
    return {"schema_version": "daxqo.motor.agent-benchmark-summary/0.1",
            "source": "real_deepseek_harness", "measurements": entries,
            "paired_duration_deltas": paired,
            "limitations": ["One baseline and one Motor run per device; these are single-run observations, not a statistical success rate.",
                            "model_api_calls is null unless Harness exposes an explicit provider request counter.",
                            "observed_model_responses counts distinct cumulative usage snapshots and is not a provider API-call count."]}


def run_serial(serial: str, adb, dataset, collector_root: Path, output_dir: Path,
               modes: tuple[str, ...] = MODES) -> list[dict]:
    from trajectory.transport import DeviceClient

    reports = []
    with DeviceClient(adb, serial) as client:
        status = _harness_status(client)
        model = status["provider"]["model"]
        _collector_status(client)  # Refuse to replace an owner's active collection session.
        for index, mode in enumerate(modes):
            report = run_mode(client, adb, dataset, serial, mode, model)
            report_path = output_dir / f"benchmark-{_safe_serial(serial)}-{mode}.json"
            if mode == "motor":
                archived = _archive_old_motor(report_path)
                if archived:
                    report["previous_attempt_archived"] = str(archived)
            report["report_path"] = str(report_path)
            _json_write(report_path, report)
            reports.append(report)
            active = _active_ids(client.service_request("agent", "/api/status"))
            if active:
                # Never overlap our next mode with an unfinished task.
                for remaining in modes[index + 1:]:
                    blocked = {"schema_version": "daxqo.motor.agent-benchmark/0.1",
                               "source": "real_deepseek_harness", "serial": serial, "mode": remaining,
                               "provider_model": model, "task_id": None, "harness_detail": None,
                               "error": "A previous task remains active; refusing to overlap runs",
                               "native_actions": {"action_count": None, "decision_sources": [], "reason": "task not started"},
                               "oracle": {"success": False, "reason": "task not started"},
                               "metrics": {key: {"value": None, "reason": "task not started"}
                                           for key in ("duration_seconds", "tokens", "android_mcp_calls", "motor_tool_calls", "observed_model_responses", "model_api_calls")}}
                    path = output_dir / f"benchmark-{_safe_serial(serial)}-{remaining}.json"
                    _json_write(path, blocked)
                    blocked["report_path"] = str(path)
                    reports.append(blocked)
                break
    return reports


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", action="append", required=True,
                        help="ADB serial; repeat for each paired device")
    parser.add_argument("--mode", action="append", choices=RUN_MODES,
                        help="Mode to run; repeatable. Defaults to baseline and motor.")
    parser.add_argument("--collector-root", type=Path, required=True,
                        help="Explicit Collector source checkout used by this hardware benchmark")
    parser.add_argument("--trajectory-data", type=Path, required=True,
                        help="Explicit shared Collector Dataset root")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--adb", help="ADB executable override")
    args = parser.parse_args(argv)
    selected_modes = tuple(dict.fromkeys(args.mode or MODES))
    collector_root = args.collector_root.resolve()
    if not (collector_root / "trajectory" / "transport.py").is_file():
        parser.error("Collector trajectory transport was not found under --collector-root")
    if str(collector_root) not in sys.path:
        sys.path.insert(0, str(collector_root))
    try:
        from trajectory.dataset import Dataset
        from trajectory.transport import Adb
        adb = Adb(args.adb)
        data_root = args.trajectory_data
        reports = []
        with Dataset(data_root) as dataset:
            for serial in dict.fromkeys(args.serial):
                try:
                    reports.extend(run_serial(serial, adb, dataset, collector_root, args.output_dir,
                                              selected_modes))
                except Exception as exc:
                    for mode in selected_modes:
                        report = {"schema_version": "daxqo.motor.agent-benchmark/0.1",
                                  "source": "real_deepseek_harness", "serial": serial, "mode": mode,
                                  "task_id": None, "provider_model": None,
                                  "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                                  "metrics": {key: {"value": None, "reason": "preflight or setup failed"}
                                              for key in ("duration_seconds", "tokens", "android_mcp_calls", "motor_tool_calls", "observed_model_responses", "model_api_calls")}}
                        path = args.output_dir / f"benchmark-{_safe_serial(serial)}-{mode}.json"
                        if mode == "motor":
                            _archive_old_motor(path)
                        _json_write(path, report)
                        report["report_path"] = str(path)
                        reports.append(report)
            prior_modes = set()
            if "warm" in selected_modes:
                prior_modes.update(("baseline", "motor"))
            elif "motor" in selected_modes:
                prior_modes.add("baseline")
            if prior_modes:
                reported = {(item.get("serial"), item.get("mode")) for item in reports}
                for serial in dict.fromkeys(args.serial):
                    for prior_mode in sorted(prior_modes):
                        prior_path = args.output_dir / f"benchmark-{_safe_serial(serial)}-{prior_mode}.json"
                        if (serial, prior_mode) in reported or not prior_path.is_file():
                            continue
                        try:
                            prior_report = json.loads(prior_path.read_text(encoding="utf-8"))
                        except (OSError, ValueError):
                            continue
                        if isinstance(prior_report, dict):
                            prior_report.setdefault("report_path", str(prior_path))
                            reports.append(prior_report)
        summary = build_summary(reports)
        summary_path = args.output_dir / "benchmark.json"
        _json_write(summary_path, summary)
    except Exception as exc:
        print(f"agent benchmark failed: {type(exc).__name__}: {str(exc)[:500]}", file=sys.stderr)
        return 2
    for report in reports:
        metrics = report.get("metrics") or {}
        success = (metrics.get("success") or {}).get("value")
        print(f"{report.get('serial')} {report.get('mode')}: oracle_success={success}; "
              f"status={(metrics.get('harness_status') or 'unavailable')} — {report.get('report_path')}")
    print(f"Wrote {summary_path}")
    started_task_failed = any(
        report.get("task_id") and
        ((report.get("metrics") or {}).get("success") or {}).get("value") is False
        for report in reports if report.get("mode") in selected_modes)
    return 1 if started_task_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
