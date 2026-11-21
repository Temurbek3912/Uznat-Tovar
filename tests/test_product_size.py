"""Размер товара из названия и ответ «уточните размер» (app/services/product_size.py, routes_catalog._check_size)."""
from app.api import routes_catalog
from app.services import product_namer as pn
import io
import json
import types

from app.services.product_size import find_sizes, name_without_size, same_size, size_label, size_of, size_variants


def test_sizes_are_read_in_any_writing():
    assert size_of("Pepsi 0,5 л") == ("l", 0.5)
    assert size_of("PEPSI 1.5L") == ("l", 1.5)
    assert size_of("Coca-Cola 500ml") == ("l", 0.5)
    assert size_of("Fanta 330 мл") == ("l", 0.33)
    assert size_of("Hydrolife 1 литр") == ("l", 1.0)
    assert size_of("Suv 0,5 litr") == ("l", 0.5)
    assert size_of("Fanta 25cl") == ("l", 0.25)
    assert size_of("Twix 82 г (2 x 41 г)") == ("g", 82.0)
    assert size_of("Сахар 1 кг") == ("g", 1000.0)
    assert size_of("Pepsi") is None
    assert size_of("Яйца 10 шт") == ("pcs", 10.0)
    assert find_sizes("Чай 25 пакетиков 50 г") == [("g", 50.0)]
    assert same_size(size_of("0,5 л"), size_of("500 мл")) and not same_size(size_of("0,5 л"), size_of("1,5 л"))


def test_name_without_size_joins_latin_and_cyrillic():
    assert name_without_size("Pepsi 0,5 л") == name_without_size("Пепси 1,5 литра") == {"pepsi"}


def test_size_variants_only_same_name_other_size():
    first = {"id": "p05", "name": "Pepsi 0,5 л", "brand": "Pepsi"}
    others = [first, {"id": "p15", "name": "Pepsi 1,5 л", "brand": "Pepsi"},
              {"id": "z15", "name": "Pepsi Zero 1,5 л", "brand": "Pepsi"},          # другой вкус — не размер
              {"id": "p05b", "name": "Pepsi 500 мл", "brand": "Pepsi"},              # тот же размер другой записью
              {"id": "c15", "name": "Coca-Cola 1,5 л", "brand": "Coca-Cola"}]
    assert [c["id"] for c in size_variants(first, others)] == ["p15"]



def test_size_missing_in_one_name_is_another_size_but_missing_in_both_is_not():
    # «Pepsi» и «Pepsi 1,5 л» по фото не различить: у одного из них размер просто не записан (замер 2026-11-21)
    others = [{"id": "p05", "name": "Pepsi 0,5 л", "brand": "Pepsi"}, {"id": "z15", "name": "Pepsi Zero 1,5 л", "brand": "Pepsi"},
              {"id": "same", "name": "Pepsi", "brand": "pepsi"}]
    assert [c["id"] for c in size_variants({"id": "x", "name": "Pepsi", "brand": ""}, others)] == ["p05"]
    mayo = {"id": "m770", "name": 'Майонез "Провансаль" 770 г', "brand": "Махеевъ"}
    plain = {"id": "m", "name": "Майонез Провансаль", "brand": "Махеевъ"}
    assert size_variants(mayo, [plain]) == [plain] and size_variants(plain, [mayo]) == [mayo]
    assert size_variants(plain, [{**plain, "id": "m2"}]) == []                          # оба без размера — не размеры


SIZES = {"status": "matched", "by": "look", "score": 0.91,
         "product": {"id": "p05", "name": "Pepsi 0,5 л", "brand": "Pepsi", "score": 0.91},
         "candidates": [{"id": "p05", "name": "Pepsi 0,5 л", "brand": "Pepsi", "score": 0.91},
                        {"id": "p15", "name": "Pepsi 1,5 л", "brand": "Pepsi", "score": 0.88},
                        {"id": "c05", "name": "Coca-Cola 0,5 л", "brand": "Coca-Cola", "score": 0.70}]}


def _label(monkeypatch, guess, size=None, asked=None):
    monkeypatch.setattr(pn, "read_label", lambda *a, **k: guess)
    monkeypatch.setattr(pn, "agreed_kind", lambda g, *a, **k: g)

    def read_size(jpeg, options, settings, **kw):          # прямой вопрос об объёме — без настоящей модели
        if asked is not None:
            asked.append(list(options))
        return size
    monkeypatch.setattr(pn, "read_size", read_size)


def test_size_read_on_label_puts_that_size_first(monkeypatch):
    _label(monkeypatch, pn.Guess("Pepsi 1,5 л", "Pepsi", "vision", sure=True))
    out = routes_catalog.add_outside_name(dict(SIZES), b"j", [], "ru")
    assert out["status"] == "matched" and out["product"]["id"] == "p15" and out["size"] == "label"
    assert [c["id"] for c in out["candidates"]] == ["p15", "p05", "c05"]


def test_size_not_read_asks_to_choose_between_sizes(monkeypatch):
    _label(monkeypatch, None)                                                     # модель не уверена или её нет
    out = routes_catalog.add_outside_name(dict(SIZES), b"j", [], "ru")
    assert out["status"] == "uncertain" and out["size"] == "choose" and out["product"]["id"] == "p05"
    assert [c["id"] for c in out["candidates"]] == ["p05", "p15", "c05"]
    _label(monkeypatch, pn.Guess("Pepsi", "Pepsi", "vision", sure=True))          # марка прочитана, размер — нет
    assert routes_catalog.add_outside_name(dict(SIZES), b"j", [], "ru")["size"] == "choose"


