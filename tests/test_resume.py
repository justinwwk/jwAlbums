# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import os

import pytest

from app.cancel import Cancelled, CancelToken
from export.resume import PartAction, decide_part
from export.transfer import stream_copy
from iphone.device import DeviceConnectionLost, RemoteFileError
from tests.fakes import FakeIPhone

FP = (1000, 5.0)


@pytest.mark.parametrize("part,recorded,current,action,offset", [
    (None, None, FP, PartAction.FRESH, 0),
    (0, FP, FP, PartAction.FRESH, 0),
    (400, FP, FP, PartAction.RESUME, 400),
    (1000, FP, FP, PartAction.RESUME, 1000),          # complete .part: nothing left but verify+rename
    (1200, FP, FP, PartAction.RESTART, 0),            # larger than remote
    (400, (1000, 6.0), FP, PartAction.RESTART, 0),    # remote mtime changed
    (400, (999, 5.0), FP, PartAction.RESTART, 0),     # remote size changed
    (400, None, FP, PartAction.RESTART, 0),           # no record of where the .part came from
])
def test_decide_part(part, recorded, current, action, offset):
    d = decide_part(part, current[0], recorded, current)
    assert (d.action, d.offset) == (action, offset)


DATA = os.urandom(3 * 1024 * 1024 + 123)


def _dev():
    d = FakeIPhone()
    d.add("DCIM/100APPLE/IMG_0001.MOV", DATA)
    return d


def run(coro):
    return asyncio.run(coro)


def test_stream_copy_fresh(tmp_path):
    part = tmp_path / "x.part"
    got = []
    n = run(stream_copy(_dev(), "DCIM/100APPLE/IMG_0001.MOV", str(part), len(DATA), 0, got.append, CancelToken(),
                        chunk=1024 * 1024))
    assert n == len(DATA) and part.read_bytes() == DATA and sum(got) == len(DATA)


def test_stream_copy_resume_from_offset(tmp_path):
    part = tmp_path / "x.part"
    part.write_bytes(DATA[:1_000_000])
    run(stream_copy(_dev(), "DCIM/100APPLE/IMG_0001.MOV", str(part), len(DATA), 1_000_000, lambda n: None,
                    CancelToken(), chunk=512 * 1024))
    assert part.read_bytes() == DATA


def test_stream_copy_disconnect_keeps_valid_prefix(tmp_path):
    dev = _dev()
    dev.fail_after_bytes = 1_500_000
    part = tmp_path / "x.part"
    with pytest.raises(DeviceConnectionLost):
        run(stream_copy(dev, "DCIM/100APPLE/IMG_0001.MOV", str(part), len(DATA), 0, lambda n: None, CancelToken(),
                        chunk=256 * 1024))
    prefix = part.read_bytes()
    assert 0 < len(prefix) <= 1_500_000 and DATA.startswith(prefix)
    # Resume after reconnect produces an identical file.
    run(dev.connect())
    run(stream_copy(dev, "DCIM/100APPLE/IMG_0001.MOV", str(part), len(DATA), len(prefix), lambda n: None,
                    CancelToken()))
    assert part.read_bytes() == DATA


def test_stream_copy_cancel_leaves_valid_part(tmp_path):
    tok = CancelToken()
    part = tmp_path / "x.part"
    seen = []

    def on_bytes(n):
        seen.append(n)
        if sum(seen) >= 1024 * 1024:
            tok.cancel()

    with pytest.raises(Cancelled):
        run(stream_copy(_dev(), "DCIM/100APPLE/IMG_0001.MOV", str(part), len(DATA), 0, on_bytes, tok,
                        chunk=256 * 1024))
    assert DATA.startswith(part.read_bytes()) and part.stat().st_size == 1024 * 1024


def test_stream_copy_source_shorter_than_stat(tmp_path):
    with pytest.raises(RemoteFileError):
        run(stream_copy(_dev(), "DCIM/100APPLE/IMG_0001.MOV", str(tmp_path / "x.part"), len(DATA) + 10, 0,
                        lambda n: None, CancelToken()))
