# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Background worker: a dedicated thread running its own asyncio event loop.

Tkinter never touches the device; it calls Worker methods (which only schedule
coroutines) and polls `Worker.out` for messages from app.messages.
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import threading
import time
from typing import Awaitable, Callable

from app import messages as m
from app.cancel import Cancelled, CancelToken
from app.settings import device_dir
from export.exporter import DiskFullError, Exporter, ExportProgress
from export.manifest import Manifest
from export.planner import ExportOptions, build_plan
from iphone import connection
from iphone.benchmark import run_benchmark
from iphone.device import DeviceConnectionLost, ReadOnlyIPhone, RemoteFileError, RemoteFileNotFound
from iphone.usb_speed import query_iphone_usb_link, summarize
from photos.index_db import AlbumIndex, udid_hash
from photos.refresh import ensure_album_index
from photos.schema import MSG_UNSUPPORTED, SchemaUnsupportedError
from photos.snapshot import LibraryChangedError

log = logging.getLogger(__name__)

# Consecutive reconnects tolerated while reading the album index or planning an export.
MAX_RECONNECTS = 20


class Worker:
    def __init__(self) -> None:
        self.out: "queue.Queue[object]" = queue.Queue()
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="device-worker", daemon=True)
        self._thread.start()
        self._busy = threading.Lock()
        self.cancel_token = CancelToken()
        self.device: ReadOnlyIPhone | None = None
        self.index: AlbumIndex | None = None
        self.dev_dir = None
        self._usb_link = None

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def post(self, msg: object) -> None:
        self.out.put(msg)

    @property
    def busy(self) -> bool:
        return self._busy.locked()

    def _submit(self, name: str, fn: Callable[[], Awaitable[None]]) -> bool:
        if not self._busy.acquire(blocking=False):
            return False
        self.cancel_token = CancelToken()
        asyncio.run_coroutine_threadsafe(self._wrap(name, fn), self.loop)
        return True

    async def _wrap(self, name: str, fn: Callable[[], Awaitable[None]]) -> None:
        try:
            await fn()
        except Cancelled:
            self.post(m.TaskDone(name, False, "Cancelled"))
        except SchemaUnsupportedError as e:
            where = f"\n{e.diagnostic_path}" if e.diagnostic_path else ""
            self.post(m.Error(MSG_UNSUPPORTED + where))
            self.post(m.TaskDone(name, False, "Unsupported album database"))
        except LibraryChangedError as e:
            self.post(m.Error(str(e)))
            self.post(m.TaskDone(name, False, "Library changed"))
        except DiskFullError as e:
            self.post(m.Error(str(e)))
            self.post(m.TaskDone(name, False, str(e)))
        except DeviceConnectionLost as e:
            self.post(m.Error(str(e) or connection.MSG_LOST))
            self.post(m.TaskDone(name, False, str(e) or connection.MSG_LOST))
        except Exception as e:
            log.exception("task %s failed", name)
            self.post(m.Error(f"{name} failed: {type(e).__name__}: {e}"))
            self.post(m.TaskDone(name, False, "Failed"))
        finally:
            self._busy.release()

    # ------------------------------------------------------------------ public commands (UI thread)

    def cancel(self) -> None:
        self.cancel_token.cancel()

    def scan_devices(self, auto: bool = False) -> bool:
        return self._submit("scan", lambda: self._scan(auto))

    def select_device(self, udid: str) -> bool:
        return self._submit("select", lambda: self._select(udid))

    def refresh_albums(self, force: bool = False) -> bool:
        return self._submit("refresh", lambda: self._refresh(force))

    def run_benchmark(self) -> bool:
        return self._submit("benchmark", self._benchmark)

    def export(self, album_ids: list[int], dest: str, options: ExportOptions) -> bool:
        return self._submit("export", lambda: self._export(album_ids, dest, options))

    def shutdown(self) -> None:
        self.cancel_token.cancel()

        async def _close() -> None:
            if self.device:
                await self.device.disconnect()

        try:
            asyncio.run_coroutine_threadsafe(_close(), self.loop).result(timeout=5)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)

    # ------------------------------------------------------------------ tasks (worker thread)

    async def _scan(self, auto: bool = False) -> None:
        if not auto:
            self.post(m.Status("Looking for iPhone..."))
        ok, devices = await connection.describe_devices()
        if not ok:
            self.post(m.DeviceList([], False, connection.MSG_NO_USBMUX, auto))
        elif not devices:
            self.post(m.DeviceList([], True, connection.MSG_NO_DEVICE, auto))
        else:
            self.post(m.DeviceList(devices, True, None, auto))
        self.post(m.TaskDone("scan", True))

    async def _select(self, udid: str) -> None:
        if self.device:
            await self.device.disconnect()
        if self.index:
            self.index.close()
            self.index = None
        self.post(m.Status("Connecting to iPhone..."))
        self.device = ReadOnlyIPhone(udid)
        try:
            await self.device.connect()
        except BaseException:
            self.device = None          # not connected: keep looking for it
            raise
        info = self.device.device_info()
        self.dev_dir = device_dir(udid)
        log.info("selected device %s, iOS %s", info.model, info.ios_version)
        self.post(m.DeviceSelected(info))

        # USB link speed is determined before any database download or export.
        self.post(m.Status("Checking USB connection..."))
        link = await asyncio.to_thread(query_iphone_usb_link, udid)
        self._usb_link = link
        headline, lines, warning = summarize(link, info.product_type, info.model)
        log.info("negotiated USB mode: %s (%s)", link.mode, link.link_text)
        self.post(m.UsbInfo(headline, lines, warning))

        await self._load_index(force=False)
        self.post(m.TaskDone("select", True))

    async def _refresh(self, force: bool) -> None:
        self._require_device()
        await self._load_index(force)
        self.post(m.TaskDone("refresh", True))

    async def _load_index(self, force: bool) -> None:
        assert self.device is not None and self.dev_dir is not None
        t0 = time.monotonic()

        def progress(done: int, total: int) -> None:
            dt = max(time.monotonic() - t0, 1e-6)
            self.post(m.Progress("snapshot", done, total, bytes_per_s=done / dt))

        if self.index:
            self.index.close()
            self.index = None
        for attempt in range(MAX_RECONNECTS + 1):
            try:
                index, from_cache = await ensure_album_index(
                    self.device, self.dev_dir, force, lambda s: self.post(m.Status(s)), progress,
                    self.cancel_token)
                break
            except RemoteFileNotFound:
                raise DeviceConnectionLost("Photos library not found on this iPhone (PhotoData/Photos.sqlite)")
            except RemoteFileError as e:
                raise RuntimeError(f"Could not read the iPhone photo database ({e})") from e
            except DeviceConnectionLost:
                if attempt == MAX_RECONNECTS:
                    raise
                await self._reconnect()
                t0 = time.monotonic()
        self.index = index
        albums = index.albums()
        log.info("album index: %d albums/folders (cache=%s)", len(albums), from_cache)
        self.post(m.AlbumsLoaded(albums, from_cache))
        self.post(m.Status("Ready", busy=False))

    async def _reconnect(self) -> None:
        assert self.device is not None
        udid = self.device.udid
        await self.device.disconnect()
        while True:
            self.post(m.Status("iPhone disconnected - reconnect it to continue"))
            if not await connection.wait_for_device(udid, self.cancel_token):
                raise Cancelled()
            try:
                await self.device.connect()
                self.post(m.Status("iPhone reconnected"))
                return
            except DeviceConnectionLost as e:
                self.post(m.Status(f"iPhone: {e}"))
                await asyncio.sleep(2)
                self.cancel_token.raise_if_cancelled()

    def _require_device(self) -> None:
        if self.device is None:
            raise DeviceConnectionLost(connection.MSG_NO_DEVICE)

    async def _benchmark(self) -> None:
        self._require_device()
        assert self.device is not None
        self.post(m.Status("Measuring iPhone read speed..."))
        hints = [a.device_path for a in (self.index.largest_assets(10) if self.index else []) if a.device_path]
        out = await run_benchmark(self.device, hints, self.cancel_token,
                                  lambda d, t: self.post(m.Progress("benchmark", d, t)))
        self.post(m.BenchmarkResult(out.mb_per_s, out.note))
        self.post(m.Status("Ready", busy=False))
        self.post(m.TaskDone("benchmark", True))

    async def _export(self, album_ids: list[int], dest: str, options: ExportOptions) -> None:
        self._require_device()
        assert self.device is not None
        if self.index is None:
            raise RuntimeError("Album list not loaded")
        if not dest:
            raise RuntimeError("Choose a destination folder")
        os.makedirs(dest, exist_ok=True)
        manifest = Manifest(dest)
        try:
            device_key = udid_hash(self.device.udid)
            for attempt in range(MAX_RECONNECTS + 1):
                try:
                    plan = await build_plan(
                        self.device, self.index, album_ids, options, self.cancel_token,
                        lambda s, i, n: self.post(m.Progress("prepare", 0, 0, i, n, current=s)))
                    break
                except DeviceConnectionLost:
                    if attempt == MAX_RECONNECTS:
                        raise
                    await self._reconnect()

            def on_progress(p: ExportProgress) -> None:
                self.post(m.Progress("export", p.done_bytes, p.total_bytes, p.done_items, p.total_items,
                                     p.bytes_per_s, p.album, p.current))

            exporter = Exporter(self.device, manifest, dest, device_key, options, on_progress,
                                lambda s: self.post(m.Status(s)), self.cancel_token, self._reconnect)
            self.post(m.Status("Exporting..."))
            summary = await exporter.run(plan)
        finally:
            manifest.close()
        text = (f"Copied {summary.copied}, skipped {summary.skipped} existing, "
                f"{len(summary.failures)} failed")
        if summary.cancelled:
            text = "Cancelled. " + text
        if plan.edited:
            text += f"\n{plan.edited} edited photos/videos were saved as edited on the iPhone."
        if plan.edits_missing:
            text += (f"\n{len(plan.edits_missing)} edited items had no saved edited version on the iPhone, "
                     f"so their original was saved instead (see log).")
            for item in plan.edits_missing[:200]:
                log.info("no edit render, original exported: %s", item)
        self.post(m.Status("Ready", busy=False))
        self.post(m.TaskDone("export", not summary.cancelled and not summary.failures, text, summary.failures))
