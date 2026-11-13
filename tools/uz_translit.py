"""Узбекская латиница → узбекская кириллица для словаря интерфейса (четвёртый языковой вариант, документ 14, §2.1 п. 4).

Что делает: из frontend/i18n.uz.js (латиница) строит frontend/i18n.uz-cyrl.js (кириллица) по правилам соответствия алфавитов
и СЛОВАРЮ ИСКЛЮЧЕНИЙ. Результат — ЧЕРНОВИК: транслитерация не безошибочна (ARCHITECTURE_DECISIONS_2026-11-07.md, §10.1),
до выпуска его вычитывает носитель языка.

Правила (официальное соответствие алфавитов 1995 г.):
  * oʻ → ў, gʻ → ғ (апостроф: ʻ U+02BB, а также ' ‘ ’ ` — на входе встречаются все), sh → ш, ch → ч, ng → нг;
  * yo, yu, ya, ye → ё, ю, я, е (кроме «y» + «oʻ»: yoʻl → йўл);
  * e → э в начале слова и после гласной, иначе е;
  * ʼ (U+02BC, «тутуқ белгиси») → ъ; «sʼh» → сҳ (разделитель, а не ъ);
  * ts → тс (по умолчанию: в исконных словах это стык морфем — ketsa → кетса); ц восстанавливается только словарём исключений;
  * h → ҳ, x → х, q → қ, j → ж, y → й.
Чего правила не умеют — и для чего исключения: русские и международные заимствования, которые в кириллице пишутся
по-русски (parol → пароль, aksiya → акция, oktabr → октябрь, litsenziya → лицензия).

Не транслитерируются («защищённые» фрагменты): подстановки {имя}, слова с цифрами (EAN-13), слова с буквами вне узбекской
латиницы (c, w — бренды вроде Coca-Cola), записи из прописных букв (QR, UPC, URL), пути, адреса и имена файлов (https://…, .env,
docs/RUNBOOK.md, ALLOW_DEBUG_EMBEDDER).

  python tools/uz_translit.py generate     записать frontend/i18n.uz-cyrl.js
  python tools/uz_translit.py check        код 1, если файл устарел (словарь латиницы поменяли, а кириллицу — нет)
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
FRONT = ROOT / "frontend"
UZ_FILE = FRONT / "i18n.uz.js"
CYRL_FILE = FRONT / "i18n.uz-cyrl.js"
LANG_CODE = "uz-Cyrl"

OKINA = "ʻ"          # ʻ  — в oʻ, gʻ
TUTUQ = "ʼ"          # ʼ  — тутуқ белгиси
APOSTROPHES = OKINA + "'‘’`"
VOWELS = set("aeiouAEIOU")

# Заимствования, которые в кириллице пишутся по-русски. Ключ — слово латиницей в нижнем регистре (как в словаре интерфейса),
# значение — кириллица в нижнем регистре. Регистр первой буквы и «всё прописными» переносятся автоматически.
EXCEPTIONS: dict[str, str] = {
    "yandex": "яндекс",                          # «Yandex Klyuch» → «Яндекс Ключ» (x здесь — «кс»)
    "parol": "пароль",
    "parolni": "паролни",
    "parolingiz": "паролингиз",
    "parollar": "пароллар",
    "model": "модель",
    "modeli": "модели",
    "modelni": "моделни",
    "profil": "профиль",
    "profilingiz": "профилингиз",
    "aksiya": "акция",
    "litsenziya": "лицензия",
    "funksiya": "функция",
    "operatsiya": "операция",
    "stansiya": "станция",
    "oktabr": "октябрь",
    "sentabr": "сентябрь",
    "noyabr": "ноябрь",
    "dekabr": "декабрь",
    "fevral": "февраль",
    "aprel": "апрель",
    "iyun": "июнь",
    "iyul": "июль",
    "sentner": "центнер",
    "sement": "цемент",
    "konsert": "концерт",
}

# Технические и чужие слова без букв c/w, которые правила не отличили бы от узбекских (https → «ҳттпс» было бы ошибкой).
PROTECTED_WORDS = {"https", "http", "localhost", "python", "zoom", "docker", "compose", "dir", "iphone", "android", "jpg", "png", "webp", "heic", "address", "redis", "up", "google"}   # address — надпись Windows «IPv4 Address»

# Единицы измерения прописными: защищены как сокращения, но в кириллице пишутся кириллицей.
UNITS = {"MB": "МБ", "KB": "КБ", "GB": "ГБ", "TB": "ТБ"}

# Основы заимствований с мягким знаком ВНУТРИ слова: основа заменяется, окончание транслитерируется по правилам
# (kompyuterda → компьютерда, filtrlarni → фильтрларни).
STEM_EXCEPTIONS: dict[str, str] = {
    "kompyuter": "компьютер",
    "filtr": "фильтр",
    "pozitsiya": "позиция",                      # ц в русских заимствованиях (позиция, позициялар)
}

_PROTECTED = re.compile(
    r"\{\w+\}"                                   # подстановка {name}
    r"|https?://\S+"                             # адрес
    r"|[\w.\-]*[/\\][\w./\\\-]*"                 # путь: docs/RUNBOOK.md
    r"|(?<![\w.])\.[A-Za-z]\w*"                  # .env
    r"|\b\w+(?:\.\w+)+\b"                        # scripts.import_catalog, catalog_guide.md
    r"|(?<![^\s])--?[A-Za-z][\w-]*"              # ключи команд после пробела: -m, --dir (а не «{n}-surat»)
    r"|\b[A-Za-z0-9]*_[A-Za-z0-9_]*\b"           # ALLOW_DEBUG_EMBEDDER
    r"|\b(?=[A-Za-z\-]*\d)[A-Za-z0-9][A-Za-z0-9\-]*\b"   # слова с цифрами: EAN-13, M200
    r"|\b[A-Z]{2,}(?:-[A-Z0-9]+)*\b"             # QR, UPC-A, URL, EAN
)
_LETTERS = r"[A-Za-z" + APOSTROPHES + TUTUQ + r"]+"
_WORD = re.compile(_LETTERS + r"(?:-" + _LETTERS + r")*")      # слова через дефис — одно целое (Wi-Fi, Shtrix-kod)

_SINGLE = {
    "a": "а", "b": "б", "d": "д", "f": "ф", "g": "г", "h": "ҳ", "i": "и", "j": "ж", "k": "к", "l": "л", "m": "м", "n": "н",
    "o": "о", "p": "п", "q": "қ", "r": "р", "s": "с", "t": "т", "u": "у", "v": "в", "x": "х", "y": "й", "z": "з",
}
_IOTATED = {"o": "ё", "u": "ю", "a": "я", "e": "е"}


def _is_apostrophe(ch: str) -> bool:
    return ch in APOSTROPHES


def _word_lower(word: str) -> str:
    """Слово в нижнем регистре с одним видом апострофа (для поиска в исключениях)."""
    out = []
    for i, ch in enumerate(word):
        if _is_apostrophe(ch) and i > 0 and word[i - 1].lower() in "og":
            out.append(OKINA)
        elif _is_apostrophe(ch) or ch == TUTUQ:
            out.append(TUTUQ)
        else:
            out.append(ch.lower())
    return "".join(out)


def _translit_lower(w: str) -> str:
    """Транслитерация слова в нижнем регистре с нормализованными апострофами (ʻ после o/g, ʼ в остальных местах)."""
    res: list[str] = []
    i, n = 0, len(w)
    while i < n:
        c = w[i]
        nxt = w[i + 1] if i + 1 < n else ""
        nxt2 = w[i + 2] if i + 2 < n else ""
        if c in "og" and nxt == OKINA:
            res.append("ў" if c == "o" else "ғ")
            i += 2
            continue
        if c == "s" and nxt == TUTUQ and nxt2 == "h":          # Isʼhoq → Исҳоқ
            res.append("сҳ")
            i += 3
            continue
        if c == "s" and nxt == "h":
            res.append("ш")
            i += 2
            continue
        if c == "c" and nxt == "h":
            res.append("ч")
            i += 2
            continue
        if c == "y" and nxt in _IOTATED and not (nxt == "o" and nxt2 == OKINA):
            res.append(_IOTATED[nxt])
            i += 2
            continue
        if c == "e":
            prev = w[i - 1] if i > 0 else ""
            res.append("э" if (i == 0 or prev in "aeiou" or prev == OKINA) else "е")
            i += 1
            continue
        if c == TUTUQ:
            res.append("ъ")
            i += 1
            continue
        if c == OKINA:                                         # одиночный ʻ без o/g — оставляем как разделитель
            res.append("ъ")
            i += 1
            continue
        res.append(_SINGLE.get(c, c))
        i += 1
    return "".join(res)


def _apply_case(src: str, cyr: str) -> str:
    letters = [ch for ch in src if ch.isalpha()]
    if len(letters) > 1 and all(ch.isupper() for ch in letters):
        return cyr.upper()
    if letters and letters[0].isupper():
        return cyr[:1].upper() + cyr[1:]
    return cyr


def translit_word(word: str, exceptions: dict[str, str] | None = None) -> str:
    exceptions = EXCEPTIONS if exceptions is None else exceptions
    if _is_foreign(word):
        return word                                            # буквы вне узбекской латиницы — бренд или чужое слово
    key = _word_lower(word)
    if key in PROTECTED_WORDS:
        return word
    cyr = exceptions.get(key)
    if cyr is None:
        for stem, stem_cyr in STEM_EXCEPTIONS.items():
            if key.startswith(stem):
                cyr = stem_cyr + _translit_lower(key[len(stem):])
                break
    if cyr is None:
        cyr = _translit_lower(key)
    return _apply_case(word, cyr)


def latin_to_cyrillic(text: str, exceptions: dict[str, str] | None = None) -> str:
    """Текст латиницей → кириллица. Защищённые фрагменты переносятся без изменений."""
    out, pos = [], 0
    for m in _PROTECTED.finditer(text):
        out.append(_translit_plain(text[pos:m.start()], exceptions))
        out.append(UNITS.get(m.group(0), m.group(0)))
        pos = m.end()
    out.append(_translit_plain(text[pos:], exceptions))
    return "".join(out)


def _is_foreign(word: str) -> bool:
    """Буквы вне узбекской латиницы (c без h, w): бренд или чужое слово — не транслитерируется."""
    return re.search(r"[cw]", word.lower().replace("ch", "")) is not None


def _translit_plain(text: str, exceptions: dict[str, str] | None) -> str:
    def repl(m: re.Match) -> str:
        compound = m.group(0)
        if "-" in compound:
            parts = compound.split("-")
            if any(_is_foreign(part) for part in parts):
                return compound                                # Wi-Fi, Coca-Cola — целиком как есть
            return "-".join(_translit_piece(part, exceptions) for part in parts)
        return _translit_piece(compound, exceptions)
    return _WORD.sub(repl, text)


def _translit_piece(word: str, exceptions: dict[str, str] | None) -> str:
    """Одно слово. Апостроф-кавычка в начале или в конце слова — не часть слова (кроме ʻ в «boʻ» на конце)."""
    lead = len(word) - len(word.lstrip(APOSTROPHES))
    body = word[lead:]
    trail_len = len(body) - len(body.rstrip(APOSTROPHES.replace(OKINA, "")))
    if trail_len and body[:-trail_len][-1:].lower() in ("o", "g"):
        trail_len -= 1
    tail = body[len(body) - trail_len:] if trail_len else ""
    core = body[:len(body) - trail_len] if trail_len else body
    return word[:lead] + (translit_word(core, exceptions) if core else "") + tail


def translit_value(value, exceptions=None):
    if isinstance(value, dict):
        return {form: latin_to_cyrillic(text, exceptions) for form, text in value.items()}
    return latin_to_cyrillic(value, exceptions)


def build_dictionary(uz_source: str) -> dict:
    if str(ROOT) not in sys.path:                      # запуск как `python tools/uz_translit.py`
        sys.path.insert(0, str(ROOT))
    from tools.uz_review import parse_dictionary

    return {key: translit_value(value) for key, value in parse_dictionary(uz_source).items()}


def render_js(cyr: dict) -> str:
    lines = [
        "/* Словарь интерфейса: узбекский (кириллица). СГЕНЕРИРОВАН tools/uz_translit.py из i18n.uz.js — руками не править:",
        "   правки латиницы вносятся в i18n.uz.js, правки только кириллицы — в EXCEPTIONS инструмента; затем",
        "   `python tools/uz_translit.py generate`. ЧЕРНОВИК: автоматическая транслитерация, до выпуска — вычитка носителем языка.",
        "   Ключи одинаковы во всех словарях (проверяет tools/i18n_smoke_test.js). */",
        "(function (root) {",
        '  "use strict";',
        "  var dict = {",
    ]
    items = list(cyr.items())
    for idx, (key, value) in enumerate(items):
        comma = "," if idx < len(items) - 1 else ""
        lines.append(f"    {json.dumps(key, ensure_ascii=False)}: {json.dumps(value, ensure_ascii=False)}{comma}")
    lines += [
        "  };",
        '  if (typeof module === "object" && module.exports) module.exports = dict;',
        f'  if (root.I18N) root.I18N.add("{LANG_CODE}", dict);',
        '})(typeof window !== "undefined" ? window : globalThis);',
        "",
    ]
    return "\n".join(lines)


def expected_file_text() -> str:
    return render_js(build_dictionary(UZ_FILE.read_text(encoding="utf-8")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("cmd", choices=["generate", "check"])
    args = parser.parse_args(argv)
    text = expected_file_text()
    if args.cmd == "generate":
        CYRL_FILE.write_text(text, encoding="utf-8")
        print(f"записано: {CYRL_FILE.relative_to(ROOT)}")
        return 0
    current = CYRL_FILE.read_text(encoding="utf-8") if CYRL_FILE.exists() else ""
    if current != text:
        print("i18n.uz-cyrl.js устарел: запустите `python tools/uz_translit.py generate`")
        return 1
    print("i18n.uz-cyrl.js соответствует i18n.uz.js")
    return 0


if __name__ == "__main__":
    sys.exit(main())
