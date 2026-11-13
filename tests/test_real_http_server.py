"""
Звенья приложения на НАСТОЯЩЕМ HTTP-сервере uvicorn (через сокет, не через вызов ASGI-функции): предел тела запроса,
заголовки безопасности и HSTS за обратным прокси, единые обработчики ошибок.

Собирается тот же стек, что в app/main.py (порядок add_middleware тот же): BodySizeLimitMiddleware внутри
SecurityHeadersMiddleware, обработчики install_error_handlers. Маршруты — свои маленькие на Starlette, потому что FastAPI в облачной
среде нет; uvicorn и Starlette — настоящие (uvicorn 0.54, Starlette 1.7). Проверяется то, что раньше было только допущением:
  * uvicorn с --proxy-headers и доверенным --forwarded-allow-ips передаёт scope["scheme"] = "https" по X-Forwarded-Proto —
    и тогда приложение отдаёт HSTS; от недоверенного адреса тот же заголовок игнорируется (подделать HTTPS нельзя);
  * предел тела срабатывает на настоящем потоковом теле (Transfer-Encoding: chunked) и по Content-Length;
  * неожиданное исключение даёт клиенту 500 без подробностей.
Не проверяется: сам FastAPI, настоящие маршруты приложения, Docker.
"""
from __future__ import annotations

import http.client
import json
import socket
import threading
import time

import pytest

uvicorn = pytest.importorskip("uvicorn")
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

from app.core.body_limit import BodySizeLimitMiddleware  # noqa: E402
from app.core.error_handlers import install_error_handlers  # noqa: E402
from app.core.errors import ApiError  # noqa: E402
from app.core.security_headers import SecurityHeadersMiddleware  # noqa: E402

LIMIT = 64 * 1024


async def echo_size(request):
    body = await request.body()
    return JSONResponse({"size": len(body)})


async def boom(request):
    raise RuntimeError("SELECT password_hash FROM users -- /srv/app/.env")


async def coded(request):
    raise ApiError(404, "product_not_found", "Товар не найден")


def _app():
    app = Starlette(routes=[Route("/echo", echo_size, methods=["POST"]), Route("/boom", boom), Route("/coded", coded)])
    install_error_handlers(app)
    app.add_middleware(BodySizeLimitMiddleware, default_limit=LIMIT)        # как в app/main.py: первым — значит внутри
    app.add_middleware(SecurityHeadersMiddleware)
    return app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module", params=[("127.0.0.1",), ("10.9.9.9",)], ids=["trusted-proxy", "untrusted-proxy"])
def server(request):
    port = _free_port()
    config = uvicorn.Config(_app(), host="127.0.0.1", port=port, log_level="warning", proxy_headers=True,
                            forwarded_allow_ips=request.param[0], lifespan="off")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.02)
    assert srv.started, "uvicorn не запустился"
    yield {"port": port, "trusted": request.param[0] == "127.0.0.1"}
    srv.should_exit = True
    thread.join(timeout=5)


