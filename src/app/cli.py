# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Command-line entry points: acceptance test, smoke test, album listing, single-album export."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

from app.cancel import CancelToken
from app.settings import device_dir
from app.version import __version__
from export.exporter import Exporter
from export.manifest import Manifest
from export.planner import ExportOptions, build_plan
from export.transfer import stream_copy
from iphone import connection
from iphone.benchmark import run_benchmark
from iphone.device import ReadOnlyIPhone
from iphone.usb_speed import query_iphone_usb_link, summarize
from photos.index_db import AlbumIndex, udid_hash
from photos.refresh import ensure_album_index
from photos.snapshot import REMOTE_DB


def _p(*a) -> None:
    print(*a, flush=True)


async def _pick_device(udid: str | None) -> ReadOnlyIPhone:
    if not await connection.usbmux_available():
        raise SystemExit(connection.MSG_NO_USBMUX)
    udids = await connection.list_usb_udids()
    if udid:
        if udid not in udids:
            raise SystemExit("That iPhone is not connected over USB")
    elif not udids:
        raise SystemExit(connection.MSG_NO_DEVICE)
    elif len(udids) > 1:
        raise SystemExit("Several iPhones are connected; pass --udid to choose one")
    else:
        udid = udids[0]
    dev = ReadOnlyIPhone(udid)
    await dev.connect()
    i = dev.device_info()
    _p(f"Device: {i.model} ({i.product_type})  iOS {i.ios_version}  Connected")
    return dev


def _print_usb(dev: ReadOnlyIPhone):
    i = dev.device_info()
    link = query_iphone_usb_link(dev.udid)
    headline, lines, warning = summarize(link, i.product_type, i.model)
    _p("USB Connection")
    _p("  " + headline)
    for ln in lines:
        _p("  " + ln)
    if warning:
        _p("  Warning: " + warning.replace("\n", "\n           "))
    return link


async def _index(dev: ReadOnlyIPhone, force: bool) -> AlbumIndex:
    t0 = [time.monotonic()]
    last = [0.0, 0]

    def prog(done: int, total: int) -> None:
        if done < last[1]:                 # snapshot restarted: measure the new attempt
            t0[0] = time.monotonic()
        last[1] = done
        if time.monotonic() - last[0] > 2 or done == total:
            last[0] = time.monotonic()
            rate = done / max(time.monotonic() - t0[0], 1e-6) / 1e6
            _p(f"  {done / 1e9:.2f} GB / {total / 1e9:.2f} GB  {rate:.0f} MB/s  {done / max(total, 1):.0%}")

    idx, cached = await ensure_album_index(dev, device_dir(dev.udid), force, lambda s: _p(s), prog, CancelToken())
    _p(f"Album index {'reused from cache' if cached else 'built'} (join table: {idx.join_table})")
    return idx


async def acceptance(udid: str | None, album_name: str | None) -> int:
    dev = await _pick_device(udid)
    try:
        link = _print_usb(dev)
        st = await dev.stat(REMOTE_DB)
        _p(f"PhotoData/Photos.sqlite exists: {st.size / 1e9:.2f} GB")
        idx = await _index(dev, force=False)
        albums = [a for a in idx.albums() if not a.is_folder]
        _p(f"\nFirst 10 user-created albums (of {len(albums)}):")
        for a in albums[:10]:
            _p(f"  {'/'.join(a.full_path):40s} {a.item_count:>7,}")
        if album_name:
            test = next((a for a in albums if "/".join(a.full_path) == album_name or a.title == album_name), None)
        else:
            nonempty = [a for a in albums if a.item_count > 0]
            test = min(nonempty, key=lambda a: a.item_count) if nonempty else None
        if test is None:
            _p("No suitable test album found")
            return 1
        _p(f"\nTest album: {'/'.join(test.full_path)} ({test.item_count} items)")
        assets = [a for a in idx.assets_for_album(test.id) if not a.trashed and not a.hidden]
        for a in assets[:10]:
            _p(f"  {a.device_path}")
        img = next((a for a in assets if a.device_path and a.kind == 0), None) or \
            next((a for a in assets if a.device_path), None)
        if img is None:
            _p("No resolvable asset in the test album")
            return 1
        rst = await dev.stat(img.device_path)
        os.makedirs("test_export", exist_ok=True)
        dest = os.path.join("test_export", os.path.basename(img.device_path))
        part = dest + ".part"
        await stream_copy(dev, img.device_path, part, rst.size, 0, lambda n: None, CancelToken())
        os.replace(part, dest)
        local = os.path.getsize(dest)
        ok = local == rst.size
        _p(f"\nCopied one file to {dest}: source {rst.size:,} bytes, destination {local:,} bytes -> "
           f"{'SIZE MATCH' if ok else 'SIZE MISMATCH'}")
        hints = [a.device_path for a in idx.largest_assets(10) if a.device_path]
        bench = await run_benchmark(dev, hints, CancelToken())
        _p(f"\nUSB link: {link.mode} ({link.link_text})")
        _p(f"Measured read: {bench.mb_per_s:.0f} MB/s" if bench.mb_per_s else f"Measured read: {bench.note}")
        idx.close()
        return 0 if ok else 1
    finally:
        await dev.disconnect()


