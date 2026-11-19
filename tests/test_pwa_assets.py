"""
Иконка на главный экран телефона (PWA): манифест, иконки, подключение к страницам, корневой редирект, scripts/phone_url.py.
Реально выполняется (PIL читает настоящие PNG). Не проверяется: как именно конкретный телефон нарисует иконку/установит приложение.
"""
from __future__ import annotations

import ast
import json
import mimetypes
import re
from pathlib import Path

from PIL import Image

from scripts import phone_url
from tools import make_icons

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
PAGES = ["index.html", "admin.html", "products.html", "identify.html", "settings.html"]



def _pixels(image):
    """Пиксели изображения: Pillow 12+ — get_flattened_data, раньше — getdata (будет удалён в Pillow 14)."""
    return getattr(image, "get_flattened_data", image.getdata)()

def _manifest():
    return json.loads((FRONTEND / "manifest.webmanifest").read_text(encoding="utf-8"))


def _open(name):
    with Image.open(FRONTEND / "icons" / name) as im:                       # копия в памяти, файл закрыт (без ResourceWarning)
        return im.copy()


# ----------------------------------------------------------------------- манифест

def test_manifest_has_required_fields_and_a_short_launcher_label():
    m = _manifest()
    assert m["display"] == "standalone" and m["lang"] == "ru"
    assert m["name"] and 0 < len(m["short_name"]) <= 12            # длиннее обрезается под иконкой
    assert m["scope"] == "./"


def test_start_url_is_relative_and_points_to_an_existing_page():
    m = _manifest()
    assert not m["start_url"].startswith(("/", "http"))             # относительный — работает при любом префиксе (/ui/)
    assert (FRONTEND / m["start_url"]).is_file()


def test_manifest_colours_match_the_pages_and_the_icon_background():
    m = _manifest()
    hexbg = "#%02x%02x%02x" % make_icons.BG
    assert m["theme_color"] == m["background_color"] == hexbg
    for page in PAGES:
        assert f'<meta name="theme-color" content="{hexbg}">' in (FRONTEND / page).read_text(encoding="utf-8"), page


