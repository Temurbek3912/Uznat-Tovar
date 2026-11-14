"""
Общий счётчик неудачных попыток входа в Redis (документ 18, §12.2; шаг Р-5, docs/PLAN_REVIEW_2026-11-13.md).

Зачем. Счётчик в памяти (app/core/throttle.py) у каждого процесса свой: api (порт 8000), admin-api (8001) и admin-api-vpn
считали отдельно, и подбирающий пароль получал втрое больше попыток. Здесь те же правила, но счётчик один на все входы.

Те же решения, что у счётчика в памяти: окно скользящее; по логину и по адресу отдельно; пока ключ заблокирован, новые попытки не
продлевают блокировку (их не записывают — отказ отдаётся до проверки пароля); успех сбрасывает только счётчик логина.

Что хранится в Redis: для каждого ключа — сортированное множество моментов неудач (секунды Unix), срок жизни = окно. Сам логин и
адрес в имя ключа не попадают — только HMAC-SHA256 от них (в Redis нет списка логинов и адресов).

Redis недоступен — без ошибки для человека: счёт переходит на счётчик в памяти этого процесса (прежнее поведение) и повторно
пробует Redis не чаще раза в RETRY_SECONDS. Вход не блокируется из-за сбоя Redis и не становится безлимитным.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import math
import threading
import time
import uuid
from collections.abc import Callable

from app.core.throttle import LoginThrottle

logger = logging.getLogger(__name__)

PREFIX = "uznat:login-throttle:"
RETRY_SECONDS = 30.0
_MAX_KEY_LENGTH = 128


class RedisLoginThrottle:
    """Тот же интерфейс, что у LoginThrottle: check / record_failure / record_success / reset."""

    def __init__(self, client, *, max_failures_per_user: int = 5, max_failures_per_ip: int = 20,
                 window_seconds: float = 15 * 60, key_secret: bytes = b"", clock: Callable[[], float] = time.time,
                 fallback: LoginThrottle | None = None, retry_seconds: float = RETRY_SECONDS,
                 monotonic: Callable[[], float] = time.monotonic):
        if max_failures_per_user < 1 or max_failures_per_ip < 1:
            raise ValueError("Лимиты должны быть >= 1")
        if window_seconds <= 0:
            raise ValueError("Окно должно быть > 0")
        self.client = client
        self.max_failures_per_user = max_failures_per_user
        self.max_failures_per_ip = max_failures_per_ip
        self.window_seconds = float(window_seconds)
        self._secret = key_secret or b"uznat-tovar-login-throttle"
        self._clock = clock
        self._monotonic = monotonic
        self._retry_seconds = retry_seconds
        self.fallback = fallback or LoginThrottle(max_failures_per_user, max_failures_per_ip, window_seconds)
        self._down_until = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ ключи

    def _key(self, kind: str, value: str) -> str:
        digest = hmac.new(self._secret, value.encode("utf-8", "replace"), hashlib.sha256).hexdigest()[:32]
        return f"{PREFIX}{kind}:{digest}"

    def _user_key(self, username: str) -> str:
        return self._key("user", (username or "").strip().lower()[:_MAX_KEY_LENGTH])

    def _ip_key(self, ip: str | None) -> str:
        return self._key("ip", (ip or "unknown")[:_MAX_KEY_LENGTH])

    # ---------------------------------------------------------- Redis или память

    def _redis_available(self) -> bool:
        return self._monotonic() >= self._down_until

    def _redis_failed(self, exc: Exception) -> None:
        with self._lock:
            first = self._down_until == 0.0 or self._monotonic() >= self._down_until
            self._down_until = self._monotonic() + self._retry_seconds
        if first:
            logger.warning("Счётчик попыток входа: Redis недоступен (%s) — считаю в памяти процесса, повтор через %.0f с",
                           type(exc).__name__, self._retry_seconds)

    def using_redis(self) -> bool:
        return self._redis_available()

    # ---------------------------------------------------------------- расчёт

    def _wait_for(self, key: str, limit: int, now: float) -> float:
        cutoff = now - self.window_seconds
        pipe = self.client.pipeline()
        pipe.zremrangebyscore(key, "-inf", cutoff)
        pipe.zcard(key)
        _removed, count = pipe.execute()
        if count < limit:
            return 0.0
        # Блокировка снимается, когда самая старая из «лишних» неудач выйдет из окна (как в LoginThrottle._retry_after).
        stamps = self.client.zrange(key, count - limit, count - limit, withscores=True)
        if not stamps:
            return 0.0
        return max(0.0, float(stamps[0][1]) + self.window_seconds - now)

    def _add(self, key: str, now: float) -> None:
        pipe = self.client.pipeline()
        pipe.zremrangebyscore(key, "-inf", now - self.window_seconds)
        pipe.zadd(key, {f"{now:.6f}:{uuid.uuid4().hex[:8]}": now})
        pipe.expire(key, int(math.ceil(self.window_seconds)) + 1)
        pipe.execute()

    # ---------------------------------------------------------- публичный API

    def check(self, username: str, ip: str | None) -> int:
        if self._redis_available():
            try:
                now = self._clock()
                wait = max(self._wait_for(self._user_key(username), self.max_failures_per_user, now),
                           self._wait_for(self._ip_key(ip), self.max_failures_per_ip, now))
                return math.ceil(wait) if wait > 0 else 0
            except Exception as exc:  # noqa: BLE001 — любой сбой Redis: счёт в памяти
                self._redis_failed(exc)
        return self.fallback.check(username, ip)

    def record_failure(self, username: str, ip: str | None) -> None:
        self.fallback.record_failure(username, ip)       # память ведётся всегда: при сбое Redis неудачи не теряются
        if self._redis_available():
            try:
                now = self._clock()
                self._add(self._user_key(username), now)
                self._add(self._ip_key(ip), now)
            except Exception as exc:  # noqa: BLE001
                self._redis_failed(exc)

    def record_success(self, username: str) -> None:
        self.fallback.record_success(username)
        if self._redis_available():
            try:
                self.client.delete(self._user_key(username))
            except Exception as exc:  # noqa: BLE001
                self._redis_failed(exc)

    def reset(self) -> None:
        """Сбросить все счётчики (тесты, консоль). Удаляет только ключи счётчика, не трогая очередь задач в том же Redis."""
        self.fallback.reset()
        try:
            for key in self.client.scan_iter(match=f"{PREFIX}*", count=500):
                self.client.delete(key)
        except Exception as exc:  # noqa: BLE001
            self._redis_failed(exc)


def build_login_throttle(settings, *, redis_factory=None):
    """Счётчик для этого процесса по настройке AUTH_THROTTLE_BACKEND: memory | redis | auto (по умолчанию — Redis очереди, если
    адрес очереди — redis://, иначе память)."""
    memory = LoginThrottle(max_failures_per_user=settings.auth_max_failed_logins_per_user,
                           max_failures_per_ip=settings.auth_max_failed_logins_per_ip,
                           window_seconds=settings.auth_lockout_window_minutes * 60)
    backend = (getattr(settings, "auth_throttle_backend", "auto") or "auto").strip().lower()
    url = getattr(settings, "queue_broker_url", "") or ""
    if backend == "memory" or (backend == "auto" and not url.startswith(("redis://", "rediss://"))):
        return memory
    if redis_factory is None:
        import redis  # noqa: PLC0415
        redis_factory = lambda u: redis.Redis.from_url(u, socket_connect_timeout=0.5, socket_timeout=0.5)  # noqa: E731
    secret = (getattr(settings, "auth_secret_key", "") or "").encode("utf-8")
    return RedisLoginThrottle(redis_factory(url), max_failures_per_user=settings.auth_max_failed_logins_per_user,
                              max_failures_per_ip=settings.auth_max_failed_logins_per_ip,
                              window_seconds=settings.auth_lockout_window_minutes * 60,
                              key_secret=hashlib.sha256(b"login-throttle:" + secret).digest(), fallback=memory)
