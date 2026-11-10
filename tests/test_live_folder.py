# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Live Photo motion clips are exported to <album>/live_videos/, never mixed with the photos."""

import os

from export import manifest as mf
from export.manifest import Manifest
from tests.test_manifest import KEY, _album_id, _asset, _export, _setup

D = "DCIM/100APPLE"


def _live_setup(tmp_path):
    files = {f"{D}/IMG_0100.HEIC": b"h" * 1000, f"{D}/IMG_0100.MOV": b"m" * 3000,
             f"{D}/IMG_0101.HEIC": b"k" * 1200, f"{D}/IMG_0101.MOV": b"n" * 2000}
    return _setup(tmp_path, [_asset(100, "IMG_0100.HEIC"), _asset(101, "IMG_0101.HEIC")],
                  [(2, 100), (2, 101)], files=files)


def _to_old_layout(dest, idx):
    """Make the export look like one made before live_videos/ existed (clips beside photos)."""
    fam_id = _album_id(idx, "Family")
    fam = dest / "Family"
    m = Manifest(dest)
    try:
        for asset, name in [(100, "IMG_0100.MOV"), (101, "IMG_0101.MOV")]:
            e = m.get(KEY, fam_id, asset, "live")
            os.replace(fam / "live_videos" / name, fam / name)
            m.upsert(KEY, fam_id, "Family", asset, "live", remote_path=e.remote_path, remote_size=e.remote_size,
                     remote_mtime=e.remote_mtime, dest_path=os.path.join("Family", name), state=mf.DONE,
                     bytes_transferred=e.remote_size)
    finally:
        m.close()
    os.rmdir(fam / "live_videos")
    return fam


def test_clips_from_older_exports_are_moved_into_live_videos(tmp_path):
    idx, dev = _live_setup(tmp_path)
    dest = tmp_path / "out"
    _export(dev, idx, [_album_id(idx, "Family")], dest)
    fam = _to_old_layout(dest, idx)
    created = os.stat(fam / "IMG_0100.MOV").st_mtime

    s, _, _ = _export(dev, idx, [_album_id(idx, "Family")], dest)
    assert sorted(os.listdir(fam)) == ["IMG_0100.HEIC", "IMG_0101.HEIC", "live_videos"]
    assert sorted(os.listdir(fam / "live_videos")) == ["IMG_0100.MOV", "IMG_0101.MOV"]
    assert (fam / "live_videos" / "IMG_0100.MOV").read_bytes() == b"m" * 3000
    assert os.stat(fam / "live_videos" / "IMG_0100.MOV").st_mtime == created    # moved, not re-copied
    assert (s.copied, s.skipped) == (0, 4) and not s.failures

    s, _, _ = _export(dev, idx, [_album_id(idx, "Family")], dest)              # stable afterwards
    assert (s.copied, s.skipped) == (0, 4)


def test_user_changed_clip_is_left_where_it_is(tmp_path):
    idx, dev = _live_setup(tmp_path)
    dest = tmp_path / "out"
    _export(dev, idx, [_album_id(idx, "Family")], dest)
    fam = _to_old_layout(dest, idx)
    (fam / "IMG_0100.MOV").write_bytes(b"trimmed by me")                      # not ours any more

    _export(dev, idx, [_album_id(idx, "Family")], dest)
    assert (fam / "IMG_0100.MOV").read_bytes() == b"trimmed by me"            # untouched, not moved
    assert (fam / "live_videos" / "IMG_0100.MOV").read_bytes() == b"m" * 3000  # fresh copy instead
    assert not (fam / "IMG_0101.MOV").exists()                                # ours: moved


def test_move_never_overwrites_a_file_already_in_live_videos(tmp_path):
    idx, dev = _live_setup(tmp_path)
    dest = tmp_path / "out"
    _export(dev, idx, [_album_id(idx, "Family")], dest)
    fam = _to_old_layout(dest, idx)
    (fam / "live_videos").mkdir()
    (fam / "live_videos" / "IMG_0100.MOV").write_bytes(b"someone else's")

    s, _, _ = _export(dev, idx, [_album_id(idx, "Family")], dest)
    assert (fam / "live_videos" / "IMG_0100.MOV").read_bytes() == b"someone else's"
    assert (fam / "IMG_0100.MOV").read_bytes() == b"m" * 3000                 # left in place
    assert not s.failures


def test_live_folder_only_when_live_motion_is_on(tmp_path):
    from export.planner import ExportOptions
    idx, dev = _live_setup(tmp_path)
    dest = tmp_path / "out"
    _export(dev, idx, [_album_id(idx, "Family")], dest, ExportOptions(include_live_video=False))
    assert sorted(os.listdir(dest / "Family")) == ["IMG_0100.HEIC", "IMG_0101.HEIC"]
