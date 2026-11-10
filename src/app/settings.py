# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Local application storage locations and persisted user settings.

Everything lives under %LOCALAPPDATA%\\jwAlbums\\.  Pairing records are
never stored here (they belong to Apple Devices / usbmuxd).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path

log = logging.getLogger(__name__)

APP_DIR_NAME = "jwAlbums"
OLD_APP_DIR_NAME = "iPhoneAlbumExporter"     # name before the rename; moved over on first start


def app_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    root = Path(base) / APP_DIR_NAME
    old = Path(base) / OLD_APP_DIR_NAME
    if not root.exists() and old.is_dir():
        try:
            os.rename(old, root)      # keep settings and album caches from the old name
        except OSError as e:
            log.warning("could not move old app folder (%s); starting fresh", type(e).__name__)
    root.mkdir(parents=True, exist_ok=True)
    return root


def logs_dir() -> Path:
    d = app_root() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def device_dir(udid: str) -> Path:
    """Per-device directory.  The UDID comes from the device, so it is sanitized."""
    safe = re.sub(r"[^A-Za-z0-9-]", "", udid)[:64]
    if not safe:
        raise ValueError("invalid device identifier")
    d = app_root() / safe
    (d / "temp").mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class Settings:
    destination: str = ""
    include_videos: bool = True
    include_live_video: bool = True
    skip_existing: bool = True
    hide_empty_albums: bool = True

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or (app_root() / "settings.json")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError) as e:
            log.warning("settings.json unreadable (%s); using defaults", type(e).__name__)
            return cls()
        known = {f.name: f.type for f in fields(cls)}
        values = {}
        for k, v in raw.items():
            if k in known and isinstance(v, (bool, str)):
                values[k] = v
        return cls(**values)

    def save(self, path: Path | None = None) -> None:
        path = path or (app_root() / "settings.json")
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        os.replace(tmp, path)
