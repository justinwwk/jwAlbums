# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Regression tests for defects found in the adversarial review of the exporter."""

import asyncio
import os
import sqlite3
from datetime import datetime

import pytest

from app.cancel import Cancelled, CancelToken
from export import manifest as mf
from export.manifest import Manifest
from export.planner import ExportOptions, album_dirs
from iphone.device import DeviceConnectionLost, ReadOnlyIPhone, RemoteFileError, _posix
from photos.album_parser import open_snapshot_readonly, parse_library
from photos.models import Album
from photos.schema import detect_schema
from photos.snapshot import REMOTE_DB, REMOTE_WAL, LibraryChangedError, take_snapshot
from tests.fakes import FakeIPhone, make_photos_db
from tests.test_export_safety import MB, _cancel_after, _run
from tests.test_manifest import KEY, _album_id, _asset, _setup

SKIP_OFF = ExportOptions(skip_existing=False)


def _movie(tmp_path, size=10 * MB):
    data = os.urandom(size)
    idx, dev = _setup(tmp_path, [_asset(100, "IMG_0001.MOV", kind=1)], [(2, 100)],
                      files={"DCIM/100APPLE/IMG_0001.MOV": data})
    return data, idx, dev, _album_id(idx, "Family"), tmp_path / "out"


def test_done_entry_offset_never_trusted_for_stale_part(tmp_path):
    data, idx, dev, fam, dest = _movie(tmp_path)
    _run(dev, idx, [fam], dest)                                   # A: complete
    tok, restore = _cancel_after(dev, 6 * MB)
    _run(dev, idx, [fam], dest, options=SKIP_OFF, cancel=tok)     # B: re-copy interrupted
    restore()
    part = dest / "Family" / "IMG_0001.MOV.part"
    with open(part, "r+b") as f:                                  # power loss: garbage tail
        f.seek(2 * MB)
        f.write(b"\0" * (3 * MB))
    m = Manifest(dest)
    m.set_state(KEY, fam, 100, "primary", mf.COPYING, bytes_transferred=2 * MB)
    m.close()
    _run(dev, idx, [fam], dest)                                   # C: skip on
    assert not part.exists()                                      # our stale .part cleaned up
    _run(dev, idx, [fam], dest, options=SKIP_OFF)                 # D: skip off
    assert (dest / "Family" / "IMG_0001.MOV").read_bytes() == data
    assert os.listdir(dest / "Family") == ["IMG_0001.MOV"]


def test_interrupted_recopy_resumes_without_duplicates(tmp_path):
    data, idx, dev, fam, dest = _movie(tmp_path)
    _run(dev, idx, [fam], dest)
    tok, restore = _cancel_after(dev, 4 * MB)
    _run(dev, idx, [fam], dest, options=SKIP_OFF, cancel=tok)
    restore()
    before = dev.bytes_read
    _run(dev, idx, [fam], dest, options=SKIP_OFF)
    assert os.listdir(dest / "Family") == ["IMG_0001.MOV"]
    assert (dest / "Family" / "IMG_0001.MOV").read_bytes() == data
    # Resumed: only the missing part plus the head/tail verification sample was read.
    assert dev.bytes_read - before < len(data)


def test_skip_off_never_overwrites_user_edit(tmp_path):
    data, idx, dev, fam, dest = _movie(tmp_path, 3 * MB)
    _run(dev, idx, [fam], dest)
    final = dest / "Family" / "IMG_0001.MOV"
    edited = bytearray(final.read_bytes())
    edited[10:20] = b"USER-EDIT!"                                 # same-size in-place edit
    final.write_bytes(bytes(edited))
    _run(dev, idx, [fam], dest, options=SKIP_OFF)
    assert final.read_bytes() == bytes(edited)
    assert (dest / "Family" / "IMG_0001_2.MOV").read_bytes() == data


