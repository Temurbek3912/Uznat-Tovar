"""app/services/barcode.py — проверка/нормализация EAN и чтение кода с кадра. Выполняется офлайн (numpy + OpenCV).

Кадры СИНТЕТИЧЕСКИЕ (код нарисован программой, поверх — шум, размытие, наклон). Это проверяет алгоритм, но НЕ заменяет проверку на
настоящей веб-камере и настоящих упаковках (фокус, блики, глянец, изгиб упаковки)."""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from app.services import barcode as bc

# Реальные по структуре коды (контрольные цифры верны): 13-значный, 8-значный, UPC-A.
EAN13 = "4780001234562"
EAN13_B = "5901234123457"
EAN13_C = "9780201379624"        # ISBN-13 (префикс 978)
EAN8 = "96385074"
UPCA = "036000291452"


def _frame(code, frac=0.4, W=1280, H=720, blur=0.0, noise=0.0, angle=0.0, seed=0, module=4, clutter=12.0):
    rng = np.random.default_rng(seed)
    bars = cv2.cvtColor(bc.render(code, module=module, height=140), cv2.COLOR_GRAY2BGR)
    k = int(W * frac) / bars.shape[1]
    bars = cv2.resize(bars, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
    frame = (150 + rng.normal(0, clutter, (H, W, 1))).clip(0, 255).astype(np.uint8).repeat(3, axis=2)
    h, w = bars.shape[:2]
    y0, x0 = (H - h) // 2, (W - w) // 2
    frame[y0:y0 + h, x0:x0 + w] = bars
    if angle:
        frame = cv2.warpAffine(frame, cv2.getRotationMatrix2D((W / 2, H / 2), angle, 1.0), (W, H), borderValue=(150, 150, 150))
    if blur:
        frame = cv2.GaussianBlur(frame, (0, 0), blur)
    if noise:
        frame = (frame + rng.normal(0, noise, frame.shape)).clip(0, 255).astype(np.uint8)
    return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)


# ------------------------------------------------------------------ проверка и нормализация

def test_check_digit_matches_known_codes():
    assert bc.check_digit("478000123456") == "2" and bc.check_digit("590123412345") == "7" and bc.check_digit("9638507") == "4"
    assert bc.check_digit("03600029145") == "2" and bc.check_digit("978020137962") == "4"


def test_normalize_accepts_valid_codes_in_common_writings():
    assert bc.normalize_code(EAN13) == EAN13
    assert bc.normalize_code("4 780001 234562") == EAN13 and bc.normalize_code("4780001-234562") == EAN13 and bc.normalize_code(" 4780001234562\n") == EAN13
    assert bc.normalize_code(4780001234562) == EAN13
    assert bc.normalize_code(EAN8) == EAN8
    assert bc.normalize_code(UPCA) == "0" + UPCA                      # UPC-A хранится как EAN-13 с нулём — так же находится при любом написании
    assert bc.normalize_code("0" + UPCA) == "0" + UPCA


def test_normalize_rejects_typos_wrong_lengths_and_junk():
    for bad in ("4780001234563", "4780001234561", "478000123456", "47800012345620", "123", "", "   ", None, True, "abcdefghijklm", "4780001234 56x",
                "4780001234.62", 12.5, "0000000000000x"):
        assert bc.normalize_code(bad) is None, repr(bad)
    assert bc.normalize_code("00000000") == "00000000"               # контрольная сумма нулей сходится — формально верный EAN-8


def test_every_single_digit_typo_is_caught_by_the_check_digit():
    for position in range(13):
        for wrong in "0123456789":
            if wrong == EAN13[position]:
                continue
            typo = EAN13[:position] + wrong + EAN13[position + 1:]
            assert bc.normalize_code(typo) is None, typo


def test_explanations_are_plain_and_distinguish_the_three_mistakes():
    assert "только цифры" in bc.explain_invalid("47800x1234562")
    assert "цифр" in bc.explain_invalid("47800012345") and "11" in bc.explain_invalid("47800012345")        # 11 цифр: не хватает одной
    assert "Контрольная цифра" in bc.explain_invalid("4780001234563") and "опечатка" in bc.explain_invalid("4780001234563")
    assert len({bc.explain_invalid(x) for x in ("abc", "123", "4780001234563")}) == 3
    assert "Контрольная цифра" in bc.explain_invalid("478000123456")                                      # 12 цифр — допустимая длина (UPC-A), но сумма не сходится


