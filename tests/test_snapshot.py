# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import os

import pytest

from app.cancel import Cancelled, CancelToken
from photos.snapshot import (REMOTE_DB, REMOTE_WAL, LibraryChangedError, remote_fingerprint,
                             take_snapshot)
from tests.fakes import FakeIPhone

DB = os.urandom(5 * 1024 * 1024)
WAL = os.urandom(300_000)


def _dev(wal=True):
    d = FakeIPhone()
    d.add(REMOTE_DB, DB, 100.0)
    if wal:
        d.add(REMOTE_WAL, WAL, 200.0)
        d.add(REMOTE_DB + "-shm", b"s" * 32768, 200.0)
    return d


def snap(dev, tmp_path, **kw):
    return asyncio.run(take_snapshot(dev, tmp_path / "temp", lambda d, t: None, CancelToken(), **kw))


def test_snapshot_copies_db_wal_shm(tmp_path):
    r = snap(_dev(), tmp_path)
    assert r.db_path.read_bytes() == DB
    assert (r.directory / "Photos.sqlite-wal").read_bytes() == WAL
    assert (r.directory / "Photos.sqlite-shm").exists()
    assert r.fingerprint.db.size == len(DB) and r.fingerprint.wal.size == len(WAL)
    assert r.directory.name.startswith("snapshot_")


def test_snapshot_without_wal(tmp_path):
    r = snap(_dev(wal=False), tmp_path)
    assert r.fingerprint.wal is None and not (r.directory / "Photos.sqlite-wal").exists()


def test_wal_change_redoes_only_wal(tmp_path):
    dev = _dev()

    def on_stat(path, n):
        if path == REMOTE_WAL and n == 3:       # WAL grows right after its first copy
            dev.add(REMOTE_WAL, WAL + b"more", 201.0)

    dev.on_stat = on_stat
    r = snap(dev, tmp_path)
    assert (r.directory / "Photos.sqlite-wal").read_bytes() == WAL + b"more"
    assert r.fingerprint.wal.size == len(WAL) + 4
    assert dev.bytes_read < 2 * len(DB)          # main DB was not re-read


def test_db_change_restarts_in_new_directory(tmp_path):
    dev = _dev()
    first_dirs = []

    def on_stat(path, n):
        if path == REMOTE_DB and n == 2:        # changed while copying
            first_dirs.extend(os.listdir(tmp_path / "temp"))
            dev.add(REMOTE_DB, DB[::-1], 101.0)

    dev.on_stat = on_stat
    r = snap(dev, tmp_path)
    assert r.db_path.read_bytes() == DB[::-1]
    assert r.directory.name not in first_dirs
    assert os.listdir(tmp_path / "temp") == [r.directory.name]   # old snapshot removed


def test_persistent_changes_give_up(tmp_path):
    dev = _dev()
    counter = [0]

    def on_stat(path, n):
        if path == REMOTE_DB:
            counter[0] += 1
            dev.add(REMOTE_DB, DB, 100.0 + counter[0])

    dev.on_stat = on_stat
    with pytest.raises(LibraryChangedError, match="Photos app"):
        snap(dev, tmp_path, max_attempts=2)


def test_resume_partial_db_after_cancel(tmp_path):
    dev = _dev()
    tok = CancelToken()
    seen = [0]

    def prog(d, t):
        seen[0] = d
        if d >= 2 * 1024 * 1024:
            tok.cancel()

    with pytest.raises(Cancelled):
        asyncio.run(take_snapshot(dev, tmp_path / "temp", prog, tok))
    (sdir,) = list((tmp_path / "temp").iterdir())
    part = sdir / "Photos.sqlite.part"
    assert part.exists() and DB.startswith(part.read_bytes())
    # A stale WAL left in the directory must never be reused.
    (sdir / "Photos.sqlite-wal").write_bytes(b"stale")
    before = dev.bytes_read
    r = snap(dev, tmp_path)
    assert r.directory == sdir and r.db_path.read_bytes() == DB
    assert (r.directory / "Photos.sqlite-wal").read_bytes() == WAL
    assert dev.bytes_read - before < len(DB)     # resumed, not restarted


def test_partial_from_different_db_is_discarded(tmp_path):
    dev = _dev()
    tok = CancelToken()

    def prog(d, t):
        if d >= 1024 * 1024:
            tok.cancel()

    with pytest.raises(Cancelled):
        asyncio.run(take_snapshot(dev, tmp_path / "temp", prog, tok))
    old = list((tmp_path / "temp").iterdir())[0]
    dev.add(REMOTE_DB, DB[::-1], 150.0)
    r = snap(dev, tmp_path)
    assert r.directory != old and not old.exists()
    assert r.db_path.read_bytes() == DB[::-1]


def test_wal_vanishing_mid_stage_is_retried(tmp_path):
    dev = _dev()
    orig_open = dev.open_read
    state = {"done": False}

    async def open_read(path):
        if path == REMOTE_WAL and not state["done"]:
            state["done"] = True
            dev.files.pop(REMOTE_WAL)              # checkpoint deleted the WAL after our stat
        return await orig_open(path)

    dev.open_read = open_read
    r = snap(dev, tmp_path)
    assert r.db_path.read_bytes() == DB
    assert r.fingerprint.wal is None and not (r.directory / "Photos.sqlite-wal").exists()


def test_db_shrinking_mid_copy_restarts(tmp_path):
    dev = _dev()
    orig_read = dev.read
    state = {"done": False}

    async def read(h, n):
        if not state["done"] and dev.bytes_read >= 1024 * 1024:
            state["done"] = True
            dev.add(REMOTE_DB, DB[: 2 * 1024 * 1024], 300.0)   # truncated by the Photos daemon
        return await orig_read(h, n)

    dev.read = read
    r = snap(dev, tmp_path)
    assert r.db_path.read_bytes() == DB[: 2 * 1024 * 1024]
    assert r.fingerprint.db.mtime == 300.0


def test_resume_uses_only_synced_offset(tmp_path):
    import json
    dev = _dev()
    tok = CancelToken()

    def prog(d, t):
        if d >= 3 * 1024 * 1024:
            tok.cancel()

    with pytest.raises(Cancelled):
        asyncio.run(take_snapshot(dev, tmp_path / "temp", prog, tok))
    (sdir,) = list((tmp_path / "temp").iterdir())
    part = sdir / "Photos.sqlite.part"
    with open(part, "r+b") as f:          # simulated power loss: unsynced garbage tail
        f.seek(1024 * 1024)
        f.write(b"\0" * (2 * 1024 * 1024))
    st = json.loads((sdir / "snapshot_state.json").read_text())
    st["db_synced"] = 1024 * 1024
    (sdir / "snapshot_state.json").write_text(json.dumps(st))
    r = snap(dev, tmp_path)
    assert r.db_path.read_bytes() == DB


def test_remote_fingerprint(tmp_path):
    fp = asyncio.run(remote_fingerprint(_dev()))
    assert (fp.db.size, fp.db.mtime, fp.wal.size) == (len(DB), 100.0, len(WAL))
