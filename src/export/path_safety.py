# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Validation of untrusted device-derived paths and safe Windows destination paths.

Album titles, ZDIRECTORY/ZFILENAME values and directory listings all come from the
iPhone and are treated as untrusted input.
"""

from __future__ import annotations

import os
import re
import unicodedata

ALLOWED_DEVICE_ROOTS = ("DCIM", "PhotoData")

_INVALID_WIN_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL",
             *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10)),
             "COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³", "CONIN$", "CONOUT$"}
MAX_COMPONENT = 120


class UnsafePathError(ValueError):
    pass


# --------------------------------------------------------------------------- device side

def validate_device_path(path: str) -> str:
    """Return a normalized AFC path below an allowed root, or raise UnsafePathError."""
    if not isinstance(path, str) or not path or "\x00" in path:
        raise UnsafePathError("empty or invalid device path")
    if "\\" in path:
        raise UnsafePathError("backslash in device path")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise UnsafePathError("absolute device path")
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if not parts:
        raise UnsafePathError("empty device path")
    if any(p == ".." for p in parts):
        raise UnsafePathError("path traversal in device path")
    if parts[0] not in ALLOWED_DEVICE_ROOTS:
        raise UnsafePathError(f"device path outside allowed roots: {parts[0]!r}")
    return "/".join(parts)


def device_asset_path(directory: str | None, filename: str | None) -> str:
    """Combine ZDIRECTORY + ZFILENAME into a validated AFC path."""
    if not filename or "/" in filename or "\\" in filename or filename in (".", ".."):
        raise UnsafePathError("invalid asset filename")
    directory = (directory or "").strip()
    if directory.startswith("/var/mobile/Media/"):
        directory = directory[len("/var/mobile/Media/"):]
    elif directory.startswith("/"):
        raise UnsafePathError("absolute asset directory")
    if not directory:
        raise UnsafePathError("missing asset directory")
    return validate_device_path(f"{directory}/{filename}")


# --------------------------------------------------------------------------- Windows side

def sanitize_component(name: str | None, fallback: str = "Untitled") -> str:
    """Make one untrusted name safe to use as a single Windows path component."""
    s = unicodedata.normalize("NFC", name or "")
    s = _INVALID_WIN_CHARS.sub("_", s)
    # Repeat until stable so the result is idempotent (safe_join relies on that):
    # truncation can expose new trailing spaces/dots, stripping can expose a reserved name.
    for _ in range(8):
        before = s
        s = _trim(s)
        if s in ("", ".", ".."):
            s = fallback
        if len(s) > MAX_COMPONENT:
            root, ext = os.path.splitext(s)
            if len(ext) > 16 or not root:
                root, ext = s, ""
            s = _trim(root[: MAX_COMPONENT - len(ext) - 1]) + ext
        stem = s.split(".", 1)[0].rstrip(" ").upper()
        if stem in _RESERVED:
            s = "_" + s
        if s == before:
            break
    return s


def _trim(s: str) -> str:
    prev = None
    while prev != s:
        prev = s
        s = s.strip().rstrip(" .")
    return s


def _is_within(child: str, root: str) -> bool:
    child_n, root_n = os.path.normcase(child), os.path.normcase(root)
    try:
        return os.path.commonpath([child_n, root_n]) == root_n
    except ValueError:  # different drives
        return False


def safe_join(root: str | os.PathLike, *components: str) -> str:
    """Join already-sanitized components below root and prove the result stays below it.

    Both sides are resolved with realpath, so a symlink or junction anywhere in the
    existing part of the chain that points outside the root is rejected.
    """
    root_real = os.path.realpath(os.fspath(root))
    for c in components:
        if not c or c in (".", "..") or _INVALID_WIN_CHARS.search(c) or os.path.isabs(c) or c != sanitize_component(c):
            raise UnsafePathError(f"unsafe path component {c!r}")
    candidate = os.path.join(root_real, *components)
    if not _is_within(os.path.abspath(candidate), root_real):
        raise UnsafePathError("path escapes destination root")
    if not _is_within(os.path.realpath(candidate), root_real):
        raise UnsafePathError("path escapes destination root through a link")
    return candidate


def assert_within_root(path: str | os.PathLike, root: str | os.PathLike) -> None:
    root_real = os.path.realpath(os.fspath(root))
    if not _is_within(os.path.realpath(os.fspath(path)), root_real):
        raise UnsafePathError("path escapes destination root")


def numbered_name(filename: str, n: int) -> str:
    """IMG_1234.JPG, 2 -> IMG_1234_2.JPG (kept within the component length limit)."""
    stem, ext = os.path.splitext(filename)
    suffix = f"_{n}"
    room = MAX_COMPONENT - len(ext) - len(suffix)
    if room < 1:
        stem, ext, room = filename, "", MAX_COMPONENT - len(suffix)
    return sanitize_component(stem[:room] + suffix + ext)


def is_regular_file_or_absent(path: str) -> bool:
    """False for links, junctions, directories and other non-regular entries."""
    if not os.path.lexists(path):
        return True
    if os.path.islink(path) or (hasattr(os.path, "isjunction") and os.path.isjunction(path)):
        return False
    return os.path.isfile(path)
