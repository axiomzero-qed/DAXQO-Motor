"""Deploy local Motor through the already-paired phone Harness, without model keys."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
import time
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from motor.trajectory import load_collector
from package_motor import create_bundle


def bootstrap(archive_sha: str, harness_root: str, serial: str, *, archive_path=None, result_path=None):
    # The payload contains source hashes and paths only. Pairing remains in DeviceClient.
    return f'''from pathlib import Path, PurePosixPath
import hashlib,json,subprocess,sys,tarfile
archive=Path({(archive_path or '/sdcard/Download/daxqo-motor/daxqo-motor-v0.1.tar.gz')!r})
result_file=Path({(result_path or '/sdcard/Download/daxqo-motor/deploy-result-' + serial + '.json')!r})
result={{'ok':False}}
try:
    if hashlib.sha256(archive.read_bytes()).hexdigest()!={archive_sha!r}:raise ValueError('Bundle digest mismatch')
    staging=Path.home()/'.local/share/daxqo-motor/staging'/{archive_sha!r}
    staging.mkdir(mode=0o700,parents=True,exist_ok=True)
    with tarfile.open(archive,'r:gz') as source:
        members=source.getmembers()
        if len(members)>512 or sum(m.size for m in members)>32*1024*1024:raise ValueError('Bundle exceeds limit')
        for m in members:
            p=PurePosixPath(m.name)
            if not m.isfile() or p.is_absolute() or '..' in p.parts or not p.parts or p.parts[0]!='daxqo-motor-v0.1':raise ValueError('Invalid source member')
            dest=staging/Path(*p.parts)
            if dest.is_symlink() or any(parent.is_symlink() for parent in dest.parents):raise ValueError('Unexpected staging link')
            dest.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            dest.write_bytes(source.extractfile(m).read())
    unpacked=staging/'daxqo-motor-v0.1'
    command=[sys.executable,str(unpacked/'scripts/install_linux.py'),'--source',str(unpacked),'--harness-root',{harness_root!r},'--integrate-harness']
    done=subprocess.run(command,capture_output=True,text=True,timeout=40)
    if done.returncode:raise RuntimeError('Installer failed: '+done.stderr[-600:])
    result=json.loads(done.stdout)
    result['ok']=True
    if (unpacked/'tests/test_mcp_cancellation.py').is_file():
        checked=subprocess.run([sys.executable,'-m','unittest','tests.test_mcp_cancellation.CancellationTests.test_linux_sigterm_subprocess_releases_its_lease','-v'],cwd=unpacked,capture_output=True,text=True,timeout=15)
        result['linux_signal_test']={{'passed':checked.returncode==0,'summary':checked.stderr[-700:]}}
        if checked.returncode:result['ok']=False
except Exception as e:
    result={{'ok':False,'error_type':type(e).__name__,'error':str(e)[:600]}}
result_file.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False))
raise SystemExit(0 if result['ok'] else 1)
'''


def upload(client, path):
    """Use the Harness's existing bounded attachment endpoint, including in PRoot."""
    port = client.adb.run(client.serial, "forward", "tcp:0", "tcp:18880")
    try:
        request = urllib.request.Request("http://127.0.0.1:" + port + "/api/uploads?name=" + urllib.parse.quote(path.name),
            data=path.read_bytes(), headers={"Host": "127.0.0.1:18880", "Authorization": "Bearer " + client.token,
                                             "Content-Type": "application/octet-stream"})
        with client.opener.open(request, timeout=15) as response:
            value = json.load(response)
        relative = PurePosixPath(value["attachment"]["path"])
        if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != "imports":
            raise RuntimeError("Invalid Harness attachment path")
        return relative.as_posix()
    finally:
        client.adb.run(client.serial, "forward", "--remove", "tcp:" + port)


