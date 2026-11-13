"""app/core/crypto.py — шифрование адресов камер: без ключа по умолчанию, без утечек в сообщениях об ошибках."""
from __future__ import annotations

import pytest

from app.core import crypto as sec

RTSP = "rtsp://admin:S3cret!pass@192.168.1.64:554/Streaming/Channels/101"


def test_round_trip_and_ciphertext_does_not_contain_the_secret():
    key = sec.generate_key()
    token = sec.encrypt_text(RTSP, key)
    assert "S3cret" not in token and "192.168" not in token and "rtsp" not in token
    assert sec.decrypt_text(token, key) == RTSP


def test_same_text_encrypts_differently_each_time_but_decrypts_the_same():
    key = sec.generate_key()
    a, b = sec.encrypt_text(RTSP, key), sec.encrypt_text(RTSP, key)
    assert a != b and sec.decrypt_text(a, key) == sec.decrypt_text(b, key) == RTSP


def test_unicode_survives():
    key = sec.generate_key()
    text = "http://камера:пароль@host/снимок.jpg"
    assert sec.decrypt_text(sec.encrypt_text(text, key), key) == text


def test_missing_key_is_an_error_with_instructions_never_a_default(tmp_path):
    for empty in ("", "   "):
        with pytest.raises(sec.SecretKeyError) as exc:
            sec.encrypt_text(RTSP, empty)
        assert "generate_secret_key" in str(exc.value) and ".env" in str(exc.value)


def test_environment_variable_is_used_when_no_explicit_key():
    import os
    key = sec.generate_key()
    previous = os.environ.get("CAMERA_SECRET_KEY")
    os.environ["CAMERA_SECRET_KEY"] = key
    try:
        assert sec.decrypt_text(sec.encrypt_text("x", None), None) == "x"
    finally:
        if previous is None:
            del os.environ["CAMERA_SECRET_KEY"]
        else:
            os.environ["CAMERA_SECRET_KEY"] = previous
    os.environ.pop("CAMERA_SECRET_KEY", None)
    with pytest.raises(sec.SecretKeyError):
        sec.encrypt_text("x", None)


def test_malformed_key_is_rejected_without_echoing_it():
    bad = "not-a-fernet-key-SECRETVALUE"
    with pytest.raises(sec.SecretKeyError) as exc:
        sec.encrypt_text("x", bad)
    assert "SECRETVALUE" not in str(exc.value) and "некорректен" in str(exc.value)


def test_wrong_key_or_tampered_token_is_a_clear_error_without_leaking():
    k1, k2 = sec.generate_key(), sec.generate_key()
    token = sec.encrypt_text(RTSP, k1)
    with pytest.raises(sec.SecretKeyError) as exc:
        sec.decrypt_text(token, k2)
    assert "изменился" in str(exc.value) and "S3cret" not in str(exc.value)
    with pytest.raises(sec.SecretKeyError):
        sec.decrypt_text(token[:-6] + "AAAAAA", k1)


def test_generated_keys_are_unique_and_valid():
    keys = {sec.generate_key() for _ in range(5)}
    assert len(keys) == 5 and all(len(k) == 44 for k in keys)
