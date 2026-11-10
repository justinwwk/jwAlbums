# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Version and the legal notices shown in the app."""

import re

from app.version import COPYRIGHT, __version__


def test_version_is_three_numbers():
    # The EXE's Windows version resource (packaging/jwAlbums.spec) needs numbers.
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)


def test_about_shows_the_gpl_notices():
    # GPL-3.0 section 5(d): copyright, no warranty, may redistribute, where the license is.
    from ui.main_window import about_text
    text = about_text()
    for needed in (COPYRIGHT, __version__, "NO WARRANTY", "redistribute", "LICENSE.txt"):
        assert needed in text
