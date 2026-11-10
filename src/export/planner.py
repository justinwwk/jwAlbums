# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Turn selected albums into a concrete list of device files to copy.

Only the DCIM directories that hold selected assets are listed (once each), and only
selected files and their companions are stat'ed.  Nothing proportional to the whole
library is done here.

Edited photos and videos are exported as the iPhone shows them: iOS keeps the original
untouched in DCIM and stores its own full-size render of the edit in
PhotoData/Mutations/<DCIM dir>/<stem>/Adjustments/FullSizeRender.<ext> (plus
FullSizeRender.mov for an edited Live Photo).  That render is copied as-is - nothing is
re-encoded.  It is used only when the Photos database marks the asset as edited, so a
leftover render of a reverted edit is never picked up.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Callable

from app.cancel import CancelToken
from export.path_safety import sanitize_component
from iphone.device import ReadOnlyIPhone, RemoteFileError
from photos.index_db import AlbumIndex
from photos.models import ASSET_KIND_VIDEO, Album

log = logging.getLogger(__name__)

ROLE_PRIMARY, ROLE_LIVE = "primary", "live"
VIDEO_EXTS = {".MOV", ".MP4", ".M4V", ".3GP", ".AVI"}
RENDER_STEM = "FULLSIZERENDER"
LIVE_DIR = "live_videos"          # Live Photo motion clips go in <album>/live_videos/


@dataclass
class ExportOptions:
    include_videos: bool = True
    include_live_video: bool = True
    skip_existing: bool = True
    edited_versions: bool = True      # export edits as shown on the iPhone (not exposed in the UI)


@dataclass
class PlannedFile:
    role: str
    remote_path: str
    remote_name: str             # actual on-device filename
    size: int | None = None      # filled by prestat
    mtime: float | None = None
    error: str | None = None
    subdir: tuple[str, ...] = ()   # folder below the album folder (Live clips)


@dataclass
class PlannedAsset:
    album_id: int
    album_label: str             # "Travel/Japan" for display
    album_dir: tuple[str, ...]   # sanitized destination components
    asset_id: int
    created_ts: float | None
    files: list[PlannedFile] = field(default_factory=list)


@dataclass
class ExportPlan:
    assets: list[PlannedAsset]
    unresolved: list[str]        # human-readable problems found while planning
    edited: int = 0              # assets exported in their edited form
    edits_missing: list[str] = field(default_factory=list)   # edited, but no render found: original used

    @property
    def total_files(self) -> int:
        return sum(1 for a in self.assets for f in a.files if f.error is None)

    @property
    def total_bytes(self) -> int:
        return sum(f.size or 0 for a in self.assets for f in a.files if f.error is None)


def album_dirs(albums: list[Album]) -> dict[int, tuple[str, ...]]:
    """Destination folder components for every album and folder in the index.

    Siblings whose sanitized names collide on case-insensitive NTFS (two albums both
    called "Trip", or "Trip" and "trip.") get " (2)", " (3)"... in album-id order, so
    different albums never share a folder and the mapping is stable across runs.
    """
    by_id = {a.id: a for a in albums}
    children: dict[int | None, list[Album]] = {}
    for a in albums:
        parent = a.parent_id if a.parent_id in by_id else None
        children.setdefault(parent, []).append(a)

    names: dict[int, str] = {}
    for siblings in children.values():
        used: set[str] = set()
        for a in sorted(siblings, key=lambda x: x.id):
            base = sanitize_component(a.title)
            name, n = base, 1
            while os.path.normcase(name) in used:
                n += 1
                name = sanitize_component(f"{base} ({n})")
            used.add(os.path.normcase(name))
            names[a.id] = name

    out: dict[int, tuple[str, ...]] = {}

    def resolve(aid: int, seen: frozenset) -> tuple[str, ...]:
        if aid in out:
            return out[aid]
        a = by_id[aid]
        parent = a.parent_id if a.parent_id in by_id and a.parent_id not in seen else None
        path = (resolve(parent, seen | {aid}) if parent is not None else ()) + (names[aid],)
        out[aid] = path
        return path

    for a in albums:
        resolve(a.id, frozenset())
    return out


