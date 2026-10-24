"""Compare completed DAXQO Harness tasks or existing benchmark reports."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "dist" / "benchmark.json"
PHONE_LABELS = {"检查手机状态", "读取手机界面", "操作手机"}
TERMINAL = {"completed", "failed", "error", "cancelled", "canceled", "interrupted", "succeeded", "finished"}
MODEL_COUNTERS = {"model_api_calls", "modelApiCalls", "provider_api_calls", "providerApiCalls",
                  "api_request_count", "apiRequestCount", "model_request_count", "modelRequestCount"}
MODEL_SCOPED_COUNTERS = {"api_calls", "apiCalls", "request_count", "requestCount"}
ANDROID_COUNTERS = {"android_mcp_calls", "androidMcpCalls", "android_tool_calls", "androidToolCalls"}
MOTOR_COUNTERS = {"motor_tool_calls", "motorToolCalls"}
MOTOR_TOOL_NAMES = {"motor_scout", "motor_map", "motor_navigate", "motor_reflex", "motor_cancel"}


def _walk(value, path=()):
    if isinstance(value, dict):
        for key, item in value.items():
            next_path = path + (str(key),)
            yield next_path, item
            yield from _walk(item, next_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, path + (str(index),))


def _counter(document, names, *, allow_scoped_tool_calls=False):
    found = []
    for path, value in _walk(document):
        key = path[-1] if path else ""
        if isinstance(value, dict) and type(value.get("value")) is int:
            value = value["value"]
        accepted = key in names
        if allow_scoped_tool_calls and key in {"tool_calls", "toolCalls"} and any(
                part.casefold() in {"motor", "motor_metrics", "metrics"} for part in path[:-1]):
            accepted = True
        if accepted and type(value) is int and value >= 0:
            found.append(value)
    # Harness metric events can be repeated snapshots; maximum avoids double counting.
    return max(found) if found else None


def _number(document, names):
    values = []
    for path, value in _walk(document):
        if isinstance(value, dict) and type(value.get("value")) in (int, float):
            value = value["value"]
        if path and path[-1] in names and type(value) in (int, float) and value >= 0:
            values.append(value)
    return max(values) if values else None


def _provider_counter(document):
    value = _counter(document, MODEL_COUNTERS)
    if value is not None:
        return value
    candidates = []
    for path, item in _walk(document):
        key = path[-1] if path else ""
        if key not in MODEL_SCOPED_COUNTERS:
            continue
        context = {part.casefold() for part in path[:-1]}
        if not context.intersection({"provider", "provider_metrics", "providermetrics", "engine", "engine_metrics", "enginemetrics", "model", "model_metrics", "modelmetrics"}):
            continue
        if isinstance(item, dict) and type(item.get("value")) is int:
            item = item["value"]
        if type(item) is int and item >= 0:
            candidates.append(item)
    return max(candidates) if candidates else None


def _metric(value, reason=None):
    return {"value": value, "reason": reason}


def _status_result(task):
    status = str(task.get("status", "")).casefold()
    error = task.get("error")
    if error:
        return _metric(False, f"task error: {str(error)[:200]}")
    if status in {"completed", "succeeded", "finished"}:
        return _metric(True)
    if status in {"failed", "error", "cancelled", "canceled", "interrupted"}:
        return _metric(False, f"task status: {status}")
    if status in {"queued", "running", "pending", "in_progress"}:
        return _metric(None, f"task is not complete (status: {status})")
    return _metric(None, "task status is missing or unrecognized")


def _parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def _progress(payload):
    detail = payload.get("progress")
    if isinstance(detail, list):
        return detail
    task = payload.get("task") or {}
    detail = task.get("progress")
    return detail if isinstance(detail, list) else []


def _find_time(payload, keys):
    for container in (payload.get("task") or {}, payload.get("activity") or {}, payload):
        for key in keys:
            parsed = _parse_time(container.get(key))
            if parsed:
                return parsed
    return None


def _android_call_count(payload):
    explicit = _counter(payload, ANDROID_COUNTERS)
    if explicit is not None:
        return explicit
    events = _progress(payload)
    if isinstance(payload.get("progress"), list) or isinstance((payload.get("task") or {}).get("progress"), list):
        return sum(1 for event in events if isinstance(event, dict)
                   and event.get("kind") == "tool"
                   and event.get("summary") in PHONE_LABELS
                   and str(event.get("status", "")).casefold() == "started")
    # Raw engine events are accepted only when they retain the server and item type.
    raw = payload.get("engine_events") or payload.get("engineEvents") or []
    return sum(1 for event in raw if isinstance(event, dict)
               and event.get("method") == "item/started"
               and isinstance(event.get("params"), dict)
               and isinstance(event["params"].get("item"), dict)
               and event["params"]["item"].get("type") == "mcpToolCall"
               and event["params"]["item"].get("server") == "daxqo_android") if raw else None


def _motor_call_count(payload):
    explicit = _counter(payload, MOTOR_COUNTERS)
    if explicit is not None:
        return explicit
    progress = _progress(payload)
    if isinstance(payload.get("progress"), list) or isinstance((payload.get("task") or {}).get("progress"), list):
        matches = [event for event in progress if isinstance(event, dict)
                   and event.get("kind") == "tool"
                   and str(event.get("status", "")).casefold() == "started"
                   and any(name in str(event.get(key, "")) for key in ("label", "summary", "tool", "name")
                           for name in MOTOR_TOOL_NAMES)]
        return len(matches) if matches else None
    raw = payload.get("engine_events") or payload.get("engineEvents") or []
    if raw:
        return sum(1 for event in raw if isinstance(event, dict)
                   and event.get("method") == "item/started"
                   and isinstance(event.get("params"), dict)
                   and isinstance(event["params"].get("item"), dict)
                   and event["params"]["item"].get("type") == "mcpToolCall"
                   and event["params"]["item"].get("server") in {"daxqo_motor", "motor"})
    return None


def task_measurement(payload, task_id=None):
    """Extract only metrics evidenced by task state, progress, or provider counters."""
    task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
    task_id = task_id or task.get("id")
    success = _status_result(task)
    progress = _progress(payload)
    started = _find_time(payload, ("startedAt", "started_at", "startTime", "start_time"))
    if not started:
        started = next((_parse_time(event.get("createdAt")) for event in progress
                        if isinstance(event, dict) and event.get("kind") == "phase"
                        and event.get("phase") == "running" and _parse_time(event.get("createdAt"))), None)
    terminal = str(task.get("status", "")).casefold() in TERMINAL or bool(task.get("error"))
    ended = _find_time(payload, ("endedAt", "ended_at", "finishedAt", "finished_at", "completedAt", "completed_at"))
    if not ended and terminal:
        ended = _parse_time(task.get("updatedAt") or task.get("updated_at"))
    elapsed = (ended - started).total_seconds() if started and ended else None
    activity = payload.get("activity") if isinstance(payload.get("activity"), dict) else {}
    if elapsed is None and terminal and isinstance(activity.get("elapsedSeconds"), (int, float)) and activity["elapsedSeconds"] > 0:
        elapsed = max(0, float(activity["elapsedSeconds"]))
    if elapsed is None:
        duration = _metric(None, "completed start/end timestamps are unavailable")
    else:
        duration = _metric(round(max(0, elapsed), 3))
    model_calls = _provider_counter(payload)
    model_metric = _metric(model_calls, None if model_calls is not None else
                            "task event feed has no provider request counter; token usage is not an API-call count")
    android_calls = _android_call_count(payload)
    android_metric = _metric(android_calls, None if android_calls is not None else
                             "no structured Android MCP call counter or event stream is available")
    motor_calls = _motor_call_count(payload)
    motor_metric = _metric(motor_calls, None if motor_calls is not None else
                           "task feed does not identify Motor tool calls separately")
    return {
        "source": "harness_task", "task_id": task_id,
        "provider": task.get("provider") or task.get("model"),
        "success": success,
        "started_at": started.isoformat() if started else None,
        "started_at_reason": None if started else "no task start timestamp or running phase event",
        "ended_at": ended.isoformat() if ended else None,
        "ended_at_reason": None if ended else "task has no terminal end timestamp",
        "duration_seconds": duration,
        "model_api_calls": model_metric,
        "android_mcp_calls": android_metric,
        "motor_tool_calls": motor_metric,
    }


def report_measurement(document, side):
    if not isinstance(document, dict):
        raise ValueError(f"{side} report must contain a JSON object")
    task = document.get("task") if isinstance(document.get("task"), dict) else document
    status_metric = _status_result(task)
    success_value = None
    for path, value in _walk(document):
        if path and path[-1] in {"ok", "success"}:
            if isinstance(value, dict): value = value.get("value")
            if type(value) is bool:
                success_value = value
                break
    if success_value is not None:
        status_metric = _metric(success_value)

    duration = None
    duration_reason = "report does not include a measured duration"
    seconds = _number(document, {"duration_seconds", "durationSeconds", "elapsed_seconds", "elapsedSeconds"})
    if seconds is not None:
        duration, duration_reason = float(seconds), None
    else:
        milliseconds = _number(document, {"elapsed_ms", "elapsedMs"})
        if milliseconds is not None:
            duration, duration_reason = milliseconds / 1000, None

    model_calls = _provider_counter(document)
    android_calls = _counter(document, ANDROID_COUNTERS)
    motor_calls = _counter(document, MOTOR_COUNTERS, allow_scoped_tool_calls=(side == "motor"))
    source_value = str(document.get("measurement_origin") or document.get("source") or document.get("origin") or "").casefold()
    if side == "motor":
        source = "local_motor"
        origin = "local_motor_report"
    elif "fixture_script" in source_value or document.get("fixture_script") is True:
        source = "fixture_script"
        origin = "fixture_script_simulation"
    else:
        source = "provided_report"
        origin = "provided_report"
    return {
        "source": source, "measurement_origin": origin,
        "reported_origin": document.get("measurement_origin") or document.get("source") or document.get("origin"),
        "task_id": document.get("task_id") or task.get("id"),
        "provider": document.get("provider") or task.get("provider") or task.get("model"),
        "success": status_metric,
        "started_at": document.get("started_at") or document.get("startedAt"),
        "started_at_reason": None if document.get("started_at") or document.get("startedAt") else "report does not include a start timestamp",
        "ended_at": document.get("ended_at") or document.get("endedAt"),
        "ended_at_reason": None if document.get("ended_at") or document.get("endedAt") else "report does not include an end timestamp",
        "duration_seconds": _metric(duration, duration_reason),
        "model_api_calls": _metric(model_calls, None if model_calls is not None else "report has no explicit model API-call counter"),
        "android_mcp_calls": _metric(android_calls, None if android_calls is not None else "report has no explicit Android MCP-call counter"),
        "motor_tool_calls": _metric(motor_calls, None if motor_calls is not None else "report has no explicit Motor tool-call counter"),
    }


def compare(baseline, motor):
    metrics = {}
    for name in ("duration_seconds", "model_api_calls", "android_mcp_calls", "motor_tool_calls"):
        left = baseline[name]["value"]
        right = motor[name]["value"]
        metrics[name] = ({"motor_minus_baseline": round(right - left, 3) if isinstance(left, (int, float)) and isinstance(right, (int, float)) else None,
                          "reason": None if isinstance(left, (int, float)) and isinstance(right, (int, float)) else "both measurements are required"})
    left_success, right_success = baseline["success"]["value"], motor["success"]["value"]
    return {"metrics": metrics,
            "success_match": {"value": left_success == right_success if type(left_success) is bool and type(right_success) is bool else None,
                              "reason": None if type(left_success) is bool and type(right_success) is bool else "both success states are required"}}


def build_benchmark(baseline, motor):
    return {"schema_version": "daxqo.motor.benchmark/0.1",
            "measurement_origins": {"baseline": baseline.get("measurement_origin", baseline["source"]),
                                    "motor": motor.get("measurement_origin", motor["source"])},
            "baseline": baseline, "motor": motor, "comparison": compare(baseline, motor)}


def _adb_run(adb, serial, *args):
    command = [adb] + (["-s", serial] if serial else []) + list(args)
    try:
        result = subprocess.run(command, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("ADB request failed") from exc
    if result.returncode:
        raise RuntimeError("ADB request failed; check that the device is connected and paired")
    return result.stdout.decode("utf-8", errors="replace").strip()


def fetch_harness_task(task_id, serial=None, *, adb=None, pairing_source=None, app_package=None):
    """GET one completed task over a temporary adb forward; never starts a task."""
    adb = adb or os.environ.get("DAXQO_ADB", "adb")
    pairing_source = pairing_source if pairing_source is not None else os.environ.get("DAXQO_PAIRING_SOURCE", "")
    app_package = app_package or os.environ.get("DAXQO_ANDROID_PACKAGE", "io.daxqo.app")
    ports = []
    token = None
    if pairing_source == "android-debug":
        if app_package not in {"io.daxqo.app", "io.daxqo.xm"}:
            raise ValueError("DAXQO_ANDROID_PACKAGE must select a paired DAXQO app")
        try:
            raw = _adb_run(adb, serial, "exec-out", "run-as", app_package, "cat", "shared_prefs/pairing_private.xml")
            prefs = ET.fromstring(raw)
            item = prefs.find("string[@name='capability']")
            token = item.text if item is not None else None
        except Exception as exc:
            raise RuntimeError("Could not read the paired app capability") from exc
    else:
        try:
            raw = _adb_run(adb, serial, "exec-out", "cat", "/sdcard/Download/daxqo-stage/gui-handoff.json")
            handoff = json.loads(raw)
            token = handoff.get("url", "").split("#token=", 1)[1]
        except Exception as exc:
            raise RuntimeError("No paired Harness handoff is available on the device") from exc
    if not isinstance(token, str) or len(token) < 32:
        raise RuntimeError("The DAXQO app is not paired with the Harness")
    try:
        forwarded = _adb_run(adb, serial, "forward", "tcp:0", "tcp:18880")
        if not forwarded.isdigit():
            raise RuntimeError("ADB returned an invalid forwarded port")
        ports.append(forwarded)
        path = "/api/tasks/" + urllib.parse.quote(str(task_id), safe="")
        request = urllib.request.Request(
            f"http://127.0.0.1:{forwarded}{path}",
            headers={"Host": "127.0.0.1:18880", "Authorization": "Bearer " + token},
            method="GET")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=45) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Harness task request failed with HTTP {exc.code}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("task"), dict):
            raise RuntimeError("Harness returned an invalid task response")
        if payload["task"].get("id") != str(task_id):
            raise RuntimeError("Harness returned a different task")
        return payload
    finally:
        token = None
        for port in reversed(ports):
            try:
                _adb_run(adb, serial, "forward", "--remove", "tcp:" + port)
            except RuntimeError:
                pass


def _read_report(path):
    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError) as exc:
        raise ValueError(f"Could not read report: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Report must be a JSON object: {path}")
    return value


def _short_summary(result):
    baseline, motor = result["baseline"], result["motor"]
    duration_delta = result["comparison"]["metrics"]["duration_seconds"]["motor_minus_baseline"]
    status = lambda item: str(item["success"]["value"]) if item["success"]["value"] is not None else "unknown"
    duration = f"{duration_delta:+.3f}s" if duration_delta is not None else "duration comparison unavailable"
    return f"Baseline ({baseline['source']}): {status(baseline)}; Motor ({motor['source']}): {status(motor)}; Motor − baseline: {duration}."


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-task-id")
    parser.add_argument("--motor-task-id")
    parser.add_argument("--baseline-report", type=Path)
    parser.add_argument("--motor-report", type=Path)
    parser.add_argument("--serial", help="ADB device serial for read-only Harness task retrieval")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    task_mode = bool(args.baseline_task_id or args.motor_task_id)
    report_mode = bool(args.baseline_report or args.motor_report)
    if task_mode == report_mode or (task_mode and not (args.baseline_task_id and args.motor_task_id)) or (report_mode and not (args.baseline_report and args.motor_report)):
        parser.error("provide both task IDs or both report paths")
    if args.serial and not task_mode:
        parser.error("--serial is only valid with task IDs")
    try:
        if task_mode:
            baseline = task_measurement(fetch_harness_task(args.baseline_task_id, args.serial), args.baseline_task_id)
            motor = task_measurement(fetch_harness_task(args.motor_task_id, args.serial), args.motor_task_id)
        else:
            baseline = report_measurement(_read_report(args.baseline_report), "baseline")
            motor = report_measurement(_read_report(args.motor_report), "motor")
        result = build_benchmark(baseline, motor)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (ValueError, RuntimeError) as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr)
        return 2
    print(_short_summary(result))
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
