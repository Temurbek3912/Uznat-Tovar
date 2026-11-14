"""«Назвать любой товар» (документ 07, §10.6): штрихкод -> открытые базы, иначе локальная модель «зрение + текст», иначе честное
«не удалось определить». Сеть и модель подменяются — тесты работают офлайн."""
import io
import json
import types

import pytest
from PIL import Image

from app.api import routes_catalog
from app.api.routes_public import public_result
from app.services import product_namer as pn

SETTINGS = types.SimpleNamespace(naming_enabled=True, naming_barcode_lookup=True, naming_vision_url="http://127.0.0.1:1234/v1",
                                 naming_vision_model="m", naming_vision_timeout_seconds=5.0)


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(pn, "_barcode_cache", pn._Lru())


def _jpeg(size=(1200, 900)):
    out = io.BytesIO()
    Image.new("RGB", size, "red").save(out, format="JPEG")
    return out.getvalue()


def _vision(answer: dict | str, calls=None):
    def post(url, payload, timeout):
        if calls is not None:
            calls.append((url, json.loads(json.dumps(payload)), timeout))   # программа очищает запрос после отправки
        content = answer if isinstance(answer, str) else json.dumps(answer)
        return {"choices": [{"message": {"content": content}}]}
    return post


def _off(products: dict, calls=None):
    def get(url, timeout):
        if calls is not None:
            calls.append(url)
        for base, product in products.items():
            if url.startswith(base):
                return {"status": 1, "product": product}
        return {"status": 0}
    return get


def test_barcode_found_in_an_open_database_names_the_product_and_only_the_code_leaves():
    calls = []
    get = _off({"https://world.openfoodfacts.org": {"product_name": "Snickers", "brands": "Mars Wrigley Confectionery UK, Snickers"}}, calls)
    guess = pn.name_product(_jpeg(), ["5000159461122"], "ru", SETTINGS, http_get=get, http_post=_vision({"x": 1}))
    assert guess == pn.Guess("Snickers", "Snickers", "barcode_db", sure=True)
    assert calls and all("5000159461122" in u for u in calls)          # наружу — только номер штрихкода


def test_barcode_lookup_tries_the_other_open_databases_and_caches_answers():
    calls = []
    get = _off({"https://world.openbeautyfacts.org": {"product_name_ru": "Крем для рук", "brands": "Nivea"}}, calls)
    assert pn.lookup_barcode("4005900000000", "ru", http_get=get).name == "Крем для рук"
    assert pn.lookup_barcode("4005900000000", "ru", http_get=get).brand == "Nivea"
    assert len(calls) == 2                                              # food -> beauty; второй раз — из памяти


def test_network_failure_is_not_cached_and_does_not_break_anything():
    def broken(url, timeout):
        raise OSError("нет интернета")
    assert pn.lookup_barcode("5000159461122", "ru", http_get=broken) is None
    hit, _ = pn._barcode_cache.get(("5000159461122", "ru"))
    assert not hit


def test_not_a_barcode_is_never_sent():
    calls = []
    assert pn.lookup_barcode("abc", "ru", http_get=_off({}, calls)) is None and calls == []


def test_vision_names_the_product_in_the_page_language_with_a_small_frame():
    calls = []
    post = _vision({"is_product": True, "name": "Red Bull энергетик 0,25 л", "brand": "Red Bull", "confidence": "high"}, calls)
    guess = pn.name_product(_jpeg((3000, 2000)), [], "uz-Cyrl", SETTINGS, http_get=_off({}), http_post=post)
    assert guess == pn.Guess("Red Bull энергетик 0,25 л", "Red Bull", "vision", sure=True)
    url, payload, timeout = calls[0]
    assert url == "http://127.0.0.1:1234/v1/chat/completions" and timeout == 5.0
    assert "Uzbek (Cyrillic script)" in payload["messages"][0]["content"][0]["text"]


@pytest.mark.parametrize("answer", [
    {"is_product": True, "name": "Что-то", "brand": "", "confidence": "low"},
    {"is_product": False, "name": "пол", "brand": "", "confidence": "high"},
    {"is_product": True, "name": "  ", "brand": "", "confidence": "high"},
    "не JSON вовсе",
])
def test_unsure_or_not_a_product_is_an_honest_could_not_determine(answer):
    assert pn.name_product(_jpeg(), [], "ru", SETTINGS, http_get=_off({}), http_post=_vision(answer)) is None


def test_thinking_text_around_json_is_tolerated_and_medium_is_a_guess_but_not_sure():
    text = '<think>хм</think>```json\n{"is_product": true, "name": "Шоколад", "brand": "", "confidence": "medium"}\n```'
    guess = pn.name_product(_jpeg(), [], "ru", SETTINGS, http_get=_off({}), http_post=_vision(text))
    assert guess.name == "Шоколад" and guess.sure is False


