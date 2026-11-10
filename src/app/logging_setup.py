# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Local-only logging with a redaction filter for pairing material."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler

from app.settings import logs_dir

# Anything that looks like pairing / key material is never written to disk.
_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*-----.*?-----END [A-Z ]*-----", re.S),
    re.compile(r"(HostPrivateKey|RootPrivateKey|DevicePrivateKey|EscrowBag|HostCertificate|"
               r"RootCertificate|DeviceCertificate|PairRecordData|SystemBUID|HostID|WiFiMACAddress)"
               r"['\"]?\s*[:=]\s*\S+", re.I),
]


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # malformed format args; let logging report it
            return True
        redacted = msg
        for pat in _SECRET_PATTERNS:
            redacted = pat.sub("[REDACTED]", redacted)
        if redacted != msg:
            record.msg, record.args = redacted, None
        return True


def setup_logging(verbose: bool = False) -> logging.Logger:
    root = logging.getLogger()
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return root
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    handler = RotatingFileHandler(logs_dir() / "app.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)
    # pymobiledevice3 logs protocol details at DEBUG; keep it quiet so pair records
    # or plist payloads can never reach the log file.
    for name in ("pymobiledevice3", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return root
