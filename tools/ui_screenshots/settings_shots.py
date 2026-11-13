"""Снимки и проверка НАСТОЯЩЕЙ страницы «Настройки» (frontend/settings.html) в Chromium: локальный сервер отдаёт файлы страницы, сервер для «me» подставной.
Данные вымышленные. Озвучки в программе больше нет, поэтому страница не должна даже трогать синтезатор речи браузера: он подменён «сторожем», который это записывает.
Нужно: pip install playwright && playwright install chromium (сеть нужна один раз).
Запуск из корня проекта:
  python tools/ui_screenshots/settings_shots.py --out /tmp/st_shots                                  # набор по умолчанию
  python tools/ui_screenshots/settings_shots.py --out /tmp/st_shots phone:uz:dark:toggle laptop:en:light:default
Вариант: устройство:язык:тема:сценарий (устройства: laptop 1280x800, phone 390x844, tablet 820x1180);
сценарии: default (просто открыть), toggle (переключить тему и «Узнавать сама», проверить, что выбор запомнен), admin (человек уже входил как администратор: «Вы вошли как…», в меню «Товары» и «Управление»),
signin (вход с нуля: неверный пароль → строка ошибки, верный → «Вы вошли как admin1» и 4 пункта меню, «Выйти» → форма вернулась; сервер подставной, пароль good-pass).
Печатает ошибки страницы и консоли, выход за край экрана, слишком мелкие кнопки, следы озвучки и несовпадение с ожидаемым (это важнее картинок); PNG сохраняет.
Шрифты — какие есть в системе; настоящий телефон и браузеры, кроме Chromium, не проверяются."""
import argparse
import functools
import http.server
import json
import pathlib
import sys
import threading
import urllib.parse

from playwright.sync_api import sync_playwright

DEVICES = {"laptop": (1280, 800), "phone": (390, 844), "tablet": (820, 1180)}
DEFAULT = ["laptop:ru:light:default", "phone:ru:light:toggle", "phone:uz:dark:default", "laptop:en:dark:toggle", "laptop:ru:light:admin", "phone:ru:light:signin", "laptop:en:dark:signin"]
FRONT = pathlib.Path(__file__).resolve().parents[2] / "frontend"

# «Сторож»: озвучки в программе нет, поэтому любое обращение страницы к синтезатору речи — ошибка
WATCH = """Object.defineProperty(window, 'speechSynthesis', { configurable: true, get() { window.__speechTouched = true; return undefined; } });"""
# слова про голос, которых на странице быть не должно (по языкам)
FORBIDDEN_WORDS = {"ru": ["озвуч", "голос", "вслух"], "uz": ["ovoz"], "en": ["voice", "aloud"]}
ME = {"username": "admin1", "role": "admin", "permissions": ["catalog:read", "catalog:write", "store:manage"]}
GOOD_PASSWORD = "good-pass"


def fake_api(route, log: list) -> None:
    """Подставной сервер входа: пароль «good-pass» — успех, «slow» — 429, остальное — 401; /auth/me отвечает всегда, /auth/logout тоже."""
    req = route.request
    log.append(f"{req.method} {req.url.split('/api/v1')[-1]}")
    if req.url.endswith("/auth/login") and req.method == "POST":
        password = urllib.parse.parse_qs(req.post_data or "").get("password", [""])[0]
        if password == GOOD_PASSWORD:
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"access_token": "tok-new", "token_type": "bearer"}))
        return route.fulfill(status=429 if password == "slow" else 401, content_type="application/json", body="{}")
    return route.fulfill(status=200, content_type="application/json", body=json.dumps(ME))


