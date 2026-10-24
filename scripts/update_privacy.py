"""Update the bundled privacy policy from an explicitly selected Collector source."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def update(collector_root: Path):
    source = collector_root / 'trajectory/privacy.py'
    payload = source.read_bytes()
    compile(payload, '<collector privacy policy>', 'exec')
    target = ROOT / 'motor/_vendor'
    target.mkdir(parents=True, exist_ok=True)
    (target / 'collector_privacy.py').write_bytes(payload)
    (target / 'PROVENANCE.json').write_text(json.dumps({
        'source_repository': 'https://github.com/axiomzero-qed/DAXQO-Trajectory-Collector',
        'source_version': '0.1.0', 'source_file': 'trajectory/privacy.py',
        'sha256': hashlib.sha256(payload).hexdigest(), 'license': 'MIT',
        'copyright': '2026 DAXQO contributors',
    }, indent=2) + '\n', encoding='utf-8', newline='\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collector-root', type=Path, required=True)
    update(parser.parse_args().collector_root)
