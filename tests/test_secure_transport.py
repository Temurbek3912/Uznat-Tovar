"""
Пароли, коды и токены — только по HTTPS или через VPN/административный вход (решение владельца 2026-11-08 21:53; документ 18, §14).

Проверяется на НАСТОЯЩЕМ uvicorn через сокет — по HTTP и по HTTPS (TLS самого uvicorn с сертификатом от scripts/make_local_tls.py):
  * /api/v1/* по HTTP на входе сотрудников — 403 insecure_transport; по HTTPS — проходит;
  * адрес «можно ли отправлять пароль» и проверки состояния не отклоняются;
  * режим «all»: с самого компьютера (127.0.0.1) по HTTP — можно, из сети по HTTP — нельзя;
  * центр сертификатов магазина ограничен именами: сертификат для чужого адреса, подписанный им, клиент отвергает.
Не проверяется: Docker (docker-compose.https.yml), браузеры телефонов (установка центра), FastAPI.
"""
from __future__ import annotations

import http.client
import os
import ipaddress
import json
import socket
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

uvicorn = pytest.importorskip("uvicorn")
pytest.importorskip("cryptography")
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

from app.core import secure_transport  # noqa: E402
from scripts import make_local_tls  # noqa: E402


def _settings(**over):
    base = dict(app_listener="public", vpn_bind_ip="", admin_allowed_hosts="", auth_require_secure_transport=True,
                public_https_url="https://192.168.1.10:8000")
    base.update(over)
    return SimpleNamespace(**base)


# ------------------------------------------------------------------ чистая логика


def test_secure_means_https_or_the_admin_listener():
    assert secure_transport.is_secure(scheme="https", request_listener="public")
    assert secure_transport.is_secure(scheme="http", request_listener="admin")      # 127.0.0.1 или VPN
    assert not secure_transport.is_secure(scheme="http", request_listener="public")  # Wi-Fi магазина без TLS


def test_only_the_api_is_guarded_and_the_transport_question_is_exempt():
    assert secure_transport.needs_check("/api/v1/auth/login") and secure_transport.needs_check("/api/v1/catalog/products")
    assert not secure_transport.needs_check("/api/v1/auth/transport")
    assert not secure_transport.needs_check("/health/ready") and not secure_transport.needs_check("/ui/index.html")
    assert secure_transport.describe(secure=False, required=True, https_url="") == {"secure": False, "required": True, "https_url": None}


# ------------------------------------------------------------------ сертификаты магазина


def test_store_ca_is_name_constrained_and_server_certificate_is_short_lived(tmp_path):
    from cryptography import x509
    info = make_local_tls.make(tmp_path / "tls", ["192.168.1.10"], ["Shop-PC"])
    ca = x509.load_pem_x509_certificate((tmp_path / "tls" / "ca.crt").read_bytes())
    nc = ca.extensions.get_extension_for_class(x509.NameConstraints)
    assert nc.critical
    permitted = {str(g.value) for g in nc.value.permitted_subtrees}
    assert {"10.0.0.0/8", "192.168.0.0/16", "172.16.0.0/12", "100.64.0.0/10", "shop-pc", "localhost"} <= permitted
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.path_length == 0
    server = x509.load_pem_x509_certificates((tmp_path / "tls" / "server.crt").read_bytes())[0]
    san = server.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert ipaddress.ip_address("192.168.1.10") in san.get_values_for_type(x509.IPAddress)
    assert set(san.get_values_for_type(x509.DNSName)) == {"shop-pc", "localhost"}
    assert (server.not_valid_after_utc - server.not_valid_before_utc) < timedelta(days=398)
    assert info["names"] == ["shop-pc", "localhost"]
    for name in ("ca.key", "server.key"):
        key = tmp_path / "tls" / name
        if os.name == "nt":                     # Windows: права — списком доступа; наследования нет, доступ — одной учётной записи
            import subprocess
            acl = subprocess.run(["icacls", str(key)], capture_output=True, text=True, check=True).stdout
            grants = [line for line in acl.splitlines()[0:-2] if ":(" in line]
            assert len(grants) == 1 and "(I)" not in acl and "(F)" in grants[0], acl
        else:
            assert key.stat().st_mode & 0o077 == 0, name          # только владельцу файла


def test_public_addresses_are_refused_and_renew_keeps_the_ca(tmp_path):
    with pytest.raises(ValueError):
        make_local_tls.make(tmp_path / "a", ["8.8.8.8"], [])
    with pytest.raises(ValueError):
        make_local_tls.make(tmp_path / "b", [], ["shop-pc"])
    make_local_tls.make(tmp_path / "c", ["192.168.1.10"], [])
    ca_before = (tmp_path / "c" / "ca.crt").read_bytes()
    with pytest.raises(ValueError):
        make_local_tls.make(tmp_path / "c", ["192.168.1.10"], [])                    # повторно — только --renew
    make_local_tls.make(tmp_path / "c", ["192.168.1.11"], [], renew=True)
    assert (tmp_path / "c" / "ca.crt").read_bytes() == ca_before                     # телефоны переустанавливать не нужно
    assert make_local_tls.main(["--ip", "8.8.8.8", "--out", str(tmp_path / "d")], out=lambda _l: None) == 1


# ------------------------------------------------------------------ настоящий uvicorn: HTTP и HTTPS


async def ping(request):
    return JSONResponse({"ok": True})


async def transport(request):
    st = request.app.state.settings
    return JSONResponse(secure_transport.describe(secure=secure_transport.secure_for_scope(request.scope, st),
                                                  required=st.auth_require_secure_transport, https_url=st.public_https_url))


