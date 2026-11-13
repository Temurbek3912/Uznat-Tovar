"""app/core/totp.py — TOTP по RFC 6238: тестовые векторы RFC, окно, защита от повтора, формат otpauth://."""
from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest

from app.core import totp

RFC_SECRET = base64.b32encode(b"12345678901234567890").decode("ascii")   # ключ из RFC 6238, приложение B (SHA-1)


@pytest.mark.parametrize("unix_time, expected8", [
    (59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"),
    (1234567890, "89005924"), (2000000000, "69279037"), (20000000000, "65353130"),
])
def test_rfc6238_appendix_b_vectors_sha1(unix_time, expected8):
    key = b"12345678901234567890"
    assert totp.hotp(key, totp.step_at(unix_time), digits=8, digest=hashlib.sha1) == expected8
    assert totp.code_at(RFC_SECRET, unix_time) == expected8[-6:]                       # 6 цифр — те же младшие разряды


def test_rfc4226_hotp_vectors():
    key = b"12345678901234567890"
    expected = ["755224", "287082", "359152", "969429", "338314", "254676", "287922", "162583", "399871", "520489"]   # RFC 4226, приложение D
    assert [totp.hotp(key, c) for c in range(10)] == expected


def test_window_accepts_neighbour_steps_only():
    now = 1_700_000_000
    for delta, ok in ((-60, False), (-30, True), (0, True), (30, True), (60, False)):
        code = totp.code_at(RFC_SECRET, now + delta)
        assert (totp.verify(RFC_SECRET, code, now=now) is not None) is ok, delta


def test_replay_of_the_same_or_older_code_is_refused():
    now = 1_700_000_000
    code = totp.code_at(RFC_SECRET, now)
    step = totp.verify(RFC_SECRET, code, now=now)
    assert step == totp.step_at(now)
    assert totp.verify(RFC_SECRET, code, now=now + 5, not_before=step) is None              # тот же код ещё раз
    older = totp.code_at(RFC_SECRET, now - 30)
    assert totp.verify(RFC_SECRET, older, now=now, not_before=step) is None                 # более старый
    newer = totp.code_at(RFC_SECRET, now + 30)
    assert totp.verify(RFC_SECRET, newer, now=now + 30, not_before=step) == step + 1


@pytest.mark.parametrize("raw, clean", [("123456", "123456"), ("123 456", "123456"), ("123-456", "123456"), (" 123456 ", "123456"),
                                        ("12345", None), ("1234567", None), ("12a456", None), ("", None), (None, None)])
def test_code_normalisation(raw, clean):
    assert totp.normalize_code(raw) == clean


def test_secret_is_160_bits_base32_and_random():
    a, b = totp.generate_secret(), totp.generate_secret()
    assert a != b and len(base64.b32decode(a + "=" * (-len(a) % 8))) == 20
    assert totp.verify(a, totp.code_at(a, 1_700_000_000), now=1_700_000_000) is not None
    assert totp.verify(a.lower(), totp.code_at(a, 1_700_000_000), now=1_700_000_000) is not None   # регистр и пробелы не важны
    assert totp.verify(totp.group_secret(a), totp.code_at(a, 1_700_000_000), now=1_700_000_000) is not None


def test_bad_secret_is_an_error_not_a_silent_pass():
    with pytest.raises(ValueError):
        totp.verify("not base32!", "123456")


def test_provisioning_uri_is_understood_by_authenticator_apps():
    uri = totp.provisioning_uri("JBSWY3DPEHPK3PXP", "admin@shop", issuer="Uznat tovar")
    parsed = urlparse(uri)
    assert parsed.scheme == "otpauth" and parsed.netloc == "totp" and parsed.path == "/Uznat%20tovar%3Aadmin%40shop"
    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert q == {"secret": "JBSWY3DPEHPK3PXP", "issuer": "Uznat tovar", "algorithm": "SHA1", "digits": "6", "period": "30"}
    assert totp.group_secret("JBSWY3DPEHPK3PXP") == "JBSW Y3DP EHPK 3PXP"
