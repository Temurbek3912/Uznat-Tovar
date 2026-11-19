"""Предел размера тела запроса до разбора (app/core/body_limit.py).

Проверяется чистое ASGI-звено и его работа с НАСТОЯЩИМ разбором multipart Starlette (без FastAPI и сервера:
в облачной среде FastAPI и httpx не установлены, поэтому приложение вызывается напрямую по протоколу ASGI).
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.core.body_limit import (DEFAULT_LIMIT, MB, BodyLimitRule, BodySizeLimitMiddleware, BodyTooLarge,
                                 build_rules)

ROOT = Path(__file__).resolve().parents[1]


def _scope(path="/x", headers=(), method="POST"):
    return {"type": "http", "method": method, "path": path, "headers": list(headers), "query_string": b""}


def _call(app, scope, chunks):
    """Вызывает ASGI-приложение; chunks — части тела. Возвращает (отправленные сообщения, сколько частей прочитано)."""
    sent, pending = [], list(chunks)
    reads = {"n": 0}

    async def receive():
        if pending:
            reads["n"] += 1
            body = pending.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(pending)}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent, reads["n"]


def _status(sent):
    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert len(starts) == 1, sent
    return starts[0]["status"]


def _json(sent):
    return json.loads(b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body"))


async def _echo_len(scope, receive, send):
    """Читает всё тело и отвечает его длиной."""
    total = 0
    while True:
        message = await receive()
        total += len(message.get("body", b""))
        if not message.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": str(total).encode()})


def test_declared_length_over_the_limit_is_rejected_without_reading_the_body():
    called = []

    async def app(scope, receive, send):
        called.append(True)

    mw = BodySizeLimitMiddleware(app, default_limit=100)
    sent, reads = _call(mw, _scope(headers=[(b"content-length", b"101")]), [b"x" * 101])
    assert _status(sent) == 413 and _json(sent)["code"] == "request_too_large"
    assert called == [] and reads == 0


@pytest.mark.parametrize("value", [b"abc", b"-1", b"1.5", b""])
def test_invalid_content_length_is_a_client_error(value):
    mw = BodySizeLimitMiddleware(_echo_len, default_limit=100)
    sent, reads = _call(mw, _scope(headers=[(b"content-length", value)]), [b"x"])
    assert _status(sent) == 400 and _json(sent)["code"] == "invalid_content_length" and reads == 0


def test_two_different_content_length_headers_are_rejected():
    mw = BodySizeLimitMiddleware(_echo_len, default_limit=100)
    sent, _ = _call(mw, _scope(headers=[(b"content-length", b"5"), (b"content-length", b"6")]), [b"x" * 5])
    assert _status(sent) == 400


def test_body_within_the_limit_passes_through_unchanged():
    mw = BodySizeLimitMiddleware(_echo_len, default_limit=100)
    sent, _ = _call(mw, _scope(headers=[(b"content-length", b"100")]), [b"a" * 60, b"b" * 40])
    assert _status(sent) == 200 and sent[1]["body"] == b"100"


def test_chunked_body_is_cut_off_as_soon_as_it_exceeds_the_limit():
    seen = {"bytes": 0, "error": None}

    async def app(scope, receive, send):
        try:
            while True:
                message = await receive()
                seen["bytes"] += len(message.get("body", b""))
                if not message.get("more_body"):
                    break
        except BodyTooLarge as exc:
            seen["error"] = exc
            raise
        await send({"type": "http.response.start", "status": 200, "headers": []})

    mw = BodySizeLimitMiddleware(app, default_limit=100)
    sent, reads = _call(mw, _scope(), [b"x" * 60, b"x" * 60, b"x" * 60, b"x" * 60])
    assert _status(sent) == 413
    assert reads == 2                      # третья и четвёртая части не читались
    assert isinstance(seen["error"], BodyTooLarge) and seen["bytes"] == 60


def test_understated_content_length_does_not_bypass_the_counter():
    mw = BodySizeLimitMiddleware(_echo_len, default_limit=100)
    sent, _ = _call(mw, _scope(headers=[(b"content-length", b"10")]), [b"x" * 80, b"x" * 80])
    assert _status(sent) == 413


def test_framework_error_response_after_overflow_is_replaced_by_a_single_413():
    """FastAPI превращает ошибку чтения тела в 400 «There was an error parsing the body» — клиент должен увидеть 413."""

    async def app(scope, receive, send):
        try:
            while True:
                message = await receive()
                if not message.get("more_body"):
                    break
        except BodyTooLarge:
            await send({"type": "http.response.start", "status": 400, "headers": []})
            await send({"type": "http.response.body", "body": b'{"detail":"There was an error parsing the body"}'})
            return

    mw = BodySizeLimitMiddleware(app, default_limit=10)
    sent, _ = _call(mw, _scope(), [b"x" * 8, b"x" * 8])
    assert _status(sent) == 413 and _json(sent)["code"] == "request_too_large"
    assert b"error parsing" not in b"".join(m.get("body", b"") for m in sent)


def test_unrelated_application_errors_are_not_masked():
    async def app(scope, receive, send):
        raise RuntimeError("boom")

    mw = BodySizeLimitMiddleware(app, default_limit=100)
    with pytest.raises(RuntimeError):
        _call(mw, _scope(), [b"x"])


def test_non_http_scopes_pass_through():
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    asyncio.run(BodySizeLimitMiddleware(app, default_limit=0)({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


def test_rules_pick_the_limit_by_full_path_match():
    mw = BodySizeLimitMiddleware(_echo_len, default_limit=10, rules=[BodyLimitRule(r"/up/[^/]+", 1000)])
    assert mw.limit_for("/up/abc") == 1000
    assert mw.limit_for("/up/abc/more") == 10 and mw.limit_for("/x/up/abc") == 10


def test_route_limits_are_not_smaller_than_the_checks_inside_the_routes():
    """Маршрут сам проверяет файл (свой понятный 413): звено не должно отрезать допустимый по маршруту запрос."""
    rules = build_rules(identify_max_mb=10, max_upload_mb=25, max_photos_per_request=8)
    mw = BodySizeLimitMiddleware(_echo_len, default_limit=DEFAULT_LIMIT, rules=rules)
    assert mw.limit_for("/api/v1/catalog/identify") > 10 * MB
    assert mw.limit_for("/api/v1/catalog/products/upload") > 25 * 8 * MB
    assert mw.limit_for("/api/v1/catalog/products/0f1e/photos") > 25 * 8 * MB
    assert mw.limit_for("/api/v1/auth/login") == DEFAULT_LIMIT
    assert mw.limit_for("/api/v1/catalog/products/0f1e") == DEFAULT_LIMIT      # PATCH товара — JSON


# --- Настоящий разбор multipart Starlette ---------------------------------------------------------------------------

def _multipart(file_bytes: int, boundary=b"BOUNDARY"):
    head = (b"--" + boundary + b"\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.jpg\"\r\n"
            b"Content-Type: image/jpeg\r\n\r\n")
    tail = b"\r\n--" + boundary + b"--\r\n"
    return head + b"\xff" * file_bytes + tail, [(b"content-type", b"multipart/form-data; boundary=" + boundary)]


def _starlette_form_app(record):
    async def upload(request: Request):
        form = await request.form()
        data = await form["file"].read()
        await form.close()                              # временный файл формы закрываем сами (без ResourceWarning)
        record["file_bytes"] = len(data)
        return JSONResponse({"ok": True})

    return Starlette(routes=[Route("/upload", upload, methods=["POST"])])


def _chunks(body: bytes, size=64 * 1024):
    return [body[i:i + size] for i in range(0, len(body), size)]


def test_control_without_the_middleware_starlette_parses_the_whole_oversized_body():
    """Контроль: без звена разбор принимает весь запрос (здесь 3 МБ), и проверить размер можно только после этого."""
    record = {}
    body, headers = _multipart(3 * MB)
    sent, reads = _call(_starlette_form_app(record), _scope("/upload", headers), _chunks(body))
    assert _status(sent) == 200 and record["file_bytes"] == 3 * MB and reads == len(_chunks(body))


def test_real_multipart_over_the_limit_is_stopped_before_the_route_runs():
    record = {}
    body, headers = _multipart(3 * MB)
    mw = BodySizeLimitMiddleware(_starlette_form_app(record), default_limit=1 * MB)
    sent, reads = _call(mw, _scope("/upload", headers), _chunks(body))
    assert _status(sent) == 413
    assert "file_bytes" not in record                         # маршрут не выполнялся
    assert reads <= (1 * MB) // (64 * 1024) + 1               # прочитано не больше предела (+ одна часть)


def test_real_multipart_with_declared_length_over_the_limit_is_not_read_at_all():
    record = {}
    body, headers = _multipart(3 * MB)
    headers = headers + [(b"content-length", str(len(body)).encode())]
    mw = BodySizeLimitMiddleware(_starlette_form_app(record), default_limit=1 * MB)
    sent, reads = _call(mw, _scope("/upload", headers), _chunks(body))
    assert _status(sent) == 413 and reads == 0 and "file_bytes" not in record


def test_real_multipart_within_the_limit_still_works():
    record = {}
    body, headers = _multipart(2 * MB)
    mw = BodySizeLimitMiddleware(_starlette_form_app(record), default_limit=3 * MB)
    sent, _ = _call(mw, _scope("/upload", headers), _chunks(body))
    assert _status(sent) == 200 and record["file_bytes"] == 2 * MB


# --- Подключение в приложении ---------------------------------------------------------------------------------------

def _main_calls():
    tree = ast.parse((ROOT / "app" / "main.py").read_text(encoding="utf-8"))
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "add_middleware"]


def _first_arg(call):
    return call.args[0].id if call.args and isinstance(call.args[0], ast.Name) else None


def test_main_installs_the_limit_inside_cors_and_security_headers():
    """Starlette: добавленное позже звено — внешнее. Предел добавлен раньше CORS и заголовков → его 413 получает их заголовки."""
    order = [_first_arg(c) for c in sorted(_main_calls(), key=lambda c: c.lineno)]
    assert order.index("BodySizeLimitMiddleware") < order.index("CORSMiddleware") < order.index("SecurityHeadersMiddleware")
    call = next(c for c in _main_calls() if _first_arg(c) == "BodySizeLimitMiddleware")
    src = ast.unparse(call)
    assert "build_rules" in src and "IDENTIFY_MAX_MB" in src and "max_upload_mb" in src and "MAX_PHOTOS_PER_REQUEST" in src
