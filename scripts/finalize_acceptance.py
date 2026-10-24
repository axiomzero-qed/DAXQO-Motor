"""Refresh the final report from idle devices and the shared Collector dataset."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from motor.trajectory import load_collector
from motor.transport import usb_transport


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', action='append', required=True)
    parser.add_argument('--trajectory-data', type=Path, required=True)
    args = parser.parse_args()
    load_collector()
    from trajectory.collector import sync_device
    from trajectory.dataset import Dataset
    from trajectory.transport import Adb, DeviceClient
    data_root = args.trajectory_data
    reports, final = [], []
    adb = Adb()
    with Dataset(data_root) as dataset:
        for serial in args.serial:
            report = json.loads((ROOT / 'dist' / ('acceptance-' + serial + '.json')).read_text(encoding='utf-8'))
            with DeviceClient(adb, serial) as client, usb_transport(serial) as native:
                harness = client.service_request('agent', '/api/status')
                collector = client.request('/api/trajectory/status')
                motor = native.request('/api/motor/status')
                if (harness.get('activeTaskId') or harness.get('activeTaskIds')
                        or collector.get('enabled') or motor.get('lease_active')):
                    raise RuntimeError('Device is not idle: ' + serial)
                transfer = sync_device(client, dataset)
                collector = client.request('/api/trajectory/status')
                package = 'io.daxqo.app' if report['platform'] == 'standard' else 'io.daxqo.xm'
                versions = {}
                for app in (package, 'io.daxqo.motorfixture'):
                    info = adb.run(serial, 'shell', 'dumpsys', 'package', app)
                    match = re.search(r'\bversionCode=(\d+)', info)
                    versions[app] = int(match.group(1)) if match else None
                final.append({'serial': serial, 'collector_enabled': collector.get('enabled'),
                    'pending': collector.get('pending'), 'queue_size': collector.get('queue_size'),
                    'active_task': harness.get('activeTaskId'),
                    'active_tasks': harness.get('activeTaskIds', []),
                    'engine_connected': harness.get('engine', {}).get('connected'),
                    'lease_active': motor.get('lease_active'), 'version_codes': versions,
                    'sync': transfer})
                failed = [row for row in dataset.records(task_id=report['task_id'])
                          if row.get('record_type') == 'action'
                          and row.get('action_id') == report['failure_action_id']]
                report['failed_record_preserved'] = len(failed) == 1 and failed[0]['result']['status'] == 'failed'
                reports.append(report)
        old = json.loads((ROOT / 'dist/acceptance.json').read_text(encoding='utf-8'))
        result = {'schema_version': 'daxqo.motor.acceptance/0.1',
            'updated_at': datetime.now(timezone.utc).isoformat(), 'data_root': str(data_root),
            'data_scope': 'Shared Collector and Motor dataset; per-device task IDs identify this acceptance only.',
            'devices': reports, 'final_device_status': final, 'map': old.get('map'),
            'dataset': dataset.stats(), 'export': dataset.export(ROOT / 'exports/acceptance', split_by='task'),
            'deployment': json.loads((ROOT / 'dist/deployment.json').read_text(encoding='utf-8')),
            'harness_stop': json.loads((ROOT / 'dist/cancel-acceptance.json').read_text(encoding='utf-8'))}
        (ROOT / 'dist/acceptance.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        (ROOT / 'dist/final-device-status.json').write_text(json.dumps(final, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'devices': final, 'dataset': result['dataset'], 'export': result['export'],
                          'failed_records_preserved': all(r['failed_record_preserved'] for r in reports)}, ensure_ascii=True))
    good = all(not row['collector_enabled'] and row['pending'] == 0 and row['queue_size'] == 0
               and row['engine_connected'] is True and not row['lease_active'] for row in final)
    good = good and all(r['passed'] and r['failed_record_preserved'] for r in reports)
    return 0 if good else 1


if __name__ == '__main__':
    raise SystemExit(main())
