"""Публичный вход покупателя (app/api/routes_public.py): без входа, только публичные поля, кадр только в памяти (D-10а),
ограничение частоты, понятные ошибки без технических подробностей; вход покупателя (APP_LISTENER=customer) — без маршрутов
сотрудников и без их страниц."""
from __future__ import annotations

import importlib
import json
import io
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("httpx")

import numpy as np  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core import frame_store as fs  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.error_handlers import install_error_handlers  # noqa: E402
from app.core.secure_transport import SecureTransportMiddleware  # noqa: E402
from app.db.models import Base, Product, ProductBarcodeORM, Store  # noqa: E402
from app.db.session import get_db  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EAN = "4780001234562"


def jpeg(seed=1, w=400, h=300) -> bytes:
    rng = np.random.default_rng(seed)
    buf = io.BytesIO()
    Image.fromarray(rng.integers(0, 255, (h, w, 3), dtype=np.uint8)).save(buf, "JPEG")
    return buf.getvalue()


def barcode_jpeg(code=EAN) -> bytes:
    import cv2

    from app.services import barcode as bc
    bars = cv2.cvtColor(bc.render(code, module=4, height=140), cv2.COLOR_GRAY2RGB)
    canvas = np.full((400, bars.shape[1] + 80, 3), 235, np.uint8)
    canvas[130:130 + bars.shape[0], 40:40 + bars.shape[1]] = bars
    buf = io.BytesIO()
    Image.fromarray(canvas).save(buf, "JPEG", quality=95)
    return buf.getvalue()


class FakeAsync:
    def __init__(self, result):
        self.result = result

    def get(self, timeout=None, propagate=True, disable_sync_subtasks=True):
        if isinstance(self.result, TimeoutError):
            raise self.result
        return self.result


class Env:
    pass


@pytest.fixture
def env(monkeypatch, tmp_path):
    e = Env()
    e.queued, e.answer = [], None
    e.store = fs.MemoryFrameStore()
    fs.set_frame_store(e.store)

    def apply_async(args=(), kwargs=None, **options):
        e.queued.append(args)
        e.seen_frame = e.store.take(fs.frame_id_of(args[0]))          # как воркер: забрать и удалить
        return FakeAsync(e.answer(e) if callable(e.answer) else e.answer)

    fake = types.ModuleType("app.worker")
    task = types.SimpleNamespace(apply_async=apply_async, delay=lambda *a: None)
    fake.identify_product_task = task
    fake.add_catalog_product_task = task
    fake.add_catalog_photos_task = task
    fake.celery_app = types.SimpleNamespace(AsyncResult=lambda _id: None)
    monkeypatch.setitem(sys.modules, "app.worker", fake)
    for name in ("app.api.routes_catalog", "app.api.routes_public"):
        sys.modules.pop(name, None)
    routes_public = importlib.import_module("app.api.routes_public")
    routes_public.rate_limit.reset()
    e.routes = routes_public
    # «Не узнали» иначе идёт к настоящей модели «зрение + текст» (LM Studio на этом ПК): когда она занята, 12 снимков не укладывались
    # в минуту и test_rate_limit_per_device падал (полный прогон 2026-11-16). Название товара вне базы здесь не проверяется.
    from app.services import product_namer
    monkeypatch.setattr(product_namer, "name_product", lambda *a, **k: None)
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path / "media"))

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    install_error_handlers(app)
    settings = get_settings().model_copy(update={"auth_require_secure_transport": True})
    app.add_middleware(SecureTransportMiddleware, settings_fn=lambda: settings)        # как в app/main.py: HTTP по Wi-Fi
    app.include_router(routes_public.router)
    app.dependency_overrides[get_db] = override_get_db
    e.client = TestClient(app, base_url="http://192.168.1.50:8080")
    with Session() as db:
        db.add(Store(name="Магазин у дома", address="ул. Секретная, 5", format="supermarket"))
        cola = Product(sku="COLA", name="Кола 0,5 л", brand="Coca-Cola", category="Напитки", expected_price=9900.0,
                       embedding_index_ref="internal-ref")
        fanta = Product(sku="FANTA", name="Фанта 0,5 л", brand="Coca-Cola", category="Напитки")
        db.add_all([cola, fanta])
        db.commit()
        db.add(ProductBarcodeORM(barcode=EAN, product_id=cola.id))
        db.commit()
        e.cola, e.fanta = cola.id, fanta.id
    e.tmp_path = tmp_path
    e.Session = Session
    yield e
    fs.set_frame_store(None)
    routes_public.rate_limit.reset()
    routes_public.browse_limit.reset()


