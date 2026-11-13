"""
Коды восстановления второго фактора (потерян телефон с приложением; решение владельца 2026-11-08, документ 18, §4.1).

  * 10 одноразовых кодов вида «7KQ4M-P2XJR» (10 знаков из 31 без похожих 0/O, 1/I/L — около 49 бит каждый); показываются ОДИН раз
    при включении второго фактора или при создании новых (старые тогда перестают действовать);
  * в базе — только HMAC-SHA256 кода с ключом, выведенным из AUTH_SECRET_KEY: утечка базы или копии не даёт кодов;
  * код принимается во втором шаге входа вместо 6 цифр, один раз; при входе по коду восстановления человек видит, сколько кодов
    осталось, и советом — включить второй фактор на новом телефоне.
Чистая логика — tests/test_recovery_codes.py.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets

ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
COUNT = 10
LENGTH = 10

_CLEAN_RE = re.compile(r"[\s-]")


def generate(count: int = COUNT) -> list[str]:
    codes = set()
    while len(codes) < count:
        raw = "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))
        codes.add(f"{raw[:5]}-{raw[5:]}")
    return sorted(codes)


def normalize(code: str | None) -> str | None:
    """«7kq4m p2xjr» -> «7KQ4MP2XJR»; не код восстановления — None. 6 цифр (код приложения) сюда не подходят."""
    clean = _CLEAN_RE.sub("", str(code or "")).upper()
    if len(clean) != LENGTH or any(ch not in ALPHABET for ch in clean):
        return None
    return clean


def looks_like_recovery(code: str | None) -> bool:
    return normalize(code) is not None


def digest(code: str, key: bytes) -> str:
    clean = normalize(code)
    if clean is None or not key:
        raise ValueError("не код восстановления или нет ключа")
    return hmac.new(key, clean.encode("ascii"), hashlib.sha256).hexdigest()
