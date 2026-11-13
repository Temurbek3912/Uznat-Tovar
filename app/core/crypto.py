"""
Шифрование секретов камер (RTSP-адреса с логином/паролем) для хранения в БД. Fernet из пакета `cryptography`
(AES-128-CBC + HMAC-SHA256, с проверкой целостности).

Ключ — переменная CAMERA_SECRET_KEY (.env). Умолчания НЕТ намеренно: ключ, зашитый в код, не защищает ничего.
Создать:   docker compose exec api python -m scripts.generate_secret_key     (напечатает ключ; вставьте в .env)
Предупреждение: потеряете ключ — зашифрованные адреса камер не расшифровать (камеры придётся ввести заново);
смените ключ — то же самое. Сохраните его отдельно от базы (менеджер паролей).

Модуль не зависит от FastAPI/SQLAlchemy — тестируется офлайн (tests/test_camera_secrets.py).
"""
from __future__ import annotations

import os

GENERATE_HINT = "Создайте ключ: docker compose exec api python -m scripts.generate_secret_key — и добавьте строку CAMERA_SECRET_KEY=<ключ> в файл .env, затем docker compose up -d"


class SecretKeyError(RuntimeError):
    """Ключ не задан, некорректен или не подходит к данным. Текст безопасен для показа (ключ и данные в него не попадают)."""


def generate_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode("ascii")


def _fernet(key: str | None):
    from cryptography.fernet import Fernet
    raw = (key if key is not None else os.environ.get("CAMERA_SECRET_KEY", "")).strip()
    if not raw:
        raise SecretKeyError("CAMERA_SECRET_KEY не задан. " + GENERATE_HINT)
    try:
        return Fernet(raw.encode("ascii"))
    except (ValueError, TypeError, UnicodeEncodeError):
        raise SecretKeyError("CAMERA_SECRET_KEY некорректен (нужен ключ Fernet: 44 символа base64). " + GENERATE_HINT) from None


def encrypt_text(plain: str, key: str | None = None) -> str:
    return _fernet(key).encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_text(token: str, key: str | None = None) -> str:
    from cryptography.fernet import InvalidToken
    try:
        return _fernet(key).decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise SecretKeyError("Не удалось расшифровать адрес камеры: ключ CAMERA_SECRET_KEY изменился или данные повреждены. "
                             "Верните прежний ключ либо введите адрес камеры заново.") from None
