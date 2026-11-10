# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Runtime enforcement of the no-internet policy.

The only network traffic the app needs is the loopback connection to Apple's usbmux
service (127.0.0.1:27015).  Any attempt to connect to a non-loopback address is
refused and logged.
"""

from __future__ import annotations

import ipaddress
import logging
import socket

log = logging.getLogger(__name__)

_installed = False


def _is_loopback(address) -> bool:
    if isinstance(address, tuple) and address:
        host = address[0]
    else:
        return True   # AF_UNIX-style paths are local
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False   # hostnames other than localhost are never resolved


class NetworkBlockedError(OSError):
    pass


def install() -> None:
    global _installed
    if _installed:
        return
    orig_connect = socket.socket.connect
    orig_connect_ex = socket.socket.connect_ex

    def connect(self, address):
        if not _is_loopback(address):
            log.error("blocked outbound network connection attempt")
            raise NetworkBlockedError("network access is disabled in jwAlbums")
        return orig_connect(self, address)

    def connect_ex(self, address):
        if not _is_loopback(address):
            log.error("blocked outbound network connection attempt")
            raise NetworkBlockedError("network access is disabled in jwAlbums")
        return orig_connect_ex(self, address)

    socket.socket.connect = connect          # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex    # type: ignore[method-assign]
    _orig_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        if host not in (None, "localhost", "127.0.0.1", "::1"):
            try:
                if not ipaddress.ip_address(host).is_loopback:
                    raise NetworkBlockedError("network access is disabled")
            except ValueError:
                raise NetworkBlockedError("name resolution is disabled") from None
        return _orig_getaddrinfo(host, *args, **kwargs)

    socket.getaddrinfo = getaddrinfo

    def _guard_name(fn):
        def wrapper(host, *args, **kwargs):
            if host not in ("localhost", "127.0.0.1", "::1"):
                raise NetworkBlockedError("name resolution is disabled")
            return fn(host, *args, **kwargs)
        return wrapper

    socket.gethostbyname = _guard_name(socket.gethostbyname)
    socket.gethostbyname_ex = _guard_name(socket.gethostbyname_ex)
    socket.gethostbyaddr = _guard_name(socket.gethostbyaddr)

    orig_sendto = socket.socket.sendto

    def sendto(self, data, *args):
        address = args[-1] if args else None
        if not _is_loopback(address):
            log.error("blocked outbound datagram")
            raise NetworkBlockedError("network access is disabled in jwAlbums")
        return orig_sendto(self, data, *args)

    socket.socket.sendto = sendto            # type: ignore[method-assign]

    # asyncio's Proactor loop (the Windows default) connects via ConnectEx, bypassing
    # socket.connect, so its sock_connect is guarded too.
    import asyncio.proactor_events
    import asyncio.selector_events
    for cls in (asyncio.proactor_events.BaseProactorEventLoop, asyncio.selector_events.BaseSelectorEventLoop):
        orig = cls.sock_connect

        async def sock_connect(self, sock, address, _orig=orig):
            if not _is_loopback(address):
                log.error("blocked outbound network connection attempt")
                raise NetworkBlockedError("network access is disabled in jwAlbums")
            return await _orig(self, sock, address)

        cls.sock_connect = sock_connect
    _installed = True
