# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

import pytest

import iphone.usb_speed as usb
from iphone.usb_speed import (
    USB2_HS,
    USB3_SS,
    USB3_SSP,
    USB_FS,
    USB_LS,
    USB_UNKNOWN,
    UsbLinkInfo,
    classify_link,
    device_max_link_mbps,
    query_iphone_usb_link,
    summarize,
)


# --- IOCTL codes -----------------------------------------------------------

def test_ioctl_codes():
    assert usb.IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX == 0x220448
    assert usb.IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX_V2 == 0x22045C


# --- classify_link ---------------------------------------------------------

@pytest.mark.parametrize(
    "speed, mode, mbps, text",
    [
        (3, USB3_SS, 5000, "up to 5 Gb/s"),
        (2, USB2_HS, 480, "480 Mb/s"),
        (1, USB_FS, 12, "12 Mb/s"),
        (0, USB_LS, None, "1.5 Mb/s"),
    ],
)
def test_classify_speed_only(speed, mode, mbps, text):
    info = classify_link(speed, None)
    assert info.mode == mode
    assert info.link_mbps == mbps
    assert info.link_text == text
    assert info.superspeed_capable is None
    assert info.superspeedplus_capable is None


def test_classify_none_is_unknown():
    info = classify_link(None, None)
    assert info.mode == USB_UNKNOWN
    assert info.link_mbps is None
    assert info.link_text == "unknown"
    assert not info.below_superspeed


def test_classify_unrecognised_speed_is_unknown():
    assert classify_link(9, None).mode == USB_UNKNOWN


def test_classify_ssp_flag_wins():
    info = classify_link(3, 0x1 | 0x2 | 0x4 | 0x8)
    assert info.mode == USB3_SSP
    assert info.link_mbps == 10000
    assert info.link_text == "up to 10 Gb/s"
    assert info.superspeed_capable is True
    assert info.superspeedplus_capable is True
    assert not info.below_superspeed


def test_classify_ss_flag_overrides_reported_high_speed():
    # Some stacks report Speed=High for SuperSpeed devices in the EX IOCTL.
    info = classify_link(2, 0x1 | 0x2)
    assert info.mode == USB3_SS
    assert info.link_mbps == 5000
    assert info.superspeed_capable is True
    assert info.superspeedplus_capable is False


def test_classify_ss_on_ssp_capable_device():
    info = classify_link(3, 0x1 | 0x2 | 0x8)
    assert info.mode == USB3_SS
    assert info.superspeedplus_capable is True


def test_classify_v2_flags_without_speed():
    assert classify_link(None, 0x4).mode == USB3_SSP
    assert classify_link(None, 0x1).mode == USB3_SS
    assert classify_link(None, 0x2).mode == USB_UNKNOWN


def test_capable_but_below_superspeed():
    info = classify_link(2, 0x2 | 0x8)  # capable of SS/SS+, operating at High
    assert info.mode == USB2_HS
    assert info.below_superspeed is True
    assert info.superspeed_capable is True
    assert info.superspeedplus_capable is True


def test_high_speed_not_capable():
    info = classify_link(2, 0x0)
    assert info.mode == USB2_HS
    assert info.below_superspeed
    assert info.superspeed_capable is False
    assert info.superspeedplus_capable is False


# --- device_max_link_mbps --------------------------------------------------

@pytest.mark.parametrize(
    "pt", ["iPhone16,1", "iPhone16,2", "iPhone17,1", "iPhone17,2", "iPhone18,1", "iPhone18,2"]
)
def test_device_max_pro(pt):
    assert device_max_link_mbps(pt) == 10000


@pytest.mark.parametrize(
    "pt", ["iPhone15,4", "iPhone15,5", "iPhone17,3", "iPhone14,2", "iPhone12,1", "", "garbage", None]
)
def test_device_max_other(pt):
    assert device_max_link_mbps(pt) == 480


# --- summarize -------------------------------------------------------------

def test_summarize_ssp_on_pro():
    link = classify_link(3, 0x1 | 0x2 | 0x4 | 0x8)
    head, lines, warn = summarize(link, "iPhone16,1", "iPhone 15 Pro")
    assert head == USB3_SSP
    assert lines == ["Negotiated link: up to 10 Gb/s", "Status: Maximum iPhone 15 Pro link speed"]
    assert warn is None


def test_summarize_high_speed_on_pro_warns():
    link = classify_link(2, 0x2 | 0x8)
    head, lines, warn = summarize(link, "iPhone17,1", "iPhone 16 Pro")
    assert head == USB2_HS
    assert lines == ["Negotiated link: 480 Mb/s"]
    assert warn == (
        "Your iPhone supports faster USB transfer, but the current cable/port connection "
        "is limited to USB 2.0.\nTry a USB 3 10 Gb/s-capable cable or another USB-C port."
    )


def test_summarize_high_speed_on_pro_warns_without_v2_flags():
    head, lines, warn = summarize(classify_link(2, None), "iPhone16,2", None)
    assert head == USB2_HS
    assert warn is not None and "USB 2.0" in warn


def test_summarize_superspeed_on_10g_device_warns_non_blocking():
    link = classify_link(3, 0x1 | 0x2 | 0x8)
    head, lines, warn = summarize(link, "iPhone16,1", "iPhone 15 Pro")
    assert head == USB3_SS
    assert lines == ["Negotiated link: up to 5 Gb/s"]
    assert warn is not None
    assert "5 Gb/s" in warn and "10 Gb/s" in warn
    assert "continue" in warn.lower()


