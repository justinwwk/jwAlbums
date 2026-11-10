# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Device discovery over Apple's usbmux, USB-attached devices only."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from pymobiledevice3 import exceptions as pmd_exc
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.usbmux import list_devices

log = logging.getLogger(__name__)

MSG_NO_DEVICE = "No iPhone connected"
MSG_NO_USBMUX = "Apple Devices is not installed/running"
MSG_UNLOCK = "Unlock iPhone"
MSG_TRUST = "Trust this computer (unlock the iPhone, open Apple Devices and tap Trust)"
MSG_LOST = "USB connection lost"


@dataclass(frozen=True)
class DeviceCandidate:
    udid: str
    name: str
    product_type: str
    model: str
    ios_version: str
    status: str            # "Connected" or a friendly error
    ok: bool


def friendly_error(e: BaseException) -> str:
    if isinstance(e, (pmd_exc.ConnectionFailedToUsbmuxdError, ConnectionRefusedError)):
        return MSG_NO_USBMUX
    if isinstance(e, (pmd_exc.NoDeviceConnectedError, pmd_exc.DeviceNotFoundError)):
        return MSG_NO_DEVICE
    if isinstance(e, (pmd_exc.PasswordRequiredError, pmd_exc.PasscodeRequiredError)):
        return MSG_UNLOCK
    if isinstance(e, (pmd_exc.PairingDialogResponsePendingError, pmd_exc.NotPairedError,
                      pmd_exc.InvalidHostIDError, pmd_exc.UserDeniedPairingError,
                      pmd_exc.NotTrustedError, pmd_exc.FatalPairingError, pmd_exc.PairingError)):
        return MSG_TRUST
    if isinstance(e, (pmd_exc.ConnectionTerminatedError, ConnectionError, asyncio.IncompleteReadError,
                      pmd_exc.MuxException, OSError, EOFError, TimeoutError)):
        return MSG_LOST
    return f"Device error ({type(e).__name__})"


async def usbmux_available() -> bool:
    try:
        await list_devices()
        return True
    except (pmd_exc.ConnectionFailedToUsbmuxdError, ConnectionError, OSError):
        return False


async def list_usb_udids() -> list[str]:
    """UDIDs of USB-attached devices.  Wi-Fi/network entries are ignored."""
    seen: list[str] = []
    for d in await list_devices():
        if d.is_usb and d.serial not in seen:
            seen.append(d.serial)
    return seen


async def open_lockdown(udid: str) -> Any:
    """Open lockdown using the existing trust relationship (Apple Devices' pair record).

    autopair=False: this application never initiates pairing, so it never causes the
    iPhone to store a new pairing record.  An untrusted phone is reported instead.
    """
    ld = await create_using_usbmux(serial=udid, connection_type="USB", autopair=False)
    if not getattr(ld, "paired", False):
        await ld.close()
        raise pmd_exc.NotPairedError()
    return ld


def model_name(lockdown: Any) -> str:
    try:
        return lockdown.display_name or lockdown.product_type or "iPhone"
    except Exception:
        return "iPhone"


async def describe_devices() -> tuple[bool, list[DeviceCandidate]]:
    """Return (usbmux_ok, USB-attached devices with name/model/iOS where readable)."""
    try:
        udids = await list_usb_udids()
    except (pmd_exc.ConnectionFailedToUsbmuxdError, ConnectionError, OSError) as e:
        log.warning("usbmux unavailable: %s", type(e).__name__)
        return False, []
    result: list[DeviceCandidate] = []
    for udid in udids:
        try:
            ld = await open_lockdown(udid)
            try:
                v = ld.short_info
                result.append(DeviceCandidate(
                    udid=udid, name=v.get("DeviceName") or "iPhone", product_type=v.get("ProductType") or "",
                    model=model_name(ld), ios_version=v.get("ProductVersion") or "?", status="Connected", ok=True))
            finally:
                await ld.close()
        except Exception as e:  # report each device's own state instead of failing the scan
            log.info("device lockdown failed: %s", type(e).__name__)
            result.append(DeviceCandidate(udid=udid, name="iPhone", product_type="", model="iPhone",
                                          ios_version="?", status=friendly_error(e), ok=False))
    return True, result


async def wait_for_device(udid: str, cancel: "asyncio.Event | Any", poll_s: float = 2.0,
                          timeout_s: float | None = None) -> bool:
    """Poll usbmux until the same UDID is attached over USB again."""
    loop = asyncio.get_running_loop()
    deadline = None if timeout_s is None else loop.time() + timeout_s
    while not cancel.is_set():
        try:
            if udid in await list_usb_udids():
                return True
        except Exception:
            pass
        if deadline is not None and loop.time() > deadline:
            return False
        await asyncio.sleep(poll_s)
    return False
