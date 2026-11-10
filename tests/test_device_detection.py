# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""The window finds an iPhone by itself, and Rescan visibly does something."""

import time

import pytest

from app import messages as m
from iphone import connection
from iphone.connection import DeviceCandidate

PHONE = DeviceCandidate("00008140-000A1B2C3D4E5F60", "iPhone", "iPhone17,1", "iPhone 16 Pro", "26.5", "Connected", True)


@pytest.fixture
def window(tmp_path, monkeypatch):
    tk = pytest.importorskip("tkinter")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    import ui.main_window as mw
    monkeypatch.setattr(mw, "AUTO_SCAN_MS", 50)

    state = {"devices": [], "selects": 0}

    async def describe_devices():
        return True, list(state["devices"])

    async def failing_select(self, udid):
        state["selects"] += 1
        self.post(m.TaskDone("select", False, "Unlock iPhone"))

    monkeypatch.setattr(connection, "describe_devices", describe_devices)
    monkeypatch.setattr(mw.Worker, "_select", failing_select)
    root.withdraw()
    win = mw.MainWindow(root)

    def pump(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            root.update()
            time.sleep(0.01)

    yield win, state, pump
    win.worker.shutdown()
    root.destroy()


def test_rescan_feedback_auto_detection_and_no_retry_loop(window):
    # One window for the whole scenario: Tk does not reliably support a second Tk() per process.
    win, state, pump = window
    pump(0.5)
    text = win.device_var.get()
    assert text.startswith("No iPhone connected (checked ") and "unlock it" in text

    win._rescan()
    assert win.device_var.get() == "Looking for iPhone..."          # immediate feedback
    pump(0.5)
    assert win.device_var.get().startswith("No iPhone connected (checked ")
    assert state["selects"] == 0

    state["devices"] = [PHONE]                                        # user plugs the iPhone in
    pump(0.6)
    assert state["selects"] == 1                                      # found by itself
    assert "Unlock iPhone" in win.device_var.get()
    pump(0.6)
    assert state["selects"] == 1                                      # no retry loop / error pop-ups

    win._rescan()                                                     # Rescan retries explicitly
    pump(0.6)
    assert state["selects"] == 2

    state["devices"] = []                                             # unplug and plug back in
    pump(0.4)
    state["devices"] = [PHONE]
    pump(0.6)
    assert state["selects"] == 3
