"""Standalone authenticated bridge transports; no Collector import required."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


class TransportError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class BridgeTransport:
    def __init__(self, port: int, capability: str, *, serial="local", opener=None, delegate=None):
        if not 1 <= int(port) <= 65535 or not re.fullmatch(r"[A-Za-z0-9_-]{32,512}", capability):
            raise TransportError("invalid_local_pairing")
        self.port, self._capability, self.serial = int(port), capability, serial
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self._delegate = delegate

    def request(self, path: str, body=None, timeout=12):
        # Never allow URL authorities, arbitrary endpoints, redirects or proxies.
        parsed = urllib.parse.urlsplit(path)
        allowed = {"/api/device/status", "/api/device/execute", "/api/motor/status", "/api/motor/events",
                   "/api/motor/lease", "/api/motor/release", "/api/motor/cancel",
                   "/api/trajectory/status", "/api/trajectory/config", "/api/trajectory/feedback",
                   "/api/trajectory/ack", "/api/trajectory/batch"}
        if parsed.path not in allowed or parsed.netloc or parsed.scheme or parsed.fragment:
            raise TransportError("unsupported_route")
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode() if body is not None else None
        req = urllib.request.Request("http://127.0.0.1:" + str(self.port) + path, data=raw,
                headers={"Host": "127.0.0.1:18883", "Authorization": "Bearer " + self._capability,
                         "Content-Type": "application/json", "Connection": "close"})
        try:
            with self.opener.open(req, timeout=max(.1, min(timeout, 20))) as response:
                data = response.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise TransportError("response_too_large")
            value = json.loads(data)
            if not isinstance(value, dict):
                raise TransportError("invalid_response")
            return value
        except urllib.error.HTTPError as exc:
            raise TransportError("http_" + str(exc.code)) from None
        except (OSError, ValueError, urllib.error.URLError):
            # Errors must not echo the authenticated request or UI contents.
            raise TransportError("bridge_unavailable_or_timeout") from None

    def close(self):
        try:
            if self._delegate:
                self._delegate.close()
        finally:
            self._capability = ""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class Adb:
    """Explicit executable or PATH/ANDROID_HOME discovery, without home scans."""

    def __init__(self, path=None):
        sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
        candidate = (path or os.environ.get("DAXQO_ADB") or shutil.which("adb")
                     or (str(Path(sdk) / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")) if sdk else None))
        if not candidate or not Path(candidate).is_file():
            raise TransportError("adb_unavailable: use --adb, DAXQO_ADB, or PATH")
        self.path = str(Path(candidate).resolve())

    def run(self, serial, *args, timeout=30):
        if serial is not None and not re.fullmatch(r"[A-Za-z0-9_.:\-]+", serial):
            raise TransportError("invalid_device_serial")
        result = subprocess.run([self.path, *(["-s", serial] if serial else []), *map(str, args)],
                                capture_output=True, timeout=timeout)
        if result.returncode:
            raise TransportError("adb_operation_failed")
        return result.stdout.decode("utf-8", errors="replace").strip()

    def devices(self):
        found = []
        for line in self.run(None, "devices", "-l").splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                found.append({"serial": parts[0], "connection": parts[1],
                              **dict(p.split(":", 1) for p in parts[2:] if ":" in p)})
        return found


class _UsbForward:
    def __init__(self, adb, serial, port):
        self.adb, self.serial, self.port = adb, serial, port

    def close(self):
        if self.port is not None:
            try:
                self.adb.run(self.serial, "forward", "--remove", "tcp:" + str(self.port))
            finally:
                self.port = None


def usb_transport(serial: str, adb=None, collector_root=None, package=None):
    # collector_root is accepted for compatibility; USB itself is independent.
    adb = Adb(adb)
    if package is None:
        installed = set(adb.run(serial, "shell", "pm", "list", "packages", "io.daxqo").splitlines())
        package = next((p for p in ("io.daxqo.app", "io.daxqo.xm") if "package:" + p in installed), None)
    if not package or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", package):
        raise TransportError("compatible_android_bridge_not_installed")
    capability = os.environ.get("DAXQO_CAPABILITY_" + re.sub(r"[^A-Za-z0-9]", "_", serial))
    if not capability:
        try:
            xml = adb.run(serial, "exec-out", "run-as", package, "cat", "shared_prefs/pairing_private.xml")
            item = ET.fromstring(xml).find("string[@name='capability']")
            capability = item.text if item is not None else None
        except Exception:
            raise TransportError("pairing_unavailable: pair the bridge or set DAXQO_CAPABILITY_<serial>") from None
    if not isinstance(capability, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,512}", capability):
        raise TransportError("invalid_local_pairing")
    try:
        port = int(adb.run(serial, "forward", "tcp:0", "tcp:18883"))
    except (ValueError, TransportError):
        raise TransportError("usb_forward_unavailable") from None
    delegate = _UsbForward(adb, serial, port)
    try:
        return BridgeTransport(port, capability, serial=serial, delegate=delegate)
    except Exception:
        delegate.close()
        raise


def linux_transport(state_dir, port=18883, device_id=None):
    root = Path(state_dir).absolute()
    file = root / "capability"
    try:
        folder_info, info = root.lstat(), file.lstat()
        if not stat.S_ISDIR(folder_info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise TransportError("invalid_local_pairing")
        if hasattr(os, "getuid") and (folder_info.st_uid != os.getuid() or info.st_uid != os.getuid()):
            raise TransportError("invalid_local_pairing")
        if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
            raise TransportError("insecure_local_pairing_permissions")
        value = file.read_text(encoding="ascii").strip()
    except (OSError, ValueError):
        raise TransportError("local_pairing_unavailable") from None
    client = BridgeTransport(port, value)
    identity = device_id
    if identity is None:
        # Legacy DAXQO bridges report identity on trajectory/status even when
        # recording is off. Other bridges can use motor/status or --device-id.
        for route in ("/api/motor/status", "/api/trajectory/status"):
            try:
                identity = client.request(route).get("device_id")
            except TransportError:
                continue
            if identity:
                break
    if not isinstance(identity, str) or not re.fullmatch(r"[a-fA-F0-9-]{36}", identity):
        client.close()
        raise TransportError("stable_device_identity_unavailable")
    client.serial = identity
    return client
