"""
Вход покупателя из интернета (docs/PUBLIC_ACCESS.md): app/core/customer_gate.py, scripts/public_access.py, scripts/serve.py,
start_demo --public, ограничение частоты по настоящему адресу покупателя за туннелем.

Настоящий сервер: scripts.serve запускается подпроцессом с настоящим приложением (FastAPI, uvicorn) на двух портах — проверяется,
что на входе покупателя открыта только страница покупателя, а служебное отвечает 404. Не проверяются настоящие Tailscale и
Cloudflare (их нет на компьютере разработки) — их команды и разбор ответа проверены на подставных.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from app.core import customer_gate as cg
from app.core.throttle import LoginThrottle
from scripts import public_access as pa
from scripts.start_demo import Options, run
from tests.test_start_demo import Env

ROOT = Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------------- что открыто на входе покупателя

@pytest.mark.parametrize("path", ["/", "/health", "/ui/shop.html", "/ui/shop.js", "/ui/i18n.uz-cyrl.js",
                                  "/api/v1/public/identify", "/api/v1/public/store", "/ui/icons/favicon-32.png"])
def test_customer_entrance_opens_the_shop_page_and_public_api(path):
    assert cg.customer_path_allowed(path)


@pytest.mark.parametrize("path", ["/ui/admin.html", "/ui/index.html", "/ui/products.html", "/ui/identify.html", "/ui/",
                                  "/api/v1/auth/login", "/api/v1/catalog/products", "/api/v1/admin/users", "/docs",
                                  "/openapi.json", "/health/ready", "/ui/../app/main.py", "/api/v1/publicx", "/ui\\admin.html"])
def test_customer_entrance_hides_everything_else(path):
    assert not cg.customer_path_allowed(path)


# ----------------------------------------------------------------------- адрес покупателя за туннелем

def test_client_ip_comes_from_the_tunnel_header():
    assert cg.client_ip_from_headers({"CF-Connecting-IP": "203.0.113.7", "X-Forwarded-For": "10.0.0.1"}) == "203.0.113.7"
    assert cg.client_ip_from_headers({"x-forwarded-for": "198.51.100.4, 100.64.0.1"}) == "198.51.100.4"
    assert cg.client_ip_from_headers([(b"x-forwarded-for", b"198.51.100.4:5555")]) == "198.51.100.4"
    assert cg.client_ip_from_headers({"x-forwarded-for": "[2001:db8::5]:443"}) == "2001:db8::5"
    assert cg.client_ip_from_headers({"x-real-ip": "2001:db8::9"}) == "2001:db8::9"


def test_missing_or_forged_garbage_header_gives_no_address():
    assert cg.client_ip_from_headers({}) is None
    assert cg.client_ip_from_headers({"x-forwarded-for": "unknown"}) is None
    assert cg.client_ip_from_headers({"x-forwarded-for": "<script>"}) is None


def test_customer_socket_is_recognised_by_the_server_port_only():
    assert cg.is_customer_socket({"server": ("127.0.0.1", 8100)}, 8100)
    assert not cg.is_customer_socket({"server": ("0.0.0.0", 8000)}, 8100)
    assert not cg.is_customer_socket({"server": None}, 8100)
    assert not cg.is_customer_socket({"server": ("127.0.0.1", 8100)}, 0)


def test_throttle_without_a_device_address_counts_only_the_total():
    t = LoginThrottle(max_failures_per_user=5, max_failures_per_ip=2, window_seconds=60, clock=lambda: 100.0)
    for _ in range(4):                                   # за туннелем без заголовка: «все — один адрес» не наступает
        assert t.check("*", None, skip_ip=True) == 0
        t.record_failure("*", None, skip_ip=True)
    t.record_failure("*", None, skip_ip=True)
    assert t.check("*", None, skip_ip=True) > 0          # общий предел входа по-прежнему действует


def test_public_route_takes_the_device_address_from_the_gate():
    from app.api import routes_public as rp
    req = NS(scope={"state": {"customer_entrance": True, "customer_ip": "203.0.113.9"}}, client=NS(host="127.0.0.1"))
    assert rp._client_ip(req) == "203.0.113.9"
    req = NS(scope={"state": {"customer_entrance": True, "customer_ip": None}}, client=NS(host="127.0.0.1"))
    assert rp._client_ip(req) is None                    # не 127.0.0.1: иначе 12 снимков в минуту на весь магазин
    req = NS(scope={}, client=NS(host="192.168.1.20"))
    assert rp._client_ip(req) == "192.168.1.20"          # сеть магазина — как раньше, заголовкам не верим


# ----------------------------------------------------------------------- что запускать: Tailscale, Cloudflare, свой прокси

def _status(dns="shop-pc.tail1234.ts.net.", state="Running"):
    return lambda argv, **kw: NS(returncode=0, stdout=json.dumps({"BackendState": state, "Self": {"DNSName": dns}}))


def test_tailscale_runs_funnel_to_the_customer_port_and_finds_its_own_address():
    p = pa.plan(NS(public_access="tailscale"), customer_port=8100, which=lambda n: "C:/ts/tailscale.exe", run_cmd=_status())
    assert p.active and p.base_url == "https://shop-pc.tail1234.ts.net/"
    assert p.shop_url == "https://shop-pc.tail1234.ts.net/ui/shop.html"
    assert p.argv == ["C:/ts/tailscale.exe", "funnel", "8100"]           # без --bg: выключается вместе с программой


def test_tailscale_problems_are_explained():
    none = pa.plan(NS(public_access="tailscale"), customer_port=8100, which=lambda n: None, exists=lambda p: False)
    assert not none.active and "не установлен" in none.problems[0]
    logged_out = pa.plan(NS(public_access="tailscale"), customer_port=8100, which=lambda n: "ts", run_cmd=_status(state="NeedsLogin"))
    assert not logged_out.active and "войдите" in logged_out.problems[0]
    silent = pa.plan(NS(public_access="tailscale"), customer_port=8100, which=lambda n: "ts",
                     run_cmd=lambda argv, **kw: NS(returncode=1, stdout=""))
    assert not silent.active and "не отвечает" in silent.problems[0]
    no_name = pa.plan(NS(public_access="tailscale"), customer_port=8100, which=lambda n: "ts", run_cmd=_status(dns=""))
    assert not no_name.active and "MagicDNS" in no_name.problems[0]


def test_cloudflare_needs_a_token_and_a_public_https_address_and_keeps_the_token_off_the_command_line():
    s = NS(public_access="cloudflare", public_base_url="https://magazin1.example.com", cloudflare_tunnel_token="SECRET-T")
    p = pa.plan(s, customer_port=8100, which=lambda n: "cloudflared")
    assert p.active and p.shop_url == "https://magazin1.example.com/ui/shop.html"
    assert p.argv == ["cloudflared", "tunnel", "--no-autoupdate", "run"] and "SECRET-T" not in " ".join(p.argv)
    assert p.env == {"TUNNEL_TOKEN": "SECRET-T"}
    assert "CLOUDFLARE_TUNNEL_TOKEN" in pa.plan(NS(**{**vars(s), "cloudflare_tunnel_token": ""}), customer_port=8100,
                                                which=lambda n: "cloudflared").problems[0]
    http_url = pa.plan(NS(**{**vars(s), "public_base_url": "http://magazin1.example.com"}), customer_port=8100, which=lambda n: "x")
    assert not http_url.active and "https" in http_url.problems[0]
    ip_url = pa.plan(NS(**{**vars(s), "public_base_url": "https://203.0.113.5"}), customer_port=8100, which=lambda n: "x")
    assert not ip_url.active and "IP" in ip_url.problems[0]


def test_manual_mode_starts_nothing_and_only_puts_the_address_in_the_qr():
    p = pa.plan(NS(public_access="manual", public_base_url="https://shop.example.uz/"), customer_port=8100)
    assert p.active and p.argv is None and p.shop_url == "https://shop.example.uz/ui/shop.html"
    assert not pa.plan(NS(public_access="manual", public_base_url=""), customer_port=8100).active


def test_off_and_unknown_modes():
    assert pa.plan(NS(), customer_port=8100).mode == "off" and not pa.plan(NS(), customer_port=8100).active
    assert "неизвестное" in pa.plan(NS(public_access="ngrok"), customer_port=8100).problems[0]
    assert pa.plan(NS(public_access="tailscale"), customer_port=8100, mode="off").mode == "off"    # --public off сильнее .env


# ----------------------------------------------------------------------- start_demo --public

class PublicEnv(Env):
    def _popen(self, argv, **kw):
        name = "tunnel" if "funnel" in argv or "tunnel" in argv else ("api" if "scripts.serve" in argv or "uvicorn" in argv
                                                                     else ("lite" if "lite" in str(argv) else "worker"))
        from tests.test_start_demo import FakeProc
        proc = FakeProc(name, **self.exit_plan.get(name, {}))
        self.started.append((name, argv, kw))
        self.procs[name] = proc
        return proc

    def _run(self, argv, **kw):
        self.docker_calls.append((argv, kw))
        if "status" in argv:
            return NS(returncode=0, stdout=json.dumps({"BackendState": "Running", "Self": {"DNSName": "shop-pc.tail1.ts.net."}}))
        return NS(returncode=0, stdout="")


SETTINGS = NS(database_url="postgresql://u:p@dbhost:5432/x", queue_broker_url="redis://redishost:6379/0",
              public_access="tailscale", customer_port=8100)


def _go(tmp_path, opts=None, **over):
    env = PublicEnv(tmp_path, settings=over.pop("settings", SETTINGS), which=lambda n: "/usr/bin/" + n, **over)
    return run(opts or Options(no_browser=True), env.deps), env


def test_full_mode_with_tailscale_serves_a_customer_port_runs_the_tunnel_and_puts_the_internet_address_in_the_qr(tmp_path):
    code, env = _go(tmp_path)
    assert code == 0
    names = [n for n, _, _ in env.started]
    assert names == ["api", "worker", "tunnel"]
    api_argv = env.started[0][1]
    assert api_argv[:3] == ["PY", "-m", "scripts.serve"] and api_argv[api_argv.index("--customer-port") + 1] == "8100"
    assert env.started[2][1] == ["/usr/bin/tailscale", "funnel", "8100"]
    assert env.started[0][2]["env"]["CUSTOMER_PORT"] == "8100"
    assert "[QR https://shop-pc.tail1.ts.net/ui/shop.html]" in env.text
    assert "[QR http://192.168.1.57" not in env.text                     # в QR — только адрес из интернета
    assert "Сотрудники (в сети магазина): http://192.168.1.57:8000/ui/index.html" in env.text


def test_lite_mode_with_tailscale_uses_the_same_single_process(tmp_path):
    code, env = _go(tmp_path, Options(no_browser=True, lite=True))
    assert code == 0
    assert [n for n, _, _ in env.started] == ["api", "tunnel"]             # один процесс программы + туннель
    assert "scripts.serve" in env.started[0][1]


def test_tunnel_failure_keeps_the_shop_network_working(tmp_path):
    code, env = _go(tmp_path, exit_plan={"tunnel": {"exits_after": 0, "code": 1}}, stop_after=8)
    assert code == 0                                                        # остановил Ctrl+C, а не сбой туннеля
    assert "Вход покупателя из интернета остановился" in env.text and env.text.count("остановился (код 1)") == 1


def test_bad_public_settings_do_not_block_the_start(tmp_path):
    settings = NS(**{**vars(SETTINGS), "public_access": "cloudflare", "public_base_url": ""})
    code, env = _go(tmp_path, settings=settings)
    assert code == 0 and [n for n, _, _ in env.started] == ["api", "worker"]
    assert "НЕ включён" in env.text and "PUBLIC_ACCESS.md" in env.text
    assert env.started[0][1][:3] == ["PY", "-m", "uvicorn"]                # вход покупателя не открывается


def test_busy_customer_port_or_same_port_turns_internet_access_off(tmp_path):
    code, env = _go(tmp_path, port_busy=lambda p: p == 8100)
    assert code == 0 and "8100" in env.text and "уже занят" in env.text and "tunnel" not in [n for n, _, _ in env.started]
    code, env = _go(tmp_path, Options(no_browser=True, port=8100))
    assert code == 0 and "совпадает" in env.text


def test_public_off_flag_overrides_the_settings(tmp_path):
    code, env = _go(tmp_path, Options(no_browser=True, public="off"))
    assert code == 0 and [n for n, _, _ in env.started] == ["api", "worker"] and "[QR http://192.168.1.57:8000/ui/shop.html]" in env.text


def test_tailscale_asks_to_enable_funnel_the_link_is_shown(tmp_path):
    (tmp_path / "logs").mkdir()
    real_popen = PublicEnv._popen

    def popen(self, argv, **kw):
        proc = real_popen(self, argv, **kw)
        if "funnel" in argv:
            kw["stdout"].write(b"Funnel is not enabled on your tailnet.\nTo enable, visit:\n\n https://login.tailscale.com/f/funnel?node=abc\n")
            kw["stdout"].flush()
        return proc

    env = PublicEnv(tmp_path, settings=SETTINGS, which=lambda n: "/usr/bin/" + n)
    env.deps.popen = popen.__get__(env)
    assert run(Options(no_browser=True), env.deps) == 0
    assert "Туннель просит действие: https://login.tailscale.com/f/funnel?node=abc" in env.text


# ----------------------------------------------------------------------- настоящий сервер на двух входах

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(port, path, method="GET", headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        r = conn.getresponse()
        return r.status, dict(r.getheaders()), r.read()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def two_entrances(tmp_path_factory):
    pytest.importorskip("fastapi")
    tmp = tmp_path_factory.mktemp("serve")
    main_port, customer_port = _free_port(), _free_port()
    env = {**os.environ, "DATABASE_URL": "sqlite:///" + (tmp / "t.db").as_posix(), "QUEUE_BROKER_URL": "memory://",
           "FRAMES_REDIS_URL": "", "AUTH_THROTTLE_BACKEND": "memory", "LITE_MODE": "0", "PYTHONIOENCODING": "utf-8"}
    log = open(tmp / "serve.log", "wb")
    proc = subprocess.Popen([sys.executable, "-m", "scripts.serve", "--host", "127.0.0.1", "--port", str(main_port),
                             "--customer-port", str(customer_port)], cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                if _get(main_port, "/health")[0] == 200 and _get(customer_port, "/health")[0] == 200:
                    break
            except OSError:
                pass
            if proc.poll() is not None:
                break
            time.sleep(0.3)
        else:
            pytest.fail("scripts.serve не запустился")
        if proc.poll() is not None:
            log.close()
            pytest.fail("scripts.serve упал:\n" + (tmp / "serve.log").read_text(encoding="utf-8", errors="replace")[-3000:])
        yield main_port, customer_port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def test_real_customer_entrance_shows_only_the_shop_page(two_entrances):
    main_port, customer_port = two_entrances
    assert _get(customer_port, "/ui/shop.html")[0] == 200
    status, headers, _ = _get(customer_port, "/")
    assert status == 307 and headers.get("location") == "/ui/shop.html"
    for path in ("/ui/admin.html", "/ui/index.html", "/ui/products.html", "/docs", "/openapi.json", "/health/ready"):
        assert _get(customer_port, path)[0] == 404, path
    login = _get(customer_port, "/api/v1/auth/login", "POST", {"Content-Type": "application/json"},
                 json.dumps({"username": "admin", "password": "x"}))
    assert login[0] == 404                                                # входа сотрудников через интернет нет вовсе
    assert _get(customer_port, "/api/v1/catalog/products")[0] == 404


def test_real_main_entrance_is_unchanged(two_entrances):
    main_port, _ = two_entrances
    assert _get(main_port, "/ui/admin.html")[0] == 200
    assert _get(main_port, "/ui/index.html")[0] == 200
    assert _get(main_port, "/api/v1/auth/login", "POST", {"Content-Type": "application/json"},
                json.dumps({"username": "nobody", "password": "x"}))[0] != 404


def test_customer_port_listens_on_this_computer_only(monkeypatch):
    pytest.importorskip("uvicorn")
    pytest.importorskip("fastapi")
    monkeypatch.setenv("CUSTOMER_PORT", "8100")               # _servers меняет переменную: вернуть после теста
    from scripts import serve
    main, customer = serve._servers(18000, 18100)
    assert customer.config.host == "127.0.0.1" and customer.config.lifespan == "off" and customer.config.proxy_headers is False
    assert main.config.host == "0.0.0.0" and main.config.port == 18000
