"""Снимки и проверка НАСТОЯЩЕЙ страницы «Товары» (frontend/products.html) в Chromium: локальный сервер отдаёт файлы страницы, ответы API (вход, права, список,
добавление, ожидание, правка, миниатюры) подставные. Данные вымышленные. Нужно: pip install playwright pillow && playwright install chromium (сеть нужна один раз).
Запуск из корня проекта:
  python tools/ui_screenshots/products_shots.py --out /tmp/pr_shots                                   # набор по умолчанию
  python tools/ui_screenshots/products_shots.py --out /tmp/pr_shots phone:uz:dark:list laptop:en:light:add
Вариант: устройство:язык:тема:сценарий (устройства: laptop 1280x800, phone 390x844, tablet 820x1180);
сценарии: login, list (список из 12 товаров), empty (каталога нет — форма открыта сама), many (250 товаров: страница догружает порциями по 100), error (список не загрузился),
denied (нет права на запись), add (выбрать 3 фото, написать название, добавить, дождаться «Готово»), dup (название уже есть — предложено добавить фото к нему),
edit (открыть первый товар на правку), barcode (адрес с ?barcode=: форма открыта, код вписан).
Снимок — видимый экран (--full: вся страница). Печатает ошибки страницы и консоли, выход за край экрана, слишком мелкие кнопки и несовпадение с ожидаемым текстом
(это важнее картинок); PNG сохраняет. Шрифты — какие есть в системе; настоящий телефон и браузеры, кроме Chromium, не проверяются."""
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
FRONT = pathlib.Path(__file__).resolve().parents[2] / "frontend"
sys.path.insert(0, str(FRONT.parent))
from tools import uz_review  # noqa: E402  (только чтобы прочитать словарь; узбекские ожидания берём из него, а не вшиваем)

_UZ = uz_review.parse_dictionary((FRONT / "i18n.uz.js").read_text(encoding="utf-8"))


def uz_fragment(key: str, **params) -> str:
    """Кусок узбекского текста для проверки «есть на экране»: подстановки заменяются params, а если остались незаполненные — берётся самый длинный статичный кусок.
    Так проверка не краснеет, когда переводчик меняет формулировку (узбекские тексты — черновик)."""
    value = _UZ[key]
    text = value if isinstance(value, str) else value["other"]
    for name, val in params.items():
        text = text.replace("{" + name + "}", str(val))
    pieces = [p.strip() for p in uz_review.re.split(r"\{\w+\}", text)]
    return max(pieces, key=len)

DEFAULT = ["laptop:ru:light:list", "phone:ru:light:list", "phone:uz:dark:add", "laptop:en:dark:edit", "laptop:ru:light:login", "laptop:ru:light:empty"]
CATEGORIES = [("Мыши", "Nordic"), ("Клавиатуры", "Delta"), ("Наушники", "Orbit"), ("Мониторы", "Pixel")]


def tiny_jpeg() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (180, 180, 180)).save(buf, "JPEG")
    return buf.getvalue()


def serve() -> http.server.ThreadingHTTPServer:
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args, **kwargs):  # журнал запросов не нужен
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(FRONT)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def product(n: int) -> dict:
    category, brand = CATEGORIES[n % len(CATEGORIES)]
    return {"id": f"p{n}", "sku": f"A{n:03d}", "name": f"{category[:-1]} {brand} M{n * 10 + 100}", "brand": brand, "category": category,
            "expected_price": None if n % 5 == 4 else 90000 + n * 13000, "photo_count": 0 if n % 6 == 5 else 2 + n % 3,
            "barcodes": [f"47800012{n:05d}"] if n % 3 == 0 else []}


ITEMS = [product(n) for n in range(12)]
EXPECT = {  # язык -> фрагмент текста на экране после сценария
    "list": {"ru": "Найдено: 12 товаров", "uz": uz_fragment("items.count", n=12), "en": "12 products"},
    "empty": {"ru": "Пока нет ни одного товара", "uz": uz_fragment("products.list_empty"), "en": "no products yet"},
    "many": {"ru": "Найдено: 250 товаров", "uz": uz_fragment("items.count", n=250), "en": "250 products"},
    "error": {"ru": "Не удалось загрузить список", "uz": uz_fragment("products.list_error"), "en": "Could not load the product list"},
    "add": {"ru": "добавлен в каталог", "uz": uz_fragment("products.done_new"), "en": "was added to the catalog"},
    "dup": {"ru": "уже есть в каталоге", "uz": uz_fragment("products.dup_default"), "en": "already in the catalog"},
    "edit": {"ru": "Изменить:", "uz": uz_fragment("products.edit_title"), "en": "Edit:"},
}
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000" "1f15c4890000000d49444154789c6360606060000000050001a5f645400000000049454e44ae426082")


