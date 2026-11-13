"""Постоянный адрес каталога и диагностика сети (app/core/public_url.py). Главное правило: QR строится только из имени домена, никогда из IP или localhost,
в какой бы записи их ни написали. Модуль чистый — тесты идут без FastAPI, базы и сети."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core import public_url as pu


def _codes(check):
    return [f.code for f in check.findings]


def test_a_normal_domain_becomes_the_canonical_address_and_the_only_thing_the_qr_may_contain():
    for raw, want in [
        ("https://catalog.example.uz", "https://catalog.example.uz/"),
        ("  https://Catalog.Example.UZ/  ", "https://catalog.example.uz/"),
        ("https://catalog.example.uz:443/", "https://catalog.example.uz/"),
        ("http://catalog.example.uz:80", "http://catalog.example.uz/"),
        ("https://catalog.example.uz:8443/", "https://catalog.example.uz:8443/"),
        ("https://catalog.example.uz.", "https://catalog.example.uz/"),
    ]:
        check = pu.validate_public_base_url(raw, "server" if raw.startswith("https") else "local")
        assert check.ok and check.url == want, (raw, check.findings)
        assert pu.qr_target(raw, "server" if raw.startswith("https") else "local") == want


def test_cyrillic_and_other_unicode_domains_are_turned_into_ascii_for_the_qr():
    check = pu.validate_public_base_url("https://каталог.магазин.uz/", "server")
    assert check.ok and check.url.startswith("https://xn--") and check.url.isascii(), check.url


def test_an_ip_address_is_refused_in_every_notation_a_browser_understands():
    """QR из IP — главный запрет: при смене IP его пришлось бы перепечатывать. Браузеры понимают IP и в десятичной, и в шестнадцатеричной, и в сокращённой записи."""
    for raw in ["https://192.168.1.50", "http://192.168.1.50:8000/", "https://10.0.0.5/", "https://8.8.8.8/", "https://2130706433/", "https://0x7f.0.0.1/", "https://0xc0.0xa8.1.50/",
                "https://127.1/", "https://0177.0.0.1/", "https://[::1]/", "https://[fe80::1]:8000/", "https://[2001:db8::1]/", "https://1.2.3/", "https://3232235826/"]:
        for mode in ("local", "server"):
            check = pu.validate_public_base_url(raw, mode)
            assert not check.ok and check.url == "", (raw, mode, check.findings)
            assert _codes(check)[0] in ("ip_literal", "bad_port"), (raw, _codes(check))
            try:
                pu.qr_target(raw, mode)
            except pu.PublicUrlError as exc:
                assert exc.findings
            else:
                raise AssertionError(f"QR создан из IP: {raw}")


def test_localhost_in_any_spelling_is_refused():
    for raw in ["http://localhost:8000", "https://LOCALHOST/", "http://localhost./", "https://shop.localhost/", "http://127.0.0.1:8000"]:
        check = pu.validate_public_base_url(raw, "local")
        assert not check.ok and _codes(check)[0] in ("localhost", "ip_literal"), (raw, check.findings)


def test_garbage_and_unsafe_addresses_are_refused_with_a_reason_the_owner_can_act_on():
    cases = {
        "": "empty", "   ": "empty", None: "empty", "catalog.example.uz": "no_scheme", "ftp://catalog.example.uz/": "bad_scheme", "javascript://catalog.example.uz/": "bad_scheme",
        "https://user:secret@catalog.example.uz/": "userinfo", "https://@catalog.example.uz/": "userinfo", "https://catalog.example.uz/?a=1": "query_or_fragment",
        "https://catalog.example.uz/#top": "query_or_fragment", "https://catalog.example.uz/shop": "path_not_root", "https://catalog.example.uz:99999/": "bad_port",
        "https://cat alog.example.uz/": "bad_chars", "https:///": "no_host", "https://-bad.example.uz/": "bad_host", "https://bad_name.example.uz/": "bad_host", "https://a..b.uz/": "bad_host",
    }
    for raw, code in cases.items():
        check = pu.validate_public_base_url(raw, "local")
        assert not check.ok and _codes(check)[0] == code, (raw, _codes(check))
        assert all(f.text and f.hint for f in check.findings), "у каждой ошибки есть и причина, и что делать"
    assert not pu.validate_public_base_url("https://x" + "a" * 70 + ".uz/", "local").ok, "метка длиннее 63 знаков"
    assert not pu.validate_public_base_url("https://" + "a" * 300, "local").ok


def test_server_mode_requires_https_and_a_public_name_while_local_mode_allows_http_with_an_honest_warning():
    assert _codes(pu.validate_public_base_url("http://catalog.example.uz/", "server")) == ["http_in_server_mode"]
    assert "not_public_name" in _codes(pu.validate_public_base_url("https://shop/", "server"))
    assert "not_public_name" in _codes(pu.validate_public_base_url("https://shop.local/", "server"))
    local = pu.validate_public_base_url("http://catalog.example.uz/", "local")
    assert local.ok and [w.code for w in local.warnings] == ["http_no_secure_context"], "камера по http не заработает — это нельзя скрывать"
    assert "камер" in local.warnings[0].text
    assert pu.validate_public_base_url("https://catalog.example.uz/", "local").warnings == []
    assert [w.code for w in pu.validate_public_base_url("https://shop.local/", "local").warnings] == ["mdns_name"]
    assert [w.code for w in pu.validate_public_base_url("https://shop/", "local").warnings] == ["single_label"]


def test_an_unknown_mode_is_an_error_not_a_silent_default():
    check = pu.validate_public_base_url("https://catalog.example.uz/", "production")
    assert not check.ok and _codes(check) == ["bad_mode"]


def test_changing_the_computer_ip_does_not_change_the_qr_address():
    """Свойство, ради которого всё затеяно: адрес QR зависит только от PUBLIC_BASE_URL, а IP на него влиять не может."""
    base = "https://catalog.example.uz/"
    before = pu.qr_target(base, "local")
    for new_ip in ["192.168.1.50", "192.168.0.77", "10.1.2.3"]:
        assert pu.qr_target(base, "local") == before and new_ip not in before


def test_ip_classification_matches_what_a_shop_owner_would_say():
    cases = {"127.0.0.1": "loopback", "::1": "loopback", "192.168.1.50": "private", "10.0.0.5": "private", "172.16.4.4": "private", "169.254.1.1": "link_local", "100.64.1.1": "shared",
             "8.8.8.8": "public", "93.184.216.34": "public", "not-an-ip": "invalid", "": "invalid"}
    for ip, kind in cases.items():
        assert pu.classify_ip(ip) == kind, ip


def test_resolve_host_returns_unique_addresses_and_treats_a_failed_lookup_as_an_answer():
    import socket

    def resolver(host, port, type=None):
        return [(socket.AF_INET, type, 6, "", ("192.168.1.50", 0)), (socket.AF_INET, type, 6, "", ("192.168.1.50", 0)), (socket.AF_INET6, type, 6, "", ("fd00::2", 0, 0, 0))]

    assert pu.resolve_host("catalog.example.uz", resolver) == ["192.168.1.50", "fd00::2"]

    def broken(host, port, type=None):
        raise socket.gaierror("нет такого имени")

    assert pu.resolve_host("catalog.example.uz", broken) == []


def test_certificate_days_are_counted_from_a_fake_connection_and_failures_give_none():
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    expires = now + timedelta(days=40, hours=3)
    cert = {"notAfter": expires.strftime("%b %d %H:%M:%S %Y GMT")}
    assert pu.cert_days_left("catalog.example.uz", 443, connect=lambda h, p: cert, now=lambda: now) == 40
    assert pu.cert_days_left("x", 443, connect=lambda h, p: {"notAfter": "в прошлом веке"}, now=lambda: now) is None
    assert pu.cert_days_left("x", 443, connect=lambda h, p: {}, now=lambda: now) is None

    def offline(host, port):
        raise OSError("нет сети")

    assert pu.cert_days_left("x", 443, connect=offline, now=lambda: now) is None


def _diag(url="https://catalog.example.uz/", mode="local", resolved=("192.168.1.50",), reachable=True, cert=60, lan=()):
    check = pu.validate_public_base_url(url, mode)
    return pu.diagnose(check, mode, resolved, reachable, cert, lan)


def test_diagnostics_of_a_healthy_local_setup_say_ok_and_nothing_alarming():
    findings = _diag(lan=("192.168.1.50",))
    assert [f.level for f in findings] == ["ok"] and findings[0].code == "reachable"


def test_diagnostics_tell_the_owner_what_to_fix_for_each_typical_problem():
    assert [f.code for f in _diag(resolved=())][0] == "dns_unresolved"
    assert "dns_loopback" in [f.code for f in _diag(resolved=("127.0.0.1",))]
    assert "dns_private_in_server_mode" in [f.code for f in _diag(mode="server", resolved=("192.168.1.50",))]
    assert "dns_public_in_local_mode" in [f.code for f in _diag(resolved=("93.184.216.34",))]
    moved = [f for f in _diag(resolved=("192.168.1.50",), lan=("192.168.1.77",)) if f.code == "dns_not_this_computer"]
    assert moved and "перепечатывать не нужно" in moved[0].hint and "DHCP" in moved[0].hint, "сменился IP — меняем DNS, а не печатаем QR заново"
    assert not [f for f in _diag(resolved=("192.168.1.50",), lan=()) if f.code == "dns_not_this_computer"], "своих адресов не знаем — не придираемся"
    unreachable = _diag(reachable=False)
    assert "unreachable" in [f.code for f in unreachable] and "reachable" not in [f.code for f in unreachable]
    assert "cert_expired" in [f.code for f in _diag(cert=-1)] and "cert_expiring" in [f.code for f in _diag(cert=5)] and "cert_unknown" in [f.code for f in _diag(cert=None)]
    assert not [f for f in _diag(cert=30) if f.code.startswith("cert_")]
    http = pu.diagnose(pu.validate_public_base_url("http://catalog.example.uz/", "local"), "local", ["192.168.1.50"], True, None)
    assert "http_no_secure_context" in [f.code for f in http] and not [f for f in http if f.code.startswith("cert_")], "у http сертификата нет — про него не говорим"


def test_diagnostics_of_an_invalid_address_return_only_the_address_problems():
    findings = pu.diagnose(pu.validate_public_base_url("https://192.168.1.50/", "local"), "local", ["192.168.1.50"], True, 60)
    assert [f.code for f in findings] == ["ip_literal"], "пока адрес негоден, сеть не проверяем и не путаем владельца вторым списком"
