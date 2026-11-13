"""Снимки и проверка НАСТОЯЩЕЙ страницы «Узнать товар» (frontend/identify.html) в Chromium: локальный сервер отдаёт файлы страницы, ответы API
(вход, права, узнавание) подставные, камера — поддельная (флаги Chromium). Данные вымышленные. Нужно: pip install playwright pillow &&
playwright install chromium (сеть нужна один раз). Запуск из корня проекта:
  python tools/ui_screenshots/identify_shots.py --out /tmp/id_shots                                  # набор по умолчанию
  python tools/ui_screenshots/identify_shots.py --out /tmp/id_shots phone:uz:dark:unknown laptop:en:light:login
Вариант: устройство:язык:тема:сценарий (устройства: laptop 1280x800, phone 390x844, tablet 820x1180);
сценарии: login (форма входа), idle (камера выключена), live (камера включена), matched, uncertain, unknown, unknown_ro (нет права добавлять),
empty, error (сервер 500), denied (нет права на каталог), nocamera (камеры нет), picked (в «похоже» выбран вариант).
Снимок — видимый экран (--full: вся страница). Печатает ошибки страницы и консоли, выход за край экрана и несовпадение с ожидаемым текстом (это важнее картинок); PNG сохраняет.
Шрифты здесь — какие есть в системе, а не Segoe UI пользователя; настоящий телефон и браузеры, кроме Chromium, не проверяются."""
import argparse
import functools
import http.server
import io
import json
import pathlib
import sys
import threading

from playwright.sync_api import sync_playwright

DEVICES = {"laptop": (1280, 800), "phone": (390, 844), "tablet": (820, 1180)}
DEFAULT = ["laptop:ru:light:matched", "phone:ru:light:matched", "phone:uz:dark:unknown", "laptop:en:dark:uncertain", "laptop:ru:light:login"]
FRONT = pathlib.Path(__file__).resolve().parents[2] / "frontend"

NORDIC = {"id": "p1", "name": "Ergo M200", "brand": "Nordic", "expected_price": 249000, "score": 0.93}
ORBIT = {"id": "p2", "name": "Glide G110", "brand": "Orbit", "expected_price": 129000, "score": 0.81}
LUMEN = {"id": "p3", "name": "Lumen L30", "brand": "Не указан", "expected_price": None, "score": 0.77}
ANSWERS = {
    "matched": {"status": "matched", "product": NORDIC, "score": 0.93, "candidates": [NORDIC, ORBIT], "demo_mode": False},
    "picked": {"status": "uncertain", "product": NORDIC, "score": 0.62, "candidates": [NORDIC, ORBIT, LUMEN], "demo_mode": False},
    "uncertain": {"status": "uncertain", "product": NORDIC, "score": 0.62, "candidates": [NORDIC, ORBIT, LUMEN], "demo_mode": False},
    "unknown": {"status": "unknown", "product": None, "score": 0.2, "candidates": [], "demo_mode": False},
    "unknown_ro": {"status": "unknown", "product": None, "score": 0.2, "candidates": [], "demo_mode": False},
    "empty": {"status": "empty_catalog", "product": None, "score": 0.0, "candidates": [], "demo_mode": False},
}
# что должно быть на экране после ответа: (язык -> фрагмент текста ценника)
EXPECT = {
    "matched": {"ru": "Ergo M200", "uz": "Ergo M200", "en": "Ergo M200"},
    "uncertain": {"ru": "Это один из этих?", "uz": "Shulardan biri", "en": "one of these"},
    "picked": {"ru": "Glide G110", "uz": "Glide G110", "en": "Glide G110"},      # в сценарии выбирается второй вариант
    "unknown": {"ru": "Этого товара нет в каталоге", "uz": "katalogda yo", "en": "isn’t in the catalog"},
    "unknown_ro": {"ru": "Попросите того, кто ведёт каталог", "uz": "yurituvchidan", "en": "Ask whoever"},
    "empty": {"ru": "В каталоге пока нет товаров", "uz": "hali mahsulot yo", "en": "no products"},
    "error": {"ru": "Не получилось распознать", "uz": "aniqlab bo", "en": "Could not identify"},
}


def tiny_jpeg() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (180, 180, 180)).save(buf, "JPEG")
    return buf.getvalue()


