"""Тесты лимитера попыток входа (app/core/throttle.py) на управляемых часах."""
from __future__ import annotations

import pytest

from app.core.throttle import LoginThrottle


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make(clock: FakeClock, **kwargs) -> LoginThrottle:
    params = dict(max_failures_per_user=3, max_failures_per_ip=10, window_seconds=600, clock=clock)
    params.update(kwargs)
    return LoginThrottle(**params)


def fail(t: LoginThrottle, username: str, ip: str = "1.1.1.1", times: int = 1) -> None:
    for _ in range(times):
        t.record_failure(username, ip)


class TestPerUser:
    def test_allowed_until_limit_reached(self):
        clock = FakeClock()
        t = make(clock)
        fail(t, "ivan", times=2)
        assert t.check("ivan", "1.1.1.1") == 0
        fail(t, "ivan")
        assert t.check("ivan", "1.1.1.1") > 0

    def test_other_users_are_unaffected(self):
        t = make(FakeClock())
        fail(t, "ivan", times=3)
        assert t.check("ivan", "9.9.9.9") > 0     # блок по логину не зависит от IP
        assert t.check("petr", "9.9.9.9") == 0

    def test_username_key_is_canonical(self):
        # Обход лимита сменой регистра/пробелов не должен работать.
        t = make(FakeClock())
        fail(t, "Ivan", times=1)
        fail(t, " IVAN ", times=1)
        fail(t, "ivan", times=1)
        assert t.check("iVaN", "1.1.1.1") > 0

    def test_retry_after_counts_down_and_unblocks_after_window(self):
        clock = FakeClock()
        t = make(clock, window_seconds=600)
        fail(t, "ivan", times=3)
        assert t.check("ivan", "1.1.1.1") == 600
        clock.advance(100)
        assert t.check("ivan", "1.1.1.1") == 500
        clock.advance(499)
        assert t.check("ivan", "1.1.1.1") == 1
        clock.advance(1)
        assert t.check("ivan", "1.1.1.1") == 0

    def test_sliding_window_old_failures_expire_individually(self):
        clock = FakeClock()
        t = make(clock, window_seconds=600)
        fail(t, "ivan")            # t=0
        clock.advance(300)
        fail(t, "ivan", times=2)   # t=300 -> заблокирован
        assert t.check("ivan", "1.1.1.1") == 300      # ждать, пока выйдет первая неудача (t=0+600)
        clock.advance(300)
        assert t.check("ivan", "1.1.1.1") == 0         # осталось 2 неудачи < лимита 3

    def test_blocked_attempts_do_not_extend_the_lock(self):
        # Роут не пишет неудачу, пока check() > 0, — эмулируем: только check(), без record_failure.
        clock = FakeClock()
        t = make(clock, window_seconds=600)
        fail(t, "ivan", times=3)
        for _ in range(50):
            clock.advance(10)
            assert t.check("ivan", "1.1.1.1") > 0
        clock.advance(101)  # суммарно 601 c с момента неудач
        assert t.check("ivan", "1.1.1.1") == 0

    def test_success_clears_user_counter(self):
        t = make(FakeClock())
        fail(t, "ivan", times=2)
        t.record_success("ivan")
        fail(t, "ivan", times=2)
        assert t.check("ivan", "1.1.1.1") == 0

    def test_unknown_usernames_are_throttled_too(self):
        # Иначе 429 выдавал бы, что такой логин существует.
        t = make(FakeClock())
        fail(t, "no-such-user", times=3)
        assert t.check("no-such-user", "1.1.1.1") > 0

    def test_empty_and_none_like_values_do_not_crash(self):
        t = make(FakeClock())
        t.record_failure("", None)
        assert t.check("", None) == 0


class TestPerIp:
    def test_many_usernames_from_one_ip_get_blocked(self):
        t = make(FakeClock(), max_failures_per_ip=5)
        for i in range(5):
            t.record_failure(f"user{i}", "6.6.6.6")
        assert t.check("brand-new-user", "6.6.6.6") > 0    # свежий логин, но IP уже исчерпал лимит
        assert t.check("brand-new-user", "7.7.7.7") == 0

    def test_success_does_not_clear_ip_counter(self):
        t = make(FakeClock(), max_failures_per_ip=5)
        for i in range(4):
            t.record_failure(f"victim{i}", "6.6.6.6")
        t.record_success("my-own-valid-account")
        t.record_failure("victim4", "6.6.6.6")
        assert t.check("anyone", "6.6.6.6") > 0

    def test_ip_block_expires(self):
        clock = FakeClock()
        t = make(clock, max_failures_per_ip=2, window_seconds=60)
        fail(t, "a", "6.6.6.6")
        fail(t, "b", "6.6.6.6")
        assert t.check("c", "6.6.6.6") == 60
        clock.advance(60)
        assert t.check("c", "6.6.6.6") == 0

    def test_missing_ip_shares_one_bucket(self):
        t = make(FakeClock(), max_failures_per_ip=2)
        t.record_failure("a", None)
        t.record_failure("b", None)
        assert t.check("c", None) > 0


class TestBoundsAndValidation:
    def test_memory_is_bounded(self):
        t = make(FakeClock(), max_keys=100, max_failures_per_ip=10**6)
        for i in range(1000):
            t.record_failure(f"user-{i}", f"10.0.{i // 250}.{i % 250}")
        assert len(t._users) <= 100
        assert len(t._ips) <= 100

    def test_expired_entries_are_pruned_from_memory(self):
        # Поведение check() от очистки не зависит (блок определяется по возрасту отметок),
        # но без неё отметки копились бы вечно — проверяем именно память.
        clock = FakeClock()
        t = make(clock, window_seconds=600)
        fail(t, "ivan", "1.1.1.1", times=2)
        clock.advance(601)
        assert t.check("ivan", "1.1.1.1") == 0
        assert "ivan" not in t._users
        assert "1.1.1.1" not in t._ips

        for _ in range(100):               # редкие неудачи в течение месяцев не должны копиться
            fail(t, "petr", "2.2.2.2")
            clock.advance(601)
        fail(t, "petr", "2.2.2.2")
        assert len(t._users["petr"]) == 1

    def test_recently_touched_keys_survive_eviction(self):
        t = make(FakeClock(), max_keys=3, max_failures_per_user=2, max_failures_per_ip=10**6)
        fail(t, "target", "1.1.1.1")
        for i in range(5):
            fail(t, f"noise-{i}", "1.1.1.1")
            fail(t, "target", "1.1.1.1")     # цель постоянно «трогается» — вытеснять её нельзя
        assert t.check("target", "1.1.1.1") > 0

    def test_very_long_username_is_truncated_for_key(self):
        t = make(FakeClock())
        t.record_failure("x" * 100_000, "1.1.1.1")
        assert all(len(k) <= 128 for k in t._users)

    def test_invalid_configuration_rejected(self):
        with pytest.raises(ValueError):
            LoginThrottle(max_failures_per_user=0)
        with pytest.raises(ValueError):
            LoginThrottle(max_failures_per_ip=0)
        with pytest.raises(ValueError):
            LoginThrottle(window_seconds=0)

    def test_reset_clears_everything(self):
        t = make(FakeClock())
        fail(t, "ivan", times=3)
        t.reset()
        assert t.check("ivan", "1.1.1.1") == 0


class TestThreadSafety:
    def test_concurrent_failures_are_all_counted(self):
        import threading

        t = make(FakeClock(), max_failures_per_user=10**6, max_failures_per_ip=10**6)

        def worker():
            for _ in range(500):
                t.record_failure("ivan", "1.1.1.1")

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert len(t._users["ivan"]) == 8 * 500
