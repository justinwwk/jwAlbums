# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Negotiated USB link detection for a connected iPhone (Windows only).

Uses only the in-box Windows USB stack (SetupAPI, CfgMgr32 and the USB hub
IOCTLs) via ctypes. No custom driver, no third-party packages.

Flow:
  1. Enumerate present devices exposing GUID_DEVINTERFACE_USB_DEVICE and find
     the Apple (VID_05AC) device whose instance-ID serial equals the UDID with
     hyphens removed (case-insensitive).
  2. CM_Get_Parent -> hub devnode -> hub interface path
     (GUID_DEVINTERFACE_USB_HUB); port index from SPDRP_ADDRESS (fallback:
     LocationInfo "Port_#0002.Hub_#0003").
  3. Open the hub and issue IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX and
     IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX_V2 for that port.

The negotiated link describes the iPhone + cable + PC port/hub combination.
It is not file-transfer throughput and it does not identify the cable.

query_iphone_usb_link() never raises; any failure yields an Unknown result
with a short reason.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass

USB2_HS = "USB 2 High-Speed"
USB3_SS = "USB 3 SuperSpeed"
USB3_SSP = "USB 3 SuperSpeedPlus"
USB_FS = "USB 1.1 Full-Speed"
USB_LS = "USB 1.1 Low-Speed"
USB_UNKNOWN = "Unknown"

# USB_DEVICE_SPEED (usbspec.h)
SPEED_LOW = 0
SPEED_FULL = 1
SPEED_HIGH = 2
SPEED_SUPER = 3

# USB_NODE_CONNECTION_INFORMATION_EX_V2_FLAGS bits (usbioctl.h)
FLAG_OPERATING_AT_SS = 0x1
FLAG_SS_CAPABLE = 0x2
FLAG_OPERATING_AT_SSP = 0x4
FLAG_SSP_CAPABLE = 0x8

APPLE_VID = 0x05AC


@dataclass(frozen=True)
class UsbLinkInfo:
    mode: str
    link_mbps: int | None
    superspeed_capable: bool | None
    superspeedplus_capable: bool | None
    reason: str = ""

    @property
    def link_text(self) -> str:
        if self.mode == USB_LS:
            return "1.5 Mb/s"
        if self.link_mbps is None:
            return "unknown"
        if self.link_mbps >= 1000:
            gb = self.link_mbps / 1000
            gb_txt = f"{gb:g}"
            return f"up to {gb_txt} Gb/s"
        return f"{self.link_mbps} Mb/s"

    @property
    def below_superspeed(self) -> bool:
        return self.mode in (USB2_HS, USB_FS, USB_LS)


def _unknown(reason: str) -> UsbLinkInfo:
    return UsbLinkInfo(USB_UNKNOWN, None, None, None, reason)


def classify_link(speed: int | None, v2_flags: int | None) -> UsbLinkInfo:
    """Pure classification of hub-reported speed + V2 flags."""
    ss_cap: bool | None = None
    ssp_cap: bool | None = None
    if v2_flags is not None:
        ss_cap = bool(v2_flags & (FLAG_SS_CAPABLE | FLAG_SSP_CAPABLE))
        ssp_cap = bool(v2_flags & FLAG_SSP_CAPABLE)
        if v2_flags & FLAG_OPERATING_AT_SSP:
            return UsbLinkInfo(USB3_SSP, 10000, ss_cap, ssp_cap)
        if v2_flags & FLAG_OPERATING_AT_SS:
            return UsbLinkInfo(USB3_SS, 5000, ss_cap, ssp_cap)

    if speed is None:
        return UsbLinkInfo(USB_UNKNOWN, None, ss_cap, ssp_cap, "speed not reported")
    if speed == SPEED_SUPER:
        return UsbLinkInfo(USB3_SS, 5000, ss_cap, ssp_cap)
    if speed == SPEED_HIGH:
        return UsbLinkInfo(USB2_HS, 480, ss_cap, ssp_cap)
    if speed == SPEED_FULL:
        return UsbLinkInfo(USB_FS, 12, ss_cap, ssp_cap)
    if speed == SPEED_LOW:
        return UsbLinkInfo(USB_LS, None, ss_cap, ssp_cap)
    return UsbLinkInfo(USB_UNKNOWN, None, ss_cap, ssp_cap, f"unrecognised speed value {speed}")


