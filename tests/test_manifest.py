# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Manifest behaviour and end-to-end export against a fake device."""

import asyncio
import os

import pytest

from app.cancel import CancelToken
from export import manifest as mf
from export.exporter import Exporter
from export.manifest import Manifest
from export.planner import ExportOptions, build_plan
from photos.album_parser import open_snapshot_readonly, parse_library
from photos.index_db import AlbumIndex, build_index
from photos.models import FileFingerprint, RemoteFingerprint
from photos.schema import detect_schema
from tests.fakes import FakeIPhone, make_photos_db

KEY = "devkey"


def test_manifest_upsert_get_and_failed(tmp_path):
    m = Manifest(tmp_path)
    m.upsert(KEY, 1, "Family", 10, "primary", remote_path="DCIM/100APPLE/IMG_1.JPG", remote_size=5,
             remote_mtime=1.0, dest_path="Family\\IMG_1.JPG", state=mf.COPYING, bytes_transferred=2)
    e = m.get(KEY, 1, 10, "primary")
    assert (e.state, e.bytes_transferred, e.remote_size) == (mf.COPYING, 2, 5)
    m.mark_failed(KEY, 1, "Family", 10, "primary", "DCIM/100APPLE/IMG_1.JPG", "boom")
    e = m.get(KEY, 1, 10, "primary")
    assert e.state == mf.FAILED and e.dest_path == "Family\\IMG_1.JPG" and e.remote_size == 5
    m.mark_failed(KEY, 2, "Baby", 11, "primary", None, "missing")
    assert m.get(KEY, 2, 11, "primary").state == mf.FAILED
    assert m.dest_paths_in_use()[os.path.normcase("Family\\IMG_1.JPG")] == (KEY, 1, 10, "primary")
    m.close()
    # Persisted across reopen.
    m2 = Manifest(tmp_path)
    assert m2.get(KEY, 1, 10, "primary").error == "boom"
    m2.close()


# --------------------------------------------------------------------------- end-to-end fixtures

def _setup(tmp_path, assets, members, albums=None, files=None, extra_sql=""):
    db = make_photos_db(tmp_path / "Photos.sqlite", assets=assets, members=members, albums=albums,
                        extra_sql=extra_sql)
    conn = open_snapshot_readonly(str(db))
    lib = parse_library(conn, detect_schema(conn))
    conn.close()
    build_index(tmp_path / "idx.sqlite", lib, RemoteFingerprint(FileFingerprint(1, 1.0), None), "26.5", "u", "n",
                "p", "Z_28ASSETS")
    dev = FakeIPhone()
    for path, data in (files or {}).items():
        dev.add(path, data)
    return AlbumIndex(tmp_path / "idx.sqlite"), dev


def _export(dev, idx, album_ids, dest, options=None, cancel=None):
    options = options or ExportOptions()
    cancel = cancel or CancelToken()

    async def go():
        plan = await build_plan(dev, idx, album_ids, options, cancel, lambda *a: None)
        m = Manifest(dest)
        statuses = []

        async def reconnect():
            statuses.append("reconnect")
            await dev.connect()

        try:
            ex = Exporter(dev, m, str(dest), KEY, options, lambda p: None, statuses.append, cancel, reconnect)
            s = await ex.run(plan)
        finally:
            m.close()
        return s, statuses, plan

    return asyncio.run(go())


def _album_id(idx, title):
    return next(a.id for a in idx.albums() if a.title == title)


def _asset(pk, name, kind=0, d="DCIM/100APPLE"):
    return (pk, d, name, 700_000_000.0, 0, 0, kind)


