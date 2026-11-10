# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Plain data models for the compact album index."""

from __future__ import annotations

from dataclasses import dataclass

KIND_USER_ALBUM = 2
KIND_ROOT_FOLDER = 3999
KIND_FOLDER = 4000

ASSET_KIND_PHOTO = 0
ASSET_KIND_VIDEO = 1

# Core Data stores dates as seconds since 2001-01-01T00:00:00Z.
CORE_DATA_EPOCH_OFFSET = 978307200


@dataclass(frozen=True)
class Album:
    id: int
    title: str
    kind: int
    parent_id: int | None        # parent folder album id (None at top level)
    full_path: tuple[str, ...]   # titles from the top-level folder down to this album
    item_count: int

    @property
    def is_folder(self) -> bool:
        return self.kind == KIND_FOLDER


@dataclass(frozen=True)
class Asset:
    id: int
    directory: str
    filename: str
    device_path: str | None      # validated AFC path, None if unsafe/unresolvable
    created_timestamp: float | None
    trashed: bool
    hidden: bool
    kind: int                    # 0 photo, 1 video
    file_size: int | None
    adjusted: bool = False       # edited on the iPhone (crop, filter, trim...)


@dataclass(frozen=True)
class FileFingerprint:
    size: int
    mtime: float

    def to_dict(self) -> dict:
        return {"size": self.size, "mtime": self.mtime}

    @classmethod
    def from_dict(cls, d: dict | None) -> "FileFingerprint | None":
        if not d:
            return None
        return cls(int(d["size"]), float(d["mtime"]))


@dataclass(frozen=True)
class RemoteFingerprint:
    db: FileFingerprint
    wal: FileFingerprint | None

    def to_dict(self) -> dict:
        return {"db": self.db.to_dict(), "wal": self.wal.to_dict() if self.wal else None}

    @classmethod
    def from_dict(cls, d: dict) -> "RemoteFingerprint":
        db = FileFingerprint.from_dict(d["db"])
        assert db is not None
        return cls(db, FileFingerprint.from_dict(d.get("wal")))


def core_data_to_unix(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value) + CORE_DATA_EPOCH_OFFSET
    except (TypeError, ValueError):
        return None
