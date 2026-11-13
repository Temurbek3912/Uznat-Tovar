"""
API каталога: «Добавить товар» (форма с фото), «+ фото», правка, миниатюры, ход задачи, «Узнать товар».

ВАЖНО: как и test_auth_api/test_stores_api, написан БЕЗ возможности выполнить (в среде разработки не было fastapi/sqlalchemy/httpx) —
при падении сначала подозревайте сам тест. Чистая логика покрыта исполняемыми tests/test_catalog_upload.py и
tests/test_identify_service.py; права на маршруты закреплены исполняемым tests/test_catalog_routes_static.py.
"""
from __future__ import annotations

import io
import sys
import types

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

from app.core.error_handlers import install_error_handlers
from tests.admin_login import admin_entry, admin_token, enable_totp  # noqa: E402
from app.api.routes_auth import router as auth_router  # noqa: E402
from app.core.security import Role  # noqa: E402
from app.db.models import Base, Product, ProductBarcodeORM  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.services.auth_service import AuthService  # noqa: E402
from app.services.catalog_upload import PhotoError  # noqa: E402

PASSWORD = "correct-horse-battery"


def jpeg(seed=1, w=400, h=300) -> bytes:
    rng = np.random.default_rng(seed)
    buf = io.BytesIO()
    Image.fromarray(rng.integers(0, 255, (h, w, 3), dtype=np.uint8)).save(buf, "JPEG")
    return buf.getvalue()


def files(*datas, name="files"):
    return [(name, (f"p{i}.jpg", data, "image/jpeg")) for i, data in enumerate(datas)]


class FakeAsync:
    def __init__(self, status="PENDING", result=None, ident="task-1"):
        self.id, self.status, self.result = ident, status, result

    def ready(self):
        return self.status in ("SUCCESS", "FAILURE")

    def get(self, timeout=None, propagate=True):
        if isinstance(self.result, TimeoutError):
            raise self.result
        return self.result


class Env:
    pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env()
    e.queued = []                       # (имя задачи, аргументы)
    e.queue_down = False
    e.async_results = {}
    e.identify_answer = {"status": "matched", "product": {"id": "x", "name": "Кола", "brand": "B", "expected_price": None, "score": 0.9},
                         "score": 0.9, "candidates": [], "demo_mode": False}

    def make_task(name, result=None):
        def delay(*args):
            if e.queue_down:
                raise ConnectionError("redis down")
            e.queued.append((name, args))
            return FakeAsync(ident=f"{name}-{len(e.queued)}", result=result() if callable(result) else result)

        def apply_async(args=(), kwargs=None, **options):
            return delay(*args)
        return types.SimpleNamespace(delay=delay, apply_async=apply_async)

    fake_worker = types.ModuleType("app.worker")
    fake_worker.add_catalog_product_task = make_task("add_product")
    fake_worker.add_catalog_photos_task = make_task("add_photos")
    fake_worker.identify_product_task = make_task("identify", result=lambda: e.identify_answer)
    fake_worker.celery_app = types.SimpleNamespace(AsyncResult=lambda task_id: e.async_results.get(task_id, FakeAsync("PENDING", None, task_id)))
    monkeypatch.setitem(sys.modules, "app.worker", fake_worker)
    sys.modules.pop("app.api.routes_catalog", None)
    import importlib
    routes_catalog = importlib.import_module("app.api.routes_catalog")
    monkeypatch.setattr(routes_catalog, "get_settings",
                        lambda: types.SimpleNamespace(max_upload_mb=1, media_root=str(tmp_path / "media")))

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
    install_error_handlers(app)          # как в app/main.py: ошибки в едином виде {detail, code, params}
    app.include_router(auth_router)
    app.include_router(routes_catalog.router)
    app.dependency_overrides[get_db] = override_get_db
    e.auth_settings = admin_entry(monkeypatch)
    e.client, e.Session, e.media = TestClient(app, base_url="http://localhost"), Session, tmp_path / "media"
    with Session() as db:
        AuthService(db).create_user("admin", PASSWORD, Role.ADMIN)
        AuthService(db).create_user("analyst", PASSWORD, Role.ANALYST)
        AuthService(db).create_user("merch", PASSWORD, Role.MERCHANDISER, [])
    enable_totp(Session, "admin", e.auth_settings)
    yield e
    sys.modules.pop("app.api.routes_catalog", None)
    sys.modules.pop("app.worker", None)


