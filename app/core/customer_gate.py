"""
Вход покупателя из интернета (docs/PUBLIC_ACCESS.md): отдельный сокет программы только на 127.0.0.1 (порт CUSTOMER_PORT, по
умолчанию 8100), к которому подключается туннель — Tailscale Funnel, Cloudflare Tunnel или свой обратный прокси программиста
магазина. На этом сокете открыта ТОЛЬКО страница покупателя и публичный API (как у APP_LISTENER=customer, документ 04, §3.5;
В-5): входа сотрудников, каталога и управления через интернет нет вовсе — ответ 404, как будто их не существует.

Почему отдельный сокет, а не проверка заголовков: туннель приходит в программу с адреса 127.0.0.1, то есть «изнутри» компьютера.
Отличить его от владельца за этим компьютером по заголовкам нельзя надёжно (их пишет сам туннель, набор у разных туннелей разный).
Номер порта сокета (scope["server"]) клиент подделать не может — по нему и решаем.

Адрес покупателя для ограничения частоты: все запросы туннеля приходят с 127.0.0.1, поэтому без настоящего адреса «12 снимков
в минуту с одного телефона» стало бы «12 в минуту на весь магазин». Настоящий адрес берётся из заголовка туннеля — и ТОЛЬКО на
этом сокете (на обычном входе такой заголовок мог бы подделать кто угодно из сети магазина). Заголовка нет — адрес неизвестен:
действует только общий предел всего входа.

Чистый модуль (без FastAPI): tests/test_public_access.py.
"""
from __future__ import annotations

import ipaddress
import json
from collections.abc import Iterable, Mapping

DEFAULT_CUSTOMER_PORT = 8100
# Заголовки с адресом покупателя, по порядку доверия: Cloudflare пишет свой, Tailscale и обычные прокси — X-Forwarded-For.
CLIENT_IP_HEADERS = ("cf-connecting-ip", "x-forwarded-for", "x-real-ip")

# Что открыто на входе покупателя: страница покупателя и то, что она подключает (тот же список, что у APP_LISTENER=customer).
CUSTOMER_FILES = frozenset({
    "shop.html", "shop.js", "shop.css", "tokens.css", "ui.css", "theme.js", "theme-apply.js", "i18n.js", "i18n.ru.js", "i18n.uz.js",
    "i18n.uz-cyrl.js", "i18n.en.js", "icons.js", "ticket.js", "boot.js", "icons/favicon-32.png", "icons/apple-touch-icon.png",
})
PUBLIC_API_PREFIX = "/api/v1/public/"
SHOP_PAGE = "/ui/shop.html"


def customer_path_allowed(path: str) -> bool:
    """Можно ли открыть этот путь на входе покупателя. Всё остальное — 404."""
    path = (path or "").replace("\\", "/")
    if path in ("/", "/health", "/favicon.ico"):
        return True
    if path.startswith(PUBLIC_API_PREFIX):
        return True
    if path.startswith("/ui/"):
        name = path[len("/ui/"):]
        return name in CUSTOMER_FILES
    return False


def _valid_ip(value: str) -> str | None:
    value = value.strip().strip('"')
    if value.startswith("[") and "]" in value:            # [2001:db8::1]:443
        value = value[1:value.index("]")]
    elif value.count(":") == 1:                            # 203.0.113.5:51234
        value = value.split(":", 1)[0]
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def client_ip_from_headers(headers: Mapping[str, str] | Iterable[tuple[str, str]]) -> str | None:
    """Настоящий адрес покупателя из заголовков туннеля; None — заголовка нет или он испорчен.
    X-Forwarded-For: первый адрес — клиент (туннель на этом компьютере — единственный прокси перед программой)."""
    items = headers.items() if isinstance(headers, Mapping) else headers
    lower = {}
    for k, v in items:
        k = (k.decode("latin-1") if isinstance(k, bytes) else str(k)).lower()
        v = v.decode("latin-1") if isinstance(v, bytes) else str(v)
        lower.setdefault(k, v)
    for name in CLIENT_IP_HEADERS:
        raw = lower.get(name)
        if raw:
            ip = _valid_ip(raw.split(",")[0])
            if ip:
                return ip
    return None


def is_customer_socket(scope: Mapping, customer_port: int) -> bool:
    """Запрос пришёл на сокет входа покупателя (по номеру порта сокета сервера — его клиент подделать не может)."""
    if not customer_port:
        return False
    server = scope.get("server") or (None, None)
    try:
        return int(server[1]) == int(customer_port)
    except (TypeError, ValueError, IndexError):
        return False


class CustomerGateMiddleware:
    """ASGI: на сокете покупателя пропускает только customer_path_allowed; «/» ведёт на страницу покупателя; адрес покупателя
    из заголовка туннеля кладётся в scope["state"]["customer_ip"] (routes_public берёт его для ограничения частоты)."""

    def __init__(self, app, port_fn):
        self.app = app
        self.port_fn = port_fn

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in ("http", "websocket") or not is_customer_socket(scope, self.port_fn()):
            await self.app(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        state["customer_entrance"] = True
        state["customer_ip"] = client_ip_from_headers(scope.get("headers") or [])
        path = scope.get("path") or ""
        if scope["type"] != "http" or not customer_path_allowed(path):
            await _plain(send, 404, {"detail": "Не найдено", "code": "not_found"})
            return
        if path == "/":
            await send({"type": "http.response.start", "status": 307,
                        "headers": [(b"location", SHOP_PAGE.encode()), (b"content-length", b"0")]})
            await send({"type": "http.response.body", "body": b""})
            return
        await self.app(scope, receive, send)


async def _plain(send, status: int, body: dict) -> None:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json; charset=utf-8"), (b"content-length", str(len(data)).encode())]})
    await send({"type": "http.response.body", "body": data})
