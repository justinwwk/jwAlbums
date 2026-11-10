# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Main Tkinter window.  Runs only on the main thread and only talks to the Worker."""

from __future__ import annotations

import logging
import os
import queue
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from app import messages as m
from app.settings import Settings
from app.version import CONTACT_URL, COPYRIGHT, __version__
from app.worker import Worker
from export.planner import ExportOptions
from photos.refresh import MSG_CHANGED, MSG_PARSING, MSG_READING
from ui.widgets import CheckTree, ProgressDialog, visible_albums

POLL_MS = 100
AUTO_SCAN_MS = 3000            # while no iPhone is connected, look for one this often
NO_DEVICE_HELP = ("Plug the iPhone in with a USB cable and unlock it.\n"
                  "It connects by itself as soon as Windows sees it.")
log = logging.getLogger(__name__)


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


def about_text() -> str:
    """The notices GPL-3.0 section 5(d) asks the program to show: copyright, no warranty, license."""
    return (f"jwAlbums {__version__}\n"
            "Copies iPhone Photos albums to Windows folders, read-only.\n\n"
            f"{COPYRIGHT}\n"
            f"{CONTACT_URL}\n\n"
            "This program is free software: you can redistribute it and/or modify it under the "
            "terms of the GNU General Public License, version 3 or (at your option) any later version.\n\n"
            "It comes with ABSOLUTELY NO WARRANTY.\n\n"
            "The license is in LICENSE.txt next to jwAlbums.exe "
            "(also https://www.gnu.org/licenses/gpl-3.0.html), the source code is in "
            "jwAlbums-source.zip, and the licenses of the open-source software it includes are in "
            "THIRD-PARTY-LICENSES.txt.\n\n"
            "Apple and iPhone are trademarks of Apple Inc. jwAlbums is not affiliated with or "
            "endorsed by Apple.")


class MainWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.worker = Worker()
        self.settings = Settings.load()
        self.devices: list = []
        self.current_task: str | None = None
        self.modal: ProgressDialog | None = None
        self._album_source = ""

        root.title(f"jwAlbums {__version__} - iPhone Album Exporter")
        root.geometry("780x860")
        root.minsize(680, 660)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build()
        self._set_busy(False)
        self.root.after(POLL_MS, self._poll)
        self._scan_sig: object = None
        self._start("scan", self.worker.scan_devices)
        self.root.after(AUTO_SCAN_MS, self._auto_scan)

    # ------------------------------------------------------------------ layout

    def _build(self) -> None:
        pad = {"padx": 10, "pady": 4}
        outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True)
        style = ttk.Style(self.root)
        style.configure("Big.TButton", font=("Segoe UI", 12, "bold"), padding=(28, 10))
        style.configure("Hint.TLabel", foreground="#6b6b6b")

        # Footer, packed first so it stays visible at the minimum window size. GPL-3.0 section 5(d)
        # asks an interactive program to show its copyright, no-warranty and license notices.
        foot = ttk.Frame(outer)
        foot.pack(side="bottom", fill="x", padx=12, pady=(0, 6))
        ttk.Label(foot, style="Hint.TLabel",
                  text=f"jwAlbums {__version__}  ·  {COPYRIGHT}  ·  Free software (GPL-3.0), no warranty"
                  ).pack(side="left")
        ttk.Button(foot, text="About", command=self._about).pack(side="right")

        # Device and USB side by side.
        top = ttk.Frame(outer)
        top.pack(fill="x", padx=10, pady=4)
        top.columnconfigure(0, weight=1, uniform="top")
        top.columnconfigure(1, weight=1, uniform="top")

        dev = ttk.LabelFrame(top, text="Device")
        dev.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        self.device_var = tk.StringVar(value="Looking for iPhone...")
        ttk.Label(dev, textvariable=self.device_var, justify="left").grid(row=0, column=0, sticky="nw", padx=8, pady=4)
        self.device_combo = ttk.Combobox(dev, state="readonly", width=30)
        self.use_btn = ttk.Button(dev, text="Use this iPhone", command=self._use_selected_device)
        self.rescan_btn = ttk.Button(dev, text="Rescan", command=self._rescan)
        self.rescan_btn.grid(row=0, column=2, sticky="ne", padx=8, pady=4)
        dev.columnconfigure(0, weight=1)

        usb = ttk.LabelFrame(top, text="USB")
        usb.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        self.usb_var = tk.StringVar(value="Not checked yet")
        ttk.Label(usb, textvariable=self.usb_var, justify="left").grid(row=0, column=0, columnspan=2, sticky="w",
                                                                      padx=8, pady=2)
        self.usb_warn_var = tk.StringVar(value="")
        self.usb_warn = ttk.Label(usb, textvariable=self.usb_warn_var, foreground="#a15c00", justify="left",
                                  wraplength=300)
        self.usb_warn.grid(row=1, column=0, columnspan=2, sticky="w", padx=8)
        usb.bind("<Configure>", lambda e: self.usb_warn.configure(wraplength=max(e.width - 24, 100)))
        self.bench_var = tk.StringVar(value="Measured read: not measured")
        ttk.Label(usb, textvariable=self.bench_var).grid(row=2, column=0, sticky="w", padx=8, pady=2)
        self.bench_btn = ttk.Button(usb, text="Run Speed Test", command=lambda: self._start("benchmark",
                                                                                             self.worker.run_benchmark))
        self.bench_btn.grid(row=2, column=1, sticky="e", padx=8, pady=(2, 6))
        usb.columnconfigure(0, weight=1)

        alb = ttk.LabelFrame(outer, text="Albums")
        alb.pack(fill="both", expand=True, **pad)
        bar = ttk.Frame(alb)
        bar.pack(fill="x", padx=6, pady=4)
        # Refresh re-reads the album database only if it changed on the iPhone.
        self.refresh_btn = ttk.Button(bar, text="Refresh Albums",
                                      command=lambda: self._start("refresh", lambda: self.worker.refresh_albums(False)))
        self.refresh_btn.pack(side="left")
        ttk.Button(bar, text="Select All", command=lambda: self.tree.set_all(True)).pack(side="left", padx=(10, 0))
        ttk.Button(bar, text="Select None", command=lambda: self.tree.set_all(False)).pack(side="left", padx=(4, 0))
        self.album_note = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.album_note).pack(side="left", padx=10)
        self.hide_empty_var = tk.BooleanVar(value=self.settings.hide_empty_albums)
        ttk.Checkbutton(bar, text="Hide empty albums", variable=self.hide_empty_var,
                        command=self._apply_album_filter).pack(side="right")
        self.albums: list = []
        self.tree = CheckTree(alb)
        self.tree.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        dst = ttk.LabelFrame(outer, text="Destination")
        dst.pack(fill="x", **pad)
        self.dest_var = tk.StringVar(value=self.settings.destination)
        ttk.Entry(dst, textvariable=self.dest_var).pack(side="left", fill="x", expand=True, padx=(8, 4), pady=6)
        ttk.Button(dst, text="Browse", command=self._browse).pack(side="right", padx=8, pady=6)

        opt = ttk.LabelFrame(outer, text="Options")
        opt.pack(fill="x", **pad)
        s = self.settings
        self.opt_vars = {
            "include_videos": tk.BooleanVar(value=s.include_videos),
            "include_live_video": tk.BooleanVar(value=s.include_live_video),
            "skip_existing": tk.BooleanVar(value=s.skip_existing),
        }
        labels = {
            "include_videos": ("Include videos", ""),
            "include_live_video": ("Include Live Photo motion", "the short clip of a Live Photo, saved in a live_videos subfolder"),
            "skip_existing": ("Skip files already exported", "so re-running only copies what is new"),
        }
        for i, (k, v) in enumerate(self.opt_vars.items()):
            text, hint = labels[k]
            ttk.Checkbutton(opt, text=text, variable=v).grid(row=i, column=0, sticky="w", padx=8, pady=1)
            ttk.Label(opt, text=hint, style="Hint.TLabel").grid(row=i, column=1, sticky="w", padx=(0, 8))
        ttk.Label(opt, style="Hint.TLabel", justify="left",
                  text="Edited photos and videos are saved the way they look on the iPhone (crops, filters, trims).\n"
                       "Files are copied byte for byte, and their Windows date is set to when they were taken."
                  ).grid(row=len(self.opt_vars), column=0, columnspan=2, sticky="w", padx=8, pady=(4, 4))

        act = ttk.Frame(outer)
        act.pack(fill="x", padx=10, pady=(8, 4))
        self.export_btn = ttk.Button(act, text="Export Selected Albums", style="Big.TButton", command=self._export)
        self.export_btn.pack(anchor="center")

        # Long tasks (album database, export, speed test) show their progress and their
        # Cancel/Stop button in a pop-up; the main window only keeps a one-line status.
        self.status_var = tk.StringVar(value="")
        ttk.Label(outer, textvariable=self.status_var, justify="left").pack(anchor="w", padx=12, pady=(2, 8))

    def _about(self) -> None:
        messagebox.showinfo("About jwAlbums", about_text(), parent=self.root)

    # ------------------------------------------------------------------ actions

    def _start(self, name: str, fn, retry: int = 0) -> None:
        if fn():
            self.current_task = name
            self._set_busy(True)
            if name == "export":
                self._open_modal("Exporting albums", "Stop").set_status("Preparing export...")
            elif name == "benchmark":
                self._open_modal("USB speed test", "Stop").set_status("Measuring read speed...")
        elif retry > 0:   # the previous task is still finishing up
            self.root.after(200, lambda: self._start(name, fn, retry - 1))

    def _rescan(self) -> None:
        # The result of this scan is always shown (DeviceList.auto is False); a background
        # check finishing meanwhile with the same result is ignored, so nothing runs twice.
        self.device_var.set("Looking for iPhone...")
        self.status_var.set("")
        self._start("scan", self.worker.scan_devices, retry=15)

    def _auto_scan(self) -> None:
        """Pick up an iPhone that is plugged in, unlocked or trusted after the app started."""
        try:
            if self.worker.device is None and not self.worker.busy and self.current_task is None \
                    and self.modal is None:
                self.worker.scan_devices(auto=True)
        finally:
            self.root.after(AUTO_SCAN_MS, self._auto_scan)

    def _set_busy(self, busy: bool) -> None:
        have_device = self.worker.device is not None
        for b in (self.refresh_btn, self.bench_btn, self.export_btn):
            b.state(["disabled"] if busy or not have_device else ["!disabled"])
        for b in (self.rescan_btn, self.use_btn):
            b.state(["disabled"] if busy else ["!disabled"])

    def _apply_album_filter(self) -> None:
        keep = set(self.tree.selected_album_ids())
        shown = visible_albums(self.albums, self.hide_empty_var.get())
        self.tree.load(shown)
        self.tree.set_checked(keep)
        n = sum(1 for a in shown if not a.is_folder)
        total = sum(1 for a in self.albums if not a.is_folder)
        hidden = total - n
        self.album_note.set(f"{n} albums" + (f" ({hidden} empty hidden)" if hidden else "") + self._album_source)

    def _browse(self) -> None:
        d = filedialog.askdirectory(initialdir=self.dest_var.get() or os.path.expanduser("~"),
                                    title="Choose export folder")
        if d:
            self.dest_var.set(os.path.normpath(d))

    def _options(self) -> ExportOptions:
        return ExportOptions(**{k: v.get() for k, v in self.opt_vars.items()})

    def _save_settings(self) -> None:
        self.settings.destination = self.dest_var.get().strip()
        for k, v in self.opt_vars.items():
            setattr(self.settings, k, bool(v.get()))
        self.settings.hide_empty_albums = bool(self.hide_empty_var.get())
        try:
            self.settings.save()
        except OSError:
            pass

    def _export(self) -> None:
        ids = self.tree.selected_album_ids()
        dest = self.dest_var.get().strip()
        if not ids:
            messagebox.showinfo("jwAlbums", "Tick one or more albums to export.")
            return
        if not dest or not os.path.isabs(dest):
            messagebox.showinfo("jwAlbums", "Choose a destination folder.")
            return
        self._save_settings()
        self._start("export", lambda: self.worker.export(ids, dest, self._options()))

    def _use_selected_device(self) -> None:
        i = self.device_combo.current()
        if 0 <= i < len(self.devices):
            udid = self.devices[i].udid
            self._start("select", lambda: self.worker.select_device(udid))

    def _on_close(self) -> None:
        if self.worker.busy and not messagebox.askyesno(
                "jwAlbums", "A task is running. Cancel it and quit?\n"
                "Partial files are kept and will resume next time."):
            return
        self._save_settings()
        self.worker.shutdown()
        self.root.destroy()

    # ------------------------------------------------------------------ worker messages

    def _poll(self) -> None:
        try:
            for _ in range(200):
                msg = self.worker.out.get_nowait()
                try:
                    self._handle(msg)
                except Exception:
                    log.exception("UI failed to handle %s", type(msg).__name__)
        except queue.Empty:
            pass
        finally:
            self.root.after(POLL_MS, self._poll)

    # ------------------------------------------------------------------ album-database pop-up

    def _open_modal(self, title: str = MSG_READING, cancel_text: str = "Cancel") -> ProgressDialog:
        if self.modal is None:
            self.modal = ProgressDialog(self.root, title, self.worker.cancel, cancel_text)
        return self.modal

    def _close_modal(self) -> None:
        if self.modal is not None:
            modal, self.modal = self.modal, None
            modal.close()

    def _handle(self, msg: object) -> None:
        if isinstance(msg, m.Status):
            if msg.text in (MSG_CHANGED, MSG_READING):
                self._open_modal()
            if self.modal is not None:
                self.modal.set_status(msg.text)
                if msg.text == MSG_PARSING:
                    self.modal.set_detail("")
                    self.modal.set_fraction(None)
            else:
                self.status_var.set(msg.text)
        elif isinstance(msg, m.DeviceList):
            self._on_devices(msg)
        elif isinstance(msg, m.DeviceSelected):
            i = msg.info
            self.device_var.set(f"{i.model}\niOS {i.ios_version}\nConnected")
            self.device_combo.grid_remove()
            self.use_btn.grid_remove()
        elif isinstance(msg, m.UsbInfo):
            self.usb_var.set("\n".join([msg.headline, *msg.lines]))
            self.usb_warn_var.set(f"Warning:\n{msg.warning}" if msg.warning else "")
        elif isinstance(msg, m.BenchmarkResult):
            self.bench_var.set(f"Measured read: {msg.mb_per_s:.0f} MB/s" if msg.mb_per_s
                               else f"Measured read: {msg.note}")
        elif isinstance(msg, m.AlbumsLoaded):
            self._close_modal()
            self.albums = list(msg.albums)
            self._album_source = "  -  no changes on iPhone" if msg.from_cache else "  -  read from iPhone"
            self._apply_album_filter()
        elif isinstance(msg, m.Progress):
            self._on_progress(msg)
        elif isinstance(msg, m.Error):
            self._close_modal()
            messagebox.showerror("jwAlbums", msg.text)
        elif isinstance(msg, m.TaskDone):
            self._close_modal()
            self._on_done(msg)

    def _on_devices(self, msg: m.DeviceList) -> None:
        sig = (msg.error, tuple((d.udid, d.status, d.ok) for d in msg.devices))
        if msg.auto and sig == self._scan_sig:
            return                    # background check, nothing changed: leave the window alone
        self._scan_sig = sig
        self.devices = msg.devices
        if not msg.auto:
            self.status_var.set("")
        if msg.error:
            when = time.strftime("%H:%M:%S")
            help_text = NO_DEVICE_HELP if msg.usbmux_ok else "Install Apple Devices from the Microsoft Store."
            self.device_var.set(f"{msg.error} (checked {when})\n{help_text}")
            self.device_combo.grid_remove()
            self.use_btn.grid_remove()
            return
        ready = [d for d in msg.devices if d.ok]
        if len(msg.devices) == 1 and ready:
            d = ready[0]
            self.device_var.set(f"{d.model}\niOS {d.ios_version}\n{d.status}")
            self.root.after(50, lambda: self._start("select", lambda: self.worker.select_device(d.udid), retry=25))
            return
        if len(msg.devices) == 1:
            self.device_var.set(f"{msg.devices[0].model}\n{msg.devices[0].status}")
            return
        # Several devices: the user must choose explicitly.
        self.device_var.set("Several iPhones are connected. Choose one:")
        self.device_combo["values"] = [f"{d.name} - {d.model} - iOS {d.ios_version} - {d.status}"
                                       for d in msg.devices]
        self.device_combo.set("")
        self.device_combo.grid(row=1, column=0, sticky="w", padx=8, pady=4)
        self.use_btn.grid(row=1, column=2, sticky="e", padx=8)

    def _on_progress(self, p: m.Progress) -> None:
        frac = (p.done_bytes / p.total_bytes) if p.total_bytes else (p.done_items / p.total_items if p.total_items else 0)
        speed = f"{p.bytes_per_s / 1e6:.0f} MB/s" if p.bytes_per_s else ""
        if p.phase == "snapshot":
            modal = self._open_modal()
            modal.set_fraction(frac)
            modal.set_detail(f"{fmt_bytes(p.done_bytes)} / {fmt_bytes(p.total_bytes)}    {speed}    {frac:.0%}")
            return
        modal = self.modal
        if modal is None:
            return
        if p.phase == "prepare":
            modal.set_status("Preparing export...")
            modal.set_fraction(frac if p.total_items else None)
            modal.set_detail(f"{p.current}  {p.done_items:,} / {p.total_items:,}" if p.total_items else p.current)
        elif p.phase == "export":
            modal.set_status("Exporting...")
            modal.set_fraction(frac)
            modal.set_detail(f"Album:  {p.album}\nFile:  {p.current}\n{p.done_items:,} / {p.total_items:,} files\n"
                             f"{fmt_bytes(p.done_bytes)} / {fmt_bytes(p.total_bytes)}    {speed}    {frac:.0%}")
        elif p.phase == "benchmark":
            modal.set_fraction(frac)
            modal.set_detail(f"Read {fmt_bytes(p.done_bytes)} / {fmt_bytes(p.total_bytes)}")

    def _on_done(self, msg: m.TaskDone) -> None:
        self.current_task = None
        self._set_busy(False)
        if msg.task in ("export", "benchmark", "refresh", "select") and msg.summary:
            self.status_var.set(msg.summary)
        if msg.task == "export":
            if msg.failures:
                self._show_failures(msg.summary, msg.failures)
            elif msg.ok:
                messagebox.showinfo("jwAlbums", f"Export complete.\n{msg.summary}")
        elif msg.task == "select" and not msg.ok:
            # Not retried by the background check until something changes (replugged, unlocked...),
            # so a failing connection does not pop up an error every few seconds.
            self.device_var.set(f"{msg.summary}\nPress Rescan, or unplug and plug it back in.")

    def _show_failures(self, summary: str, failures: list[str]) -> None:
        win = tk.Toplevel(self.root)
        win.title("Export finished with problems")
        ttk.Label(win, text=summary).pack(anchor="w", padx=10, pady=6)
        txt = tk.Text(win, width=90, height=20, wrap="none")
        txt.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        txt.insert("1.0", "\n".join(failures))
        txt.configure(state="disabled")


def run_gui() -> None:
    root = tk.Tk()
    try:
        ttk.Style(root).theme_use("vista")
    except tk.TclError:
        pass
    MainWindow(root)
    root.mainloop()