def test_colliding_album_names_get_separate_folders(tmp_path):
    albums = [(1, None, 3999, None, 0), (2, "Trip", 2, 1, 0), (3, "trip.", 2, 1, 0), (4, "Trip", 2, 1, 0)]
    files = {"DCIM/100APPLE/IMG_0100.JPG": b"a", "DCIM/100APPLE/IMG_0101.JPG": b"b",
             "DCIM/100APPLE/IMG_0102.JPG": b"c"}
    idx, dev = _setup(tmp_path, [_asset(100, "IMG_0100.JPG"), _asset(101, "IMG_0101.JPG"),
                                 _asset(102, "IMG_0102.JPG")], [(2, 100), (3, 101), (4, 102)],
                      albums=albums, files=files)
    dest = tmp_path / "out"
    _run(dev, idx, [2, 3, 4], dest)
    assert set(os.listdir(dest)) == {".jwAlbums", "Trip", "trip (2)", "Trip (3)"}
    assert os.listdir(dest / "Trip") == ["IMG_0100.JPG"]
    assert os.listdir(dest / "trip (2)") == ["IMG_0101.JPG"]
    assert os.listdir(dest / "Trip (3)") == ["IMG_0102.JPG"]


def test_album_dirs_stable_and_nested():
    albums = [Album(3, "Travel", 4000, None, ("Travel",), 0), Album(4, "Japan", 2, 3, ("Travel", "Japan"), 1),
              Album(9, "japan", 2, 3, ("Travel", "japan"), 1), Album(5, "Travel", 2, None, ("Travel",), 1)]
    d = album_dirs(albums)
    assert d[4] == ("Travel", "Japan") and d[9] == ("Travel", "japan (2)") and d[5] == ("Travel (2)",)
    assert album_dirs(list(reversed(albums))) == d


def test_folder_cycle_does_not_break_tree(tmp_path):
    albums = [(1, None, 3999, None, 0), (20, "X", 4000, 21, 0), (21, "Y", 4000, 20, 0), (22, "Loop", 2, 20, 0)]
    conn = open_snapshot_readonly(str(make_photos_db(tmp_path / "p.sqlite", albums=albums, members=[(22, 100)])))
    lib = parse_library(conn, detect_schema(conn))
    by_id = {a.id: a for a in lib.albums}
    for a in lib.albums:      # parent links and paths agree: no node points into a cycle
        assert a.parent_id is None or a.parent_id in by_id
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    from ui.widgets import CheckTree
    t = CheckTree(root)
    t.load(lib.albums)
    assert len(t.tree.get_children("")) >= 1
    # Select All / Select None buttons
    t.set_all(True)
    assert t.selected_album_ids() == [22]
    assert all(t._state[i] == "☑" for i in t._state)
    t.set_all(False)
    assert t.selected_album_ids() == []
    assert all(t._state[i] == "☐" for i in t._state)
    root.destroy()


@pytest.mark.parametrize("title_len", [40, 80])
def test_too_long_destination_is_reported(tmp_path, title_len):
    albums = [(1, None, 3999, None, 0), (2, "F" * title_len, 2, 1, 0)]
    idx, dev = _setup(tmp_path, [_asset(100, "IMG_0001.MOV", kind=1)], [(2, 100)], albums=albums,
                      files={"DCIM/100APPLE/IMG_0001.MOV": b"x" * 1000})
    dest = tmp_path / ("d" * (200 - len(str(tmp_path))))      # root ~200 chars: the manifest still fits
    s = _run(dev, idx, [2], dest)
    assert len(s.failures) == 1 and "too long" in s.failures[0]


def test_fok_column_does_not_create_ambiguity(tmp_path):
    # Ordering keys 1024*k coincide with real asset PKs.
    assets = [(pk, "DCIM/100APPLE", f"IMG_{pk:05d}.JPG", 1.0, 0, 0, 0) for pk in range(1, 20001)]
    albums = [(1, None, 3999, None, 0)] + [(pk, f"A{pk}", 2, 1, 0) for pk in range(2, 12)]
    members = [(a, 500 + a * 20 + i) for a in range(2, 12) for i in range(5)]
    conn = sqlite3.connect(make_photos_db(tmp_path / "p.sqlite", albums=albums, assets=assets, members=members))
    conn.execute("UPDATE Z_28ASSETS SET Z_FOK_3ASSETS = 1024 * (rowid % 15 + 1)")
    conn.commit()
    s = detect_schema(conn)
    assert (s.join.album_col, s.join.asset_col) == ("Z_28ALBUMS", "Z_3ASSETS")


