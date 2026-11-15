"""Страницы в настоящем Chromium: нет горизонтальной прокрутки, ошибок JavaScript и нарушений политики безопасности (CSP) на всех
четырёх языковых вариантах. Статический сервер теста отдаёт те же заголовки безопасности, что и приложение (SECURITY_HEADERS).

Нужен Playwright с Chromium (есть в облачной среде разработки; в Docker-образ приложения не входит — тогда тест пропускается).
Сервер API не запускается: страницы открываются как статика, поэтому проверяется вёрстка экрана входа и общих элементов
(верхняя строка с переключателями языка и темы, меню), а не данные. Это не проверка на Windows и не на реальном телефоне.
"""
from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")

from app.core.security_headers import SECURITY_HEADERS  # noqa: E402

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
PAGES = ("identify.html", "settings.html", "products.html", "preview.html", "index.html", "admin.html", "login.html")
LANGS = ("ru", "uz", "uz-Cyrl", "en")
WIDTHS = (320, 390, 1280)


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def end_headers(self):
        for name, value in SECURITY_HEADERS:
            self.send_header(name.decode(), value.decode())
        super().end_headers()


@pytest.fixture(scope="module")
def base_url():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(FRONTEND)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


@pytest.fixture(scope="module")
def browser():
    with playwright_sync.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001 — нет браузера в этой среде
            pytest.skip(f"Chromium недоступен: {exc}")
        yield b
        b.close()


@pytest.mark.parametrize("page_name", PAGES)
def test_no_horizontal_scroll_and_no_js_errors_in_every_language(browser, base_url, page_name):
    problems = []
    for lang in LANGS:
        for width in WIDTHS:
            context = browser.new_context(viewport={"width": width, "height": 800})
            context.add_init_script(f"localStorage.setItem('app_lang', '{lang}');"
                                    "window.__csp = []; document.addEventListener('securitypolicyviolation',"
                                    " (e) => window.__csp.push(e.violatedDirective + ' ' + e.blockedURI));")
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(f"{base_url}/{page_name}")
            page.wait_for_timeout(200)
            overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
            html_lang = page.evaluate("document.documentElement.lang")
            csp = page.evaluate("window.__csp")
            lang_ok = html_lang == lang                                                # все страницы — на четырёх языковых вариантах
            if overflow > 0 or errors or csp or not lang_ok:
                problems.append((lang, width, overflow, html_lang, errors[:1], csp[:2]))
            context.close()
    assert problems == []


@pytest.mark.parametrize("lang, expected", [("uz", "Kirish"), ("uz-Cyrl", "Кириш"), ("en", "Sign in")])
def test_sign_in_page_shows_the_chosen_language_and_switches_on_the_fly(browser, base_url, lang, expected):
    """Главная без входа ведёт на страницу входа (редизайн 2026-11-15); без сервера API — вход логином и паролем. Заголовок
    и переключатель языков; нажатие на «Русский» переводит на лету."""
    context = browser.new_context(viewport={"width": 390, "height": 800})
    context.add_init_script(f"localStorage.setItem('app_lang', '{lang}');")
    page = context.new_page()
    page.goto(f"{base_url}/index.html")
    page.wait_for_url("**/login.html")
    page.wait_for_selector("#passView:not([hidden])")
    assert page.inner_text("#passView h1") == expected
    assert page.locator("#langHost .lang-btn").count() == 4
    assert page.locator(f'#langHost .lang-btn[data-lang="{lang}"]').get_attribute("aria-pressed") == "true"
    page.click('#langHost .lang-btn[data-lang="ru"]')
    page.wait_for_timeout(100)
    assert page.inner_text("#passView h1") == "Вход" and page.evaluate("document.documentElement.lang") == "ru"
    assert page.title() == "Вход — Узнать товар"
    context.close()