def identify(e, data=None, *, multipart=False, ip=None):
    data = jpeg() if data is None else data
    headers = {}
    if multipart:
        return e.client.post("/api/v1/public/identify", files={"file": ("a.jpg", data, "image/jpeg")})
    headers["Content-Type"] = "image/jpeg"
    return e.client.post("/api/v1/public/identify", content=data, headers=headers)


def test_known_barcode_gives_only_public_fields_without_login_over_plain_http(env):
    resp = identify(env, barcode_jpeg())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"status": "matched", "by": "barcode", "candidates": [],
                    "product": {"name": "Кола 0,5 л", "brand": "Coca-Cola", "category": "Напитки", "price": 9900.0,
                                "details": {}}}
    for secret in (env.cola, "COLA", "internal-ref", EAN, "score"):
        assert secret not in resp.text
    assert env.queued == []                                            # штрихкод — без очереди и без модели


def test_look_path_passes_only_a_frame_number_and_the_frame_is_gone_after_the_answer(env):
    env.answer = lambda e: {"status": "matched", "product": {"id": e.cola, "name": "x"}, "score": 0.93, "candidates": [], "demo_mode": False}
    data = jpeg(3)
    resp = identify(env, data, multipart=True)                         # прежний способ (форма) тоже без диска
    assert resp.status_code == 200 and resp.json()["product"]["name"] == "Кола 0,5 л" and resp.json()["by"] == "look"
    (ref,), = env.queued
    assert fs.is_frame_ref(ref) and env.seen_frame                   # в очереди — номер, кадр воркер забрал из памяти
    assert env.store.count() == 0
    assert not (env.tmp_path / "media").exists()                        # ни одного файла кадра


def test_uncertain_shows_public_candidates_and_unknown_or_demo_say_not_found(env):
    env.answer = lambda e: {"status": "uncertain", "product": {"id": e.cola}, "candidates": [{"id": e.cola}, {"id": e.fanta}, {"id": "ghost"}],
                            "demo_mode": False}
    body = identify(env).json()
    assert body["status"] == "uncertain" and [c["name"] for c in body["candidates"]] == ["Кола 0,5 л", "Фанта 0,5 л"]
    assert all(set(c) == {"name", "brand", "category", "price", "details"} for c in body["candidates"])
    env.answer = {"status": "unknown", "product": None, "candidates": [{"id": "x"}], "demo_mode": False}
    assert identify(env).json() == {"status": "unknown", "by": None, "product": None, "candidates": []}
    env.answer = {"status": "empty_catalog", "product": None, "candidates": [], "demo_mode": False}
    assert identify(env).json()["status"] == "unknown"
    env.answer = lambda e: {"status": "matched", "product": {"id": e.cola}, "candidates": [], "demo_mode": True}
    assert identify(env).json()["status"] == "unknown"                  # заглушка вместо модели — честное «не узнали»


def test_each_answer_leaves_one_scan_row_without_frame_or_customer_data(env):
    """«Почему не узнал» (решение владельца 2026-11-17): итог скана — да; кадр, адрес, устройство — нет; заглушка не считается."""
    from app.db.models import ScanEventORM

    assert identify(env, barcode_jpeg()).status_code == 200
    env.answer = lambda e: {"status": "uncertain", "product": {"id": e.fanta}, "candidates": [{"id": e.fanta}], "demo_mode": False}
    identify(env)
    env.answer = {"status": "unknown", "product": None, "candidates": [], "demo_mode": False}
    identify(env)
    env.answer = lambda e: {"status": "matched", "product": {"id": e.cola}, "candidates": [], "demo_mode": True}
    identify(env)
    with env.Session() as db:
        rows = db.query(ScanEventORM).all()
        got = [(r.status, r.via, r.product_id) for r in rows]
    assert sorted(got, key=str) == sorted([("matched", "barcode", env.cola), ("uncertain", "look", env.fanta), ("unknown", None, None)], key=str)
    assert set(ScanEventORM.__table__.columns.keys()) == {"id", "at", "status", "via", "product_id", "guess_name", "guess_kind"}


def test_server_problems_are_explained_without_technical_details(env):
    env.answer = TimeoutError("no answer")
    resp = identify(env)
    assert resp.status_code == 503 and resp.json()["code"] == "public_unavailable"
    assert "docker" not in resp.text.lower() and "redis" not in resp.text.lower()
    assert env.store.count() == 0
    bad = identify(env, b"not an image")
    assert bad.status_code == 422
    wrong = env.client.post("/api/v1/public/identify", content=b"{}", headers={"Content-Type": "application/json"})
    assert wrong.status_code == 415 and wrong.json()["code"] == "frame_type_unsupported"


