"""
Ограничение частоты неудачных попыток входа (F13): защита от подбора пароля.

Только stdlib, без FastAPI/БД — логика проверяется офлайн (tests/test_throttle.py).

Два независимых счётчика неудач в скользящем окне:
  * по ЛОГИНУ — против подбора пароля к конкретной учётке, откуда бы ни шли попытки;
  * по IP     — против перебора многих логинов с одного адреса (credential stuffing).

Решения, которые стоит знать:
  * Счётчик по логину ведётся для ЛЮБОЙ введённой строки, а не только для существующих
    пользователей — иначе по появлению 429 можно было бы отличить «логин есть» от «логина нет».
  * Пока ключ заблокирован, новые попытки НЕ продлевают блокировку (отказ отдаётся до проверки
    пароля и неудачей не записывается): атакующий не может держать чужую учётку заблокированной
    вечно одним потоком запросов, а лишние запросы не жгут CPU на scrypt.
  * Успешный вход сбрасывает счётчик логина, но НЕ счётчик IP: иначе атакующий с одной
    валидной учёткой обнулял бы свой лимит после каждого удачного входа.
  * Память ограничена (max_keys): при переполнении вытесняются давно не трогавшиеся ключи.

Ограничения (см. docs/HANDOFF.md): состояние живёт в памяти ОДНОГО процесса. При нескольких
воркерах uvicorn или репликах API эффективный лимит умножается на их число; для этого нужен
общий счётчик (Redis уже есть в docker-compose). Блокировка по логину позволяет постороннему
временно «запереть» известную учётку — плата за защиту от подбора; блокировка временная.
"""
from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable

_MAX_KEY_LENGTH = 128  # длинные «логины» от атакующего не должны раздувать память


class LoginThrottle:
    def __init__(
        self,
        max_failures_per_user: int = 5,
        max_failures_per_ip: int = 20,
        window_seconds: float = 15 * 60,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ):
        if max_failures_per_user < 1 or max_failures_per_ip < 1:
            raise ValueError("Лимиты должны быть >= 1")
        if window_seconds <= 0:
            raise ValueError("Окно должно быть > 0")
        self.max_failures_per_user = max_failures_per_user
        self.max_failures_per_ip = max_failures_per_ip
        self.window_seconds = float(window_seconds)
        self.max_keys = max_keys
        self._clock = clock
        self._lock = threading.Lock()
        self._users: OrderedDict[str, deque[float]] = OrderedDict()
        self._ips: OrderedDict[str, deque[float]] = OrderedDict()

    # ------------------------------------------------------------------ ключи

    @staticmethod
    def _user_key(username: str) -> str:
        # Та же каноничная форма, что и при поиске пользователя (security.canonical_username),
        # иначе «Admin» и «admin » считались бы разными счётчиками — лимит обходился бы регистром.
        return (username or "").strip().lower()[:_MAX_KEY_LENGTH]

    @staticmethod
    def _ip_key(ip: str | None) -> str:
        return (ip or "unknown")[:_MAX_KEY_LENGTH]

    # ---------------------------------------------------------------- внутреннее

    def _fresh(self, bucket: OrderedDict[str, deque[float]], key: str, now: float) -> deque[float] | None:
        """Возвращает окно неудач ключа без просроченных отметок (или None, если их не осталось)."""
        stamps = bucket.get(key)
        if stamps is None:
            return None
        cutoff = now - self.window_seconds
        while stamps and stamps[0] <= cutoff:
            stamps.popleft()
        if not stamps:
            del bucket[key]
            return None
        return stamps

    def _retry_after(self, stamps: deque[float] | None, limit: int, now: float) -> float:
        if stamps is None or len(stamps) < limit:
            return 0.0
        # Блокировка снимается, когда самая старая из «лишних» неудач выйдет из окна.
        return max(0.0, stamps[len(stamps) - limit] + self.window_seconds - now)

    def _add(self, bucket: OrderedDict[str, deque[float]], key: str, now: float) -> None:
        stamps = self._fresh(bucket, key, now)
        if stamps is None:
            stamps = deque()
            bucket[key] = stamps
        stamps.append(now)
        bucket.move_to_end(key)
        while len(bucket) > self.max_keys:
            bucket.popitem(last=False)  # самый давно тронутый ключ

    # -------------------------------------------------------------- публичный API

    def check(self, username: str, ip: str | None, *, skip_ip: bool = False) -> int:
        """Сколько секунд ждать до следующей попытки; 0 — попытка разрешена. skip_ip — адрес клиента неизвестен по устройству
        (вход покупателя через туннель без заголовка адреса): считается только общий предел, а не «все — один адрес»."""
        now = self._clock()
        with self._lock:
            wait = max(
                self._retry_after(
                    self._fresh(self._users, self._user_key(username), now), self.max_failures_per_user, now
                ),
                0 if skip_ip else self._retry_after(
                    self._fresh(self._ips, self._ip_key(ip), now), self.max_failures_per_ip, now
                ),
            )
        return math.ceil(wait) if wait > 0 else 0

    def record_failure(self, username: str, ip: str | None, *, skip_ip: bool = False) -> None:
        now = self._clock()
        with self._lock:
            self._add(self._users, self._user_key(username), now)
            if not skip_ip:
                self._add(self._ips, self._ip_key(ip), now)

    def record_success(self, username: str) -> None:
        with self._lock:
            self._users.pop(self._user_key(username), None)

    def reset(self) -> None:
        with self._lock:
            self._users.clear()
            self._ips.clear()