def test_admin_signed_in_views_fit_narrow_screens_in_every_language(browser, base_url):
    """Управление после входа (ответы API подменены в браузере): верхняя строка с переключателем языков, таблицы и карточки
    не дают горизонтальной прокрутки страницы; нет ошибок JavaScript и нарушений CSP."""
    import json
    me = {"username": "administrator.long", "role": "admin", "permissions": ["store:manage", "user:manage", "catalog:write"], "all_stores": True,
          "store_ids": [], "admin_listener": True}
    system = {"server_time_local": "2026-11-07 23:10:00", "timezone": "Asia/Tashkent", "database": "ok", "queue": "ok",
              "worker": {"online": False, "nodes": []}, "storage": {"path": "data", "usage": {"bytes": 1, "files": 1, "truncated": False}, "disk": None},
              "settings": [{"key": "max_upload_mb", "label": "x", "value": 25, "source": ".env: MAX_UPLOAD_MB"}], "settings_note": "x"}
    users = [{"id": "u1", "username": "administrator.long", "role": "admin", "is_active": True, "store_ids": [], "created_at": "x"},
             {"id": "u2", "username": "merchandiser.with.long.name", "role": "merchandiser", "is_active": True, "store_ids": [], "created_at": "x",
              "must_change_password": True, "temp_password_expires_at": "2026-11-09T10:00:00"}]
    answers = {"/api/v1/auth/me": me, "/api/v1/stores": [], "/api/v1/users": users, "/api/v1/admin/system": system,
               "/api/v1/admin/products": {"total": 0, "limit": 50, "offset": 0, "items": []}}

    def fulfil(route):
        path = route.request.url.split("?")[0].split("127.0.0.1")[-1]
        path = path[path.index("/api/"):] if "/api/" in path else path
        route.fulfill(status=200, content_type="application/json", body=json.dumps(answers.get(path, {})))

    problems = []
    for lang in LANGS:
        for width in (320, 390):
            for section in ("#/products", "#/users", "#/system"):
                context = browser.new_context(viewport={"width": width, "height": 800})
                context.add_init_script(f"localStorage.setItem('app_lang', '{lang}'); sessionStorage.setItem('shelf_audit_token', 't');"
                                        "window.__csp = []; document.addEventListener('securitypolicyviolation',"
                                        " (e) => window.__csp.push(e.violatedDirective));")
                context.route("**/api/v1/**", fulfil)
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.goto(f"{base_url}/admin.html{section}")
                page.wait_for_timeout(250)
                overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
                csp = page.evaluate("window.__csp")
                if overflow > 0 or errors or csp or page.evaluate("document.documentElement.lang") != lang:
                    problems.append((lang, width, section, overflow, errors[:1], csp[:1]))
                context.close()
    assert problems == []


def _mock_auth_api(context, state):
    """Подменённый сервер входа (документ 18) для проверки страниц в настоящем Chromium: пароль → код → токен; сеансы; 2FA."""
    import json

    def reply(route, status, body):
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def handler(route):
        request = route.request
        path = request.url.split("/api/v1", 1)[1].split("?")[0]
        body = request.post_data or ""
        state.setdefault("log", []).append(f"{request.method} {path}")
        if path == "/auth/login":
            return reply(route, 200, {"mfa_required": True, "mfa_token": "pass-0123456789", "expires_in": 300, "username": "admin"})
        if path == "/auth/login/2fa":
            ok = '"123456"' in body
            return reply(route, 200 if ok else 422, {"access_token": "tok-2fa", "token_type": "bearer", "expires_in": 600,
                                                     "username": "admin", "role": "admin"} if ok else
                         {"detail": "Код не подошёл", "code": "mfa_code_wrong", "params": {"attempts_left": 4}})
        if path == "/auth/me":
            return reply(route, 200, {"username": "admin", "role": "admin", "permissions": ["catalog:read", "catalog:write", "store:manage"],
                                      "all_stores": True, "store_ids": [], "mfa_enabled": state.get("enabled", False)})
        if path == "/auth/2fa":
            return reply(route, 200, {"enabled": state.get("enabled", False), "available": True, "pending": False,
                                      "enabled_at": "2026-11-08T06:00:00" if state.get("enabled") else None})
        if path == "/auth/sessions":
            return reply(route, 200, [
                {"id": "s1", "created_at": "2026-11-08T05:00:00", "expires_at": "2026-11-08T17:00:00", "last_used_at": "2026-11-08T06:00:00",
                 "device_label": None, "device_kind": "computer", "device_os": "Windows", "device_browser": "Chrome", "source": "local",
                 "mfa": True, "current": True},
                {"id": "s2", "created_at": "2026-11-07T05:00:00", "expires_at": "2026-11-07T17:00:00", "last_used_at": None,
                 "device_label": "Очень длинное название устройства, которое человек ввёл сам", "device_kind": "phone",
                 "device_os": "Android", "device_browser": "Chrome", "source": "remote", "mfa": False, "current": False}])
        if path == "/auth/2fa/setup":
            return reply(route, 200, {"secret": "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP", "secret_grouped": "JBSW Y3DP EHPK 3PXP JBSW Y3DP EHPK 3PXP",
                                      "otpauth_uri": "otpauth://totp/Uznat%20tovar%3Aadmin?secret=JBSWY3DPEHPK3PXP"})
        if path == "/auth/2fa/confirm":
            state["enabled"] = True
            return reply(route, 200, {"status": "enabled", "other_sessions_ended": 1})
        if path == "/auth/password":
            return reply(route, 200, {"access_token": "tok-new", "token_type": "bearer", "expires_in": 600, "username": "admin",
                                      "role": "admin"})
        return reply(route, 404, {})

    context.route("**/api/v1/**", handler)


