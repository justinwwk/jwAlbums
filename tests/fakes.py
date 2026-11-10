# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Test doubles: an in-memory read-only iPhone and a synthetic Photos.sqlite builder."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from iphone.device import DeviceConnectionLost, DeviceInfo, RemoteFileNotFound, RemoteStat


@dataclass
class FakeFile:
    data: bytes
    mtime: float = 1_700_000_000.0


class FakeIPhone:
    """Implements the ReadOnlyIPhone interface over a dict of paths.

    fail_after_bytes: simulate the USB session dying once that many bytes have been
    read in total (then the counter is cleared, so a reconnect succeeds).
    """

    def __init__(self, files: dict[str, FakeFile] | None = None, udid: str = "00008140-0000000000000001") -> None:
        self.udid = udid
        self.files: dict[str, FakeFile] = dict(files or {})
        self.connected = True
        self._handles: dict[int, list] = {}
        self._next = 1
        self.bytes_read = 0
        self.fail_after_bytes: int | None = None
        self.on_stat: Callable[[str, int], None] | None = None
        self.stat_calls: dict[str, int] = {}
        self.listdir_calls: list[str] = []
        self.unreadable: set[str] = set()
        self.connects = 0

    def add(self, path: str, data: bytes, mtime: float = 1_700_000_000.0) -> None:
        self.files[path] = FakeFile(data, mtime)

    def device_info(self) -> DeviceInfo:
        return DeviceInfo(self.udid, "Test iPhone", "iPhone17,1", "iPhone 16 Pro", "26.5", "23F00")

    async def connect(self) -> None:
        self.connected = True
        self.connects += 1

    async def disconnect(self) -> None:
        self.connected = False
        self._handles.clear()

    def _check(self) -> None:
        if not self.connected:
            raise DeviceConnectionLost("USB connection lost")

    def _dirs(self) -> set[str]:
        out = set()
        for p in self.files:
            parts = p.split("/")
            for i in range(1, len(parts)):
                out.add("/".join(parts[:i]))
        return out

    async def stat(self, path: str) -> RemoteStat:
        self._check()
        n = self.stat_calls[path] = self.stat_calls.get(path, 0) + 1
        if self.on_stat:
            self.on_stat(path, n)
        if path in self.files:
            f = self.files[path]
            return RemoteStat(len(f.data), f.mtime, f.mtime, False)
        if path in self._dirs():
            return RemoteStat(0, 0.0, 0.0, True)
        raise RemoteFileNotFound("not found on iPhone")

    async def exists(self, path: str) -> bool:
        try:
            await self.stat(path)
            return True
        except RemoteFileNotFound:
            return False

    async def listdir(self, path: str) -> list[str]:
        self._check()
        self.listdir_calls.append(path)
        prefix = path.rstrip("/") + "/"
        names = {p[len(prefix):].split("/")[0] for p in list(self.files) + list(self._dirs()) if p.startswith(prefix)}
        if not names and path not in self._dirs():
            raise RemoteFileNotFound("not found on iPhone")
        return sorted(names)

    async def open_read(self, path: str) -> int:
        self._check()
        if path not in self.files:
            raise RemoteFileNotFound("not found on iPhone")
        h = self._next
        self._next += 1
        self._handles[h] = [path, 0]
        return h

    async def read(self, handle: int, size: int) -> bytes:
        self._check()
        path, pos = self._handles[handle]
        if path in self.unreadable:
            from iphone.device import RemoteFileError
            raise RemoteFileError("AFC error 4")
        data = self.files[path].data[pos:pos + size]
        if self.fail_after_bytes is not None and self.bytes_read + len(data) > self.fail_after_bytes:
            allowed = self.fail_after_bytes - self.bytes_read
            self.fail_after_bytes = None
            self.bytes_read = 0
            await self.disconnect()
            # Some bytes of this chunk never arrive.
            del allowed
            raise DeviceConnectionLost("USB connection lost")
        self.bytes_read += len(data)
        self._handles[handle][1] = pos + len(data)
        return data

    async def seek(self, handle: int, offset: int) -> None:
        self._check()
        self._handles[handle][1] = offset

    async def close(self, handle: int) -> None:
        self._handles.pop(handle, None)

    async def ping(self) -> bool:
        return self.connected


# --------------------------------------------------------------------------- Photos.sqlite fixtures

