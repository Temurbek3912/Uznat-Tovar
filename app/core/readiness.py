"""
Проверка готовности сервиса (GET /health/ready) — чистая логика без FastAPI/SQLAlchemy, тестируется офлайн.

/health — «процесс жив» (его ждёт scripts/start_demo.py), /health/ready — «можно работать»: БД отвечает, таблицы на
месте, брокер очереди доступен. Именно её использует healthcheck контейнера api в docker-compose.yml.

Ни текст, ни класс исключения наружу НЕ отдаются (единая система ошибок: внутренние подробности клиенту не показываются; адрес
открыт без входа): только «fail». Подробности — в журнал сервера (URL с паролем вырезается).
Исключение — CheckFailed: его текст написан нами и безопасен для показа («нет таблиц: users, stores»).
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping

from app.db.url import redact_database_url

logger = logging.getLogger(__name__)


class CheckFailed(Exception):
    """Проверка не пройдена; сообщение безопасно показывать клиенту."""


def run_checks(checks: Mapping[str, Callable[[], None]]) -> dict:
    """Выполняет все проверки (не останавливаясь на первой). -> {"status": "ok"|"unavailable", "checks": {имя: …}}."""
    results: dict[str, str] = {}
    healthy = True
    for name, check in checks.items():
        try:
            check()
            results[name] = "ok"
        except CheckFailed as exc:
            healthy = False
            results[name] = f"fail: {exc}"
        except Exception as exc:  # noqa: BLE001 — любая ошибка зависимости = «не готов»
            healthy = False
            results[name] = "fail"
            # redact: в тексте ошибки подключения бывает URL с паролем — в журнал он попадать не должен
            logger.warning("Проверка готовности «%s» не пройдена: %s: %s", name, type(exc).__name__, redact_database_url(str(exc)))
    return {"status": "ok" if healthy else "unavailable", "checks": results}
