# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

import sqlite3

import pytest

from photos.schema import SchemaUnsupportedError, detect_schema, write_diagnostic
from tests.fakes import make_photos_db


def _open(p):
    conn = sqlite3.connect(p)
    conn.execute("PRAGMA query_only = ON")
    return conn


def test_detects_z28_join_table(tmp_path):
    conn = _open(make_photos_db(tmp_path / "p.sqlite"))
    s = detect_schema(conn)
    assert s.asset_table == "ZASSET"
    assert (s.join.table, s.join.album_col, s.join.asset_col) == ("Z_28ASSETS", "Z_28ALBUMS", "Z_3ASSETS")


def test_detects_other_join_table_number(tmp_path):
    conn = _open(make_photos_db(tmp_path / "p.sqlite", join_table="Z_26ASSETS", album_col="Z_26ALBUMS",
                                asset_col="Z_3ASSETS"))
    s = detect_schema(conn)
    assert s.join.table == "Z_26ASSETS"
    assert s.join.album_col == "Z_26ALBUMS"


def test_detects_without_name_hints(tmp_path):
    # Column names give no hint; detection must come from the data alone.
    conn = _open(make_photos_db(tmp_path / "p.sqlite", join_table="Z_31ASSETS", album_col="Z_31X",
                                asset_col="Z_9Y"))
    s = detect_schema(conn)
    assert (s.join.album_col, s.join.asset_col) == ("Z_31X", "Z_9Y")


def test_overlapping_pk_ranges(tmp_path):
    # Realistic: album PKs are a small range that is also inside the asset PK range.
    albums = [(1, None, 3999, None, 0)] + [(pk, f"A{pk}", 2, 1, 0) for pk in range(2, 40)]
    assets = [(pk, "DCIM/100APPLE", f"IMG_{pk:04d}.JPG", 1.0, 0, 0, 0) for pk in range(1, 3000)]
    members = [(2 + (i % 38), 40 + i) for i in range(2000)]
    conn = _open(make_photos_db(tmp_path / "p.sqlite", albums=albums, assets=assets, members=members))
    s = detect_schema(conn)
    assert (s.join.album_col, s.join.asset_col) == ("Z_28ALBUMS", "Z_3ASSETS")


def test_generic_asset_fallback(tmp_path):
    conn = _open(make_photos_db(tmp_path / "p.sqlite", asset_table="ZGENERICASSET"))
    s = detect_schema(conn)
    assert s.asset_table == "ZGENERICASSET"
    assert s.join.table == "Z_28ASSETS"


def test_prefers_zasset_when_both_exist(tmp_path):
    p = make_photos_db(tmp_path / "p.sqlite",
                       extra_sql="CREATE TABLE ZGENERICASSET (Z_PK INTEGER PRIMARY KEY, ZDIRECTORY, ZFILENAME);")
    assert detect_schema(_open(p)).asset_table == "ZASSET"


def test_ambiguous_tables_refuse_to_guess(tmp_path):
    # An identical second membership table makes the choice ambiguous.
    p = make_photos_db(tmp_path / "p.sqlite", extra_sql=
                       "CREATE TABLE Z_29ASSETS AS SELECT Z_28ALBUMS AS Z_29ALBUMS, Z_3ASSETS AS Z_4ASSETS FROM Z_28ASSETS;")
    with pytest.raises(SchemaUnsupportedError) as ei:
        detect_schema(_open(p))
    assert ei.value.analysis


def test_missing_membership_table_with_user_albums(tmp_path):
    p = make_photos_db(tmp_path / "p.sqlite", extra_sql="DROP TABLE Z_28ASSETS;")
    with pytest.raises(SchemaUnsupportedError):
        detect_schema(_open(p))


def test_no_user_albums_needs_no_join(tmp_path):
    p = make_photos_db(tmp_path / "p.sqlite", albums=[(1, None, 3999, None, 0)], members=[],
                       extra_sql="DROP TABLE Z_28ASSETS;")
    assert detect_schema(_open(p)).join is None


def test_missing_tables(tmp_path):
    p = tmp_path / "x.sqlite"
    sqlite3.connect(p).execute("CREATE TABLE ZFOO (Z_PK INTEGER)").connection.commit()
    with pytest.raises(SchemaUnsupportedError):
        detect_schema(_open(p))


def test_missing_required_column(tmp_path):
    p = tmp_path / "x.sqlite"
    c = sqlite3.connect(p)
    c.executescript("CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZFILENAME TEXT);"
                    "CREATE TABLE ZGENERICALBUM (Z_PK INTEGER PRIMARY KEY, ZTITLE, ZKIND, ZPARENTFOLDER);")
    c.commit()
    with pytest.raises(SchemaUnsupportedError, match="ZDIRECTORY"):
        detect_schema(_open(p))


def test_diagnostic_contains_structure_not_metadata(tmp_path):
    p = make_photos_db(tmp_path / "p.sqlite", extra_sql="DROP TABLE Z_28ASSETS;")
    conn = _open(p)
    with pytest.raises(SchemaUnsupportedError) as ei:
        detect_schema(conn)
    out = write_diagnostic(conn, tmp_path / "schema_diagnostic.txt", "26.5", ei.value.reason, ei.value.analysis)
    text = out.read_text(encoding="utf-8")
    assert "iOS version: 26.5" in text and "PRAGMA user_version" in text
    assert "ZGENERICALBUM:" in text and "ZTITLE" in text
    assert "Candidate relationship-table analysis" in text
    # No album titles or file names are dumped.
    assert "Family" not in text and "IMG_" not in text


def test_query_only_blocks_writes(tmp_path):
    conn = _open(make_photos_db(tmp_path / "p.sqlite"))
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM ZASSET")