def headers_for(e, username):
    if username == "admin":
        return {"Authorization": f"Bearer {admin_token(e.client, e.Session, username, PASSWORD)}"}
    resp = e.client.post("/api/v1/auth/login", data={"username": username, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def add_product(e, **kw):
    with e.Session() as db:
        p = Product(sku=kw.get("sku", "COLA"), name=kw.get("name", "Кола"), brand="X", category="Напитки", expected_price=kw.get("price"))
        db.add(p)
        db.commit()
        return p.id


def upload(e, who="admin", data=None, photos=None):
    return e.client.post("/api/v1/catalog/products/upload", headers=headers_for(e, who),
                         data=data if data is not None else {"name": "Кола 0,5 л"}, files=photos if photos is not None else files(jpeg(1), jpeg(2)))


# ------------------------------------------------------------------ права

def test_upload_requires_login_and_admin(env):
    assert env.client.post("/api/v1/catalog/products/upload", data={"name": "X"}, files=files(jpeg())).status_code == 401
    for who in ("merch", "analyst"):
        assert upload(env, who).status_code == 403
    assert env.queued == [] and not env.media.exists()


def test_every_role_can_read_and_search_but_only_admin_changes(env):
    pid = add_product(env)
    for who in ("admin", "merch", "analyst"):
        assert env.client.get("/api/v1/catalog/products", headers=headers_for(env, who)).status_code == 200
    for who in ("merch", "analyst"):
        h = headers_for(env, who)
        assert env.client.patch(f"/api/v1/catalog/products/{pid}", headers=h, json={"name": "Другое"}).status_code == 403
        assert env.client.post(f"/api/v1/catalog/products/{pid}/photos", headers=h, files=files(jpeg())).status_code == 403
        assert env.client.get("/api/v1/catalog/products/tasks/abc", headers=h).status_code == 403


# ------------------------------------------------------------------ новый товар

def test_upload_builds_sku_saves_photos_and_queues_the_task(env):
    resp = upload(env, data={"name": "  Кола  0,5 л ", "price": "9 900", "brand": "BrandCola"})
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["sku"] == "KOLA-0.5-L" and body["name"] == "Кола 0,5 л" and body["photos"] == 2 and body["task_id"]
    (task, args), = env.queued
    payload = args[0]
    assert task == "add_product"
    assert payload["sku"] == "KOLA-0.5-L" and payload["brand"] == "BrandCola" and payload["category"] == "Без категории"
    assert payload["expected_price"] == 9900.0 and len(payload["reference_image_urls"]) == 2
    assert all(r.startswith("upload:KOLA-0.5-L/") for r in payload["reference_image_urls"])
    stored = sorted((env.media / "catalog-uploads" / "KOLA-0.5-L").iterdir())
    assert [p.name[:4] for p in stored] == ["001-", "002-"]


def test_upload_picks_a_free_sku_when_the_slug_is_taken(env):
    add_product(env, sku="KOLA-0.5-L", name="Совсем другое название")
    assert upload(env).json()["sku"] == "KOLA-0.5-L-2"


def test_upload_rejects_bad_forms_before_touching_disk_or_queue(env):
    cases = [({"name": "  "}, files(jpeg()), "Напишите название"),
             ({"name": "Кола", "price": "abc"}, files(jpeg()), "Цена"),
             ({"name": "Кола", "price": "-5"}, files(jpeg()), "Цена"),
             ({"name": "Кола"}, [], "хотя бы одно фото"),
             ({"name": "Кола"}, files(*[jpeg(i) for i in range(9)]), "не больше 8")]
    for data, photos, fragment in cases:
        resp = upload(env, data=data, photos=photos)
        assert resp.status_code == 422 and fragment in resp.json()["detail"], (data, resp.text)
    assert env.queued == [] and not env.media.exists()


def test_upload_names_the_bad_photo(env):
    resp = upload(env, photos=files(jpeg(1), b"definitely not a picture"))
    assert resp.status_code == 422 and resp.json()["detail"].startswith("Фото 2:")
    assert env.queued == [] and not (env.media / "catalog-uploads").exists()


def test_upload_refuses_huge_files(env):
    resp = upload(env, photos=files(b"\xff\xd8" + b"0" * (1024 * 1024 + 10)))
    assert resp.status_code == 413 and "МБ" in resp.json()["detail"]
    assert env.queued == []


def test_duplicate_name_is_a_409_that_points_to_the_existing_product(env):
    pid = add_product(env, sku="KOLA-0.5-L", name="Кола 0,5 л")
    resp = upload(env, data={"name": "кола 0.5 Л"})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["product_id"] == pid and detail["product_name"] == "Кола 0,5 л" and "уже есть" in detail["message"]
    assert env.queued == [] and not env.media.exists()


def test_queue_outage_gives_a_plain_503(env):
    env.queue_down = True
    resp = upload(env)
    assert resp.status_code == 503 and "docker compose ps" in resp.json()["detail"]


# ------------------------------------------------------------------ фото к существующему

def test_add_photos_queues_refs_for_the_existing_product(env):
    pid = add_product(env, sku="COLA")
    resp = env.client.post(f"/api/v1/catalog/products/{pid}/photos", headers=headers_for(env, "admin"), files=files(jpeg(1), jpeg(2), jpeg(2)))
    assert resp.status_code == 202 and resp.json()["photos"] == 3 and resp.json()["name"] == "Кола"
    (task, args), = env.queued
    assert task == "add_photos" and args[0] == pid and len(args[1]) == 3 and args[1][1] == args[1][2]     # одинаковое фото — один файл
    assert len(list((env.media / "catalog-uploads" / "COLA").iterdir())) == 2


def test_add_photos_unknown_product_and_bad_photo(env):
    h = headers_for(env, "admin")
    assert env.client.post("/api/v1/catalog/products/nope/photos", headers=h, files=files(jpeg())).status_code == 404
    pid = add_product(env)
    assert env.client.post(f"/api/v1/catalog/products/{pid}/photos", headers=h, files=files(b"junk")).status_code == 422
    assert env.queued == []


# ------------------------------------------------------------------ правка

def test_patch_changes_name_and_price_but_never_the_sku(env):
    pid = add_product(env, sku="COLA", price=1000.0)
    h = headers_for(env, "admin")
    resp = env.client.patch(f"/api/v1/catalog/products/{pid}", headers=h, json={"name": "  Кола 0,5 л ", "expected_price": "9 900"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Кола 0,5 л" and resp.json()["expected_price"] == 9900.0 and resp.json()["sku"] == "COLA"
    resp = env.client.patch(f"/api/v1/catalog/products/{pid}", headers=h, json={"clear_price": True})
    assert resp.json()["expected_price"] is None and resp.json()["name"] == "Кола 0,5 л"


def test_patch_rejects_duplicates_bad_prices_and_unknown_products_without_half_applying(env):
    pid = add_product(env, sku="COLA", name="Кола")
    add_product(env, sku="FANTA", name="Фанта")
    h = headers_for(env, "admin")
    resp = env.client.patch(f"/api/v1/catalog/products/{pid}", headers=h, json={"name": "фанта"})
    assert resp.status_code == 409 and resp.json()["detail"]["product_name"] == "Фанта"
    resp = env.client.patch(f"/api/v1/catalog/products/{pid}", headers=h, json={"name": "Новое имя", "expected_price": "abc"})
    assert resp.status_code == 422
    assert env.client.get("/api/v1/catalog/products?q=Новое", headers=h).json() == []      # имя не применилось наполовину
    assert env.client.patch("/api/v1/catalog/products/nope", headers=h, json={"name": "X"}).status_code == 404
    assert env.client.patch(f"/api/v1/catalog/products/{pid}", headers=h, json={"name": "Кола"}).status_code == 200   # своё же имя можно


# ------------------------------------------------------------------ список и фото

def test_list_shows_photo_count_price_offset_and_newest_first(env):
    h = headers_for(env, "admin")
    pid = add_product(env, sku="P1", name="Один", price=5.0)
    add_product(env, sku="P2", name="Два")
    env.client.post(f"/api/v1/catalog/products/{pid}/photos", headers=h, files=files(jpeg(1), jpeg(2)))
    rows = env.client.get("/api/v1/catalog/products?limit=1&offset=1&sort=sku", headers=h).json()
    assert [r["sku"] for r in rows] == ["P2"]
    one = next(r for r in env.client.get("/api/v1/catalog/products?q=Один", headers=h).json())
    assert one["photo_count"] == 2 and one["expected_price"] == 5.0


def test_photo_endpoint_serves_a_small_thumbnail_and_the_full_picture(env):
    h = headers_for(env, "admin")
    pid = add_product(env, sku="P1")
    assert env.client.get(f"/api/v1/catalog/products/{pid}/photo", headers=h).status_code == 404          # фото ещё нет
    env.client.post(f"/api/v1/catalog/products/{pid}/photos", headers=h, files=files(jpeg(1, 1200, 900)))
    thumb = env.client.get(f"/api/v1/catalog/products/{pid}/photo", headers=headers_for(env, "analyst"))
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"
    assert max(Image.open(io.BytesIO(thumb.content)).size) == 240
    assert list((env.media / "catalog-uploads" / "P1").glob("thumb-*.jpg"))                              # кэш миниатюры создан
    full = env.client.get(f"/api/v1/catalog/products/{pid}/photo?size=full", headers=h)
    assert max(Image.open(io.BytesIO(full.content)).size) == 1200 and len(full.content) > len(thumb.content)
    assert env.client.get(f"/api/v1/catalog/products/{pid}/photo?n=5", headers=h).status_code == 404
    assert env.client.get("/api/v1/catalog/products/nope/photo", headers=h).status_code == 404
    assert env.client.get(f"/api/v1/catalog/products/{pid}/photo").status_code == 401


# ------------------------------------------------------------------ ход задачи

def test_task_status_speaks_plain_russian(env):
    h = headers_for(env, "admin")
    env.async_results["w"] = FakeAsync("PENDING", None, "w")
    env.async_results["d"] = FakeAsync("SUCCESS", "product-1", "d")
    env.async_results["f"] = FakeAsync("FAILURE", PhotoError("Фото повреждено и не читается — загрузите другое."), "f")
    env.async_results["g"] = FakeAsync("FAILURE", RuntimeError("connection refused: postgres:5432"), "g")
    get = lambda t: env.client.get(f"/api/v1/catalog/products/tasks/{t}", headers=h).json()
    assert get("w")["state"] == "working"
    assert get("d")["state"] == "done" and get("d")["product_id"] == "product-1" and get("d")["status"] == "SUCCESS"
    assert get("f")["state"] == "failed" and get("f")["message"].startswith("Фото повреждено") and get("f")["detail"] is None
    generic = get("g")
    # Текст постороннего исключения (адреса, SQL, пути) клиенту не отдаётся ни в message, ни в detail, ни в result — только общий
    # текст и код; подробности — в журнале воркера (запрет владельца, ENGINEERING_STATUS §6.5).
    assert generic["state"] == "failed" and generic["code"] == "task_failed" and generic["detail"] is None
    assert "postgres" not in str(generic)


# ------------------------------------------------------------------ узнать товар

def test_identify_returns_the_workers_answer_and_stores_the_frame_only_temporarily(env):
    resp = env.client.post("/api/v1/catalog/identify", headers=headers_for(env, "analyst"), files=files(jpeg(1), name="file")[:1])
    assert resp.status_code == 200, resp.text
    assert resp.json()["product"]["name"] == "Кола" and resp.json()["status"] == "matched"
    (task, args), = env.queued
    assert task == "identify" and args[0].startswith("identify:") and args[0].endswith(".jpg")
    assert (env.media / "identify-tmp").is_dir()


def test_identify_errors_are_plain(env):
    h = headers_for(env, "admin")
    bad = env.client.post("/api/v1/catalog/identify", headers=h, files=files(b"junk", name="file")[:1])
    assert bad.status_code == 422 and "Не удалось открыть" in bad.json()["detail"]
    env.identify_answer = PhotoError("Кадр не читается — попробуйте ещё раз.")
    assert env.client.post("/api/v1/catalog/identify", headers=h, files=files(jpeg(), name="file")[:1]).status_code == 422
    env.identify_answer = TimeoutError("нет ответа")
    slow = env.client.post("/api/v1/catalog/identify", headers=h, files=files(jpeg(), name="file")[:1])
    assert slow.status_code == 504 and "docker compose ps" in slow.json()["detail"]
    env.identify_answer = RuntimeError("boom")
    assert env.client.post("/api/v1/catalog/identify", headers=h, files=files(jpeg(), name="file")[:1]).status_code == 500
    env.queue_down = True
    assert env.client.post("/api/v1/catalog/identify", headers=h, files=files(jpeg(), name="file")[:1]).status_code == 503
    assert env.client.post("/api/v1/catalog/identify", files=files(jpeg(), name="file")[:1]).status_code == 401


# ------------------------------------------------------------------ штрих-коды (как и весь файл — не запускалось без fastapi)

EAN_A, EAN_B = "4780001234562", "5901234123457"


def barcode_jpeg(code, W=1000, H=600, frac=0.55) -> bytes:
    import cv2

    from app.services import barcode as bc
    bars = cv2.cvtColor(bc.render(code, module=4, height=140), cv2.COLOR_GRAY2RGB)
    k = int(W * frac) / bars.shape[1]
    bars = cv2.resize(bars, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
    frame = np.full((H, W, 3), 150, np.uint8)
    h, w = bars.shape[:2]
    frame[(H - h) // 2:(H - h) // 2 + h, (W - w) // 2:(W - w) // 2 + w] = bars
    buf = io.BytesIO()
    Image.fromarray(frame).save(buf, "JPEG", quality=92)
    return buf.getvalue()


def link_barcode(e, product_id, code):
    with e.Session() as db:
        db.add(ProductBarcodeORM(barcode=code, product_id=product_id))
        db.commit()


def test_upload_with_barcode_validates_and_refuses_a_code_owned_by_another_product(env):
    owner = add_product(env, sku="FANTA", name="Фанта")
    link_barcode(env, owner, EAN_A)
    taken = upload(env, data={"name": "Кола", "barcode": EAN_A})
    assert taken.status_code == 409 and taken.json()["detail"]["product_id"] == owner
    typo = upload(env, data={"name": "Кола", "barcode": "4780001234563"})
    assert typo.status_code == 422 and "Контрольная цифра" in typo.json()["detail"]
    ok = upload(env, data={"name": "Кола", "barcode": EAN_B})
    assert ok.status_code == 202 and ok.json()["barcodes"] == [EAN_B]
    assert env.queued[-1][1][0]["barcodes"] == [EAN_B]


def test_a_barcode_on_an_uploaded_photo_is_found_and_reported(env):
    resp = upload(env, data={"name": "Кола"}, photos=files(jpeg(1), barcode_jpeg(EAN_B)))
    assert resp.status_code == 202 and resp.json()["barcodes"] == [EAN_B]
    assert any("Найден штрих-код" in w["message"] for w in resp.json()["warnings"])


def test_patch_adds_and_removes_barcodes_and_the_list_shows_them(env):
    pid = add_product(env, sku="COLA", name="Кола")
    h = headers_for(env, "admin")
    out = env.client.patch(f"/api/v1/catalog/products/{pid}", headers=h, json={"add_barcode": "4 780001 234562"})
    assert out.status_code == 200 and out.json()["barcodes"] == [EAN_A]
    listing = env.client.get("/api/v1/catalog/products?q=Кола", headers=h).json()
    assert listing[0]["barcodes"] == [EAN_A]
    other = add_product(env, sku="FANTA", name="Фанта")
    assert env.client.patch(f"/api/v1/catalog/products/{other}", headers=h, json={"add_barcode": EAN_A}).status_code == 409
    assert env.client.patch(f"/api/v1/catalog/products/{other}", headers=h, json={"remove_barcode": EAN_A}).status_code == 404
    done = env.client.patch(f"/api/v1/catalog/products/{pid}", headers=h, json={"remove_barcode": EAN_A})
    assert done.status_code == 200 and done.json()["barcodes"] == []


def test_identify_answers_from_a_known_barcode_without_the_queue(env):
    pid = add_product(env, sku="COLA", name="Кола 0,5 л", price=9900.0)
    link_barcode(env, pid, EAN_A)
    env.queue_down = True                                         # воркер не нужен: код читается в API
    resp = env.client.post("/api/v1/catalog/identify", headers=headers_for(env, "analyst"), files=[("file", ("f.jpg", barcode_jpeg(EAN_A), "image/jpeg"))])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["by"] == "barcode" and body["status"] == "matched" and body["product"]["name"] == "Кола 0,5 л" and body["product"]["expected_price"] == 9900.0
    assert body["barcode"] == {"code": EAN_A, "known": True} and env.queued == []
