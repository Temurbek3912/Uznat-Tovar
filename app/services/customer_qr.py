"""QR-код страницы покупателя для сотрудников и владельца: какой адрес сейчас в QR и лист для печати (по просьбе владельца 2026-11-16).

Адрес: вход из интернета, если программа запущена с ним (start_demo кладёт адрес в CUSTOMER_PUBLIC_URL процесса сервера), иначе —
адрес компьютера в сети магазина (телефон должен быть в Wi-Fi магазина). Картинки строятся в памяти, на диск ничего не пишется."""
from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path

SHOP_PATH = "/ui/shop.html"
ENV_PUBLIC_URL = "CUSTOMER_PUBLIC_URL"

# Надписи листа (как на листе, сделанном вручную 2026-11-16: data/qr-pokupatel.png). Узбекский апостроф — ʻ (U+02BB).
SHEET_TITLE = "Узнайте товар по фото"
SHEET_LINES = ("Наведите камеру телефона на код", "Mahsulotni rasm orqali bilib oling")

_FONT_DIRS = (Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts", Path("/usr/share/fonts/truetype/dejavu"))
_FONTS_BOLD = ("arialbd.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf")
_FONTS_REGULAR = ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf")


@dataclass(frozen=True)
class CustomerAddress:
    url: str          # полный адрес страницы покупателя ("" — адрес не определён)
    source: str       # "internet" | "wifi" | "none"


def customer_address(*, public_url: str | None, lan_ip: str | None, port: int, scheme: str) -> CustomerAddress:
    """Чистое правило выбора адреса. public_url — из CUSTOMER_PUBLIC_URL (уже https://…/ui/shop.html)."""
    public = (public_url or "").strip()
    if public.startswith("https://"):
        return CustomerAddress(public, "internet")
    if lan_ip:
        return CustomerAddress(f"{scheme}://{lan_ip}:{port}{SHOP_PATH}", "wifi")
    return CustomerAddress("", "none")


def lan_ip() -> str | None:
    """IP компьютера в сети магазина (как scripts/phone_url.lan_ip): UDP-«соединение» ничего не отправляет, лишь выбирает интерфейс."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip


def qr_svg_data_uri(url: str) -> str:
    """QR картинкой SVG (data:-адрес) для страницы; без пакета qrcode — пустая строка."""
    from app.core.totp import qr_data_uri

    return qr_data_uri(url) if url else ""


def _font(names: tuple[str, ...], size: int):
    from PIL import ImageFont

    for folder in _FONT_DIRS:
        for name in names:
            path = folder / name
            if path.is_file():
                try:
                    return ImageFont.truetype(str(path), size)
                except OSError:
                    continue
    return ImageFont.load_default(size=size)


def print_sheet_png(url: str) -> bytes:
    """Лист A4 (1240×1754, 150 точек на дюйм) для печати: заголовок, две строки, крупный QR, адрес мелко внизу."""
    import qrcode
    from PIL import Image, ImageDraw

    width, height = 1240, 1754
    sheet = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(sheet)

    def centered(text: str, y: int, font, fill="black") -> None:
        box = draw.textbbox((0, 0), text, font=font)
        draw.text(((width - (box[2] - box[0])) // 2 - box[0], y), text, font=font, fill=fill)

    title = _font(_FONTS_BOLD, 84)
    while draw.textlength(SHEET_TITLE, font=title) > width - 120 and title.size > 40:
        title = _font(_FONTS_BOLD, title.size - 4)
    centered(SHEET_TITLE, 80, title)
    regular = _font(_FONTS_REGULAR, 46)
    centered(SHEET_LINES[0], 210, regular)
    centered(SHEET_LINES[1], 290, regular)

    qr = qrcode.QRCode(border=0, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    side = 1000
    module = max(1, side // image.size[0])
    image = image.resize((image.size[0] * module, image.size[1] * module), Image.NEAREST)
    sheet.paste(image, ((width - image.size[0]) // 2, 420 + (side - image.size[1]) // 2))

    small = _font(_FONTS_REGULAR, 30)
    while draw.textlength(url, font=small) > width - 80 and small.size > 14:
        small = _font(_FONTS_REGULAR, small.size - 2)
    centered(url, 1500, small, fill="#444444")

    out = io.BytesIO()
    sheet.save(out, format="PNG", dpi=(150, 150))
    return out.getvalue()
