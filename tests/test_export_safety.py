# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Regression tests for destination ownership, durable resume and disconnect edge cases."""

import asyncio
import os

import pytest

from app.cancel import Cancelled, CancelToken
from export import manifest as mf
from export.exporter import Exporter
from export.manifest import Manifest
from export.planner import ExportOptions, build_plan
from iphone.device import DeviceConnectionLost
from tests.fakes import FakeIPhone
from tests.test_manifest import KEY, _album_id, _asset, _export, _setup

MB = 1024 * 1024


def _run(dev, idx, album_ids, dest, key=KEY, options=None, cancel=None, reconnect=None):
    options = options or ExportOptions()
    cancel = cancel or CancelToken()

    async def go():
        plan = await build_plan(dev, idx, album_ids, options, cancel, lambda *a: None)
        m = Manifest(dest)

        async def default_reconnect():
            await dev.connect()

        try:
            return await Exporter(dev, m, str(dest), key, options, lambda p: None, lambda s: None, cancel,
                                  reconnect or default_reconnect).run(plan)
        finally:
            m.close()

    return asyncio.run(go())


def _cancel_after(dev, nbytes):
    tok = CancelToken()
    orig = dev.read
    seen = [0]

    async def read(h, n):
        data = await orig(h, n)
        seen[0] += len(data)
        if seen[0] >= nbytes:
            tok.cancel()
        return data

    dev.read = read
    return tok, lambda: setattr(dev, "read", orig)


def test_two_iphones_same_folder_never_mix_bytes(tmp_path):
    a_data, b_data = os.urandom(10 * MB), os.urandom(10 * MB)
    idx, dev_a = _setup(tmp_path, [_asset(100, "IMG_0001.MOV", kind=1)], [(2, 100)],
                        files={"DCIM/100APPLE/IMG_0001.MOV": a_data})
    dev_b = FakeIPhone(udid="other")
    dev_b.add("DCIM/100APPLE/IMG_0001.MOV", b_data)
    fam = _album_id(idx, "Family")
    dest = tmp_path / "out"

    tok, restore = _cancel_after(dev_a, 4 * MB)
    assert _run(dev_a, idx, [fam], dest, key="A", cancel=tok).cancelled
    restore()
    tok, restore = _cancel_after(dev_b, 8 * MB)
    assert _run(dev_b, idx, [fam], dest, key="B", cancel=tok).cancelled
    restore()
    _run(dev_a, idx, [fam], dest, key="A")
    _run(dev_b, idx, [fam], dest, key="B")

    files = {n: (dest / "Family" / n).read_bytes() for n in os.listdir(dest / "Family")}
    assert sorted(files) == ["IMG_0001.MOV", "IMG_0001_2.MOV"]
    assert sorted(files.values()) == sorted([a_data, b_data])


def test_foreign_part_file_is_never_deleted(tmp_path):
    idx, dev = _setup(tmp_path, [_asset(100, "IMG_0001.JPG")], [(2, 100)],
                      files={"DCIM/100APPLE/IMG_0001.JPG": b"photo" * 100})
    dest = tmp_path / "out"
    (dest / "Family").mkdir(parents=True)
    (dest / "Family" / "IMG_0001.JPG.part").write_bytes(b"someone's download")
    _run(dev, idx, [_album_id(idx, "Family")], dest)
    assert (dest / "Family" / "IMG_0001.JPG.part").read_bytes() == b"someone's download"
    assert (dest / "Family" / "IMG_0001_2.JPG").read_bytes() == b"photo" * 100


def test_same_size_different_content_is_not_adopted(tmp_path):
    original = os.urandom(3 * MB)
    idx, dev = _setup(tmp_path, [_asset(100, "IMG_0001.JPG")], [(2, 100)],
                      files={"DCIM/100APPLE/IMG_0001.JPG": original})
    dest = tmp_path / "out"
    (dest / "Family").mkdir(parents=True)
    imposter = os.urandom(3 * MB)
    (dest / "Family" / "IMG_0001.JPG").write_bytes(imposter)
    s = _run(dev, idx, [_album_id(idx, "Family")], dest)
    assert (dest / "Family" / "IMG_0001.JPG").read_bytes() == imposter
    assert (dest / "Family" / "IMG_0001_2.JPG").read_bytes() == original
    assert s.copied == 1 and s.skipped == 0