def _request(port, method, path, body=None, headers=None, chunked=False):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        if chunked:
            conn.putrequest(method, path)
            for k, v in (headers or {}).items():
                conn.putheader(k, v)
            conn.putheader("Transfer-Encoding", "chunked")
            conn.endheaders()
            for i in range(0, len(body), 8192):
                part = body[i:i + 8192]
                try:
                    conn.send(f"{len(part):X}\r\n".encode() + part + b"\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    break                                  # сервер уже ответил 413 и закрыл приём — так и должно быть
            else:
                conn.send(b"0\r\n\r\n")
        else:
            conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
    finally:
        conn.close()


def test_hsts_only_when_a_trusted_proxy_says_https(server):
    status, headers, _ = _request(server["port"], "GET", "/coded", headers={"X-Forwarded-Proto": "https"})
    assert status == 404 and headers["x-content-type-options"] == "nosniff"
    if server["trusted"]:
        assert headers.get("strict-transport-security") == "max-age=15552000"
    else:
        assert "strict-transport-security" not in headers          # недоверенный адрес не может «включить» HTTPS заголовком
    _, plain, _ = _request(server["port"], "GET", "/coded")
    assert "strict-transport-security" not in plain


def test_coded_error_body_over_real_http(server):
    status, headers, data = _request(server["port"], "GET", "/coded")
    assert status == 404 and headers["content-type"].startswith("application/json")
    assert json.loads(data) == {"detail": "Товар не найден", "code": "product_not_found", "params": {}}


def test_unexpected_exception_leaks_nothing_over_real_http(server):
    status, headers, data = _request(server["port"], "GET", "/boom")
    body = json.loads(data)
    assert status == 500 and body["code"] == "internal_error"
    for leak in ("SELECT", "password_hash", "/srv/", ".env", "RuntimeError", "Traceback"):
        assert leak not in data.decode("utf-8"), leak
    assert headers["x-frame-options"] == "DENY"                     # даже ответ 500 получает заголовки безопасности


def test_unexpected_error_carries_cors_headers_only_for_an_allowed_origin():
    """Долг §6.10: ответ 500 отдаёт ServerErrorMiddleware снаружи CORS — заголовки CORS ставит сам обработчик (на настоящем uvicorn)."""
    app = Starlette(routes=[Route("/boom", boom)])
    install_error_handlers(app, cors_origins=["http://allowed.example"])
    app.add_middleware(SecurityHeadersMiddleware)
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical", lifespan="off"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.02)
    try:
        status, headers, _ = _request(port, "GET", "/boom", headers={"Origin": "http://allowed.example"})
        assert status == 500 and headers["access-control-allow-origin"] == "http://allowed.example" and headers["vary"] == "Origin"
        _, other, _ = _request(port, "GET", "/boom", headers={"Origin": "http://evil.example"})
        assert "access-control-allow-origin" not in other
        _, plain, _ = _request(port, "GET", "/boom")
        assert "access-control-allow-origin" not in plain and plain["x-frame-options"] == "DENY"
    finally:
        srv.should_exit = True
        thread.join(timeout=5)
        install_error_handlers(Starlette())                       # вернуть пустой список источников для других тестов


def test_body_limit_by_content_length_and_by_streamed_chunks(server):
    ok = _request(server["port"], "POST", "/echo", body=b"x" * 1000, headers={"Content-Type": "application/octet-stream"})
    assert ok[0] == 200 and json.loads(ok[2]) == {"size": 1000}
    big = _request(server["port"], "POST", "/echo", body=b"x" * (LIMIT + 1), headers={"Content-Type": "application/octet-stream"})
    assert big[0] == 413 and json.loads(big[2])["code"] == "request_too_large"
    streamed = _request(server["port"], "POST", "/echo", body=b"y" * (LIMIT * 4), headers={"Content-Type": "application/octet-stream"},
                        chunked=True)
    assert streamed[0] == 413 and json.loads(streamed[2])["code"] == "request_too_large"
    assert streamed[1]["x-content-type-options"] == "nosniff"
    bad_length = _request(server["port"], "POST", "/echo", body=b"", headers={"Content-Length": "abc"})
    assert bad_length[0] in (400, 413, 422)


# ------------------------------------------- какой вход у запроса — на настоящем сокете (аудит 2026-11-08, документ 18, §12)
#
# Права управления в режиме «all» (запуск без Docker, scripts/start_demo.py на 0.0.0.0) даются только запросу, пришедшему на петлевой
# адрес сервера. Здесь проверяется то, на чём это держится: scope["server"] у настоящего uvicorn — адрес сокета, на который
# подключился клиент, и заголовки (Host, X-Forwarded-*, даже от «доверенного» адреса при --proxy-headers) его не меняют.

from app.core.mfa_policy import listener_for_request  # noqa: E402


def _local_network_ip() -> str | None:
    """Адрес этой машины в сети (не 127.x). UDP connect не шлёт пакетов — только выбирает исходящий интерфейс."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        except OSError:
            return None
    return None if ip.startswith("127.") else ip


async def whoami(request):
    return JSONResponse({"server": list(request.scope.get("server") or []),
                         "all": listener_for_request("all", request.scope, request.headers),
                         "admin": listener_for_request("admin", request.scope, request.headers, vpn_bind_ip="100.64.0.5")})


@pytest.fixture(scope="module")
def wide_server():
    port = _free_port()
    app = Starlette(routes=[Route("/whoami", whoami)])
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning", proxy_headers=True,
                            forwarded_allow_ips="*", lifespan="off")          # худший случай: заголовкам прокси доверяют всем
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.02)
    assert srv.started, "uvicorn не запустился"
    yield port
    srv.should_exit = True
    thread.join(timeout=5)


def _whoami(host_ip, port, headers=None):
    conn = http.client.HTTPConnection(host_ip, port, timeout=10)
    try:
        conn.request("GET", "/whoami", headers=headers or {})
        resp = conn.getresponse()
        return json.loads(resp.read())
    finally:
        conn.close()


def test_single_process_on_all_interfaces_gives_admin_only_over_loopback(wide_server):
    local = _whoami("127.0.0.1", wide_server, {"Host": f"localhost:{wide_server}"})
    assert local["server"][0] == "127.0.0.1" and local["all"] == "admin"
    forwarded = _whoami("127.0.0.1", wide_server, {"Host": "localhost", "X-Forwarded-For": "203.0.113.9"})
    assert forwarded["all"] == "public"                                   # через прокси на этом компьютере — не «этот компьютер»
    rebinding = _whoami("127.0.0.1", wide_server, {"Host": "evil.example"})
    assert rebinding["all"] == "public" and rebinding["admin"] == "public"


def test_request_from_the_network_cannot_pretend_to_be_local(wide_server):
    ip = _local_network_ip()
    if ip is None:
        pytest.skip("у машины нет сетевого адреса кроме 127.x — проверить приход из сети нельзя")
    remote = _whoami(ip, wide_server, {"Host": "localhost", "X-Forwarded-Host": "localhost"})
    assert remote["server"][0] == ip                                       # адрес сокета, а не из заголовков
    assert remote["all"] == "public"
    spoofed = _whoami(ip, wide_server, {"Host": "127.0.0.1"})
    assert spoofed["server"][0] == ip and spoofed["all"] == "public"
    assert _whoami(ip, wide_server, {"Host": "100.64.0.5:8001"})["admin"] == "admin"   # административный вход: по имени VPN