def install_api(page, scenario: str, stats: dict) -> None:
    perms = ["catalog:read", "catalog:write", "store:manage"] if scenario != "denied" else ["catalog:read"]
    items = ITEMS if scenario not in ("empty", "many") else ([] if scenario == "empty" else [product(n) for n in range(250)])

    def respond(route):
        url, method = route.request.url, route.request.method
        path = url.split("/api/v1", 1)[-1]
        ok = lambda body, status=200: route.fulfill(status=status, content_type="application/json", body=json.dumps(body))  # noqa: E731
        if path.endswith("/auth/login"):
            return ok({"access_token": "tok"})
        if path.endswith("/auth/me"):
            return ok({"username": "a", "role": "admin", "permissions": perms})
        if "/catalog/products/upload" in path:
            if scenario == "dup":
                return ok({"detail": {"message": "Такой товар уже есть в каталоге.", "product_id": "p1", "product_name": ITEMS[1]["name"]}}, 409)
            return ok({"task_id": "t1", "name": "Мышь Nordic M200", "photos": 3, "warnings": []}, 202)
        if "/catalog/products/tasks/" in path:
            return ok({"state": "done"})
        if "/photo" in path and method == "GET":
            return route.fulfill(status=200, content_type="image/png", body=PNG)
        if path.startswith("/catalog/products?") and method == "GET":
            if scenario == "error":
                return ok({"detail": ""}, 500)
            query = dict(part.split("=", 1) for part in path.split("?", 1)[1].split("&") if "=" in part)
            stats["list_calls"] = stats.get("list_calls", 0) + 1
            offset, limit = int(query.get("offset", 0)), int(query.get("limit", 20))
            return ok(items[offset:offset + limit])
        if method == "PATCH":
            return ok(ITEMS[0])
        return route.fulfill(status=404, body="{}")
    page.route("**/api/**", respond)


def run_variant(browser, base: str, out: pathlib.Path, variant: str, photo: bytes, full: bool) -> int:
    device, lang, theme, scenario = variant.split(":")
    w, h = DEVICES[device]
    ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=1, has_touch=device == "phone", reduced_motion="reduce")
    init = [f"localStorage.setItem('app_lang', {json.dumps(lang)});", f"localStorage.setItem('app_theme', {json.dumps(theme)});"]
    if scenario != "login":
        init.append("sessionStorage.setItem('shelf_audit_token', 'tok');")
    ctx.add_init_script("\n".join(init))
    page = ctx.new_page()
    problems: list[str] = []
    stats: dict = {}
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))

    def on_console(m):
        if m.type in ("error", "warning") and not (scenario in ("error", "dup") and ("status of 500" in m.text or "status of 409" in m.text)):
            problems.append(f"console.{m.type}: {m.text}")
    page.on("console", on_console)
    page.on("requestfailed", lambda r: problems.append(f"request failed: {r.url}"))
    install_api(page, scenario, stats)
    page.goto(f"{base}/products.html" + ("?barcode=4780001234562" if scenario == "barcode" else ""))
    page.wait_for_timeout(500)
    files = [{"name": f"p{i}.jpg", "mimeType": "image/jpeg", "buffer": photo} for i in range(3)]
    if scenario in ("add", "dup"):
        page.click("#openAdd")
        page.fill("#nameInput", "Мышь Nordic M200")
        page.set_input_files("#photoInput", files=files)
        page.wait_for_timeout(500)
        page.click("#submitBtn")
        page.wait_for_timeout(1700 if scenario == "add" else 500)
    if scenario == "edit":
        page.click(".prow >> nth=0")
        page.wait_for_timeout(300)
    # проверки
    body = page.inner_text("body")
    want = EXPECT.get(scenario, {}).get(lang)
    if want and want.lower() not in body.lower():
        problems.append(f"на экране нет «{want}»")
    if scenario == "login" and not page.locator("#loginPanel").is_visible():
        problems.append("форма входа не показана")
    if scenario == "dup":
        top = page.evaluate("(() => { const r = document.getElementById('dupNotice').getBoundingClientRect(); return [r.top, r.bottom, window.innerHeight]; })()")
        if not (top[0] >= 0 and top[1] <= top[2]):
            problems.append(f"уведомление о дубле за краем экрана: {top}")
    if scenario == "denied" and not page.locator("#accessBanner").is_visible():
        problems.append("нет сообщения «Нет доступа»")
    if scenario == "barcode" and (page.input_value("#barcodeInput") != "4780001234562" or not page.locator("#addCard").is_visible()):
        problems.append("штрих-код из адреса не вписан или форма не открыта")
    if scenario == "empty" and not page.locator("#addCard").is_visible():
        problems.append("каталог пуст, а форма добавления не открыта")
    if scenario == "many" and stats.get("list_calls") != 3:
        problems.append(f"250 товаров должны прийти за 3 запроса, было {stats.get('list_calls')}")
    if scenario in ("list", "edit") and page.locator(".prow").count() != 12:
        problems.append(f"строк в списке {page.locator('.prow').count()}, ожидалось 12")
    if scenario in ("list", "edit"):
        page.wait_for_timeout(300)
        if page.locator(".prow__thumb img").count() == 0:
            problems.append("миниатюры не подгрузились")
    over = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    if over > 0:
        problems.append(f"страница шире экрана на {over}px")
    small = page.evaluate("""() => [...document.querySelectorAll('button, a.rail__item, a.btn, input.input, .prow')]
        .filter(e => e.offsetParent !== null && !e.closest('[hidden]') && !e.closest('.plist__filters'))
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
    ap.add_argument("--out", default="/tmp/pr_shots")
    ap.add_argument("--full", action="store_true", help="снимать всю страницу, а не видимый экран")
    ap.add_argument("variants", nargs="*", default=DEFAULT)
    args = ap.parse_args()
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    server = serve()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    photo = tiny_jpeg()
    problems = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for variant in args.variants:
            problems += run_variant(browser, base, out, variant, photo, args.full)
        browser.close()
    server.shutdown()
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