def test_manifest_icons_exist_with_declared_sizes_and_cover_any_and_maskable():
    icons = _manifest()["icons"]
    assert {(i["sizes"], i["purpose"]) for i in icons} >= {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")}
    for i in icons:
        path = FRONTEND / i["src"]
        assert path.is_file(), i["src"]
        w, h = (int(x) for x in i["sizes"].split("x"))
        with Image.open(path) as im:
            assert im.size == (w, h) and i["type"] == "image/png"


def test_manifest_is_served_with_the_manifest_mime_type():
    assert mimetypes.guess_type("manifest.webmanifest")[0] == "application/manifest+json"


# ----------------------------------------------------------------------- иконки

def test_any_icons_have_rounded_transparent_corners_and_opaque_centre():
    for name in ("icon-192.png", "icon-512.png"):
        im = _open(name).convert("RGBA")
        w, h = im.size
        assert im.getpixel((0, 0))[3] == 0 and im.getpixel((w - 1, h - 1))[3] == 0, name
        assert im.getpixel((w // 2, h // 2))[3] == 255, name


def test_maskable_icon_is_full_bleed_and_keeps_the_glyph_inside_the_safe_circle():
    im = _open("icon-maskable-512.png").convert("RGBA")
    w, h = im.size
    bg = make_icons.BG + (255,)
    assert im.getpixel((0, 0)) == bg and im.getpixel((w - 1, h - 1)) == bg      # фон на весь квадрат, без прозрачности
    safe_radius = w * 0.40                                                       # Android гарантирует круг 80% стороны
    cx = cy = w / 2
    outside_non_bg = [
        (x, y) for y in range(0, h, 2) for x in range(0, w, 2)
        if ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 > safe_radius and im.getpixel((x, y)) != bg
    ]
    assert outside_non_bg == []                                                  # ничего значимого не обрежется маской
    assert any(im.getpixel((x, y)) != bg for y in range(0, h, 4) for x in range(0, w, 4))   # а рисунок вообще есть


def test_apple_touch_icon_is_a_flat_180px_square_without_transparency():
    im = _open("apple-touch-icon.png")
    assert im.size == (180, 180) and im.mode == "RGB"                            # iOS заливает прозрачное чёрным


def test_icons_are_not_blank():
    for name in ("icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png"):
        colours = _open(name).convert("RGB").getcolors(maxcolors=1 << 20)
        assert len(colours) > 5, name
    assert _open("favicon-32.png").size == (32, 32)


def test_windows_ico_has_all_sizes_and_matches_the_generator():
    with Image.open(FRONTEND / "icons" / "app.ico") as ico:
        assert set(ico.ico.sizes()) >= {(16, 16), (32, 32), (48, 48), (256, 256)}    # Проводник/панель задач берут свой размер
        fresh = make_icons.render(256, kind="any").convert("RGBA")
        assert list(_pixels(ico.ico.getimage((256, 256)).convert("RGBA"))) == list(_pixels(fresh))


def test_committed_icons_match_the_generator_output():
    # если поменяли tools/make_icons.py — перегенерируйте: python tools/make_icons.py
    for name, size, kind in make_icons.SPECS:
        fresh = make_icons.render(size, kind=kind).convert("RGBA")
        committed = _open(name).convert("RGBA")
        assert fresh.size == committed.size and list(_pixels(fresh)) == list(_pixels(committed)), name


# ----------------------------------------------------------------------- подключение к страницам

def test_every_page_links_manifest_icons_and_ios_meta_to_existing_files():
    for page in PAGES:
        html = (FRONTEND / page).read_text(encoding="utf-8")
        assert '<link rel="manifest" href="manifest.webmanifest">' in html, page
        assert 'name="apple-mobile-web-app-capable" content="yes"' in html, page
        for href in re.findall(r'<link rel="(?:icon|apple-touch-icon|manifest)"[^>]*href="([^"]+)"', html):
            assert (FRONTEND / href).is_file(), (page, href)
        assert 'rel="apple-touch-icon"' in html and 'rel="icon"' in html, page


def test_server_root_redirects_to_the_home_page():
    tree = ast.parse((ROOT / "app" / "main.py").read_text(encoding="utf-8"))
    handler = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
        and any(isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "get" and d.args
                and isinstance(d.args[0], ast.Constant) and d.args[0].value == "/" for d in n.decorator_list)
    )
    code = ast.unparse(handler).replace("'", '"')
    # сотрудники — главная; вход покупателя (адрес из QR) — страница покупателя
    assert re.search(r'RedirectResponse\(url="/ui/shop.html" if CUSTOMER_ENTRANCE else "/ui/index.html"\)', code), code
    assert (FRONTEND / "index.html").is_file()


# ----------------------------------------------------------------------- scripts/phone_url.py

def test_lan_ip_rejects_loopback_and_offline():
    assert phone_url.lan_ip(lambda: "192.168.1.57") == "192.168.1.57"
    assert phone_url.lan_ip(lambda: "127.0.0.1") is None and phone_url.lan_ip(lambda: "0.0.0.0") is None
    assert phone_url.lan_ip(lambda: (_ for _ in ()).throw(OSError("offline"))) is None


def test_phone_urls_point_at_the_ui_pages_served_by_the_api():
    urls = phone_url.phone_urls("192.168.1.57", 8000)
    assert urls["home"] == "http://192.168.1.57:8000/ui/index.html"
    for key, page in (("home", "index.html"), ("identify", "identify.html"), ("products", "products.html")):
        assert urls[key].endswith("/ui/" + page) and (FRONTEND / page).is_file()


class FakeQr:
    """Замена модуля qrcode: запоминает данные и умеет print_ascii."""
    last = None
    def QRCode(self, border=1):
        outer = self
        class Q:
            def add_data(self, data): outer.data = data
            def make(self, fit=True): pass
            def print_ascii(self, out=None, tty=False, invert=False):
                out.write(f"[QR {outer.data} invert={invert}]")
        return Q()


def test_render_qr_returns_ascii_or_none_when_the_package_is_missing():
    text = phone_url.render_qr("http://x/ui/identify.html", qr_module=FakeQr())
    assert text == "[QR http://x/ui/identify.html invert=True]"
    assert "invert=False" in phone_url.render_qr("http://x", invert=False, qr_module=FakeQr())


def test_main_prints_url_qr_and_checklist():
    lines = []
    assert phone_url.main([], lan=lambda: "192.168.1.57", out=lines.append, qr_module=FakeQr()) == 0
    text = "\n".join(lines)
    assert "http://192.168.1.57:8000/ui/index.html" in text and "[QR http://192.168.1.57:8000/ui/index.html" in text
    assert "0.0.0.0" in text and "брандмауэр" in text and "главный экран" in text


def test_main_port_host_and_light_options_and_failure_paths():
    lines = []
    # автоопределение нашло ДРУГОЙ адрес (например, адаптер VPN) — явный --host должен победить
    assert phone_url.main(["--port", "8080", "--host", "10.0.0.7", "--light"], lan=lambda: "172.16.0.9", out=lines.append,
                          qr_module=FakeQr()) == 0
    text = "\n".join(lines)
    assert "http://10.0.0.7:8080/ui/index.html" in text and "172.16.0.9" not in text and "invert=False" in text
    lines.clear()
    assert phone_url.main([], lan=lambda: None, out=lines.append) == 1
    assert "--host" in lines[0] and "ipconfig" in lines[0]


def test_main_without_qrcode_package_still_prints_the_address_and_survives_console_encoding_errors():
    lines = []
    # qr_module=None: берётся настоящий пакет qrcode; если его нет — QR пропускается, адрес всё равно печатается
    assert phone_url.main([], lan=lambda: "192.168.1.5", out=lines.append, qr_module=None) == 0
    assert any("http://192.168.1.5:8000/ui/identify.html" in l for l in lines)

    def picky_out(msg):
        if "[QR" in str(msg):
            raise UnicodeEncodeError("cp866", "█", 0, 1, "nope")
        lines.append(msg)
    lines.clear()
    assert phone_url.main([], lan=lambda: "192.168.1.5", out=picky_out, qr_module=FakeQr()) == 0
    assert any("консоль не показывает QR-код" in l for l in lines)
