"""
Чистка названий и брендов из открытых баз (Open Food / Beauty / Products Facts) при загрузке.

Покупатели пишут в открытую базу что попало: состав («Ingredients: …»), калории, цену («3.500 СУМ»), арабские «граммы»,
значки ®, первую букву маленькой. tidy_name / tidy_brand убирают только то, что точно лишнее и ничего не придумывают.
Опечатки, набор букв, длинные описания сами не правятся — name_doubts говорит, что проверить глазами
(список потом применяется scripts/fix_product_names.py после одобрения владельца).
"""
from __future__ import annotations

import re
import unicodedata

ARABIC = r"؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿"
_ARABIC_TOKEN = re.compile(rf"\S*[{ARABIC}]\S*")
_INGREDIENTS = re.compile(r"\b(?:ingredients?|ingr[ée]dients?|zutaten|ingredientes|ingredienti|состав|ингредиенты|tarkibi)\s*:",
                          re.IGNORECASE)
_NUTRITION = re.compile(r"\d+(?:[.,]\d+)?\s*(?:кдж|kj|ккал|kcal)(?!\w)|\b(?:пищевая|энергетическая)\s+ценность|"
                        r"\bnutrition(?:al)?\s+(?:facts|value)|\b(?:белки|жиры|углеводы)\s*:?\s*\d", re.IGNORECASE)
_PRICE = re.compile(r"(?<![\d.,])(?:\d{1,3}(?:[ .,]\d{3})+|\d+)\s*(?:сум|сўм|so['ʻ’`]?m|som|uzs|руб\.?|₽)(?!\w)", re.IGNORECASE)
_NETTO = re.compile(r"\b(?:масса\s+нетто|нетто|net\s*wt\.?|net\s+weight)\s*:?\s*", re.IGNORECASE)
_MARKS = str.maketrans({"®": "", "™": "", "©": "", "℅": "%"})
_CYR = re.compile(r"[а-яёўқғҳ]", re.IGNORECASE)
_LAT = re.compile(r"[a-z]", re.IGNORECASE)

LONG_NAME = 70           # длиннее — скорее описание с упаковки, чем название
NO_BRAND = {"", "не указан", "unknown"}


def _no_pictograms(text: str) -> str:
    """Эмодзи и прочие значки (🍓) — не часть названия."""
    return "".join(ch for ch in text if unicodedata.category(ch) != "So")


def _tidy_punctuation(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;:!?)])", r"\1", text)
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip(" ,;:-–—/|")
    last = text.rsplit(" ", 1)[-1]
    if last.endswith(".") and last[:-1].isalpha() and len(last) <= 5:
        return text                                 # «2 шт.», «& comp.» — точка сокращения остаётся
    return text.rstrip(" .")


def _cut_at(pattern: re.Pattern, text: str) -> str:
    m = pattern.search(text)
    return text[:m.start()] if m else text


def capitalize_first(text: str) -> str:
    """Первая буква — заглавная («ветчина 500 г» -> «Ветчина 500 г»); «iPhone» (вторая заглавная) не трогаем."""
    for i, ch in enumerate(text):
        if ch.isalnum():
            if ch.islower() and not (i + 1 < len(text) and text[i + 1].isupper()):
                return text[:i] + ch.upper() + text[i + 1:]
            return text
    return text


def tidy_name(text: str) -> str:
    """Название из открытой базы без лишнего. Пустая строка — от названия ничего не осталось."""
    text = re.sub(r"\s+", " ", str(text or "")).translate(_MARKS)
    text = _no_pictograms(text)
    text = _cut_at(_INGREDIENTS, text)
    text = _cut_at(_NUTRITION, text)
    text = _PRICE.sub(" ", text)
    text = _ARABIC_TOKEN.sub(" ", text)
    text = _NETTO.sub("", text)
    return capitalize_first(_tidy_punctuation(text))


def tidy_brand(text: str) -> str:
    """Бренд без ®, арабских слов, цены и эмодзи (регистр не меняем: «adidas», «dm» пишут с маленькой)."""
    text = re.sub(r"\s+", " ", str(text or "")).translate(_MARKS)
    text = _no_pictograms(text)
    text = _PRICE.sub(" ", text)
    text = _ARABIC_TOKEN.sub(" ", text)
    return _tidy_punctuation(text)


def name_doubts(name: str, brand: str = "") -> list[str]:
    """Что в названии стоит проверить глазами (само не исправляется). Пусто — на вид нормально."""
    why = []
    words = re.findall(r"[^\W\d_]+", name)
    if any(_CYR.search(w) and _LAT.search(w) for w in words):
        why.append("в слове смешаны русские и латинские буквы — опечатка?")
    if any(len(w) >= 12 and w.isascii() and w.isupper() for w in words):
        why.append("похоже на набор букв")
    if len(name) > LONG_NAME:
        why.append("длинное — похоже на описание с упаковки, сократить")
    brand = "" if brand.strip().casefold() in NO_BRAND else brand
    if brand and name.strip(" .").casefold() == brand.strip(" .").casefold():
        why.append("в названии только бренд — какой товар?")
    elif len(words) == 1 and not re.search(r"\d", name) and not brand:
        why.append("одно слово и нет бренда — какой товар?")
    return why
