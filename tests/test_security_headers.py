"""Заголовки безопасности (app/core/security_headers.py): чистое ASGI-звено проверяется без FastAPI и сервера."""
from __future__ import annotations

import asyncio
from pathlib import Path

from app.core.security_headers import SECURITY_HEADERS, SecurityHeadersMiddleware

ROOT = Path(__file__).resolve().parents[1]


def _run(app, scope):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(SecurityHeadersMiddleware(app)(scope, receive, send))
    return sent


async def _plain_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/html")]})
    await send({"type": "http.response.body", "body": b"ok"})


def test_every_http_response_gets_all_security_headers():
    start, body = _run(_plain_app, {"type": "http", "path": "/ui/index.html"})
    headers = dict(start["headers"])
    for name, value in SECURITY_HEADERS:
        assert headers[name] == value
    assert headers[b"content-type"] == b"text/html" and body["body"] == b"ok"


def test_headers_already_set_by_the_route_are_not_overridden():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [(b"X-Frame-Options", b"SAMEORIGIN")]})
        await send({"type": "http.response.body", "body": b""})

    start, _ = _run(app, {"type": "http"})
    values = [v for n, v in start["headers"] if n.lower() == b"x-frame-options"]
    assert values == [b"SAMEORIGIN"]


def test_non_http_scopes_pass_through_untouched():
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])
        await send({"type": "lifespan.startup.complete"})

    assert _run(app, {"type": "lifespan"}) == [{"type": "lifespan.startup.complete"}] and seen == ["lifespan"]


def test_csp_allows_only_scripts_from_the_server_itself_and_pages_have_no_inline_scripts():
    """script-src 'self': встроенные <script> и обработчики on…="…" браузер не выполнит — поэтому их нет ни на одной странице."""
    import re

    csp = dict(SECURITY_HEADERS)[b"content-security-policy"].decode()
    directives = {d.strip().split(" ", 1)[0]: d.strip() for d in csp.split(";") if d.strip()}
    assert directives["script-src"] == "script-src 'self'"
    assert "'unsafe-inline'" not in csp and "'unsafe-eval'" not in csp
    for page in (ROOT / "frontend").glob("*.html"):
        html = page.read_text(encoding="utf-8")
        assert "<script>" not in html, page.name
        assert not re.search(r"\son[a-z]+\s*=", html, re.I), page.name


def test_main_app_installs_the_middleware():
    src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "app.add_middleware(SecurityHeadersMiddleware)" in src


def test_hsts_only_over_https_and_never_over_plain_http():
    from app.core.security_headers import HSTS_HEADER

    over_https = dict(_run(_plain_app, {"type": "http", "path": "/", "scheme": "https"})[0]["headers"])
    over_http = dict(_run(_plain_app, {"type": "http", "path": "/", "scheme": "http"})[0]["headers"])
    no_scheme = dict(_run(_plain_app, {"type": "http", "path": "/"})[0]["headers"])
    assert over_https[HSTS_HEADER[0]] == b"max-age=15552000"
    assert HSTS_HEADER[0] not in over_http and HSTS_HEADER[0] not in no_scheme      # адрес http://192.168.x.x продолжает работать