def test_rate_limit_per_device(env):
    env.answer = {"status": "unknown", "product": None, "candidates": [], "demo_mode": False}
    codes = [identify(env).status_code for _ in range(env.routes.PER_DEVICE_PER_MINUTE)]
    assert codes == [200] * env.routes.PER_DEVICE_PER_MINUTE
    blocked = identify(env)
    assert blocked.status_code == 429 and blocked.json()["code"] == "public_rate_limited" and int(blocked.headers["Retry-After"]) > 0


def test_store_info_has_name_and_languages_but_not_the_address(env):
    body = env.client.get("/api/v1/public/store").json()
    assert body == {"name": "Магазин у дома", "languages": ["ru", "uz", "uz-Cyrl", "en"],
                    "card_fields": ["price", "net_content", "manufacturer", "country", "in_stock"]}


# ------------------------------------------------------------------ вход покупателя: что на нём есть, а чего нет

CHECK_CUSTOMER_ENTRANCE = r"""
import sys, types
fake = types.ModuleType("app.worker")
task = types.SimpleNamespace(apply_async=lambda *a, **k: None, delay=lambda *a: None)
fake.identify_product_task = fake.add_catalog_product_task = fake.add_catalog_photos_task = task
fake.celery_app = types.SimpleNamespace(AsyncResult=lambda _id: None)
sys.modules["app.worker"] = fake
from fastapi.testclient import TestClient
from app.main import app, CUSTOMER_ENTRANCE
assert CUSTOMER_ENTRANCE
c = TestClient(app, base_url="http://192.168.1.50:8080")
schema = set(app.openapi()["paths"])
assert {"/api/v1/public/identify", "/api/v1/public/store"} <= schema, schema
assert not [p for p in schema if not p.startswith("/api/v1/public/") and p.startswith("/api/")], schema
for method, path in (("get", "/api/v1/auth/me"), ("get", "/api/v1/catalog/products"), ("get", "/api/v1/users"),
                     ("get", "/api/v1/stores"), ("get", "/api/v1/admin/system")):
    assert getattr(c, method)(path).status_code in (403, 404), path          # 403 — раньше маршрута: незащищённый канал
assert c.get("/ui/shop.html").status_code == 200 and c.get("/ui/shop.js").status_code == 200
assert c.get("/ui/shop-list.html").status_code == 200 and c.get("/ui/shop-list.js").status_code == 200
assert {"/api/v1/public/products", "/api/v1/public/categories", "/api/v1/public/photo/{ref}"} <= schema, schema
for page in ("/ui/admin.html", "/ui/index.html", "/ui/products.html", "/ui/admin.js", "/ui/auth.js", "/ui/", "/ui/../app/main.py"):
    assert c.get(page).status_code == 404, page
assert c.post("/api/v1/auth/login", data={"username": "a", "password": "b"}).status_code in (403, 404, 405)
assert c.get("/", follow_redirects=False).headers["location"] == "/ui/shop.html"
print("OK")
"""


