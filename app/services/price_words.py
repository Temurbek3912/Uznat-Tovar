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

# «подешевле», «подорожала» и английские слова — замер 2026-11-28 (ml/assistant_owner_questions.py): «сделай сникерс подешевле на
# 1000» и «Raise the price of Fanta by 500» давали карточку «цена 1000» / «цена 500».
_UP = ("podnim", "podnya", "povys", "uvelich", "dobav", "pribav", "nakin", "dorozh", "podorozh", "bolshe",
       "oshir", "kotar", "qosh", "qimmat", "raise", "increase", "hike")
_DOWN = ("sniz", "opust", "umensh", "poniz", "sbav", "skin", "ubav", "deshev", "podeshev", "udeshev", "menshe", "skidk",
         "tushir", "kamaytir", "pasaytir", "arzon", "chegirma", "lower", "reduce", "decrease", "discount", "cheaper")
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


# Вопрос, а не изменение (§10.16). Живая qwen3-vl-4b (2026-11-17) на «Nutella qancha turadi?», «Fanta narxi qancha?» и даже
# «Сколько стоит Нутелла?» звала find_products → product_details, а третьим шагом — propose_update(in_stock=true): карточка
# «в наличии» вместо цены (7 из 7 по-узбекски). Поэтому что это вопрос, решает программа по словам владельца.
_ASK = re.compile(r"\b(?:qancha|necha|nechta|nechi|skolko|kakaya|kakoy|kakie|kakov\w*|pochem|pochom|how\s+much|how\s+many|"
                  r"what)\b")
_PRICE = re.compile(r"\b(?:narh\w*|qancha|necha\s*pul|sto(?:it|yat|imost)\w*|pochem|pochom|tsen\w*|how\s+much|price\w*|"
                    r"cost\w*)")
# Глагол изменения (и «в архив», «удалить», «новый товар»): есть он — это просьба изменить, хоть и со словом «сколько».
_CHANGE = ("qil", "qoy", "ozgartir", "almashtir", "otkaz", "belgila", "yoz", "ochir", "olib", "arhiv", "qosh", "oshir",
           "kotar", "tushir", "kamaytir", "pasaytir", "arzonlashtir", "qimmatlashtir", "yangila",
           "postav", "sdela", "izmen", "smen", "pomen", "zamen", "ustanov", "ukazh", "ukaz", "napish", "naznach", "zada",
           "ispra", "uber", "ubra", "udal", "dobav", "podnim", "povys", "sniz", "umensh", "verni", "vern",
           "set", "change", "make", "raise", "lower", "remove", "delete", "add", "archive", "put",
           "move", "rename", "increase", "decrease", "reduce", "discount", "give", "mark", "restore", "unarchive", "create",
           "update", "assign")


def asks_only(said: str) -> bool:
    """Последняя реплика владельца — вопрос («Nutella qancha turadi?», «Сколько стоит Нутелла?», «Fanta narxi qancha?»,
    «Цена Фанты?») без глагола изменения: предлагать изменения тут нечего."""
    text = latin(said or "")
    if not (_ASK.search(text) or (_PRICE.search(text) and "?" in text)):
        return False
    return not any(w.startswith(_CHANGE) for w in re.findall(r"[a-z]+", text))


def asks_price(said: str) -> bool:
    """Вопрос о цене: найден один товар — его цена уже в данных, дальше модель только отвечает."""
    return asks_only(said) and bool(_PRICE.search(latin(said or "")))
