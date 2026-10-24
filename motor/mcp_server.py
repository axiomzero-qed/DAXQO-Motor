"""Small newline-delimited JSON-RPC MCP server for DAXQO Motor."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import queue
import signal
import sys
import threading

from .engine import REQUEST_CANCEL, REQUEST_GUARD
from .models import Action, Budget, Selector

MAX_LINE = 1024 * 1024
MAX_TEXT = 3000  # Fits the existing Harness's 2000-token tool budget, including Chinese.
TOOL_DEFINITIONS = [
    {"name": "motor_scout", "description": "Explore an app with bounded Motor actions.", "inputSchema": {
        "type": "object", "properties": {"package": {"type": "string"}, "mode": {"type": "string", "enum": ["quick", "deep"]}, "goal": {"type": "string"}, "seconds": {"type": "number"}, "max_scrolls": {"type": "integer"}, "max_depth": {"type": "integer"}}, "required": ["package"], "additionalProperties": False}},
    {"name": "motor_map", "description": "Summarize Motor's learned app map.", "inputSchema": {
        "type": "object", "properties": {"package": {"type": "string"}, "query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["package"], "additionalProperties": False}},
    {"name": "motor_navigate", "description": "Navigate to a locally observed page using verified map routes and bounded local search.", "inputSchema": {
        "type": "object", "properties": {"package": {"type": "string"}, "query": {"type": "string"}, "open_target": {"type": "boolean"}}, "required": ["package", "query"], "additionalProperties": False}},
    {"name": "motor_reflex", "description": "Perform one policy-checked Motor action.", "inputSchema": {
        "type": "object", "properties": {"package": {"type": "string"}, "action": {"type": "object"}}, "required": ["package", "action"], "additionalProperties": False}},
    {"name": "motor_cancel", "description": "Cancel the active Motor task.", "inputSchema": {
        "type": "object", "properties": {"reason": {"type": "string"}}, "additionalProperties": False}},
]
_PUBLIC_DROP = {"tree", "nodes", "state", "before", "after", "screenshot", "raw", "reasoning", "thought", "chain_of_thought", "hidden_reasoning"}


class MCPError(ValueError):
    pass


def _object(value, name):
    if not isinstance(value, dict):
        raise MCPError(f"{name} must be an object")
    return value


def _keys(value, allowed, name):
    unknown = set(value) - set(allowed)
    if unknown:
        raise MCPError(f"Unknown {name} field: {sorted(unknown)[0]}")


def _string(value, name, *, required=True, limit=255):
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > limit:
        raise MCPError(f"{name} must be a string of at most {limit} characters")
    return value


def _public(value, depth=0):
    if depth > 6:
        return None
    if isinstance(value, dict):
        return {str(k): _public(v, depth + 1) for k, v in value.items()
                if str(k).casefold() not in _PUBLIC_DROP}
    if isinstance(value, (list, tuple)):
        return [_public(v, depth + 1) for v in value[:100]]
    if value is None or type(value) in (str, int, float, bool):
        return value[:2000] if isinstance(value, str) else value
    public = getattr(value, "summary", None)
    return _public(public(), depth + 1) if callable(public) else str(value)[:200]


def _tool_text(value, request_id=None):
    public = _public(value)
    if request_id is not None:
        if not isinstance(public, dict):
            public = {"result": public}
        # JSON-RPC request correlation only; never place this in decision.tool_call_id.
        public["mcp_request_id"] = request_id
    raw = json.dumps(public, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if isinstance(public, dict) and public.get("schema_version") == "daxqo.motor.scout/0.1":
        public = _scout_brief(public)
        raw = json.dumps(public, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    elif len(raw) > MAX_TEXT:
        public = _compact_report(public)
        raw = json.dumps(public, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return {"content": [{"type": "text", "text": raw}]}


def _scout_brief(value):
    """Task facts precede navigation details so downstream token clipping stays useful."""
    from collections import Counter
    pages = value.get("pages", [])
    goal = str(value.get("goal", ""))[:160]
    repeated = Counter(label for page in pages for label in set(page.get("labels", [])))
    pairs = {goal[i:i+2] for i in range(max(0, len(goal)-1))}

    def relevance(page):
        title = str(page.get("title", "")).splitlines()[-1:]
        title_score = 100 if title and len(title[0]) >= 2 and title[0] in goal else 0
        facts = " ".join(page.get("labels", []))
        return title_score + sum(pair in facts for pair in pairs)

    summaries = []
    nav = {}
    for page in sorted(pages, key=relevance, reverse=True):
        title = str(page.get("title", "")).splitlines()[-1:]
        labels = [str(text)[:120] for text in page.get("labels", []) if text != page.get("title")
                  and repeated[text] <= max(1, len(pages) // 2)]
        # Put shared navigation once, rather than repeating every selector on every page.
        local_controls = []
        for selector in page.get("controls", []):
            resource = selector.get("resource_id", "") if isinstance(selector, dict) else ""
            label = selector.get("text") or selector.get("description", "") if isinstance(selector, dict) else ""
            if label and ("tab_" in resource or "nav_" in resource):
                if len(nav) < 10:
                    nav[str(label)[:40]] = {"resource_id": str(resource)[:180]}
            elif isinstance(selector, dict):
                local_controls.append({k: str(v)[:160] for k, v in selector.items() if k in {"resource_id", "text", "description"}})
        content = page.get("content", [])
        summaries.append({"title": title[0][:80] if title else "", "findings": labels[:5],
                          "items": len(content), "samples": [str(s)[:120] for s in dict.fromkeys(content[:1] + content[-1:])],
                          "entry": local_controls[:1], "stop": page.get("stop_reason", "")})
    metrics = value.get("metrics", {})
    result = {"ok": value.get("ok", value.get("escalation") is None), "goal": goal,
              "pages": summaries, "restoration": {k: value.get("restoration", {}).get(k) for k in ("status", "exact")},
              "escalation": _shorten(value.get("escalation"), 160), "navigation": nav,
              "metrics": {k: metrics.get(k) for k in ("elapsed_ms", "cloud_calls", "pages", "visited_tabs", "discovered_tabs", "tab_coverage")},
              "mcp_request_id": _shorten(value.get("mcp_request_id"), 80), "truncated": True,
              "detail": "Full verified routes remain in SQLite; motor_map can query one function."}
    # Keep all page headings and task facts. Trim unrelated samples/entries before facts.
    def size():
        return len(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    if size() > MAX_TEXT:
        for page in reversed(summaries):
            page.pop("entry", None)
            page.pop("samples", None)
            if size() <= MAX_TEXT:
                break
    if size() > MAX_TEXT:
        for page in reversed(summaries):
            page["findings"] = page["findings"][:2]
            if size() <= MAX_TEXT:
                break
    while size() > MAX_TEXT and len(summaries) > 1:
        summaries.pop()
    result["page_count"] = len(pages)
    if size() > MAX_TEXT:
        result.pop("navigation", None)
        result.pop("detail", None)
    return result


def _page_summary(page, *, labels=10, content=10, controls=8):
    if not isinstance(page, dict):
        return _public(page)
    fields = ("id", "page_fingerprint", "title", "stop_reason", "task_matches", "labels",
              "content", "content_samples", "controls", "devices")
    result = {key: page[key] for key in fields if key in page}
    for key, limit in (("task_matches", labels), ("labels", labels), ("content", content),
                       ("content_samples", content), ("controls", controls), ("devices", 2)):
        if isinstance(result.get(key), list):
            result[key] = result[key][:limit]
    return _shorten(_public(result))


def _shorten(value, max_text=180):
    if isinstance(value, str):
        return value[:max_text]
    if isinstance(value, dict):
        return {key: _shorten(item, max_text) for key, item in value.items()}
    if isinstance(value, list):
        return [_shorten(item, max_text) for item in value]
    return value


def _edge_summary(edge):
    if not isinstance(edge, dict):
        return _public(edge)
    result = {key: edge[key] for key in ("from", "to", "verified", "confidence", "source", "last_verified") if key in edge}
    action = edge.get("action")
    if isinstance(action, dict):
        result["action"] = {key: action[key] for key in ("kind", "type", "direction", "purpose", "selector") if key in action}
        if isinstance(result["action"].get("selector"), dict):
            result["action"]["selector"] = {key: result["action"]["selector"][key]
                                              for key in ("resource_id", "text", "description", "role")
                                              if key in result["action"]["selector"]}
    return _shorten(_public(result))


def _compact_report(value):
    """Keep actionable public findings while trimming large scout/map reports."""
    if not isinstance(value, dict):
        return {"summary": _public(value), "truncated": True}
    keys = ("ok", "schema_version", "mode", "stop_reason", "restoration", "escalation",
            "metrics", "current", "reused_map", "shared_semantics", "invalidated", "mcp_request_id")
    base = {key: value[key] for key in keys if key in value}
    if isinstance(base.get("current"), dict):
        current = dict(base["current"])
        for key, limit in (("labels", 12), ("controls", 8)):
            if isinstance(current.get(key), list):
                current[key] = current[key][:limit]
        base["current"] = _shorten(current)
    base["truncated"] = True
    top_pages = value.get("pages") if isinstance(value.get("pages"), list) else None
    app_map = value.get("app_map") if isinstance(value.get("app_map"), dict) else None
    if top_pages is not None:
        base["page_count"] = len(top_pages)
    elif "total_pages" in value:
        base["total_pages"] = value["total_pages"]
    if "coverage" in value:
        base["coverage"] = value["coverage"]
    if "entry_points" in value:
        base["entry_points"] = value["entry_points"][:12] if isinstance(value["entry_points"], list) else value["entry_points"]
    if top_pages is not None or app_map is not None:
        for page_limit, item_limit in ((8, 10), (5, 6), (3, 4), (1, 2), (0, 0)):
            candidate = dict(base)
            if top_pages is not None:
                candidate["pages"] = [_page_summary(page, labels=item_limit, content=item_limit,
                                                    controls=max(2, item_limit))
                                      for page in top_pages[:page_limit]]
            if app_map is not None:
                compact_map = {}
                for key in ("total_pages", "coverage", "invalidated", "entry_points"):
                    if key in app_map:
                        compact_map[key] = app_map[key]
                if isinstance(app_map.get("pages"), list):
                    compact_map["pages"] = [_page_summary(page, labels=item_limit, content=item_limit,
                                                          controls=max(2, item_limit))
                                             for page in app_map["pages"][:page_limit]]
                if isinstance(app_map.get("edges"), list):
                    compact_map["edges"] = [_edge_summary(edge) for edge in app_map["edges"][:page_limit * 2]]
                candidate["app_map"] = compact_map
            if isinstance(value.get("edges"), list):
                candidate["edges"] = [_edge_summary(edge) for edge in value["edges"][:page_limit * 2]]
            encoded = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            if len(encoded) <= MAX_TEXT:
                return candidate
    # Extreme metrics or error strings still get a useful bounded response.
    fallback = {key: base[key] for key in ("ok", "mode", "stop_reason", "restoration", "escalation", "mcp_request_id") if key in base}
    if isinstance(base.get("metrics"), dict):
        fallback["metrics"] = {key: base["metrics"][key] for key in
                               ("pages", "actions", "scrolls", "elapsed_ms", "restored", "visited_tabs", "discovered_tabs")
                               if key in base["metrics"]}
    if isinstance(base.get("current"), dict):
        fallback["current"] = {key: base["current"][key] for key in ("title", "page_fingerprint", "excluded")
                                if key in base["current"]}
    fallback.update({"page_count": base.get("page_count", base.get("total_pages", 0)), "pages": [], "truncated": True})
    if "coverage" in base:
        fallback["coverage"] = base["coverage"]
    return fallback


def _parse_action(value):
    value = _object(value, "action")
    allowed = {"kind", "selector", "direction", "purpose", "expected_text", "expected_page", "wait_ms", "id"}
    _keys(value, allowed, "action")
    kind = value.get("kind")
    if kind not in ("CLICK", "SCROLL", "BACK", "WAIT"):
        raise MCPError("action.kind must be CLICK, SCROLL, BACK, or WAIT")
    selector = value.get("selector")
    if selector is not None:
        selector = _object(selector, "selector")
        _keys(selector, {"resource_id", "text", "description", "role"}, "selector")
        selector = Selector(**{k: _string(v, "selector." + k, required=False, limit=500) or ""
                               for k, v in selector.items()})
    direction = value.get("direction", "forward")
    if direction not in ("forward", "backward", "up", "down", "left", "right"):
        raise MCPError("Invalid action.direction")
    if kind == "SCROLL" and direction not in ("forward", "backward"):
        raise MCPError("SCROLL direction must be forward or backward")
    purpose = value.get("purpose", "navigation")
    if purpose not in ("navigation", "observation", "task"):
        raise MCPError("Invalid action.purpose")
    wait_ms = value.get("wait_ms", 500)
    if type(wait_ms) is not int or not 0 <= wait_ms <= 10000:
        raise MCPError("action.wait_ms must be an integer in [0,10000]")
    return Action(kind, selector, direction, purpose,
                  _string(value.get("expected_text", ""), "expected_text", required=False, limit=500) or "",
                  _string(value.get("expected_page", ""), "expected_page", required=False, limit=500) or "",
                  wait_ms, _string(value.get("id", ""), "action.id", required=False, limit=100) or "")


class MotorMCP:
    def __init__(self, motor, task_guard_factory=None, source="mcp"):
        self.motor = motor
        self.task_guard_factory = task_guard_factory
        self.source = source

    def _refresh_task(self):
        sink = getattr(self.motor, "sink", None)
        if sink is None:
            return
        try:
            status = sink.client.request("/api/trajectory/status")
            if isinstance(status, dict) and status.get("ok") is False:
                error = status.get("error")
                code = error.get("code", "status_rejected") if isinstance(error, dict) else "status_rejected"
                sink.errors.append(f"/api/trajectory/status: {code}")
                return
            task = status.get("task") if isinstance(status, dict) else None
            if isinstance(task, dict) and isinstance(task.get("id"), str) and task["id"].strip():
                sink.task = dict(task)
        except Exception as exc:
            sink.errors.append(f"/api/trajectory/status: {type(exc).__name__}")

    def call_tool(self, name, args, request_id=None):
        args = _object(args, "arguments")
        package = _string(args.get("package"), "package") if name != "motor_cancel" else None
        if name == "motor_scout":
            _keys(args, {"package", "mode", "goal", "seconds", "max_scrolls", "max_depth"}, name)
            mode = args.get("mode", "quick")
            if mode not in ("quick", "deep"):
                raise MCPError("mode must be quick or deep")
            seconds = args.get("seconds", 30)
            scrolls, depth = args.get("max_scrolls", 8), args.get("max_depth", 2)
            if type(seconds) not in (int, float) or not 0 < seconds <= 110:
                raise MCPError("seconds must be in (0,110]")
            if type(scrolls) is not int or not 0 <= scrolls <= 100:
                raise MCPError("max_scrolls must be an integer in [0,100]")
            if type(depth) is not int or not 0 <= depth <= 10:
                raise MCPError("max_depth must be an integer in [0,10]")
            goal = _string(args.get("goal", ""), "goal", required=False, limit=2000) or ""
            return _tool_text(self.motor.scout(package, mode=mode, goal=goal,
                              budget=Budget(seconds=float(seconds), max_scrolls=scrolls, max_depth=depth)), request_id)
        if name == "motor_map":
            _keys(args, {"package", "query", "limit"}, name)
            query = _string(args.get("query"), "query", required=False, limit=500)
            limit = args.get("limit", 8)
            if type(limit) is not int or not 1 <= limit <= 100:
                raise MCPError("limit must be an integer in [1,100]")
            return _tool_text(self.motor.brief(package, query=query, limit=limit), request_id)
        if name == "motor_navigate":
            _keys(args, {"package", "query", "open_target"}, name)
            query = _string(args.get("query"), "query", limit=500)
            open_target = args.get("open_target", False)
            if type(open_target) is not bool:
                raise MCPError("open_target must be a boolean")
            return _tool_text(self.motor.navigate(package, query, open_target=open_target), request_id)
        if name == "motor_reflex":
            _keys(args, {"package", "action"}, name)
            return _tool_text(self.motor.act(package, _parse_action(args.get("action")), source=self.source), request_id)
        if name == "motor_cancel":
            _keys(args, {"reason"}, name)
            reason = _string(args.get("reason", "cancelled"), "reason", limit=500)
            return _tool_text(self.motor.cancel(reason), request_id)
        raise MCPError("Unknown Motor tool")

    def dispatch(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return {"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None,
                    "error": {"code": -32600, "message": "Invalid JSON-RPC request"}}
        ident, method = message.get("id"), message["method"]
        if ident is None:
            return None
        if method == "initialize":
            params = message.get("params") or {}
            version = params.get("protocolVersion", "2024-11-05") if isinstance(params, dict) else None
            if version not in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
                return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": "Unsupported MCP protocol version"}}
            result = {"protocolVersion": version, "capabilities": {"tools": {}},
                      "serverInfo": {"name": "daxqo-motor", "version": "0.1.0"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOL_DEFINITIONS}
        elif method == "tools/call":
            params = message.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": "tools/call requires a tool name"}}
            if params["name"] != "motor_cancel":
                self._refresh_task()
            try:
                if self.task_guard_factory and params['name'] in {'motor_scout', 'motor_reflex', 'motor_navigate'} and REQUEST_GUARD.get() is None:
                    raise MCPError('harness_task_guard_unbound')
                result = self.call_tool(params["name"], params.get("arguments", {}), request_id=ident)
            except (MCPError, TypeError, ValueError) as exc:
                result = _tool_text({"ok": False, "error": str(exc)}, ident)
                result["isError"] = True
            except Exception as exc:
                result = _tool_text({"ok": False, "error": "motor operation failed", "type": type(exc).__name__}, ident)
                result["isError"] = True
        else:
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "Method not found"}}
        return {"jsonrpc": "2.0", "id": ident, "result": result}

    def serve(self, source=None, sink=None):
        source, sink = source or sys.stdin.buffer, sink or sys.stdout.buffer
        jobs, pending, seen, silent = queue.Queue(maxsize=16), {}, set(), set()
        state_lock, output_lock = threading.Lock(), threading.Lock()
        def error(ident, code, message):
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}
        def cancel(event, reason):
            with state_lock:
                if event not in pending.values():
                    return
                silent.add(event)
            event.set()
            notify = getattr(self.motor, "cancel_request", None)
            if notify is not None:
                notify(event, reason)
        def cancel_all(reason):
            with state_lock:
                events = list(pending.values())
                silent.update(events)
            for event in events:
                event.set()
            for event in events:
                cancel(event, reason)
        def write(response):
            if response is None:
                return
            with output_lock:
                sink.write((json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
                sink.flush()
        def worker():
            while (job := jobs.get()) is not None:
                message, event, guard = job
                token = REQUEST_CANCEL.set(event)
                guard_token = REQUEST_GUARD.set(guard)
                try:
                    response = self.dispatch(message) if not event.is_set() else None
                    with state_lock:
                        if event in silent:
                            response = None  # Only transport/user cancellations suppress the result.
                    write(response)
                except (OSError, ValueError):
                    cancel_all("transport_closed")
                finally:
                    REQUEST_CANCEL.reset(token)
                    REQUEST_GUARD.reset(guard_token)
                    with state_lock:
                        pending.pop(message["id"], None)
                        silent.discard(event)
        thread = threading.Thread(target=worker, name="daxqo-motor-mcp")
        old_handlers = {}
        def terminate(_signum, _frame):
            raise SystemExit(0)
        thread.start()
        try:
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGTERM, signal.SIGINT):
                    old_handlers[signum] = signal.signal(signum, terminate)
            while line := source.readline(MAX_LINE + 1):
                if len(line) > MAX_LINE:
                    write(error(None, -32600, "Request line too large"))
                    while line and not line.endswith(b"\n"):
                        line = source.readline(MAX_LINE + 1)
                    continue
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError):
                    write(error(None, -32700, "Parse error"))
                    continue
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    write(self.dispatch(message))
                    continue
                ident, method, params = message.get("id"), message.get("method"), message.get("params")
                if method == "notifications/cancelled" and "id" not in message:
                    request_id = params.get("requestId") if isinstance(params, dict) else None
                    if isinstance(params, dict) and "reason" in params and not isinstance(params["reason"], str):
                        continue
                    with state_lock:
                        event = pending.get(request_id) if type(request_id) in (int, str) else None
                    if event is not None and not event.is_set():
                        cancel(event, "request_cancelled")
                    continue
                if ident is not None:
                    if type(ident) not in (int, str) or ident in seen:
                        write(error(None, -32600, "Invalid or duplicate request id"))
                        continue
                    seen.add(ident)  # Never let a late notification target a reused ID.
                if method == "tools/call" and ident is not None:
                    if isinstance(params, dict) and params.get("name") == "motor_cancel":
                        args = params.get("arguments", {})
                        if isinstance(args, dict) and not set(args) - {"reason"} and isinstance(args.get("reason", "cancelled"), str) and args.get("reason", "cancelled").strip() and len(args.get("reason", "cancelled")) <= 500:
                            cancel_all(args.get("reason", "cancelled"))
                        write(self.dispatch(message))
                        continue
                    guard = None
                    if self.task_guard_factory and isinstance(params, dict) and params.get('name') in {'motor_scout', 'motor_reflex', 'motor_navigate'}:
                        try:
                            guard = self.task_guard_factory()  # Bind on arrival, never in the worker.
                        except Exception as exc:
                            from .task_guard import GuardError
                            reason = str(exc) if isinstance(exc, GuardError) else 'harness_guard_unavailable'
                            result = _tool_text({'ok': False, 'escalation': {'required': True, 'reason': reason}}, ident)
                            result['isError'] = True
                            write({'jsonrpc': '2.0', 'id': ident, 'result': result})
                            continue
                    with state_lock:
                        if len(pending) >= 16:
                            write(error(ident, -32000, "Too many pending requests"))
                            continue
                        event = pending[ident] = threading.Event()
                    jobs.put_nowait((message, event, guard))
                else:
                    write(self.dispatch(message))
        finally:
            for signum in old_handlers:
                signal.signal(signum, signal.SIG_IGN)
            try:
                cancel_all("transport_closed")
                jobs.put(None)
                thread.join()
            finally:
                for signum, handler in old_handlers.items():
                    signal.signal(signum, handler)
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, help="DAXQO bridge pairing directory")
    parser.add_argument("--serial", help="USB device running a compatible Android bridge")
    parser.add_argument("--adb")
    parser.add_argument("--android-package")
    parser.add_argument("--device-id", help="Stable UUID for bridges without an identity endpoint")
    parser.add_argument("--adapter", help="Trusted local module:factory implementing DeviceAdapter")
    parser.add_argument("--adapter-config", type=Path, help="JSON keyword arguments for the adapter factory")
    parser.add_argument("--map-db", type=Path, default=Path.home() / ".local/share/daxqo-motor/map.sqlite3")
    parser.add_argument("--collector-root", type=Path)
    parser.add_argument("--harness-task-guard", action="store_true")
    parser.add_argument("--trajectory-feedback", action="store_true", help="Attach to an existing bridge recording session; never ACK")
    parser.add_argument("--source", default="mcp", help="Public caller attribution")
    args = parser.parse_args(argv)
    try:
        from contextlib import ExitStack
        if args.collector_root:
            from .trajectory import load_collector
            load_collector(args.collector_root)
        from .engine import Motor
        from .adapter import AndroidAdapter
        from .app_map import MapStore
        from .trajectory import CollectorSink
        from .transport import linux_transport, usb_transport
        if sum(bool(value) for value in (args.adapter, args.serial, args.state_dir)) != 1:
            parser.error("Specify exactly one of --adapter, --serial, or --state-dir")
        if args.harness_task_guard and not args.state_dir:
            parser.error("--harness-task-guard is specific to the DAXQO --state-dir adapter")
        with ExitStack() as resources:
            if args.adapter:
                from .plugins import load_adapter
                adapter = load_adapter(args.adapter, args.adapter_config)
                if callable(getattr(adapter, "close", None)):
                    resources.callback(adapter.close)
                if args.trajectory_feedback:
                    parser.error("--trajectory-feedback requires the Android bridge adapter")
                sink = None
            else:
                transport = resources.enter_context(usb_transport(args.serial, args.adb, package=args.android_package) if args.serial
                                                    else linux_transport(args.state_dir, device_id=args.device_id))
                adapter = AndroidAdapter(transport)
                # The existing DAXQO launcher passes --collector-root. Keep that
                # explicit opt-in compatible without making all hosts collect.
                sink = CollectorSink(transport, {}) if args.trajectory_feedback or args.collector_root else None
            store = resources.enter_context(MapStore(args.map_db))
            store.bind_local_device(adapter.device_id)
            engine = Motor(adapter, store, sink)
            from .task_guard import HarnessTaskGuard
            factory = (lambda: HarnessTaskGuard.bind(args.state_dir)) if args.harness_task_guard else None
            return MotorMCP(engine, task_guard_factory=factory, source=args.source).serve()
    except Exception as exc:
        print(f"Motor MCP startup failed: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