@pytest.mark.parametrize("width", (320, 390, 1280))
def test_two_step_sign_in_and_security_panel_work_in_chromium_under_csp(browser, base_url, width):
    """Страница «Настройки» в настоящем Chromium с настоящими заголовками безопасности: вход паролем → поле кода → вход; группа
    «Безопасность входа»: сеансы, включение второго фактора (ключ и ссылка otpauth://). Нет ошибок JS, нарушений CSP и прокрутки вбок."""
    state = {}
    context = browser.new_context(viewport={"width": width, "height": 900})
    context.add_init_script("window.__csp = []; document.addEventListener('securitypolicyviolation', (e) => window.__csp.push(e.violatedDirective));")
    _mock_auth_api(context, state)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(f"{base_url}/login.html?next=settings.html")      # вход — на единой странице (редизайн 2026-11-15)
    page.fill("#loginUser", "admin")
    page.fill("#loginPass", "secret-pass")
    page.click("#passBtn")
    page.wait_for_selector("#loginCodeField:not([hidden])")
    page.fill("#loginCode", "000000")
    page.click("#passBtn")
    page.wait_for_selector("#passError:has-text('4')")          # wait_for_function со строкой запрещён CSP страницы (и правильно)
    page.fill("#loginCode", "123456")
    page.click("#passBtn")
    page.wait_for_url("**/settings.html")
    page.wait_for_selector("#securityGroup:not([hidden])")
    page.wait_for_selector("#sessionList li >> nth=1")
    assert page.locator("#sessionList li").count() == 2
    page.click("#mfaEnableBtn")
    page.fill("#mfaSetupPass", "secret-pass")
    page.click("#mfaSetupBtn")
    page.wait_for_selector("#mfaConfirmForm:not([hidden])")
    assert page.inner_text("#mfaSecret").startswith("JBSW Y3DP")
    assert page.get_attribute("#mfaLink", "href").startswith("otpauth://totp/")
    page.fill("#mfaConfirmCode", "123456")
    page.click("#mfaConfirmBtn")
    page.wait_for_selector("#mfaDisableBtn:not([hidden])")
    page.click("#passwordChangeBtn")                            # смена своего пароля (документ 05, §12.11)
    page.wait_for_selector("#passwordForm:not([hidden])")
    assert page.is_visible("#pwCodeField")
    page.fill("#pwCurrent", "secret-pass"); page.fill("#pwNew", "new-secret-1"); page.fill("#pwRepeat", "new-secret-1")
    page.fill("#pwCode", "123456")
    page.click("#passwordConfirmBtn")
    page.wait_for_selector("#securityDone:not([hidden])")
    assert page.evaluate("sessionStorage.getItem('shelf_audit_token')") == "tok-new"
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    csp = page.evaluate("window.__csp")
    assert (overflow, errors, csp) == (0, [], [])
    assert state["log"].count("POST /auth/login/2fa") == 2 and "POST /auth/2fa/confirm" in state["log"]
    assert "POST /auth/password" in state["log"]
    context.close()


