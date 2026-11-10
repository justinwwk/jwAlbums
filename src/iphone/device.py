# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""ReadOnlyIPhone: the application's single entry point for iPhone filesystem access.

Only read operations are exposed.  Every device path is validated against the
allowed media roots (DCIM/, PhotoData/) before it reaches AFC, and library errors
are translated into three categories the rest of the app reasons about:

  RemoteFileNotFound / RemoteFileError  -> a problem with one file
  DeviceConnectionLost                  -> the USB session is gone; pause and reconnect
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, TypeVar

from pymobiledevice3 import exceptions as pmd_exc

from export.path_safety import validate_device_path
from iphone import connection
from iphone.readonly_afc import MAX_READ, ReadOnlyAfc

log = logging.getLogger(__name__)
T = TypeVar("T")

OP_TIMEOUT = 60.0

# Failures that mean the USB/usbmux/AFC session itself is gone.
_TRANSPORT_ERRORS = (ConnectionError, EOFError, TimeoutError, asyncio.IncompleteReadError,
                     pmd_exc.ConnectionTerminatedError, pmd_exc.MuxException, pmd_exc.NoDeviceConnectedError,
                     pmd_exc.DeviceNotFoundError)


def _posix(dt: Any) -> float:
    """pymobiledevice3 returns naive local datetimes; Windows rejects some near/pre-epoch ones."""
    try:
        return dt.timestamp()
    except (OSError, OverflowError, ValueError):
        try:
            return (dt - datetime(1970, 1, 1)).total_seconds() + time.timezone
        except Exception:
            return 0.0


class DeviceError(Exception):
    pass


class DeviceConnectionLost(DeviceError):
    pass


class RemoteFileError(DeviceError):
    pass


class RemoteFileNotFound(RemoteFileError):
    pass


@dataclass(frozen=True)
class DeviceInfo:
    udid: str
    name: str
    product_type: str
    model: str
    ios_version: str
    build: str


@dataclass(frozen=True)
class RemoteStat:
    size: int
    mtime: float          # POSIX seconds
    birthtime: float
    is_dir: bool


class ReadOnlyIPhone:
    def __init__(self, udid: str) -> None:
        self.udid = udid
        self._lockdown: Any = None
        self._afc: ReadOnlyAfc | None = None
        self._info: DeviceInfo | None = None

    # ------------------------------------------------------------------ session

    async def connect(self) -> None:
        await self.disconnect()
        try:
            self._lockdown = await connection.open_lockdown(self.udid)
            v = self._lockdown.short_info
            self._info = DeviceInfo(
                udid=self.udid, name=v.get("DeviceName") or "iPhone", product_type=v.get("ProductType") or "",
                model=connection.model_name(self._lockdown), ios_version=v.get("ProductVersion") or "?",
                build=v.get("BuildVersion") or "")
            self._afc = ReadOnlyAfc(self._lockdown)
            await asyncio.wait_for(self._afc.open(), OP_TIMEOUT)
        except Exception as e:
            await self.disconnect()
            raise DeviceConnectionLost(connection.friendly_error(e)) from e
        log.info("connected: %s iOS %s", self._info.model, self._info.ios_version)

    async def disconnect(self) -> None:
        afc, ld = self._afc, self._lockdown
        self._afc = self._lockdown = None
        for closer in ((afc.close if afc else None), (ld.close if ld else None)):
            if closer is None:
                continue
            try:
                await asyncio.wait_for(closer(), 5)
            except Exception:
                pass

    @property
    def connected(self) -> bool:
        return self._afc is not None

    def device_info(self) -> DeviceInfo:
        if self._info is None:
            raise DeviceConnectionLost("not connected")
        return self._info

    # ------------------------------------------------------------------ reads

    async def _call(self, aw: Awaitable[T], path: str = "") -> T:
        try:
            return await asyncio.wait_for(aw, OP_TIMEOUT)
        except pmd_exc.AfcFileNotFoundError as e:
            raise RemoteFileNotFound("not found on iPhone") from e
        except pmd_exc.AfcException as e:
            raise RemoteFileError(f"AFC error {getattr(e, 'status', '')}".strip()) from e
        except (DeviceError, asyncio.CancelledError):
            raise
        except _TRANSPORT_ERRORS as e:
            await self._lost(e)
        except Exception as e:
            # Not obviously a transport failure (e.g. unparseable metadata for one file).
            # Ask the device directly whether the session is still alive.
            if await self._raw_alive():
                log.warning("unreadable device metadata (%s)", type(e).__name__)
                raise RemoteFileError(f"unreadable metadata ({type(e).__name__})") from e
            await self._lost(e)
        raise AssertionError("unreachable")

    async def _lost(self, e: BaseException) -> None:
        log.warning("device connection lost (%s)", type(e).__name__)
        await self.disconnect()
        raise DeviceConnectionLost(connection.MSG_LOST) from e

    async def _raw_alive(self) -> bool:
        if self._afc is None:
            return False
        try:
            await asyncio.wait_for(self._afc.stat("DCIM"), 10)
            return True
        except pmd_exc.AfcException:
            return True
        except Exception:
            return False

    def _require(self) -> ReadOnlyAfc:
        if self._afc is None:
            raise DeviceConnectionLost("not connected")
        return self._afc

    async def exists(self, path: str) -> bool:
        p = validate_device_path(path)
        try:
            await self._call(self._require().stat(p), p)
            return True
        except RemoteFileNotFound:
            return False

    async def stat(self, path: str) -> RemoteStat:
        p = validate_device_path(path)
        st = await self._call(self._require().stat(p), p)
        try:
            return RemoteStat(size=int(st["st_size"]), mtime=_posix(st["st_mtime"]),
                              birthtime=_posix(st["st_birthtime"]), is_dir=st.get("st_ifmt") == "S_IFDIR")
        except (KeyError, TypeError, ValueError) as e:
            raise RemoteFileError(f"unreadable metadata ({type(e).__name__})") from e

    async def listdir(self, path: str) -> list[str]:
        p = validate_device_path(path)
        names = await self._call(self._require().listdir(p), p)
        return [n for n in names if n not in (".", "..")]

    async def open_read(self, path: str) -> int:
        p = validate_device_path(path)
        return await self._call(self._require().fopen_read(p), p)

    async def read(self, handle: int, size: int) -> bytes:
        return await self._call(self._require().fread(handle, min(size, MAX_READ)))

    async def seek(self, handle: int, offset: int) -> None:
        await self._call(self._require().fseek(handle, offset))

    async def close(self, handle: int) -> None:
        if self._afc is None:
            return
        try:
            await self._call(self._afc.fclose(handle))
        except DeviceError:
            pass

    async def ping(self) -> bool:
        """Cheap liveness check used to tell a bad file from a dead connection."""
        try:
            await self.stat("DCIM")
            return True
        except DeviceConnectionLost:
            return False
        except DeviceError:
            return True