_TEN_GB_PRODUCTS = frozenset(
    {
        "iPhone16,1", "iPhone16,2",  # iPhone 15 Pro / Pro Max
        "iPhone17,1", "iPhone17,2",  # iPhone 16 Pro / Pro Max
        "iPhone18,1", "iPhone18,2",  # iPhone 17 Pro / Pro Max
    }
)


def device_max_link_mbps(product_type: str) -> int:
    """Maximum USB data rate the iPhone model supports (Mb/s)."""
    if isinstance(product_type, str) and product_type.strip() in _TEN_GB_PRODUCTS:
        return 10000
    return 480


def summarize(
    link: UsbLinkInfo, product_type: str, display_name: str | None
) -> tuple[str, list[str], str | None]:
    """Return (headline, lines, warning_or_None) for display."""
    if link.mode == USB_UNKNOWN or (link.link_mbps is None and link.mode != USB_LS):
        return USB_UNKNOWN, ["Negotiated link: could not be determined"], None

    name = display_name or "iPhone"
    max_mbps = device_max_link_mbps(product_type)
    # If Windows reports the device as SS/SS+ capable, honour that too (e.g. a
    # newer Pro model whose product type is not yet in the table).
    if link.superspeedplus_capable:
        max_mbps = max(max_mbps, 10000)
    elif link.superspeed_capable:
        max_mbps = max(max_mbps, 5000)

    lines = [f"Negotiated link: {link.link_text}"]
    warning: str | None = None
    current = link.link_mbps or 0

    if current >= max_mbps:
        lines.append(f"Status: Maximum {name} link speed")
    elif link.below_superspeed and max_mbps >= 5000:
        target = "10 Gb/s-capable" if max_mbps >= 10000 else "5 Gb/s-capable"
        warning = (
            "Your iPhone supports faster USB transfer, but the current cable/port "
            "connection is limited to USB 2.0.\n"
            f"Try a USB 3 {target} cable or another USB-C port."
        )
    elif link.mode == USB3_SS and max_mbps >= 10000:
        warning = (
            "The current cable/port connection is running at up to 5 Gb/s; "
            "your iPhone supports up to 10 Gb/s.\n"
            "You can continue, or try a USB 3 10 Gb/s-capable cable or another USB-C port."
        )
    return link.mode, lines, warning


def query_iphone_usb_link(udid: str) -> UsbLinkInfo:
    """Query the negotiated USB link of the iPhone with this UDID. Never raises."""
    try:
        if sys.platform != "win32":
            return _unknown("USB link detection is only available on Windows")
        if not isinstance(udid, str) or not _normalize_serial(udid):
            return _unknown("no device identifier")
        return _query_windows(udid)
    except Exception as exc:  # noqa: BLE001 - must never raise
        return _unknown(f"USB topology lookup failed ({type(exc).__name__})")


# ---------------------------------------------------------------------------
# Windows implementation
# ---------------------------------------------------------------------------