async def list_albums(udid: str | None, refresh: bool) -> int:
    dev = await _pick_device(udid)
    try:
        _print_usb(dev)
        idx = await _index(dev, force=refresh)
        for a in idx.albums():
            indent = "  " * (len(a.full_path) - 1)
            _p(f"{indent}{a.title}/" if a.is_folder else f"{indent}{a.title:<40s} {a.item_count:>7,}")
        idx.close()
        return 0
    finally:
        await dev.disconnect()


async def export_album(udid: str | None, album_name: str, dest: str) -> int:
    dev = await _pick_device(udid)
    try:
        idx = await _index(dev, force=False)
        album = next((a for a in idx.albums() if not a.is_folder and
                      ("/".join(a.full_path) == album_name or a.title == album_name)), None)
        if album is None:
            raise SystemExit(f"Album not found: {album_name}")
        cancel = CancelToken()
        plan = await build_plan(dev, idx, [album.id], ExportOptions(), cancel, lambda s, i, n: None)
        _p(f"Exporting {plan.total_files} files ({plan.total_bytes / 1e9:.2f} GB) to {dest}")
        os.makedirs(dest, exist_ok=True)
        manifest = Manifest(dest)
        last = [0.0]

        def prog(p) -> None:
            if time.monotonic() - last[0] > 2:
                last[0] = time.monotonic()
                _p(f"  {p.done_items}/{p.total_items}  {p.done_bytes / 1e6:.0f}/{p.total_bytes / 1e6:.0f} MB  "
                   f"{p.bytes_per_s / 1e6:.0f} MB/s")

        async def reconnect() -> None:
            await dev.disconnect()
            while True:
                _p("iPhone disconnected - waiting...")
                if not await connection.wait_for_device(dev.udid, cancel):
                    raise SystemExit("cancelled")
                try:
                    await dev.connect()
                    return
                except Exception:
                    await asyncio.sleep(2)

        try:
            summary = await Exporter(dev, manifest, dest, udid_hash(dev.udid), ExportOptions(), prog, _p,
                                     cancel, reconnect).run(plan)
        finally:
            manifest.close()
        _p(f"Copied {summary.copied}, skipped {summary.skipped}, failed {len(summary.failures)}")
        for f in summary.failures:
            _p("  FAILED " + f)
        idx.close()
        return 0 if not summary.failures else 2
    finally:
        await dev.disconnect()


def main(argv: list[str] | None = None) -> int:
    from app import network_guard
    network_guard.install()
    # Album titles may contain characters the console code page cannot encode.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(prog="jwAlbums")
    ap.add_argument("--version", action="version", version=f"jwAlbums {__version__}")
    ap.add_argument("--udid", help="choose a device when several are connected")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--acceptance", action="store_true", help="run the initial real-device acceptance test")
    g.add_argument("--smoke", action="store_true", help="run the read-only real-device smoke test")
    g.add_argument("--list-albums", action="store_true")
    g.add_argument("--export-album", metavar="ALBUM")
    ap.add_argument("--album", help="album to use for --acceptance")
    ap.add_argument("--dest", help="destination folder for --export-album")
    ap.add_argument("--refresh", action="store_true", help="force re-reading the album database")
    args = ap.parse_args(argv)
    if args.smoke:
        from app.smoke import smoke
        return asyncio.run(smoke(args.udid))
    if args.acceptance:
        return asyncio.run(acceptance(args.udid, args.album))
    if args.list_albums:
        return asyncio.run(list_albums(args.udid, args.refresh))
    if not args.dest:
        ap.error("--export-album requires --dest")
    return asyncio.run(export_album(args.udid, args.export_album, args.dest))


if __name__ == "__main__":
    sys.exit(main())
