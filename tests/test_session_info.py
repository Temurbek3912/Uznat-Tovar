"""app/core/session_info.py — сведения о сеансе без полного IP и полной строки браузера."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.core import session_info as si


@pytest.mark.parametrize("ip, source", [
    ("127.0.0.1", "local"), ("::1", "local"), ("::ffff:127.0.0.1", "local"),
    ("192.168.1.50", "lan"), ("10.0.0.7", "lan"), ("172.20.1.1", "lan"), ("fd12::1", "lan"), ("fe80::1%eth0", "lan"),
    ("8.8.8.8", "remote"), ("2001:4860:4860::8888", "remote"), ("::ffff:8.8.8.8", "remote"),
    (None, "unknown"), ("", "unknown"), ("not-an-ip", "unknown"),
])
def test_source_classes(ip, source):
    assert si.classify_source(ip) == source


def test_ip_fingerprint_is_keyed_and_stable_and_not_the_address():
    a = si.ip_fingerprint("192.168.1.50", b"k1")
    assert a == si.ip_fingerprint("192.168.1.50", b"k1") and a != si.ip_fingerprint("192.168.1.50", b"k2")
    assert "192" not in a and len(a) == 32
    assert si.ip_fingerprint("192.168.1.50", b"") is None and si.ip_fingerprint(None, b"k") is None


@pytest.mark.parametrize("ua, expected", [
    ("Mozilla/5.0 (Linux; Android 14; SM-A546E) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Mobile Safari/537.36",
     {"kind": "phone", "os": "Android", "browser": "Chrome"}),
    ("Mozilla/5.0 (Linux; Android 13; SM-X200) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
     {"kind": "tablet", "os": "Android", "browser": "Chrome"}),
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
     {"kind": "phone", "os": "iPhone", "browser": "Safari"}),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36 Edg/131.0",
     {"kind": "computer", "os": "Windows", "browser": "Edge"}),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 YaBrowser/24.10 Safari/537.36",
     {"kind": "computer", "os": "Windows", "browser": "Яндекс Браузер"}),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) Gecko/20100101 Firefox/132.0", {"kind": "computer", "os": "macOS", "browser": "Firefox"}),
    ("curl/8.5.0", {"kind": "unknown", "os": "", "browser": "программа"}),
    (None, {"kind": "unknown", "os": "", "browser": ""}),
])
def test_user_agent_summary(ua, expected):
    assert si.summarize_user_agent(ua) == expected


def test_device_label_is_cleaned():
    assert si.clean_device_label("  Ноутбук\n дома\t ") == "Ноутбук дома"
    assert si.clean_device_label("x" * 100) == "x" * 60
    assert si.clean_device_label("   ") is None and si.clean_device_label(None) is None


def test_last_used_is_written_at_most_once_a_minute():
    now = datetime(2026, 11, 8, 12, 0, 0)
    assert si.needs_last_used_update(None, now)
    assert not si.needs_last_used_update(now - timedelta(seconds=59), now)
    assert si.needs_last_used_update(now - timedelta(seconds=60), now)


def test_epoch_rule_old_tokens_belong_to_generation_zero():
    assert si.epoch_matches(None, 0) and si.epoch_matches(0, None) and si.epoch_matches(3, 3)
    assert not si.epoch_matches(None, 1) and not si.epoch_matches(2, 3)
