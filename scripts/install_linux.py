#!/usr/bin/env python3
"""Install Motor in existing phone Linux; optionally append its Harness MCP entry."""
from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import json
import os
from pathlib import Path
import py_compile
import shutil
import stat
import sys
import tempfile
import uuid

SCHEMA = 'daxqo.motor.install/0.1'
BEGIN = '# DAXQO_MOTOR_INTEGRATION_BEGIN v0.1'
END = '# DAXQO_MOTOR_INTEGRATION_END v0.1'
ANCHOR = '        config.write_text(content, encoding="utf-8")\n'
DEFAULT_RUNTIME = Path.home() / '.local' / 'share' / 'daxqo-motor' / 'runtime'
LAUNCHER = '''#!/usr/bin/env python3
"""Installed local Motor MCP launcher; credentials stay in Harness state."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from motor.mcp_server import main
if __name__ == "__main__":
    raise SystemExit(main())
'''


class InstallError(RuntimeError):
    pass


def absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def check_path(path: Path, *, exists=False):
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            item = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(item.st_mode):
            raise InstallError('Symlinks are not allowed in installation paths')
    if exists and not path.exists():
        raise InstallError('A required source or Harness path is missing')


def regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def owned(path: Path):
    item = path.lstat()
    if hasattr(os, 'getuid') and item.st_uid != os.getuid():
        raise InstallError('Installation targets must belong to the current Linux user')


def sources(source: Path) -> dict[str, bytes]:
    collector = source / 'trajectory'
    result: dict[str, bytes] = {}
    packages = [(source / 'motor', 'motor')]
    if collector.is_dir():
        packages.append((collector, 'trajectory'))
    for package, label in packages:
        check_path(package, exists=True)
        if not regular(package / '__init__.py'):
            raise InstallError('A selected Python source package is missing or unsafe')
        for path in sorted(package.rglob('*.py')):
            relative = path.relative_to(package)
            if any(part.startswith('.') or part == '__pycache__' for part in relative.parts):
                continue
            check_path(path, exists=True)
            if not regular(path) or path.stat().st_size > 4 * 1024 * 1024:
                raise InstallError('A Python source file is unsafe or too large')
            result[label + '/' + relative.as_posix()] = path.read_bytes()
    if len(result) > 512 or sum(map(len, result.values())) > 32 * 1024 * 1024:
        raise InstallError('Motor source package exceeds the installation limit')
    for name in ('README.md', 'LICENSE'):
        document = source / name
        if regular(document) and document.stat().st_size <= 1024 * 1024:
            result[name] = document.read_bytes()
    result['run_motor_mcp.py'] = LAUNCHER.encode('utf-8')
    return result


def integration_block(runtime: Path) -> str:
    # These keys follow the existing Harness and the official configuration reference:
    # https://learn.chatgpt.com/docs/config-file/config-reference
    return f'''        {BEGIN}
        _motor_runtime = Path({str(runtime)!r})
        if ((_motor_runtime / "run_motor_mcp.py").is_file()
                and (_motor_runtime / "motor" / "mcp_server.py").is_file()
                and (_motor_runtime / "motor" / "task_guard.py").is_file()
                and "[mcp_servers.daxqo_motor]" not in content):
            content += (
                "\\n[mcp_servers.daxqo_motor]\\n"
                f"command = {{json.dumps(sys.executable)}}\\n"
                f"args = {{json.dumps([str(_motor_runtime / 'run_motor_mcp.py'), '--state-dir', str(self.state), '--map-db', str(self.state / 'motor' / 'app-map.sqlite3'), '--trajectory-feedback', '--harness-task-guard'])}}\\n"
                'enabled_tools = ["motor_map", "motor_scout", "motor_navigate", "motor_reflex", "motor_cancel"]\\n'
                'default_tools_approval_mode = "prompt"\\n'
                'startup_timeout_sec = 10\\n'
                'tool_timeout_sec = 180\\n\\n'
                '[mcp_servers.daxqo_motor.tools.motor_map]\\n'
                'approval_mode = "approve"\\n\\n'
                '[mcp_servers.daxqo_motor.tools.motor_scout]\\n'
                'approval_mode = "approve"\\n\\n'
                '[mcp_servers.daxqo_motor.tools.motor_navigate]\\n'
                'approval_mode = "approve"\\n\\n'
                '[mcp_servers.daxqo_motor.tools.motor_reflex]\\n'
                'approval_mode = "approve"\\n\\n'
                '[mcp_servers.daxqo_motor.tools.motor_cancel]\\n'
                'approval_mode = "approve"\\n\\n')
        {END}
'''