def test_identical_unrecorded_file_is_adopted(tmp_path):
    # e.g. the manifest was deleted, or the files were copied there earlier by hand.
    original = os.urandom(3 * MB)
    idx, dev = _setup(tmp_path, [_asset(100, "IMG_0001.JPG")], [(2, 100)],
                      files={"DCIM/100APPLE/IMG_0001.JPG": original})
    dest = tmp_path / "out"
    (dest / "Family").mkdir(parents=True)
    (dest / "Family" / "IMG_0001.JPG").write_bytes(original)
    s = _run(dev, idx, [_album_id(idx, "Family")], dest)
    assert s.skipped == 1 and s.copied == 0
    assert os.listdir(dest / "Family") == ["IMG_0001.JPG"]


def test_resume_trusts_only_fsynced_offset(tmp_path):
    data = os.urandom(6 * MB)
    idx, dev = _setup(tmp_path, [_asset(100, "IMG_0001.MOV", kind=1)], [(2, 100)],
                      files={"DCIM/100APPLE/IMG_0001.MOV": data})
    fam = _album_id(idx, "Family")
    dest = tmp_path / "out"
    tok, restore = _cancel_after(dev, 4 * MB)
    _run(dev, idx, [fam], dest, cancel=tok)
    restore()
    part = dest / "Family" / "IMG_0001.MOV.part"
    # Simulate a power loss: the file grew but its tail never reached the disk intact,
    # and the manifest only recorded an earlier durable offset.
    with open(part, "r+b") as f:
        f.seek(2 * MB)
        f.write(b"\0" * (3 * MB))
    m = Manifest(dest)
    m.set_state(KEY, fam, 100, "primary", mf.COPYING, bytes_transferred=2 * MB)
    m.close()
    _run(dev, idx, [fam], dest)
    assert (dest / "Family" / "IMG_0001.MOV").read_bytes() == data


def test_changed_source_path_restarts(tmp_path):
    data = os.urandom(5 * MB)
    idx, dev = _setup(tmp_path, [_asset(100, "IMG_0001.MOV", kind=1)], [(2, 100)],
                      files={"DCIM/100APPLE/IMG_0001.MOV": data})
    fam = _album_id(idx, "Family")
    dest = tmp_path / "out"
    tok, restore = _cancel_after(dev, 2 * MB)
    _run(dev, idx, [fam], dest, cancel=tok)
    restore()
    m = Manifest(dest)
    e = m.get(KEY, fam, 100, "primary")
    m.upsert(KEY, fam, "Family", 100, "primary", remote_path="DCIM/999APPLE/OTHER.MOV", remote_size=e.remote_size,
             remote_mtime=e.remote_mtime, dest_path=e.dest_path, state=mf.COPYING,
             bytes_transferred=e.bytes_transferred)
    m.close()
    before = dev.bytes_read
    _run(dev, idx, [fam], dest)
    assert (dest / "Family" / "IMG_0001.MOV").read_bytes() == data
    assert dev.bytes_read - before == len(data)       # restarted from zero


def test_second_disconnect_during_restat_does_not_abort(tmp_path):
    data = os.urandom(6 * MB)
    idx, dev = _setup(tmp_path, [_asset(100, "IMG_0001.MOV", kind=1), _asset(101, "IMG_0002.JPG")],
                      [(2, 100), (2, 101)],
                      files={"DCIM/100APPLE/IMG_0001.MOV": data, "DCIM/100APPLE/IMG_0002.JPG": b"j" * 10})
    dev.fail_after_bytes = 3 * MB
    reconnects = [0]
    orig_stat = dev.stat

    async def flaky_stat(path):
        # The cable drops again right after the first reconnect.
        if reconnects[0] == 1 and path.endswith("IMG_0001.MOV"):
            reconnects[0] += 1
            await dev.disconnect()
            raise DeviceConnectionLost("USB connection lost")
        return await orig_stat(path)

    dev.stat = flaky_stat

    async def reconnect():
        reconnects[0] += 1
        await dev.connect()

    s = _run(dev, idx, [_album_id(idx, "Family")], tmp_path / "out", reconnect=reconnect)
    assert not s.cancelled and not s.failures and s.copied == 2
    assert (tmp_path / "out" / "Family" / "IMG_0001.MOV").read_bytes() == data


