"""Вид товара — «это напиток», «это обувь» (документ 07, §10.7): большая модель называет вид в том же ответе, что и название;
без неё — малая модель (здесь подменена); покупателю — только код вида, подпись — в словарях страниц."""
import io
import json
import re
import types
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.api import routes_catalog
from app.api.routes_public import public_result
from app.pipeline import kind_classifier as kc
from app.services import product_kinds as pk
from app.services import product_namer as pn

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = types.SimpleNamespace(naming_enabled=True, naming_barcode_lookup=False, naming_vision_url="http://127.0.0.1:1234/v1",
                                 naming_vision_model="m", naming_vision_timeout_seconds=5.0, naming_min_confidence="high",
                                 naming_kind_model="")


def _jpeg():
    out = io.BytesIO()
    Image.new("RGB", (64, 64), "blue").save(out, format="JPEG")
    return out.getvalue()


def _vision(answer, calls=None):
    def post(url, payload, timeout):
        if calls is not None:
            calls.append(json.loads(json.dumps(payload)))
        return {"choices": [{"message": {"content": json.dumps(answer)}}]}
    return post


def _down(url, payload, timeout):
    raise ConnectionRefusedError()


def test_vision_is_asked_for_a_kind_from_the_closed_list():
    calls = []
    pn.name_product(_jpeg(), [], "ru", SETTINGS, http_post=_vision({"is_product": False}, calls))
    schema = calls[0]["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["kind"]["enum"] == [*pk.KIND_IDS, "other"] and "kind" in schema["required"]
    prompt = calls[0]["messages"][0]["content"][0]["text"]
    assert all(f"{k} = " in prompt for k in pk.KIND_IDS)


def test_sure_name_keeps_the_kind_and_unsure_name_gives_only_the_kind():
    sure = {"is_product": True, "name": "Santal Artesian Water", "brand": "Santal", "confidence": "high", "kind": "drink"}
    g = pn.name_product(_jpeg(), [], "ru", SETTINGS, http_post=_vision(sure))
    assert (g.name, g.kind, g.sure) == ("Santal Artesian Water", "drink", True)
    unsure = {**sure, "confidence": "medium", "kind": "footwear"}
    g = pn.name_product(_jpeg(), [], "ru", SETTINGS, http_post=_vision(unsure))
    assert (g.name, g.brand, g.kind, g.sure) == ("", "", "footwear", False)


@pytest.mark.parametrize("answer", [
    {"is_product": False, "name": "", "brand": "", "confidence": "low", "kind": "drink"},     # «не товар» — вид не называется
    {"is_product": True, "name": "", "brand": "", "confidence": "low", "kind": "other"},
    {"is_product": True, "name": "", "brand": "", "confidence": "low", "kind": "weapons"},   # код вне списка
])
def test_no_kind_when_not_a_product_or_other(answer):
    assert pn.name_product(_jpeg(), [], "ru", SETTINGS, http_post=_vision(answer)) is None


def test_small_model_is_used_only_when_the_big_one_is_unavailable():
    asked = []

    def small(jpeg, settings, min_score=None):
        asked.append(min_score)
        return kc.KindScore("footwear", 0.9)
    settings = types.SimpleNamespace(**{**vars(SETTINGS), "naming_kind_model": "x"})
    g = pn.name_product(_jpeg(), [], "ru", settings, http_post=_down, classify=small)
    assert (g.name, g.kind, g.source) == ("", "footwear", "kind_model")
    # большая модель ответила «не товар» — малую не спрашиваем
    assert pn.name_product(_jpeg(), [], "ru", settings, http_post=_vision({"is_product": False}), classify=small) is None
    assert len(asked) == 1
    assert pn.name_product(_jpeg(), [], "ru", settings, http_post=_down, classify=lambda j, s, min_score=None: None) is None
    assert asked == [None]           # без большой модели — порог из настроек


def _small(kind):
    return lambda jpeg, settings, min_score=None: kc.KindScore(kind, 0.3) if kind else None


def test_with_both_models_the_kind_is_shown_only_when_they_agree():
    unsure = {"is_product": True, "name": "X", "brand": "", "confidence": "low", "kind": "drink"}
    sure = {**unsure, "name": "Santal Water", "confidence": "high"}
    agree = pn.name_product(_jpeg(), [], "ru", SETTINGS, http_post=_vision(unsure), classify=_small("drink"))
    assert (agree.name, agree.kind) == ("", "drink")                      # согласны (уверенность малой не важна)
    assert pn.name_product(_jpeg(), [], "ru", SETTINGS, http_post=_vision(unsure), classify=_small("dairy")) is None
    named = pn.name_product(_jpeg(), [], "ru", SETTINGS, http_post=_vision(sure), classify=_small("dairy"))
    assert (named.name, named.kind) == ("Santal Water", "")             # название остаётся, спорный вид — нет
    alone = pn.name_product(_jpeg(), [], "ru", SETTINGS, http_post=_vision(unsure))   # малая модель выключена
    assert alone.kind == "drink"


def test_kind_only_guess_turns_unknown_into_not_in_catalog_but_never_overrules_uncertain(monkeypatch):
    monkeypatch.setattr(pn, "name_product", lambda *a, **k: pn.Guess(name="", source="vision", kind="drink"))
    out = routes_catalog.add_outside_name({"status": "unknown", "product": None, "candidates": []}, b"j", [], "ru")
    assert out["status"] == "not_in_catalog" and out["guess"]["kind"] == "drink" and out["guess"]["name"] == ""
    similar = {"status": "uncertain", "product": {"id": "c", "name": "Coca-Cola"}, "candidates": [{"id": "c", "name": "Coca-Cola"}]}
    assert routes_catalog.add_outside_name(similar, b"j", [], "ru") is similar


def test_public_answer_carries_only_a_known_kind_code():
    ok = public_result(None, {"status": "not_in_catalog", "guess": {"name": "", "brand": "x", "source": "kind_model", "kind": "drink"}})
    assert ok["guess"] == {"name": "", "brand": "", "kind": "drink"}
    bad = public_result(None, {"status": "not_in_catalog", "guess": {"name": "", "kind": "<script>"}})
    assert bad["status"] == "unknown" and "guess" not in bad


def test_scores_from_logits_takes_best_prompt_per_kind_and_none_wins_for_non_products():
    groups = ["drink", "drink", "footwear", kc.NONE]
    s = kc.scores_from_logits(groups, np.array([1.0, 5.0, 4.0, 0.0]))
    assert s.kind == "drink" and s.runner_up == "footwear" and 0.5 < s.score < 1
    s = kc.scores_from_logits(groups, np.array([0.0, 0.0, 0.0, 9.0]))
    assert s.kind is None


def test_label_texts_cover_every_kind_and_the_not_a_product_group():
    groups, texts = kc.label_texts("siglip")
    assert set(groups) == {*pk.KIND_IDS, kc.NONE} and all(t.startswith("This is a photo of ") for t in texts)


def test_classify_applies_the_threshold(monkeypatch):
    class Fake:
        repo = "x"

        def classify_jpeg(self, jpeg):
            return kc.KindScore("toy", 0.45)
    monkeypatch.setattr(kc, "get_classifier", lambda s: Fake())
    assert kc.classify(b"j", types.SimpleNamespace(naming_kind_min_score=0.5)) is None
    assert kc.classify(b"j", types.SimpleNamespace(naming_kind_min_score=0.4)).kind == "toy"


def test_off_categories_map_to_kinds_in_priority_order():
    assert pk.kind_for_categories(["en:beverages", "en:dairies"]) == "dairy"       # кефир — молочное, хоть и напиток
    assert pk.kind_for_categories(["en:beverages"]) == "drink"
    assert pk.kind_for_categories(["en:unknown"]) is None
    assert {k for _, k in pk.OFF_CATEGORY_KINDS} <= set(pk.KIND_IDS)
    assert set(pk.VISION_HINTS) == set(pk.KIND_IDS)


@pytest.mark.parametrize("lang", ["ru", "en", "uz", "uz-cyrl"])
def test_every_kind_has_a_label_in_every_language(lang):
    text = (ROOT / "frontend" / f"i18n.{lang}.js").read_text(encoding="utf-8")
    keys = set(re.findall(r'"(kind\.[a-z_]+)":', text))
    assert keys == {f"kind.{k}" for k in pk.KIND_IDS}
    assert all(f'"ticket.{k}"' in text for k in ("kind_title", "kind_label", "kind_no_name"))
