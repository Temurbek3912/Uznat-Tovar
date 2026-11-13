"""Картинки и «видео с камеры» для tools/ui_screenshots/e2e.js (только для разработки): python tools/e2e_assets.py <папка>.

Три «товара» — цветные карточки (красная, зелёная, синяя) по 3 крупных фото с шумом (чтобы не считались размытыми), «камера» —
y4m-видео 640×480, на котором по кругу: пустая серая стена -> красная карточка -> стена -> зелёная -> стена -> синяя -> стена ->
штрих-код колы -> стена -> штрих-код, которого нет в каталоге. Кроме того: «обратные» фото упаковок со штрих-кодом и фото одного
штрих-кода на белом фоне. Настоящие упаковки и настоящий шум веб-камеры это не заменяет."""
from __future__ import annotations

import io
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.services import barcode as bc  # noqa: E402

CARDS = {"cola": (210, 35, 35), "lime": (35, 200, 35), "sky": (35, 35, 210)}
GRAY = (127, 127, 127)
CODES = {"cola": "4780001234562", "lime": "5901234123457", "sky": "4607000000014", "unknown": "9780201379624"}   # unknown — в каталог не добавляется
assert all(bc.normalize_code(c) == c for c in CODES.values())


def barcode_on(background_rgb, code, width, height, frac=0.62, label=True):
    """Картинка width×height: фон цвета background_rgb, по центру белая этикетка со штрих-кодом (RGB uint8)."""
    img = np.zeros((height, width, 3), np.uint8)
    img[:] = background_rgb
    bars = cv2.cvtColor(bc.render(code, module=4, height=160), cv2.COLOR_GRAY2RGB)
    k = int(width * frac) / bars.shape[1]
    bars = cv2.resize(bars, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
    h, w = bars.shape[:2]
    y0, x0 = (height - h) // 2, (width - w) // 2
    img[y0:y0 + h, x0:x0 + w] = bars
    return img


def jpeg_bytes(rgb, quality=92) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def card_photo(rgb, seed, w=3000, h=2000) -> bytes:
    rng = np.random.default_rng(seed)
    base = np.zeros((h, w, 3), np.float32) + np.array(rgb, np.float32)
    base += rng.normal(0, 14, (h, w, 1))                       # шум яркости: фото «резкое», цвет в среднем тот же
    base[h // 4: h // 2, w // 4: w // 2] *= 0.82               # «этикетка» — тёмный прямоугольник (красит в тот же оттенок)
    img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def yuv420(rgb, w, h):
    r, g, b = rgb
    y = int(0.257 * r + 0.504 * g + 0.098 * b + 16)
    u = int(-0.148 * r - 0.291 * g + 0.439 * b + 128)
    v = int(0.439 * r - 0.368 * g - 0.071 * b + 128)
    return bytes([y]) * (w * h) + bytes([u]) * (w * h // 4) + bytes([v]) * (w * h // 4)


def yuv420_from_rgb(img):
    """Кадр RGB uint8 (h, w, 3) -> байты YUV420 (для y4m)."""
    yuv = cv2.cvtColor(img, cv2.COLOR_RGB2YUV_I420)
    return yuv.tobytes()


def write_y4m(path: Path, seconds_per_scene=4, fps=2, w=640, h=480) -> None:
    flat = lambda rgb: np.full((h, w, 3), rgb, np.uint8)
    scenes = [flat(GRAY), flat(CARDS["cola"]), flat(GRAY), flat(CARDS["lime"]), flat(GRAY), flat(CARDS["sky"]), flat(GRAY),
              barcode_on((255, 255, 255), CODES["cola"], w, h), flat(GRAY), barcode_on((255, 255, 255), CODES["unknown"], w, h)]
    with open(path, "wb") as f:
        f.write(f"YUV4MPEG2 W{w} H{h} F{fps}:1 Ip A1:1 C420jpeg\n".encode())
        for img in scenes:
            frame = b"FRAME\n" + yuv420_from_rgb(img)
            for _ in range(seconds_per_scene * fps):
                f.write(frame)


def make(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    seed = 1
    for name, rgb in CARDS.items():
        for i in range(3):
            (folder / f"{name}-{i + 1}.jpg").write_bytes(card_photo(rgb, seed))
            seed += 1
    # «обратная» сторона упаковки: цвет товара + белая этикетка со штрих-кодом (этикетка мала — цвет в среднем тот же)
    (folder / "lime-back.jpg").write_bytes(jpeg_bytes(barcode_on(CARDS["lime"], CODES["lime"], 3000, 2000, frac=0.30)))
    (folder / "sky-with-code.jpg").write_bytes(jpeg_bytes(barcode_on(CARDS["sky"], CODES["sky"], 3000, 2000, frac=0.30)))
    for name in ("cola", "lime", "sky", "unknown"):                                    # просто штрих-код на белом фоне
        (folder / f"barcode-{name}.jpg").write_bytes(jpeg_bytes(barcode_on((255, 255, 255), CODES[name], 1280, 720)))
    (folder / "not-a-photo.txt").write_bytes(b"this is not an image")
    flat = io.BytesIO()
    Image.new("RGB", (800, 600), GRAY).save(flat, "JPEG")
    (folder / "flat.jpg").write_bytes(flat.getvalue())
    write_y4m(folder / "camera.y4m")


if __name__ == "__main__":
    make(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/e2e-assets"))
    print("assets ready")
