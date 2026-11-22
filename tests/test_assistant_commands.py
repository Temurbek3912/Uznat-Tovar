"""ИИ-помощник не путает команды (замер 2026-11-23, ml/benchmark_assistant.py: «не та команда» 22 из 126 -> 0).

Живая qwen3-vl-4b на проверочных фразах предлагала не то: вопрос -> «в архив», «закончилась» -> архив, «удалить фото» -> архив,
«удали все товары» -> «все напитки в архив», «пароль» -> сменить язык и фон. Выбор модели сверяется со словами владельца
(app/services/command_words.py, app/services/assistant.command_check). Модель подменяется: проверяется наш код."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("httpx")
pytest.importorskip("alembic")

from tests.test_assistant import FakeModel, _ask, _session_factory  # noqa: E402, F401 — фикстура Session и подменная модель


def _no_card(reply):
    return reply.proposal is None and reply.ui == []


def test_a_question_without_a_change_verb_never_gets_a_card(Session):
    archive = {"tool": "propose_archive", "args": {"product": "COLA", "status": "archived"}}
    for said in ("Есть у нас кока-кола?", "кока кола скока стоит", "Что за товар со штрихкодом 4780001234562?",
                 "Kola bormi?"):
        model = FakeModel(archive, {"tool": "", "text": "Да, есть."})
        reply = _ask(Session, model, said)
        assert _no_card(reply), said
        schema = model.calls[0][1]["response_format"]["json_schema"]["schema"]
        branches = [b["properties"]["tool"]["enum"][0] for b in schema["anyOf"]]
        assert not any(b.startswith("propose_") for b in branches), said        # изменений модели не предлагали вовсе


def test_after_finding_the_product_for_a_question_the_model_only_answers(Session):
    model = FakeModel({"tool": "find_products", "args": {"query": "Хлеб"}}, {"tool": "", "text": "Хлеб есть, 4000 сум."})
    reply = _ask(Session, model, "Хлеб есть?")
    assert reply.text.startswith("Хлеб есть")
    assert "anyOf" not in model.calls[1][1]["response_format"]["json_schema"]["schema"]   # второй шаг — только ответ


def test_language_and_background_change_only_as_the_owner_said(Session):
    lang = FakeModel({"tool": "set_language", "args": {"lang": "ru"}}, {"tool": "set_theme", "args": {"theme": "dark"}},
                     {"tool": "", "text": "Не могу."})
    assert _no_card(_ask(Session, lang, "Поменяй пароль администратора на 1234"))          # не просили — ничего
    cyr = FakeModel({"tool": "set_language", "args": {"lang": "uz-Cyrl"}}, {"tool": "", "text": "Bajarildi."})
    assert _ask(Session, cyr, "Тилни лотинчага ўтказинг", lang="uz-Cyrl").ui == [{"kind": "lang", "value": "uz"}]
    twice = FakeModel({"tool": "set_theme", "args": {"theme": "dark"}}, {"tool": "set_language", "args": {"lang": "uz"}},
                      {"tool": "", "text": "Tayyor."})
    assert _ask(Session, twice, "Qorongʻi rejim", lang="uz").ui == [{"kind": "theme", "value": "dark"}]
    light = FakeModel({"tool": "set_theme", "args": {"theme": "dark"}}, {"tool": "", "text": "Тайёр."})
    assert _ask(Session, light, "Ёруғ фон қилинг", lang="uz-Cyrl").ui == [{"kind": "theme", "value": "light"}]


def test_out_of_stock_is_not_archive_and_photos_are_not_archived(Session):
    archive = {"tool": "propose_archive", "args": {"product": "COLA", "status": "archived"}}
    card = _ask(Session, FakeModel(archive), "Кока-кола закончилась").proposal
    assert card["kind"] == "update" and card["changes"] == {"in_stock": False}
    added = FakeModel({"tool": "propose_add", "args": {"name": "Хлеб"}})
    assert _ask(Session, added, "Xleb tugadi", lang="uz").proposal["changes"] == {"in_stock": False}
    photo = FakeModel({"tool": "propose_archive", "args": {"product": "BREAD", "status": "archived"}},
                      {"tool": "", "text": "Нет."})
    reply = _ask(Session, photo, "Удалить фото у хлеба")
    assert reply.proposal is None and "Фото" in reply.proposal_error


def test_bulk_change_needs_a_category_the_owner_named(Session):
    invented = {"tool": "propose_bulk", "args": {"where": "category", "query": "Напитки", "change": "archive", "value": ""}}
    reply = _ask(Session, FakeModel(invented, {"tool": "", "text": "Уточните."}), "Удали все товары")
    assert reply.proposal is None and "не называл" in reply.proposal_error
    dairy = {"tool": "propose_bulk", "args": {"where": "category", "query": "Молочные продукты", "change": "price_set",
                                              "value": 9000}}
    reply = _ask(Session, FakeModel(dairy, {"tool": "", "text": "Кефира нет."}), "Поставь цену кефиру 9000")
    assert reply.proposal is None                                             # «кефиру» — один товар, а не все молочные
    one = {"tool": "propose_bulk", "args": {"where": "category", "query": "Без категории", "change": "archive", "value": ""}}
    card = _ask(Session, FakeModel(one), "Хлеб больше не продаём, убери").proposal
    assert card["kind"] == "status" and card["sku"] == "BREAD"               # назван один товар — один товар в архив


def test_prices_and_names_come_from_the_owner_words(Session):
    invented = {"tool": "propose_update", "args": {"product": "COLA", "field": "price", "value": 10000}}
    for said in ("Kola narxini oʻzgartiring", "Верни цену коле как было"):
        reply = _ask(Session, FakeModel(invented, {"tool": "", "text": "Какую цену?"}), said)
        assert reply.proposal is None and "не придумывает" in reply.proposal_error, said
    renamed = {"tool": "propose_update", "args": {"product": "BREAD", "field": "name", "value": "Хлеб 4500"}}
    assert _ask(Session, FakeModel(renamed), "Сделай хлеб 4500").proposal["changes"] == {"price": 4500.0}
    archive = {"tool": "propose_archive", "args": {"product": "COLA", "status": "archived"}}
    card = _ask(Session, FakeModel(archive), "Кока-кола нархини 9500 қилинг", lang="uz-Cyrl").proposal
    assert card["changes"] == {"price": 9500.0}


def test_a_product_the_owner_did_not_name_is_not_changed_and_his_word_finds_the_product(Session):
    other = {"tool": "propose_archive", "args": {"product": "BREAD", "status": "archived"}}
    reply = _ask(Session, FakeModel(other, {"tool": "", "text": "Какой товар?"}), "удали")
    assert reply.proposal is None and "не называл" in reply.proposal_error
    cased = {"tool": "propose_archive", "args": {"product": "Хлебни", "status": "archived"}}
    assert _ask(Session, FakeModel(cased), "Хлебни архивга олинг", lang="uz-Cyrl").proposal["sku"] == "BREAD"
    back = {"tool": "propose_archive", "args": {"product": "Staryy tovarni", "status": "active"}}
    assert _ask(Session, FakeModel(back), "Stariy tovarni arxivdan qaytaring", lang="uz").proposal["sku"] == "OLD"


def test_a_ready_summary_is_the_answer_when_the_model_loops(Session):
    """Узбекская кириллица: живая модель после scan_problems повторяла одну фразу до конца ответа — JSON не дочитывался."""
    model = FakeModel({"tool": "scan_problems", "args": {"days": 30}}, '{"tool": "","text":"Покупателер танишган, чунки')
    reply = _ask(Session, model, "Нега дастур товарларни танимаяпти?", lang="uz-Cyrl")
    assert reply.text and "скан" in reply.text


def test_command_words():
    from app.services import command_words as W
    assert W.wants_change("Поставь цену фанты 9000") and W.wants_change("Snickers tugadi")
    assert W.wants_change("Barcha ichimliklar narxi 8 ming") and W.wants_change("pepsini arxivga")
    assert not W.wants_change("Есть у нас пепси?") and not W.wants_change("где не указан бренд")
    assert not W.wants_change("Pepsi narxi qancha? Oʻzgartirmang") and not W.wants_change("Не меняй цену пепси")
    assert W.ui_meant("Переключи на узбекский") == ("lang", "uz")
    assert W.ui_meant("Кириллицага ўтказинг") == ("lang", "uz-Cyrl")
    assert W.ui_meant("ночной режим включи") == ("theme", "dark")
    assert W.ui_meant("Переименуй хлеб белый в Хлеб пшеничный") is None
    assert W.ui_meant("Почему язык узбекский?") is None
    assert W.plural("napitki") and W.plural("ichimliklarni") and not W.plural("kefiru")
