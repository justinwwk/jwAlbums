# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""The ONLY module in this application that imports pymobiledevice3's AfcService.

AfcService supports writes, deletes, renames and more.  This adapter forwards only
read operations; nothing else in the application can reach the underlying service.
tests/test_readonly_contract.py enforces both properties.
"""

from __future__ import annotations

from typing import Any

from pymobiledevice3.services.afc import AfcService

# The AFC open mode used for every file handle: AFC_FOPEN_RDONLY.
_READ_ONLY_MODE = "r"
# AFC returns at most this much per read request.
MAX_READ = 4 * 1024 * 1024


class ReadOnlyAfc:
    """Minimal read-only facade around an AfcService instance."""

    __slots__ = ("__afc",)

    def __init__(self, lockdown: Any) -> None:
        self.__afc = AfcService(lockdown)

    async def open(self) -> None:
        await self.__afc.__aenter__()

    async def close(self) -> None:
        await self.__afc.aclose()

    async def stat(self, path: str) -> dict[str, Any]:
        return await self.__afc.stat(path)

    async def listdir(self, path: str) -> list[str]:
        return await self.__afc.listdir(path)

    async def exists(self, path: str) -> bool:
        return await self.__afc.exists(path)

    async def fopen_read(self, path: str) -> int:
        return await self.__afc.fopen(path, _READ_ONLY_MODE)

    async def fread(self, handle: int, size: int) -> bytes:
        # Never request more than one AFC packet: pymobiledevice3's fread does not
        # stop on short reads, so callers always pass size <= remaining bytes.
        return await self.__afc.fread(handle, min(size, MAX_READ))

    async def fseek(self, handle: int, offset: int) -> None:
        await self.__afc.fseek(handle, offset)

    async def fclose(self, handle: int) -> None:
        await self.__afc.fclose(handle)