def _normalize_serial(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", s).upper()


_api = None  # lazily-built namespace of ctypes bindings


def _load_api():
    global _api
    if _api is not None:
        return _api

    import ctypes
    from ctypes import wintypes as wt

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wt.DWORD),
            ("Data2", wt.WORD),
            ("Data3", wt.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

        @classmethod
        def from_str(cls, s: str) -> "GUID":
            h = s.strip("{}").replace("-", "")
            g = cls()
            g.Data1 = int(h[0:8], 16)
            g.Data2 = int(h[8:12], 16)
            g.Data3 = int(h[12:16], 16)
            for i, b in enumerate(bytes.fromhex(h[16:32])):
                g.Data4[i] = b
            return g

    class SP_DEVINFO_DATA(ctypes.Structure):
        _fields_ = [
            ("cbSize", wt.DWORD),
            ("ClassGuid", GUID),
            ("DevInst", wt.DWORD),
            ("Reserved", ctypes.c_size_t),
        ]

    class USB_DEVICE_DESCRIPTOR(ctypes.Structure):
        _pack_ = 1
        _fields_ = [
            ("bLength", ctypes.c_ubyte),
            ("bDescriptorType", ctypes.c_ubyte),
            ("bcdUSB", ctypes.c_ushort),
            ("bDeviceClass", ctypes.c_ubyte),
            ("bDeviceSubClass", ctypes.c_ubyte),
            ("bDeviceProtocol", ctypes.c_ubyte),
            ("bMaxPacketSize0", ctypes.c_ubyte),
            ("idVendor", ctypes.c_ushort),
            ("idProduct", ctypes.c_ushort),
            ("bcdDevice", ctypes.c_ushort),
            ("iManufacturer", ctypes.c_ubyte),
            ("iProduct", ctypes.c_ubyte),
            ("iSerialNumber", ctypes.c_ubyte),
            ("bNumConfigurations", ctypes.c_ubyte),
        ]

    class USB_NODE_CONNECTION_INFORMATION_EX(ctypes.Structure):
        _pack_ = 1
        _fields_ = [
            ("ConnectionIndex", ctypes.c_ulong),
            ("DeviceDescriptor", USB_DEVICE_DESCRIPTOR),
            ("CurrentConfigurationValue", ctypes.c_ubyte),
            ("Speed", ctypes.c_ubyte),
            ("DeviceIsHub", ctypes.c_ubyte),
            ("DeviceAddress", ctypes.c_ushort),
            ("NumberOfOpenPipes", ctypes.c_ulong),
            ("ConnectionStatus", ctypes.c_int),
            # USB_PIPE_INFO PipeList[0] follows (11 bytes each, packed)
        ]

    class USB_NODE_CONNECTION_INFORMATION_EX_V2(ctypes.Structure):
        _pack_ = 1
        _fields_ = [
            ("ConnectionIndex", ctypes.c_ulong),
            ("Length", ctypes.c_ulong),
            ("SupportedUsbProtocols", ctypes.c_ulong),
            ("Flags", ctypes.c_ulong),
        ]

    assert ctypes.sizeof(USB_DEVICE_DESCRIPTOR) == 18
    assert ctypes.sizeof(USB_NODE_CONNECTION_INFORMATION_EX) == 35
    assert ctypes.sizeof(USB_NODE_CONNECTION_INFORMATION_EX_V2) == 16

    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    cfgmgr32 = ctypes.WinDLL("cfgmgr32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    HDEVINFO = ctypes.c_void_p
    PDATA = ctypes.POINTER(SP_DEVINFO_DATA)

    setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.POINTER(GUID), wt.LPCWSTR, wt.HWND, wt.DWORD]
    setupapi.SetupDiGetClassDevsW.restype = HDEVINFO
    setupapi.SetupDiEnumDeviceInfo.argtypes = [HDEVINFO, wt.DWORD, PDATA]
    setupapi.SetupDiEnumDeviceInfo.restype = wt.BOOL
    setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [
        HDEVINFO, PDATA, wt.LPWSTR, wt.DWORD, ctypes.POINTER(wt.DWORD)]
    setupapi.SetupDiGetDeviceInstanceIdW.restype = wt.BOOL
    setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
        HDEVINFO, PDATA, wt.DWORD, ctypes.POINTER(wt.DWORD),
        ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)]
    setupapi.SetupDiGetDeviceRegistryPropertyW.restype = wt.BOOL
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [HDEVINFO]
    setupapi.SetupDiDestroyDeviceInfoList.restype = wt.BOOL

    cfgmgr32.CM_Get_Parent.argtypes = [ctypes.POINTER(wt.DWORD), wt.DWORD, wt.ULONG]
    cfgmgr32.CM_Get_Parent.restype = wt.DWORD
    cfgmgr32.CM_Get_Device_IDW.argtypes = [wt.DWORD, wt.LPWSTR, wt.ULONG, wt.ULONG]
    cfgmgr32.CM_Get_Device_IDW.restype = wt.DWORD
    cfgmgr32.CM_Get_Device_Interface_List_SizeW.argtypes = [
        ctypes.POINTER(wt.ULONG), ctypes.POINTER(GUID), wt.LPCWSTR, wt.ULONG]
    cfgmgr32.CM_Get_Device_Interface_List_SizeW.restype = wt.DWORD
    cfgmgr32.CM_Get_Device_Interface_ListW.argtypes = [
        ctypes.POINTER(GUID), wt.LPCWSTR, ctypes.c_void_p, wt.ULONG, wt.ULONG]
    cfgmgr32.CM_Get_Device_Interface_ListW.restype = wt.DWORD

    kernel32.CreateFileW.argtypes = [
        wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
    kernel32.CreateFileW.restype = wt.HANDLE
    kernel32.DeviceIoControl.argtypes = [
        wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, wt.DWORD,
        ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
    kernel32.DeviceIoControl.restype = wt.BOOL
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    kernel32.CloseHandle.restype = wt.BOOL

    class _Api:
        pass

    api = _Api()
    api.ctypes = ctypes
    api.wt = wt
    api.GUID = GUID
    api.SP_DEVINFO_DATA = SP_DEVINFO_DATA
    api.CONN_EX = USB_NODE_CONNECTION_INFORMATION_EX
    api.CONN_EX_V2 = USB_NODE_CONNECTION_INFORMATION_EX_V2
    api.setupapi = setupapi
    api.cfgmgr32 = cfgmgr32
    api.kernel32 = kernel32
    _api = api
    return api


GUID_DEVINTERFACE_USB_DEVICE = "{A5DCBF10-6530-11D2-901F-00C04FB951ED}"
GUID_DEVINTERFACE_USB_HUB = "{F18A0E88-C30C-11D0-8815-00A0C906BED8}"

DIGCF_PRESENT = 0x02
DIGCF_ALLCLASSES = 0x04
DIGCF_DEVICEINTERFACE = 0x10
SPDRP_LOCATION_INFORMATION = 0x0D
SPDRP_ADDRESS = 0x1C
CR_SUCCESS = 0
CM_GET_DEVICE_INTERFACE_LIST_PRESENT = 0

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = (1 << (8 * 8)) - 1 if sys.maxsize > 2**32 else (1 << 32) - 1


def _ctl_code(device_type: int, function: int, method: int = 0, access: int = 0) -> int:
    return (device_type << 16) | (access << 14) | (function << 2) | method


FILE_DEVICE_USB = 0x22  # == FILE_DEVICE_UNKNOWN
USB_GET_NODE_CONNECTION_INFORMATION_EX = 274
USB_GET_NODE_CONNECTION_INFORMATION_EX_V2 = 279
IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX = _ctl_code(
    FILE_DEVICE_USB, USB_GET_NODE_CONNECTION_INFORMATION_EX)
IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX_V2 = _ctl_code(
    FILE_DEVICE_USB, USB_GET_NODE_CONNECTION_INFORMATION_EX_V2)
assert IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX == 0x220448
assert IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX_V2 == 0x22045C

USB_PROTOCOLS_ALL = 0x7  # Usb110 | Usb200 | Usb300
DEVICE_CONNECTED = 1  # USB_CONNECTION_STATUS.DeviceConnected


class _LookupError(Exception):
    """Internal: carries a short, user-safe reason."""


def _is_invalid_handle(h) -> bool:
    return h is None or h == 0 or h == INVALID_HANDLE_VALUE or h == -1


@dataclass(frozen=True)
class _DeviceLocation:
    devinst: int
    instance_id: str
    port: int | None
    location_info: str | None


def _find_apple_device(udid: str) -> _DeviceLocation:
    """Locate the physical Apple USB device whose serial matches the UDID."""
    api = _load_api()
    want = _normalize_serial(udid)

    candidates_seen = 0
    # Primary: devices exposing GUID_DEVINTERFACE_USB_DEVICE; fallback: all
    # present devices under the USB enumerator.
    attempts = [
        (api.GUID.from_str(GUID_DEVINTERFACE_USB_DEVICE), None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE),
        (None, "USB", DIGCF_PRESENT | DIGCF_ALLCLASSES),
    ]
    for guid, enumerator, flags in attempts:
        found, seen = _scan_device_set(api, guid, enumerator, flags, want)
        candidates_seen = max(candidates_seen, seen)
        if found is not None:
            return found

    if candidates_seen == 0:
        raise _LookupError("no Apple USB device found")
    raise _LookupError("no Apple USB device matched this iPhone")


def _scan_device_set(api, guid, enumerator, flags, want):
    ctypes, wt = api.ctypes, api.wt
    sa = api.setupapi
    guid_ptr = ctypes.byref(guid) if guid is not None else None
    hdev = sa.SetupDiGetClassDevsW(guid_ptr, enumerator, None, flags)
    if _is_invalid_handle(hdev):
        return None, 0
    seen = 0
    try:
        idx = 0
        while True:
            data = api.SP_DEVINFO_DATA()
            data.cbSize = ctypes.sizeof(api.SP_DEVINFO_DATA)
            if not sa.SetupDiEnumDeviceInfo(hdev, idx, ctypes.byref(data)):
                break
            idx += 1
            if idx > 4096:
                break
            buf = ctypes.create_unicode_buffer(512)
            req = wt.DWORD(0)
            if not sa.SetupDiGetDeviceInstanceIdW(hdev, ctypes.byref(data), buf, 512, ctypes.byref(req)):
                continue
            inst = buf.value
            up = inst.upper()
            if not up.startswith("USB\\VID_05AC&") or "&MI_" in up:
                continue
            seen += 1
            parts = inst.split("\\")
            if len(parts) < 3 or _normalize_serial(parts[2]) != want:
                continue
            port = _get_reg_dword(api, hdev, data, SPDRP_ADDRESS)
            loc = _get_reg_string(api, hdev, data, SPDRP_LOCATION_INFORMATION)
            return _DeviceLocation(int(data.DevInst), inst, port, loc), seen
    finally:
        sa.SetupDiDestroyDeviceInfoList(hdev)
    return None, seen


def _get_reg_dword(api, hdev, data, prop) -> int | None:
    ctypes, wt = api.ctypes, api.wt
    val = wt.DWORD(0)
    regtype = wt.DWORD(0)
    req = wt.DWORD(0)
    ok = api.setupapi.SetupDiGetDeviceRegistryPropertyW(
        hdev, ctypes.byref(data), prop, ctypes.byref(regtype),
        ctypes.byref(val), ctypes.sizeof(val), ctypes.byref(req))
    return int(val.value) if ok else None


def _get_reg_string(api, hdev, data, prop) -> str | None:
    ctypes, wt = api.ctypes, api.wt
    buf = ctypes.create_unicode_buffer(512)
    regtype = wt.DWORD(0)
    req = wt.DWORD(0)
    ok = api.setupapi.SetupDiGetDeviceRegistryPropertyW(
        hdev, ctypes.byref(data), prop, ctypes.byref(regtype),
        buf, ctypes.sizeof(buf), ctypes.byref(req))
    return buf.value if ok else None


def _port_from_location(loc: str | None) -> int | None:
    if not loc:
        return None
    m = re.search(r"Port_#(\d+)", loc, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _get_hub_path(devinst: int) -> str:
    api = _load_api()
    ctypes, wt = api.ctypes, api.wt
    cm = api.cfgmgr32

    parent = wt.DWORD(0)
    if cm.CM_Get_Parent(ctypes.byref(parent), devinst, 0) != CR_SUCCESS:
        raise _LookupError("parent USB hub not found")
    idbuf = ctypes.create_unicode_buffer(512)
    if cm.CM_Get_Device_IDW(parent.value, idbuf, 512, 0) != CR_SUCCESS:
        raise _LookupError("parent USB hub ID unavailable")
    hub_id = idbuf.value

    guid = api.GUID.from_str(GUID_DEVINTERFACE_USB_HUB)
    size = wt.ULONG(0)
    cr = cm.CM_Get_Device_Interface_List_SizeW(
        ctypes.byref(size), ctypes.byref(guid), hub_id, CM_GET_DEVICE_INTERFACE_LIST_PRESENT)
    if cr != CR_SUCCESS or size.value <= 1:
        raise _LookupError("parent USB hub interface not found")
    lst = ctypes.create_unicode_buffer(size.value + 1)
    cr = cm.CM_Get_Device_Interface_ListW(
        ctypes.byref(guid), hub_id, lst, size.value, CM_GET_DEVICE_INTERFACE_LIST_PRESENT)
    if cr != CR_SUCCESS:
        raise _LookupError("parent USB hub interface not found")
    raw = ctypes.wstring_at(ctypes.addressof(lst), size.value)
    paths = [p for p in raw.split("\x00") if p]
    if not paths:
        raise _LookupError("parent USB hub interface not found")
    return paths[0]


def _open_hub(path: str):
    api = _load_api()
    k32 = api.kernel32
    share = FILE_SHARE_READ | FILE_SHARE_WRITE
    for access in (GENERIC_WRITE, 0, GENERIC_READ):
        h = k32.CreateFileW(path, access, share, None, OPEN_EXISTING, 0, None)
        if not _is_invalid_handle(h):
            return h
    raise _LookupError("could not open USB hub")


def _query_hub_port(path: str, port: int) -> tuple[int | None, int | None, int | None]:
    """Return (speed, v2_flags, idVendor) for the hub port."""
    api = _load_api()
    ctypes, wt = api.ctypes, api.wt
    k32 = api.kernel32

    h = _open_hub(path)
    try:
        speed = None
        vendor = None
        size = ctypes.sizeof(api.CONN_EX) + 64 * 11 + 64
        buf = ctypes.create_string_buffer(size)
        info = api.CONN_EX.from_buffer(buf)
        info.ConnectionIndex = port
        ret = wt.DWORD(0)
        if k32.DeviceIoControl(h, IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX,
                               buf, size, buf, size, ctypes.byref(ret), None):
            if ret.value >= ctypes.sizeof(api.CONN_EX):
                if info.ConnectionStatus != DEVICE_CONNECTED:
                    raise _LookupError("hub reports no device connected on that port")
                speed = int(info.Speed)
                vendor = int(info.DeviceDescriptor.idVendor)

        flags = None
        v2 = api.CONN_EX_V2()
        v2.ConnectionIndex = port
        v2.Length = ctypes.sizeof(api.CONN_EX_V2)
        v2.SupportedUsbProtocols = USB_PROTOCOLS_ALL
        ret2 = wt.DWORD(0)
        if k32.DeviceIoControl(h, IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX_V2,
                               ctypes.byref(v2), ctypes.sizeof(v2),
                               ctypes.byref(v2), ctypes.sizeof(v2),
                               ctypes.byref(ret2), None):
            if ret2.value >= ctypes.sizeof(api.CONN_EX_V2):
                flags = int(v2.Flags)

        if speed is None and flags is None:
            raise _LookupError("USB hub did not report connection information")
        return speed, flags, vendor
    finally:
        k32.CloseHandle(h)


def _query_windows(udid: str) -> UsbLinkInfo:
    try:
        dev = _find_apple_device(udid)
        port = dev.port if dev.port else _port_from_location(dev.location_info)
        if not port:
            return _unknown("USB port number unavailable")
        hub_path = _get_hub_path(dev.devinst)
        speed, flags, vendor = _query_hub_port(hub_path, port)
    except _LookupError as exc:
        return _unknown(str(exc))

    if vendor is not None and vendor != APPLE_VID:
        return _unknown("hub port does not hold the Apple device")
    info = classify_link(speed, flags)
    if info.mode == USB_UNKNOWN:
        return info
    detail = f"hub port {port}"
    return UsbLinkInfo(info.mode, info.link_mbps, info.superspeed_capable,
                       info.superspeedplus_capable, detail)
