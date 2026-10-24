"""Validate isolated installation and both existing Harness config generators."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('motor_linux_installer', ROOT / 'scripts' / 'install_linux.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

HARNESS = '''from pathlib import Path
import json
import sys
class Harness:
    def _write_codex_config(self):
        content = '[model_providers.saved]\\nname = "Keep provider"\\n[mcp_servers.daxqo_android]\\ncommand = "existing"\\n'
        config = self.codex_home / 'config.toml'
        config.write_text(content, encoding="utf-8")
'''


class LinuxInstallTests(unittest.TestCase):
    def setup_source(self, base):
        source, harness, runtime = base / 'source', base / 'harness', base / 'motor-local' / 'runtime'
        for name in ('motor', 'trajectory'):
            package = source / name
            package.mkdir(parents=True)
            (package / '__init__.py').write_text('', encoding='utf-8')
            (package / 'private.sqlite3').write_bytes(b'PRIVATE_DATA')
        (source / 'motor' / 'mcp_server.py').write_text('def main():\n    return 0\n', encoding='utf-8')
        (source / 'motor' / 'task_guard.py').write_text('', encoding='utf-8')
        (source / 'README.md').write_text('Motor README', encoding='utf-8')
        (source / 'data').mkdir()
        (source / 'data' / 'private.jsonl').write_text('PRIVATE_DATA', encoding='utf-8')
        (source / '.env').write_text('MODEL_KEY=DO_NOT_COPY', encoding='utf-8')
        harness.mkdir()
        (harness / 'server.py').write_text(HARNESS, encoding='utf-8')
        (harness / 'state').mkdir()
        (harness / 'state' / 'provider.json').write_text('{"apiKey":"DO_NOT_READ"}', encoding='utf-8')
        return source, harness, runtime

    def config(self, server, runtime, harness):
        namespace = {}
        exec(compile(server, '<isolated harness>', 'exec'), namespace)
        instance = namespace['Harness']()
        instance.state = harness / 'state'
        instance.codex_home = harness / 'codex-test'
        instance.codex_home.mkdir(exist_ok=True)
        instance._write_codex_config()
        return (instance.codex_home / 'config.toml').read_text(encoding='utf-8')

    def test_install_integrate_idempotent_and_no_private_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, harness, runtime = self.setup_source(Path(temporary))
            before = (harness / 'server.py').read_bytes()
            report = installer.install(source, harness, runtime, integrate_harness=True)
            self.assertTrue(report['ok'] and report['needs_restart'])
            self.assertEqual(Path(report['server_backup']).read_bytes(), before)
            installed = [p.relative_to(runtime).as_posix() for p in runtime.rglob('*') if p.is_file()]
            self.assertTrue('run_motor_mcp.py' in installed and 'trajectory/__init__.py' in installed)
            self.assertFalse(any('private' in name or '.env' in name or 'provider' in name or 'data/' in name for name in installed))
            config = self.config((harness / 'server.py').read_text(encoding='utf-8'), runtime, harness)
            self.assertIn('[mcp_servers.daxqo_android]', config)
            self.assertIn('name = "Keep provider"', config)
            self.assertEqual(config.count('[mcp_servers.daxqo_motor]'), 1)
            self.assertIn('default_tools_approval_mode = "prompt"', config)
            self.assertIn('"motor_navigate"', config)
            self.assertIn('"motor_cancel"', config)
            self.assertIn('--harness-task-guard', config)
            self.assertNotIn('DO_NOT_READ', config)
            again = installer.install(source, harness, runtime, integrate_harness=True)
            self.assertFalse(again['runtime_changed'] or again['needs_restart'])
            self.assertEqual(again['integration'], 'already_integrated')
            self.assertEqual((harness / 'state' / 'provider.json').read_text(encoding='utf-8'), '{"apiKey":"DO_NOT_READ"}')
            (runtime / 'motor' / 'mcp_server.py').unlink()
            without_module = self.config((harness / 'server.py').read_text(encoding='utf-8'), runtime, harness)
            self.assertNotIn('[mcp_servers.daxqo_motor]', without_module)

    def test_known_old_integration_upgrades_with_server_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, harness, runtime = self.setup_source(Path(temporary))
            old_block = installer.previous_integration_block(runtime)
            server = harness / 'server.py'
            old_server = HARNESS.replace(installer.ANCHOR, old_block + installer.ANCHOR)
            server.write_text(old_server, encoding='utf-8')
            report = installer.install(source, harness, runtime, integrate_harness=True)
            self.assertTrue(report['needs_restart'])
            self.assertEqual(report['integration'], 'upgraded')
            self.assertEqual(Path(report['server_backup']).read_text(encoding='utf-8'), old_server)
            updated = server.read_text(encoding='utf-8')
            generated_config = self.config(updated, runtime, harness)
            self.assertEqual(generated_config.count('[mcp_servers.daxqo_motor]'), 1)
            self.assertIn('"motor_navigate"', generated_config)
            self.assertIn('--harness-task-guard', generated_config)
            self.assertNotIn('enabled_tools = ["motor_map", "motor_scout", "motor_reflex", "motor_cancel"]', updated)
            again, state = installer.patch_server(updated, runtime)
            self.assertEqual(state, 'already_integrated')
            self.assertEqual(again, updated)

            modified_old = old_server.replace('tool_timeout_sec = 180', 'tool_timeout_sec = 181')
            with self.assertRaises(installer.InstallError):
                installer.patch_server(modified_old, runtime)

    def test_known_five_tool_block_upgrades_but_user_changes_are_preserved(self):
        runtime = Path('/home/user/.local/share/daxqo-motor/runtime')
        old = HARNESS.replace(installer.ANCHOR, installer.unguarded_integration_block(runtime) + installer.ANCHOR)
        updated, state = installer.patch_server(old, runtime)
        self.assertEqual(state, 'upgraded')
        self.assertEqual(updated, HARNESS.replace(installer.ANCHOR, installer.integration_block(runtime) + installer.ANCHOR))
        with self.assertRaises(installer.InstallError):
            installer.patch_server(old.replace('tool_timeout_sec = 180', 'tool_timeout_sec = 181'), runtime)

    def test_server_activation_failure_rolls_runtime_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, harness, runtime = self.setup_source(Path(temporary))
            installer.install(source, harness, runtime)
            original = (runtime / 'motor' / 'mcp_server.py').read_bytes()
            (source / 'motor' / 'mcp_server.py').write_text('def main():\n    return 9\n', encoding='utf-8')
            replace = installer.os.replace
            def fail_server(source_path, destination):
                if Path(destination) == harness / 'server.py':
                    raise OSError('controlled replacement failure')
                return replace(source_path, destination)
            with mock.patch.object(installer.os, 'replace', side_effect=fail_server):
                with self.assertRaises(OSError):
                    installer.install(source, harness, runtime, integrate_harness=True)
            self.assertEqual((runtime / 'motor' / 'mcp_server.py').read_bytes(), original)
            self.assertEqual((harness / 'server.py').read_text(encoding='utf-8'), HARNESS)

    def test_standalone_source_does_not_read_sibling_collector_or_provider(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, harness, runtime = self.setup_source(base)
            sibling = base / 'DAXQO Trajectory Collector'
            sibling.mkdir()
            collector = source / 'trajectory'
            target = sibling / 'trajectory'
            self.assertTrue(collector.resolve().is_relative_to(base.resolve()) and target.resolve().is_relative_to(base.resolve()))
            collector.rename(target)
            original_open = Path.open
            def guarded_open(path, *args, **kwargs):
                if path.name == 'provider.json' or sibling in path.parents:
                    raise AssertionError('Installer accessed an unrelated source or credentials')
                return original_open(path, *args, **kwargs)
            with mock.patch.object(Path, 'open', guarded_open):
                report = installer.install(source, harness, runtime, integrate_harness=True)
            self.assertTrue(report['ok'])
            self.assertNotIn('DO_NOT_READ', json.dumps(report))
            self.assertFalse((runtime / 'trajectory').exists())

    def test_old_runtime_retained_as_backup_on_source_update(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, harness, runtime = self.setup_source(Path(temporary))
            installer.install(source, harness, runtime)
            (source / 'motor' / 'mcp_server.py').write_text('def main():\n    return 1\n', encoding='utf-8')
            updated = installer.install(source, harness, runtime)
            backup = Path(updated['runtime_backup'])
            self.assertIn('return 0', (backup / 'motor' / 'mcp_server.py').read_text(encoding='utf-8'))
            self.assertIn('return 1', (runtime / 'motor' / 'mcp_server.py').read_text(encoding='utf-8'))

    def test_invalid_python_or_anchor_never_activates(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, harness, runtime = self.setup_source(Path(temporary))
            server = harness / 'server.py'
            server.write_text(HARNESS.replace('config.write_text(content, encoding="utf-8")', 'config.write_text(content)'), encoding='utf-8')
            before = server.read_bytes()
            with self.assertRaises(installer.InstallError):
                installer.install(source, harness, runtime, integrate_harness=True)
            self.assertFalse(runtime.exists())
            self.assertEqual(server.read_bytes(), before)
            server.write_text(HARNESS, encoding='utf-8')
            (source / 'motor' / 'bad.py').write_text('def broken(:\n', encoding='utf-8')
            with self.assertRaises(installer.InstallError):
                installer.install(source, harness, runtime, integrate_harness=True)
            self.assertFalse(runtime.exists())
            self.assertEqual(server.read_text(encoding='utf-8'), HARNESS)

    def test_unknown_existing_runtime_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, harness, runtime = self.setup_source(Path(temporary))
            runtime.mkdir(parents=True)
            (runtime / 'user.txt').write_text('Keep', encoding='utf-8')
            with self.assertRaises(installer.InstallError):
                installer.install(source, harness, runtime, integrate_harness=True)
            self.assertEqual((runtime / 'user.txt').read_text(encoding='utf-8'), 'Keep')
            self.assertEqual((harness / 'server.py').read_text(encoding='utf-8'), HARNESS)

    def test_v04_v05_config_writer_anchors(self):
        candidates = [Path(value) / 'backend/server.py' for value in
                      [os.environ.get('DAXQO_ANDROID_SOURCE')] if value]
        if not candidates or not all(path.is_file() for path in candidates):
            self.skipTest('Existing Harness source copies are unavailable')
        for source in candidates:
            with self.subTest(version=source.parent.name):
                original = source.read_text(encoding='utf-8')
                patched, state = installer.patch_server(original, Path('/home/user/.local/share/daxqo-motor/runtime'))
                self.assertEqual(state, 'integrated')
                compile(patched, '<patched harness>', 'exec')
                self.assertEqual(installer.patch_server(patched, Path('/home/user/.local/share/daxqo-motor/runtime'))[0], patched)
                self.assertEqual(original, patched.replace(installer.integration_block(Path('/home/user/.local/share/daxqo-motor/runtime')), ''))

    def test_real_source_launcher_has_working_help(self):
        if not (ROOT / 'motor' / 'mcp_server.py').is_file():
            self.skipTest('Complete development packages are unavailable')
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            harness, runtime = base / 'harness', base / 'runtime'
            harness.mkdir()
            report = installer.install(ROOT, harness, runtime)
            self.assertFalse(report['needs_restart'])
            result = subprocess.run([sys.executable, str(runtime / 'run_motor_mcp.py'), '--help'],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('--state-dir', result.stdout)
            self.assertIn('--collector-root', result.stdout)


if __name__ == '__main__':
    unittest.main()
