"""
Временный пароль (документ 05, §12.2–§12.4): выдаётся при создании сотрудника и при сбросе, действует только для установки своего
пароля при первом входе, у него есть срок.

  * генерирует сервер (secrets): 12 символов из алфавита без похожих знаков (нет 0/O, 1/l/I), группами по 4 — «7KQM-X4HP-9TDW»;
    31 символ алфавита -> около 59 бит случайности;
  * хранится только хеш — тем же алгоритмом, что и постоянные пароли (он и есть текущий пароль учётной записи, пока не заменён);
  * вход с ним даёт не сеанс, а пропуск «смена пароля» на 15 минут (mfa_policy.SCOPE_PASSWORD), который принимает только маршрут
    установки пароля; после установки нового пароля временный перестаёт существовать (хеш заменён);
  * срок — настройка auth.temp_password_hours (вопрос Q-19 открыт; рекомендация документа 05 — 24 часа); пароль владельца из .env при
    первом запуске (bootstrap) — без срока, но тоже только для первой смены.
Чистые функции: без базы и FastAPI.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta

ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"   # без 0, 1, I, L, O — их путают при переписывании с листка
GROUPS = 3
GROUP_LEN = 4
PASS_TTL_MINUTES = 15                       # срок пропуска «смена пароля»


def generate() -> str:
    return "-".join("".join(secrets.choice(ALPHABET) for _ in range(GROUP_LEN)) for _ in range(GROUPS))


def expires_at(now: datetime, hours: int | float | None) -> datetime | None:
    """Срок временного пароля. hours не задан или ≤ 0 — без срока (только пароль из .env при первом запуске)."""
    if not hours or hours <= 0:
        return None
    return now + timedelta(hours=float(hours))


def is_expired(deadline: datetime | None, now: datetime) -> bool:
    if deadline is None:
        return False
    if deadline.tzinfo is None and now.tzinfo is not None:      # SQLite возвращает время без пояса
        now = now.replace(tzinfo=None)
    return now >= deadline


def entropy_bits() -> float:
    import math
    return GROUPS * GROUP_LEN * math.log2(len(ALPHABET))