def make_photos_db(path: Path, *, asset_table: str = "ZASSET", join_table: str = "Z_28ASSETS",
                   album_col: str = "Z_28ALBUMS", asset_col: str = "Z_3ASSETS",
                   albums: list[tuple] | None = None, assets: list[tuple] | None = None,
                   members: list[tuple[int, int]] | None = None, decoys: bool = True,
                   cached_counts: bool = True, extra_sql: str = "") -> Path:
    """albums: (pk, title, kind, parent, trashed); assets: (pk, dir, filename, date, trashed, hidden, kind)."""
    if albums is None:
        albums = [
            (1, None, 3999, None, 0),                  # root folder (not shown)
            (2, "Family", 2, 1, 0),
            (3, "Travel", 4000, 1, 0),
            (4, "Japan", 2, 3, 0),
            (5, "China", 2, 3, 0),
            (6, "Old", 2, 1, 1),                       # trashed album
            (7, "Favorites", 1505, None, 0),           # smart/shared album
            (8, "Baby", 2, 1, 0),
        ]
    if assets is None:
        assets = [(pk, "DCIM/100APPLE", f"IMG_{pk:04d}.JPG", 700_000_000.0 + pk, 0, 0, 0) for pk in range(100, 300)]
    if members is None:
        members = [(2, pk) for pk in range(100, 130)] + [(4, pk) for pk in range(130, 150)] + \
                  [(5, pk) for pk in range(150, 160)] + [(8, pk) for pk in range(160, 220)] + \
                  [(7, pk) for pk in range(100, 110)]
    conn = sqlite3.connect(path)
    conn.executescript(f"""
        CREATE TABLE {asset_table} (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, ZDIRECTORY TEXT, ZFILENAME TEXT,
            ZDATECREATED TIMESTAMP, ZTRASHEDSTATE INTEGER, ZHIDDEN INTEGER, ZKIND INTEGER);
        CREATE TABLE ZGENERICALBUM (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, ZTITLE TEXT, ZKIND INTEGER,
            ZPARENTFOLDER INTEGER, ZTRASHEDSTATE INTEGER {', ZCACHEDCOUNT INTEGER' if cached_counts else ''});
        CREATE TABLE {join_table} ({album_col} INTEGER, {asset_col} INTEGER, Z_FOK_{asset_col[2:]} INTEGER,
            PRIMARY KEY ({album_col}, {asset_col}));
        CREATE TABLE ZADDITIONALASSETATTRIBUTES (Z_PK INTEGER PRIMARY KEY, ZASSET INTEGER, ZORIGINALFILESIZE INTEGER);
    """)
    counts: dict[int, int] = {}
    for a, _ in members:
        counts[a] = counts.get(a, 0) + 1
    for pk, title, kind, parent, trashed in albums:
        vals = [pk, 1, title, kind, parent, trashed] + ([counts.get(pk, 0)] if cached_counts else [])
        conn.execute(f"INSERT INTO ZGENERICALBUM VALUES ({','.join('?' * len(vals))})", vals)
    for row in assets:
        conn.execute(f"INSERT INTO {asset_table} VALUES (?,?,?,?,?,?,?,?)", (row[0], 3, *row[1:]))
        conn.execute("INSERT INTO ZADDITIONALASSETATTRIBUTES (ZASSET, ZORIGINALFILESIZE) VALUES (?,?)",
                     (row[0], 1000 + row[0]))
    for i, (a, s) in enumerate(members):
        conn.execute(f"INSERT INTO {join_table} VALUES (?,?,?)", (a, s, 1024 * (i + 1)))
    if decoys:
        # Other Core Data many-to-many tables that must not be mistaken for album membership.
        conn.executescript("""
            CREATE TABLE ZKEYWORD (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT);
            CREATE TABLE Z_1KEYWORDS (Z_1ASSETATTRIBUTES INTEGER, Z_40KEYWORDS INTEGER);
            CREATE TABLE Z_3MEMORIESBEINGCURATEDASSETS (Z_3CURATEDASSETS INTEGER, Z_45MEMORIES INTEGER);
        """)
        for i in range(50):
            conn.execute("INSERT INTO Z_1KEYWORDS VALUES (?,?)", (100 + i, 5000 + i % 3))
            conn.execute("INSERT INTO Z_3MEMORIESBEINGCURATEDASSETS VALUES (?,?)", (100 + i, 9000 + i % 4))
    if extra_sql:
        conn.executescript(extra_sql)
    conn.commit()
    conn.close()
    return path
