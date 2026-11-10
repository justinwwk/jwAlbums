# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Extract user albums, folders and their member assets from a local Photos.sqlite snapshot."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from export.path_safety import UnsafePathError, device_asset_path
from photos.models import (KIND_FOLDER, KIND_USER_ALBUM, Album, Asset, core_data_to_unix)
from photos.schema import PhotosSchema, _q

log = logging.getLogger(__name__)

MAX_FOLDER_DEPTH = 32


@dataclass
class ParsedLibrary:
    albums: list[Album]                   # user albums and folders, folders included for hierarchy
    assets: dict[int, Asset]              # only assets that belong to a user album
    members: list[tuple[int, int]]        # (album_id, asset_id) for user albums


def open_snapshot_readonly(db_path: str) -> sqlite3.Connection:
    """Open the local copy strictly read-only.

    mode=ro matters: a read-write connection (even with query_only) checkpoints the WAL
    into the main file when it closes, which would alter the kept base copy.  A read-only
    connection still reads the WAL but never writes the main file.
    """
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def parse_library(conn: sqlite3.Connection, schema: PhotosSchema) -> ParsedLibrary:
    acols = schema.album_cols
    trashed_filter = "AND (ZTRASHEDSTATE IS NULL OR ZTRASHEDSTATE = 0)" if "ZTRASHEDSTATE" in acols else ""
    raw = conn.execute(
        f"SELECT Z_PK, ZTITLE, ZKIND, ZPARENTFOLDER FROM ZGENERICALBUM "
        f"WHERE ZKIND IN (?, ?) {trashed_filter}", (KIND_USER_ALBUM, KIND_FOLDER)).fetchall()
    rows = {pk: (title, kind, parent) for pk, title, kind, parent in raw}

    def chain(pk: int) -> tuple[tuple[str, ...], int | None]:
        """Titles from the outermost user folder down to pk; parent id within the tree."""
        titles: list[str] = []
        seen: set[int] = set()
        cur: int | None = pk
        while cur is not None and cur in rows and cur not in seen and len(titles) < MAX_FOLDER_DEPTH:
            seen.add(cur)
            title, _kind, parent = rows[cur]
            titles.append(title or "Untitled")
            cur = parent if (parent in rows and rows[parent][1] == KIND_FOLDER) else None
        cyclic = cur is not None   # stopped on a cycle or depth limit: treat as top level
        parent = rows[pk][2]
        parent_id = parent if (not cyclic and parent in rows and rows[parent][1] == KIND_FOLDER
                               and parent != pk) else None
        if cyclic:
            return (rows[pk][0] or "Untitled",), None
        return tuple(reversed(titles)), parent_id

    members: list[tuple[int, int]] = []
    user_album_ids = [pk for pk, (_t, kind, _p) in rows.items() if kind == KIND_USER_ALBUM]
    if schema.join is not None and user_album_ids:
        j = schema.join
        ids = set(user_album_ids)
        for album_id, asset_id in conn.execute(
                f"SELECT {_q(j.album_col)}, {_q(j.asset_col)} FROM {_q(j.table)}"):
            if album_id in ids and isinstance(asset_id, int):
                members.append((album_id, asset_id))

    member_asset_ids = {a for _, a in members}
    assets = _load_assets(conn, schema, member_asset_ids)

    # Drop memberships pointing at trashed/hidden/missing assets for counting purposes.
    visible = {aid for aid, a in assets.items() if not a.trashed and not a.hidden}
    counts: dict[int, int] = {}
    for album_id, asset_id in members:
        if asset_id in visible:
            counts[album_id] = counts.get(album_id, 0) + 1

    albums = []
    for pk, (title, kind, _parent) in rows.items():
        path, parent_id = chain(pk)
        albums.append(Album(id=pk, title=title or "Untitled", kind=kind, parent_id=parent_id,
                            full_path=path, item_count=counts.get(pk, 0)))
    albums.sort(key=lambda a: [p.casefold() for p in a.full_path])
    log.info("parsed %d user albums, %d folders, %d member assets",
             sum(1 for a in albums if not a.is_folder), sum(1 for a in albums if a.is_folder), len(assets))
    return ParsedLibrary(albums, assets, members)


def _load_assets(conn: sqlite3.Connection, schema: PhotosSchema, ids: set[int]) -> dict[int, Asset]:
    if not ids:
        return {}
    c = schema.asset_cols
    t = _q(schema.asset_table)
    size_join = ""
    size_col = "NULL AS ZORIGINALFILESIZE"
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "ZADDITIONALASSETATTRIBUTES" in tables:
        aa_cols = {r[1] for r in conn.execute("PRAGMA table_info(ZADDITIONALASSETATTRIBUTES)")}
        if {"ZASSET", "ZORIGINALFILESIZE"} <= aa_cols:
            size_join = "LEFT JOIN ZADDITIONALASSETATTRIBUTES aa ON aa.ZASSET = a.Z_PK"
            size_col = "aa.ZORIGINALFILESIZE"
    # Edited assets: ZADJUSTMENTSSTATE (iOS 14+) is non-zero, older versions use ZHASADJUSTMENTS.
    adj_col = next((f"a.{n}" for n in ("ZADJUSTMENTSSTATE", "ZHASADJUSTMENTS") if n in c), "NULL")
    sql = (f"SELECT a.Z_PK, a.ZDIRECTORY, a.ZFILENAME, {_opt_a(c, 'ZDATECREATED')}, "
           f"{_opt_a(c, 'ZTRASHEDSTATE')}, {_opt_a(c, 'ZHIDDEN')}, {_opt_a(c, 'ZKIND')}, {size_col}, {adj_col} "
           f"FROM {t} a {size_join} WHERE a.Z_PK = ?")
    out: dict[int, Asset] = {}
    unsafe = 0
    for aid in sorted(ids):
        row = conn.execute(sql, (aid,)).fetchone()
        if row is None:
            continue
        pk, directory, filename, created, trashed, hidden, kind, size, adjusted = row
        try:
            dpath = device_asset_path(directory, filename)
        except UnsafePathError:
            dpath = None
            unsafe += 1
        if dpath and not dpath.startswith("DCIM/"):
            log.info("asset %d stored outside DCIM (%s)", pk, (directory or "").split("/")[0])
        out[pk] = Asset(id=pk, directory=directory or "", filename=filename or "", device_path=dpath,
                        created_timestamp=core_data_to_unix(created), trashed=bool(trashed),
                        hidden=bool(hidden), kind=int(kind or 0),
                        file_size=int(size) if isinstance(size, int) else None,
                        adjusted=isinstance(adjusted, int) and adjusted != 0)
    if unsafe:
        log.warning("%d assets have unusable device paths and will be reported as failures", unsafe)
    return out


def _opt_a(cols: set[str], name: str) -> str:
    return f"a.{name}" if name in cols else f"NULL AS {name}"