# --------------------------------------------------------------------------- device error classification

class _FakeAfc:
    def __init__(self, fail_path_exc=None, alive=True):
        self.fail_path_exc = fail_path_exc
        self.alive = alive

    async def stat(self, path):
        if path == "DCIM":
            if not self.alive:
                raise ConnectionResetError()
            return {"st_size": "0", "st_mtime": datetime(2024, 1, 1), "st_birthtime": datetime(2024, 1, 1),
                    "st_ifmt": "S_IFDIR"}
        raise self.fail_path_exc

    async def close(self):
        pass


def _phone(afc):
    p = ReadOnlyIPhone("x")
    p._afc = afc
    return p


def test_unparseable_metadata_is_a_file_error_not_disconnect():
    p = _phone(_FakeAfc(OSError(22, "Invalid argument"), alive=True))
    with pytest.raises(RemoteFileError):
        asyncio.run(p.stat("DCIM/100APPLE/IMG_1.JPG"))
    assert p.connected


def test_unknown_error_with_dead_session_is_disconnect():
    p = _phone(_FakeAfc(OSError(121, "semaphore timeout"), alive=False))
    with pytest.raises(DeviceConnectionLost):
        asyncio.run(p.stat("DCIM/100APPLE/IMG_1.JPG"))
    assert not p.connected


def test_transport_error_is_disconnect():
    p = _phone(_FakeAfc(ConnectionResetError(), alive=True))
    with pytest.raises(DeviceConnectionLost):
        asyncio.run(p.stat("DCIM/100APPLE/IMG_1.JPG"))


def test_posix_handles_epoch_edge_dates():
    assert isinstance(_posix(datetime(1970, 1, 1, 0, 0, 1)), float)
    assert isinstance(_posix(datetime(1900, 1, 1)), float)


# --------------------------------------------------------------------------- snapshot

DB = os.urandom(4 * MB)


def _snapdev():
    d = FakeIPhone()
    d.add(REMOTE_DB, DB, 100.0)
    d.add(REMOTE_WAL, b"w" * 1000, 200.0)
    d.add(REMOTE_DB + "-shm", b"s" * 100, 200.0)
    return d


def test_wal_giveup_keeps_db_for_next_refresh(tmp_path):
    dev = _snapdev()
    n = [0]

    def on_stat(path, k):
        if path == REMOTE_WAL:
            n[0] += 1
            dev.add(REMOTE_WAL, b"w" * (1000 + n[0]), 200.0 + n[0])     # constantly appended

    dev.on_stat = on_stat
    with pytest.raises(LibraryChangedError):
        asyncio.run(take_snapshot(dev, tmp_path / "t", lambda d, t: None, CancelToken()))
    dev.on_stat = None
    before = dev.bytes_read
    r = asyncio.run(take_snapshot(dev, tmp_path / "t", lambda d, t: None, CancelToken()))
    assert r.db_path.read_bytes() == DB
    assert dev.bytes_read - before < len(DB)          # the main DB was not downloaded again


def test_unreadable_shm_is_optional(tmp_path):
    dev = _snapdev()
    dev.unreadable.add(REMOTE_DB + "-shm")
    r = asyncio.run(take_snapshot(dev, tmp_path / "t", lambda d, t: None, CancelToken()))
    assert r.db_path.read_bytes() == DB and not (r.directory / "Photos.sqlite-shm").exists()


def test_persistent_db_read_error_is_not_library_changed(tmp_path):
    dev = _snapdev()
    dev.unreadable.add(REMOTE_DB)
    with pytest.raises(RemoteFileError) as ei:
        asyncio.run(take_snapshot(dev, tmp_path / "t", lambda d, t: None, CancelToken()))
    assert not isinstance(ei.value, LibraryChangedError)


def test_corrupt_cached_index_releases_file(tmp_path):
    from photos.refresh import load_cached_index
    p = tmp_path / "album_index.sqlite"
    p.write_bytes(b"\0" * 4096)
    assert load_cached_index(tmp_path) is None
    replacement = tmp_path / "new.sqlite"
    replacement.write_bytes(b"x")
    os.replace(replacement, p)                        # would fail if the bad file were still open
