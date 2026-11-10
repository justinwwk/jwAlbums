# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

import os

import pytest

from export.path_safety import (UnsafePathError, assert_within_root, device_asset_path, numbered_name, safe_join,
                                sanitize_component, validate_device_path)


@pytest.mark.parametrize("raw,expected", [
    ("Family", "Family"),
    ('a\\b/c:d*e?f"g<h>i|j', "a_b_c_d_e_f_g_h_i_j"),
    ("Trip.  ", "Trip"),
    ("Trip...", "Trip"),
    ("  spaced  ", "spaced"),
    ("", "Untitled"),
    (None, "Untitled"),
    ("..", "Untitled"),
    ("tab\there", "tab_here"),
])
def test_sanitize_component(raw, expected):
    assert sanitize_component(raw) == expected


@pytest.mark.parametrize("name", ["CON", "con", "PRN", "AUX", "NUL", "COM1", "com9", "LPT1", "LPT9", "CON.txt",
                                  "nul.jpg", "COM3 "])
def test_reserved_names(name):
    out = sanitize_component(name)
    assert out.startswith("_")
    assert out.split(".")[0].upper().strip("_ ") == name.split(".")[0].upper().strip()


def test_not_reserved_lookalikes():
    assert sanitize_component("CONTACTS") == "CONTACTS"
    assert sanitize_component("COM10") == "COM10"


def test_long_names_truncated_keep_extension():
    s = sanitize_component("x" * 300 + ".HEIC")
    assert len(s) <= 120 and s.endswith(".HEIC")


def test_numbered_name():
    assert numbered_name("IMG_1234.JPG", 2) == "IMG_1234_2.JPG"
    assert numbered_name("noext", 3) == "noext_3"
    long = sanitize_component("x" * 300 + ".HEIC")
    n = numbered_name(long, 12)
    assert len(n) <= 120 and n.endswith("_12.HEIC") and sanitize_component(n) == n


def test_sanitize_fuzz_idempotent_and_joinable(tmp_path):
    import random
    rng = random.Random(1234)
    alphabet = list("abcXYZ019 ._-") + [" ", "　", "​", "\\", "/", ":", "*", "?", '"', "<", ">",
                                        "|", "\t", "é", "日", "😀", "CON", "nul", "COM1"]
    for _ in range(20000):
        raw = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 160)))
        s = sanitize_component(raw)
        assert s and len(s) <= 120
        assert sanitize_component(s) == s, repr(raw)
        safe_join(tmp_path, s)
        safe_join(tmp_path, numbered_name(s, 99))


@pytest.mark.parametrize("bad", ["..", "../x", "a/../b", "C:", "C:\\x", "\\\\server\\share", "/abs", "x\x00y",
                                 "a/b", "a\\b"])
def test_safe_join_rejects_bad_components(tmp_path, bad):
    with pytest.raises(UnsafePathError):
        safe_join(tmp_path, bad)


def test_safe_join_ok(tmp_path):
    p = safe_join(tmp_path, "Travel", "Japan", "IMG_1.JPG")
    assert p == os.path.join(os.path.realpath(tmp_path), "Travel", "Japan", "IMG_1.JPG")


def test_sanitized_album_cannot_escape(tmp_path):
    comps = [sanitize_component(x) for x in ["..", "..\\..\\Windows", "C:\\Users", "\\\\evil\\share"]]
    p = safe_join(tmp_path, *comps)
    assert_within_root(p, tmp_path)


def test_junction_escape_rejected(tmp_path):
    _winapi = pytest.importorskip("_winapi")
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    try:
        _winapi.CreateJunction(str(outside), str(root / "Album"))
    except OSError as e:
        pytest.skip(f"cannot create junction: {e}")
    with pytest.raises(UnsafePathError):
        safe_join(root, "Album", "IMG_1.JPG")
    with pytest.raises(UnsafePathError):
        assert_within_root(root / "Album", root)


# ---------------------------------------------------------------- device-side paths

@pytest.mark.parametrize("good,norm", [
    ("DCIM/100APPLE/IMG_1.JPG", "DCIM/100APPLE/IMG_1.JPG"),
    ("DCIM//100APPLE/./IMG_1.JPG", "DCIM/100APPLE/IMG_1.JPG"),
    ("PhotoData/Photos.sqlite", "PhotoData/Photos.sqlite"),
    ("DCIM", "DCIM"),
])
def test_device_path_ok(good, norm):
    assert validate_device_path(good) == norm


@pytest.mark.parametrize("bad", ["", "/var/mobile/Media/DCIM/x", "DCIM/../Library/x", "../DCIM", "Downloads/x",
                                 "C:/x", "DCIM\\100APPLE\\x", "Books/x", "DCIM/a\x00b", "iTunes_Control/x"])
def test_device_path_rejected(bad):
    with pytest.raises(UnsafePathError):
        validate_device_path(bad)


def test_device_asset_path():
    assert device_asset_path("DCIM/100APPLE", "IMG_1.JPG") == "DCIM/100APPLE/IMG_1.JPG"
    assert device_asset_path("/var/mobile/Media/DCIM/100APPLE", "IMG_1.JPG") == "DCIM/100APPLE/IMG_1.JPG"
    for d, f in [("DCIM/100APPLE", "../x"), ("DCIM/100APPLE", "a/b"), ("/etc", "passwd"), (None, "x"),
                 ("DCIM", ".."), ("DCIM/100APPLE", "")]:
        with pytest.raises(UnsafePathError):
            device_asset_path(d, f)
