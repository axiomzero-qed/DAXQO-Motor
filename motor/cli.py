"""Harness-neutral CLI, with optional Android and Collector integrations."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import uuid

from .adapter import AndroidAdapter
from .app_map import MapStore
from .engine import Motor, action_from_dict
from .models import Budget
from .trajectory import CollectorSink, load_collector
from .transport import Adb, linux_transport, usb_transport


def parser():
    p = argparse.ArgumentParser(description="DAXQO Motor v0.1")
    p.add_argument("--serial", help="USB device running a compatible Android bridge")
    p.add_argument("--adb")
    p.add_argument("--android-package", help="Explicit compatible bridge APK package")
    p.add_argument("--collector-root", help="Explicit Collector source checkout (optional)")
    p.add_argument("--state-dir", help="Existing DAXQO bridge pairing directory")
    p.add_argument("--device-id", help="Stable UUID for bridges without an identity endpoint")
    p.add_argument("--adapter", help="Trusted local module:factory implementing DeviceAdapter")
    p.add_argument("--adapter-config", type=Path, help="JSON keyword arguments for the factory")
    p.add_argument("--data", default=str(Path.home() / ".local/share/daxqo-motor"))
    p.add_argument("--trajectory-data", help="Explicit Collector Dataset root; shared by all receivers")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("devices")
    sub.add_parser("doctor")
    scout = sub.add_parser("scout")
    scout.add_argument("package")
    scout.add_argument("--mode", choices=("quick", "deep"), default="quick")
    scout.add_argument("--goal", default="")
    scout.add_argument("--seconds", type=float, default=30)
    scout.add_argument("--max-scrolls", type=int, default=8)
    scout.add_argument("--max-depth", type=int, default=2)
    scout.add_argument("--max-pages", type=int, default=12)
    scout.add_argument("--launch", action="store_true")
    scout.add_argument("--collect", action="store_true")
    scout.add_argument("--page-only", action="store_true")
    scout.add_argument("--output")
    brief = sub.add_parser("map")
    brief.add_argument("package")
    brief.add_argument("--query")
    nav = sub.add_parser("navigate")
    nav.add_argument("package")
    nav.add_argument("query")
    nav.add_argument("--open", action="store_true", dest="open_target")
    action = sub.add_parser("reflex")
    action.add_argument("package")
    action.add_argument("action_json", help="Semantic Action JSON")
    sub.add_parser("sync")
    export = sub.add_parser("export")
    export.add_argument("output")
    sub.add_parser("cancel")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "devices":
        print(json.dumps(Adb(args.adb).devices(), ensure_ascii=False))
        return 0
    root = Path(args.data)
    collecting = args.command == "scout" and args.collect
    uses_dataset = collecting or args.command in {"sync", "export"}
    if uses_dataset and not args.trajectory_data:
        raise ValueError("Collection, sync and export require an explicit --trajectory-data Dataset root")
    with ExitStack() as resources:
        dataset = None
        if uses_dataset:
            load_collector(args.collector_root)
            from trajectory.dataset import Dataset
            dataset = resources.enter_context(Dataset(Path(args.trajectory_data)))
        if args.command == "export":
            print(json.dumps(dataset.export(Path(args.output), split_by="task"), ensure_ascii=False))
            return 0
        store = resources.enter_context(MapStore(root / "app-map.sqlite3"))
        if args.command == "map":
            print(json.dumps(store.summary(args.package, query=args.query), ensure_ascii=False))
            return 0
        if sum(bool(v) for v in (args.adapter, args.serial, args.state_dir)) != 1:
            raise ValueError("Specify exactly one of --adapter, --serial, or --state-dir")
        client = None
        if args.adapter:
            from .plugins import load_adapter
            adapter = load_adapter(args.adapter, args.adapter_config)
            if callable(getattr(adapter, "close", None)):
                resources.callback(adapter.close)
            if collecting or args.command == "sync" or getattr(args, "launch", False):
                raise ValueError("--collect, sync and --launch require the Android bridge adapter")
        else:
            client = resources.enter_context(usb_transport(args.serial, args.adb, package=args.android_package)
                                            if args.serial else linux_transport(args.state_dir, device_id=args.device_id))
            adapter = AndroidAdapter(client)
        store.bind_local_device(adapter.device_id)
        if args.command == "doctor":
            result = adapter.capabilities()
        elif args.command == "sync":
            result = CollectorSink(client, {}, dataset, args.collector_root).sync()
        else:
            task = {"id": str(uuid.uuid4()), "user_task": getattr(args, "goal", "") or args.command,
                    "subgoal": "", "stage": args.command, "source": "cli"}
            sink = CollectorSink(client, task, dataset, args.collector_root) if collecting else None
            if collecting:
                previous = client.request("/api/trajectory/status")
                if previous.get("enabled"):
                    raise RuntimeError("Collector already active; leave its current session undisturbed")
                sink.enable([args.package])
                if sink.errors:
                    raise RuntimeError("Collector could not be enabled")
            motor = Motor(adapter, store, sink)
            try:
                if args.command == "scout":
                    if args.launch:
                        launch = adapter.perform("open_app", {"packageName": args.package}, str(uuid.uuid4()))
                        adapter.check(launch)
                        adapter.events(0, 1000)
                    result = motor.scout(args.package, args.mode, args.goal,
                        Budget(args.seconds, args.max_scrolls, args.max_pages, args.max_depth), follow_tabs=not args.page_only)
                elif args.command == "reflex":
                    result = motor.act(args.package, action_from_dict(json.loads(args.action_json)), source="cli")
                elif args.command == "navigate":
                    result = motor.navigate(args.package, args.query, open_target=args.open_target)
                else:
                    result = motor.cancel()
            finally:
                if collecting:
                    sink.disable()
                    sync = sink.sync()
                    sink.close()
            if collecting:
                result["transfer"] = sync
                result["collection"] = sink.report()
        if getattr(args, "output", None):
            out = Path(args.output)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
        return 0 if not isinstance(result, dict) or result.get("ok", True) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, ImportError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
