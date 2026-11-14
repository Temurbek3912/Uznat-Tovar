"""
Одноразовые коды по времени — TOTP (RFC 6238, на основе HOTP RFC 4226) для второго фактора входа (2FA).

Совместим с приложениями-аутентификаторами (Google Authenticator, Microsoft Authenticator, Яндекс Ключ, FreeOTP, Aegis):
SHA-1, 6 цифр, шаг 30 секунд — это умолчания формата `otpauth://`, которые понимают все такие приложения.

Только стандартная библиотека; модуль не зависит от FastAPI/SQLAlchemy и проверяется офлайн (tests/test_totp.py, тестовые
векторы RFC 6238, приложение B).

Защита:
  * сравнение кода — за постоянное время (hmac.compare_digest);
  * окно ±1 шаг (часы телефона могут отставать или спешить на десятки секунд);
  * повтор: `verify` возвращает номер шага принятого кода; вызывающий хранит последний принятый шаг и передаёт его в
    `not_before` — тот же код (и более старые) повторно не принимается, даже если его подсмотрели;
  * секрет — 160 бит из `secrets` (рекомендация RFC 4226, §4).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import struct
import time
from urllib.parse import quote, urlencode

DIGITS = 6
STEP_SECONDS = 30
WINDOW = 1                     # принимаются коды соседних шагов: −30 … +30 секунд
SECRET_BYTES = 20              # 160 бит

_CODE_RE = re.compile(r"^\d{6}$")


def generate_secret() -> str:
    """Новый секрет в base32 без «=» (так его вводят вручную в приложение)."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def _key(secret_b32: str) -> bytes:
    clean = re.sub(r"[\s-]", "", str(secret_b32)).upper()
    if not clean or not re.fullmatch(r"[A-Z2-7]+", clean):
        raise ValueError("секрет TOTP должен быть в base32")
    return base64.b32decode(clean + "=" * (-len(clean) % 8))


def hotp(key: bytes, counter: int, digits: int = DIGITS, digest=hashlib.sha1) -> str:
    """HOTP (RFC 4226, §5.3): динамическое усечение HMAC и остаток от деления на 10^digits."""
    mac = hmac.new(key, struct.pack(">Q", counter), digest).digest()
    offset = mac[-1] & 0x0F
    binary = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10 ** digits)).zfill(digits)


def step_at(unix_time: float, step: int = STEP_SECONDS) -> int:
    return int(unix_time // step)


def code_at(secret_b32: str, unix_time: float) -> str:
    return hotp(_key(secret_b32), step_at(unix_time))


def normalize_code(code: str) -> str | None:
    """«123 456», «123-456» -> «123456»; не 6 цифр — None (такой код даже не сравнивается)."""
    clean = re.sub(r"[\s-]", "", str(code or ""))
    return clean if _CODE_RE.match(clean) else None


def verify(secret_b32: str, code: str, *, now: float | None = None, not_before: int | None = None,
           window: int = WINDOW) -> int | None:
    """Номер шага, которому соответствует код, или None. Шаги ≤ not_before не принимаются (защита от повтора).
    Все кандидаты окна проверяются всегда (время ответа не зависит от того, какой шаг совпал)."""
    clean = normalize_code(code)
    if clean is None:
        return None
    key = _key(secret_b32)
    current = step_at(time.time() if now is None else now)
    matched = None
    for delta in range(-window, window + 1):
        step = current + delta
        if hmac.compare_digest(hotp(key, step), clean) and matched is None:
            matched = step
    if matched is None or (not_before is not None and matched <= not_before):
        return None
    return matched


def provisioning_uri(secret_b32: str, account: str, issuer: str = "Uznat tovar") -> str:
    """Адрес otpauth:// для приложения-аутентификатора (формат Key Uri, его понимают все распространённые приложения)."""
    label = quote(f"{issuer}:{account}", safe="")
    query = urlencode({"secret": secret_b32, "issuer": issuer, "algorithm": "SHA1", "digits": DIGITS, "period": STEP_SECONDS})
    return f"otpauth://totp/{label}?{query}"


def group_secret(secret_b32: str) -> str:
    """Секрет группами по 4 символа — удобнее вводить вручную: «JBSW Y3DP EHPK 3PXP»."""
    return " ".join(secret_b32[i:i + 4] for i in range(0, len(secret_b32), 4))


def qr_data_uri(text: str) -> str:
    """QR-код ссылки otpauth:// картинкой SVG (data:-адрес): владелец наводит камеру телефона на экран компьютера вместо того, чтобы
    набирать 32 знака ключа. Нет библиотеки qrcode — пустая строка (страница покажет только ключ). Картинка строится в памяти
    и не сохраняется; секрет и так показывается один раз тем же ответом."""
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:  # pragma: no cover — qrcode необязательна (requirements.txt)
        return ""
    image = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathFillImage, box_size=10, border=2)
    svg = image.to_string()
    return "data:image/svg+xml;base64," + base64.b64encode(svg).decode("ascii")