def test_far_size_or_barcode_answer_is_untouched(monkeypatch):
    _label(monkeypatch, None)
    far = {**SIZES, "candidates": [SIZES["candidates"][0], {**SIZES["candidates"][1], "score": 0.70}]}
    assert routes_catalog.add_outside_name(far, b"j", [], "ru") is far             # другой размер далеко по счёту
    by_code = {**SIZES, "by": "barcode"}
    assert routes_catalog.add_outside_name(by_code, b"j", [], "ru") is by_code     # по штрихкоду размер точный


def test_size_not_written_in_close_candidate_asks_to_choose(monkeypatch):
    _label(monkeypatch, None)
    first = {"id": "m770", "name": 'Майонез "Провансаль" 770 г', "brand": "Махеевъ", "score": 0.90}
    plain = {"id": "m", "name": "Майонез Провансаль", "brand": "Махеевъ", "score": 0.87}
    other = {"id": "k", "name": "Кетчуп Чили 300g", "brand": "Махеевъ", "score": 0.80}
    res = {"status": "matched", "by": "look", "score": 0.90, "product": first, "candidates": [first, plain, other]}
    out = routes_catalog.add_outside_name(res, b"j", [], "ru")
    assert out["status"] == "uncertain" and out["size"] == "choose"
    assert [c["id"] for c in out["candidates"]] == ["m770", "m", "k"]


def test_size_label_is_what_the_model_is_asked_about():
    assert [size_label(size_of(t)) for t in ("Pepsi 0,5 л", "Pepsi 1.5L", "Coca-Cola Zero 222ml", "Oreo 95 г", "Кетчуп 1000 г")]         == ["0,5 л", "1,5 л", "222 мл", "95 г", "1 кг"]


def test_size_answered_by_the_direct_question_puts_that_size_first(monkeypatch):
    asked = []
    _label(monkeypatch, None, size="1,5 л", asked=asked)              # в общем названии размера нет, на вопрос — «1,5 л»
    out = routes_catalog.add_outside_name(dict(SIZES), b"j", [], "ru")
    assert out["status"] == "matched" and out["product"]["id"] == "p15" and out["size"] == "label"
    assert [c["id"] for c in out["candidates"]] == ["p15", "p05", "c05"] and asked == [["0,5 л", "1,5 л"]]


SETTINGS = types.SimpleNamespace(naming_enabled=True, naming_vision_url="http://127.0.0.1:1234/v1", naming_vision_model="m",
                                 naming_vision_timeout_seconds=40.0, naming_size_question=True)


def _jpeg():
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (64, 64), "red").save(out, format="JPEG")
    return out.getvalue()


J = _jpeg()


def _model(*answers, calls=None):
    replies = iter(answers)

    def post(url, payload, timeout):
        if calls is not None:
            calls.append((payload["response_format"]["json_schema"]["schema"], timeout))
        return {"choices": [{"message": {"content": json.dumps({"size": next(replies)})}}]}
    return post


def test_read_size_takes_the_answer_only_when_both_questions_agree(monkeypatch):
    monkeypatch.setattr(pn, "_label_pause_until", 0.0)
    calls = []
    assert pn.read_size(J, ["0,5 л", "1,5 л"], SETTINGS, http_post=_model("1.5 L", "1,5 л", calls=calls)) == "1,5 л"
    assert calls[1][0]["properties"]["size"]["enum"] == ["0,5 л", "1,5 л", "not_visible"]
    assert all(t == pn.LABEL_CHECK_TIMEOUT for _, t in calls)              # коротко, не 40 с
    assert pn.read_size(J, ["0,5 л", "1,5 л"], SETTINGS, http_post=_model("1,5 l", "0,5 л")) is None   # не совпали
    assert pn.read_size(J, ["0,5 л", "1,5 л"], SETTINGS, http_post=_model("1,5 l", "not_visible")) is None
    calls.clear()
    assert pn.read_size(J, ["0,5 л", "1,5 л"], SETTINGS, http_post=_model("100 g", calls=calls)) is None
    assert pn.read_size(J, ["0,5 л", "1,5 л"], SETTINGS, http_post=_model("", calls=calls)) is None
    assert len(calls) == 2                                                 # чужое число или пусто — второго вопроса нет


def test_read_size_is_off_by_setting_and_pauses_when_the_model_is_down(monkeypatch):
    monkeypatch.setattr(pn, "_label_pause_until", 0.0)
    calls = []
    off = types.SimpleNamespace(**{**vars(SETTINGS), "naming_size_question": False})
    assert pn.read_size(J, ["0,5 л"], off, http_post=_model("0,5 l", "0,5 л", calls=calls)) is None and calls == []
    tries = []

    def down(url, payload, timeout):
        tries.append(1)
        raise TimeoutError()
    assert pn.read_size(J, ["0,5 л"], SETTINGS, http_post=down) is None
    assert pn.read_size(J, ["0,5 л"], SETTINGS, http_post=down) is None and len(tries) == 1
