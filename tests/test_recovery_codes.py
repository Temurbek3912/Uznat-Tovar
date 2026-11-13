"""app/core/recovery_codes.py — коды восстановления второго фактора."""
from __future__ import annotations

import pytest

from app.core import auth_secrets
from app.core import recovery_codes as rc
from app.core.crypto import generate_key


def test_generate_ten_unique_readable_codes():
    codes = rc.generate()
    assert len(codes) == 10 and len(set(codes)) == 10
    for code in codes:
        assert len(code) == 11 and code[5] == "-" and all(ch in rc.ALPHABET for ch in code.replace("-", ""))
        assert not set(code) & set("01OIL")


@pytest.mark.parametrize("raw, clean", [("7KQ4M-P2XJR", "7KQ4MP2XJR"), ("7kq4m p2xjr", "7KQ4MP2XJR"), (" 7KQ4MP2XJR ", "7KQ4MP2XJR"),
                                        ("123456", None), ("7KQ4M-P2XJ", None), ("7KQ4M-P2XJ0", None), ("", None), (None, None)])
def test_normalize(raw, clean):
    assert rc.normalize(raw) == clean
    assert rc.looks_like_recovery(raw) is (clean is not None)


def test_digest_is_keyed_and_format_insensitive_and_hides_the_code():
    key = auth_secrets.recovery_hmac_key(generate_key())
    a = rc.digest("7KQ4M-P2XJR", key)
    assert a == rc.digest("7kq4mp2xjr", key) and "7KQ4M" not in a
    assert a != rc.digest("7KQ4M-P2XJR", auth_secrets.recovery_hmac_key(generate_key()))
    with pytest.raises(ValueError):
        rc.digest("123456", key)
    with pytest.raises(ValueError):
        rc.digest("7KQ4M-P2XJR", b"")