def test_many_disconnects_with_progress_still_complete(tmp_path):
    # Each attempt copies one 4 MiB chunk before the cable drops again; 7 drops in total
    # exceeds the no-progress limit (5), so this only completes if progress resets the count.
    data = os.urandom(32 * MB)
    idx, dev = _setup(tmp_path, [_asset(100, "IMG_0001.MOV", kind=1)], [(2, 100)],
                      files={"DCIM/100APPLE/IMG_0001.MOV": data})
    drops = [0]

    async def reconnect():
        drops[0] += 1
        await dev.connect()
        if drops[0] < 7:
            dev.fail_after_bytes = 5 * MB

    dev.fail_after_bytes = 5 * MB
    s = _run(dev, idx, [_album_id(idx, "Family")], tmp_path / "out", reconnect=reconnect)
    assert drops[0] == 7 and not s.failures
    assert (tmp_path / "out" / "Family" / "IMG_0001.MOV").read_bytes() == data


def test_skip_off_does_not_multiply_copies(tmp_path):
    idx, dev = _setup(tmp_path, [_asset(100, "IMG_0001.JPG")], [(2, 100)],
                      files={"DCIM/100APPLE/IMG_0001.JPG": b"x" * 1000})
    dest = tmp_path / "out"
    opts = ExportOptions(skip_existing=False)
    for _ in range(3):
        s = _run(dev, idx, [_album_id(idx, "Family")], dest, options=opts)
        assert s.copied == 1
    assert os.listdir(dest / "Family") == ["IMG_0001.JPG"]


def test_part_link_is_never_followed(tmp_path):
    _winapi = pytest.importorskip("_winapi")
    idx, dev = _setup(tmp_path, [_asset(100, "IMG_0001.JPG")], [(2, 100)],
                      files={"DCIM/100APPLE/IMG_0001.JPG": b"x" * 1000})
    dest = tmp_path / "out"
    outside = tmp_path / "outside"
    (dest / "Family").mkdir(parents=True)
    outside.mkdir()
    try:
        _winapi.CreateJunction(str(outside), str(dest / "Family" / "IMG_0001.JPG.part"))
    except OSError as e:
        pytest.skip(str(e))
    s = _run(dev, idx, [_album_id(idx, "Family")], dest)
    assert not os.listdir(outside)
    assert (dest / "Family" / "IMG_0001_2.JPG").read_bytes() == b"x" * 1000 and not s.failures


def test_listing_failure_is_reported(tmp_path):
    from iphone.device import RemoteFileError
    idx, dev = _setup(tmp_path, [_asset(100, "IMG_0001.JPG")], [(2, 100)],
                      files={"DCIM/100APPLE/IMG_0001.JPG": b"x"})

    async def bad_listdir(path):
        raise RemoteFileError("AFC error 8")

    dev.listdir = bad_listdir
    s = _run(dev, idx, [_album_id(idx, "Family")], tmp_path / "out")
    assert any("could not list folder" in f for f in s.failures)
    assert s.copied == 1


def test_cancel_during_reconnect_wait(tmp_path):
    data = os.urandom(4 * MB)
    idx, dev = _setup(tmp_path, [_asset(100, "IMG_0001.MOV", kind=1)], [(2, 100)],
                      files={"DCIM/100APPLE/IMG_0001.MOV": data})
    dev.fail_after_bytes = MB

    async def reconnect():
        raise Cancelled()

    s = _run(dev, idx, [_album_id(idx, "Family")], tmp_path / "out", reconnect=reconnect)
    assert s.cancelled and not s.failures
    part = tmp_path / "out" / "Family" / "IMG_0001.MOV.part"
    assert data.startswith(part.read_bytes())
