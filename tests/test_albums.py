# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

import sqlite3

from photos.album_parser import open_snapshot_readonly, parse_library
from photos.index_db import AlbumIndex, build_index
from photos.models import CORE_DATA_EPOCH_OFFSET, FileFingerprint, RemoteFingerprint
from photos.schema import detect_schema
from tests.fakes import make_photos_db


def _parse(path):
    conn = open_snapshot_readonly(str(path))
    return parse_library(conn, detect_schema(conn))


def test_hierarchy_reconstruction(tmp_path):
    lib = _parse(make_photos_db(tmp_path / "p.sqlite"))
    paths = {a.full_path: a for a in lib.albums}
    assert ("Travel",) in paths and paths[("Travel",)].is_folder
    assert ("Travel", "Japan") in paths and ("Travel", "China") in paths
    assert paths[("Travel", "Japan")].parent_id == 3
    assert ("Family",) in paths and paths[("Family",)].parent_id is None
    assert ("Baby",) in paths


def test_excludes_trashed_smart_and_root(tmp_path):
    lib = _parse(make_photos_db(tmp_path / "p.sqlite"))
    titles = {a.title for a in lib.albums}
    assert "Old" not in titles            # trashed album
    assert "Favorites" not in titles      # smart / shared kinds
    assert "Untitled" not in titles       # root folder 3999


def test_counts_exclude_trashed_and_hidden(tmp_path):
    assets = [(pk, "DCIM/100APPLE", f"IMG_{pk:04d}.JPG", 1.0, 1 if pk == 101 else 0, 1 if pk == 102 else 0, 0)
              for pk in range(100, 300)]
    lib = _parse(make_photos_db(tmp_path / "p.sqlite", assets=assets))
    fam = next(a for a in lib.albums if a.title == "Family")
    assert fam.item_count == 28


def test_nested_folders_and_cycle_guard(tmp_path):
    albums = [(1, None, 3999, None, 0), (10, "A", 4000, 1, 0), (11, "B", 4000, 10, 0), (12, "Deep", 2, 11, 0),
              (20, "X", 4000, 21, 0), (21, "Y", 4000, 20, 0), (22, "Loop", 2, 20, 0)]
    members = [(12, 100), (22, 101)]
    lib = _parse(make_photos_db(tmp_path / "p.sqlite", albums=albums, members=members))
    paths = {a.title: a.full_path for a in lib.albums}
    assert paths["Deep"] == ("A", "B", "Deep")
    assert paths["Loop"][-1] == "Loop" and len(paths["Loop"]) <= 3


def test_asset_paths_and_dates(tmp_path):
    assets = [(100, "DCIM/100APPLE", "IMG_0100.HEIC", 10.0, 0, 0, 0),
              (101, "../../etc", "passwd", 10.0, 0, 0, 0),
              (102, "PhotoData/Sync/100SYNCD", "IMG_0102.JPG", None, 0, 0, 1)]
    lib = _parse(make_photos_db(tmp_path / "p.sqlite", assets=assets, members=[(2, 100), (2, 101), (2, 102)]))
    assert lib.assets[100].device_path == "DCIM/100APPLE/IMG_0100.HEIC"
    assert lib.assets[100].created_timestamp == 10.0 + CORE_DATA_EPOCH_OFFSET
    assert lib.assets[101].device_path is None          # traversal rejected
    assert lib.assets[102].device_path == "PhotoData/Sync/100SYNCD/IMG_0102.JPG"
    assert lib.assets[102].kind == 1 and lib.assets[102].created_timestamp is None


def test_index_round_trip(tmp_path):
    lib = _parse(make_photos_db(tmp_path / "p.sqlite"))
    fp = RemoteFingerprint(FileFingerprint(123, 4.5), None)
    build_index(tmp_path / "album_index.sqlite", lib, fp, "26.5", "UDID-1", "Phone", "iPhone17,1", "Z_28ASSETS")
    idx = AlbumIndex(tmp_path / "album_index.sqlite")
    assert idx.fingerprint == fp
    japan = next(a for a in idx.albums() if a.full_path == ("Travel", "Japan"))
    assert japan.item_count == 20
    assets = idx.assets_for_album(japan.id)
    assert len(assets) == 20 and assets[0].device_path.startswith("DCIM/100APPLE/")
    assert assets == sorted(assets, key=lambda a: (a.created_timestamp, a.id))
    # The UDID itself is not stored, only a hash.
    raw = sqlite3.connect(tmp_path / "album_index.sqlite").execute("SELECT udid_hash FROM device").fetchone()[0]
    assert raw != "UDID-1" and len(raw) == 16
    idx.close()


def test_title_with_slash_kept_as_single_component(tmp_path):
    albums = [(1, None, 3999, None, 0), (2, "2023/2024 Trip", 2, 1, 0)]
    lib = _parse(make_photos_db(tmp_path / "p.sqlite", albums=albums, members=[(2, 100)]))
    build_index(tmp_path / "i.sqlite", lib, RemoteFingerprint(FileFingerprint(1, 1.0), None), "x", "u", "n", "p", None)
    idx = AlbumIndex(tmp_path / "i.sqlite")
    assert idx.albums()[0].full_path == ("2023/2024 Trip",)
    idx.close()