def test_describe_type():
    assert bc.describe_type(EAN13) == "EAN-13" and bc.describe_type(EAN8) == "EAN-8"


def test_renderer_refuses_invalid_codes_and_draws_the_standard_structure():
    with pytest.raises(ValueError):
        bc.encode_bits("4780001234563")
    bits = bc.encode_bits(EAN13)
    assert len(bits) == 95 and bits.startswith("101") and bits.endswith("101") and bits[45:50] == "01010"
    assert len(bc.encode_bits(EAN8)) == 67
    image = bc.render(EAN13, module=3, height=50, quiet=10)
    assert image.shape == (50, (95 + 20) * 3) and image.dtype == np.uint8 and set(np.unique(image)) == {0, 255}
    assert (image[:, :30] == 255).all()                              # тихая зона слева


# ------------------------------------------------------------------ чтение кадров

def test_reads_clean_codes_of_every_kind_including_upca_as_ean13():
    for code, expected in ((EAN13, EAN13), (EAN13_B, EAN13_B), (EAN13_C, EAN13_C), (EAN8, EAN8), (UPCA, "0" + UPCA)):
        got = bc.decode_image(_frame(code, frac=0.45))
        assert got and got[0] == expected, (code, got)


def test_reads_through_noise_blur_tilt_and_different_sizes():
    cases = [dict(frac=0.25), dict(frac=0.85), dict(frac=0.4, noise=6.0), dict(frac=0.4, blur=1.0), dict(frac=0.45, angle=6.0), dict(frac=0.45, angle=-12.0),
             dict(frac=0.3, blur=1.0, noise=6.0, angle=8.0)]
    for i, kw in enumerate(cases):
        got = bc.decode_image(_frame(EAN13_B, seed=i, **kw))
        assert got and got[0] == EAN13_B, (kw, got)


def test_reads_a_vertically_held_code():
    frame = _frame(EAN13, frac=0.5)
    turned = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    assert bc.decode_image(turned)[:1] == [EAN13]


def test_when_two_codes_are_visible_one_valid_code_is_returned_never_a_mix():
    top, bottom = _frame(EAN13, frac=0.4, H=360, seed=1), _frame(EAN13_B, frac=0.4, H=360, seed=2)
    got = bc.decode_image(np.concatenate([top, bottom], axis=0))
    assert got and got[0] in {EAN13, EAN13_B} and all(bc.normalize_code(c) == c for c in got)


def test_empty_noise_and_text_like_stripes_do_not_produce_codes():
    rng = np.random.default_rng(5)
    for frame in ((150 + rng.normal(0, 12, (720, 1280, 3))).clip(0, 255).astype(np.uint8),
                  np.full((720, 1280, 3), 127, np.uint8),
                  np.tile(np.array([0, 255], np.uint8).repeat(3), 600)[:1280][None, :, None].repeat(720, 0).repeat(3, 2)):
        assert bc.decode_image(frame) == []


def test_a_damaged_code_is_never_misread_as_another_code():
    # Портим полосы кода (стираем часть): допустимо «не прочитал», недопустимо «прочитал другой».
    rng = np.random.default_rng(11)
    for trial in range(12):
        frame = _frame(EAN13_B, frac=0.5, seed=trial)
        h, w = frame.shape[:2]
        x = int(rng.integers(int(w * 0.28), int(w * 0.62)))
        frame[:, x:x + int(rng.integers(6, 40))] = 150
        got = bc.decode_image(frame)
        assert got == [] or got == [EAN13_B], (trial, got)


def test_reading_works_on_grayscale_input_and_small_frames():
    gray = cv2.cvtColor(_frame(EAN13, frac=0.5), cv2.COLOR_RGB2GRAY)
    assert bc.decode_image(gray)[:1] == [EAN13]
    small = cv2.resize(_frame(EAN13, frac=0.7, W=640, H=360, module=6), (640, 360))
    assert bc.decode_image(small)[:1] == [EAN13]


def test_opencv_fallback_alone_returns_only_valid_13_digit_codes():
    gray = cv2.cvtColor(_frame(EAN13, frac=0.25, blur=1.0), cv2.COLOR_RGB2GRAY)
    got = bc.decode_opencv(gray)
    assert all(len(c) == 13 and bc.normalize_code(c) == c for c in got)


