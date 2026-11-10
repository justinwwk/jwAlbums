# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""jwAlbums: copy iPhone Photos albums to normal Windows folders (read-only)."""

from __future__ import annotations

import logging
import sys


def main() -> int:
    from app import network_guard
    from app.logging_setup import setup_logging
    from app.version import __version__

    network_guard.install()
    setup_logging("--verbose" in sys.argv)
    argv = [a for a in sys.argv[1:] if a != "--verbose"]
    logging.getLogger(__name__).info("startup jwAlbums %s (%s)", __version__, "cli" if argv else "gui")
    if argv:
        from app.cli import main as cli_main
        return cli_main(argv)
    from ui.main_window import run_gui
    run_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