def test_export_live_photo_and_timestamps(tmp_path):
    files = {"DCIM/100APPLE/IMG_0100.HEIC": b"h" * 1000, "DCIM/100APPLE/IMG_0100.MOV": b"m" * 3000,
             "DCIM/100APPLE/IMG_0100.AAE": b"<plist/>", "DCIM/100APPLE/IMG_0101.MOV": b"v" * 500}
    idx, dev = _setup(tmp_path, [_asset(100, "IMG_0100.HEIC"), _asset(101, "IMG_0101.MOV", kind=1)],
                      [(2, 100), (2, 101)], files=files)
    dest = tmp_path / "out"
    s, _, _ = _export(dev, idx, [_album_id(idx, "Family")], dest)
    fam = dest / "Family"
    # .AAE files are Apple-only edit instructions; edits are exported as rendered files instead.
    # Live Photo motion clips go in their own subfolder, so the album shows only photos and videos.
    assert sorted(os.listdir(fam)) == ["IMG_0100.HEIC", "IMG_0101.MOV", "live_videos"]
    assert os.listdir(fam / "live_videos") == ["IMG_0100.MOV"]
    assert (fam / "live_videos" / "IMG_0100.MOV").read_bytes() == b"m" * 3000
    assert s.copied == 3 and not s.failures
    taken = 700_000_000.0 + 978307200
    for p in [fam / "IMG_0100.HEIC", fam / "IMG_0101.MOV", fam / "live_videos" / "IMG_0100.MOV"]:
        st = os.stat(p)                              # Windows modified AND created = capture time
        assert abs(st.st_mtime - taken) < 1
        if os.name == "nt":
            assert abs(st.st_birthtime - taken) < 1
    # Only the one needed directory was listed; nothing else on the device was touched.
    assert dev.listdir_calls == ["DCIM/100APPLE"]

    # Options turn companions and videos off.
    dest2 = tmp_path / "out2"
    _export(dev, idx, [_album_id(idx, "Family")], dest2,
            ExportOptions(include_videos=False, include_live_video=False))
    assert os.listdir(dest2 / "Family") == ["IMG_0100.HEIC"]


def test_second_run_skips_verified_files(tmp_path):
    files = {f"DCIM/100APPLE/IMG_{i:04d}.JPG": os.urandom(2000) for i in range(100, 110)}
    idx, dev = _setup(tmp_path, [_asset(i, f"IMG_{i:04d}.JPG") for i in range(100, 110)],
                      [(2, i) for i in range(100, 110)], files=files)
    dest = tmp_path / "out"
    s1, _, _ = _export(dev, idx, [_album_id(idx, "Family")], dest)
    s2, _, _ = _export(dev, idx, [_album_id(idx, "Family")], dest)
    assert (s1.copied, s1.skipped) == (10, 0)
    assert (s2.copied, s2.skipped) == (0, 10)


def test_existing_different_file_is_never_overwritten(tmp_path):
    files = {"DCIM/100APPLE/IMG_0100.JPG": b"new" * 100}
    idx, dev = _setup(tmp_path, [_asset(100, "IMG_0100.JPG")], [(2, 100)], files=files)
    dest = tmp_path / "out"
    (dest / "Family").mkdir(parents=True)
    (dest / "Family" / "IMG_0100.JPG").write_bytes(b"user's own file")
    s, _, _ = _export(dev, idx, [_album_id(idx, "Family")], dest)
    assert (dest / "Family" / "IMG_0100.JPG").read_bytes() == b"user's own file"
    assert (dest / "Family" / "IMG_0100_2.JPG").read_bytes() == b"new" * 100
    assert s.copied == 1


def test_duplicate_filenames_in_one_album(tmp_path):
    # IMG_0001 wraps around between DCIM folders: same name, different photos.
    files = {"DCIM/100APPLE/IMG_0001.JPG": b"a" * 10, "DCIM/101APPLE/IMG_0001.JPG": b"b" * 20,
             "DCIM/101APPLE/IMG_0001.MOV": b"live"}
    idx, dev = _setup(tmp_path, [_asset(100, "IMG_0001.JPG"), _asset(101, "IMG_0001.JPG", d="DCIM/101APPLE")],
                      [(2, 100), (2, 101)], files=files)
    dest = tmp_path / "out"
    _export(dev, idx, [_album_id(idx, "Family")], dest)
    fam = dest / "Family"
    # The Live clip keeps the name of the photo it belongs to.
    assert sorted(os.listdir(fam)) == ["IMG_0001.JPG", "IMG_0001_2.JPG", "live_videos"]
    assert os.listdir(fam / "live_videos") == ["IMG_0001_2.MOV"]
    assert (fam / "IMG_0001_2.JPG").read_bytes() == b"b" * 20
    # Running again keeps the same mapping and skips everything.
    s, _, _ = _export(dev, idx, [_album_id(idx, "Family")], dest)
    assert s.skipped == 3 and s.copied == 0
    assert sorted(os.listdir(fam)) == ["IMG_0001.JPG", "IMG_0001_2.JPG", "live_videos"]
    assert os.listdir(fam / "live_videos") == ["IMG_0001_2.MOV"]