def test_an_8_digit_fragment_of_a_13_digit_code_is_not_accepted_from_opencv():
    # Реальный случай из проверки: OpenCV выдал «62894173» (с верной контрольной цифрой!) вместо «0628949738312».
    assert bc.normalize_code("62894173") == "62894173"                 # формально это верный EAN-8 ...
    assert bc.accept_opencv_code("62894173") is None                   # ... но от OpenCV 8 цифр не принимаем
    assert bc.accept_opencv_code("24115827") is None and bc.accept_opencv_code("2247276238869") == "2247276238869"
    assert bc.accept_opencv_code(UPCA) == "0" + UPCA and bc.accept_opencv_code("4780001234563") is None and bc.accept_opencv_code(None) is None


def _line_image(code, rows_with_code, height=200, width=640, module=4):
    """Серая картинка, где штрих-код нарисован ТОЛЬКО в указанных строках (остальное — ровный серый фон)."""
    row = bc.render(code, module=module, height=1)[0]
    image = np.full((height, width), 150, np.uint8)
    x0 = (width - len(row)) // 2
    for y in rows_with_code:
        image[y, x0:x0 + len(row)] = row
    return image


def test_ean13_is_accepted_from_a_single_row_but_ean8_needs_two_different_rows():
    h = 200
    first, last = int(h * 0.08), int(h * 0.92)                       # какие строки возьмёт rows=2
    kw = dict(angles=(0,), scales=(1.0,))
    assert bc.decode_scanlines(_line_image(EAN13, [first]), rows=2, **kw) == [EAN13]          # EAN-13: хватает одной строки
    assert bc.decode_scanlines(_line_image(EAN8, [first]), rows=2, **kw) == []                # EAN-8 из одной строки — не принимаем
    assert bc.decode_scanlines(_line_image(EAN8, [first, last]), rows=2, **kw) == [EAN8]      # две разные строки согласны — принимаем
    assert bc.decode_scanlines(_line_image(EAN8, [first]), rows=1, **kw) == []                # два сглаживания одной строки — это НЕ согласие строк


# ------------------------------------------------------------------ привязка кодов

def test_plan_links_separates_free_own_and_foreign_codes():
    owners = {EAN13: ("p-cola", "Кола"), EAN13_B: ("p-fanta", "Фанта")}
    plan = bc.plan_links([EAN13, EAN13_B, EAN13_C, "garbage", EAN13_C, "4 780001 234562"], owners, "p-cola")
    assert plan.already == [EAN13] and plan.to_add == [EAN13_C] and plan.conflicts == [(EAN13_B, "p-fanta", "Фанта")]
    brand_new = bc.plan_links([EAN13, EAN13_C], owners, None)                 # товар ещё не создан: занятый код — всегда конфликт
    assert brand_new.to_add == [EAN13_C] and brand_new.already == [] and brand_new.conflicts == [(EAN13, "p-cola", "Кола")]
    assert bc.plan_links([], owners, "x") == bc.LinkPlan()
    upc = bc.plan_links([UPCA], {"0" + UPCA: ("p-1", "Печенье")}, "p-2")           # UPC-A и EAN-13 с нулём — один и тот же код
    assert upc.conflicts == [("0" + UPCA, "p-1", "Печенье")]


# --------------------------------------------------------------------------- поиск по штрихкоду (этап 5)

def test_search_keys_understand_spaces_gtin14_upc_and_padded_ean8():
    assert bc.search_keys("4780001234562") == ["4780001234562"]
    assert bc.search_keys("4 780001-234562") == ["4780001234562"]
    assert bc.search_keys("04780001234562") == ["4780001234562"]             # GTIN-14 со сканера / из учётной программы
    assert bc.search_keys("036000291452") == ["0036000291452"]               # UPC-A хранится как EAN-13 с нулём
    assert bc.search_keys("0036000291452") == ["0036000291452"]
    assert "12345670" in bc.search_keys("0000012345670")                     # EAN-8, дополненный нулями до 13
    assert bc.search_keys("12345670") == ["12345670"]


def test_search_keys_reject_typos_text_and_too_long_input():
    assert bc.search_keys("4780001234563") == []                             # контрольная цифра не сходится
    assert bc.search_keys("кола") == [] and bc.search_keys("") == [] and bc.search_keys(None) == []
    assert bc.search_keys("1" * 15) == []


def test_partial_digits_need_at_least_four_digits():
    assert bc.partial_digits("2345") == "2345" and bc.partial_digits(" 23-45 ") == "2345"
    assert bc.partial_digits("234") is None and bc.partial_digits("кола 2345") is None
