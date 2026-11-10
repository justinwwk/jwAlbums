# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Consistent, resumable local snapshot of PhotoData/Photos.sqlite (+ WAL).

The live database is never opened on the iPhone.  Its files are streamed read-only
into temp\\snapshot_<uuid>\\ and the before/after fingerprints are compared so an old
WAL is never paired with a new main database.

A complete main-database copy may be left in place between attempts (for example after
the WAL kept changing); it is reused only while the phone's main database has the same
size and modification time AND the local file still has the size and modification time
recorded when it was downloaded.  It is opened read-only (album_parser.open_snapshot_readonly),
so SQLite never folds a WAL into it.  The -shm is copied best effort but never relied on:
SQLite's Windows VFS truncates and rebuilds the WAL index when it is the first opener.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.cancel import CancelToken
from export import fsutil
from export.transfer import stream_copy
from iphone.device import ReadOnlyIPhone, RemoteFileError, RemoteFileNotFound
from photos.models import FileFingerprint, RemoteFingerprint

log = logging.getLogger(__name__)

REMOTE_DB = "PhotoData/Photos.sqlite"
REMOTE_WAL = REMOTE_DB + "-wal"
REMOTE_SHM = REMOTE_DB + "-shm"
LOCAL_DB = "Photos.sqlite"
STATE_FILE = "snapshot_state.json"
MAX_ATTEMPTS = 3

MSG_LIBRARY_CHANGED = ("Photo library changed while reading the album index.\n"
                       "Leave the Photos app closed for a moment and try Refresh again.")


class LibraryChangedError(Exception):
    def __init__(self) -> None:
        super().__init__(MSG_LIBRARY_CHANGED)


@dataclass(frozen=True)
class SnapshotResult:
    directory: Path
    db_path: Path
    fingerprint: RemoteFingerprint


ProgressFn = Callable[[int, int], None]   # (done_bytes, total_bytes)


async def _stat_fp(iphone: ReadOnlyIPhone, path: str) -> FileFingerprint | None:
    try:
        st = await iphone.stat(path)
    except RemoteFileNotFound:
        return None
    return FileFingerprint(st.size, st.mtime)


async def remote_fingerprint(iphone: ReadOnlyIPhone) -> RemoteFingerprint:
    db = await _stat_fp(iphone, REMOTE_DB)
    if db is None:
        raise RemoteFileNotFound("PhotoData/Photos.sqlite not found")
    return RemoteFingerprint(db, await _stat_fp(iphone, REMOTE_WAL))


async def _remove_sidecars(d: Path) -> None:
    for name in os.listdir(d):
        if name.startswith(LOCAL_DB + "-"):
            await fsutil.retry((d / name).unlink, missing_ok=True)


async def _prepare_dir(temp_root: Path, db_fp: FileFingerprint) -> Path:
    """Reuse a partial snapshot of the *same* main database, otherwise start fresh.

    Whatever is reused, every WAL/SHM file in it is deleted first.  If that is not
    possible (files still locked), the directory is abandoned and a fresh one is used.
    """
    temp_root.mkdir(parents=True, exist_ok=True)
    reuse: Path | None = None
    for d in sorted(temp_root.glob("snapshot_*")):
        try:
            state = json.loads((d / STATE_FILE).read_text(encoding="utf-8"))
            same = FileFingerprint.from_dict(state.get("db")) == db_fp
        except (OSError, ValueError, KeyError, TypeError):
            same = False
        if same and reuse is None:
            reuse = d
        else:
            shutil.rmtree(d, ignore_errors=True)   # best effort; leftovers are removed next time
    if reuse is not None:
        try:
            await _remove_sidecars(reuse)
            log.info("resuming partial Photos.sqlite snapshot")
            return reuse
        except OSError as e:
            log.warning("cannot reuse partial snapshot (%s); starting a fresh one", type(e).__name__)
            shutil.rmtree(reuse, ignore_errors=True)
    d = temp_root / f"snapshot_{uuid.uuid4().hex}"
    d.mkdir()
    _write_state(d, {"db": db_fp.to_dict(), "db_synced": 0})
    return d


