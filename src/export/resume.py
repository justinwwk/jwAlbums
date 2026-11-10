# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pure decision logic for resuming partial downloads and handling existing files."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PartAction(Enum):
    FRESH = "fresh"          # no usable partial data, start at byte 0
    RESUME = "resume"        # continue at offset
    RESTART = "restart"      # discard partial data, start at byte 0


@dataclass(frozen=True)
class PartDecision:
    action: PartAction
    offset: int
    reason: str = ""


def decide_part(part_size: int | None, remote_size: int,
                recorded: tuple[int, float] | None, current: tuple[int, float]) -> PartDecision:
    """Decide what to do with an existing destination.ext.part file.

    part_size  -- size of the local .part file, or None if it does not exist
    recorded   -- (remote_size, remote_mtime) stored when the .part was started
    current    -- (remote_size, remote_mtime) from a fresh stat
    """
    if part_size is None:
        return PartDecision(PartAction.FRESH, 0)
    if recorded is None:
        return PartDecision(PartAction.RESTART, 0, "no record of the partial transfer source")
    if recorded[0] != current[0] or abs(recorded[1] - current[1]) > 1e-3:
        return PartDecision(PartAction.RESTART, 0, "source changed since partial transfer")
    if part_size > remote_size:
        return PartDecision(PartAction.RESTART, 0, "partial file larger than source")
    if part_size == 0:
        return PartDecision(PartAction.FRESH, 0)
    return PartDecision(PartAction.RESUME, part_size)
