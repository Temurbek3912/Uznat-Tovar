"""
Штрих-коды товаров (EAN-13, EAN-8, UPC-A): проверка, нормализация и чтение с кадра камеры. Чистый модуль: нужны только numpy и
OpenCV (уже в проекте); без FastAPI/SQLAlchemy — проверяется офлайн (tests/test_barcode.py).

Зачем свой читатель, а не только OpenCV. cv2.barcode на проверке читал лишь ~60% аккуратно сгенерированных кадров (крупные чёткие коды
иногда не находит вовсе), хотя неверных кодов не выдаёт. Поэтому основной путь — собственный построчный декодер (тот же принцип,
что у ZXing: границы полос с субпиксельной точностью, сопоставление ширин с образцами L/G/R, контрольная цифра), а OpenCV — запасной.
ЛЮБОЙ ответ проходит контрольную цифру: случайная ошибка чтения не станет «другим товаром» (в проверке — 0 неверных кодов).

Что НЕ читается: Code 128/Code 39/QR/DataMatrix (логистические этикетки), коды без контрастных полос, смазанные и не в фокусе.
Это честное ограничение: на настоящей веб-камере качество зависит от фокуса и света — проверьте на своих упаковках.

Нормализация: UPC-A (12 цифр) хранится как EAN-13 с нулём впереди — так один и тот же код находится при любом способе записи;
EAN-8 хранится как есть (8 цифр).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping

import numpy as np

_L = ["0001101", "0011001", "0010011", "0111101", "0100011", "0110001", "0101111", "0111011", "0110111", "0001011"]
_G = ["0100111", "0110011", "0011011", "0100001", "0011101", "0111001", "0000101", "0010001", "0001001", "0010111"]
_R = ["1110010", "1100110", "1101100", "1000010", "1011100", "1001110", "1010000", "1000100", "1001000", "1110100"]
_PARITY = ["LLLLLL", "LLGLGG", "LLGGLG", "LLGGGL", "LGLLGG", "LGGLLG", "LGGGLL", "LGLGLG", "LGLGGL", "LGGLGL"]


def _runs(bits: str) -> tuple[int, ...]:
    out, prev, n = [], bits[0], 0
    for ch in bits:
        if ch == prev:
            n += 1
        else:
            out.append(n)
            prev, n = ch, 1
    out.append(n)
    return tuple(out)


# Образцы ширин полос/пробелов (4 числа на цифру, в сумме 7 модулей). Левые цифры начинаются с пробела, правые — с полосы:
# после защитных шаблонов чередование цветов задаёт, что идёт следом, поэтому достаточно сравнивать только числа.
_PATTERNS_LEFT = [(d, "L", np.array(_runs(_L[d]), float)) for d in range(10)] + [(d, "G", np.array(_runs(_G[d]), float)) for d in range(10)]
_PATTERNS_RIGHT = [(d, "R", np.array(_runs(_R[d]), float)) for d in range(10)]


# --------------------------------------------------------------------------- проверка и нормализация

def check_digit(body: str) -> str:
    """Контрольная цифра EAN/UPC по телу без неё (веса 3 и 1, считая с правого края)."""
    total = sum(int(ch) * (3 if i % 2 == 0 else 1) for i, ch in enumerate(reversed(body)))
    return str((10 - total % 10) % 10)


def normalize_code(raw: object) -> str | None:
    """«4780001234562» / «4 780001 234562» / UPC-A -> каноническая строка, или None, если это не верный EAN-13/UPC-A/EAN-8."""
    if raw is None or isinstance(raw, bool):
        return None
    digits = re.sub(r"[\s\-]", "", str(raw))
    if not digits.isdigit() or len(digits) not in (8, 12, 13):
        return None
    if check_digit(digits[:-1]) != digits[-1]:
        return None
    return "0" + digits if len(digits) == 12 else digits


def explain_invalid(raw: object) -> str:
    """Понятное человеку объяснение, почему введённый штрих-код не принят."""
    digits = re.sub(r"[\s\-]", "", "" if raw is None else str(raw))
    if not digits.isdigit():
        return "Штрих-код — это только цифры, например 4780001234562."
    if len(digits) not in (8, 12, 13):
        return f"В штрих-коде {len(digits)} цифр, а нужно 13 (бывает 8 или 12). Проверьте, все ли цифры переписаны."
    return "Контрольная цифра не сходится — вероятно, опечатка в одной из цифр. Проверьте код по упаковке."


def describe_type(code: str) -> str:
    return "EAN-8" if len(code) == 8 else "EAN-13"


# --------------------------------------------------------------------------- привязка кодов к товарам (чистая логика)

@dataclass
class LinkPlan:
    to_add: list[str] = field(default_factory=list)                           # свободные коды: можно привязать
    already: list[str] = field(default_factory=list)                          # уже привязаны к ЭТОМУ товару
    conflicts: list[tuple[str, str, str]] = field(default_factory=list)       # (код, id владельца, название владельца)


def plan_links(codes: Iterable[str], owners: Mapping[str, tuple[str, str]], product_id: str | None) -> LinkPlan:
    """Что делать с кодами: owners — {код: (id товара, название)} для уже привязанных кодов. Код уникален во всей системе: один
    штрих-код не может вести на два товара. product_id=None — товар ещё не создан (любой занятый код — конфликт)."""
    plan, seen = LinkPlan(), set()
    for raw in codes:
        code = normalize_code(raw)
        if code is None or code in seen:
            continue
        seen.add(code)
        owner = owners.get(code)
        if owner is None:
            plan.to_add.append(code)
        elif product_id is not None and owner[0] == product_id:
            plan.already.append(code)
        else:
            plan.conflicts.append((code, owner[0], owner[1]))
    return plan


# --------------------------------------------------------------------------- рисование (для тестов и демо)

def encode_bits(code: str) -> str:
    """Строка из 0/1 (1 = полоса) для EAN-13 или EAN-8. Нужна тестам и демо-видео; в работе системы не используется."""
    normalized = normalize_code(code)
    if normalized is None:
        raise ValueError(f"неверный штрих-код: {code!r}")
    if len(normalized) == 8:
        return "101" + "".join(_L[int(c)] for c in normalized[:4]) + "01010" + "".join(_R[int(c)] for c in normalized[4:]) + "101"
    parity = _PARITY[int(normalized[0])]
    left = "".join((_L if parity[i] == "L" else _G)[int(c)] for i, c in enumerate(normalized[1:7]))
    return "101" + left + "01010" + "".join(_R[int(c)] for c in normalized[7:]) + "101"


def render(code: str, module: int = 3, height: int = 120, quiet: int = 10) -> np.ndarray:
    """Серое изображение штрих-кода (uint8, 255 — фон, 0 — полоса) с полями слева и справа по `quiet` модулей."""
    bits = encode_bits(code)
    row = np.full((len(bits) + 2 * quiet) * module, 255, np.uint8)
    for i, bit in enumerate(bits):
        if bit == "1":
            row[(quiet + i) * module:(quiet + i + 1) * module] = 0
    return np.tile(row, (height, 1))


# --------------------------------------------------------------------------- построчный декодер

_MAX_DIGIT_ERROR = 0.30      # допустимое среднее отклонение нормированных ширин одной цифры (в модулях)
_MAX_GUARD_SPREAD = 0.45     # допустимый разброс ширин в защитном шаблоне относительно среднего


def _crossings(row: np.ndarray, window: int, sigma: float) -> tuple[np.ndarray, bool]:
    """Подпиксельные позиции переходов тёмное<->светлое в строке (порог — скользящее среднее). -> (позиции, первый интервал тёмный?)."""
    if sigma > 0:
        radius = int(3 * sigma) + 1
        kernel = np.exp(-0.5 * (np.arange(-radius, radius + 1) / sigma) ** 2)
        row = np.convolve(np.pad(row, radius, mode="edge"), kernel / kernel.sum(), mode="valid")
    csum = np.cumsum(np.insert(row, 0, 0.0))
    half = max(2, window // 2)
    idx = np.arange(len(row))
    lo, hi = np.clip(idx - half, 0, len(row)), np.clip(idx + half + 1, 0, len(row))
    signal = row - (csum[hi] - csum[lo]) / (hi - lo)
    light = signal > 0
    change = np.nonzero(light[1:] != light[:-1])[0]
    if len(change) == 0:
        return np.array([]), False
    a, b = signal[change], signal[change + 1]
    return change + a / (a - b), not bool(light[change[0] + 1])


_PAT_LEFT = np.array([p[2] for p in _PATTERNS_LEFT])          # (20, 4): 10 цифр в кодировке L, затем 10 в кодировке G
_PAT_RIGHT = np.array([p[2] for p in _PATTERNS_RIGHT])        # (10, 4)


def _match_digits(segment: np.ndarray, patterns: np.ndarray):
    """segment (n, 4) ширин -> (индексы лучших образцов, подходят ли все достаточно точно). Нормируем каждую цифру на 7 модулей."""
    totals = segment.sum(axis=1, keepdims=True)
    if (totals <= 0).any():
        return None, False
    norm = segment * (7.0 / totals)
    errors = np.abs(norm[:, None, :] - patterns[None, :, :]).mean(axis=2)
    best = errors.argmin(axis=1)
    return best, bool((errors[np.arange(len(best)), best] <= _MAX_DIGIT_ERROR).all())


def _candidate_starts(widths: np.ndarray, first_dark: int, per_side: int) -> np.ndarray:
    """Все места, где защитные шаблоны (начало, середина, конец) выглядят как у EAN: полосы почти равной ширины (проверка векторами)."""
    total_runs = 11 + 8 * per_side
    starts = np.arange(first_dark, len(widths) - total_runs + 1, 2)         # стартовая защитная полоса — всегда тёмная
    if len(starts) == 0:
        return starts
    mean = (widths[starts] + widths[starts + 1] + widths[starts + 2]) / 3.0
    ok = mean > 0
    for k in (0, 1, 2):
        ok &= np.abs(widths[starts + k] - mean) <= _MAX_GUARD_SPREAD * mean
    middle = 3 + 4 * per_side
    for k in range(5):
        ok &= np.abs(widths[starts + middle + k] - mean) <= 1.3 * _MAX_GUARD_SPREAD * mean
    last = 8 + 8 * per_side
    for k in range(3):
        ok &= np.abs(widths[starts + last + k] - mean) <= _MAX_GUARD_SPREAD * mean
    return starts[ok]


def _read_ean(widths: np.ndarray, start: int, per_side: int) -> str | None:
    """Читает код с серии ширин widths[start:] (widths[start] — первая полоса стартового шаблона). None — не похоже или не сошлась контрольная цифра."""
    left = widths[start + 3:start + 3 + 4 * per_side].reshape(per_side, 4)
    right = widths[start + 8 + 4 * per_side:start + 8 + 8 * per_side].reshape(per_side, 4)
    best_left, ok_left = _match_digits(left, _PAT_LEFT if per_side == 6 else _PAT_LEFT[:10])
    if not ok_left:
        return None
    best_right, ok_right = _match_digits(right, _PAT_RIGHT)
    if not ok_right:
        return None
    digits_left = "".join(str(int(i) % 10) for i in best_left)
    digits_right = "".join(str(int(i)) for i in best_right)
    if per_side == 4:
        return normalize_code(digits_left + digits_right)
    kinds = "".join("L" if int(i) < 10 else "G" for i in best_left)
    if kinds not in _PARITY:
        return None
    return normalize_code(str(_PARITY.index(kinds)) + digits_left + digits_right)


def _decode_row(row: np.ndarray, sigma: float) -> list[str]:
    window = max(15, len(row) // 8)
    pos, first_dark_interval = _crossings(row.astype(np.float64), window, sigma)
    if len(pos) < 44:
        return []
    widths = np.diff(pos)                       # widths[i] — i-й интервал (полоса или пробел), цвета чередуются
    first_dark = 0 if first_dark_interval else 1
    found: list[str] = []
    for per_side in (6, 4):
        for start in _candidate_starts(widths, first_dark, per_side):
            code = _read_ean(widths, int(start), per_side)
            if code and code not in found:
                found.append(code)
    return found


def _rotate(gray: np.ndarray, angle: float) -> np.ndarray:
    import cv2

    if angle == 0:
        return gray
    h, w = gray.shape
    if angle in (90, -90, 270):
        return cv2.rotate(gray, cv2.ROTATE_90_CLOCKWISE if angle == 90 else cv2.ROTATE_90_COUNTERCLOCKWISE)
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    cos, sin = abs(matrix[0, 0]), abs(matrix[0, 1])
    nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
    matrix[0, 2] += nw / 2 - w / 2
    matrix[1, 2] += nh / 2 - h / 2
    border = int(np.median(gray))
    return cv2.warpAffine(gray, matrix, (nw, nh), flags=cv2.INTER_LINEAR, borderValue=border)


def decode_scanlines(gray: np.ndarray, *, rows: int = 40, angles=(0, -7, 7, -14, 14, 90), scales=(1.0, 0.5), stop_at_first: bool = True) -> list[str]:
    """Собственный декодер: сканирует горизонтальные линии по кадру (при нескольких наклонах и масштабах).
    EAN-13 принимается с одной строки (контрольная цифра + строгое совпадение каждой цифры); EAN-8 короче и потому легче
    «найти» в обрывке чужого кода — его принимаем только если две разные строки прочитали ровно то же самое."""
    import cv2

    counts: dict[str, int] = {}
    found: list[str] = []
    for scale in scales:
        base = gray if scale == 1.0 else cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        if min(base.shape) < 40:
            continue
        for angle in angles:
            image = _rotate(base, angle)
            h = image.shape[0]
            for y in np.unique(np.linspace(h * 0.08, h * 0.92, rows).astype(int)):
                in_row: list[str] = []
                for sigma in (0.8, 2.0):
                    for code in _decode_row(image[y], sigma):
                        if code not in in_row:
                            in_row.append(code)
                for code in in_row:                               # «согласие строк» считаем по РАЗНЫМ строкам, а не по двум сглаживаниям одной
                    counts[code] = counts.get(code, 0) + 1
                    if (len(code) == 13 or counts[code] >= 2) and code not in found:
                        found.append(code)
                        if stop_at_first:
                            return found
    return found


def accept_opencv_code(raw: object) -> str | None:
    """OpenCV иногда выдаёт 8-значный «код» из обрывка настоящего 13-значного (проверено: «62894173» вместо «0628949738312»), а
    контрольная цифра пропускает такую подделку примерно в 10% случаев. Поэтому от OpenCV принимаем только 13 цифр."""
    code = normalize_code(raw)
    return code if code is not None and len(code) == 13 else None


def decode_opencv(gray: np.ndarray) -> list[str]:
    """Запасной путь: cv2.barcode (есть в OpenCV >= 4.8). Только EAN-13/UPC-A и только с верной контрольной цифрой (см. accept_opencv_code)."""
    import cv2

    if not hasattr(cv2, "barcode"):
        return []
    detector = cv2.barcode.BarcodeDetector()
    found: list[str] = []
    h, w = gray.shape
    for scale in (1.0, 0.75, 0.5, 0.35):
        image = gray if scale == 1.0 else cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        try:
            ok, info, _types, _pts = detector.detectAndDecodeWithType(image)
        except cv2.error:
            continue
        if ok:
            for raw in info:
                code = accept_opencv_code(raw)
                if code and code not in found:
                    found.append(code)
        if found:
            break
    return found


def decode_image(rgb: np.ndarray) -> list[str]:
    """Кадр (RGB uint8) -> найденные верные штрих-коды (нормализованные), самый вероятный первым. Пусто — кода не видно/не читается."""
    import cv2

    if rgb.ndim == 3:
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    else:
        gray = rgb
    codes = decode_scanlines(gray)
    if codes:
        return codes
    return decode_opencv(gray)
