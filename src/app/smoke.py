# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Real-device smoke test.  Strictly read-only: nothing is written to the iPhone,
and nothing read from it is kept (beyond printing sizes and names)."""

from __future__ import annotations

from iphone import connection
from iphone.device import ReadOnlyIPhone, RemoteFileError
from iphone.usb_speed import query_iphone_usb_link

SMALL_READ = 64 * 1024


async def smoke(udid: str | None = None) -> int:
    ok = True
    print("1. Device detection")
    if not await connection.usbmux_available():
        print("   FAIL:", connection.MSG_NO_USBMUX)
        return 1
    udids = await connection.list_usb_udids()
    if not udids:
        print("   FAIL:", connection.MSG_NO_DEVICE)
        return 1
    if udid is None and len(udids) > 1:
        print("   FAIL: several devices attached; pass --udid")
        return 1
    dev = ReadOnlyIPhone(udid or udids[0])
    await dev.connect()
    i = dev.device_info()
    print(f"   OK: {i.model} ({i.product_type}) iOS {i.ios_version}")
    try:
        print("2. PhotoData exists")
        e = await dev.exists("PhotoData")
        print("   OK" if e else "   FAIL")
        ok &= e

        print("3. Database metadata")
        for p in ("PhotoData/Photos.sqlite", "PhotoData/Photos.sqlite-wal"):
            try:
                st = await dev.stat(p)
                print(f"   {p}: {st.size:,} bytes, mtime {st.mtime:.0f}")
            except RemoteFileError as ex:
                print(f"   {p}: {ex}")
                ok &= not p.endswith("Photos.sqlite")

        print("4. List one DCIM directory")
        dirs = sorted(d for d in await dev.listdir("DCIM") if d[:3].isdigit())
        media = []
        if dirs:
            names = await dev.listdir(f"DCIM/{dirs[-1]}")
            media = sorted(n for n in names if not n.startswith("."))
            print(f"   DCIM/{dirs[-1]}: {len(media)} entries")
        else:
            print("   no DCIM directories")

        print("5. Read a few bytes from one media file")
        if media:
            path = f"DCIM/{dirs[-1]}/{media[0]}"
            h = await dev.open_read(path)
            try:
                data = await dev.read(h, SMALL_READ)
            finally:
                await dev.close(h)
            print(f"   read {len(data):,} bytes (discarded)")
            del data
        else:
            print("   skipped (no media)")

        print("6. Negotiated USB connection")
        link = query_iphone_usb_link(dev.udid)
        print(f"   {link.mode} ({link.link_text})" + (f" [{link.reason}]" if link.reason else ""))
    finally:
        await dev.disconnect()
    print("SMOKE TEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1