def serve() -> http.server.ThreadingHTTPServer:
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args, **kwargs):  # журнал запросов не нужен
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(FRONT)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def run_variant(browser, base: str, out: pathlib.Path, variant: str, full: bool) -> int:
    device, lang, theme, scenario = variant.split(":")
    w, h = DEVICES[device]
    ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=1, has_touch=device == "phone", reduced_motion="reduce")
    init = [f"localStorage.setItem('app_lang', {json.dumps(lang)});", f"localStorage.setItem('app_theme', {json.dumps(theme)});", WATCH]
    if scenario == "admin":
        init.append("sessionStorage.setItem('shelf_audit_token', 'tok');")
    ctx.add_init_script("\n".join(init))
    page = ctx.new_page()
    problems: list[str] = []
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    # в сценарии «signin» неверный пароль нарочно даёт 401, и Chromium пишет об этом в консоль сам, — это не ошибка страницы
    expected = ("status of 401",) if scenario == "signin" else ()
    page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") and not any(x in m.text for x in expected) else None)
    page.on("requestfailed", lambda r: problems.append(f"request failed: {r.url}"))
    api_log: list[str] = []
    page.route("**/api/**", lambda route: fake_api(route, api_log))
    page.goto(f"{base}/settings.html")
    page.wait_for_timeout(500)
    for leftover in ("#voiceSelect", "#voiceTest", "#voiceInfo", "#speakToggle", "#speakPrice"):
        if page.locator(leftover).count():
            problems.append(f"на странице остался элемент озвучки {leftover}")
    body = page.inner_text("body").lower()
    for word in FORBIDDEN_WORDS[lang]:
        if word in body:
            problems.append(f"на странице есть слово про озвучку «{word}»")
    if scenario == "toggle":
        page.click("#themeHost button[data-mode='dark']")
        if page.evaluate("document.documentElement.getAttribute('data-theme')") != "dark":
            problems.append("тема не переключилась на тёмную")
        page.click("#autoToggle")
        if json.loads(page.evaluate("localStorage.getItem('identify_settings')")) != {"auto": False}:
            problems.append("переключатель «Узнавать сама» не запомнен как {auto: false}: " + str(page.evaluate("localStorage.getItem('identify_settings')")))
    rail = lambda: page.locator(".rail__item:visible").count()
    token = lambda: page.evaluate("sessionStorage.getItem('shelf_audit_token')")
    if scenario == "signin":
        if not page.locator("#loginPanel").is_visible() or page.locator("#accountInfo").is_visible():
            problems.append("без входа должна быть видна форма и скрыто «Вы вошли»")
        page.fill("#loginUser", "admin1"); page.fill("#loginPass", "wrong")
        page.click("#loginBtn"); page.wait_for_timeout(300)
        if not page.locator("#loginError").is_visible() or not page.inner_text("#loginError").strip():
            problems.append("неверный пароль: под формой нет строки ошибки")
        if token() is not None:
            problems.append("неверный пароль, а токен появился")
        page.screenshot(path=str(out / f"{device}_{lang}_{theme}_signin_error.png"), full_page=full)
        page.fill("#loginPass", GOOD_PASSWORD)
        page.click("#loginBtn"); page.wait_for_timeout(400)
        if not page.locator("#accountInfo").is_visible() or page.locator("#loginPanel").is_visible():
            problems.append("после входа должно быть видно «Вы вошли», форма скрыта")
        elif "admin1" not in page.inner_text("#accountName"):
            problems.append("в «Вы вошли как …» нет имени: " + page.inner_text("#accountName"))
        if token() != "tok-new":
            problems.append(f"токен после входа: {token()!r}")
        if page.input_value("#loginPass") != "":
            problems.append("пароль остался в поле после входа")
        if rail() != 4:
            problems.append(f"после входа в меню видно {rail()} пунктов, ожидалось 4")
        page.screenshot(path=str(out / f"{device}_{lang}_{theme}_signin_done.png"), full_page=full)
        page.click("#logoutBtn"); page.wait_for_timeout(300)
        if token() is not None:
            problems.append("после «Выйти» токен остался")
        if not page.locator("#loginPanel").is_visible() or page.locator("#accountInfo").is_visible():
            problems.append("после «Выйти» должна вернуться форма")
        if not any(line.startswith("POST") and line.endswith("/auth/logout") for line in api_log):
            problems.append("серверу не сообщили о выходе: " + "; ".join(api_log))
        if page.locator("#loginPass").count() != 1 or page.get_attribute("#loginPass", "autocomplete") != "current-password":
            problems.append("у поля пароля нет подсказки для менеджера паролей")
    if scenario == "admin":
        if not page.locator("#accountInfo").is_visible() or "admin1" not in page.inner_text("#accountName") or page.locator("#loginPanel").is_visible():
            problems.append("с токеном должно быть видно «Вы вошли как admin1», без формы")
        if rail() != 4:
            problems.append(f"в меню видно {rail()} пунктов, ожидалось 4 (Узнать, Товары, Настройки, Управление)")
    elif rail() != 2:
        problems.append(f"без входа в меню должно быть 2 пункта, видно {page.locator('.rail__item:visible').count()}")
    if page.evaluate("window.__speechTouched === true"):
        problems.append("страница обратилась к синтезатору речи, а озвучки больше нет")
    over = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    if over > 0:
        problems.append(f"страница шире экрана на {over}px")
    small = page.evaluate("""() => [...document.querySelectorAll('button, a.rail__item, select, .check')]
        .filter(e => e.offsetParent !== null && !e.closest('[hidden]') && !(e.classList.contains('check') && matchMedia('(pointer: fine)').matches))   // строка-переключатель с мышью компактнее — так задумано (ui.css)
        .map(e => [e, e.getBoundingClientRect()]).filter(([e, r]) => r.height < 43.5)
        .map(([e, r]) => (e.id || e.className || e.tagName) + ':' + Math.round(r.height))""")
    if small:
        problems.append("нажимаемое меньше 44px: " + ", ".join(small[:6]))
    name = f"{device}_{lang}_{theme}_{scenario}.png"
    page.screenshot(path=str(out / name), full_page=full)
    print(("ОШИБКИ " if problems else "ok     ") + name)
    for p in problems:
        print("   ", p)
    ctx.close()
    return len(problems)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/st_shots")
    ap.add_argument("--full", action="store_true", help="снимать всю страницу, а не видимый экран")
    ap.add_argument("variants", nargs="*", default=DEFAULT)
    args = ap.parse_args()
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    server = serve()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    problems = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for variant in args.variants:
            problems += run_variant(browser, base, out, variant, args.full)
        browser.close()
    server.shutdown()
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