def test_two_step_sign_in_on_the_sign_in_page_in_chromium(browser, base_url):
    state = {}
    context = browser.new_context(viewport={"width": 390, "height": 800})
    context.add_init_script("window.__csp = []; document.addEventListener('securitypolicyviolation', (e) => window.__csp.push(e.violatedDirective));")
    _mock_auth_api(context, state)
    context.route("**/api/v1/catalog/products**", lambda route: route.fulfill(status=200, content_type="application/json", body="[]"))
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(f"{base_url}/index.html")
    page.wait_for_url("**/login.html")
    page.fill("#loginUser", "admin")
    page.fill("#loginPass", "secret-pass")
    page.click("#passBtn")
    page.wait_for_selector("#loginCodeField:not([hidden])")
    page.fill("#loginCode", "123456")
    page.click("#passBtn")
    page.wait_for_url("**/products.html")                      # администратор — сразу к товарам
    assert page.evaluate("sessionStorage.getItem('shelf_audit_token')") == "tok-2fa"
    assert (errors, page.evaluate("window.__csp")) == ([], [])
    context.close()


def test_temporary_password_first_sign_in_in_chromium(browser, base_url):
    """Вход временным паролем (документ 05, §12.2–§12.3) в настоящем Chromium с CSP: форма своего пароля вместо сеанса, установка
    с пропуском, затем обычный сеанс. Ответы сервера подменены."""
    import json
    log = []

    def fulfil(route):
        req = route.request
        path = req.url.split("?")[0]
        path = path[path.index("/api/"):]
        log.append(f"{req.method} {path} {req.headers.get('authorization', '')}")
        replies = {
            "/api/v1/auth/login": {"password_change_required": True, "password_token": "pw-1", "expires_in": 900, "username": "new.merch",
                                   "code_required": False},
            "/api/v1/auth/password/initial": {"access_token": "tok-own", "token_type": "bearer", "expires_in": 3600, "username": "new.merch",
                                              "role": "merchandiser"},
            "/api/v1/auth/me": {"username": "new.merch", "role": "merchandiser", "permissions": ["catalog:read"], "admin_listener": False},
            "/api/v1/auth/2fa": {"enabled": False, "required": False, "available": True, "recovery_left": 0},
            "/api/v1/auth/sessions": [],
        }
        route.fulfill(status=200, content_type="application/json", body=json.dumps(replies.get(path, {})))

    context = browser.new_context(viewport={"width": 320, "height": 800})
    context.add_init_script("localStorage.setItem('app_lang', 'ru'); window.__csp = [];"
                            " document.addEventListener('securitypolicyviolation', (e) => window.__csp.push(e.violatedDirective));")
    context.route("**/api/v1/**", fulfil)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(f"{base_url}/login.html")
    page.fill("#loginUser", "new.merch"); page.fill("#loginPass", "7KQM-X4HP-9TDW")
    page.click("#passBtn")
    page.wait_for_url("**/settings.html")                      # временный пароль — свой пароль задаётся в «Настройках»
    page.wait_for_selector("#initialPanel:not([hidden])")
    assert page.is_hidden("#loginPanel") and page.is_hidden("#initialCodeField")
    assert page.evaluate("sessionStorage.getItem('shelf_audit_token')") is None
    page.fill("#initialNew", "own-pass-123"); page.fill("#initialRepeat", "own-pass-123")
    page.click("#initialBtn")
    page.wait_for_selector("#accountInfo:not([hidden])")
    assert page.evaluate("sessionStorage.getItem('shelf_audit_token')") == "tok-own"
    assert "POST /api/v1/auth/password/initial Bearer pw-1" in log
    assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0
    assert errors == [] and page.evaluate("window.__csp") == []
    context.close()
