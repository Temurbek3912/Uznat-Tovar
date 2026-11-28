"""
Что владелец ПРОСИЛ сделать — по его словам, без модели (замер 2026-11-23, ml/benchmark_assistant.py,
docs/benchmark/assistant_report_2026-11-23.md).

Зачем. Живая qwen3-vl-4b на 126 проверочных фразах в 22 случаях (17 %) предложила или сделала НЕ ТО: «Есть у нас пепси?» —
карточка «в архив», «кока кола скока стоит» — «нет в наличии», «Удали все товары» — «все напитки в архив», «Поменяй пароль
администратора» — сменила язык и фон, «Тилни лотинчага ўтказинг» — кириллицу вместо латиницы. Подсказку модели удлинять нельзя
(движок держит 4096 токенов), поэтому программа сверяет выбор модели со словами владельца (app/services/assistant.py, chat):

- wants_change — в реплике есть глагол изменения. Нет — изменения (propose_*) модели не предлагаются вовсе: вопрос остаётся
  вопросом;
- ui_meant — язык или фон, которые владелец назвал. Модель может сменить язык или фон только на названные (иначе — ничего);
- archive_said / restore_said / stock_said / photo_said — «в архив», «из архива», «закончился», «фото»: «Пепси закончилась»
  — это «нет в наличии», а не архив; «Удалить фото у хлеба» — не архив;
- all_said — «все / barcha / hamma»: без такого слова изменение многим товарам сразу не предлагается.

Всё — по латинской записи слов (category_words.latin: кириллица, узбекская кириллица и латиница сводятся к одному виду).
"""
from __future__ import annotations

import re

from app.services.category_words import latin
from app.services.price_words import _CHANGE

# Глаголы изменения сверх price_words._CHANGE (он про цену): переименовать, перевести в категорию, новый товар, закончился…
_MORE_CHANGE = ("pereimen", "perevedi", "perenes", "perekin", "novy", "novyi", "novogo", "yangi", "skidk", "chegirma",
                "bolsin", "bulsin", "nomla", "nomini", "deshev", "dorozh", "arzon", "qimmat", "vyklad", "vklyuch", "vykl",
                "podorozh", "podeshev", "udeshev")
# «молоко снова есть в наличии» (замер 2026-11-28) — снова в наличии, а не вопрос.
_STOCK_IN = ("poyavil", "privezl", "postupil", "keldi", "bor boldi", "back in stock", "snova est", "opyat est",
             "snova v nalich", "yana bor", "yana sotuvda", "is back")
_STOCK = ("zakonch", "konchil", "konchaet", "net v nalich", "netu v nalich", "tugad", "tugab", "qolmad", "yoq boldi",
          "out of stock", "sold out") + _STOCK_IN
_RENAME = ("pereimen", "nazov", "nazvan", "nomla", "nomini", "rename")
_ARCHIVE = ("arhiv", "udal", "uber", "ubra", "ochir", "olib tashla", "ne prodaem", "ne prodayom", "bolshe ne prod",
            "sotmaymiz", "sotilmaydi", "remove", "delete", "archive")
_RESTORE = ("vern", "vosstan", "qaytar", "restore", "iz arhiva", "arhivdan")
_PHOTO = ("foto", "fotk", "kartink", "snimok", "snimk", "rasm", "surat", "photo", "picture")
# «Coca-Cola mahsulotlari narxini 5% ga tushiring» («товары Coca-Cola») — тоже все товары бренда (замер 2026-11-28).
_ALL = ("vse", "vsyo", "vsem", "vseh", "vsego", "lyub", "barcha", "hamma", "butun", "all", "every",
        "mahsulotlari", "tovarlari")
_QUESTION = re.compile(r"\?\s*$")


def _text(said: str | None) -> str:
    return " " + " ".join(re.findall(r"[a-z0-9]+", latin(said or ""))) + " "


# Не просьба: «не указан» (а не «укажи»), «не меняйте», «oʻzgartirmang» / «qilmang» (узбекское «не делайте»).
_NOT_CHANGE = ("ukazan",)
_UZ_NO = re.compile(r"(?:mang|mangiz|masin|maslik)$")


def _words(said: str | None) -> list[str]:
    words = re.findall(r"[a-z0-9]+", latin(said or ""))
    return [w for n, w in enumerate(words)
            if not w.startswith(_NOT_CHANGE) and not (n and words[n - 1] in ("ne", "nelzya")) and not _UZ_NO.search(w)]


