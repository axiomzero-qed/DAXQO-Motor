"""Create a reproducible, source-only Linux bundle without data or credentials."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]
PREFIX = 'daxqo-motor-v0.1'


def source_files(package: Path, label: str) -> dict[str, bytes]:
    if not (package / '__init__.py').is_file():
        raise FileNotFoundError(label + ' Python package is missing')
    result = {}
    for path in sorted(package.rglob('*.py')):
        relative = path.relative_to(package)
        if any(part.startswith('.') or part == '__pycache__' for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError('Package symlinks are not allowed')
        result[label + '/' + relative.as_posix()] = path.read_bytes()
    return result


def create_bundle(output: Path, *, collector_root: Path | None = None, root: Path = ROOT, readme: Path | None = None) -> dict:
    files = source_files(root / 'motor', 'motor')
    if collector_root is not None:
        files.update(source_files(collector_root / 'trajectory', 'trajectory'))
    if (root / 'LICENSE').is_file():
        files['LICENSE'] = (root / 'LICENSE').read_bytes()
    installer = root / 'scripts' / 'install_linux.py'
    if not installer.is_file() or installer.is_symlink():
        raise FileNotFoundError('Motor Linux installer source is missing or unsafe')
    files['scripts/install_linux.py'] = installer.read_bytes()
    # A small, source-only Linux signal regression also runs on the actual phone Python.
    for relative in ('tests/__init__.py', 'tests/fakes.py', 'tests/test_mcp_cancellation.py'):
        candidate = root / relative
        if candidate.is_file() and not candidate.is_symlink():
            files[relative] = candidate.read_bytes()
    readme = readme or root / 'README.md'
    if not readme.is_file():
        readme = ROOT / 'native' / 'README-linux.md'
    files['README.md'] = readme.read_bytes()
    manifest = {'schema_version': 'daxqo.motor.bundle/0.1',
                'files': {name: {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
                          for name, data in sorted(files.items())}}
    files['MANIFEST.json'] = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()
    output = output.resolve()
    # Guard against accidentally replacing one of the source files.
    if output == readme.resolve() or output.suffixes[-2:] != ['.tar', '.gz']:
        raise ValueError('Output must be a distinct .tar.gz file')
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('wb') as stream:
        with gzip.GzipFile(filename='', mode='wb', fileobj=stream, mtime=0) as compressed:
            with tarfile.open(mode='w', fileobj=compressed, format=tarfile.GNU_FORMAT) as archive:
                for name, data in sorted(files.items()):
                    info = tarfile.TarInfo(PREFIX + '/' + name)
                    info.size = len(data)
                    info.mode = 0o644
                    info.mtime = 0
                    info.uid = info.gid = 0
                    info.uname = info.gname = ''
                    archive.addfile(info, io.BytesIO(data))
    with tarfile.open(output, mode='r:gz') as archive:
        for member in archive.getmembers():
            name = member.name.removeprefix(PREFIX + '/')
            if not member.isfile() or name not in files:
                raise ValueError('Unexpected bundle member')
            if hashlib.sha256(archive.extractfile(member).read()).digest() != hashlib.sha256(files[name]).digest():
                raise ValueError('Bundle verification failed')
    return {'output': str(output), 'files': len(files), 'bytes': output.stat().st_size,
            'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collector-root', type=Path, help='Explicit opt-in: also bundle Collector Python sources')
    parser.add_argument('--output', type=Path, default=ROOT / 'dist' / 'daxqo-motor-v0.1.tar.gz')
    parser.add_argument('--readme', type=Path)
    args = parser.parse_args()
    print(json.dumps(create_bundle(args.output, collector_root=args.collector_root, readme=args.readme), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
