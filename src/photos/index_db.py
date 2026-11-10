# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""album_index.sqlite: the application's own compact album index."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path

from export import fsutil
from photos.album_parser import ParsedLibrary
from photos.models import Album, Asset, FileFingerprint, RemoteFingerprint

INDEX_NAME = "album_index.sqlite"
SCHEMA_VERSION = 2

_DDL = """
CREATE TABLE device (udid_hash TEXT, name TEXT, product_type TEXT);
CREATE TABLE albums (id INTEGER PRIMARY KEY, title TEXT, kind INTEGER, parent_id INTEGER,
                     full_path TEXT, item_count INTEGER);
CREATE TABLE assets (id INTEGER PRIMARY KEY, directory TEXT, filename TEXT, device_path TEXT,
                     created_timestamp REAL, trashed INTEGER, hidden INTEGER, kind INTEGER, file_size INTEGER,
                     adjusted INTEGER);
CREATE TABLE album_assets (album_id INTEGER, asset_id INTEGER, PRIMARY KEY (album_id, asset_id));
CREATE TABLE snapshot_info (ios_version TEXT, photos_db_size INTEGER, photos_db_mtime REAL,
                            wal_size INTEGER, wal_mtime REAL, index_created_at REAL, join_table TEXT,
                            schema_version INTEGER);
"""

# Album paths are stored as titles joined by a separator that cannot appear in an album title.
_PATH_SEP = "\x1f"


def udid_hash(udid: str) -> str:
    return hashlib.sha256(udid.encode("utf-8")).hexdigest()[:16]


def build_index(path: Path, parsed: ParsedLibrary, fingerprint: RemoteFingerprint, ios_version: str,
                udid: str, device_name: str, product_type: str, join_table: str | None) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript(_DDL)
        conn.execute("INSERT INTO device VALUES (?,?,?)", (udid_hash(udid), device_name, product_type))
        conn.executemany("INSERT INTO albums VALUES (?,?,?,?,?,?)",
                         [(a.id, a.title, a.kind, a.parent_id, _PATH_SEP.join(a.full_path), a.item_count)
                          for a in parsed.albums])
        conn.executemany("INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?,?)",
                         [(a.id, a.directory, a.filename, a.device_path, a.created_timestamp,
                           int(a.trashed), int(a.hidden), a.kind, a.file_size, int(a.adjusted))
                          for a in parsed.assets.values()])
        conn.executemany("INSERT OR IGNORE INTO album_assets VALUES (?,?)", parsed.members)
        wal = fingerprint.wal
        conn.execute("INSERT INTO snapshot_info VALUES (?,?,?,?,?,?,?,?)",
                     (ios_version, fingerprint.db.size, fingerprint.db.mtime, wal.size if wal else None,
                      wal.mtime if wal else None, time.time(), join_table, SCHEMA_VERSION))
        conn.commit()
    finally:
        conn.close()
    with open(tmp, "rb+") as f:
        os.fsync(f.fileno())
    fsutil.retry_sync(os.replace, tmp, path)


class AlbumIndex:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        try:
            row = self._conn.execute(
                "SELECT ios_version, photos_db_size, photos_db_mtime, wal_size, wal_mtime, index_created_at, "
                "join_table, schema_version FROM snapshot_info").fetchone()
            if row is None or row[7] != SCHEMA_VERSION:
                raise ValueError("album index is from an incompatible version")
            self.ios_version = row[0]
            wal = FileFingerprint(row[3], row[4]) if row[3] is not None else None
            self.fingerprint = RemoteFingerprint(FileFingerprint(row[1], row[2]), wal)
            self.created_at = row[5]
            self.join_table = row[6]
        except BaseException:
            # Close now so a rebuild can replace the file (Windows locks open files).
            self._conn.close()
            raise

    def close(self) -> None:
        self._conn.close()

    def albums(self) -> list[Album]:
        out = []
        for id_, title, kind, parent, full_path, count in self._conn.execute(
                "SELECT id, title, kind, parent_id, full_path, item_count FROM albums"):
            out.append(Album(id_, title, kind, parent, tuple(full_path.split(_PATH_SEP)) if full_path else (title,),
                             count))
        out.sort(key=lambda a: [p.casefold() for p in a.full_path])
        return out

    def album(self, album_id: int) -> Album | None:
        return next((a for a in self.albums() if a.id == album_id), None)

    def assets_for_album(self, album_id: int) -> list[Asset]:
        rows = self._conn.execute(
            "SELECT a.id, a.directory, a.filename, a.device_path, a.created_timestamp, a.trashed, a.hidden, "
            "a.kind, a.file_size, a.adjusted FROM album_assets m JOIN assets a ON a.id = m.asset_id "
            "WHERE m.album_id = ? ORDER BY a.created_timestamp, a.id", (album_id,)).fetchall()
        return [Asset(r[0], r[1], r[2], r[3], r[4], bool(r[5]), bool(r[6]), r[7], r[8], bool(r[9])) for r in rows]

    def largest_assets(self, limit: int = 10) -> list[Asset]:
        rows = self._conn.execute(
            "SELECT id, directory, filename, device_path, created_timestamp, trashed, hidden, kind, file_size "
            "FROM assets WHERE device_path IS NOT NULL AND trashed = 0 "
            "ORDER BY COALESCE(file_size, 0) DESC, kind DESC LIMIT ?", (limit,)).fetchall()
        return [Asset(r[0], r[1], r[2], r[3], r[4], bool(r[5]), bool(r[6]), r[7], r[8]) for r in rows]