async def build_plan(device: ReadOnlyIPhone, index: AlbumIndex, album_ids: list[int], options: ExportOptions,
                     cancel: CancelToken, on_status: Callable[[str, int, int], None]) -> ExportPlan:
    all_albums = index.albums()
    albums = {a.id: a for a in all_albums}
    dirs = album_dirs(all_albums)
    planned: list[PlannedAsset] = []
    unresolved: list[str] = []
    needed_dirs: dict[str, None] = {}
    edits_missing: list[str] = []
    edited_count = 0

    selected = [albums[i] for i in album_ids if i in albums and not albums[i].is_folder]
    per_album = []
    for album in selected:
        assets = [a for a in index.assets_for_album(album.id) if not a.trashed and not a.hidden]
        per_album.append((album, assets))
        for a in assets:
            if a.device_path:
                needed_dirs[a.device_path.rsplit("/", 1)[0]] = None

    # One listing per required DCIM directory: NAME.UPPER() -> actual name.
    listings: dict[str, dict[str, str]] = {}
    for i, d in enumerate(needed_dirs):
        cancel.raise_if_cancelled()
        on_status(f"Listing {d}", i, len(needed_dirs))
        try:
            listings[d] = {n.upper(): n for n in await device.listdir(d)}
        except RemoteFileError as e:
            listings[d] = {}
            log.warning("could not list a required directory")
            unresolved.append(f"{d}: could not list folder ({e}); Live Photo videos "
                              f"from this folder may be missing")

    edits_total = len({a.id for _, assets in per_album for a in assets if a.adjusted}) \
        if options.edited_versions else 0
    render_cache: dict[int, dict[str, str]] = {}
    for album, assets in per_album:
        label = "/".join(album.full_path)
        dir_parts = dirs[album.id]
        for a in assets:
            pa = PlannedAsset(album.id, label, dir_parts, a.id, a.created_timestamp)
            if not a.device_path:
                unresolved.append(f"{label}: asset #{a.id} has an unusable file path")
                continue
            directory, fname = a.device_path.rsplit("/", 1)
            stem, ext = os.path.splitext(fname)
            is_video = a.kind == ASSET_KIND_VIDEO or ext.upper() in VIDEO_EXTS
            if is_video and not options.include_videos:
                continue
            listing = listings.get(directory, {})
            actual = listing.get(fname.upper(), fname)
            renders: dict[str, str] = {}
            if a.adjusted and options.edited_versions:
                if a.id not in render_cache:          # an asset can be in several selected albums
                    cancel.raise_if_cancelled()
                    if len(render_cache) % 25 == 0:
                        on_status("Finding edited versions", len(render_cache), edits_total)
                    render_cache[a.id] = await _edit_renders(device, directory, stem)
                renders = render_cache[a.id]
                if not renders:
                    edits_missing.append(f"{label}/{actual}")
            primary_render = _pick_render(renders, video=is_video)
            if primary_render:
                name = stem + os.path.splitext(primary_render[1])[1].upper()
                pa.files.append(PlannedFile(ROLE_PRIMARY, primary_render[0], name))
            else:
                pa.files.append(PlannedFile(ROLE_PRIMARY, f"{directory}/{actual}", actual))
            if not is_video and options.include_live_video:
                live_render = _pick_render(renders, video=True)
                mov = listing.get((stem + ".MOV").upper())
                if live_render and mov:
                    pa.files.append(PlannedFile(ROLE_LIVE, live_render[0], stem + ".MOV", subdir=(LIVE_DIR,)))
                elif mov:
                    pa.files.append(PlannedFile(ROLE_LIVE, f"{directory}/{mov}", mov, subdir=(LIVE_DIR,)))
            if primary_render:
                edited_count += 1
            planned.append(pa)

    if edits_missing:
        log.info("%d edited assets have no saved edit render; originals used", len(edits_missing))
    plan = ExportPlan(planned, unresolved, edited_count, edits_missing)
    await prestat(device, plan, cancel, on_status)
    return plan


async def _edit_renders(device: ReadOnlyIPhone, directory: str, stem: str) -> dict[str, str]:
    """Upper-case extension -> device path of the iPhone's own render(s) of an edited asset."""
    adj = f"PhotoData/Mutations/{directory}/{stem}/Adjustments"
    try:
        names = await device.listdir(adj)
    except RemoteFileError:
        return {}
    out: dict[str, str] = {}
    for n in names:
        base, ext = os.path.splitext(n)
        if base.upper() == RENDER_STEM and ext:
            out[ext.upper()] = f"{adj}/{n}"
    return out


def _pick_render(renders: dict[str, str], video: bool) -> tuple[str, str] | None:
    """(device path, file name) of the still or the video render, if there is one."""
    for ext, path in sorted(renders.items()):
        if (ext in VIDEO_EXTS) == video:
            return path, path.rsplit("/", 1)[1]
    return None


async def prestat(device: ReadOnlyIPhone, plan: ExportPlan, cancel: CancelToken,
                  on_status: Callable[[str, int, int], None]) -> None:
    files = [f for a in plan.assets for f in a.files]
    for i, f in enumerate(files):
        cancel.raise_if_cancelled()
        if i % 25 == 0:
            on_status("Checking files on iPhone", i, len(files))
        try:
            st = await device.stat(f.remote_path)
            if st.is_dir:
                f.error = "is a directory"
            else:
                f.size, f.mtime = st.size, st.mtime
        except RemoteFileError as e:
            f.error = str(e) or "not readable"