def test_summarize_non_pro_high_speed_no_warning():
    link = classify_link(2, 0x0)
    head, lines, warn = summarize(link, "iPhone15,4", "iPhone 15")
    assert head == USB2_HS
    assert lines == ["Negotiated link: 480 Mb/s", "Status: Maximum iPhone 15 link speed"]
    assert warn is None


def test_summarize_unknown_product_but_flags_say_capable():
    link = classify_link(2, 0x2 | 0x8)
    _, _, warn = summarize(link, "iPhone99,9", None)
    assert warn is not None


def test_summarize_default_display_name():
    link = classify_link(3, 0x4 | 0x8)
    _, lines, _ = summarize(link, "iPhone18,2", None)
    assert lines[-1] == "Status: Maximum iPhone link speed"


def test_summarize_unknown():
    for link in (classify_link(None, None), UsbLinkInfo(USB_UNKNOWN, None, None, None, "x")):
        assert summarize(link, "iPhone16,1", "iPhone 15 Pro") == (
            "Unknown", ["Negotiated link: could not be determined"], None)


def test_summary_never_says_cable_speed():
    for speed, flags in [(2, 0xA), (3, 0xB), (3, 0xF), (2, 0), (None, None)]:
        head, lines, warn = summarize(classify_link(speed, flags), "iPhone16,1", "iPhone 15 Pro")
        text = " ".join([head, *lines, warn or ""]).lower()
        assert "cable speed" not in text


# --- query_iphone_usb_link graceful fallback -------------------------------

def test_query_fallback_when_topology_lookup_raises(monkeypatch):
    def boom(_udid):
        raise OSError("simulated SetupAPI failure")

    monkeypatch.setattr(usb.sys, "platform", "win32")
    monkeypatch.setattr(usb, "_query_windows", boom)
    info = query_iphone_usb_link("00008140-001A2B3C0E12801C")
    assert info.mode == USB_UNKNOWN
    assert info.link_mbps is None
    assert info.reason
    assert "001A2B3C" not in info.reason


def test_query_fallback_when_device_search_raises(monkeypatch):
    def boom(_udid):
        raise RuntimeError("simulated")

    monkeypatch.setattr(usb, "_find_apple_device", boom)
    info = query_iphone_usb_link("00008140-001A2B3C0E12801C")
    assert info.mode == USB_UNKNOWN


def test_query_fallback_when_hub_ioctl_fails(monkeypatch):
    loc = usb._DeviceLocation(devinst=1, instance_id="USB\\VID_05AC&PID_12A8\\X", port=2,
                              location_info=None)
    monkeypatch.setattr(usb, "_find_apple_device", lambda _u: loc)
    monkeypatch.setattr(usb, "_get_hub_path", lambda _d: "\\\\?\\nonexistent-hub")
    info = query_iphone_usb_link("00008140-001A2B3C0E12801C")
    assert info.mode == USB_UNKNOWN


def test_query_uses_location_fallback_and_classifies(monkeypatch):
    loc = usb._DeviceLocation(devinst=1, instance_id="x", port=None,
                              location_info="Port_#0003.Hub_#0002")
    seen = {}

    def fake_query(path, port):
        seen["port"] = port
        return 3, 0x1 | 0x2 | 0x4 | 0x8, 0x05AC

    monkeypatch.setattr(usb.sys, "platform", "win32")
    monkeypatch.setattr(usb, "_find_apple_device", lambda _u: loc)
    monkeypatch.setattr(usb, "_get_hub_path", lambda _d: "hub")
    monkeypatch.setattr(usb, "_query_hub_port", fake_query)
    info = query_iphone_usb_link("00008140-001A2B3C0E12801C")
    assert seen["port"] == 3
    assert info.mode == USB3_SSP


def test_query_rejects_non_apple_port(monkeypatch):
    loc = usb._DeviceLocation(devinst=1, instance_id="x", port=1, location_info=None)
    monkeypatch.setattr(usb.sys, "platform", "win32")
    monkeypatch.setattr(usb, "_find_apple_device", lambda _u: loc)
    monkeypatch.setattr(usb, "_get_hub_path", lambda _d: "hub")
    monkeypatch.setattr(usb, "_query_hub_port", lambda p, n: (2, 0, 0x1234))
    assert query_iphone_usb_link("00008140-001A2B3C0E12801C").mode == USB_UNKNOWN


def test_query_non_windows(monkeypatch):
    monkeypatch.setattr(usb.sys, "platform", "linux")
    info = query_iphone_usb_link("00008140-001A2B3C0E12801C")
    assert info.mode == USB_UNKNOWN
    assert "Windows" in info.reason


@pytest.mark.parametrize("udid", ["00008140-DEADBEEF00000000", "", None, 12345])
def test_query_bogus_udid_returns_unknown(udid):
    info = query_iphone_usb_link(udid)
    assert info.mode == USB_UNKNOWN
    assert info.link_mbps is None


def test_port_from_location():
    assert usb._port_from_location("Port_#0002.Hub_#0003") == 2
    assert usb._port_from_location("garbage") is None
    assert usb._port_from_location(None) is None
