"""Общий счётчик неудачных попыток входа в Redis (app/core/throttle_redis.py). Всегда — на маленьком поддельном Redis в памяти;
на настоящем — если задан REDIS_TEST_URL (например, временный контейнер redis:7-alpine)."""
from __future__ import annotations

import fnmatch
import os
import types
import uuid

import pytest

from app.core.throttle import LoginThrottle
from app.core.throttle_redis import PREFIX, RedisLoginThrottle, build_login_throttle


class FakeRedis:
    """Только команды, которые использует счётчик: сортированные множества, срок жизни, удаление, поиск ключей."""

    def __init__(self):
        self.data: dict[str, dict[str, float]] = {}
        self.ttl: dict[str, int] = {}
        self.down = False

    def _guard(self):
        if self.down:
            raise ConnectionError("redis down")

    def zremrangebyscore(self, key, lo, hi):
        self._guard()
        z = self.data.get(key, {})
        gone = [m for m, s in z.items() if s <= float(hi)]
        for m in gone:
            del z[m]
        return len(gone)

    def zcard(self, key):
        self._guard()
        return len(self.data.get(key, {}))

    def zrange(self, key, start, end, withscores=False):
        self._guard()
        items = sorted(self.data.get(key, {}).items(), key=lambda kv: kv[1])[start:end + 1]
        return [(m, s) for m, s in items] if withscores else [m for m, _ in items]

    def zadd(self, key, mapping):
        self._guard()
        self.data.setdefault(key, {}).update(mapping)

    def expire(self, key, seconds):
        self._guard()
        self.ttl[key] = seconds

    def delete(self, key):
        self._guard()
        self.data.pop(key, None)

    def scan_iter(self, match="*", count=None):
        self._guard()
        return [k for k in list(self.data) if fnmatch.fnmatch(k, match)]

    def pipeline(self):
        outer = self

        class Pipe:
            def __init__(self):
                self.calls = []

            def __getattr__(self, name):
                return lambda *a, **kw: self.calls.append((name, a, kw))

            def execute(self):
                return [getattr(outer, name)(*a, **kw) for name, a, kw in self.calls]

        return Pipe()


def _real_redis():
    url = os.environ.get("REDIS_TEST_URL")
    if not url:
        return None
    redis = pytest.importorskip("redis")
    client = redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1, decode_responses=True)
    client.ping()
    return client


@pytest.fixture(params=["fake", "real"])
def client(request):
    if request.param == "fake":
        return FakeRedis()
    real = _real_redis()
    if real is None:
        pytest.skip("нет REDIS_TEST_URL — настоящего Redis")
    return real


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make(client, clock, **kw):
    # Свой секрет ключей на каждый тест: тесты на настоящем Redis не мешают друг другу.
    params = dict(max_failures_per_user=3, max_failures_per_ip=5, window_seconds=60, key_secret=uuid.uuid4().bytes, clock=clock)
    params.update(kw)
    return RedisLoginThrottle(client, **params)


def test_two_entrances_share_one_counter(client):
    clock = Clock()
    secret = uuid.uuid4().bytes
    api, admin_api = make(client, clock, key_secret=secret), make(client, clock, key_secret=secret)   # два процесса — один Redis
    api.record_failure("Owner", "10.0.0.5")
    admin_api.record_failure("owner ", "100.64.0.9")
    assert api.check("owner", "1.1.1.1") == 0
    api.record_failure("OWNER", "10.0.0.5")
    assert admin_api.check("owner", "8.8.8.8") == 60          # третья неудача на любом входе — блок по логину везде
    assert api.check("other", "8.8.8.8") == 0


def test_window_slides_and_retry_after_counts_from_the_oldest_extra_failure(client):
    clock = Clock()
    t = make(client, clock)
    for _ in range(3):
        t.record_failure("u", "ip")
        clock.t += 10
    assert t.check("u", "ip") == 30                           # первая неудача (t0) уходит из окна в t0+60; сейчас t0+30
    clock.t += 31
    assert t.check("u", "ip") == 0


def test_success_clears_only_the_login_counter_not_the_address(client):
    clock = Clock()
    t = make(client, clock)
    for i in range(5):
        t.record_failure(f"spray{i}", "6.6.6.6")
    assert t.check("fresh", "6.6.6.6") > 0
    t.record_success("spray0")
    assert t.check("fresh", "6.6.6.6") > 0                   # успешный вход не обнуляет лимит адреса
    t.record_failure("u", "ip")
    t.record_failure("u", "ip")
    t.record_success("u")
    t.record_failure("u", "ip")
    assert t.check("u", "ip2") == 0


def test_logins_and_addresses_are_not_stored_in_redis_keys():
    fake = FakeRedis()
    t = make(fake, Clock())
    t.record_failure("m.karimov", "192.168.1.77")
    keys = " ".join(fake.data)
    assert "m.karimov" not in keys and "192.168.1.77" not in keys and all(k.startswith(PREFIX) for k in fake.data)
    assert all(ttl == 61 for ttl in fake.ttl.values())       # ключи сами исчезают после окна


def test_redis_failure_falls_back_to_memory_without_errors_and_returns_later():
    fake, clock, mono = FakeRedis(), Clock(), Clock(0.0)
    memory = LoginThrottle(3, 5, 60, clock=clock)
    t = RedisLoginThrottle(fake, max_failures_per_user=3, max_failures_per_ip=5, window_seconds=60, clock=clock,
                           fallback=memory, retry_seconds=30, monotonic=mono)
    fake.down = True
    for _ in range(3):
        t.record_failure("u", "ip")                           # ни одной ошибки наружу
    assert t.check("u", "ip") > 0 and not t.using_redis()     # лимит действует и без Redis
    fake.down = False
    assert not t.using_redis()                                # до повтора — память
    mono.t += 31
    assert t.using_redis() and t.check("other", "ip9") == 0


def test_reset_removes_only_counter_keys():
    fake = FakeRedis()
    fake.data["celery-task-meta-1"] = {"x": 1.0}               # очередь задач в том же Redis
    t = make(fake, Clock())
    t.record_failure("u", "ip")
    t.reset()
    assert list(fake.data) == ["celery-task-meta-1"]


def _settings(**kw):
    base = dict(auth_max_failed_logins_per_user=5, auth_max_failed_logins_per_ip=20, auth_lockout_window_minutes=15,
                auth_throttle_backend="auto", queue_broker_url="redis://redis:6379/0", auth_secret_key="k")
    return types.SimpleNamespace(**{**base, **kw})


def test_backend_choice():
    made = []
    factory = lambda url: made.append(url) or FakeRedis()    # noqa: E731
    assert isinstance(build_login_throttle(_settings(), redis_factory=factory), RedisLoginThrottle) and made == ["redis://redis:6379/0"]
    assert isinstance(build_login_throttle(_settings(auth_throttle_backend="memory"), redis_factory=factory), LoginThrottle)
    assert isinstance(build_login_throttle(_settings(queue_broker_url="memory://"), redis_factory=factory), LoginThrottle)
    t = build_login_throttle(_settings(auth_throttle_backend="redis"), redis_factory=factory)
    assert isinstance(t, RedisLoginThrottle) and t.max_failures_per_user == 5 and t.window_seconds == 900
