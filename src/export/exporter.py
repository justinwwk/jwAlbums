# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Run an ExportPlan: copy files, resume partials, isolate per-file errors, survive disconnects.

Ownership rules (what keeps user files and other exports safe):
  * A destination name belongs to exactly one (device, album, asset, role) in the manifest.
  * A name that is not ours is never written, resumed or deleted - including "<name>.part".
  * An existing file we did not record is only adopted as "already exported" after its
    size and its head and tail bytes match the iPhone original; otherwise a new name is used.
  * A .part is resumed only from the offset recorded as fsynced, and only when the source
    path, size and mtime recorded for it still match.
"""

from __future__ import annotations

import errno
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field, replace
from typing import Awaitable, Callable

from app.cancel import Cancelled, CancelToken
from export import fsutil
from export import manifest as mf
from export.manifest import Manifest
from export.path_safety import (UnsafePathError, assert_within_root, is_regular_file_or_absent, numbered_name,
                                safe_join, sanitize_component)
from export.planner import ROLE_PRIMARY, ExportOptions, ExportPlan, PlannedAsset, PlannedFile
from export.resume import PartAction, decide_part
from export.transfer import stream_copy
from iphone.device import DeviceConnectionLost, RemoteFileError

log = logging.getLogger(__name__)

MAX_DISCONNECTS_WITHOUT_PROGRESS = 5
MIN_VALID_TS = 315532800          # 1980-01-01
ERROR_DISK_FULL = 112             # Windows ERROR_DISK_FULL
SAMPLE = 1024 * 1024              # bytes compared at head and tail to verify an unrecorded file
MAX_PATH = 259                    # without long-path support Windows rejects longer paths
MAX_DIR_PATH = 247                # CreateDirectory limit (MAX_PATH - 12)
TOO_LONG = "destination path is too long for Windows; choose a shorter destination folder"


class DiskFullError(Exception):
    pass


@dataclass
class ExportProgress:
    album: str
    current: str
    done_items: int
    total_items: int
    done_bytes: int
    total_bytes: int
    bytes_per_s: float


@dataclass
class ExportSummary:
    copied: int = 0
    skipped: int = 0
    failures: list[str] = field(default_factory=list)
    bytes_copied: int = 0
    cancelled: bool = False


ReconnectFn = Callable[[], Awaitable[None]]


class _Speed:
    """Throughput over a sliding window of a few seconds."""

    def __init__(self, window: float = 5.0) -> None:
        self.window = window
        self.samples: list[tuple[float, int]] = []

    def add(self, n: int) -> None:
        now = time.monotonic()
        self.samples.append((now, n))
        cutoff = now - self.window
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.pop(0)

    def rate(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        span = self.samples[-1][0] - self.samples[0][0]
        return sum(n for _, n in self.samples[1:]) / span if span > 0 else 0.0


def _dest_parts(pa: PlannedAsset, f: PlannedFile) -> tuple[str, ...]:
    """Destination folder of one file, relative to the export root."""
    return pa.album_dir + f.subdir


def _regular_size(path: str) -> int | None:
    """Size of a regular file, None if absent, -1 for anything else (link, junction, dir)."""
    if not os.path.lexists(path):
        return None
    if not is_regular_file_or_absent(path):
        return -1
    return os.path.getsize(path)


class Exporter:
    def __init__(self, device, manifest: Manifest, root: str, device_key: str, options: ExportOptions,
                 on_progress: Callable[[ExportProgress], None], on_status: Callable[[str], None],
                 cancel: CancelToken, reconnect: ReconnectFn) -> None:
        self.device = device
        self.manifest = manifest
        self.root = os.path.realpath(root)
        self.device_key = device_key
        self.options = options
        self.on_progress = on_progress
        self.on_status = on_status
        self.cancel = cancel
        self.reconnect = reconnect
        self._in_use = manifest.dest_paths_in_use()
        self._speed = _Speed()
        self._last_report = 0.0
        self._done_items = 0
        self._done_bytes = 0
        self._total_items = 0
        self._total_bytes = 0
        self._attempt_read = 0
        self._album = ""
        self._current = ""

    # ------------------------------------------------------------------ progress

    def _report(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_report < 0.2:
            return
        self._last_report = now
        self.on_progress(ExportProgress(self._album, self._current, self._done_items, self._total_items,
                                        self._done_bytes, self._total_bytes, self._speed.rate()))

    def _on_bytes(self, n: int) -> None:
        self._done_bytes += n
        self._speed.add(n)
        self._report()

    def _on_read(self, n: int) -> None:
        self._attempt_read += n
        self._on_bytes(n)

    # ------------------------------------------------------------------ main loop

    async def run(self, plan: ExportPlan) -> ExportSummary:
        summary = ExportSummary(failures=list(plan.unresolved))
        self._total_items = plan.total_files
        self._total_bytes = plan.total_bytes
        try:
            for pa in plan.assets:
                await self._export_asset(pa, summary)
        except Cancelled:
            summary.cancelled = True
            log.info("export cancelled")
        self._report(force=True)
        log.info("export finished: copied=%d skipped=%d failed=%d cancelled=%s",
                 summary.copied, summary.skipped, len(summary.failures), summary.cancelled)
        return summary

    async def _export_asset(self, pa: PlannedAsset, summary: ExportSummary) -> None:
        self._album = pa.album_label
        primary_stem: str | None = None
        for f in pa.files:
            self.cancel.raise_if_cancelled()
            if f.error:
                self._fail(pa, f, f.error, summary)
                continue
            self._current = f.remote_name
            desired = sanitize_component(f.remote_name)
            if f.role != ROLE_PRIMARY and primary_stem:
                desired = sanitize_component(primary_stem + os.path.splitext(desired)[1])
            rel = await self._export_with_retries(pa, f, desired, summary)
            if rel and f.role == ROLE_PRIMARY:
                primary_stem = os.path.splitext(os.path.basename(rel))[0]
            self._done_items += 1
            self._report(force=True)

    async def _export_with_retries(self, pa: PlannedAsset, f: PlannedFile, desired: str,
                                   summary: ExportSummary) -> str | None:
        """Export one file.  Returns the relative destination, or None if it failed."""
        disconnects = 0
        need_restat = False
        while True:
            self._attempt_read = 0
            try:
                if need_restat:
                    need_restat = False
                    f = await self._restat(f)
                    if f.error:
                        self._fail(pa, f, f.error, summary)
                        return None
                rel, copied = await self._export_file(pa, f, desired)
                if copied:
                    summary.copied += 1
                    summary.bytes_copied += f.size or 0
                else:
                    summary.skipped += 1
                return rel
            except (DeviceConnectionLost, RemoteFileError) as e:
                if isinstance(e, RemoteFileError) and await self.device.ping():
                    self._fail(pa, f, str(e) or type(e).__name__, summary)
                    return None
                # Connection lost.  Only consecutive losses without progress count toward giving up.
                disconnects = 1 if self._attempt_read > 0 else disconnects + 1
                if disconnects > MAX_DISCONNECTS_WITHOUT_PROGRESS:
                    self._fail(pa, f, "connection repeatedly lost while reading this file", summary)
                    return None
                await self._wait_reconnect()
                need_restat = True
            except UnsafePathError as e:
                self._fail(pa, f, str(e), summary)
                return None
            except sqlite3.OperationalError as e:
                if "full" in str(e).lower():
                    raise DiskFullError("The destination disk is full.") from e
                raise
            except OSError as e:
                if e.errno == errno.ENOSPC or getattr(e, "winerror", None) == ERROR_DISK_FULL:
                    raise DiskFullError("The destination disk is full.") from e
                self._fail(pa, f, f"local write failed ({e.strerror or type(e).__name__})", summary)
                return None

    def _fail(self, pa: PlannedAsset, f: PlannedFile, reason: str, summary: ExportSummary) -> None:
        log.warning("asset %d (%s) failed: %s", pa.asset_id, f.role, reason)
        summary.failures.append(f"{pa.album_label}/{f.remote_name}: {reason}")
        self.manifest.mark_failed(self.device_key, pa.album_id, pa.album_label, pa.asset_id, f.role,
                                  f.remote_path, reason)

    async def _wait_reconnect(self) -> None:
        self.on_status("iPhone disconnected")
        log.warning("device connection lost; waiting to reconnect")
        await self.reconnect()
        self.on_status("Reconnected; resuming")
        log.info("device reconnected; resuming")

    async def _restat(self, f: PlannedFile) -> PlannedFile:
        """Fresh stat after a reconnect.  DeviceConnectionLost propagates to the retry loop."""
        try:
            st = await self.device.stat(f.remote_path)
        except DeviceConnectionLost:
            raise
        except RemoteFileError as e:
            return replace(f, error=str(e) or "not readable")
        return replace(f, size=st.size, mtime=st.mtime, error=None)

    # ------------------------------------------------------------------ ownership / naming

    def _key(self, pa: PlannedAsset, f: PlannedFile) -> tuple[str, int, int, str]:
        return (self.device_key, pa.album_id, pa.asset_id, f.role)

    def _owner(self, rel: str):
        return self._in_use.get(os.path.normcase(rel))

    def _claim(self, rel: str, key) -> None:
        self._in_use[os.path.normcase(rel)] = key

    async def _sample_matches(self, local: str, f: PlannedFile) -> bool:
        """Compare head and tail bytes of a local file with the iPhone original (read-only)."""
        assert f.size is not None
        if os.path.getsize(local) != f.size:
            return False
        ranges = [(0, f.size)] if f.size <= 2 * SAMPLE else [(0, SAMPLE), (f.size - SAMPLE, SAMPLE)]
        handle = await self.device.open_read(f.remote_path)
        try:
            with open(local, "rb") as lf:
                for off, n in ranges:
                    await self.device.seek(handle, off)
                    remote = bytearray()
                    while len(remote) < n:
                        chunk = await self.device.read(handle, n - len(remote))
                        if not chunk:
                            return False
                        remote += chunk
                    lf.seek(off)
                    if lf.read(n) != bytes(remote):
                        return False
        finally:
            await self.device.close(handle)
        return True

    async def _allocate(self, pa: PlannedAsset, f: PlannedFile, desired: str) -> tuple[str, bool]:
        """Pick a name we may write.  Returns (rel, adopt_existing)."""
        key = self._key(pa, f)
        parts = _dest_parts(pa, f)
        n = 1
        while True:
            name = desired if n == 1 else numbered_name(desired, n)
            n += 1
            rel = os.path.join(*parts, name)
            owner = self._owner(rel)
            if owner is not None and owner != key:
                continue
            final = safe_join(self.root, *parts, name)
            if os.path.lexists(final + ".part"):
                continue          # somebody else's partial file: never touch it
            if os.path.lexists(final):
                if (self.options.skip_existing and _regular_size(final) == f.size
                        and await self._sample_matches(final, f)):
                    self._claim(rel, key)
                    return rel, True
                continue
            self._claim(rel, key)
            return rel, False

    # ------------------------------------------------------------------ one file

    async def _export_file(self, pa: PlannedAsset, f: PlannedFile, desired: str) -> tuple[str, bool]:
        """Returns (relative destination, copied?)."""
        assert f.size is not None
        parts = _dest_parts(pa, f)
        dest_dir = safe_join(self.root, *parts)
        if len(dest_dir) > MAX_DIR_PATH:
            raise UnsafePathError(TOO_LONG)
        os.makedirs(dest_dir, exist_ok=True)
        assert_within_root(dest_dir, self.root)
        key = self._key(pa, f)

        entry = self.manifest.get(self.device_key, pa.album_id, pa.asset_id, f.role)
        if entry and entry.dest_path and f.subdir:
            entry = self._move_into_subfolder(pa, f, entry)
        rel: str | None = None
        replace_own = False
        if entry and entry.dest_path and self._owner(entry.dest_path) in (None, key) and \
                os.path.normcase(os.path.dirname(entry.dest_path)) == os.path.normcase(os.path.join(*parts)):
            rel = entry.dest_path
            self._claim(rel, key)
            final = safe_join(self.root, *parts, os.path.basename(rel))
            size = _regular_size(final)
            if size is not None:
                same_source = (entry.remote_path == f.remote_path and entry.remote_size == f.size
                               and entry.remote_mtime == f.mtime)
                # A file we finished and that still has the size we wrote is our untouched copy.
                ours_untouched = entry.state == mf.DONE and size == entry.remote_size
                if self.options.skip_existing and (
                        (ours_untouched and same_source)
                        or (size == f.size and await self._sample_matches(final, f))):
                    return self._record_skip(pa, f, rel), False
                if ours_untouched and not same_source:
                    # The photo was edited (or re-edited) on the iPhone after we exported it:
                    # update our own copy in place rather than leaving a stale one beside a new name.
                    log.info("asset %d (%s): source changed since export; updating", pa.asset_id, f.role)
                    replace_own = True
                elif self.options.skip_existing:
                    rel = None              # our name is occupied by something else now
                elif size == f.size and await self._sample_matches(final, f):
                    # "skip" is off: re-copy our own file in place.  Only when its bytes still
                    # match the original, so an in-place edit by the user is never overwritten.
                    # This also resumes an interrupted earlier re-copy (state COPYING).
                    replace_own = True
                else:
                    rel = None
        if rel is None:
            entry = None
            rel, adopt = await self._allocate(pa, f, desired)
            if adopt:
                return self._record_skip(pa, f, rel), False

        final = safe_join(self.root, *parts, os.path.basename(rel))
        part = final + ".part"
        if len(part) > MAX_PATH:
            raise UnsafePathError(TOO_LONG)
        if not is_regular_file_or_absent(part) or not is_regular_file_or_absent(final):
            raise UnsafePathError("destination is a link or not a regular file")
        assert_within_root(part, self.root)

        part_size = os.path.getsize(part) if os.path.lexists(part) else None
        recorded = None
        if entry and entry.dest_path == rel and entry.remote_path == f.remote_path \
                and entry.remote_size is not None and entry.remote_mtime is not None:
            recorded = (entry.remote_size, entry.remote_mtime)
        decision = decide_part(part_size, f.size, recorded, (f.size, f.mtime or 0.0))
        offset = decision.offset
        if decision.action is PartAction.RESUME:
            # Only bytes recorded as fsynced by an in-progress copy are trusted (protects
            # against power loss).  A DONE entry's byte count says nothing about a .part.
            trusted = (entry.bytes_transferred or 0) if entry and entry.state in (mf.COPYING, mf.FAILED) else 0
            offset = min(offset, trusted)
            log.info("asset %d (%s): resuming at byte %d", pa.asset_id, f.role, offset)
        elif decision.action is PartAction.RESTART:
            log.info("asset %d (%s): restarting partial file (%s)", pa.asset_id, f.role, decision.reason)
        if offset == 0 and part_size is not None:
            await fsutil.retry(os.unlink, part)   # ours: the name is owned by this key in the manifest

        self.manifest.upsert(self.device_key, pa.album_id, pa.album_label, pa.asset_id, f.role,
                             remote_path=f.remote_path, remote_size=f.size, remote_mtime=f.mtime,
                             dest_path=rel, state=mf.COPYING, bytes_transferred=offset)

        def on_synced(pos: int) -> None:
            self.manifest.set_state(self.device_key, pa.album_id, pa.asset_id, f.role, mf.COPYING,
                                    bytes_transferred=pos)

        if offset:
            self._on_bytes(offset)
        start_bytes = self._done_bytes - offset
        try:
            await stream_copy(self.device, f.remote_path, part, f.size, offset, self._on_read, self.cancel,
                              on_synced=on_synced)
        except BaseException:
            # Bytes still in the .part are counted again when the file is resumed.
            self._done_bytes = start_bytes
            raise

        if replace_own:
            await fsutil.retry(os.replace, part, final)
        else:
            await fsutil.retry(os.rename, part, final)   # fails rather than overwriting if something appeared meanwhile
        # Windows "Date modified" and "Date created" = when the photo was taken (or, if the
        # library has no valid capture date, the file's date on the iPhone).
        ts = pa.created_ts if (pa.created_ts and MIN_VALID_TS < pa.created_ts < time.time() + 86400) \
            else f.mtime
        if ts:
            await fsutil.retry(fsutil.set_file_times, final, ts)
        self.manifest.set_state(self.device_key, pa.album_id, pa.asset_id, f.role, mf.DONE,
                                bytes_transferred=f.size)
        return rel, True

    def _move_into_subfolder(self, pa: PlannedAsset, f: PlannedFile, entry: mf.ManifestEntry) -> mf.ManifestEntry:
        """Live clips exported before they got their own folder sat beside the photos.

        Move our own finished, unchanged copy into the subfolder (same volume rename, so the
        dates are kept).  Anything that is not clearly ours and untouched is left where it is.
        """
        key = self._key(pa, f)
        old_rel = entry.dest_path
        assert old_rel
        if os.path.normcase(os.path.dirname(old_rel)) != os.path.normcase(os.path.join(*pa.album_dir)) \
                or self._owner(old_rel) not in (None, key):
            return entry
        name = os.path.basename(old_rel)
        old = safe_join(self.root, *pa.album_dir, name)
        if entry.state != mf.DONE:
            # Unfinished copy at the old place: drop our .part, it restarts in the subfolder.
            if os.path.lexists(old + ".part") and is_regular_file_or_absent(old + ".part"):
                fsutil.retry_sync(os.unlink, old + ".part")
            return entry
        if _regular_size(old) != entry.remote_size:
            return entry                       # gone, or changed by the user: leave it alone
        parts = _dest_parts(pa, f)
        new_rel = os.path.join(*parts, name)
        new_dir = safe_join(self.root, *parts)
        new = safe_join(self.root, *parts, name)
        if self._owner(new_rel) not in (None, key) or os.path.lexists(new) or os.path.lexists(new + ".part") \
                or len(new) > MAX_PATH:
            return entry
        os.makedirs(new_dir, exist_ok=True)
        assert_within_root(new_dir, self.root)
        fsutil.retry_sync(os.rename, old, new)          # fails rather than overwriting
        self._in_use.pop(os.path.normcase(old_rel), None)
        self._claim(new_rel, key)
        self.manifest.upsert(self.device_key, pa.album_id, pa.album_label, pa.asset_id, f.role,
                             remote_path=entry.remote_path, remote_size=entry.remote_size,
                             remote_mtime=entry.remote_mtime, dest_path=new_rel, state=mf.DONE,
                             bytes_transferred=entry.bytes_transferred)
        log.info("asset %d (%s): moved into %s", pa.asset_id, f.role, "/".join(f.subdir))
        return self.manifest.get(self.device_key, pa.album_id, pa.asset_id, f.role) or entry

    def _record_skip(self, pa: PlannedAsset, f: PlannedFile, rel: str) -> str:
        # rel is owned by this key, so a leftover .part beside it is ours (e.g. an
        # interrupted re-copy) and no longer needed.
        part = safe_join(self.root, *_dest_parts(pa, f), os.path.basename(rel)) + ".part"
        if os.path.lexists(part) and is_regular_file_or_absent(part):
            fsutil.retry_sync(os.unlink, part)
        self.manifest.upsert(self.device_key, pa.album_id, pa.album_label, pa.asset_id, f.role,
                             remote_path=f.remote_path, remote_size=f.size, remote_mtime=f.mtime,
                             dest_path=rel, state=mf.DONE, bytes_transferred=f.size)
        self._on_bytes(f.size or 0)
        return rel
