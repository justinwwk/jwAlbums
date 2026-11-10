# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Transient Windows file locks (antivirus / indexer holding a fresh file) must not fail tasks."""

import asyncio
import os
import sys
import threading
import time

import pytest

from app.cancel import CancelToken
from export import fsutil
from photos.snapshot import REMOTE_DB, REMOTE_WAL, take_snapshot
from tests.fakes import FakeIPhone

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows sharing semantics")


def _hold(path, seconds):
    """Hold the file open without delete sharing, like a scanner, then release it."""
    f = open(path, "rb")
    t = threading.Timer(seconds, f.close)
    t.start()
    return t


def test_unlink_waits_for_transient_lock(tmp_path):
    p = tmp_path / "x.part"
    p.write_bytes(b"data")
    _hold(p, 0.4)
    with pytest.raises(PermissionError):
        os.unlink(p)                       # proves the lock is real
    asyncio.run(fsutil.retry(os.unlink, p))
    assert not p.exists()


def test_rename_waits_for_transient_lock(tmp_path):
    src, dst = tmp_path / "a.part", tmp_path / "a.jpg"
    src.write_bytes(b"data")
    _hold(src, 0.4)
    fsutil.retry_sync(os.rename, src, dst)
    assert dst.read_bytes() == b"data"


def test_non_transient_errors_are_not_retried(tmp_path):
    t0 = time.monotonic()
    with pytest.raises(FileNotFoundError):
        asyncio.run(fsutil.retry(os.unlink, tmp_path / "missing"))
    with pytest.raises(FileExistsError):
        (tmp_path / "e").write_bytes(b"")
        asyncio.run(fsutil.retry(open, tmp_path / "e", "xb"))
    assert time.monotonic() - t0 < 1


def test_persistent_lock_gives_up(tmp_path):
    p = tmp_path / "x"
    p.write_bytes(b"d")
    f = open(p, "rb")
    try:
        with pytest.raises(PermissionError):
            fsutil.retry_sync(os.unlink, p, timeout=0.3)
    finally:
        f.close()


def test_snapshot_survives_locked_files_in_reused_dir(tmp_path):
    dev = FakeIPhone()
    db = os.urandom(3 * 1024 * 1024)
    dev.add(REMOTE_DB, db, 100.0)
    dev.add(REMOTE_WAL, b"w" * 500, 200.0)
    tok = CancelToken()

    def prog(d, t):
        if d >= 1024 * 1024:
            tok.cancel()

    with pytest.raises(Exception):
        asyncio.run(take_snapshot(dev, tmp_path / "t", prog, tok))
    (sdir,) = list((tmp_path / "t").iterdir())
    stale = sdir / "Photos.sqlite-wal"
    stale.write_bytes(b"stale wal")
    f = open(stale, "rb")                   # locked for longer than the retry window
    old_timeout = fsutil.TIMEOUT_S
    fsutil.TIMEOUT_S = 0.3
    try:
        import photos.snapshot as snapmod
        r = asyncio.run(take_snapshot(dev, tmp_path / "t", lambda d, t: None, CancelToken()))
    finally:
        fsutil.TIMEOUT_S = old_timeout
        f.close()
    assert r.directory != sdir                                  # locked dir abandoned, fresh one used
    assert r.db_path.read_bytes() == db
    assert (r.directory / "Photos.sqlite-wal").read_bytes() == b"w" * 500
