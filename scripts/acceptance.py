"""Real-device Motor acceptance, confined to the zero-permission Motor fixture."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import re
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from motor.adapter import AndroidAdapter
from motor.app_map import MapStore
from motor.arbiter import ActionArbiter, TaskAuthorization, authorization_key
from motor.engine import Motor
from motor.models import Action, Budget, Selector
from motor.reflex import ReflexEngine
from motor.trajectory import CollectorSink, load_collector
from motor.transport import usb_transport

APP = "io.daxqo.motorfixture"


def require(value, message):
    if not value:
        raise AssertionError(message)


def run_device(adb, serial, dataset, store, output):
    load_collector()
    report = {"serial": serial, "passed": False, "checks": [], "data_origin": "fixture"}
    task = {"id": str(uuid.uuid4()), "user_task": "Motor四标签、长列表、恢复、地图复用和安全仲裁验收",
            "subgoal": "", "stage": "acceptance", "source": "fixture"}
    report["task_id"] = task["id"]
    start = time.monotonic()
    with usb_transport(serial) as client:
        adapter = AndroidAdapter(client)
        sink = CollectorSink(client, task, dataset)
        motor = Motor(adapter, store, sink)

        def wake():
            d = client.request("/api/device/status")
            require(d.get("controlEnabled") and d.get("accessibilityConnected"), "Owner must enable phone control and accessibility")
            if not d.get("screenOn") or d.get("locked"):
                adapter.check(adapter.perform("wake", {}, str(uuid.uuid4())))

        def reset():
            wake()
            adb.run(serial, "shell", "am", "start", "-S", "-W", "-n", APP + "/.MainActivity")

        def act(action, grant=None, expect=None):
            result = motor.act(APP, action, source="reflex", grant=grant)
            if expect is not None:
                require(result.get("ok"), "Fixture action failed: " + result.get("reason", "unknown"))
                require(any(expect in label for label in result["current"]["labels"]), "Fixture oracle postcondition missing: " + expect)
                # Only this test oracle is an independent fixture assertion.
                client.request("/api/trajectory/feedback", {"action_id": result["action_id"], "task": task,
                    "verification": {"status": "verified", "source": "fixture_assertion"}})
            return result

        try:
            status = adapter.capabilities()
            require(status["motor"].get("capabilities", {}).get("action_lease"), "Install Motor Android bridge first")
            report["device"] = status["device"].get("device")
            report["platform"] = status["device"].get("platformId")
            collector = client.request("/api/trajectory/status")
            report["device_id"] = collector["device_id"]
            require(not collector.get("enabled"), "Existing Collector session is active; leave it undisturbed")
            sink.enable([APP])
            require(not sink.errors, "Cannot enable Collector")
            reset()
            quick = motor.scout(APP, budget=Budget(seconds=40, max_scrolls=2, max_pages=16, max_depth=0))
            report["quick"] = quick
            require(quick["metrics"]["visited_tabs"] >= 4, "Four main tabs were not traversed")
            require(quick["restoration"]["exact"], "Quick Scout did not restore its initial visible state")
            require(quick["metrics"]["cloud_calls"] == 0, "Scout unexpectedly used a cloud model")
            report["checks"].append("four_tabs_quick_and_restore")

            reset()
            deep = motor.scout(APP, "deep", "商品 19", Budget(seconds=65, max_scrolls=14, max_pages=20, max_depth=1))
            report["deep"] = deep
            products = {text for page in deep.get("pages", []) for text in page["content"]
                        if re.fullmatch(r"商品 \d{2}", text)}
            require(len(products) == 24, "Incremental traversal did not extract all 24 distinct products")
            require(any(page["stop_reason"] == "observed_boundary" for page in deep["pages"]), "Finite list boundary not observed")
            require(deep["restoration"]["exact"], "Deep Scout did not restore its initial visible state")
            report["checks"].extend(["incremental_list_dedup_and_boundary", "six_tabs_deep_and_restore"])

            reused = motor.navigate(APP, "商品 19", open_target=True)
            report["reuse"] = reused
            require(reused.get("ok") and reused.get("reused_map"), "Known map route was not reused")
            require(any("商品 19" in label for label in reused["current"]["labels"]), "Known product was not reached")
            report["checks"].append("persisted_map_route_reused")

            act(Action("CLICK", Selector(resource_id=APP + ":id/motor_tab_profile", text="我的")), expect="我的")
            denied = act(Action("CLICK", Selector(text="删除全部")))
            require(denied.get("status") == "denied", "Dangerous fixture control was not denied")
            require(any("次数：0" in label for label in denied["current"]["labels"]), "Dangerous control was executed")
            report["checks"].append("dangerous_action_denied_without_click")

            with ActionArbiter(adapter, APP, seconds=6) as arbiter:
                original = ReflexEngine(arbiter, store).observe(stable=True)
                original_id = store.observe(original)
            change = Action("CLICK", Selector(text="显示附加入口"), purpose="task", expected_text="查看附加说明")
            grant = TaskAuthorization(APP, frozenset({authorization_key(change)}), "Explicit fixture structural change")
            changed = act(change, grant, "查看附加说明")
            require(changed["current"]["page_fingerprint"] != original.page_fingerprint, "Changed controls did not change semantic page fingerprint")
            remembered = store.summary(APP, device_id=serial, query="查看附加说明")
            require(any(p["id"] != original_id for p in remembered["pages"]), "Changed device variant was not remembered")
            act(Action("CLICK", Selector(resource_id=APP + ":id/motor_tab_home", text="首页")), expect="首页")
            act(Action("CLICK", Selector(resource_id=APP + ":id/motor_tab_profile", text="我的")), expect="查看附加说明")
            with ActionArbiter(adapter, APP, seconds=6) as arbiter:
                current = ReflexEngine(arbiter, store).observe(stable=True)
                summary = store.summary(APP, device_id=serial)
                require(all(page["id"] != original_id for page in summary["pages"]), "Old page variant was not invalidated locally")
            report["checks"].append("changed_page_locally_invalidated_and_relearned")

            # The popup opener is explicitly authorized for this isolated fixture action.
            popup = Action("CLICK", Selector(text="显示提示"), purpose="task", expected_text="提示")
            grant = TaskAuthorization(APP, frozenset({authorization_key(popup)}), "Explicit Motor fixture popup acceptance")
            act(popup, grant, "提示")
            act(Action("CLICK", Selector(text="关闭")), expect="我的")
            report["checks"].append("popup_observed_closed_and_verified")

            act(Action("CLICK", Selector(resource_id=APP + ":id/motor_tab_products", text="商品")), expect="商品")
            lazy = Action("CLICK", Selector(text="加载更多商品（延迟）"), purpose="task", expected_text="延迟商品已加载")
            grant = TaskAuthorization(APP, frozenset({authorization_key(lazy)}), "Explicit Motor fixture lazy-loading acceptance")
            act(lazy, grant, "延迟商品已加载")
            report["checks"].append("event_driven_lazy_load_wait")

            act(Action("CLICK", Selector(text="查看循环列表（测试模式）")), expect="循环列表")
            infinite = motor.scout(APP, "deep", budget=Budget(seconds=20, max_scrolls=2, max_actions=4, max_depth=0), follow_tabs=False)
            report["infinite"] = infinite
            require(infinite["metrics"]["scrolls"] <= 4, "Infinite list exceeded scan and bounded restore allowance")
            require(any(p["stop_reason"] in {"scroll_budget", "no_new_content"} for p in infinite.get("pages", [])), "Infinite list did not stop under budget")
            report["checks"].append("infinite_list_budget_and_explicit_restore_report")

            reset()
            with ActionArbiter(adapter, APP, seconds=6) as arbiter:
                old = dict(arbiter.lease)
                legacy = adapter.perform("back", {"expectedPackage": APP}, str(uuid.uuid4()))
                require(legacy.get("error", {}).get("code") == "motor_busy", "Legacy DeepSeek action bypassed Motor ownership")
                adapter.cancel("fixture_cancel", old)
                stale = adapter.perform("back", {"expectedPackage": APP}, str(uuid.uuid4()), old)
                require(not stale.get("ok"), "Cancelled lease executed an operation")
            report["checks"].extend(["legacy_and_motor_share_one_executor", "cancel_fences_stale_operations"])

            with ActionArbiter(adapter, APP, seconds=6) as arbiter:
                reflex = ReflexEngine(arbiter, store, sink)
                before = reflex.observe(stable=True)
                reflex.observe()  # Deliberately supersede the retained target snapshot.
                failed = reflex.execute(Action("CLICK", Selector(resource_id=APP + ":id/motor_tab_products", text="商品")),
                                        state=before)
                require(failed.status == "failed" and failed.reason == "stale_snapshot", "Stale snapshot was not rejected without retry")
                report["failure_action_id"] = failed.action_id
                report["map_before"] = store.stats()
                require(bool(store.summary(APP)), "Map persistence unavailable")
            report["checks"].append("failed_action_preserved_without_positive_relabel")
            report["passed"] = True
        except Exception as exc:
            report["error_type"] = type(exc).__name__
            report["error"] = str(exc)[:300]
        finally:
            sink.disable()
            report["transfer"] = sink.sync()
            report["second_transfer"] = sink.sync()
            report["collection"] = sink.report()
            report["elapsed_ms"] = round((time.monotonic() - start) * 1000, 2)
            memory = adb.run(serial, "shell", "dumpsys", "meminfo", "io.daxqo.xm" if report.get("platform") == "xm" else "io.daxqo.app")
            match = re.search(r"TOTAL PSS:\s*(\d+)", memory)
            report["android_process_pss_kb"] = int(match.group(1)) if match else None
            report["resource_note"] = "PSS is the entire DAXQO process; isolated Motor overhead requires paired baseline measurement."
            sink.close()
    destination = output / ("acceptance-" + serial + ".json")
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--serial", action="append", required=True)
    p.add_argument("--data", type=Path, default=ROOT / "data/acceptance")
    p.add_argument("--output", type=Path, default=ROOT / "dist")
    p.add_argument("--trajectory-data", type=Path, required=True, help="Explicit shared Collector Dataset root")
    args = p.parse_args(argv)
    load_collector()
    from trajectory.dataset import Dataset
    from trajectory.transport import Adb
    args.output.mkdir(parents=True, exist_ok=True)
    with Dataset(args.trajectory_data) as dataset, MapStore(args.data / "map.sqlite3") as store:
        reports = []
        with ThreadPoolExecutor(max_workers=len(args.serial)) as pool:
            futures = {pool.submit(run_device, Adb(), serial, dataset, store, args.output): serial for serial in args.serial}
            for future in as_completed(futures):
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"serial": futures[future], "passed": False, "error_type": type(exc).__name__}
                reports.append(result)
                print(json.dumps({k: result.get(k) for k in ("serial", "passed", "checks", "error", "elapsed_ms")}, ensure_ascii=True), flush=True)
        export = dataset.export(ROOT / "exports/acceptance", split_by="task")
        overall = {"devices": reports, "map": store.stats(), "dataset": dataset.stats(), "export": export}
        (args.output / "acceptance.json").write_text(json.dumps(overall, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all(r["passed"] for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
