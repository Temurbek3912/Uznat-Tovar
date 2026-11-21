"""
Предел размера тела запроса ДО разбора (2026-11-07).

Зачем. Starlette разбирает `multipart/form-data` целиком ещё до того, как маршрут проверит размер файла (`_read_limited`
в routes_catalog). Файловые части больше 1 МБ при этом пишутся во временный файл на диске, а общего предела размера
у разбора нет. Значит, любой клиент — даже без входа, потому что тело разбирается раньше проверки прав — мог заставить
сервер принять и записать на диск запрос любого размера: это и отказ в обслуживании (диск, память), и лишняя запись
кадров покупателей на диск (D-10а).

Что делает это звено (чистое ASGI, без зависимостей):
  1. Объявленный `Content-Length` больше предела → сразу 413, тело не читается вовсе.
  2. Некорректный `Content-Length` → 400.
  3. Тело без длины (chunked) или с заниженной длиной → байты считаются по мере чтения; как только предел превышен,
     чтение прекращается (дальше `receive` бросает `BodyTooLarge`), а клиент получает 413 — даже если фреймворк
     превратил исключение в свой ответ (FastAPI отдаёт 400 «There was an error parsing the body»).

Предел выбирается по пути (`BodyLimitRule`), остальные маршруты получают `default_limit`.
Чего звено НЕ делает: не убирает запись частей ≤ предела во временный файл Starlette — для кадра это решено в
app/core/memory_upload.py (кадр целиком в памяти, без временного файла; D-10а).
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass

MB = 1024 * 1024


class BodyTooLarge(Exception):
    """Тело запроса превысило предел; дальнейшее чтение запрещено."""


@dataclass(frozen=True)
class BodyLimitRule:
    """Путь (регулярное выражение, целиком) → предел тела в байтах."""

    pattern: str
    limit: int

    def matches(self, path: str) -> bool:
        return re.fullmatch(self.pattern, path) is not None


def _declared_length(headers: Iterable[tuple[bytes, bytes]]) -> tuple[bool, int | None]:
    """(корректно ли, длина или None). Несколько разных Content-Length или не число — некорректно."""
    values = {value.strip() for name, value in headers if name.lower() == b"content-length"}
    if not values:
        return True, None
    if len(values) != 1:
        return False, None
    raw = next(iter(values))
    if not raw.isdigit():
        return False, None
    return True, int(raw)


def _human(limit: int) -> str:
    if limit >= MB:
        return f"{limit / MB:g} МБ"
    return f"{max(1, limit // 1024)} КБ"


async def _respond(send, status: int, code: str, detail: str) -> None:
    body = json.dumps({"detail": detail, "code": code}, ensure_ascii=False).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json; charset=utf-8"),
                            (b"content-length", str(len(body)).encode("ascii"))]})
    await send({"type": "http.response.body", "body": body})


class BodySizeLimitMiddleware:
    """Ограничивает размер тела HTTP-запроса до того, как его начнёт разбирать приложение."""

    def __init__(self, app, *, default_limit: int, rules: Iterable[BodyLimitRule] = ()):
        if default_limit < 0:
            raise ValueError("default_limit must be >= 0")
        self.app = app
        self.default_limit = default_limit
        self.rules = tuple(rules)

    def limit_for(self, path: str) -> int:
        for rule in self.rules:
            if rule.matches(path):
                return rule.limit
        return self.default_limit

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        limit = self.limit_for(scope.get("path", ""))
        valid, declared = _declared_length(scope.get("headers") or [])
        if not valid:
            await _respond(send, 400, "invalid_content_length", "Некорректный заголовок Content-Length.")
            return
        too_large_detail = f"Запрос больше допустимого ({_human(limit)})."
        if declared is not None and declared > limit:
            await _respond(send, 413, "request_too_large", too_large_detail)
            return

        received = 0
        exceeded = False
        app_started = False      # приложение начало СВОЙ ответ до превышения
        rejected = False         # наш 413 уже отправлен

        async def limited_receive():
            nonlocal received, exceeded
            if exceeded:
                raise BodyTooLarge()
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise BodyTooLarge()
            return message

        async def guarded_send(message):
            nonlocal app_started, rejected
            if exceeded and not app_started:
                # Ответ приложения после превышения (например, 400 от фреймворка) заменяем одним 413.
                if not rejected:
                    rejected = True
                    await _respond(send, 413, "request_too_large", too_large_detail)
                return
            if message.get("type") == "http.response.start":
                app_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if exceeded and not app_started:
                if not rejected:
                    rejected = True
                    await _respond(send, 413, "request_too_large", too_large_detail)
                return
            raise
        if exceeded and not app_started and not rejected:
            await _respond(send, 413, "request_too_large", too_large_detail)


DEFAULT_LIMIT = 1 * MB            # JSON-запросы (вход, пользователи, магазины, правка товара): с большим запасом
MULTIPART_OVERHEAD = 1 * MB       # границы частей, заголовки частей, текстовые поля формы
IDENTIFY_OVERHEAD = 256 * 1024


def build_rules(*, identify_max_mb: int, max_upload_mb: int, max_photos_per_request: int) -> tuple[BodyLimitRule, ...]:
    """Пределы для маршрутов с файлами. Не меньше, чем пропускают проверки самих маршрутов, плюс накладные расходы формы."""
    photos = max_upload_mb * MB * max_photos_per_request + MULTIPART_OVERHEAD
    return (
        BodyLimitRule(r"/api/v1/catalog/identify", identify_max_mb * MB + IDENTIFY_OVERHEAD),
        BodyLimitRule(r"/api/v1/public/identify", identify_max_mb * MB + IDENTIFY_OVERHEAD),
        BodyLimitRule(r"/api/v1/catalog/products/upload", photos),
        BodyLimitRule(r"/api/v1/catalog/products/[^/]+/photos", photos),
    )
