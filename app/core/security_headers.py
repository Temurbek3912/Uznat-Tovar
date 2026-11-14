"""
Заголовки безопасности для всех ответов (страницы /ui и API), 2026-11-07.

Только те заголовки, которые не ломают текущие страницы:
  - X-Content-Type-Options: nosniff — браузер не «угадывает» тип файла (загруженное как картинка не исполнится как скрипт);
  - X-Frame-Options: DENY и CSP frame-ancestors 'none' — страницы нельзя встроить в чужой сайт (защита от подмены кликов
    по невидимой рамке: «Удалить», «Сохранить»);
  - CSP base-uri 'self' и object-src 'none' — нельзя подменить базовый адрес страницы и встроить плагин;
  - CSP script-src 'self' (с 2026-11-07, после выноса встроенных скриптов в файлы) — выполняются только скрипты с самого сервера:
    внедрённый в страницу <script> или обработчик onerror="…" браузер не выполнит (защита от XSS);
  - Referrer-Policy: no-referrer — адреса страниц не уходят на сторонние сайты;
  - Permissions-Policy — камера только для самого сайта (живая камера «Узнать товар»), микрофон и геолокация выключены.

HSTS (Strict-Transport-Security, документ 18) — ТОЛЬКО на ответах запросов, пришедших по HTTPS (scope["scheme"] == "https": прямой
TLS в uvicorn или обратный прокси при uvicorn --proxy-headers и доверенном --forwarded-allow-ips). По HTTP заголовок не
отправляется: браузер его там всё равно игнорирует, а адрес http://192.168.x.x:8000 в магазине продолжает работать.
Срок 180 дней, без includeSubDomains и preload: домен владельца может обслуживать и другие сайты (решение — при выборе домена).

CSP style-src 'self' (с 2026-11-08, после выноса встроенных <style> и атрибутов style="…" в файлы CSS): внедрённые в страницу стили
браузер не применит (нельзя спрятать или подменить кнопки внедрённым CSS). Изменение стилей из своих скриптов через element.style
CSP не запрещает. Чего здесь сознательно НЕТ: default-src — камера и изображения blob: не ограничиваются.
Уже заданный заголовок не перезаписывается.

Cache-Control: no-store (аудит 2026-11-08) — на ответах входа и управления пользователями (/api/v1/auth/*, /api/v1/users*):
там токены, секрет второго фактора и коды восстановления; на общем компьютере магазина они не должны оставаться в кеше браузера
или прокси (RFC 6749, §5.1 требует того же для ответов с токеном).

Cache-Control: no-cache (2026-11-14) — на страницах и скриптах /ui: браузер каждый раз сверяется с сервером (ETag, ответ 304 без
тела — дёшево). Без него браузер по своей эвристике держал старые страницы после обновления программы: найдено вживую — после
обновления «Настроек» браузер показал прежнюю страницу без QR-кода второго фактора.
"""
from __future__ import annotations

SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(self), microphone=(), geolocation=()"),
)
HSTS_HEADER: tuple[bytes, bytes] = (b"strict-transport-security", b"max-age=15552000")
NO_STORE_HEADERS: tuple[tuple[bytes, bytes], ...] = ((b"cache-control", b"no-store"), (b"pragma", b"no-cache"))
NO_STORE_PREFIXES = ("/api/v1/auth", "/api/v1/users")
REVALIDATE_HEADERS: tuple[tuple[bytes, bytes], ...] = ((b"cache-control", b"no-cache"),)
REVALIDATE_PREFIXES = ("/ui/", "/ui")


def _extra_for(scope) -> tuple[tuple[bytes, bytes], ...]:
    scope = scope or {}
    extra = SECURITY_HEADERS + ((HSTS_HEADER,) if scope.get("scheme") == "https" else ())
    path = str(scope.get("path") or "")
    if path.startswith(NO_STORE_PREFIXES):
        extra += NO_STORE_HEADERS
    elif path.startswith(REVALIDATE_PREFIXES):
        extra += REVALIDATE_HEADERS
    return extra


def headers_for(scope) -> dict[str, str]:
    """Те же заголовки словарём — для ответа 500 на необработанное исключение. Его Starlette (и FastAPI) отдаёт из
    ServerErrorMiddleware, САМОГО ВНЕШНЕГО звена, мимо SecurityHeadersMiddleware; найдено проверкой на настоящем uvicorn
    (tests/test_real_http_server.py, 2026-11-08)."""
    return {name.decode("latin-1"): value.decode("latin-1") for name, value in _extra_for(scope)}


class SecurityHeadersMiddleware:
    """Чистое ASGI-промежуточное звено: добавляет SECURITY_HEADERS к каждому HTTP-ответу."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        extra = _extra_for(scope)

        async def send_with_headers(message):
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {name.lower() for name, _ in headers}
                headers += [(name, value) for name, value in extra if name not in present]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
