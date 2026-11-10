# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Edited photos/videos are exported as the iPhone shows them (iOS's own full-size render)."""

import os

from export.path_safety import validate_device_path
from photos.index_db import AlbumIndex
from tests.test_manifest import _album_id, _asset, _export, _setup

D = "DCIM/100APPLE"
MUT = "PhotoData/Mutations/DCIM/100APPLE"


def _adjusted(*pks):
    return ("ALTER TABLE ZASSET ADD COLUMN ZADJUSTMENTSSTATE INTEGER DEFAULT 0;"
            f"UPDATE ZASSET SET ZADJUSTMENTSSTATE = 2 WHERE Z_PK IN ({','.join(map(str, pks))});" if pks else
            "ALTER TABLE ZASSET ADD COLUMN ZADJUSTMENTSSTATE INTEGER DEFAULT 0;")


def test_edited_photo_and_live_motion_use_iphone_render(tmp_path):
    files = {f"{D}/IMG_0100.HEIC": b"orig" * 500, f"{D}/IMG_0100.MOV": b"origmov" * 300,
             f"{MUT}/IMG_0100/Adjustments/FullSizeRender.jpg": b"edited" * 400,
             f"{MUT}/IMG_0100/Adjustments/FullSizeRender.mov": b"editedmov" * 200,
             f"{MUT}/IMG_0100/Adjustments/Adjustments.plist": b"<plist/>"}
    idx, dev = _setup(tmp_path, [_asset(100, "IMG_0100.HEIC")], [(2, 100)], files=files, extra_sql=_adjusted(100))
    dest = tmp_path / "out"
    s, _, plan = _export(dev, idx, [_album_id(idx, "Family")], dest)
    fam = dest / "Family"
    assert sorted(os.listdir(fam)) == ["IMG_0100.JPG", "live_videos"]   # no untouched original
    assert (fam / "IMG_0100.JPG").read_bytes() == b"edited" * 400
    assert (fam / "live_videos" / "IMG_0100.MOV").read_bytes() == b"editedmov" * 200
    assert plan.edited == 1 and not plan.edits_missing and not s.failures
    assert abs(os.path.getmtime(fam / "IMG_0100.JPG") - (700_000_000.0 + 978307200)) < 1


def test_edited_video_uses_render(tmp_path):
    files = {f"{D}/IMG_0101.MOV": b"long" * 1000, f"{MUT}/IMG_0101/Adjustments/FullSizeRender.mov": b"trim" * 100}
    idx, dev = _setup(tmp_path, [_asset(101, "IMG_0101.MOV", kind=1)], [(2, 101)], files=files,
                      extra_sql=_adjusted(101))
    dest = tmp_path / "out"
    _export(dev, idx, [_album_id(idx, "Family")], dest)
    assert os.listdir(dest / "Family") == ["IMG_0101.MOV"]
    assert (dest / "Family" / "IMG_0101.MOV").read_bytes() == b"trim" * 100


def test_edited_without_render_falls_back_to_original_and_reports_it(tmp_path):
    files = {f"{D}/IMG_0100.JPG": b"orig" * 500}
    idx, dev = _setup(tmp_path, [_asset(100, "IMG_0100.JPG")], [(2, 100)], files=files, extra_sql=_adjusted(100))
    dest = tmp_path / "out"
    s, _, plan = _export(dev, idx, [_album_id(idx, "Family")], dest)
    assert (dest / "Family" / "IMG_0100.JPG").read_bytes() == b"orig" * 500
    assert plan.edited == 0 and plan.edits_missing == ["Family/IMG_0100.JPG"] and not s.failures


def test_render_of_unedited_photo_is_ignored(tmp_path):
    # A leftover render (e.g. the edit was reverted) must not be used when Photos says "not edited".
    files = {f"{D}/IMG_0100.JPG": b"orig" * 500, f"{MUT}/IMG_0100/Adjustments/FullSizeRender.jpg": b"stale" * 10}
    idx, dev = _setup(tmp_path, [_asset(100, "IMG_0100.JPG")], [(2, 100)], files=files, extra_sql=_adjusted())
    dest = tmp_path / "out"
    _export(dev, idx, [_album_id(idx, "Family")], dest)
    assert (dest / "Family" / "IMG_0100.JPG").read_bytes() == b"orig" * 500
    assert not any(c.startswith("PhotoData") for c in dev.listdir_calls)   # not even looked up


def test_photo_edited_after_export_updates_our_copy_in_place(tmp_path):
    files = {f"{D}/IMG_0100.JPG": os.urandom(3000)}
    (tmp_path / "a").mkdir()
    idx, dev = _setup(tmp_path / "a", [_asset(100, "IMG_0100.JPG")], [(2, 100)], files=files, extra_sql=_adjusted())
    dest = tmp_path / "out"
    _export(dev, idx, [_album_id(idx, "Family")], dest)
    idx.close()

    for i, render in enumerate([os.urandom(2500), os.urandom(2500)]):   # edit, then re-edit (same size)
        dev.add(f"{MUT}/IMG_0100/Adjustments/FullSizeRender.jpg", render, 1_800_000_000.0 + i)
        sub = tmp_path / f"b{i}"
        sub.mkdir()
        idx, _ = _setup(sub, [_asset(100, "IMG_0100.JPG")], [(2, 100)], extra_sql=_adjusted(100))
        s, _, _ = _export(dev, idx, [_album_id(idx, "Family")], dest)
        idx.close()
        assert os.listdir(dest / "Family") == ["IMG_0100.JPG"]           # no IMG_0100_2.JPG
        assert (dest / "Family" / "IMG_0100.JPG").read_bytes() == render
        assert s.copied == 1 and not s.failures

    # Nothing changed since: skipped, not copied again.
    idx = AlbumIndex(tmp_path / "b1" / "idx.sqlite")
    s, _, _ = _export(dev, idx, [_album_id(idx, "Family")], dest)
    idx.close()
    assert (s.copied, s.skipped) == (0, 1)


def test_user_modified_copy_is_never_replaced_by_new_edit(tmp_path):
    files = {f"{D}/IMG_0100.JPG": os.urandom(3000)}
    (tmp_path / "a").mkdir()
    idx, dev = _setup(tmp_path / "a", [_asset(100, "IMG_0100.JPG")], [(2, 100)], files=files, extra_sql=_adjusted())
    dest = tmp_path / "out"
    _export(dev, idx, [_album_id(idx, "Family")], dest)
    idx.close()
    mine = dest / "Family" / "IMG_0100.JPG"
    mine.write_bytes(b"I drew on this photo in Paint")          # user changed our exported copy
    render = os.urandom(2500)
    dev.add(f"{MUT}/IMG_0100/Adjustments/FullSizeRender.jpg", render, 1_800_000_000.0)
    (tmp_path / "b").mkdir()
    idx, _ = _setup(tmp_path / "b", [_asset(100, "IMG_0100.JPG")], [(2, 100)], extra_sql=_adjusted(100))
    _export(dev, idx, [_album_id(idx, "Family")], dest)
    idx.close()
    assert mine.read_bytes() == b"I drew on this photo in Paint"
    assert (dest / "Family" / "IMG_0100_2.JPG").read_bytes() == render


def test_mutations_paths_pass_device_path_validation():
    assert validate_device_path(f"{MUT}/IMG_0100/Adjustments/FullSizeRender.jpg")
