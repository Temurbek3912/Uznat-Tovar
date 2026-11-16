"""
Секреты входа (документ 18): шифрование секрета второго фактора TOTP в базе и ключ для отпечатков IP-адресов сеансов.

Один ключ сервера AUTH_SECRET_KEY (ключ Fernet, `python -m scripts.generate_secret_key --auth`); из него выводятся два
независимых подключа (HMAC-SHA256 с разными метками), чтобы шифрование и отпечатки адресов не использовали один и тот же ключ.
Fernet — AES-128-CBC + HMAC-SHA256 с проверкой целостности (пакет cryptography, как у app/core/crypto.py).
Тексты ошибок безопасны для показа: ни ключ, ни секрет в них не попадают. Модуль без FastAPI/SQLAlchemy (tests/test_auth_secrets.py).
"""
from __future__ import annotations

import base64
import hashlib
import hmac

KEY_HINT = ("Создайте ключ: docker compose exec api python -m scripts.generate_secret_key --auth — и добавьте строку "
            "AUTH_SECRET_KEY=<ключ> в файл .env, затем docker compose up -d")


class AuthSecretError(RuntimeError):
    """Ключ не задан, некорректен или не подходит к данным. Текст безопасен для показа."""


def _master(key: str) -> bytes:
    raw = (key or "").strip()
    if not raw:
        raise AuthSecretError("AUTH_SECRET_KEY не задан — второй фактор входа включить нельзя. " + KEY_HINT)
    try:
        decoded = base64.urlsafe_b64decode(raw.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        decoded = b""
    if len(decoded) != 32:
        raise AuthSecretError("AUTH_SECRET_KEY некорректен (нужен ключ Fernet: 44 символа base64). " + KEY_HINT)
    return decoded


def _subkey(key: str, label: bytes) -> bytes:
    return hmac.new(_master(key), label, hashlib.sha256).digest()


def is_configured(key: str) -> bool:
    try:
        _master(key)
        return True
    except AuthSecretError:
        return False


def _fernet(key: str):
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(_subkey(key, b"uznat:totp-secret:v1")))


def encrypt_totp_secret(secret: str, key: str) -> str:
    return _fernet(key).encrypt(secret.encode("ascii")).decode("ascii")


def decrypt_totp_secret(token: str, key: str) -> str:
    from cryptography.fernet import InvalidToken
    try:
        return _fernet(key).decrypt(token.encode("ascii")).decode("ascii")
    except InvalidToken:
        raise AuthSecretError("Секрет второго фактора не расшифровывается: AUTH_SECRET_KEY изменился или данные повреждены. "
                              "Верните прежний ключ либо сбросьте второй фактор: python -m scripts.user_admin reset-2fa "
                              "--username …") from None


def encrypt_text(text: str, key: str, label: bytes) -> str:
    """Зашифровать произвольный текст отдельным подключом (label) — например, ключ доступа к Google Диску (2026-11-16)."""
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(_subkey(key, label))).encrypt(text.encode("utf-8")).decode("ascii")


def decrypt_text(token: str, key: str, label: bytes) -> str:
    from cryptography.fernet import Fernet, InvalidToken
    try:
        return Fernet(base64.urlsafe_b64encode(_subkey(key, label))).decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise AuthSecretError("Данные не расшифровываются: AUTH_SECRET_KEY изменился или данные повреждены.") from None


def recovery_hmac_key(key: str) -> bytes:
    """Ключ хешей кодов восстановления (без ключа сервера второго фактора нет — значит, и кодов)."""
    return _subkey(key, b"uznat:recovery-code:v1")


def ip_hmac_key(key: str) -> bytes:
    """Ключ отпечатков IP-адресов; нет ключа сервера — b"" (адрес тогда не запоминается вовсе)."""
    return _subkey(key, b"uznat:ip-fingerprint:v1") if is_configured(key) else b""
