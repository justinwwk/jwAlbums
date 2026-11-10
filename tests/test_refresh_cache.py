# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""The album list is re-read only when the phone's database actually changed - and then
always, so it is never stale.  The downloaded database copy is never kept."""

import asyncio
import json
import os
import sqlite3

from app.cancel import CancelToken
from photos import snapshot
from photos.album_parser import open_snapshot_readonly
from photos.refresh import ensure_album_index
from photos.snapshot import REMOTE_DB, REMOTE_WAL
from tests.fakes import FakeIPhone, make_photos_db
from ui.widgets import visible_albums


class Library:
    """A real WAL-mode Photos-like database; captures (main, wal) bytes as the phone would hold them."""

    def __init__(self, tmp_path):
        self.path = make_photos_db(tmp_path / "phone.sqlite")
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA wal_autocheckpoint=0")     # changes stay in the WAL until checkpoint()
        self.next_pk = 20

    def add_album(self, title, members=range(100, 105)):
        pk = self.next_pk
        self.next_pk += 1
        members = list(members)
        self.conn.execute("INSERT INTO ZGENERICALBUM VALUES (?,?,?,?,?,?,?)", (pk, 1, title, 2, 1, 0, len(members)))
        for i, a in enumerate(members):
            self.conn.execute("INSERT INTO Z_28ASSETS VALUES (?,?,?)", (pk, a, i + 1))
        self.conn.commit()

    def checkpoint(self):
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def files(self):
        main = self.path.read_bytes()
        wal_p = self.path.with_name(self.path.name + "-wal")
        wal = wal_p.read_bytes() if wal_p.exists() and wal_p.stat().st_size else None
        return main, wal


def _publish(dev, lib, t):
    main, wal = lib.files()
    if REMOTE_DB not in dev.files or dev.files[REMOTE_DB].data != main:
        dev.add(REMOTE_DB, main, t)
    if wal is None:
        dev.files.pop(REMOTE_WAL, None)
    elif REMOTE_WAL not in dev.files or dev.files[REMOTE_WAL].data != wal:
        dev.add(REMOTE_WAL, wal, t + 0.5)
    return main, wal


def _load(dev, d, force=False):
    idx, cached = asyncio.run(ensure_album_index(dev, d, force, lambda s: None, lambda a, b: None, CancelToken()))
    titles = {a.title for a in idx.albums()}
    idx.close()
    return titles, cached


def _snapshots(d):
    return list((d / "temp").glob("snapshot_*"))


def test_reread_only_when_changed_and_never_stale(tmp_path):
    lib = Library(tmp_path)
    lib.add_album("InWal")
    dev = FakeIPhone()
    main0, wal0 = _publish(dev, lib, 1000.0)
    d = tmp_path / "dev"
    d.mkdir()

    titles, cached = _load(dev, d)
    assert "InWal" in titles and not cached
    assert _snapshots(d) == []          # the multi-GB copy (locations, people...) is never kept

    # 1. Nothing changed: the cache is used and nothing is downloaded.
    before = dev.bytes_read
    titles, cached = _load(dev, d)
    assert cached and "InWal" in titles and dev.bytes_read == before

    # 2. A change that is only in the WAL is detected and read.
    lib.add_album("SecondChange")
    main1, wal1 = _publish(dev, lib, 2000.0)
    assert main1 == main0 and wal1 != wal0
    titles, cached = _load(dev, d)
    assert not cached and {"InWal", "SecondChange"} <= titles

    # 3. A checkpoint rewrote the main database: detected and read.
    lib.checkpoint()
    lib.add_album("AfterCheckpoint")
    _publish(dev, lib, 3000.0)
    titles, cached = _load(dev, d)
    assert not cached and {"InWal", "SecondChange", "AfterCheckpoint"} <= titles
    assert _snapshots(d) == []


def test_album_deleted_in_wal_disappears(tmp_path):
    lib = Library(tmp_path)
    dev = FakeIPhone()
    _publish(dev, lib, 1000.0)
    d = tmp_path / "dev"
    d.mkdir()
    assert "Family" in _load(dev, d)[0]
    lib.conn.execute("DELETE FROM Z_28ASSETS WHERE Z_28ALBUMS = 2")   # Core Data removes memberships too
    lib.conn.execute("DELETE FROM ZGENERICALBUM WHERE ZTITLE='Family'")
    lib.conn.commit()
    _publish(dev, lib, 2000.0)
    titles, cached = _load(dev, d)
    assert not cached and "Family" not in titles


def _wal_giveup(dev, temp):
    """WAL keeps changing: the snapshot gives up but keeps the complete main-database copy."""
    import pytest
    n = [0]

    def on_stat(path, k):
        if path == REMOTE_WAL:
            n[0] += 1
            dev.add(REMOTE_WAL, b"w" * (1000 + n[0]), 5000.0 + n[0])

    dev.on_stat = on_stat
    with pytest.raises(snapshot.LibraryChangedError):
        asyncio.run(snapshot.take_snapshot(dev, temp, lambda a, b: None, CancelToken()))
    dev.on_stat = None
    (sdir,) = list(temp.glob("snapshot_*"))
    return sdir / "Photos.sqlite"


def test_kept_main_copy_records_its_local_state(tmp_path):
    dev = FakeIPhone()
    main = os.urandom(3 * 1024 * 1024)
    dev.add(REMOTE_DB, main, 1000.0)
    base = _wal_giveup(dev, tmp_path / "t")
    state = json.loads((base.parent / snapshot.STATE_FILE).read_text())
    assert state["local"] == {"size": base.stat().st_size, "mtime_ns": base.stat().st_mtime_ns}
    before = dev.bytes_read
    r = asyncio.run(snapshot.take_snapshot(dev, tmp_path / "t", lambda a, b: None, CancelToken()))
    assert r.db_path.read_bytes() == main and dev.bytes_read - before < len(main)   # reused


def test_altered_kept_copy_is_not_trusted(tmp_path):
    dev = FakeIPhone()
    main = os.urandom(3 * 1024 * 1024)
    dev.add(REMOTE_DB, main, 1000.0)
    base = _wal_giveup(dev, tmp_path / "t")
    with open(base, "r+b") as f:                 # e.g. some other tool touched the file
        f.seek(len(main) - 1)
        f.write(b"")
    before = dev.bytes_read
    r = asyncio.run(snapshot.take_snapshot(dev, tmp_path / "t", lambda a, b: None, CancelToken()))
    assert r.db_path.read_bytes() == main and dev.bytes_read - before >= len(main)   # downloaded again


def test_force_rereads_everything(tmp_path):
    lib = Library(tmp_path)
    dev = FakeIPhone()
    main0, _ = _publish(dev, lib, 1000.0)
    d = tmp_path / "dev"
    d.mkdir()
    _load(dev, d)
    before = dev.bytes_read
    _, cached = _load(dev, d, force=True)
    assert not cached and dev.bytes_read - before >= len(main0)


def test_failed_parse_does_not_keep_copy(tmp_path):
    dev = FakeIPhone()
    dev.add(REMOTE_DB, b"not a database" * 1000, 1000.0)
    d = tmp_path / "dev"
    d.mkdir()
    try:
        _load(dev, d)
    except Exception:
        pass
    assert list((d / "temp").glob("snapshot_*")) == []


def test_readonly_open_never_modifies_main_file(tmp_path):
    lib = Library(tmp_path)
    lib.add_album("OnlyInWal")
    main, wal = lib.files()
    d = tmp_path / "copy"
    d.mkdir()
    (d / "Photos.sqlite").write_bytes(main)
    (d / "Photos.sqlite-wal").write_bytes(wal)
    (d / "Photos.sqlite-shm").write_bytes(os.urandom(32768))    # garbage shm must not matter
    mtime = os.stat(d / "Photos.sqlite").st_mtime_ns
    conn = open_snapshot_readonly(str(d / "Photos.sqlite"))
    assert conn.execute("SELECT count(*) FROM ZGENERICALBUM WHERE ZTITLE='OnlyInWal'").fetchone()[0] == 1
    conn.close()
    assert (d / "Photos.sqlite").read_bytes() == main
    assert os.stat(d / "Photos.sqlite").st_mtime_ns == mtime


# --------------------------------------------------------------------------- hide empty albums

def _albums():
    from photos.models import Album
    return [
        Album(1, "Trips", 4000, None, ("Trips",), 0),
        Album(2, "Japan", 2, 1, ("Trips", "Japan"), 12),
        Album(3, "Empty", 2, 1, ("Trips", "Empty"), 0),
        Album(4, "Old", 4000, None, ("Old",), 0),
        Album(5, "Nothing", 2, 4, ("Old", "Nothing"), 0),
        Album(6, "Family", 2, None, ("Family",), 3),
        Album(7, "Blank", 2, None, ("Blank",), 0),
    ]


def test_hide_empty_albums():
    shown = {a.title for a in visible_albums(_albums(), True)}
    assert shown == {"Trips", "Japan", "Family"}
    assert len(visible_albums(_albums(), False)) == 7