def test_model_unavailable_or_naming_switched_off_gives_none():
    def down(url, payload, timeout):
        raise ConnectionRefusedError()
    assert pn.name_product(_jpeg(), [], "ru", SETTINGS, http_get=_off({}), http_post=down) is None
    off = types.SimpleNamespace(**{**vars(SETTINGS), "naming_enabled": False})
    assert pn.name_product(_jpeg(), ["5000159461122"], "ru", off, http_get=_off({"https://world.openfoodfacts.org": {"product_name": "X"}}),
                           http_post=_vision({"x": 1})) is None
    no_url = types.SimpleNamespace(**{**vars(SETTINGS), "naming_vision_url": ""})
    assert pn.name_product(_jpeg(), [], "ru", no_url, http_get=_off({}), http_post=_vision({"x": 1})) is None


def test_names_overlap_compares_latin_and_cyrillic():
    assert pn.names_overlap(pn.Guess("Coca-Cola Zero"), {"name": "Кока-Кола 0,5 л", "brand": ""})
    assert not pn.names_overlap(pn.Guess("Red Bull Energy Drink", "Red Bull"), {"name": "Coca-Cola 0,33 л", "brand": "Coca-Cola"})


def test_normalize_lang():
    assert [pn.normalize_lang(x) for x in ("uz-cyrl", "EN", "", None, "de")] == ["uz-Cyrl", "en", "ru", "ru", "ru"]


# ---------------------------------------------------------------- решение «нет в базе» поверх ответа поиска по каталогу

def _with_guess(monkeypatch, guess):
    monkeypatch.setattr(pn, "name_product", lambda *a, **k: guess)


def test_unknown_with_a_name_becomes_not_in_catalog(monkeypatch):
    _with_guess(monkeypatch, pn.Guess("Snickers", "Snickers", "vision", sure=False))
    out = routes_catalog.add_outside_name({"status": "unknown", "product": None, "candidates": []}, b"j", [], "ru")
    assert out["status"] == "not_in_catalog" and out["guess"] == {"name": "Snickers", "brand": "Snickers", "source": "vision"}


def test_unknown_without_a_name_stays_unknown_and_found_products_are_untouched(monkeypatch):
    _with_guess(monkeypatch, None)
    assert routes_catalog.add_outside_name({"status": "unknown"}, b"j", [], "ru") == {"status": "unknown"}
    _with_guess(monkeypatch, pn.Guess("X", sure=True))
    matched = {"status": "matched", "product": {"id": "1", "name": "Nutella"}}
    assert routes_catalog.add_outside_name(matched, b"j", [], "ru") is matched
    demo = {"status": "unknown", "demo_mode": True}
    assert routes_catalog.add_outside_name(demo, b"j", [], "ru") is demo          # заглушка вместо модели: «нет в базе» не говорим


def test_uncertain_is_overruled_only_by_a_sure_name_of_a_different_product(monkeypatch):
    similar = {"status": "uncertain", "product": {"id": "c", "name": "Coca-Cola 0,33 л", "brand": "Coca-Cola"},
               "candidates": [{"id": "c", "name": "Coca-Cola 0,33 л", "brand": "Coca-Cola"}, {"id": "p", "name": "Pringles", "brand": ""}]}
    _with_guess(monkeypatch, pn.Guess("Red Bull Energy Drink", "Red Bull", "vision", sure=True))
    out = routes_catalog.add_outside_name(similar, b"j", [], "ru")
    assert out["status"] == "not_in_catalog" and out["candidates"] == [] and out["product"] is None
    _with_guess(monkeypatch, pn.Guess("Red Bull Energy Drink", "Red Bull", "vision", sure=False))
    assert routes_catalog.add_outside_name(similar, b"j", [], "ru") is similar      # не уверена — выбор за человеком
    _with_guess(monkeypatch, pn.Guess("Coca-Cola Classic", "Coca-Cola", "vision", sure=True))
    assert routes_catalog.add_outside_name(similar, b"j", [], "ru") is similar      # модель назвала тот же товар


def test_public_answer_shows_only_name_and_brand_of_the_outside_product():
    answer = public_result(None, {"status": "not_in_catalog", "guess": {"name": "Snickers", "brand": "Mars", "source": "barcode_db"},
                                  "barcode": {"code": "5000159461122"}, "score": 0.3})
    assert answer == {"status": "not_in_catalog", "by": None, "product": None, "candidates": [],
                      "guess": {"name": "Snickers", "brand": "Mars"}}