def unguarded_integration_block(runtime: Path) -> str:
    """Exact known five-tool block before the fixed Harness task guard."""
    return integration_block(runtime).replace(
        '                and (_motor_runtime / "motor" / "task_guard.py").is_file()\n', '', 1).replace(
        ", '--harness-task-guard'", '', 1)


def legacy_collector_integration_block(runtime: Path) -> str:
    """Exact guarded prototype block before Collector became optional."""
    return integration_block(runtime).replace(
        '                and (_motor_runtime / "motor" / "task_guard.py").is_file()\n',
        '                and (_motor_runtime / "motor" / "task_guard.py").is_file()\n'
        '                and (_motor_runtime / "trajectory" / "__init__.py").is_file()\n', 1).replace(
        ", '--trajectory-feedback', '--harness-task-guard'",
        ", '--collector-root', str(_motor_runtime), '--harness-task-guard'", 1)


def previous_integration_block(runtime: Path) -> str:
    """Exact v0.1 four-tool block installed before navigate was added."""
    current = unguarded_integration_block(runtime)
    current = current.replace(
        'enabled_tools = ["motor_map", "motor_scout", "motor_navigate", "motor_reflex", "motor_cancel"]',
        'enabled_tools = ["motor_map", "motor_scout", "motor_reflex", "motor_cancel"]', 1)
    start_marker = "                '[mcp_servers.daxqo_motor.tools.motor_navigate]"
    end_marker = "                '[mcp_servers.daxqo_motor.tools.motor_reflex]"
    if current.count(start_marker) != 1 or current.count(end_marker) != 1:
        raise InstallError('Could not construct the known previous Motor integration')
    start, end = current.index(start_marker), current.index(end_marker)
    return current[:start] + current[end:]


def patch_server(text: str, runtime: Path) -> tuple[str, str]:
    newline = '\r\n' if '\r\n' in text else '\n'
    if newline == '\r\n' and '\n' in text.replace('\r\n', ''):
        raise InstallError('Mixed Harness source line endings are unsupported')
    text = text.replace('\r\n', '\n')
    def finish(value: str, state: str):
        return value.replace('\n', newline), state
    if text.count(ANCHOR) != 1:
        raise InstallError('Harness config anchor changed; server was left unchanged')
    try:
        tree = ast.parse(text)
    except SyntaxError:
        raise InstallError('Existing Harness Python source is invalid') from None
    functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == '_write_codex_config']
    anchor_line = text[:text.index(ANCHOR)].count('\n') + 1
    if len(functions) != 1 or not functions[0].lineno < anchor_line <= functions[0].end_lineno:
        raise InstallError('Config anchor is outside the unique Harness config writer')
    block = integration_block(runtime)
    if BEGIN in text or END in text:
        if text.count(BEGIN) != 1 or text.count(END) != 1:
            raise InstallError('Existing Motor integration differs; server was left unchanged')
        if text.count(block) == 1:
            return finish(text, 'already_integrated')
        for old_block in (unguarded_integration_block(runtime), previous_integration_block(runtime),
                          legacy_collector_integration_block(runtime)):
            if text.count(old_block) == 1:
                return finish(text.replace(old_block, block, 1), 'upgraded')
        raise InstallError('Existing Motor integration differs; server was left unchanged')
    if '[mcp_servers.daxqo_motor]' in text:
        return finish(text, 'existing_motor_config_preserved')
    return finish(text.replace(ANCHOR, block + ANCHOR, 1), 'integrated')


