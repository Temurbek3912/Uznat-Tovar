"""Проверка узбекских текстов носителем языка (tools/uz_review.py): чистая логика — разбор словарей, подстановки, апострофы, проверка и внесение правок,
поиск экранов, где виден текст. Работа с Excel (openpyxl) здесь не проверяется: в образе проекта этой библиотеки может не быть."""
from __future__ import annotations

import pathlib

from tools import uz_review as ur

FRONT = pathlib.Path(__file__).resolve().parents[1] / "frontend"


def _load(lang: str) -> tuple[str, dict]:
    source = (FRONT / f"i18n.{lang}.js").read_text(encoding="utf-8")
    return source, ur.parse_dictionary(source)


def test_the_three_real_dictionaries_parse_to_the_same_keys_with_matching_placeholders():
    _, ru = _load("ru")
    _, uz = _load("uz")
    _, en = _load("en")
    assert len(ru) > 150 and set(ru) == set(uz) == set(en)
    for key in ru:
        assert ur.placeholders(ur.flatten(ru[key])) == ur.placeholders(ur.flatten(uz[key])) == ur.placeholders(ur.flatten(en[key])), key
    assert set(uz["photos.count"]) == {"other"} and "{n}" in uz["photos.count"]["other"], "у узбекского счёта одна форма other"
    assert set(ru["items.count"]) == {"one", "few", "many", "other"}


def test_apostrophes_are_turned_into_the_right_uzbek_marks():
    assert ur.normalize_apostrophes("o'zbek, g'oya, ko‘rsatma") == ("o\u02BBzbek, g\u02BBoya, ko\u02BBrsatma", ["o' → o\u02BB", "g' → g\u02BB", "o‘ → o\u02BB"])
    assert ur.normalize_apostrophes("ma'lumot, ma’no") == ("ma\u02BClumot, ma\u02BCno", ["a' → a\u02BC", "a’ → a\u02BC"])
    assert ur.normalize_apostrophes("o\u02BBzbek ma\u02BClumot") == ("o\u02BBzbek ma\u02BClumot", []), "уже правильные знаки не трогаем"
    assert ur.normalize_apostrophes("o\u02BCzbek")[0] == "o\u02BBzbek", "ʼ после o — это ошибка: должно быть ʻ"
    assert ur.normalize_apostrophes("Salom, dunyo!") == ("Salom, dunyo!", [])


def test_a_correction_is_checked_before_it_is_accepted():
    _, ru = _load("ru")
    _, uz = _load("uz")
    ok, notes, errors = ur.validate_correction("settings.signed_in", uz["settings.signed_in"], ru["settings.signed_in"], "  Siz  {name}  sifatida  kirdingiz  ")
    assert errors == [] and ok == "Siz {name} sifatida kirdingiz", "лишние пробелы убираются"
    assert ur.validate_correction("settings.signed_in", "x", ru["settings.signed_in"], "Siz kirdingiz")[2], "потеряна подстановка {name}"
    assert ur.validate_correction("settings.signed_in", "x", ru["settings.signed_in"], "Siz {user} kirdingiz")[2], "чужая подстановка"
    assert ur.validate_correction("login.title", "Kirish", ru["login.title"], "Вход")[2], "кириллица"
    assert ur.validate_correction("login.title", "Kirish", ru["login.title"], "   ")[2] == ["пусто"]
    assert ur.validate_correction("login.title", "Kirish", ru["login.title"], "Kirish\nqayta")[2], "перенос строки"
    assert ur.validate_correction("items.count", uz["items.count"], ru["items.count"], "ta mahsulot")[2], "в тексте со счётом нужно {n}"
    long_text = "Juda uzun matn " * 8
    assert any("длиннее" in n for n in ur.validate_correction("login.user", "Login", ru["login.user"], long_text)[1])


