# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Export manifest stored at <destination>\\.jwAlbums\\manifest.sqlite.

One row per (device, album, asset, role).  Contains no photo contents.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

STATE_DIR = ".jwAlbums"
OLD_STATE_DIR = ".iPhoneAlbumExporter"     # name before the rename; moved over when found
MANIFEST_NAME = "manifest.sqlite"

PENDING, COPYING, DONE, FAILED = "pending", "copying", "done", "failed"

_DDL = """
CREATE TABLE IF NOT EXISTS files (
    device_key TEXT NOT NULL,
    album_id INTEGER NOT NULL,
    album TEXT,
    asset_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    remote_path TEXT,
    remote_size INTEGER,
    remote_mtime REAL,
    dest_path TEXT,              -- relative to the export root
    state TEXT NOT NULL,
    bytes_transferred INTEGER DEFAULT 0,
    error TEXT,
    completed_at REAL,
    updated_at REAL,
    PRIMARY KEY (device_key, album_id, asset_id, role)
);
"""


@dataclass
class ManifestEntry:
    device_key: str
    album_id: int
    asset_id: int
    role: str
    remote_path: str | None
    remote_size: int | None
    remote_mtime: float | None
    dest_path: str | None
    state: str
    bytes_transferred: int
    error: str | None


class Manifest:
    def __init__(self, export_root: str | os.PathLike) -> None:
        self.root = Path(export_root)
        state_dir = self.root / STATE_DIR
        old = self.root / OLD_STATE_DIR
        if not state_dir.exists() and (old / MANIFEST_NAME).is_file():
            # An export made before the rename: keep its history so nothing is copied twice.
            try:
                os.rename(old, state_dir)
            except OSError:
                state_dir = old       # in use or read-only: keep using it under the old name
        state_dir.mkdir(parents=True, exist_ok=True)
        _hide_on_windows(state_dir)
        self.path = state_dir / MANIFEST_NAME
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_DDL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def get(self, device_key: str, album_id: int, asset_id: int, role: str) -> ManifestEntry | None:
        r = self._conn.execute(
            "SELECT device_key, album_id, asset_id, role, remote_path, remote_size, remote_mtime, dest_path, "
            "state, bytes_transferred, error FROM files WHERE device_key=? AND album_id=? AND asset_id=? AND role=?",
            (device_key, album_id, asset_id, role)).fetchone()
        return ManifestEntry(*r) if r else None

    def dest_paths_in_use(self) -> dict[str, tuple[str, int, int, str]]:
        """normcased relative dest path -> (device_key, album_id, asset_id, role), for ALL devices.

        Names are owned across devices so two iPhones exporting into the same folder can
        never share (or resume each other's) destination or .part file.
        """
        out = {}
        for device_key, album_id, asset_id, role, dest in self._conn.execute(
                "SELECT device_key, album_id, asset_id, role, dest_path FROM files WHERE dest_path IS NOT NULL"):
            out[os.path.normcase(dest)] = (device_key, album_id, asset_id, role)
        return out

    def upsert(self, device_key: str, album_id: int, album: str, asset_id: int, role: str, *,
               remote_path: str | None = None, remote_size: int | None = None, remote_mtime: float | None = None,
               dest_path: str | None = None, state: str = PENDING, bytes_transferred: int = 0,
               error: str | None = None) -> None:
        now = time.time()
        self._conn.execute(
            "INSERT INTO files (device_key, album_id, album, asset_id, role, remote_path, remote_size, remote_mtime, "
            "dest_path, state, bytes_transferred, error, completed_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(device_key, album_id, asset_id, role) DO UPDATE SET album=excluded.album, "
            "remote_path=excluded.remote_path, remote_size=excluded.remote_size, remote_mtime=excluded.remote_mtime, "
            "dest_path=excluded.dest_path, state=excluded.state, bytes_transferred=excluded.bytes_transferred, "
            "error=excluded.error, completed_at=excluded.completed_at, updated_at=excluded.updated_at",
            (device_key, album_id, album, asset_id, role, remote_path, remote_size, remote_mtime, dest_path, state,
             bytes_transferred, error, now if state == DONE else None, now))
        self._conn.commit()

    def set_state(self, device_key: str, album_id: int, asset_id: int, role: str, state: str,
                  bytes_transferred: int | None = None, error: str | None = None) -> None:
        now = time.time()
        self._conn.execute(
            "UPDATE files SET state=?, bytes_transferred=COALESCE(?, bytes_transferred), error=?, "
            "completed_at=CASE WHEN ?='done' THEN ? ELSE completed_at END, updated_at=? "
            "WHERE device_key=? AND album_id=? AND asset_id=? AND role=?",
            (state, bytes_transferred, error, state, now, now, device_key, album_id, asset_id, role))
        self._conn.commit()

    def mark_failed(self, device_key: str, album_id: int, album: str, asset_id: int, role: str,
                    remote_path: str | None, error: str) -> None:
        """Record a failure without discarding the dest/remote fingerprint of an existing row."""
        now = time.time()
        self._conn.execute(
            "INSERT INTO files (device_key, album_id, album, asset_id, role, remote_path, state, error, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(device_key, album_id, asset_id, role) DO UPDATE SET "
            "state=excluded.state, error=excluded.error, updated_at=excluded.updated_at",
            (device_key, album_id, album, asset_id, role, remote_path, FAILED, error, now))
        self._conn.commit()

    def counts(self, device_key: str) -> dict[str, int]:
        return dict(self._conn.execute(
            "SELECT state, COUNT(*) FROM files WHERE device_key=? GROUP BY state", (device_key,)).fetchall())


def _hide_on_windows(path: Path) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        FILE_ATTRIBUTE_HIDDEN = 0x2
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs != -1 and not attrs & FILE_ATTRIBUTE_HIDDEN:
            ctypes.windll.kernel32.SetFileAttributesW(str(path), attrs | FILE_ATTRIBUTE_HIDDEN)
    except Exception:
        pass
