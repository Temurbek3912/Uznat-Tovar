"""
Пароли, коды второго фактора и токены — только по защищённому каналу (решение владельца 2026-11-08 21:53, документ 18, §14).

Защищённым считается запрос, пришедший:
  * по HTTPS — scope["scheme"] == "https": TLS самого uvicorn (docker-compose.https.yml, scripts/make_local_tls.py) или доверенный
    обратный прокси (uvicorn --proxy-headers с --forwarded-allow-ips; заголовок от недоверенного адреса не учитывается — проверено
    на настоящем uvicorn, tests/test_real_http_server.py);
  * на административный вход (mfa_policy.listener_for_request == admin): он опубликован только на 127.0.0.1 и на адресе VPN, то есть
    канал — либо сам компьютер, либо зашифрованный VPN; в режиме «all» — только запрос на петлевой адрес с самого компьютера.
Всё остальное (порт сотрудников по HTTP через Wi-Fi магазина) — незащищённый канал.

При auth.require_secure_transport = true (по умолчанию) запросы к /api/v1/* по незащищённому каналу отклоняются ответом 403
insecure_transport — кроме GET /api/v1/auth/transport (страница спрашивает, можно ли отправлять пароль, ДО отправки) и проверок
состояния (/health*). Страницы (frontend/auth.js) сначала спрашивают сервер и пароль по такому каналу не отправляют вовсе:
отказ сервера — вторая линия для старых клиентов. Токен с защищённого адреса на незащищённый не попадает сам: sessionStorage
у https://… и http://… — разные источники.

Чистые функции и ASGI-обёртка без FastAPI.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping

from app.core.mfa_policy import LISTENER_ADMIN, listener_for_request

EXEMPT_PATHS = ("/api/v1/auth/transport",)
PROTECTED_PREFIX = "/api/v1/"


def is_secure(*, scheme: str | None, request_listener: str) -> bool:
    return scheme == "https" or request_listener == LISTENER_ADMIN


def needs_check(path: str) -> bool:
    return path.startswith(PROTECTED_PREFIX) and path not in EXEMPT_PATHS


def describe(*, secure: bool, required: bool, https_url: str | None) -> dict:
    """Ответ GET /api/v1/auth/transport: можно ли отправлять пароль отсюда и куда идти, если нельзя. Без секретов."""
    return {"secure": secure, "required": required, "https_url": https_url or None}


class _Headers(Mapping):
    def __init__(self, raw):
        self._d = {}
        for k, v in raw or []:
            self._d.setdefault(k.decode("latin-1").lower(), v.decode("latin-1"))

    def __getitem__(self, key):
        return self._d[key.lower()]

    def __iter__(self):
        return iter(self._d)

    def __len__(self):
        return len(self._d)

    def get(self, key, default=None):
        return self._d.get(key.lower(), default)


def secure_for_scope(scope, settings) -> bool:
    headers = _Headers(scope.get("headers"))
    listener = listener_for_request(settings.app_listener, scope, headers, vpn_bind_ip=settings.vpn_bind_ip,
                                    extra_hosts=settings.admin_allowed_hosts)
    return is_secure(scheme=scope.get("scheme"), request_listener=listener)


class SecureTransportMiddleware:
    """403 insecure_transport на /api/v1/* по незащищённому каналу, если так требует настройка (settings_fn() читается на каждый
    запрос — тесты и перезапуск с другой настройкой видят новое значение)."""

    def __init__(self, app, settings_fn: Callable):
        self.app = app
        self.settings_fn = settings_fn

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not needs_check(str(scope.get("path") or "")):
            return await self.app(scope, receive, send)
        settings = self.settings_fn()
        if not settings.auth_require_secure_transport or secure_for_scope(scope, settings):
            return await self.app(scope, receive, send)
        body = json.dumps({
            "detail": "Вход и работа с программой — только по защищённому адресу (HTTPS) или через VPN",
            "code": "insecure_transport",
            "params": {"url": settings.public_https_url or ""},
        }, ensure_ascii=False).encode("utf-8")
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                                (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})
