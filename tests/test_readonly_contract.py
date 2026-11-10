# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""The application must be read-only toward the iPhone.

These tests inspect our own source tree (not pymobiledevice3) and fail if
device-facing code could call an AFC mutation, or if anything other than the
read-only adapter can reach AfcService.
"""

import ast
import asyncio
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "src"     # the application source
APP_DIRS = ["app", "iphone", "photos", "export", "ui"]
ADAPTER = ROOT / "iphone" / "readonly_afc.py"

PROHIBITED = {
    "fwrite", "write", "pwrite", "remove", "rm", "rm_single", "rename", "mkdir", "makedirs", "rmdir", "truncate",
    "ftruncate", "set_file_contents", "push", "upload", "unlink", "link", "make_link", "set_file_time", "lock",
    "write_bytes", "write_text", "delete", "move", "hardlink", "symlink", "set_time",
    "set_value", "remove_value", "pair", "unpair",
}
ADAPTER_ALLOWED_AFC_CALLS = {"__aenter__", "aclose", "stat", "listdir", "exists", "fopen", "fread", "fseek", "fclose"}
READONLY_IPHONE_API = {"connect", "disconnect", "device_info", "exists", "stat", "listdir", "open_read", "read",
                       "seek", "close", "ping", "connected", "udid"}
DEVICE_RECEIVER = re.compile(r"afc|iphone|device\b|\bdev\b|lockdown", re.I)


def _sources():
    files = []
    for d in APP_DIRS:
        files += sorted((ROOT / d).rglob("*.py"))
    files.append(ROOT / "main.py")
    return [(p, p.read_text(encoding="utf-8")) for p in files]


def _calls(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            yield node


def test_only_adapter_imports_afcservice():
    offenders = []
    for path, src in _sources():
        tree = ast.parse(src)
        for node in ast.walk(tree):
            hit = False
            if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[-1] == "afc":
                hit = True
            if isinstance(node, ast.Import) and any(a.name.endswith(".afc") for a in node.names):
                hit = True
            if isinstance(node, (ast.Name, ast.Attribute)) and \
                    (getattr(node, "id", None) == "AfcService" or getattr(node, "attr", None) == "AfcService"):
                hit = True
            if hit and path != ADAPTER:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, f"AfcService used outside the read-only adapter: {offenders}"


def test_device_facing_modules_call_no_mutations():
    offenders = []
    for path in sorted((ROOT / "iphone").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _calls(tree):
            if call.func.attr in PROHIBITED:
                offenders.append(f"{path.relative_to(ROOT)}:{call.lineno} .{call.func.attr}()")
    assert not offenders, offenders


def test_no_mutation_calls_on_device_objects_anywhere():
    offenders = []
    for path, src in _sources():
        tree = ast.parse(src)
        for call in _calls(tree):
            if call.func.attr not in PROHIBITED:
                continue
            receiver = ast.get_source_segment(src, call.func.value) or ""
            if DEVICE_RECEIVER.search(receiver):
                offenders.append(f"{path.relative_to(ROOT)}:{call.lineno} {receiver}.{call.func.attr}()")
    assert not offenders, offenders


def test_adapter_only_forwards_read_operations():
    src = ADAPTER.read_text(encoding="utf-8")
    tree = ast.parse(src)
    forwarded = set()
    for call in _calls(tree):
        recv = ast.get_source_segment(src, call.func.value) or ""
        if "__afc" in recv:
            forwarded.add(call.func.attr)
            if call.func.attr == "fopen":
                mode = call.args[1] if len(call.args) > 1 else None
                assert isinstance(mode, ast.Name) and mode.id == "_READ_ONLY_MODE"
    assert forwarded <= ADAPTER_ALLOWED_AFC_CALLS, forwarded - ADAPTER_ALLOWED_AFC_CALLS
    assert re.search(r'^_READ_ONLY_MODE = "r"$', src, re.M)


def test_public_api_has_no_mutation_methods():
    from iphone.device import ReadOnlyIPhone
    from iphone.readonly_afc import ReadOnlyAfc

    afc_api = {n for n in dir(ReadOnlyAfc) if not n.startswith("_")}
    assert afc_api == {"open", "close", "stat", "listdir", "exists", "fopen_read", "fread", "fseek", "fclose"}
    phone_api = {n for n in dir(ReadOnlyIPhone) if not n.startswith("_")}
    assert phone_api <= READONLY_IPHONE_API, phone_api - READONLY_IPHONE_API
    for name in PROHIBITED:
        assert not hasattr(ReadOnlyIPhone("x"), name)


class _RecordingAfc:
    """Stands in for pymobiledevice3's AfcService; any mutation call fails the test."""

    calls: list = []

    def __init__(self, lockdown, service_name=None):
        pass

    def __getattr__(self, name):
        if name in PROHIBITED or name in {"pwrite", "ftruncate", "fwrite", "set_file_contents", "makedirs"}:
            pytest.fail(f"read-only adapter attempted AfcService.{name}")
        raise AttributeError(name)

    async def __aenter__(self):
        self.calls.append(("open",))
        return self

    async def aclose(self):
        self.calls.append(("close",))

    async def stat(self, p):
        self.calls.append(("stat", p))
        return {}

    async def listdir(self, p):
        self.calls.append(("listdir", p))
        return []

    async def exists(self, p):
        return True

    async def fopen(self, p, mode="r"):
        self.calls.append(("fopen", p, mode))
        return 1

    async def fread(self, h, n):
        self.calls.append(("fread", n))
        return b""

    async def fseek(self, h, off, whence=0):
        self.calls.append(("fseek", off))

    async def fclose(self, h):
        self.calls.append(("fclose",))