def _has(said: str | None, stems) -> bool:
    text, words = _text(said), _words(said)
    return any((" " in s and f" {s}" in text) or any(w.startswith(s) for w in words) for s in stems)


_PRICE_WORD = ("narh", "tsen", "stoim", "price")
_NUMBER = re.compile(r"\d")


# «Нутелла теперь стоит 46 тысяч», «Fanta endi 9000 turadi» (замер 2026-11-28): новая цена словами «стоит» + «теперь», без вопроса.
_ASK_WORD = ("skolk", "skok", "pochem", "qancha", "necha", "how", "kak", "what")
_PRICE_NUMBER = re.compile(r"\d[\d  ]{2,}|\d+\s*(?:тыс|тыщ|ming|минг|k\b)", re.I)


def wants_change(said: str | None) -> bool:
    """В реплике владельца есть просьба что-то изменить (глагол изменения, «в архив», «закончился», «новый товар»…) или
    новая цена без вопроса («Barcha ichimliklar narxi 8 ming», «Нутелла теперь стоит 46 тысяч»)."""
    if _has(said, _CHANGE + _MORE_CHANGE + _STOCK + _ARCHIVE + _RESTORE):
        return True
    if not said or _QUESTION.search(said) or not _NUMBER.search(said):
        return False
    if _has(said, _PRICE_WORD):
        return True
    return (bool(_PRICE_NUMBER.search(said)) and _has(said, ("stoit", "stoyat", "turadi", "costs"))
            and _has(said, ("endi", "teper", "now")) and not _has(said, _ASK_WORD))


_PLURAL = re.compile(r"(?:lar|lari|larni|larga|lardan|larning|larini|i|y|iy|am|yam|ah|yah|ov|ev|ey|ami|yami)$")


def plural(word: str) -> bool:
    """Слово (латиницей) похоже на множественное число: «napitki», «sladostyam», «ichimliklar», «gazaklarni». «kefiru»,
    «moloko» — нет: это один товар, а не категория."""
    return bool(_PLURAL.search(word or ""))


def stock_said(said: str | None) -> bool:
    return _has(said, _STOCK)


def stock_in_said(said: str | None) -> bool:
    """«Появился», «привезли», «keldi» — снова в наличии (иначе «закончился» — нет в наличии)."""
    return _has(said, _STOCK_IN)


def rename_said(said: str | None) -> bool:
    return _has(said, _RENAME)


def archive_said(said: str | None) -> bool:
    return _has(said, _ARCHIVE)


def restore_said(said: str | None) -> bool:
    return _has(said, _RESTORE)


def photo_said(said: str | None) -> bool:
    return _has(said, _PHOTO)


# Штрихкод и «копия» (замер 2026-11-28, ml/assistant_owner_questions.py): «убери штрихкод у фанты» — модель предлагала фанту в
# архив; «Сделай копию нутеллы, только 750 г» — меняла саму нутеллу. Копировать товар помощник не умеет.
_BARCODE = ("shtrih", "barkod", "barcode", "bar kod", "ean")
_COPY = ("kopi", "skopir", "kopir", "nusha", "dublir", "dublik", "duplicat", "copy", "klonir")


def barcode_said(said: str | None) -> bool:
    return _has(said, _BARCODE)


def copy_said(said: str | None) -> bool:
    return _has(said, _COPY)


def all_said(said: str | None) -> bool:
    return _has(said, _ALL)


# Язык и фон. Глагол не нужен («Qorongʻi rejim», «тёмная тема» — уже просьба), но вопрос («Почему язык узбекский?») — не просьба.
_LANG = (("uz-Cyrl", ("kiril",)), ("uz", ("lotin", "latin", "uzbek", "ozbek")), ("ru", ("russ", "rus ", "rusch", "rustil")),
         ("en", ("angl", "ingliz", "english")))
_LANG_WORD = ("yazyk", "til", "lang", "perekl", "uzbek", "ozbek", "russ", "angl", "ingliz", "english", "kiril", "lotin",
              "latin", "rus ")
_DARK = ("temn", "tyomn", "tomn", "noch", "chern", "qorong", "qora", "tungi", "dark")
_LIGHT = ("svetl", "dnevn", "yorug", "erug", "oq ", "kunduzgi", "light")
_THEME_WORD = ("tem", "fon", "rezhim", "rejim", "theme", "mode", "rang", "dizayn")


