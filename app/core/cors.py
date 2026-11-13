"""
Список источников (origins), которым браузер разрешено обращаться к API с другого адреса (CORS), — чистая логика без
FastAPI, поэтому проверяется офлайн (tests/test_cors.py).

Страницы проекта раздаются самим API (http://<сервер>:8000/ui/...) и ходят на тот же источник — CORS им не нужен. Он нужен
только страницам, открытым как файл с диска (origin «null»), и страницам на другом адресе, которые указывают API через
?api_base=... Токен передаётся заголовком, а не cookie, поэтому «*» не даёт чужому сайту доступа к данным без токена,
но и без необходимости открытым его оставлять не стоит.

Переменная окружения CORS_ALLOW_ORIGINS (через запятую):
    не задана / пусто  -> development: любой источник («*», как раньше); production: кросс-доменный доступ ЗАКРЫТ;
    https://a.example.com,http://192.168.1.50:8000 -> только перечисленные;
    *                  -> любой источник (осознанный выбор; в production пишется предупреждение).
Ошибка в значении -> ValueError при старте (лучше упасть сразу, чем тихо не пускать страницу).
"""
from __future__ import annotations

import re

# схема://хост[:порт] — без пути, запроса и подстановочных знаков. Хост: имя/IPv4 или IPv6 в квадратных скобках.
_ORIGIN_RE = re.compile(r"^(https?)://([a-z0-9](?:[a-z0-9.-]*[a-z0-9])?|\[[0-9a-f:]+\])(?::(\d{1,5}))?$")


def is_production(environment: str | None) -> bool:
    """То же правило, что в app/main.py для /docs: ровно «production»."""
    return (environment or "") == "production"


def _normalize_origin(item: str) -> str:
    candidate = item[:-1] if item.endswith("/") else item          # «http://host:8000/» — частая опечатка; браузер шлёт без «/»
    if "*" in candidate:
        raise ValueError(
            f"CORS_ALLOW_ORIGINS: подстановочные знаки в адресе не поддерживаются: «{item}». "
            "Перечислите адреса явно или используйте одно значение «*»."
        )
    match = _ORIGIN_RE.match(candidate.lower())
    if not match:
        raise ValueError(
            f"CORS_ALLOW_ORIGINS: «{item}» — не адрес источника. Нужен вид http://host или https://host:порт "
            "(без пути после адреса)."
        )
    port = match.group(3)
    if port is not None and not 1 <= int(port) <= 65535:
        raise ValueError(f"CORS_ALLOW_ORIGINS: порт в «{item}» вне диапазона 1–65535.")
    return candidate.lower()


def resolve_cors_origins(raw: str | None, environment: str | None) -> list[str]:
    """Значение CORS_ALLOW_ORIGINS + режим -> список для CORSMiddleware. Пустой список = middleware не нужен."""
    items = [part.strip() for part in (raw or "").split(",") if part.strip()]
    if not items:
        return [] if is_production(environment) else ["*"]
    if "*" in items:
        if len(items) > 1:
            raise ValueError("CORS_ALLOW_ORIGINS: «*» нельзя сочетать с конкретными адресами — оставьте что-то одно.")
        return ["*"]
    origins: list[str] = []
    for item in items:
        origin = _normalize_origin(item)
        if origin not in origins:
            origins.append(origin)
    return origins


def describe_cors(origins: list[str], environment: str | None) -> str:
    """Одна строка для журнала запуска: что именно сейчас разрешено."""
    if not origins:
        return "CORS выключен: страницы работают только с того же адреса, что и API"
    if origins == ["*"]:
        if is_production(environment):
            return "CORS: разрешён ЛЮБОЙ источник (в production это небезопасно — задайте CORS_ALLOW_ORIGINS списком адресов)"
        return "CORS: разрешён ЛЮБОЙ источник"
    return "CORS: разрешены только " + ", ".join(origins)