def test_applying_corrections_changes_only_the_named_lines_and_keeps_the_file_valid():
    """Правки строятся как «текущий текст + метка», поэтому тест не зависит от того, какими словами сейчас написан узбекский словарь."""
    source, uz = _load("uz")
    _, ru = _load("ru")
    marker = " \u00B7"
    keys = ["login.title", "settings.signed_in", "items.count", "photos.count"]
    current = lambda k: uz[k] if isinstance(uz[k], str) else uz[k]["other"]
    corrections = {k: current(k) + marker for k in keys}
    new_source, changes, errors = ur.apply_corrections(source, corrections, ru)
    assert errors == {} and sorted(c["key"] for c in changes) == sorted(keys)
    after = ur.parse_dictionary(new_source)
    for k in keys:
        want = current(k) + marker
        assert after[k] == (want if isinstance(uz[k], str) else {"other": want}), k
    assert {k: v for k, v in after.items() if k not in keys} == {k: v for k, v in uz.items() if k not in keys}, "остальные тексты не тронуты"
    old_lines, new_lines = source.split("\n"), new_source.split("\n")
    assert len(old_lines) == len(new_lines) and sum(a != b for a, b in zip(old_lines, new_lines)) == 4, "изменились ровно четыре строки"
    assert new_source.startswith(source[:300]) and new_source.rstrip().endswith("globalThis);"), "шапка и конец файла целы"
    again, none, _ = ur.apply_corrections(new_source, {"login.title": current("login.title") + marker}, ru)
    assert again == new_source and none == [], "та же правка второй раз ничего не меняет"


def test_one_bad_correction_stops_everything_and_nothing_is_changed():
    source, _ = _load("uz")
    _, ru = _load("ru")
    result, changes, errors = ur.apply_corrections(source, {"login.title": "Tizimga kirish", "settings.signed_in": "Siz kirdingiz", "no.such.key": "x"}, ru)
    assert result == source and changes == [] and set(errors) == {"settings.signed_in", "no.such.key"}


def test_quotes_and_backslashes_in_a_correction_do_not_break_the_javascript_string():
    source, _ = _load("uz")
    _, ru = _load("ru")
    new_source, _, errors = ur.apply_corrections(source, {"login.user": 'Foydalanuvchi "nomi" \\ login'}, ru)
    assert errors == {} and ur.parse_dictionary(new_source)["login.user"] == 'Foydalanuvchi "nomi" \\ login'


def test_usage_map_finds_screens_by_markup_code_and_dynamic_keys():
    files = {"settings.html": '<h2 data-i18n="settings.account">x</h2>', "auth.js": 'return { key: "login.wrong" };', "settings.js": 't("theme." + mode)',
             "old_page.html": '<p data-i18n="settings.old">x</p>', "ticket.js": 't("ticket.retake")'}
    found = ur.usage_map(files, ["settings.account", "login.wrong", "theme.dark", "theme.light", "settings.old", "ticket.retake", "nowhere.key"])
    assert found["settings.account"] == ["Настройки"] and found["login.wrong"] == ["Вход (все страницы)"]
    assert found["theme.dark"] == found["theme.light"] == ["Настройки"], "динамический ключ «theme.» + режим"
    assert found["settings.old"] == [] and found["nowhere.key"] == [], "файлы не из списка экранов и неиспользуемые ключи — без экрана"
    assert found["ticket.retake"] == ["Ценник на «Узнать товар» и «Товарах»"]
    more = ur.usage_map({"identify.html": '<div data-i18n-attr="aria-label:identify.stage_label;placeholder:products.code_add"></div>', "productlist.js": 'select("listSort", "list.sort", SORTS, "sort.", state.sort)',
                         "preview.js": 't("demo.state." + id)'}, ["identify.stage_label", "products.code_add", "sort.name", "sort.new", "demo.state.matched", "group.brand"])
    assert more["identify.stage_label"] == ["Узнать товар"] and more["products.code_add"] == ["Узнать товар"], "ключи в data-i18n-attr"
    assert more["sort.name"] == more["sort.new"] == ["Список товаров"], "префикс, переданный аргументом"
    assert more["demo.state.matched"] == ["Витрина интерфейса (preview.html)"] and more["group.brand"] == []


def test_real_frontend_shows_where_every_new_account_text_lives():
    texts = ur.read_frontend_texts()
    _, uz = _load("uz")
    found = ur.usage_map(texts, list(uz))
    for key in ("settings.account", "settings.signed_in", "settings.sign_out"):
        assert found[key] == ["Настройки"], key
    assert "Узнать товар" in found["identify.auto"] and "Вход (все страницы)" in found["login.wrong"]