def _app(settings):
    app = Starlette(routes=[Route("/api/v1/auth/login", ping, methods=["POST"]), Route("/api/v1/ping", ping),
                            Route("/api/v1/auth/transport", transport), Route("/health/ready", ping)])
    app.state.settings = settings
    app.add_middleware(secure_transport.SecureTransportMiddleware, settings_fn=lambda: app.state.settings)
    return app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _network_ip() -> str | None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        except OSError:
            return None
    return None if ip.startswith("127.") else ip


def _serve(app, host, **ssl_kw):
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="off", **ssl_kw))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(300):
        if srv.started:
            break
        time.sleep(0.02)
    assert srv.started
    return srv, thread, port


def _get(conn, method, path, body=None):
    conn.request(method, path, body=body, headers={"Content-Type": "application/x-www-form-urlencoded"} if body else {})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, json.loads(data or b"{}")


@pytest.fixture(scope="module")
def tls_files(tmp_path_factory):
    out = tmp_path_factory.mktemp("tls")
    make_local_tls.make(out, ["127.0.0.1"], ["localhost"])
    return out


def test_employee_listener_over_plain_http_refuses_the_api_but_answers_the_transport_question():
    app = _app(_settings())
    srv, thread, port = _serve(app, "127.0.0.1")
    try:
        status, body = _get(http.client.HTTPConnection("127.0.0.1", port, timeout=10), "POST", "/api/v1/auth/login",
                            body="username=a&password=secret")
        assert status == 403 and body["code"] == "insecure_transport" and body["params"]["url"] == "https://192.168.1.10:8000"
        assert _get(http.client.HTTPConnection("127.0.0.1", port, timeout=10), "GET", "/api/v1/auth/transport") == (
            200, {"secure": False, "required": True, "https_url": "https://192.168.1.10:8000"})
        assert _get(http.client.HTTPConnection("127.0.0.1", port, timeout=10), "GET", "/health/ready")[0] == 200
        app.state.settings = _settings(auth_require_secure_transport=False)          # настройка выключена — как раньше
        assert _get(http.client.HTTPConnection("127.0.0.1", port, timeout=10), "GET", "/api/v1/ping")[0] == 200
    finally:
        srv.should_exit = True
        thread.join(timeout=5)


def test_https_with_the_store_certificate_is_accepted(tls_files):
    app = _app(_settings())
    srv, thread, port = _serve(app, "127.0.0.1", ssl_certfile=str(tls_files / "server.crt"), ssl_keyfile=str(tls_files / "server.key"))
    try:
        ctx = ssl.create_default_context(cafile=str(tls_files / "ca.crt"))          # телефон с установленным центром магазина
        status, body = _get(http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=10), "POST", "/api/v1/auth/login",
                            body="username=a&password=secret")
        assert status == 200 and body == {"ok": True}
        assert _get(http.client.HTTPSConnection("localhost", port, context=ctx, timeout=10), "GET", "/api/v1/auth/transport")[1]["secure"]
        with pytest.raises(ssl.SSLCertVerificationError):                           # без центра — браузер предупредит
            _get(http.client.HTTPSConnection("127.0.0.1", port, context=ssl.create_default_context(), timeout=10), "GET", "/api/v1/ping")
    finally:
        srv.should_exit = True
        thread.join(timeout=5)


def test_single_process_mode_accepts_http_only_from_this_computer():
    app = _app(_settings(app_listener="all"))
    srv, thread, port = _serve(app, "0.0.0.0")
    try:
        local = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        assert _get(local, "GET", "/api/v1/ping")[0] == 200                         # браузер на самом компьютере
        ip = _network_ip()
        if ip is None:
            pytest.skip("нет сетевого адреса кроме 127.x")
        status, body = _get(http.client.HTTPConnection(ip, port, timeout=10), "GET", "/api/v1/ping")
        assert status == 403 and body["code"] == "insecure_transport"              # телефон в Wi-Fi по HTTP
    finally:
        srv.should_exit = True
        thread.join(timeout=5)


def test_store_ca_cannot_vouch_for_a_foreign_site(tls_files, tmp_path):
    """Даже с украденным ca.key: сертификат для чужого имени, подписанный центром магазина, клиент отвергает (Name Constraints)."""
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    ca_cert = x509.load_pem_x509_certificate((tls_files / "ca.crt").read_bytes())
    ca_key = load_pem_private_key((tls_files / "ca.key").read_bytes(), password=None)
    key, cert = make_local_tls.build_server(ca_key, ca_cert, [ipaddress.ip_address("127.0.0.1")], ["bank.example"],
                                            datetime.now(timezone.utc))
    (tmp_path / "evil.crt").write_bytes(make_local_tls._pem_cert(cert) + make_local_tls._pem_cert(ca_cert))
    (tmp_path / "evil.key").write_bytes(make_local_tls._pem_key(key))
    srv, thread, port = _serve(_app(_settings()), "127.0.0.1", ssl_certfile=str(tmp_path / "evil.crt"), ssl_keyfile=str(tmp_path / "evil.key"))
    try:
        ctx = ssl.create_default_context(cafile=str(tls_files / "ca.crt"))
        ctx.check_hostname = False                                                 # проверяется именно ограничение центра
        with pytest.raises(ssl.SSLCertVerificationError, match="(?i)name constraint|permitted|excluded"):
            _get(http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=10), "GET", "/api/v1/ping")
    finally:
        srv.should_exit = True
        thread.join(timeout=5)