def _read_state(d: Path) -> dict:
    try:
        return json.loads((d / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(d: Path, state: dict) -> None:
    tmp = d / (STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    fsutil.retry_sync(os.replace, tmp, d / STATE_FILE)


async def _copy_file(iphone: ReadOnlyIPhone, remote: str, local: Path, size: int, resume: bool,
                     on_bytes: Callable[[int], None], cancel: CancelToken) -> None:
    """Copy one database file.  With resume, only bytes recorded as fsynced are reused."""
    d = local.parent
    if resume and local.exists():
        st = local.stat()
        if st.st_size == size and _read_state(d).get("local") == _local_fp(st):
            on_bytes(size)
            return
        log.info("local database copy does not match its record; downloading it again")
        await fsutil.retry(local.unlink)
    part = local.with_name(local.name + ".part")
    offset = 0
    if resume and part.exists():
        synced = int(_read_state(d).get("db_synced") or 0)
        offset = min(part.stat().st_size, synced)
        if offset > size:
            offset = 0
    if offset == 0 and part.exists():
        await fsutil.retry(part.unlink)
    if offset:
        on_bytes(offset)

    on_synced = None
    if resume:
        def on_synced(pos: int) -> None:
            state = _read_state(d)
            state["db_synced"] = pos
            _write_state(d, state)

    await stream_copy(iphone, remote, str(part), size, offset, on_bytes, cancel, on_synced=on_synced)
    await fsutil.retry(os.replace, part, local)
    if resume:
        state = _read_state(d)
        state["local"] = _local_fp(local.stat())
        _write_state(d, state)


def _local_fp(st: os.stat_result) -> dict:
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


async def take_snapshot(iphone: ReadOnlyIPhone, temp_root: Path, progress: ProgressFn,
                        cancel: CancelToken, max_attempts: int = MAX_ATTEMPTS) -> SnapshotResult:
    unreadable: RemoteFileError | None = None    # a read error while the database was NOT changing
    for attempt in range(1, max_attempts + 1):
        before = await remote_fingerprint(iphone)
        sdir = await _prepare_dir(temp_root, before.db)
        total = before.db.size + (before.wal.size if before.wal else 0)
        done = 0

        def on_bytes(n: int) -> None:
            nonlocal done
            done += n
            progress(done, total)

        log.info("snapshot attempt %d: db=%d bytes wal=%s", attempt, before.db.size,
                 before.wal.size if before.wal else "none")
        try:
            await _copy_file(iphone, REMOTE_DB, sdir / LOCAL_DB, before.db.size, True, on_bytes, cancel)
        except RemoteFileError as e:
            # Shrunk or vanished mid-copy means the library is changing; the partial copy is
            # kept and reused next attempt only if the fingerprint still matches.  If the
            # database did not change, this is a genuine read error and is reported as such.
            try:
                changed = await _stat_fp(iphone, REMOTE_DB) != before.db
            except RemoteFileError:
                changed = True
            unreadable = None if changed else e
            log.warning("main database read failed during snapshot (%s; changed=%s)", e, changed)
            continue
        db_done = done

        wal_fp: FileFingerprint | None = None
        for _ in range(max_attempts):
            await _remove_sidecars(sdir)
            done = db_done
            try:
                wal_fp = await _stat_fp(iphone, REMOTE_WAL)
                total = before.db.size + (wal_fp.size if wal_fp else 0)
                if wal_fp:
                    await _copy_file(iphone, REMOTE_WAL, sdir / (LOCAL_DB + "-wal"), wal_fp.size, False,
                                     on_bytes, cancel)
                stable = await _stat_fp(iphone, REMOTE_WAL) == wal_fp
            except RemoteFileError as e:
                log.info("WAL changed while being read (%s); redoing WAL stage", e)
                continue
            if stable:
                await _copy_shm(iphone, sdir, cancel)
                break
            log.info("WAL changed during copy; redoing WAL stage")
        else:
            # Keep the complete main database: the next Refresh reuses it if it is unchanged.
            await _remove_sidecars(sdir)
            raise LibraryChangedError()

        try:
            after_db = await _stat_fp(iphone, REMOTE_DB)
        except RemoteFileError:
            after_db = None
        if after_db == before.db:
            log.info("snapshot consistent")
            return SnapshotResult(sdir, sdir / LOCAL_DB, RemoteFingerprint(before.db, wal_fp))
        log.info("Photos.sqlite changed during snapshot; restarting with a new snapshot directory")
        shutil.rmtree(sdir, ignore_errors=True)
    if unreadable is not None:
        raise unreadable
    raise LibraryChangedError()


async def _copy_shm(iphone: ReadOnlyIPhone, sdir: Path, cancel: CancelToken) -> None:
    """Best effort: SQLite rebuilds the WAL index when it is the first opener, so the SHM is optional."""
    local = sdir / (LOCAL_DB + "-shm")
    try:
        shm_fp = await _stat_fp(iphone, REMOTE_SHM)
        if shm_fp:
            await _copy_file(iphone, REMOTE_SHM, local, shm_fp.size, False, lambda n: None, cancel)
    except RemoteFileError as e:
        log.info("SHM not copied (%s); SQLite will rebuild it", e)
        for p in (local, local.with_name(local.name + ".part")):
            try:
                await fsutil.retry(p.unlink, missing_ok=True)
            except OSError:
                pass   # the stale SHM is harmless: SQLite resets it when it opens the database first


def discard_snapshot(result: SnapshotResult | Path) -> None:
    d = result.directory if isinstance(result, SnapshotResult) else result
    shutil.rmtree(d, ignore_errors=True)