def test_device_access_is_read_only(monkeypatch):
    """Drive the adapter end to end and check every AFC call that reaches the library."""
    import iphone.readonly_afc as ra

    _RecordingAfc.calls = []
    monkeypatch.setattr(ra, "AfcService", _RecordingAfc)
    afc = ra.ReadOnlyAfc(object())

    async def go():
        await afc.open()
        await afc.stat("DCIM")
        await afc.listdir("DCIM")
        h = await afc.fopen_read("DCIM/100APPLE/IMG_1.JPG")
        await afc.fseek(h, 10)
        await afc.fread(h, 100 * 1024 * 1024)
        await afc.fclose(h)
        await afc.close()

    asyncio.run(go())
    modes = [c[2] for c in _RecordingAfc.calls if c[0] == "fopen"]
    assert modes == ["r"]
    reads = [c[1] for c in _RecordingAfc.calls if c[0] == "fread"]
    assert reads == [ra.MAX_READ]      # oversized requests are clamped to one AFC packet
    assert {c[0] for c in _RecordingAfc.calls} <= {"open", "close", "stat", "listdir", "fopen", "fread", "fseek",
                                                    "fclose"}


# Every pymobiledevice3 import in the application, per module.  Anything new must be
# reviewed for device-side effects and added here deliberately.
PMD_IMPORT_ALLOWLIST = {
    "iphone/readonly_afc.py": {"pymobiledevice3.services.afc:AfcService"},
    "iphone/connection.py": {"pymobiledevice3:exceptions", "pymobiledevice3.lockdown:create_using_usbmux",
                             "pymobiledevice3.usbmux:list_devices"},
    "iphone/device.py": {"pymobiledevice3:exceptions"},
}
# Lockdown / service calls that can change device state or open other services.
BANNED_CALLS = {
    "set_value", "remove_value", "pair", "unpair", "pair_supervised", "start_lockdown_service",
    "start_lockdown_developer_service", "create_service_connection", "start_service", "enter_recovery",
    "_rm_tree", "_remove", "_push_internal", "_do_operation", "_send_and_wait", "_dispatch_packet",
    "import_module", "__import__",
}


def test_pymobiledevice3_imports_are_allowlisted():
    found = {}
    for path, src in _sources():
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "pymobiledevice3":
                for a in node.names:
                    found.setdefault(rel, set()).add(f"{node.module}:{a.name}")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] == "pymobiledevice3":
                        found.setdefault(rel, set()).add(a.name)
    assert found == PMD_IMPORT_ALLOWLIST


def test_no_banned_device_calls_or_dynamic_imports():
    offenders = []
    for path, src in _sources():
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
                if name in BANNED_CALLS:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} {name}()")
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = [a.name for a in node.names] + ([node.module] if isinstance(node, ast.ImportFrom) else [])
                if "importlib" in mods:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} importlib")
    assert not offenders, offenders


def test_lockdown_never_initiates_pairing():
    src = (ROOT / "iphone" / "connection.py").read_text(encoding="utf-8")
    calls = [c for c in ast.walk(ast.parse(src))
             if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "create_using_usbmux"]
    assert calls, "create_using_usbmux not found"
    for c in calls:
        kw = {k.arg: k.value for k in c.keywords}
        assert isinstance(kw.get("autopair"), ast.Constant) and kw["autopair"].value is False
        assert isinstance(kw.get("connection_type"), ast.Constant) and kw["connection_type"].value == "USB"


def test_device_paths_validated_before_afc():
    from export.path_safety import UnsafePathError
    from iphone.device import ReadOnlyIPhone

    dev = ReadOnlyIPhone("x")
    for bad in ["../Library/SMS/sms.db", "/private/var/mobile", "Books/x", "DCIM/../../etc"]:
        with pytest.raises(UnsafePathError):
            asyncio.run(dev.stat(bad))
