# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Retry local file operations that hit transient Windows locks.

Right after a large file is written, antivirus real-time scanning or the search indexer
often holds it open for a moment.  Deleting, renaming or re-creating it then fails with
ERROR_SHARING_VIOLATION (32), ERROR_LOCK_VIOLATION (33), or ERROR_ACCESS_DENIED (5,
also returned while a deleted file is still "delete pending").  These clear up by
themselves, so such operations are retried briefly instead of failing the task.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any, Callable, TypeVar

T = TypeVar("T")

TRANSIENT_WINERRORS = {5, 32, 33}
TIMEOUT_S = 15.0


def _transient(e: OSError) -> bool:
    return getattr(e, "winerror", None) in TRANSIENT_WINERRORS


def retry_sync(fn: Callable[..., T], *args: Any, timeout: float | None = None, **kwargs: Any) -> T:
    deadline = time.monotonic() + (TIMEOUT_S if timeout is None else timeout)
    delay = 0.05
    while True:
        try:
            return fn(*args, **kwargs)
        except OSError as e:
            if not _transient(e) or time.monotonic() >= deadline:
                raise
        time.sleep(delay)
        delay = min(delay * 2, 1.0)


async def retry(fn: Callable[..., T], *args: Any, timeout: float | None = None, **kwargs: Any) -> T:
    """Async variant: waits without blocking the event loop, so cancellation stays responsive."""
    deadline = time.monotonic() + (TIMEOUT_S if timeout is None else timeout)
    delay = 0.05
    while True:
        try:
            return fn(*args, **kwargs)
        except OSError as e:
            if not _transient(e) or time.monotonic() >= deadline:
                raise
        await asyncio.sleep(delay)
        delay = min(delay * 2, 1.0)


def set_file_times(path: str, ts: float) -> None:
    """Set modified, accessed and (on Windows) created time of a regular file to ts."""
    os.utime(path, (ts, ts))
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.SetFileTime.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    file_write_attributes, open_existing, flag_open_reparse_point = 0x100, 3, 0x200000
    h = k32.CreateFileW(path, file_write_attributes, 0x7, None, open_existing, flag_open_reparse_point, None)
    if h is None or h == ctypes.c_void_p(-1).value:
        err = ctypes.get_last_error()
        raise OSError(None, ctypes.FormatError(err), path, err)
    try:
        ft = ctypes.c_uint64(int(round((ts + 11644473600) * 10_000_000)))   # 100 ns ticks since 1601
        if not k32.SetFileTime(h, ctypes.byref(ft), None, None):
            err = ctypes.get_last_error()
            raise OSError(None, ctypes.FormatError(err), path, err)
    finally:
        k32.CloseHandle(h)