def test_same_photo_in_two_albums_gets_two_copies(tmp_path):
    files = {"DCIM/100APPLE/IMG_0100.JPG": b"x" * 50}
    idx, dev = _setup(tmp_path, [_asset(100, "IMG_0100.JPG")], [(2, 100), (8, 100)], files=files)
    dest = tmp_path / "out"
    _export(dev, idx, [_album_id(idx, "Family"), _album_id(idx, "Baby")], dest)
    assert (dest / "Family" / "IMG_0100.JPG").exists() and (dest / "Baby" / "IMG_0100.JPG").exists()


def test_folder_hierarchy_and_hostile_titles(tmp_path):
    albums = [(1, None, 3999, None, 0), (3, "Travel", 4000, 1, 0), (4, "Japan", 2, 3, 0),
              (5, "..\\..\\Windows", 2, 1, 0), (6, "CON", 2, 1, 0)]
    files = {"DCIM/100APPLE/IMG_0100.JPG": b"x"}
    idx, dev = _setup(tmp_path, [_asset(100, "IMG_0100.JPG")], [(4, 100), (5, 100), (6, 100)], albums=albums,
                      files=files)
    dest = tmp_path / "out"
    _export(dev, idx, [4, 5, 6], dest)
    assert (dest / "Travel" / "Japan" / "IMG_0100.JPG").exists()
    assert (dest / "_CON" / "IMG_0100.JPG").exists()
    hostile = [d for d in os.listdir(dest) if "Windows" in d]
    # Separators are replaced, so the title becomes one harmless folder name below the root.
    assert hostile == [".._.._Windows"] and (dest / hostile[0] / "IMG_0100.JPG").exists()
    assert not (tmp_path / "Windows").exists()


def test_one_bad_file_does_not_abort(tmp_path):
    files = {"DCIM/100APPLE/IMG_0100.JPG": b"a" * 10, "DCIM/100APPLE/IMG_0102.JPG": b"c" * 10,
             "DCIM/100APPLE/IMG_0103.JPG": b"d" * 10}
    # IMG_0101 is missing on the device; IMG_0103 fails to read.
    idx, dev = _setup(tmp_path, [_asset(i, f"IMG_{i:04d}.JPG") for i in range(100, 104)],
                      [(2, i) for i in range(100, 104)], files=files)
    dev.unreadable.add("DCIM/100APPLE/IMG_0103.JPG")
    dest = tmp_path / "out"
    s, statuses, _ = _export(dev, idx, [_album_id(idx, "Family")], dest)
    assert s.copied == 2 and len(s.failures) == 2
    assert "reconnect" not in statuses
    m = Manifest(dest)
    assert m.get(KEY, _album_id(idx, "Family"), 101, "primary").state == mf.FAILED
    assert m.get(KEY, _album_id(idx, "Family"), 103, "primary").state == mf.FAILED
    assert m.get(KEY, _album_id(idx, "Family"), 102, "primary").state == mf.DONE
    m.close()


def test_connection_loss_pauses_reconnects_and_resumes(tmp_path):
    big = os.urandom(9 * 1024 * 1024)
    files = {"DCIM/100APPLE/IMG_0100.MOV": big, "DCIM/100APPLE/IMG_0101.JPG": b"j" * 100}
    idx, dev = _setup(tmp_path, [_asset(100, "IMG_0100.MOV", kind=1), _asset(101, "IMG_0101.JPG")],
                      [(2, 100), (2, 101)], files=files)
    dev.fail_after_bytes = 5 * 1024 * 1024
    dest = tmp_path / "out"
    s, statuses, _ = _export(dev, idx, [_album_id(idx, "Family")], dest)
    assert "iPhone disconnected" in statuses and "reconnect" in statuses
    assert (dest / "Family" / "IMG_0100.MOV").read_bytes() == big
    assert s.copied == 2 and not s.failures          # not marked failed because of the disconnect
    assert dev.connects == 1