def serve() -> http.server.ThreadingHTTPServer:
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args, **kwargs):  # журнал запросов не нужен
            pass

    handler = functools.partial(Quiet, directory=str(FRONT))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def install_api(page, scenario: str) -> None:
    perms = ["catalog:read", "catalog:write", "store:manage"]
    if scenario == "unknown_ro":
        perms = ["catalog:read"]
    if scenario == "denied":
        perms = ["report:read"]

    def respond(route):
        url, method = route.request.url, route.request.method
        if url.endswith("/auth/login"):
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"access_token": "tok"}))
        if url.endswith("/auth/me"):
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"username": "a", "role": "admin", "permissions": perms}))
        if url.endswith("/catalog/identify") and method == "POST":
            if scenario == "error":
                return route.fulfill(status=500, content_type="application/json", body=json.dumps({"detail": ""}))
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(ANSWERS.get(scenario, ANSWERS["matched"])))
        return route.fulfill(status=404, body="{}")
    page.route("**/api/**", respond)


def run_variant(browser, base: str, out: pathlib.Path, variant: str, photo: bytes, full: bool = False) -> int:
    device, lang, theme, scenario = variant.split(":")
    w, h = DEVICES[device]
    ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=1, has_touch=device == "phone", reduced_motion="reduce", permissions=["camera"])
    init = [f"localStorage.setItem('app_lang', {json.dumps(lang)});", f"localStorage.setItem('app_theme', {json.dumps(theme)});"]
    if scenario != "login":
        init.append("sessionStorage.setItem('shelf_audit_token', 'tok');")
    if scenario == "nocamera":
        init.append("Object.defineProperty(navigator, 'mediaDevices', {value: undefined, configurable: true});")
    ctx.add_init_script("\n".join(init))
    page = ctx.new_page()
    problems: list[str] = []
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    def on_console(m):
        if m.type not in ("error", "warning"):
            return
        if scenario == "error" and "status of 500" in m.text:  # браузер сам пишет про подставной ответ 500 — так и задумано
            return
        problems.append(f"console.{m.type}: {m.text}")
    page.on("console", on_console)
    page.on("requestfailed", lambda r: problems.append(f"request failed: {r.url}"))
    install_api(page, scenario)
    page.goto(f"{base}/identify.html")
    page.wait_for_timeout(300)
    if scenario in ("live", "matched", "uncertain", "unknown", "unknown_ro", "empty", "error", "picked"):
        if scenario == "live":
            page.click("#camBtn")
            page.wait_for_timeout(900)
        else:
            page.set_input_files("#fileInput", files=[{"name": "a.jpg", "mimeType": "image/jpeg", "buffer": photo}])
            page.wait_for_timeout(700)
    if scenario == "picked":
        page.click(".cand >> nth=1")
        page.wait_for_timeout(300)
    # проверки
    ticket = page.inner_text("#ticket") if page.locator("#ticket").count() and page.locator("#mainArea").is_visible() else ""
    want = EXPECT.get(scenario, {}).get(lang)
    if want and want.lower() not in (ticket + " " + page.inner_text("#resError")).lower():
        problems.append(f"на экране нет «{want}»; ценник: {ticket[:120]!r}")
    if scenario == "login" and not page.locator("#loginPanel").is_visible():
        problems.append("форма входа не показана")
    if scenario == "denied" and not page.locator("#accessBanner").is_visible():
        problems.append("нет сообщения «Нет доступа»")
    if scenario == "live" and page.get_attribute("#stage", "data-live") != "true":
        problems.append("камера не включилась (data-live != true)")
    over = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    if over > 0:
        problems.append(f"страница шире экрана на {over}px")
    small = page.evaluate("""() => [...document.querySelectorAll('button, a.rail__item, input[type=checkbox], select, input.input')]
        .filter(e => e.offsetParent !== null && !e.closest('[hidden]'))
        .map(e => [e, e.getBoundingClientRect()]).filter(([e, r]) => (e.type === 'checkbox' ? false : r.height < 43.5))
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
    ap.add_argument("--out", default="/tmp/id_shots")
    ap.add_argument("--full", action="store_true", help="снимать всю страницу, а не видимый экран (закреплённое меню тогда рисуется посередине)")
    ap.add_argument("variants", nargs="*", default=DEFAULT)
    args = ap.parse_args()
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    server = serve()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    photo = tiny_jpeg()
    problems = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"])
        for variant in args.variants:
            problems += run_variant(browser, base, out, variant, photo, args.full)
        browser.close()
    server.shutdown()
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
