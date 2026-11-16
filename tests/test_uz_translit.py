"""Узбекская латиница → кириллица для словаря интерфейса (tools/uz_translit.py).

Проверяются правила соответствия алфавитов, известные трудности (ARCHITECTURE_DECISIONS_2026-11-07.md, §10.1), защита технических
фрагментов и то, что сгенерированный frontend/i18n.uz-cyrl.js соответствует латинскому словарю. Качество перевода как такового
здесь не проверяется: кириллический словарь — черновик для вычитки носителем языка.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tools import uz_translit as ut

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("latin, cyrillic", [
    ("oʻzbek", "ўзбек"), ("gʻoya", "ғоя"), ("shahar", "шаҳар"), ("choy", "чой"), ("ming", "минг"),
    ("yoʻl", "йўл"),                        # y + oʻ, а не «ё»
    ("yoki", "ёки"), ("yuk", "юк"), ("yangi", "янги"), ("yer", "ер"),
    ("eshik", "эшик"), ("ertak", "эртак"),  # e в начале слова → э
    ("poeziya", "поэзия"),                  # e после гласной → э
    ("telefon", "телефон"),                 # e после согласной → е
    ("maʼlumot", "маълумот"),               # тутуқ белгиси → ъ
    ("Isʼhoq", "Исҳоқ"),                    # sʼh — разделитель, а не ъ
    ("ketsa", "кетса"),                     # ts на стыке морфем — т+с, не ц
    ("xato", "хато"), ("hisob", "ҳисоб"), ("qiziq", "қизиқ"), ("jahon", "жаҳон"),
])
def test_alphabet_rules(latin, cyrillic):
    assert ut.latin_to_cyrillic(latin) == cyrillic


@pytest.mark.parametrize("variant", ["oʻ", "o'", "o‘", "o’", "o`"])
def test_every_apostrophe_variant_after_o_and_g_gives_the_letter(variant):
    assert ut.latin_to_cyrillic(f"t{variant}gri") == "тўгри"
    assert ut.latin_to_cyrillic(variant.replace("o", "g") + "alla") == "ғалла"


@pytest.mark.parametrize("latin, cyrillic", [
    ("parol", "пароль"), ("oktabr", "октябрь"), ("aksiya", "акция"), ("litsenziya", "лицензия"),
    ("kompyuterda", "компьютерда"), ("filtrlarni", "фильтрларни"),
])
def test_loanwords_spelled_the_russian_way_come_from_the_exception_list(latin, cyrillic):
    assert ut.latin_to_cyrillic(latin) == cyrillic


def test_without_exceptions_the_rules_alone_would_be_wrong_which_is_why_the_list_exists():
    assert ut.latin_to_cyrillic("aksiya", exceptions={}) == "аксия"
    assert ut.latin_to_cyrillic("parol", exceptions={}) == "парол"


def test_case_is_preserved():
    assert ut.latin_to_cyrillic("Shahar") == "Шаҳар"
    assert ut.latin_to_cyrillic("Oʻzbekiston") == "Ўзбекистон"
    assert ut.latin_to_cyrillic("Parol") == "Пароль"


@pytest.mark.parametrize("text", [
    "{name}", "EAN-13", "UPC-A", "https://example.org/a", "docs/RUNBOOK.md", ".env", "ALLOW_DEBUG_EMBEDDER",
    "python -m scripts.import_catalog --dir catalog", "Coca-Cola", "Wi-Fi", "QR", "localhost", "M200",
])
def test_technical_fragments_brands_and_codes_are_not_transliterated(text):
    assert ut.latin_to_cyrillic(text) == text


def test_drive_letter_stays_and_the_folder_name_is_transliterated():
    assert ut.latin_to_cyrillic("masalan D:\\Nusxalar yoki E:\\.") == "масалан D:\\Нусхалар ёки E:\\."


def test_mixed_sentence_keeps_placeholders_and_codes():
    src = "{n}-surat: {message}. Kod EAN-13 va UPC-A mos keladi, (https) manzil."
    assert ut.latin_to_cyrillic(src) == "{n}-сурат: {message}. Код EAN-13 ва UPC-A мос келади, (https) манзил."


def test_quotes_around_words_are_kept_and_not_read_as_tutuq():
    assert ut.latin_to_cyrillic("«Suratga olish»") == "«Суратга олиш»"
    assert ut.latin_to_cyrillic("'bor'") == "'бор'"


def test_plural_forms_are_transliterated_form_by_form():
    assert ut.translit_value({"other": "{n} ta mahsulot"}) == {"other": "{n} та маҳсулот"}


def test_generated_dictionary_is_in_sync_with_the_latin_one():
    """Латиницу поменяли, а кириллицу не пересоздали — тест падает (исправление: python tools/uz_translit.py generate)."""
    assert ut.CYRL_FILE.read_text(encoding="utf-8") == ut.expected_file_text()
    assert ut.main(["check"]) == 0


def test_generated_dictionary_has_exactly_the_latin_keys():
    from tools.uz_review import parse_dictionary

    latin = parse_dictionary(ut.UZ_FILE.read_text(encoding="utf-8"))
    cyrl = ut.build_dictionary(ut.UZ_FILE.read_text(encoding="utf-8"))
    assert list(cyrl) == list(latin)


def test_generated_file_is_marked_as_a_draft_and_registers_the_uz_cyrl_code():
    text = ut.CYRL_FILE.read_text(encoding="utf-8")
    assert "ЧЕРНОВИК" in text and "носителем" in text and 'I18N.add("uz-Cyrl", dict)' in text


def test_every_page_with_the_latin_dictionary_also_loads_the_cyrillic_one_right_after_it():
    for page in (ROOT / "frontend").glob("*.html"):
        html = page.read_text(encoding="utf-8")
        if 'src="i18n.uz.js"' in html:
            assert '<script src="i18n.uz.js"></script>\n<script src="i18n.uz-cyrl.js"></script>' in html, page.name
