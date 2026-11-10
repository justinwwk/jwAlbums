# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Optional read-only throughput benchmark ("Measured read speed").

Reads up to ~128 MB from one existing large media file and discards the bytes.
This measures real AFC read throughput; it is NOT the cable or link speed.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Callable

from app.cancel import Cancelled, CancelToken
from iphone.device import ReadOnlyIPhone, RemoteFileError

log = logging.getLogger(__name__)

TARGET_BYTES = 128 * 1024 * 1024
MIN_FILE_BYTES = 32 * 1024 * 1024
TIME_LIMIT_S = 20.0
CHUNK = 4 * 1024 * 1024
MAX_CANDIDATE_STATS = 25


@dataclass(frozen=True)
class BenchmarkOutcome:
    mb_per_s: float | None
    bytes_read: int
    seconds: float
    note: str


async def find_candidate(device: ReadOnlyIPhone, hints: list[str]) -> tuple[str, int] | None:
    """Find a large media file cheaply: index hints first, then videos in the newest DCIM folder."""
    stats = 0
    for path in hints:
        if stats >= MAX_CANDIDATE_STATS:
            return None
        stats += 1
        try:
            st = await device.stat(path)
        except RemoteFileError:
            continue
        if not st.is_dir and st.size >= MIN_FILE_BYTES:
            return path, st.size
    try:
        dirs = sorted(d for d in await device.listdir("DCIM") if d[:3].isdigit())
    except RemoteFileError:
        return None
    for d in reversed(dirs[-3:]):
        try:
            names = await device.listdir(f"DCIM/{d}")
        except RemoteFileError:
            continue
        for n in sorted((n for n in names if os.path.splitext(n)[1].upper() in (".MOV", ".MP4")), reverse=True):
            if stats >= MAX_CANDIDATE_STATS:
                return None
            stats += 1
            try:
                st = await device.stat(f"DCIM/{d}/{n}")
            except RemoteFileError:
                continue
            if st.size >= MIN_FILE_BYTES:
                return f"DCIM/{d}/{n}", st.size
    return None


async def run_benchmark(device: ReadOnlyIPhone, hints: list[str], cancel: CancelToken,
                        on_progress: Callable[[int, int], None] = lambda d, t: None) -> BenchmarkOutcome:
    cand = await find_candidate(device, hints)
    if cand is None:
        return BenchmarkOutcome(None, 0, 0.0, "Skipped: no suitably large media file found cheaply")
    path, size = cand
    target = min(TARGET_BYTES, size)
    handle = await device.open_read(path)
    read = 0
    t0 = time.perf_counter()
    try:
        while read < target:
            if cancel.is_set():
                raise Cancelled()
            if time.perf_counter() - t0 > TIME_LIMIT_S:
                break
            data = await device.read(handle, min(CHUNK, target - read))
            if not data:
                break
            read += len(data)
            del data          # benchmark bytes are discarded immediately
            on_progress(read, target)
    finally:
        await device.close(handle)
    secs = time.perf_counter() - t0
    if read == 0 or secs <= 0:
        return BenchmarkOutcome(None, read, secs, "Benchmark read no data")
    mbps = read / 1e6 / secs
    log.info("benchmark: %.1f MB/s (%d bytes in %.2fs)", mbps, read, secs)
    return BenchmarkOutcome(mbps, read, secs, f"Read {read / 1e6:.0f} MB in {secs:.1f} s")