def test_interrupted_run_resumes_part_file(tmp_path):
    big = os.urandom(10 * 1024 * 1024)
    files = {"DCIM/100APPLE/IMG_0100.MOV": big}
    idx, dev = _setup(tmp_path, [_asset(100, "IMG_0100.MOV", kind=1)], [(2, 100)], files=files)
    dest = tmp_path / "out"
    tok = CancelToken()
    orig_read = dev.read
    read_total = [0]

    async def read(h, n):
        data = await orig_read(h, n)
        read_total[0] += len(data)
        if read_total[0] >= 4 * 1024 * 1024:
            tok.cancel()
        return data

    dev.read = read
    s1, _, _ = _export(dev, idx, [_album_id(idx, "Family")], dest, cancel=tok)
    assert s1.cancelled
    part = dest / "Family" / "IMG_0100.MOV.part"
    assert part.exists() and big.startswith(part.read_bytes())
    m = Manifest(dest)
    assert m.get(KEY, _album_id(idx, "Family"), 100, "primary").state == mf.COPYING
    m.close()

    dev.read = orig_read
    before = dev.bytes_read
    s2, _, _ = _export(dev, idx, [_album_id(idx, "Family")], dest)
    assert (dest / "Family" / "IMG_0100.MOV").read_bytes() == big and not part.exists()
    assert dev.bytes_read - before == len(big) - 4 * 1024 * 1024   # only the missing tail was read
    assert s2.copied == 1


def test_changed_source_restarts_part(tmp_path):
    data = os.urandom(6 * 1024 * 1024)
    idx, dev = _setup(tmp_path, [_asset(100, "IMG_0100.MOV", kind=1)], [(2, 100)],
                      files={"DCIM/100APPLE/IMG_0100.MOV": data})
    dest = tmp_path / "out"
    dev.fail_after_bytes = 2 * 1024 * 1024
    tok = CancelToken()

    # First run: disconnect, and the reconnect hook cancels (user gave up).
    async def go():
        plan = await build_plan(dev, idx, [_album_id(idx, "Family")], ExportOptions(), tok, lambda *a: None)
        m = Manifest(dest)

        async def reconnect():
            from app.cancel import Cancelled
            raise Cancelled()

        try:
            await Exporter(dev, m, str(dest), KEY, ExportOptions(), lambda p: None, lambda s: None, tok,
                           reconnect).run(plan)
        finally:
            m.close()

    asyncio.run(go())
    asyncio.run(dev.connect())
    assert (dest / "Family" / "IMG_0100.MOV.part").exists()
    new = os.urandom(6 * 1024 * 1024)
    dev.add("DCIM/100APPLE/IMG_0100.MOV", new, mtime=1_800_000_000.0)    # file changed on the device
    _export(dev, idx, [_album_id(idx, "Family")], dest)
    assert (dest / "Family" / "IMG_0100.MOV").read_bytes() == new


@pytest.mark.parametrize("dest_kind", ["junction"])
def test_destination_link_escape_blocked(tmp_path, dest_kind):
    _winapi = pytest.importorskip("_winapi")
    files = {"DCIM/100APPLE/IMG_0100.JPG": b"x"}
    idx, dev = _setup(tmp_path, [_asset(100, "IMG_0100.JPG")], [(2, 100)], files=files)
    dest = tmp_path / "out"
    outside = tmp_path / "outside"
    dest.mkdir()
    outside.mkdir()
    try:
        _winapi.CreateJunction(str(outside), str(dest / "Family"))
    except OSError as e:
        pytest.skip(str(e))
    s, _, _ = _export(dev, idx, [_album_id(idx, "Family")], dest)
    assert s.failures and not os.listdir(outside)
