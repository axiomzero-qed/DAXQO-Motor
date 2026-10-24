"""Behavioral native arbitration tests plus non-destructive overlay checks."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('motor_native_overlay', ROOT / 'scripts' / 'prepare_native.py')
overlay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overlay)
bundle_spec = importlib.util.spec_from_file_location('motor_source_bundle', ROOT / 'scripts' / 'package_motor.py')
bundle = importlib.util.module_from_spec(bundle_spec)
bundle_spec.loader.exec_module(bundle)


class NativeOverlayTests(unittest.TestCase):
    def test_anchor_drift_fails_closed(self):
        with self.assertRaises(ValueError):
            overlay.overlay_service('new unrelated implementation')

    def test_bridge_preserves_auth_idempotency_and_collector(self):
        source = overlay.DEFAULT_SOURCE / 'android' / 'app' / 'src' / 'main' / 'java' / 'io' / 'daxqo' / 'app'
        if not source.exists():
            self.skipTest('Existing collector native source not available')
        original = (source / 'DeviceHttpBridge.java').read_text(encoding='utf-8')
        patched = overlay.overlay_bridge(original)
        for behavior in ('MessageDigest.isEqual', 'saveJournal()', 'synchronized (actions)',
                         '/api/trajectory/(config|ack|feedback)', 'service.decideApproval'):
            self.assertIn(behavior, patched)
        self.assertIn('/api/motor/(lease|release|cancel)', patched)
        service = overlay.overlay_service((source / 'DeviceAccessibilityService.java').read_text(encoding='utf-8'))
        self.assertIn('executeMotorMain(request, id, action, actionArgs', service)
        self.assertIn('synchronized (motorGate)', service)
        self.assertIn('AccessibilityNodeInfo.ACTION_SCROLL_FORWARD', service)
        self.assertIn('semantic_click_unavailable', service)

    def test_gate_event_regressions_in_jvm(self):
        candidate = Path(os.environ.get('JAVA_HOME', '/jdk-not-configured')) / 'bin'
        javac = shutil.which('javac') or (str(candidate / 'javac.exe') if (candidate / 'javac.exe').exists() else None)
        java = shutil.which('java') or (str(candidate / 'java.exe') if (candidate / 'java.exe').exists() else None)
        if not javac or not java:
            self.skipTest('A JDK is required for native gate tests')
        with tempfile.TemporaryDirectory() as temp:
            subprocess.run([javac, '-encoding', 'UTF-8', '-d', temp,
                            *[str(ROOT / 'native' / name) for name in ('MotorGate.java', 'MotorEvents.java', 'MotorGateRegression.java')]],
                           check=True, capture_output=True, timeout=30)
            run = subprocess.run([java, '-cp', temp, 'io.daxqo.app.MotorGateRegression'],
                                 check=True, capture_output=True, text=True, timeout=10)
            self.assertIn('regressions passed', run.stdout)

    def test_fresh_tree_clears_once_with_motor_default_and_sdk_guard(self):
        candidate = Path(os.environ.get('JAVA_HOME', '/jdk-not-configured')) / 'bin'
        javac = shutil.which('javac') or (str(candidate / 'javac.exe') if (candidate / 'javac.exe').exists() else None)
        java = shutil.which('java') or (str(candidate / 'java.exe') if (candidate / 'java.exe').exists() else None)
        if not javac or not java:
            self.skipTest('A JDK is required')
        method = re.search(r'    private JSONObject motorTree\(JSONObject args\) throws Exception \{[\s\S]*?\n    \}', overlay.SERVICE_METHODS).group(0)
        regression = '''import java.util.HashMap;
public final class FreshTreeRegression {
    static class Build { static class VERSION { static int SDK_INT = 35; } }
    static class JSONObject {
        HashMap<String, Object> values = new HashMap<>();
        JSONObject put(String key, Object value) { values.put(key, value); return this; }
        boolean optBoolean(String key, boolean fallback) { Object value = values.get(key); return value instanceof Boolean ? (Boolean) value : fallback; }
    }
    int clears, reads;
    boolean clearCache() { clears++; return true; }
    JSONObject tree() { reads++; return new JSONObject(); }
''' + method + '''
    public static void main(String[] args) throws Exception {
        FreshTreeRegression s = new FreshTreeRegression();
        s.motorTree(new JSONObject());
        if (s.clears != 0 || s.reads != 1) throw new AssertionError("legacy default");
        JSONObject fresh = s.motorTree(new JSONObject().put("fresh", true));
        if (s.clears != 1 || s.reads != 2 || !fresh.optBoolean("cacheCleared", false)) throw new AssertionError("one clear per tree");
        s.motorTree(new JSONObject().put("_motorSemanticOnly", true));
        if (s.clears != 2) throw new AssertionError("Motor defaults fresh");
        Build.VERSION.SDK_INT = 32;
        JSONObject old = s.motorTree(new JSONObject().put("fresh", true));
        if (s.clears != 2 || old.optBoolean("freshSupported", true)) throw new AssertionError("old SDK guard");
        Build.VERSION.SDK_INT = 35;
        s.motorTree(new JSONObject().put("_motorSemanticOnly", true).put("fresh", false));
        if (s.clears != 2 || s.reads != 5) throw new AssertionError("explicit cache opt-out");
    }
}'''
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'FreshTreeRegression.java'
            source.write_text(regression, encoding='utf-8')
            subprocess.run([javac, '-encoding', 'UTF-8', '-d', temp, str(source)], check=True, capture_output=True, timeout=30)
            subprocess.run([java, '-cp', temp, 'FreshTreeRegression'], check=True, capture_output=True, timeout=10)

    def test_staging_uses_latest_collector_recorder_and_keeps_motor(self):
        if not overlay.DEFAULT_RECORDER.is_file() or not overlay.DEFAULT_SOURCE.is_dir():
            self.skipTest('Collector integration source unavailable')
        with tempfile.TemporaryDirectory() as temp:
            stage = overlay.prepare(overlay.DEFAULT_SOURCE, Path(temp) / 'native')
            java = stage / 'android' / 'app' / 'src' / 'main' / 'java' / 'io' / 'daxqo' / 'app'
            self.assertEqual((java / 'TrajectoryRecorder.java').read_bytes(), overlay.DEFAULT_RECORDER.read_bytes())
            self.assertTrue((java / 'MotorGate.java').is_file())
            self.assertIn('executeMotorMain(request', (java / 'DeviceAccessibilityService.java').read_text(encoding='utf-8'))
            manifest = json.loads((stage / 'overlay-manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['version_code'], 10)
            self.assertIn('TrajectoryRecorder.java', manifest['source_sha256'])

    def test_snapshot_semantic_changes_are_stale(self):
        candidate = Path(os.environ.get('JAVA_HOME', '/jdk-not-configured')) / 'bin'
        javac = shutil.which('javac') or (str(candidate / 'javac.exe') if (candidate / 'javac.exe').exists() else None)
        java = shutil.which('java') or (str(candidate / 'java.exe') if (candidate / 'java.exe').exists() else None)
        source = overlay.DEFAULT_SOURCE / 'android' / 'app' / 'src' / 'main' / 'java' / 'io' / 'daxqo' / 'app' / 'NodeSnapshot.java'
        if not javac or not java or not source.is_file():
            self.skipTest('A JDK and existing native source are required')
        regression = '''package io.daxqo.app;
import java.util.Arrays;
public final class SemanticSnapshotRegression {
    public static void main(String[] args) {
        NodeSnapshot.Node off = new NodeSnapshot.Node("0", "toggle", "", "id", "Switch", 0, 0, 100, 100, true, true, false, "off");
        NodeSnapshot.Node on = new NodeSnapshot.Node("0", "toggle", "", "id", "Switch", 0, 0, 100, 100, true, true, false, "on");
        NodeSnapshot before = new NodeSnapshot("s", "test.app", Arrays.asList(off));
        if (before.match("s", "0", new NodeSnapshot("", "test.app", Arrays.asList(off)), 100, 100) == null) throw new AssertionError("unchanged control");
        if (before.match("s", "0", new NodeSnapshot("", "test.app", Arrays.asList(on)), 100, 100) != null) throw new AssertionError("changed semantic state accepted");
    }
}'''
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / 'NodeSnapshot.java').write_text(overlay.overlay_snapshot(source.read_text(encoding='utf-8')), encoding='utf-8')
            (base / 'SemanticSnapshotRegression.java').write_text(regression, encoding='utf-8')
            subprocess.run([javac, '-encoding', 'UTF-8', '-d', temp, str(base / 'NodeSnapshot.java'), str(base / 'SemanticSnapshotRegression.java')],
                           check=True, capture_output=True, timeout=30)
            subprocess.run([java, '-cp', temp, 'io.daxqo.app.SemanticSnapshotRegression'], check=True, capture_output=True, timeout=10)

    def test_linux_bundle_is_reproducible_and_source_only(self):
        import tarfile
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            motor_root, collector_root = base / 'motor-project', base / 'collector-project'
            for root, name in ((motor_root, 'motor'), (collector_root, 'trajectory')):
                package = root / name
                package.mkdir(parents=True)
                (package / '__init__.py').write_text('"""package"""\n', encoding='utf-8')
                (package / 'private.sqlite3').write_bytes(b'PRIVATE')
                (root / 'data').mkdir()
                (root / 'data' / 'private.jsonl').write_text('PRIVATE', encoding='utf-8')
                (root / '.env').write_text('TOKEN=PRIVATE', encoding='utf-8')
            (motor_root / 'README.md').write_text('Readme\n', encoding='utf-8')
            (motor_root / 'scripts').mkdir()
            (motor_root / 'scripts' / 'install_linux.py').write_text('print("installer")\n', encoding='utf-8')
            one, two = base / 'one.tar.gz', base / 'two.tar.gz'
            bundle.create_bundle(one, collector_root=collector_root, root=motor_root)
            bundle.create_bundle(two, collector_root=collector_root, root=motor_root)
            self.assertEqual(one.read_bytes(), two.read_bytes())
            with tarfile.open(one) as archive:
                names = archive.getnames()
                self.assertEqual(len(names), 5)
                self.assertFalse(any('private' in name or '.env' in name for name in names))


if __name__ == '__main__':
    unittest.main()