def compile_sources(paths: list[Path], cache: Path):
    cache.mkdir()
    try:
        for index, path in enumerate(paths):
            try:
                py_compile.compile(str(path), cfile=str(cache / f'{index}.pyc'), doraise=True)
            except (py_compile.PyCompileError, OSError):
                # Compiler errors may include source lines; never echo them.
                raise InstallError('Python source validation failed; no installation was activated') from None
    finally:
        safe_remove(cache, cache.parent)


def safe_remove(path: Path, parent: Path):
    # Every recursive cleanup is of a known temporary child, never a user tree.
    if path.parent.resolve() != parent.resolve() or path.is_symlink():
        raise InstallError('Unsafe temporary cleanup path')
    if path.exists():
        shutil.rmtree(path)


@contextlib.contextmanager
def install_lock(path: Path):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise InstallError('Another Motor installation holds the installation lock') from None
    try:
        with os.fdopen(fd, 'w', encoding='ascii') as output:
            output.write(str(os.getpid()))
        yield
    finally:
        path.unlink(missing_ok=True)


def managed_runtime(runtime: Path):
    check_path(runtime, exists=True)
    owned(runtime)
    marker = runtime / 'INSTALL-MANIFEST.json'
    if not runtime.is_dir() or not regular(marker):
        raise InstallError('Existing runtime is not a managed Motor installation; left unchanged')
    try:
        value = json.loads(marker.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise InstallError('Existing Motor installation marker is invalid') from None
    if not isinstance(value, dict) or value.get('schema_version') != SCHEMA:
        raise InstallError('Existing runtime is not a managed Motor installation; left unchanged')
    return value


def matches_runtime(runtime: Path, manifest: dict) -> bool:
    if managed_runtime(runtime) != manifest:
        return False
    actual = {p.relative_to(runtime).as_posix() for p in runtime.rglob('*')
              if (p.is_file() or p.is_symlink()) and '__pycache__' not in p.parts}
    expected = set(manifest['files']) | {'INSTALL-MANIFEST.json'}
    if actual != expected:
        return False
    return all(regular(runtime / name) and hashlib.sha256((runtime / name).read_bytes()).hexdigest() == digest
               for name, digest in manifest['files'].items())


def install(source: Path, harness_root: Path | None = None, runtime_root: Path = DEFAULT_RUNTIME, *, integrate_harness=False) -> dict:
    source, runtime = map(absolute, (source, runtime_root))
    if integrate_harness and harness_root is None:
        raise InstallError('--harness-root is required with --integrate-harness')
    harness = absolute(harness_root) if harness_root is not None else None
    for path in ((source, harness) if integrate_harness else (source,)):
        check_path(path, exists=True)
        if not path.is_dir():
            raise InstallError('Source and Harness roots must be directories')
    check_path(runtime)
    if runtime == Path(runtime.anchor) or runtime == harness or runtime == source or source in runtime.parents or runtime in source.parents:
        raise InstallError('Source, Harness and Motor runtime targets must be separate')
    files = sources(source)
    if 'motor/mcp_server.py' not in files:
        raise InstallError('Motor MCP module is missing from the source package')
    manifest = {'schema_version': SCHEMA, 'files': {name: hashlib.sha256(value).hexdigest() for name, value in sorted(files.items())}}
    files['INSTALL-MANIFEST.json'] = (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode('utf-8')
    runtime.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    owned(runtime.parent)
    server = harness / 'server.py' if harness is not None else None
    if integrate_harness:
        check_path(server, exists=True)
        if not regular(server):
            raise InstallError('Installed Harness server.py is missing or unsafe')
        owned(server)
    with contextlib.ExitStack() as locks:
        locks.enter_context(install_lock(runtime.parent / ('.' + runtime.name + '.install.lock')))
        if integrate_harness:
            locks.enter_context(install_lock(harness / '.motor-harness-install.lock'))
        stage = Path(tempfile.mkdtemp(prefix='.motor-stage-', dir=runtime.parent))
        server_temp = None
        backup = server_backup = None
        runtime_changed = False
        integration_state = 'not_requested'
        original = patched = None
        try:
            for name, data in files.items():
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            py_files = sorted(stage.rglob('*.py'))
            if integrate_harness:
                original = server.read_bytes()
                try:
                    text = original.decode('utf-8')
                except UnicodeError:
                    raise InstallError('Harness source encoding is unsupported') from None
                new_text, integration_state = patch_server(text, runtime)
                patched = new_text.encode('utf-8')
                fd, temporary = tempfile.mkstemp(prefix='.motor-server-', suffix='.py', dir=harness)
                server_temp = Path(temporary)
                with os.fdopen(fd, 'wb') as output:
                    output.write(patched)
                    output.flush()
                    os.fsync(output.fileno())
                server_temp.chmod(stat.S_IMODE(server.stat().st_mode))
                py_files.append(server_temp)
            compile_sources(py_files, stage / '.compile-cache')
            runtime_changed = not runtime.exists() or not matches_runtime(runtime, manifest)
            if integrate_harness and patched != original:
                if server.read_bytes() != original:
                    raise InstallError('Harness source changed during validation; left unchanged')
                digest = hashlib.sha256(original).hexdigest()
                server_backup = server.with_name('server.py.motor-backup-' + digest[:16] + '.py')
                if server_backup.exists():
                    if not regular(server_backup) or server_backup.read_bytes() != original:
                        raise InstallError('Harness backup conflicts with existing content')
                else:
                    with server_backup.open('xb') as output:
                        output.write(original)
                    server_backup.chmod(0o600)
            if runtime_changed:
                if runtime.exists():
                    managed_runtime(runtime)
                    backup = runtime.with_name(runtime.name + '.backup-' + uuid.uuid4().hex[:12])
                    if backup.parent.resolve() != runtime.parent.resolve():
                        raise InstallError('Unsafe Motor backup target')
                    os.replace(runtime, backup)
                try:
                    os.replace(stage, runtime)
                except Exception:
                    if backup is not None:
                        os.replace(backup, runtime)
                    raise
            if integrate_harness and patched != original:
                try:
                    if server.read_bytes() != original:
                        raise InstallError('Harness source changed during activation; left unchanged')
                    os.replace(server_temp, server)
                except Exception:
                    if runtime_changed:
                        failed = runtime.with_name('.motor-failed-' + uuid.uuid4().hex)
                        os.replace(runtime, failed)
                        if backup is not None:
                            os.replace(backup, runtime)
                        safe_remove(failed, runtime.parent)
                    raise
            return {'ok': True, 'runtime_root': str(runtime), 'launcher': str(runtime / 'run_motor_mcp.py'),
                    'runtime_changed': runtime_changed, 'runtime_backup': str(backup) if backup else None,
                    'integration': integration_state, 'server_backup': str(server_backup) if server_backup else None,
                    'server_sha256_before': hashlib.sha256(original).hexdigest() if original is not None else None,
                    'server_sha256_after': hashlib.sha256(patched).hexdigest() if patched is not None else None,
                    'needs_restart': bool(integrate_harness and (runtime_changed or patched != original)),
                    'copied_source_files': sum(name.endswith('.py') for name in files)}
        finally:
            if server_temp is not None:
                server_temp.unlink(missing_ok=True)
            if stage.exists():
                safe_remove(stage, runtime.parent)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--harness-root', type=Path, help='Only required for the optional DAXQO integration')
    parser.add_argument('--runtime-root', type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument('--integrate-harness', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = install(args.source, args.harness_root, args.runtime_root, integrate_harness=args.integrate_harness)
    except InstallError as exc:
        print(json.dumps({'ok': False, 'error': str(exc), 'needs_restart': False}), file=sys.stderr)
        return 1
    except OSError:
        # Never print exception values or compiler output: they can echo source/config values.
        print(json.dumps({'ok': False, 'error': 'Motor installation failed; existing runtime and Harness were preserved'}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