def test_customer_entrance_has_only_the_public_api_and_the_shop_page():
    env = {**os.environ, "APP_LISTENER": "customer", "AUTH_THROTTLE_BACKEND": "memory", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([sys.executable, "-c", CHECK_CUSTOMER_ENTRANCE], cwd=ROOT, env=env, capture_output=True, text=True,
                          encoding="utf-8", timeout=120)
    assert proc.returncode == 0 and "OK" in proc.stdout, proc.stdout + proc.stderr


def test_barcode_of_an_archived_product_is_not_a_match(env):
    """Товар в архиве (миграция 0006): его штрихкод не даёт карточку — дальше поиск по виду, как для чужого товара."""
    with env.Session() as db:
        db.get(Product, env.cola).status = "archived"
        db.commit()
    env.answer = lambda e: {"status": "unknown", "product": None, "score": 0.1, "candidates": [], "demo_mode": False}
    resp = identify(env, barcode_jpeg())
    assert resp.status_code == 200
    assert resp.json()["status"] != "matched" and "Кола" not in resp.text
    assert len(env.queued) == 1                                        # штрихкод не узнан — кадр ушёл на поиск по виду


# ------------------------------------------------------------------ список товаров покупателя и поля карточки магазина (шаг 3)

def _browse_reset(env):
    env.routes.browse_limit.reset()


def test_product_list_search_category_paging_and_no_archived(env):
    _browse_reset(env)
    with env.Session() as db:
        db.add_all([Product(sku="WATER", name="Вода Ёлочка", brand="Nestle", category="Вода"),
                    Product(sku="OLD", name="Старая кола", brand="X", category="Напитки", status="archived")])
        db.commit()
    body = env.client.get("/api/v1/public/products").json()
    assert body["total"] == 3 and [i["name"] for i in body["items"]] == ["Вода Ёлочка", "Кола 0,5 л", "Фанта 0,5 л"]
    assert body["next_offset"] is None
    for secret in ("COLA", "internal-ref", EAN, "Старая"):
        assert secret not in str(body)
    assert all(set(i) == {"name", "brand", "category", "price", "details", "ref", "has_photo"} for i in body["items"])
    assert [i["name"] for i in env.client.get("/api/v1/public/products", params={"q": "елочка"}).json()["items"]] == ["Вода Ёлочка"]
    assert [i["name"] for i in env.client.get("/api/v1/public/products", params={"q": EAN}).json()["items"]] == ["Кола 0,5 л"]
    assert env.client.get("/api/v1/public/products", params={"q": "FANTA"}).json()["total"] == 0   # по артикулу — нет (служебный)
    cat = env.client.get("/api/v1/public/products", params={"category": "Напитки"}).json()
    assert [i["name"] for i in cat["items"]] == ["Кола 0,5 л", "Фанта 0,5 л"]
    page = env.client.get("/api/v1/public/products", params={"limit": 1, "offset": 1}).json()
    assert [i["name"] for i in page["items"]] == ["Кола 0,5 л"] and page["next_offset"] == 2
    cats = env.client.get("/api/v1/public/categories").json()
    assert cats == {"items": [{"name": "Вода", "count": 1}, {"name": "Напитки", "count": 2}]}      # архив не считается


def test_card_shows_only_the_fields_the_shop_enabled_and_never_expiry(env):
    _browse_reset(env)
    with env.Session() as db:
        cola = db.get(Product, env.cola)
        cola.country, cola.manufacturer, cola.ingredients, cola.in_stock = "Узбекистан", "Coca-Cola Ichimligi", "вода, сахар", True
        db.query(Store).one().card_fields = json.dumps(["country", "in_stock", "expiry_date"])     # срок вписан руками в базу
        db.commit()
    assert env.client.get("/api/v1/public/store").json()["card_fields"] == ["country", "in_stock"]
    item = next(i for i in env.client.get("/api/v1/public/products").json()["items"] if i["name"] == "Кола 0,5 л")
    assert item["details"] == {"country": "Узбекистан", "in_stock": True}
    assert "price" not in item and "9900" not in str(item)                                    # цену магазин не показывает
    card = identify(env, barcode_jpeg()).json()["product"]
    assert card == {"name": "Кола 0,5 л", "brand": "Coca-Cola", "category": "Напитки",
                    "details": {"country": "Узбекистан", "in_stock": True}}
    assert "вода, сахар" not in str(card) and "expiry" not in str(card)


def test_product_photo_thumbnail_only_for_listed_products(env, monkeypatch):
    _browse_reset(env)
    from app.services import catalog_upload as cu

    media = env.tmp_path / "media"
    monkeypatch.setattr(env.routes, "_media_root", lambda: media)
    folder = cu.uploads_root(media) / cu.folder_name("COLA")
    folder.mkdir(parents=True)
    (folder / "001-abcdef123456.jpg").write_bytes(jpeg(5, 800, 600))
    files = cu.list_photo_files(cu.uploads_root(media), "COLA")
    assert files, "имя файла фото должно подходить под шаблон каталога"
    items = {i["name"]: i for i in env.client.get("/api/v1/public/products").json()["items"]}
    assert items["Кола 0,5 л"]["has_photo"] is True and items["Фанта 0,5 л"]["has_photo"] is False
    resp = env.client.get(f"/api/v1/public/photo/{items['Кола 0,5 л']['ref']}")
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/jpeg"
    assert max(Image.open(io.BytesIO(resp.content)).size) <= 400
    assert env.client.get(f"/api/v1/public/photo/{items['Фанта 0,5 л']['ref']}").status_code == 404
    assert env.client.get("/api/v1/public/photo/ghost").json()["code"] == "public_photo_not_found"
    with env.Session() as db:
        db.get(Product, env.cola).status = "archived"
        db.commit()
    assert env.client.get(f"/api/v1/public/photo/{env.cola}").status_code == 404              # товар в архиве — фото нет


def test_browsing_has_its_own_rate_limit(env):
    _browse_reset(env)
    limit = env.routes.BROWSE_PER_DEVICE_PER_MINUTE
    codes = {env.client.get("/api/v1/public/categories").status_code for _ in range(limit)}
    assert codes == {200}
    blocked = env.client.get("/api/v1/public/products")
    assert blocked.status_code == 429 and blocked.json()["code"] == "public_browse_limited"
    _browse_reset(env)
