"""Общие настройки pytest.

Метка `environment` — тесты СООТВЕТСТВИЯ ОКРУЖЕНИЯ требованиям проекта (а не логики кода): например, что установленный cv2 —
одна сборка OpenCV 4.x с файлом каскада лиц (requirements.txt, constraints-opencv.txt). В несоответствующем окружении они
ПАДАЮТ — это правильно, падение не скрывается и не пропускается. Чтобы причина была видна сразу, в конце прогона печатается
диагноз окружения (то же, что `python -m scripts.check_opencv`).
"""
from __future__ import annotations

import os

# Тесты не должны зависеть от того, запущен ли на этом компьютере Redis: счётчик попыток входа — в памяти. Общий счётчик в Redis
# проверяется отдельно (tests/test_throttle_redis.py).
os.environ.setdefault("AUTH_THROTTLE_BACKEND", "memory")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_vision_download(monkeypatch):
    """Тесты не скачивают модель «зрение + текст» (3 ГБ) и не копируют её из LM Studio этого компьютера (scripts/vision_engine.py)."""
    from scripts import vision_engine

    def refuse(*args, **kwargs):
        raise OSError("tests: no downloads")
    monkeypatch.setattr(vision_engine, "http_download", refuse)
    monkeypatch.setattr(vision_engine, "lmstudio_copy", lambda part, home=None: None)


def pytest_configure(config):
    config.addinivalue_line("markers", "environment: проверка соответствия окружения требованиям проекта (не логики кода)")


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    failed = [r for r in terminalreporter.stats.get("failed", []) if "environment" in getattr(r, "keywords", {})]
    if not failed:
        return
    terminalreporter.section("окружение не соответствует требованиям проекта")
    for report in failed:
        terminalreporter.write_line(f"  {report.nodeid}")
    try:
        import cv2

        from app.core import opencv_check as oc
        dists = oc.installed_distributions()
        for line in oc.report_lines(cv2, dists):
            terminalreporter.write_line(f"  {line}")
        for problem in oc.distribution_problems(dists) + oc.capability_problems(cv2):
            terminalreporter.write_line(f"  - {problem}")
    except Exception as exc:  # noqa: BLE001 — диагноз не должен ронять прогон
        terminalreporter.write_line(f"  диагноз OpenCV не получен: {type(exc).__name__}: {exc}")
    terminalreporter.write_line("  Решение: окружение по requirements.txt с constraints-opencv.txt (так собирается образ Docker; "
                                "сборка падает на scripts/check_opencv). Код приложения менять не нужно.")