def deploy(serial, bundle, sha, *, restart=False, seconds=180):
    from trajectory.transport import Adb, DeviceClient
    adb = Adb()
    report = {"serial": serial, "ok": False, "bundle_sha256": sha}
    with DeviceClient(adb, serial) as client:
        status = client.service_request("agent", "/api/status")
        if status.get("activeTaskId") or status.get("activeTaskIds"):
            raise RuntimeError("An existing task is active; deployment was not started")
        workspace = status.get("workspace")
        if not isinstance(workspace, str) or not workspace.startswith("/"):
            raise RuntimeError("Harness did not report its Linux workspace")
        harness_root = str(PurePosixPath(workspace).parent)
        archive_relative = upload(client, bundle)
        result_relative = "imports/deploy-result-" + serial + ".json"
        script = ROOT / "dist" / ("deploy-" + serial + ".py")
        script.write_text(bootstrap(sha, harness_root, serial,
            archive_path=str(PurePosixPath(workspace) / archive_relative),
            result_path=str(PurePosixPath(workspace) / result_relative)), encoding="utf-8")
        script_relative = upload(client, script)
        script_remote = str(PurePosixPath(workspace) / script_relative)
        prompt = ("这是用户授权的 DAXQO Motor 本地安装。只执行一次终端命令：python3 " + script_remote + " 。"
                  "该脚本核对源码包哈希、安装 Motor/Collector Python 包并给当前 Harness 追加独立 Motor MCP，保留旧配置且不重启。"
                  "不要操作手机UI，不读取或打印模型密钥，不自行改脚本或其他文件。完成后简短报告脚本JSON；失败如实报告。")
        task = client.service_request("agent", "/api/tasks", {"title": "Motor本地安装", "prompt": prompt,
                                      "clientRequestId": "motor-deploy-" + str(uuid.uuid4())})["task"]
        report["task_id"] = task["id"]
        report["harness_root"] = harness_root
        last = None
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            detail = client.service_request("agent", "/api/tasks/" + task["id"])
            state = detail["task"]["status"]
            if state != last:
                print(json.dumps({"serial": serial, "task_id": task["id"], "state": state}), flush=True)
                last = state
            if state not in {"queued", "running", "starting"}:
                break
            time.sleep(2)
        else:
            client.service_request("agent", "/api/tasks/" + task["id"] + "/stop", {})
            raise RuntimeError("Deployment task timed out and was stopped")
        report["task_status"] = state
        try:
            body = client.service_request("agent", "/api/files/text?path=" + urllib.parse.quote(result_relative))
            result = json.loads(body["text"])
        except Exception:
            result = {"ok": False, "error": "Installer result unavailable"}
        report["installation"] = result
        report["ok"] = state == "completed" and result.get("ok") is True
        if report["ok"] and restart:
            idle = client.service_request("agent", "/api/status")
            if idle.get("activeTaskId") or idle.get("activeTaskIds"):
                report["restart"] = "deferred_existing_task_active"
            else:
                report["restart"] = client.service_request("runtime", "/api/runtime/restart", {})
                ready_deadline = time.monotonic() + 50
                while time.monotonic() < ready_deadline:
                    try:
                        current = client.service_request("agent", "/api/status", timeout=4)
                        if current.get("engine", {}).get("connected"):
                            report["restart_ready"] = True
                            break
                    except Exception:
                        pass
                    time.sleep(2)
        (ROOT / "dist" / ("deploy-result-" + serial + ".json")).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--serial", action="append", required=True)
    p.add_argument("--restart", action="store_true")
    p.add_argument("--collector-root", type=Path, help="Explicit Collector checkout to include in the offline bundle")
    args = p.parse_args()
    load_collector()
    bundle = ROOT / "dist/daxqo-motor-v0.1.tar.gz"
    meta = create_bundle(bundle, collector_root=args.collector_root)
    reports = []
    with ThreadPoolExecutor(max_workers=len(args.serial)) as pool:
        futures = {pool.submit(deploy, serial, bundle, meta["sha256"], restart=args.restart): serial for serial in args.serial}
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:
                result = {"serial": futures[future], "ok": False, "error_type": type(exc).__name__, "error": str(exc)[:300]}
            reports.append(result)
            print(json.dumps(result, ensure_ascii=True), flush=True)
    (ROOT / "dist/deployment.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all(r["ok"] for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
