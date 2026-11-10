# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Messages passed from the worker thread to the Tkinter thread via queue.Queue."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Status:
    text: str
    busy: bool = True


@dataclass(frozen=True)
class DeviceList:
    devices: list[Any]          # list[iphone.connection.DeviceCandidate]
    usbmux_ok: bool
    error: str | None = None
    auto: bool = False          # from the background check while no iPhone is connected


@dataclass(frozen=True)
class DeviceSelected:
    info: Any                   # iphone.device.DeviceInfo


@dataclass(frozen=True)
class UsbInfo:
    headline: str
    lines: list[str]
    warning: str | None


@dataclass(frozen=True)
class BenchmarkResult:
    mb_per_s: float | None
    note: str


@dataclass(frozen=True)
class AlbumsLoaded:
    albums: list[Any]           # list[photos.models.Album], includes folders
    from_cache: bool


@dataclass(frozen=True)
class Progress:
    phase: str                  # "snapshot" | "prepare" | "export" | "benchmark"
    done_bytes: int = 0
    total_bytes: int = 0
    done_items: int = 0
    total_items: int = 0
    bytes_per_s: float = 0.0
    album: str = ""
    current: str = ""


@dataclass(frozen=True)
class Error:
    text: str


@dataclass(frozen=True)
class TaskDone:
    task: str
    ok: bool
    summary: str = ""
    failures: list[str] = field(default_factory=list)
