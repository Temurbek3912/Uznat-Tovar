"""«Глобальный ИИ» Gemini (app/services/gemini.py) и его работа вместе со своей моделью. Gemini — только заглушка: настоящих
запросов к Google нет. Плюс фильтр категории у «товаров без …» помощника («какие сладости без цены»)."""
from __future__ import annotations

import json
import types
from datetime import datetime

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("cryptography")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.core.crypto import generate_key  # noqa: E402
from app.core.security import CurrentUser, Role  # noqa: E402
from app.db import migrate  # noqa: E402
from app.db.models import Product  # noqa: E402
from app.services import assistant as A  # noqa: E402
from app.services import assistant_tools as T  # noqa: E402
from app.services import gemini as G  # noqa: E402
from app.services import product_namer as N  # noqa: E402

OWNER = CurrentUser(id="u1", username="owner", role=Role.ADMIN)
KEY = "AIzaSyTEST-not-a-real-key-0123456789ab"


class FakeHttp:
    """Заглушка сети Google: отвечает по очереди заданными ответами (или ошибками) и запоминает запросы."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, method, url, body, headers, timeout):
        self.calls.append({"method": method, "url": url, "body": body, "headers": headers})
        answer = self.answers.pop(0) if self.answers else {}
        if isinstance(answer, Exception):
            raise answer
        return answer


def _service(tmp_path, http=None, **kw) -> G.Gemini:
    return G.Gemini(media_root=tmp_path, secret_key=generate_key(), http=http or FakeHttp(), **kw)


def _content(obj) -> dict:
    return {"choices": [{"message": {"content": json.dumps(obj, ensure_ascii=False)}}]}


# --- ключ, лимит, ошибки --------------------------------------------------------------------------------------------------------

def test_key_is_stored_encrypted_and_never_shown(tmp_path):
    g = _service(tmp_path)
    assert g.state() == "no_key" and not g.usable()
    with pytest.raises(ValueError):
        g.save_key("ключ с пробелами")
    g.save_key("  " + KEY + " ")
    raw = (tmp_path / G.SETTINGS_FILE).read_text(encoding="utf-8")
    assert KEY not in raw and KEY[-4:] in raw
    status = g.status()
    assert KEY not in json.dumps(status) and status["key_tail"] == KEY[-4:]
    assert status["state"] == "ok" and status["enabled"] is True and status["photos"] is False, "фото — по умолчанию нет"
    assert g.api_key() == KEY
    g.remove_key()
    assert g.state() == "no_key" and g.api_key() == ""


def test_without_auth_secret_the_key_is_not_stored(tmp_path):
    g = G.Gemini(media_root=tmp_path, secret_key="", http=FakeHttp())
    with pytest.raises(G.GeminiError) as err:
        g.save_key(KEY)
    assert err.value.code == "no_secret" and not (tmp_path / G.SETTINGS_FILE).exists()


def test_key_goes_only_in_the_header_and_answers_have_room_to_think(tmp_path):
    http = FakeHttp(_content({"ok": True}))
    g = _service(tmp_path, http)
    g.save_key(KEY)
    g.chat({"model": "qwen", "max_tokens": 500, "messages": [{"role": "user", "content": "привет"}]})
    call = http.calls[0]
    assert call["url"].endswith("/openai/chat/completions") and KEY not in call["url"]
    assert call["headers"] == {"x-goog-api-key": KEY}
    assert call["body"]["model"] == g.model and call["body"]["max_tokens"] >= G.MIN_OUTPUT_TOKENS


def test_daily_limit_is_counted_and_not_overspent(tmp_path):
    clock = [1_800_000_000.0]
    g = _service(tmp_path, FakeHttp(_content({}), _content({}), _content({})), daily_limit=2, clock=lambda: clock[0])
    g.save_key(KEY)
    g.check_key()                                     # проверка ключа лимит не тратит
    assert g.used_today() == 0
    g.chat({"messages": []})
    g.chat({"messages": []})
    assert g.used_today() == 2 and g.state() == "limit" and not g.usable()
    with pytest.raises(G.GeminiError) as err:
        g.chat({"messages": []})
    assert err.value.code == "limit"
    clock[0] += 24 * 3600                             # новые сутки — снова можно
    assert g.used_today() == 0 and g.usable()


def test_google_errors_become_clear_states(tmp_path):
    clock = [1_800_000_000.0]
    http = FakeHttp(G.HttpStatus(429), OSError("нет сети"), G.HttpStatus(400, '{"error": {"message": "API key not valid"}}'))
    g = _service(tmp_path, http, clock=lambda: clock[0])
    g.save_key(KEY)
    for code in ("limit", "offline", "bad_key"):
        with pytest.raises(G.GeminiError) as err:
            g.chat({"messages": []})
        assert err.value.code == code and g.state() == code
        clock[0] += 3600                              # пауза прошла
    g.save_key(KEY)                                   # новый ключ — снова можно
    assert g.state() == "ok"


def test_photos_need_their_own_switch(tmp_path):
    g = _service(tmp_path, FakeHttp(_content({})))
    g.save_key(KEY)
    assert g.usable() and not g.usable(photo=True)
    with pytest.raises(G.GeminiError):
        g.chat({"messages": []}, photo=True)
    g.set_options(photos=True)
    assert g.usable(photo=True)
    g.set_options(enabled=False)
    assert not g.usable() and g.state() == "off"


# --- помощник: две модели одновременно -----------------------------------------------------------------------------------------

@pytest.fixture(name="Session")
def _session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}", connect_args={"check_same_thread": False})
    migrate.upgrade_to_head(engine)
    S = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with S() as db:
        rows = [
            Product(sku="SNIK", name="Сникерс 50 г", brand="Snickers", category="Сладости"),
            Product(sku="MARS", name="Марс 50 г", brand="Mars", category="Сладости", expected_price=8000),
            Product(sku="COLA", name="Кока-кола 0,5 л", brand="Coca-Cola", category="Напитки"),
            Product(sku="LAYS", name="Лейс 90 г", brand="Lays", category="Снеки"),
        ]
        for i, p in enumerate(rows):
            p.created_at = datetime(2026, 11, 1 + i)
            db.add(p)
        db.commit()
    yield S
    engine.dispose()


class FakeLocal:
    def __init__(self, *steps):
        self.steps, self.calls = list(steps), []

    def __call__(self, url, payload, timeout):
        self.calls.append(json.loads(json.dumps(payload)))
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return _content(step)


class FakeCloud:
    """Заглушка Gemini для помощника: тот же вход, что у своей модели."""

    timeout = 5.0

    def __init__(self, *steps, photos=False):
        self.steps, self.calls, self.photos = list(steps), [], photos

    def usable(self, *, photo=False):
        return self.photos or not photo

    def chat(self, payload, *, photo=False):
        self.calls.append({"payload": json.loads(json.dumps(payload)), "photo": photo})
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return _content(step)

    def search(self, prompt):
        self.calls.append({"search": prompt})
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return json.dumps(step)


SETTINGS = types.SimpleNamespace(naming_enabled=True, assistant_enabled=True, naming_vision_url="http://127.0.0.1:8090/v1",
                                 naming_vision_model="qwen3-vl-4b")


def _chat(Session, monkeypatch, text, local, cloud, settings=SETTINGS, lang="ru"):
    monkeypatch.setattr(G, "usable", lambda _settings, photo=False: cloud)
    with Session() as db:
        return A.chat(db, OWNER, [{"role": "user", "content": text}], lang, settings, http_post=local)


def test_without_gemini_everything_is_as_before(Session, monkeypatch):
    local = FakeLocal({"action": "answer", "tool": "", "args": {}, "text": "Здравствуйте!"})
    reply = _chat(Session, monkeypatch, "Привет", local, None)
    assert reply.text == "Здравствуйте!" and reply.engine == "local" and len(local.calls) == 1


def test_both_models_are_asked_and_a_good_gemini_answer_is_preferred(Session, monkeypatch):
    local = FakeLocal({"action": "answer", "tool": "", "args": {}, "text": "Ответ своей модели"})
    cloud = FakeCloud({"action": "answer", "tool": "", "args": {}, "text": "Ответ Gemini"})
    reply = _chat(Session, monkeypatch, "Привет", local, cloud)
    assert reply.text == "Ответ Gemini" and reply.engine == "gemini"
    assert cloud.calls and cloud.calls[0]["photo"] is False
    assert cloud.calls[0]["payload"]["response_format"]["type"] == "json_schema", "тот же запрос, что своей модели"


def test_gemini_failure_falls_back_to_the_local_model(Session, monkeypatch):
    local = FakeLocal({"action": "answer", "tool": "", "args": {}, "text": "Ответ своей модели"})
    cloud = FakeCloud(G.GeminiError("offline"))
    reply = _chat(Session, monkeypatch, "Привет", local, cloud)
    assert reply.text == "Ответ своей модели" and reply.engine == "local"


def test_with_gemini_the_assistant_works_without_a_local_model(Session, monkeypatch):
    no_local = types.SimpleNamespace(**{**vars(SETTINGS), "naming_vision_url": ""})
    cloud = FakeCloud({"action": "answer", "tool": "", "args": {}, "text": "Ответ Gemini"})
    reply = _chat(Session, monkeypatch, "Привет", FakeLocal(), cloud, settings=no_local)
    assert reply.text == "Ответ Gemini"
    monkeypatch.setattr(G, "usable", lambda _settings, photo=False: None)
    assert not A.available(no_local)
    with Session() as db, pytest.raises(A.AssistantUnavailable):
        A.chat(db, OWNER, [{"role": "user", "content": "Привет"}], "ru", no_local, http_post=FakeLocal())


# --- «какие сладости без цены» -------------------------------------------------------------------------------------------------

def test_missing_tool_filters_by_category(Session):
    with Session() as db:
        out = T.products_missing(db, {"field": "price", "category": "Сладости"})
        assert [i["sku"] for i in out["items"]] == ["SNIK"] and out["category"] == ["Сладости"]
        assert "Сладости" in out["summary"] and "1 из 2" in out["summary"]
        uz = T.products_missing(db, {"field": "price", "category": "shirinliklar"})
        assert [i["sku"] for i in uz["items"]] == ["SNIK"], "узбекское слово категории — по словарю"
        none = T.products_missing(db, {"field": "price", "category": "Игрушки"})
        assert none["total"] == 0 and "нет" in none["summary"]
        assert T.products_missing(db, {"field": "price"})["total"] == 3, "без категории — как раньше"


@pytest.mark.parametrize("question, lang", [("Какие сладости без цены?", "ru"), ("Qaysi shirinliklarning narxi yoʻq?", "uz"),
                                            ("Қайси ширинликларнинг нархи йўқ?", "uz-Cyrl")])
def test_the_category_the_owner_named_is_applied_even_if_the_model_forgets_it(Session, monkeypatch, question, lang):
    local = FakeLocal({"action": "tool", "tool": "products_missing", "args": {"field": "price"}, "text": ""},
                      {"action": "answer", "tool": "", "args": {}, "text": "Сникерс 50 г"})
    _chat(Session, monkeypatch, question, local, None, lang=lang)
    data = next(m["content"] for m in local.calls[1]["messages"] if "ДАННЫЕ инструмента products_missing" in m["content"])
    assert "SNIK" in data and "COLA" not in data and "LAYS" not in data


# --- распознавание неизвестного товара ------------------------------------------------------------------------------------------

NAMING = types.SimpleNamespace(naming_enabled=True, naming_barcode_lookup=True, naming_vision_url="", naming_vision_model="",
                               naming_min_confidence="high", naming_vision_timeout_seconds=5.0, naming_kind_model="")


def test_unknown_barcode_is_searched_in_google_without_the_photo(monkeypatch):
    monkeypatch.setattr(N, "lookup_barcode", lambda *a, **k: None)
    N._barcode_cache._d.clear()
    cloud = FakeCloud({"found": True, "name": "Choco Pie 30 г", "brand": "Orion", "confidence": "high"})
    guess = N.name_product(b"jpeg", ["4780001234562"], "ru", NAMING, cloud=cloud, classify=lambda *a, **k: None)
    assert guess is not None and guess.name == "Choco Pie 30 г" and guess.source == "gemini_search"
    assert [c for c in cloud.calls if "photo" in c] == [], "фото в Gemini не ушло — переключатель выключен"
    assert "4780001234562" in cloud.calls[0]["search"]


def test_unsure_search_is_not_a_guess(monkeypatch):
    monkeypatch.setattr(N, "lookup_barcode", lambda *a, **k: None)
    N._barcode_cache._d.clear()
    cloud = FakeCloud({"found": False, "name": "Что-то", "brand": "", "confidence": "low"})
    assert N.name_product(None, ["4780001234579"], "ru", NAMING, cloud=cloud) is None


def test_photo_goes_to_gemini_only_when_allowed(monkeypatch):
    monkeypatch.setattr(N, "lookup_barcode", lambda *a, **k: None)
    monkeypatch.setattr(N, "_shrink_jpeg", lambda jpeg: jpeg)
    cloud = FakeCloud({"is_product": True, "name": "Fanta 1 л", "brand": "Fanta", "confidence": "high", "kind": "drink"},
                      photos=True)
    guess = N.name_product(b"jpeg", [], "ru", NAMING, cloud=cloud, classify=lambda *a, **k: None)
    assert guess is not None and guess.name == "Fanta 1 л" and guess.source == "gemini"
    assert cloud.calls[0]["photo"] is True
