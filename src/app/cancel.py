# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Cooperative cancellation shared between the Tk thread and the worker loop."""

from __future__ import annotations

import threading


class Cancelled(Exception):
    pass


class CancelToken:
    def __init__(self) -> None:
        self._ev = threading.Event()

    def cancel(self) -> None:
        self._ev.set()

    def is_set(self) -> bool:
        return self._ev.is_set()

    def raise_if_cancelled(self) -> None:
        if self._ev.is_set():
            raise Cancelled()