def ui_meant(said: str | None) -> tuple[str, str] | None:
    """("lang", язык) | ("theme", light|dark) | None — что владелец просит сменить на странице. Оба сразу или ни одного ясно —
    None (оба сразу — ui_wanted)."""
    wanted = ui_wanted(said)
    return next(iter(wanted.items())) if len(wanted) == 1 else None


def ui_wanted(said: str | None) -> dict[str, str]:
    """{"lang": язык, "theme": light|dark} — что владелец просит сменить на странице (одно, оба или ничего). «Переключи на
    узбекский и сделай тёмный фон» — оба (замер 2026-11-24: раньше не менялось ничего)."""
    if not said or _QUESTION.search(said) and not wants_change(said):
        return {}
    text = _text(said)
    lang = None
    if _has(said, _LANG_WORD):
        for code, stems in _LANG:
            if any(s in text if s.endswith(" ") else _has(said, (s,)) for s in stems):
                lang = code
                break
    theme = None
    if _has(said, _THEME_WORD) or _has(said, _DARK + _LIGHT):
        dark, light = _has(said, _DARK), any(s in text if s.endswith(" ") else _has(said, (s,)) for s in _LIGHT)
        theme = "dark" if dark and not light else "light" if light and not dark else None
    return {k: v for k, v in (("lang", lang), ("theme", theme)) if v}


# --- разговор: продолжение просьбы (замер 2026-11-24, ml/assistant_dialogs.py) ----------------------------------------------------
# «Убери пепси в архив» → «нет, не пепси, а фанту»; «Поставь цену фанты 9000» → «и кока-коле тоже 9500»: во второй реплике нет
# глагола — это не вопрос, а та же просьба о другом товаре. Отрицание «не пепси» / «Pepsi emas» — товар, который владелец НЕ имел
# в виду.
_NEGATED = re.compile(r"(?i)(?:^|(?<=[\s,.;:!]))(?:не|ne|not)\s+[\wʻ'’-]+|[\wʻ'’-]+\s+(?:emas|эмас)\b")
FOLLOW_MAX_WORDS = 8


def without_negated(said: str | None) -> str:
    """Реплика без «не X» / «X emas»: «нет, не пепси, а фанту» → «нет, , а фанту»."""
    return _NEGATED.sub(" ", said or "")


_PRONOUN = ("ego", "ee", "eyo", "ei", "ey", "nee", "ney", "emu", "nego", "nemu", "nei", "neyo", "neye", "on", "ona", "ono", "eto", "etot", "eta",
            "etu", "etomu", "etoi", "etoy", "tot", "ta", "tu", "tomu", "toi", "toy", "uni", "unga", "uning", "shu", "shuni",
            "shunga", "ushbu", "it", "its", "this", "that", "him", "her", "them", "tozhe", "ham", "also", "too", "net", "yoq",
            "da", "ha", "ok", "na", "do", "ga", "dlya", "za", "sum", "som", "soum", "uzs", "tys", "tysyach", "ming", "pozhaluista",
            "pozhaluysta", "iltimos", "please", "tsenu", "tsena", "tseny", "narx", "narxi", "narxini", "narhi", "narhini",
            "price", "olin", "oling", "olinglar", "qiling", "qilib", "qoying", "boling", "bolsin",
            "to", "by", "the", "a", "of", "for", "now", "teper", "endi")


def points_back(said: str | None) -> bool:
    """В реплике нет слов, которые могут быть товаром: только глагол, число, цена, «её / ему / uni / тоже» — товар тот, о
    котором шла речь («поменяй её цену на 15000», «подними на 2000», «uni arxivga oling»). «Поставь кефиру 9000» — нет."""
    stems = _CHANGE + _MORE_CHANGE + _STOCK + _ARCHIVE + _RESTORE + _PRICE_WORD
    words = [w for w in re.findall(r"[a-z0-9]+", latin(said or "")) if not w.isdigit()]
    return bool(words) and all(w in _PRONOUN or w.startswith(stems) or len(w) <= 1 for w in words)


def follow_up(last: str | None, previous: str | None) -> bool:
    """Короткая реплика без глагола изменения и без вопроса после просьбы изменить — продолжение той же просьбы."""
    if not last or not previous or wants_change(last) or _QUESTION.search(last):
        return False
    return len(re.findall(r"\w+", last)) <= FOLLOW_MAX_WORDS and wants_change(previous)
