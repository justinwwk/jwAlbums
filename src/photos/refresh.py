# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Reuse the cached album index when the remote database is unchanged, otherwise rebuild it."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Callable

from app.cancel import CancelToken
from export import fsutil
from iphone.device import ReadOnlyIPhone
from photos import snapshot
from photos.album_parser import open_snapshot_readonly, parse_library
from photos.index_db import INDEX_NAME, AlbumIndex, build_index
from photos.schema import SchemaUnsupportedError, detect_schema, write_diagnostic

log = logging.getLogger(__name__)

MSG_CHANGED = "Photo library changed. Refreshing album list..."
MSG_READING = "Reading iPhone album database"
MSG_PARSING = "Parsing albums..."


def load_cached_index(device_dir: Path) -> AlbumIndex | None:
    p = device_dir / INDEX_NAME
    if not p.exists():
        return None
    try:
        return AlbumIndex(p)
    except Exception as e:
        log.warning("cached album index unusable (%s)", type(e).__name__)
        return None


async def ensure_album_index(iphone: ReadOnlyIPhone, device_dir: Path, force: bool,
                             status: Callable[[str], None], progress: Callable[[int, int], None],
                             cancel: CancelToken) -> tuple[AlbumIndex, bool]:
    """Return (index, from_cache).

    The cache is used only when the phone's Photos.sqlite and its WAL are both exactly
    as they were when the cache was built (size and modification time, to the
    microsecond).  Any committed change to the library writes one of them, so a match
    means the album data cannot have changed; any difference triggers a re-read.
    force=True skips the check.
    """
    status("Checking photo library...")
    current = await snapshot.remote_fingerprint(iphone)
    cached = load_cached_index(device_dir)
    if cached is not None and not force:
        if cached.fingerprint == current:
            log.info("album index cache valid; reusing")
            return cached, True
        status(MSG_CHANGED)
        log.info("photo library changed since last index")
    if cached is not None:
        cached.close()
    temp = device_dir / "temp"
    if force:
        for d in temp.glob("snapshot_*"):
            snapshot.discard_snapshot(d)

    status(MSG_READING)
    snap = await snapshot.take_snapshot(iphone, temp, progress, cancel)
    info = iphone.device_info()
    conn = None
    try:
        cancel.raise_if_cancelled()
        status(MSG_PARSING)
        conn = open_snapshot_readonly(str(snap.db_path))
        try:
            schema = detect_schema(conn)
        except SchemaUnsupportedError as e:
            e.diagnostic_path = write_diagnostic(conn, device_dir / "schema_diagnostic.txt",
                                                 info.ios_version, e.reason, e.analysis)
            log.error("unsupported Photos schema: %s (diagnostic written)", e.reason)
            raise
        cancel.raise_if_cancelled()
        parsed = parse_library(conn, schema)
        cancel.raise_if_cancelled()
        build_index(device_dir / INDEX_NAME, parsed, snap.fingerprint, info.ios_version, info.udid,
                    info.name, info.product_type, schema.join.table if schema.join else None)
    finally:
        if conn is not None:
            conn.close()
        # The multi-GB copy holds all Photos metadata (locations, people...): never kept.
        snapshot.discard_snapshot(snap)
    tmp = device_dir / "fingerprint.json.tmp"
    tmp.write_text(json.dumps(snap.fingerprint.to_dict(), indent=2), encoding="utf-8")
    fsutil.retry_sync(os.replace, tmp, device_dir / "fingerprint.json")
    return AlbumIndex(device_dir / INDEX_NAME), False
