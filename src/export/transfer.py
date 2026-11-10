# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Streaming, resumable, read-only copy of one device file to a local .part file."""

from __future__ import annotations

import os
from typing import Callable, Protocol

from app.cancel import Cancelled, CancelToken
from export import fsutil
from iphone.device import RemoteFileError

CHUNK = 4 * 1024 * 1024
# Data is fsynced at least this often; on_synced reports the durable offset so a resume
# after a crash or power loss never trusts bytes that may not have reached the disk.
SYNC_EVERY = 64 * 1024 * 1024


class ReadableDevice(Protocol):
    async def open_read(self, path: str) -> int: ...
    async def read(self, handle: int, size: int) -> bytes: ...
    async def seek(self, handle: int, offset: int) -> None: ...
    async def close(self, handle: int) -> None: ...


async def stream_copy(device: ReadableDevice, remote_path: str, part_path: str, remote_size: int,
                      offset: int, on_bytes: Callable[[int], None], cancel: CancelToken,
                      chunk: int = CHUNK, on_synced: Callable[[int], None] | None = None) -> int:
    """Copy remote[offset:remote_size] onto part_path.

    offset == 0: part_path must not exist; it is created exclusively (never follows or
    overwrites an existing file or link).  offset > 0: part_path holds at least `offset`
    valid bytes and is truncated to exactly `offset` first.

    Never holds more than one chunk in memory.  On cancellation or error the .part
    file is flushed, fsynced and left in place for a later resume.
    """
    handle = await device.open_read(remote_path)
    try:
        if offset:
            await device.seek(handle, offset)
        f = await fsutil.retry(open, part_path, "r+b" if offset else "xb")
        with f:
            if offset:
                f.truncate(offset)
                f.seek(offset)
            pos = offset
            unsynced = 0
            try:
                while pos < remote_size:
                    if cancel.is_set():
                        raise Cancelled()
                    data = await device.read(handle, min(chunk, remote_size - pos))
                    if not data:
                        raise RemoteFileError("source ended before its reported size")
                    f.write(data)
                    pos += len(data)
                    unsynced += len(data)
                    on_bytes(len(data))
                    if unsynced >= SYNC_EVERY:
                        f.flush()
                        os.fsync(f.fileno())
                        unsynced = 0
                        if on_synced:
                            on_synced(pos)
            finally:
                f.flush()
                os.fsync(f.fileno())
                if on_synced:
                    on_synced(pos)
    finally:
        await device.close(handle)
    actual = os.path.getsize(part_path)
    if actual != remote_size:
        raise RemoteFileError(f"size mismatch after copy ({actual} != {remote_size})")
    return actual
