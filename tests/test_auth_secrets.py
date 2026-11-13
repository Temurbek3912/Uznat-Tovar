"""app/core/auth_secrets.py — шифрование секрета второго фактора и ключ отпечатков IP."""
from __future__ import annotations

import pytest

from app.core import auth_secrets as s
from app.core.crypto import generate_key


def test_round_trip_and_ciphertext_hides_the_secret():
    key = generate_key()
    token = s.encrypt_totp_secret("JBSWY3DPEHPK3PXP", key)
    assert "JBSWY3DPEHPK3PXP" not in token and s.decrypt_totp_secret(token, key) == "JBSWY3DPEHPK3PXP"


def test_wrong_key_gives_a_safe_actionable_error():
    token = s.encrypt_totp_secret("JBSWY3DPEHPK3PXP", generate_key())
    other = generate_key()
    with pytest.raises(s.AuthSecretError) as err:
        s.decrypt_totp_secret(token, other)
    assert "reset-2fa" in str(err.value) and other not in str(err.value) and "JBSW" not in str(err.value)


@pytest.mark.parametrize("bad", ["", "   ", "short", "!" * 44])
def test_missing_or_malformed_key_is_refused_with_a_hint(bad):
    assert not s.is_configured(bad)
    with pytest.raises(s.AuthSecretError) as err:
        s.encrypt_totp_secret("JBSWY3DPEHPK3PXP", bad)
    assert "generate_secret_key --auth" in str(err.value)


def test_subkeys_are_independent_of_each_other_and_of_the_camera_use():
    key = generate_key()
    assert s.ip_hmac_key(key) and s.ip_hmac_key(key) == s.ip_hmac_key(key) and s.ip_hmac_key(generate_key()) != s.ip_hmac_key(key)
    assert s.ip_hmac_key("") == b""
    from app.core.crypto import decrypt_text, SecretKeyError
    with pytest.raises(SecretKeyError):                      # секрет 2FA не расшифровывается «камерным» способом тем же ключом
        decrypt_text(s.encrypt_totp_secret("JBSWY3DPEHPK3PXP", key), key)


def test_key_script_prints_the_auth_variable(capsys):
    from scripts import generate_secret_key as g
    assert g.main(["--auth"]) == 0 and capsys.readouterr().out.startswith("AUTH_SECRET_KEY=")
    assert g.main([]) == 0 and capsys.readouterr().out.startswith("CAMERA_SECRET_KEY=")
