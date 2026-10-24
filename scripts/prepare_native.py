"""Build a Motor overlay in its own staging directory; never edit source projects."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = Path(os.environ.get('DAXQO_COLLECTOR_ANDROID_SOURCE', ROOT / 'dist' / 'collector-source'))
DEFAULT_RECORDER = Path(os.environ.get('DAXQO_COLLECTOR_RECORDER',
    DEFAULT_SOURCE / 'android/app/src/main/java/io/daxqo/app/TrajectoryRecorder.java'))
STAGE = ROOT / 'dist' / 'native'
VERSION_CODE = 10


def replace(source: str, old: str, new: str, label: str) -> str:
    if source.count(old) != 1:
        raise ValueError('Native overlay anchor changed: ' + label)
    return source.replace(old, new, 1)


SERVICE_METHODS = r'''
    private final MotorGate motorGate = new MotorGate(android.os.SystemClock::elapsedRealtime);
    private final MotorEvents motorEvents = new MotorEvents();

    private String motorAppVersion(String pkg) {
        try {
            android.content.pm.PackageInfo info = getPackageManager().getPackageInfo(pkg, 0);
            return (info.versionName == null ? "" : info.versionName) + ":" + (Build.VERSION.SDK_INT >= 28 ? info.getLongVersionCode() : info.versionCode);
        } catch (Exception ignored) { return "unknown"; }
    }
    private int motorWindowType() {
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) return -1;
        android.view.accessibility.AccessibilityWindowInfo window = null;
        try { window = root.getWindow(); return window == null ? -1 : window.getType(); }
        finally { if (window != null) window.recycle(); root.recycle(); }
    }
    private JSONObject motorTree(JSONObject args) throws Exception {
        // One cache clear per requested tree, rather than a Binder refresh per node.
        // https://developer.android.com/reference/android/accessibilityservice/AccessibilityService#clearCache()
        boolean fresh = args.optBoolean("fresh", args.optBoolean("_motorSemanticOnly", false));
        boolean supported = Build.VERSION.SDK_INT >= 33;
        boolean cleared = fresh && supported && clearCache();
        return tree().put("freshRequested", fresh).put("freshSupported", supported).put("cacheCleared", cleared);
    }

    JSONObject motorStatus() throws Exception {
        return motorStatus(motorGate.snapshot());
    }
    private JSONObject motorStatus(MotorGate.Snapshot lease) throws Exception {
        JSONObject capabilities = new JSONObject().put("accessibility", current == this)
                .put("semantic_scroll", true).put("semantic_click", true).put("event_wait", true)
                .put("action_lease", true).put("screenshot", Build.VERSION.SDK_INT >= 30)
                .put("fresh_tree", Build.VERSION.SDK_INT >= 33)
                .put("cursor_input", cursorInputMethodSupported())
                .put("manual_takeover_detection", "touch_interaction_events_and_foreground_change");
        return new JSONObject().put("ok", true).put("version", "daxqo.motor/0.1")
                .put("capabilities", capabilities).put("generation", lease.generation)
                .put("lease_active", !lease.lease.isEmpty()).put("owner", lease.owner)
                .put("expected_package", lease.expectedPackage).put("expires_in_ms", lease.remaining)
                .put("eventSequence", motorEvents.sequence()).put("reason", lease.reason);
    }
    JSONObject motorEvents(long after, int timeout) throws Exception {
        if (after < 0 || timeout < 0 || timeout > 2000) return failure("", "invalid_args", "事件预算无效");
        long seq = motorEvents.await(after, timeout);
        return motorStatus().put("changed", seq > after).put("eventSequence", seq).put("sequence", seq)
                .put("event_type", motorEvents.type()).put("packageName", motorEvents.packageName())
                .put("scrollX", motorEvents.scrollX()).put("scrollY", motorEvents.scrollY())
                .put("fromIndex", motorEvents.fromIndex()).put("toIndex", motorEvents.toIndex())
                .put("itemCount", motorEvents.itemCount());
    }
    JSONObject motorAcquire(JSONObject request) throws Exception {
        synchronized (motorGate) {
            if (request.has("lease_id") && request.has("generation") && request.optLong("generation", -1) != motorGate.generation()) return failure("", "motor_cancelled", "续租代次已失效");
            String err = motorGate.acquire(request.optString("owner", ""), request.optString("expected_package", ""),
                    request.optLong("ttl_ms", 30000), request.optString("lease_id", ""));
            if (!err.isEmpty()) return failure("", err, "操作权不可用或租约参数无效");
            MotorGate.Snapshot lease = motorGate.snapshot();
            if (lease.lease.isEmpty()) return failure("", "motor_cancelled", "租约已失效");
            return motorStatus(lease).put("lease_id", lease.lease);
        }
    }
    JSONObject motorRelease(JSONObject request) throws Exception {
        if (!motorGate.release(request.optString("lease_id", ""))) return failure("", "motor_cancelled", "租约已失效");
        motorEvents.changed(0, "", -1, -1, -1, -1, -1);
        return motorStatus();
    }
    JSONObject motorCancel(JSONObject request) throws Exception {
        if (request.has("lease_id") && request.optString("lease_id", "").isEmpty()) return failure("", "motor_cancelled", "撤销租约标识无效");
        if (!motorGate.cancel(request.optString("lease_id", ""))) return failure("", "motor_cancelled", "旧租约不能撤销其他任务");
        motorEvents.changed(0, "", -1, -1, -1, -1, -1);
        return motorStatus();
    }
    private String motorCheck(JSONObject request) {
        if ("status".equals(request.optString("action"))) return "";
        JSONObject claim = request.optJSONObject("motor");
        if (request.has("motor") && (claim == null || claim.optString("lease_id", "").isEmpty()
                || claim.optString("owner", "").isEmpty() || !claim.has("generation"))) return "motor_cancelled";
        return motorGate.check(claim == null ? "" : claim.optString("lease_id", ""),
                claim == null ? "" : claim.optString("owner", ""),
                claim == null ? -1 : claim.optLong("generation", -1), foregroundPackage());
    }
    private void executeMotorMain(JSONObject request, String id, String action, JSONObject args, Completion callback) throws Exception {
        // Cancel/release cannot slip between the final fence and a synchronous native action.
        // An asynchronous OS action already dispatched cannot be undone; the next request is fenced.
        synchronized (motorGate) {
            String error = motorCheck(request);
            if (!error.isEmpty()) { callback.done(failure(id, error, "操作权已撤销，未执行")); return; }
            executeMain(id, action, args, callback);
        }
    }
    private void motorEvent(AccessibilityEvent event) {
        if (event == null) return;
        int type = event.getEventType();
        if (type == AccessibilityEvent.TYPE_TOUCH_INTERACTION_START && !motorGate.lease().isEmpty()) motorGate.revoke("manual_touch");
        if (type == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED && !motorGate.lease().isEmpty()) {
            String actual = foregroundPackage();
            if (!actual.isEmpty() && !motorGate.expectedPackage().equals(actual)) motorGate.revoke("foreground_changed");
        }
        motorEvents.changed(type, value(event.getPackageName(), 200), event.getScrollX(), event.getScrollY(),
                event.getFromIndex(), event.getToIndex(), event.getItemCount());
    }
    private String motorSemanticState(JSONObject node) {
        ArrayList<Integer> actions = new ArrayList<>();
        JSONArray source = node.optJSONArray("actions");
        for (int i = 0; source != null && i < source.length(); i++) actions.add(source.optInt(i));
        Collections.sort(actions);
        return "" + node.optBoolean("selected") + ":" + node.optBoolean("checked") + ":" + node.optBoolean("checkable")
                + ":" + node.optBoolean("scrollable") + ":" + node.optBoolean("clickable") + ":" + node.optBoolean("editable") + ":" + actions;
    }
    private void scrollNode(String id, JSONObject args, Completion callback) throws Exception {
        String nodeId = args.optString("nodeId", ""), observed = args.optString("snapshotId", "");
        String direction = args.optString("direction", "forward");
        if (nodeId.isEmpty() || nodeId.length() > 255 || observed.isEmpty() || observed.length() > 255
                || !("forward".equals(direction) || "backward".equals(direction))) {
            callback.done(failure(id, "invalid_args", "滚动需要有效节点、快照和方向")); return;
        }
        if (nodeSnapshot == null || !snapshotId.equals(observed)) {
            callback.done(failure(id, "stale_snapshot", "滚动快照已失效，请重新读取")); return;
        }
        AccessibilityNodeInfo root = getRootInActiveWindow(), target = null;
        if (root == null) { callback.done(failure(id, "no_window", "当前无可用窗口")); return; }
        try {
            target = nodeAt(root, nodeId);
            if (target == null || verifiedTarget(target, nodeId, observed, foregroundPackage(), screenSize()) == null) {
                callback.done(failure(id, "stale_snapshot", "滚动区域已变化，未执行")); return;
            }
            if (!target.isScrollable()) { callback.done(failure(id, "not_scrollable", "控件不支持滚动")); return; }
            JSONObject blocked = authorizeMutation(id, "scroll_node", args, sensitiveReason());
            if (blocked != null) { callback.done(blocked); return; }
            if (verifiedTarget(target, nodeId, observed, foregroundPackage(), screenSize()) == null) {
                callback.done(failure(id, "stale_snapshot", "滚动检查期间界面已变化，未执行")); return;
            }
            boolean performed = target.performAction("forward".equals(direction)
                    ? AccessibilityNodeInfo.ACTION_SCROLL_FORWARD : AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD);
            invalidateSnapshot();
            callback.done(success(id, new JSONObject().put("performed", performed).put("direction", direction)
                    .put("nodeId", nodeId).put("method", "accessibility")));
        } finally { if (target != null) target.recycle(); root.recycle(); }
    }
'''


def overlay_service(text: str) -> str:
    text = replace(text, '    private DeviceHttpBridge bridge;\n', '    private DeviceHttpBridge bridge;\n' + SERVICE_METHODS, 'motor service state')
    text = replace(text, '    @Override public void onAccessibilityEvent(AccessibilityEvent event) {\n',
                   '    @Override public void onAccessibilityEvent(AccessibilityEvent event) {\n        motorEvent(event);\n', 'events')
    text = replace(text, '    @Override public void onDestroy() {\n',
                   '    @Override public void onDestroy() {\n        motorGate.revoke("service_stopped");\n        motorEvents.changed(0, "", -1, -1, -1, -1, -1);\n', 'shutdown')
    text = replace(text, '        if ("status".equals(action)) return success(id, DeviceState.status(this));\n',
                   '        if ("status".equals(action)) return success(id, DeviceState.status(this));\n'
                   '        String motorError = motorCheck(request);\n'
                   '        if (!motorError.isEmpty()) return failure(id, motorError, "Motor操作权已失效或被其他任务占用");\n'
                   '        if (request.optJSONObject("motor") != null) { try { args.put("_motorSemanticOnly", true); } catch (Exception ignored) { } }\n', 'worker gate')
    text = replace(text, '            if (cancelled.get()) return;\n            final TrajectoryRecorder.Scope',
                   '            if (cancelled.get()) return;\n'
                   '            String gateError = motorCheck(request);\n'
                   '            if (!gateError.isEmpty()) { result[0] = failure(id, gateError, "操作权已被撤销，未执行"); finished.countDown(); return; }\n'
                   '            final TrajectoryRecorder.Scope', 'main dispatch fence')
    text = replace(text, 'try { executeMain(id, action, actionArgs, response -> {',
                   'try { executeMotorMain(request, id, action, actionArgs, response -> {', 'atomic native dispatch')
    text = replace(text, '                cancelled.set(true);\n                DeviceState.requireUser',
                   '                cancelled.set(true);\n                motorGate.revoke("action_timeout");\n                motorEvents.changed(0, "", -1, -1, -1, -1, -1);\n                DeviceState.requireUser', 'timeout')
    text = replace(text, '            case "tap_node": tapNode(id, args, callback); return;\n',
                   '            case "tap_node": tapNode(id, args, callback); return;\n            case "scroll_node": scrollNode(id, args, callback); return;\n', 'scroll action')
    text = replace(text, '            case "tree": callback.done(success(id, tree())); return;\n',
                   '            case "tree": callback.done(success(id, motorTree(args))); return;\n', 'fresh accessibility tree')
    text = replace(text, '    private JSONObject paused(String id) {\n',
                   '    private JSONObject paused(String id) {\n        if (!motorGate.lease().isEmpty()) { motorGate.revoke("control_paused"); motorEvents.changed(0, "", -1, -1, -1, -1, -1); }\n', 'control pause')
    text = replace(text, '            gesture(id, "tap", new JSONObject().put("x", x).put("y", y), response -> {\n',
                   '            if (args.optBoolean("_motorSemanticOnly")) { callback.done(failure(id, "semantic_click_unavailable", "控件无原生点击动作，请交由任务规划处理")); return; }\n'
                   '            gesture(id, "tap", new JSONObject().put("x", x).put("y", y), response -> {\n', 'no motor gesture fallback')
    old = '.put("enabled", node.isEnabled()).put("visible", node.isVisibleToUser()).put("password", password);'
    new = old[:-1] + '\n                .put("selected", node.isSelected()).put("checked", node.isChecked()).put("checkable", node.isCheckable())\n' \
          '                .put("windowId", node.getWindowId());\n' \
          '        JSONArray actions = new JSONArray();\n' \
          '        for (AccessibilityNodeInfo.AccessibilityAction action : node.getActionList()) actions.put(action.getId());\n' \
          '        item.put("actions", actions);'
    text = replace(text, old, new, 'node semantics')
    text = replace(text, 'return new JSONObject().put("snapshotId", snapshotId).put("packageName", snapshotPackage)',
                   'return new JSONObject().put("eventSequence", motorEvents.sequence()).put("windowType", motorWindowType()).put("packageVersion", motorAppVersion(snapshotPackage)).put("windowId", nodes.length() > 0 ? nodes.optJSONObject(0).optInt("windowId", -1) : -1).put("snapshotId", snapshotId).put("packageName", snapshotPackage)', 'tree events')
    text = replace(text, 'n.optBoolean("visible"), n.optBoolean("enabled"), n.optBoolean("password")));',
                   'n.optBoolean("visible"), n.optBoolean("enabled"), n.optBoolean("password"), motorSemanticState(n)));', 'snapshot semantic capture')
    return text


def overlay_snapshot(text: str) -> str:
    text = replace(text, '        final boolean visible, enabled, password;\n',
                   '        final boolean visible, enabled, password;\n        final String semanticState;\n', 'snapshot state field')
    anchor = '             int left, int top, int right, int bottom, boolean visible, boolean enabled, boolean password) {\n'
    updated = anchor + '            this(id, text, description, viewId, className, left, top, right, bottom, visible, enabled, password, "");\n        }\n' \
              '        Node(String id, String text, String description, String viewId, String className,\n' \
              '             int left, int top, int right, int bottom, boolean visible, boolean enabled, boolean password, String semanticState) {\n' \
              '            this.semanticState = semanticState;\n'
    text = replace(text, anchor, updated, 'snapshot state constructor')
    text = replace(text, '&& enabled == other.enabled && password == other.password;',
                   '&& enabled == other.enabled && password == other.password && Objects.equals(semanticState, other.semanticState);', 'snapshot state identity')
    return text


BRIDGE_GET = r'''
            if ("GET".equals(start[0]) && start[1].startsWith("/api/motor/")) {
                if ("/api/motor/status".equals(start[1])) { reply(client, 200, service.motorStatus()); return; }
                if (start[1].startsWith("/api/motor/events?")) {
                    android.net.Uri uri = android.net.Uri.parse(start[1]);
                    try { reply(client, 200, service.motorEvents(Long.parseLong(uri.getQueryParameter("after")), Integer.parseInt(uri.getQueryParameter("timeoutMs")))); }
                    catch (IllegalArgumentException ignored) { reply(client, 400, error("invalid_args", "事件参数无效")); }
                    return;
                }
                reply(client, 404, error("not_found", "Motor接口不存在")); return;
            }
'''

BRIDGE_POST = r'''
            if (motorRoute) {
                if (start[1].endsWith("/cancel")) { reply(client, 200, service.motorCancel(request)); return; }
                if (start[1].endsWith("/release")) { reply(client, 200, service.motorRelease(request)); return; }
                synchronized (actions) { reply(client, 200, service.motorAcquire(request)); }
                return;
            }
'''


def overlay_bridge(text: str) -> str:
    anchor = '            TrajectoryRecorder recorder = service.trajectory();\n'
    text = replace(text, anchor, BRIDGE_GET + anchor, 'motor GET routes')
    anchor = '            boolean trajectoryRoute = start[1].matches("/api/trajectory/(config|ack|feedback)");\n'
    text = replace(text, anchor, anchor + '            boolean motorRoute = start[1].matches("/api/motor/(lease|release|cancel)");\n', 'motor POST allowlist')
    text = replace(text, ' && !approvalDecision && !trajectoryRoute))', ' && !approvalDecision && !trajectoryRoute && !motorRoute))', 'POST validation')
    text = replace(text, '            if (trajectoryRoute) {\n', BRIDGE_POST + '            if (trajectoryRoute) {\n', 'motor POST routes')
    return text


def prepare(source: Path = DEFAULT_SOURCE, stage: Path = STAGE, recorder: Path | None = None) -> Path:
    source, stage = source.resolve(), stage.resolve()
    if not (source / 'android' / 'app' / 'src' / 'main' / 'java' / 'io' / 'daxqo' / 'app' / 'TrajectoryRecorder.java').is_file():
        raise FileNotFoundError('Existing collector native source is required (--source)')
    if stage == source or source in stage.parents or stage in source.parents:
        raise ValueError('Motor staging must be separate from source')
    stage.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source / 'android', stage / 'android', dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('build', '.gradle', '.git', '.tools', 'local.properties'))
    java = stage / 'android' / 'app' / 'src' / 'main' / 'java' / 'io' / 'daxqo' / 'app'
    manifests = {}
    for name, overlay in [('DeviceAccessibilityService.java', overlay_service), ('DeviceHttpBridge.java', overlay_bridge), ('NodeSnapshot.java', overlay_snapshot)]:
        path = java / name
        original = path.read_text(encoding='utf-8')
        manifests[name] = hashlib.sha256(original.encode()).hexdigest()
        path.write_text(overlay(original), encoding='utf-8', newline='\n')
    for name in ('MotorGate.java', 'MotorEvents.java'):
        shutil.copy2(ROOT / 'native' / name, java / name)
    recorder = (recorder or source / 'android/app/src/main/java/io/daxqo/app/TrajectoryRecorder.java').resolve()
    recorder_bytes = recorder.read_bytes()
    recorder_text = recorder_bytes.decode('utf-8')
    for signature in ('package io.daxqo.app;', 'Scope begin(JSONObject request)',
                      'void finish(Scope scope, JSONObject response, long actionMs, int settleMs)',
                      'void event(AccessibilityEvent event)', 'JSONObject batch(long after, int limit)',
                      'JSONObject ack(JSONObject body)', 'service.trajectoryTree()'):
        if signature not in recorder_text:
            raise ValueError('Collector recorder API changed; refusing an incompatible overlay')
    (java / 'TrajectoryRecorder.java').write_bytes(recorder_bytes)
    manifests['TrajectoryRecorder.java'] = hashlib.sha256(recorder_bytes).hexdigest()
    xml = stage / 'android' / 'app' / 'src' / 'main' / 'res' / 'xml' / 'device_accessibility.xml'
    value = xml.read_text(encoding='utf-8')
    value = replace(value, 'typeViewScrolled"', 'typeViewScrolled|typeTouchInteractionStart|typeTouchInteractionEnd"', 'touch takeover events')
    xml.write_text(value, encoding='utf-8', newline='\n')
    gradle = stage / 'android' / 'app' / 'build.gradle'
    value = gradle.read_text(encoding='utf-8')
    pattern = r"versionCode project\.hasProperty\('trajectoryBuild'\) \? [0-9]+ : 5"
    if len(re.findall(pattern, value)) != 1:
        raise ValueError('Collector versionCode anchor changed')
    value = re.sub(pattern, 'versionCode ' + str(VERSION_CODE), value, count=1)
    gradle.write_text(value, encoding='utf-8', newline='\n')
    (stage / 'overlay-manifest.json').write_text(json.dumps({'motor_version': '0.1', 'version_code': VERSION_CODE,
        'source': str(source), 'collector_recorder_source': str(recorder), 'source_sha256': manifests}, indent=2), encoding='utf-8')
    return stage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=DEFAULT_SOURCE)
    parser.add_argument('--stage', type=Path, default=STAGE)
    parser.add_argument('--collector-recorder', type=Path, help='Explicit recorder override; otherwise use --source content')
    args = parser.parse_args()
    print('Motor native source prepared: ' + str(prepare(args.source, args.stage, args.collector_recorder)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
