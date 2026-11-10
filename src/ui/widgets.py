# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Small Tk/ttk helpers."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable


class ProgressDialog(tk.Toplevel):
    """Modal progress pop-up with a Cancel button.

    While it is open it holds the input grab, so the main window cannot be used.
    Closing the pop-up with the window's X counts as Cancel.
    """

    def __init__(self, master: tk.Misc, title: str, on_cancel: Callable[[], None],
                 cancel_text: str = "Cancel") -> None:
        super().__init__(master)
        self.title(title)
        self.transient(master)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self._on_cancel = on_cancel
        self.cancelling = False

        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        self.status_var = tk.StringVar(value=title)
        ttk.Label(body, textvariable=self.status_var, font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.detail_var = tk.StringVar(value="")
        ttk.Label(body, textvariable=self.detail_var, justify="left", width=62).pack(anchor="w", pady=(4, 8))
        self.bar = ttk.Progressbar(body, mode="determinate", maximum=1000, length=460)
        self.bar.pack(fill="x")
        self._cancel_text = cancel_text
        self.cancel_btn = ttk.Button(body, text=cancel_text, command=self._cancel)
        self.cancel_btn.pack(anchor="e", pady=(12, 0))

        self.update_idletasks()
        x = master.winfo_rootx() + max((master.winfo_width() - self.winfo_width()) // 2, 0)
        y = master.winfo_rooty() + max((master.winfo_height() - self.winfo_height()) // 3, 0)
        self.geometry(f"+{x}+{y}")
        self.grab_set()
        self.cancel_btn.focus_set()

    def set_status(self, text: str) -> None:
        if not self.cancelling:
            self.status_var.set(text)

    def set_detail(self, text: str) -> None:
        self.detail_var.set(text)

    def set_fraction(self, frac: float | None) -> None:
        """A fraction 0..1, or None for an indeterminate (busy) bar."""
        if frac is None:
            if str(self.bar.cget("mode")) != "indeterminate":
                self.bar.configure(mode="indeterminate")
                self.bar.start(15)
            return
        if str(self.bar.cget("mode")) != "determinate":
            self.bar.stop()
            self.bar.configure(mode="determinate")
        self.bar.configure(value=int(min(max(frac, 0.0), 1.0) * 1000))

    def _cancel(self) -> None:
        if self.cancelling:
            return
        self.cancelling = True
        self.status_var.set("Stopping after the current chunk..." if self._cancel_text == "Stop"
                            else "Cancelling...")
        self.cancel_btn.state(["disabled"])
        self._on_cancel()

    def close(self) -> None:
        try:
            self.bar.stop()
            self.grab_release()
        finally:
            self.destroy()

UNCHECKED, CHECKED, PARTIAL = "☐", "☑", "▣"


def visible_albums(albums, hide_empty: bool) -> list:
    """With hide_empty, drop albums with no items and folders that contain no non-empty album."""
    if not hide_empty:
        return list(albums)
    by_id = {a.id: a for a in albums}
    keep: set[int] = set()
    for a in albums:
        if a.is_folder or a.item_count <= 0:
            continue
        cur, seen = a, set()
        while cur is not None and cur.id not in seen:
            seen.add(cur.id)
            keep.add(cur.id)
            cur = by_id.get(cur.parent_id) if cur.parent_id is not None else None
    return [a for a in albums if a.id in keep]


class CheckTree(ttk.Frame):
    """A Treeview with tri-state checkboxes.  Checking a folder checks all its albums."""

    def __init__(self, master: tk.Misc, **kw) -> None:
        super().__init__(master, **kw)
        self.tree = ttk.Treeview(self, columns=("count",), show="tree headings", selectmode="none", height=10)
        self.tree.heading("#0", text="Albums", anchor="w")
        self.tree.heading("count", text="Items", anchor="e")
        self.tree.column("#0", width=380, stretch=True)
        self.tree.column("count", width=80, anchor="e", stretch=False)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self._state: dict[str, str] = {}
        self._title: dict[str, str] = {}
        self._album_ids: dict[str, int] = {}     # iid -> album id (albums only)
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", self._on_space)

    def clear(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._state.clear()
        self._title.clear()
        self._album_ids.clear()

    def load(self, albums) -> None:
        """albums: photos.models.Album list including folders, sorted by full_path."""
        self.clear()
        by_id = {a.id: a for a in albums}
        for a in albums:
            parent = f"a{a.parent_id}" if a.parent_id in by_id else ""
            if parent and not self.tree.exists(parent):
                parent = ""      # malformed hierarchy: show it at top level rather than fail
            iid = f"a{a.id}"
            count = "" if a.is_folder else f"{a.item_count:,}"
            self._title[iid] = a.title
            self._state[iid] = UNCHECKED
            self.tree.insert(parent, "end", iid=iid, text=f"{UNCHECKED} {a.title}", values=(count,), open=True)
            if not a.is_folder:
                self._album_ids[iid] = a.id

    def selected_album_ids(self) -> list[int]:
        return [aid for iid, aid in self._album_ids.items() if self._state.get(iid) == CHECKED]

    def set_checked(self, album_ids: set[int]) -> None:
        for iid, aid in self._album_ids.items():
            self._set(iid, CHECKED if aid in album_ids else UNCHECKED)
        for top in self.tree.get_children():
            self._recompute(top)

    def set_all(self, checked: bool) -> None:
        """Tick (or untick) every album currently shown in the list."""
        state = CHECKED if checked else UNCHECKED
        for top in self.tree.get_children():
            self._set_subtree(top, state)
        self.event_generate("<<CheckChanged>>")

    def _set(self, iid: str, state: str) -> None:
        self._state[iid] = state
        self.tree.item(iid, text=f"{state} {self._title[iid]}")

    def _set_subtree(self, iid: str, state: str) -> None:
        self._set(iid, state)
        for c in self.tree.get_children(iid):
            self._set_subtree(c, state)

    def _recompute(self, iid: str) -> str:
        kids = self.tree.get_children(iid)
        if not kids:
            return self._state[iid]
        states = {self._recompute(c) for c in kids}
        if iid in self._album_ids:          # an album that also has children (not expected)
            return self._state[iid]
        new = states.pop() if len(states) == 1 and PARTIAL not in states else PARTIAL
        self._set(iid, new)
        return new

    def _toggle(self, iid: str) -> None:
        new = UNCHECKED if self._state.get(iid) == CHECKED else CHECKED
        self._set_subtree(iid, new)
        for top in self.tree.get_children():
            self._recompute(top)
        self.event_generate("<<CheckChanged>>")

    def _on_click(self, event) -> str | None:
        if self.tree.identify_region(event.x, event.y) not in ("tree", "cell"):
            return None
        iid = self.tree.identify_row(event.y)
        if not iid:
            return None
        if self.tree.identify_element(event.x, event.y) == "Treeitem.indicator":
            return None   # expand/collapse arrow
        self._toggle(iid)
        return "break"

    def _on_space(self, _event) -> str:
        iid = self.tree.focus()
        if iid:
            self._toggle(iid)
        return "break"
