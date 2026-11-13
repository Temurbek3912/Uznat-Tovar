#!/usr/bin/env python3
"""
Генерирует иконки приложения (frontend/icons/*) — воспроизводимо, без внешних файлов и шрифтов.

    python tools/make_icons.py

Рисунок: стеллаж с тремя полками и товарами разной высоты на бирюзовом фоне. Рисуется в 4x и уменьшается
(сглаживание). Три варианта под разные платформы:
  icon-192.png / icon-512.png   «any»: скруглённый квадрат с прозрачными углами (Android, ярлыки, вкладка);
  icon-maskable-512.png         «maskable»: фон на весь квадрат, рисунок в безопасной зоне ~80% — Android сам
                                вырезает круг/«каплю» любой формы, ничего важного не обрежется;
  apple-touch-icon.png (180)    iOS: сплошной квадрат БЕЗ прозрачности (iOS сам скругляет и заливает прозрачное чёрным);
  favicon-32.png                значок вкладки браузера;
  app.ico                       значок для ярлыка Windows (несколько размеров 16–256 в одном файле).
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

BG = (15, 118, 110)                     # #0f766e — тот же, что theme-color страниц
BOARD = (255, 255, 255)
PRODUCTS = [(251, 191, 36), (248, 113, 113), (103, 232, 249), (190, 242, 100), (253, 186, 116), (255, 255, 255)]
SS = 4                                  # суперсэмплинг


def _glyph(draw: ImageDraw.ImageDraw, size: int, margin: float) -> None:
    """Стеллаж внутри квадрата size x size, отступ margin (доля стороны) со всех сторон."""
    m = size * margin
    left, right, top, bottom = m, size - m, m, size - m
    w, h = right - left, bottom - top
    board_h = h * 0.055
    # три полки: товары стоят НА досках; верхняя полка ниже верхнего края, чтобы рисунок смотрелся компактно
    levels = [0.30, 0.60, 0.90]
    layouts = [[0.55, 0.85, 0.65, 0.95, 0.7], [0.8, 0.55, 0.9, 0.6, 0.75], [0.6, 0.9, 0.7, 0.55, 0.85]]
    for row, (frac, heights) in enumerate(zip(levels, layouts)):
        y_board = top + h * frac
        n = len(heights)
        gap = w * 0.035
        bw = (w - gap * (n + 1)) / n
        for i, hh in enumerate(heights):
            x0 = left + gap + i * (bw + gap)
            ph = h * 0.26 * hh
            colour = PRODUCTS[(i + row * 2) % len(PRODUCTS)]
            draw.rounded_rectangle([x0, y_board - ph, x0 + bw, y_board], radius=bw * 0.18, fill=colour)
        draw.rounded_rectangle([left, y_board, right, y_board + board_h], radius=board_h / 2, fill=BOARD)
    # боковые стойки стеллажа
    post = w * 0.035
    for x in (left - post * 1.4, right + post * 0.4):
        draw.rounded_rectangle([x, top + h * 0.02, x + post, bottom + board_h], radius=post / 2, fill=BOARD)


def render(size: int, *, kind: str) -> Image.Image:
    big = size * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if kind == "any":
        d.rounded_rectangle([0, 0, big - 1, big - 1], radius=int(big * 0.22), fill=BG + (255,))
        margin = 0.20
    elif kind == "maskable":
        d.rectangle([0, 0, big, big], fill=BG + (255,))
        margin = 0.27                                        # рисунок целиком внутри круга диаметром 80% стороны
    elif kind == "apple":
        d.rectangle([0, 0, big, big], fill=BG + (255,))
        margin = 0.20
    else:
        raise ValueError(kind)
    _glyph(d, big, margin)
    out = img.resize((size, size), Image.LANCZOS)
    if kind == "apple":                                       # iOS: никакой прозрачности
        flat = Image.new("RGB", out.size, BG)
        flat.paste(out, mask=out.split()[3])
        return flat
    return out


SPECS = [
    ("icon-192.png", 192, "any"), ("icon-512.png", 512, "any"),
    ("icon-maskable-512.png", 512, "maskable"), ("apple-touch-icon.png", 180, "apple"), ("favicon-32.png", 32, "any"),
]


ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def render_ico(path: Path) -> None:
    render(256, kind="any").save(path, format="ICO", sizes=ICO_SIZES)


def main(out_dir: Path | None = None) -> list[Path]:
    out_dir = out_dir or Path(__file__).resolve().parent.parent / "frontend" / "icons"
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, size, kind in SPECS:
        path = out_dir / name
        render(size, kind=kind).save(path, optimize=True)
        written.append(path)
    ico = out_dir / "app.ico"
    render_ico(ico)
    written.append(ico)
    return written


if __name__ == "__main__":
    for p in main():
        print(p)
