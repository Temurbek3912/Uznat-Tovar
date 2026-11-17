"""
Что владелец сказал о цене: «поднять на 1000», «поставить 1000» или «на 10 %» (2026-11-17).

Зачем. Живая qwen3-vl-4b на «Gazli ichimliklar narxini 1000 soʻmga oshiring» («поднимите на 1000») и на «Подними цену Fanta
на 1000» выбирала price_set — карточка показывала 9000 → 1000. Подсказку модели удлинять нельзя (движок держит 4096 токенов),
поэтому вид изменения по словам владельца определяет программа, а модель называет только товары и число.

Как. Берётся последняя реплика владельца (латиницей, как category_words.latin) и число, которое назвала модель:
- после числа «%», «foiz», «процент» — изменение на процент;
- «до 10000», «10000 soʻmgacha» — новая цена;
- иначе есть слово «поднять / снизить» (oshiring, koʻtaring, tushiring, kamaytiring, дороже, скидка…) — на сумму, знак по слову;
- иначе («сделай цену 8000», «8000 qiling») — новая цена.
Если числа модели в словах владельца нет или слова противоречат друг другу — ничего не решаем (как выбрала модель).
"""
from __future__ import annotations

import re

from app.services.category_words import latin

_UP = ("podnim", "podnya", "povys", "uvelich", "dobav", "pribav", "nakin", "dorozh", "bolshe",
       "oshir", "kotar", "qosh", "qimmat")
_DOWN = ("sniz", "opust", "umensh", "poniz", "sbav", "skin", "ubav", "deshev", "menshe", "skidk",
         "tushir", "kamaytir", "pasaytir", "arzon", "chegirma")
_PERCENT = re.compile(r"\s*(?:%|foiz|protsent|prots)")
_THOUSAND = re.compile(r"\s*(?:tys|ming|k\b)")
_UPTO_AFTER = re.compile(r"\s*(?:so[mʻ]?m|sum|sym|rubl\w*)?\s*gacha")
_UPTO_BEFORE = re.compile(r"\bdo\s*$")


def _direction(text: str) -> int:
    words = re.findall(r"[a-z]+", text)
    up = any(w.startswith(_UP) for w in words) or "kop qil" in text
    down = any(w.startswith(_DOWN) for w in words)
    return 1 if up and not down else -1 if down and not up else 0


def _mentions(text: str, value: float) -> list[tuple[bool, bool]]:
    """Где в тексте это число: [(на процент?, «до»?)]."""
    out = []
    for match in re.finditer(r"\d+(?:[.,]\d+)?", text):
        number = float(match.group(0).replace(",", "."))
        if _THOUSAND.match(text, match.end()):
            number *= 1000
        if number != value:
            continue
        out.append((bool(_PERCENT.match(text, match.end())),
                    bool(_UPTO_AFTER.match(text, match.end()) or _UPTO_BEFORE.search(text[:match.start()]))))
    return out


def meant(said: str, value) -> tuple[str, float] | None:
    """Последняя реплика владельца + число модели → (price_percent | price_add | price_set, число со знаком) или None."""
    try:
        number = abs(float(str(value).replace(" ", "").replace(",", ".").rstrip("%")))
    except ValueError:
        return None
    text = re.sub(r"(?<=\d)[\s  ](?=\d{3}\b)", "", latin(said))
    places = _mentions(text, number)
    if not places or not number:
        return None
    percent, upto = any(p for p, _u in places), any(u for _p, u in places)
    direction = _direction(text)
    if percent:
        return ("price_percent", direction * number) if direction else None
    if upto:
        return "price_set", number
    if direction:
        return "price_add", direction * number
    return "price_set", number
