"""
Категории товаров на разных языках: «shirinliklar» = «Сладости», «gazak» = «Снеки» (решение владельца 2026-11-17).

Зачем. Владелец говорит с помощником по-узбекски, а категории в каталоге чаще по-русски (или наоборот). Маленькая модель
(qwen3-vl-4b) переводила категорию сама и раз ошиблась: «gazak» (снеки) приняла за «Газированные напитки». Теперь слово
сопоставляет программа — по этому словарю, а не модель.

Как устроено. Группа — одно понятие: основы слов на русском (латиницей, как после _latin) и на узбекском (латиница; кириллица
приводится к ней). Слово подходит к основе, если начинается с неё («shirinliklar» → «shirinlik»); основа из двух слов — два слова
подряд («gazli ichimliklar»). Если в запросе нашлась длинная основа, короткая внутри неё не считается: «gazli ichimliklar» —
только газированные напитки, а не все напитки. Категория каталога подходит, если в ней есть все найденные понятия (иначе — любое).

Словарь небольшой и только про магазин у дома; чего в нём нет — работает как раньше (поиск по написанию).
"""
from __future__ import annotations

import re

from app.core import text_search
from app.services.catalog_upload import _TRANSLIT

# Основы — в нижнем регистре латиницей, без апострофов; «х/x/kh» → «h» (как в latin()). Короткие основы, похожие на другие слова,
# записаны целым словом с «=» на конце: «suv=» подходит только к слову «suv»/«suvlar», а не к «suvenir».
GROUPS: tuple[tuple[str, ...], ...] = (
    # напитки
    ("gazirovann", "gazli ichim", "gazlangan", "gazli suv", "газли ичим"),
    ("napit", "ichimlik"),
    ("mineraln", "mineral suv", "suv=", "voda=", "vody="),
    ("sok=", "soki=", "sokov", "sharbat"),
    ("chay=", "chaynye", "choy="),
    ("kofe", "qahva", "kofey"),
    ("energetik", "energetich", "energetik ichim"),
    ("piv", "pivo"),
    ("alkogol", "spirtli ichim", "spirtli", "vino", "vodk"),
    # сладкое и снеки
    ("sladost", "shirinlik", "konditer", "qandolat"),
    ("konfet",),
    ("shokolad",),
    ("pechen", "pechenye"),
    ("snek", "sneki", "gazak", "chips", "chipsy"),
    ("orehi", "oreh", "yongoq", "semechk", "pista"),
    ("morozhen", "muzqaymoq"),
    # еда
    ("moloch", "sut=", "sut mahsulot", "sutli", "kislomoloch", "kefir", "qatiq"),
    ("hleb", "non=", "nonlar", "vypechk", "pishiriq"),
    ("myas", "gosht", "kolbas"),
    ("ryb", "baliq"),
    ("yayts", "yaytsa", "tuhum"),
    ("frukt", "meva"),
    ("ovoshch", "sabzavot"),
    ("krup", "yorma", "ris=", "guruch"),
    ("makaron",),
    ("muka=", "muki=", "un=", "un mahsulot"),
    ("sahar", "shakar"),
    ("maslo", "masla", "yog="),
    ("konserv",),
    ("spetsi", "pripravy", "ziravor"),
    ("zamorozh", "muzlatilgan"),
    ("bakale", "oziq-ovqat", "oziq ovqat"),
    # не еда
    ("bytov", "himiya", "maishiy kimyo", "kimyo", "tozalash"),
    ("gigien", "shampun", "mylo", "sovun"),
    ("kosmetik", "pardoz"),
    ("detsk", "bolalar", "dlya detey"),
    ("hoztovar", "hozyaystv", "uy rozgor", "uy-rozgor"),
    ("sigaret", "tabak", "tamaki"),
    ("korm", "hayvon", "dlya zhivotn"),
    ("kantstovar", "kants", "kantselyar"),
)

_UZ_CYR = {"ў": "o", "қ": "q", "ғ": "g", "ҳ": "h"}      # узбекская кириллица -> латиница (остальное — как русская)


def latin(text: str | None) -> str:
    """«Газированные напитки» → «gazirovannye napitki»; «Ширинликлар» → «shirinliklar»; «Oʻzbek» → «ozbek»; x/kh → h."""
    value = text_search.normalize(text).replace("'", "")
    value = "".join(_UZ_CYR.get(ch) or _TRANSLIT.get(ch, ch) for ch in value)
    return value.replace("kh", "h").replace("x", "h")


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9-]+", latin(text))


def _word_fits(word: str, stem: str) -> bool:
    if stem.endswith("="):                       # целое слово (+ узбекское «-lar», «-lari»)
        stem = stem[:-1]
        return word in (stem, stem + "lar", stem + "lari", stem + "i")
    return word.startswith(stem)


def _hits(stem: str, words: list[str]) -> set[int] | None:
    """Номера слов, на которые легла основа (из одного или нескольких слов), или None."""
    parts = latin(stem.rstrip("=")).split() if " " in stem else [stem]
    if " " in stem and stem.endswith("="):
        parts[-1] += "="
    for i in range(len(words) - len(parts) + 1):
        if all(_word_fits(words[i + k], parts[k]) for k in range(len(parts))):
            return set(range(i, i + len(parts)))
    return None


def groups_of(text: str | None) -> list[int]:
    """Номера групп словаря, которые названы в тексте; «внутренние» (короче и внутри длинной) отброшены."""
    words = _words(text or "")
    found: list[tuple[int, set[int]]] = []
    for g, stems in enumerate(GROUPS):
        spans = [s for s in (_hits(stem, words) for stem in stems) if s]
        if spans:
            found.append((g, max(spans, key=len)))
    return [g for g, span in found if not any(span < other for _, other in found)]


def _fits(category: str | None, group: int) -> bool:
    words = _words(category or "")
    return any(_hits(stem, words) for stem in GROUPS[group])


def matching_categories(query: str | None, categories) -> list[str]:
    """Какие из категорий каталога подходят к запросу по словарю (на любом языке).

    Сначала — категории, где есть ВСЕ названные понятия: «Газированные напитки» — это газированные И напитки, поэтому
    просто «Напитки» сюда не попадут. Если таких нет (в запросе две категории: «shirinliklar va gazaklar») — где есть любое."""
    groups = groups_of(query)
    if not groups:
        return []
    names = [c for c in dict.fromkeys(categories) if c]
    every = [c for c in names if all(_fits(c, g) for g in groups)]
    return every or [c for c in names if any(_fits(c, g) for g in groups)]


def existing_category(value: str | None, categories) -> str | None:
    """Новое значение категории → уже существующая категория, если это она (на другом языке или в другом написании):
    «Gazaklar» при категории «Снеки» → «Снеки». Иначе None: тогда будет новая категория, как написал владелец."""
    key = latin(value)
    if not key:
        return None
    same = [c for c in categories if c and latin(c) == key]
    if same:
        return same[0]
    by_words = matching_categories(value, categories)
    return by_words[0] if len(by_words) == 1 else None
