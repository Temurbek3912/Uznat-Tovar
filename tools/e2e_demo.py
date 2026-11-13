"""
Сквозная проверка «Добавить товар» и «Узнать товар» в НАСТОЯЩЕМ браузере без Docker, Postgres, Redis и Celery. Только для разработки.

Что здесь настоящее: страницы frontend/, код маршрутов app/api/routes_catalog.py, приём и сжатие фото, артикулы, миниатюры,
логика решения «товар / похоже / не знаю» (identify_service), индекс FAISS-подобного поиска (NumpyCatalogIndex).
Что подделано: фреймворк (вместо FastAPI — стандартный http.server), база (в памяти), очередь (задача выполняется на месте) и
ЭМБЕДДЕР: вместо DINOv2 — «средний цвет кадра». Поэтому демо узнаёт цветные карточки, а не настоящие упаковки; качество настоящей
модели оно НЕ показывает.

Запуск:  python tools/e2e_demo.py --port 8765 [--assets папка-с-картинками]
Печатает «READY <порт>», когда готов. Логин любой (admin/admin).
"""
from __future__ import annotations

import argparse
import asyncio
import email.parser
import email.policy
import json
import sys
import threading
import types
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

try:
    import app.core.schemas  # noqa: F401, E402 — настоящие схемы (если pydantic установлен)
except ImportError:
    import tools.offline_test_harness  # noqa: F401, E402 — без pydantic: заглушки схем из офлайн-раннера (как в тестах)

from tests.test_catalog_routes_logic import FakeBarcode, FakeProduct, Upload, load  # noqa: E402

TOKEN = "demo-token"
ME = {"username": "admin", "role": "admin", "all_stores": True, "store_ids": [],
      "permissions": ["snapshot:upload", "report:read", "review:manage", "catalog:read", "catalog:write", "user:manage",
                      "store:manage", "camera:manage", "camera:agent"]}
MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".png": "image/png", ".webmanifest": "application/manifest+json"}


class ColorEmbedder:
    """Средний цвет кадра -> единичный вектор (серый кадр — нулевой вектор). НЕ DINOv2 — только для этого демо."""
    embedding_dim = 3

    def embed(self, crops):
        out = []
        for crop in crops:
            v = crop.reshape(-1, 3).astype(np.float64).mean(axis=0) - 127.5
            n = np.linalg.norm(v)
            out.append(v / n if n > 1e-6 else np.zeros(3))
        return np.array(out, dtype=np.float32)


def build_state(workdir: Path):
    env, _restore = load(workdir)                    # заглушки остаются на всё время работы сервера
    from app.pipeline.imageio import load_rgb
    from app.pipeline.matcher import NumpyCatalogIndex, ProductMatcher
    from app.services import catalog_upload as cu
    from app.services.identify_service import ProductInfo, add_reference_vectors, identify

    embedder, index = ColorEmbedder(), NumpyCatalogIndex()
    matcher = ProductMatcher(index, match_threshold=0.9, unknown_threshold=0.5)
    tasks: dict[str, dict] = {}
    media = Path(env.settings.media_root)
    n = [0]

    def new_task(result):
        n[0] += 1
        task_id = f"task-{n[0]}"
        tasks[task_id] = {"polls": 0, "result": result}
        return types.SimpleNamespace(id=task_id)

    def images_for(refs):
        return [load_rgb(cu.resolve_upload_ref(cu.uploads_root(media), r)) for r in refs]

    def add_product_delay(payload):
        if "ОШИБКА" in payload["name"].upper():
            return new_task(cu.PhotoError("Фото повреждено и не читается — загрузите другое."))
        product = FakeProduct(payload["sku"], payload["name"], payload["brand"], payload["category"], payload["expected_price"],
                              id=uuid.uuid4().hex, created_at=len(env.db.products) + 1)
        env.db.products.append(product)
        for code in payload.get("barcodes") or []:                                  # как воркер: после товара привязывает коды
            env.db.barcodes.append(FakeBarcode(code, product.id))
        add_reference_vectors(embedder, index, product.id, images_for(payload["reference_image_urls"]), None)
        return new_task(product.id)

    def add_photos_delay(product_id, refs):
        add_reference_vectors(embedder, index, product_id, images_for(refs), None)
        return new_task(product_id)

    def identify_delay(ref):
        path = cu.resolve_ref(cu.identify_root(media), cu.IDENTIFY_REF_PREFIX, ref)
        image = load_rgb(path)
        path.unlink()

        def lookup(ids):
            return {p.id: ProductInfo(p.id, p.name, p.brand, p.expected_price) for p in env.db.products if p.id in ids}

        result = identify(image, embedder, matcher, lookup)
        result["demo_mode"] = False
        return types.SimpleNamespace(id="identify", get=lambda timeout=None, propagate=True: result)

    env.routes.add_catalog_product_task = types.SimpleNamespace(delay=add_product_delay)
    env.routes.add_catalog_photos_task = types.SimpleNamespace(delay=add_photos_delay)
    env.routes.identify_product_task = types.SimpleNamespace(delay=identify_delay)
    return env, tasks, cu


def parse_multipart(content_type: str, body: bytes):
    raw = b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(raw)
    fields: dict[str, str] = {}
    files: list[tuple[str, bytes]] = []
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True) or b""
        if part.get_filename() is not None:
            files.append((name, payload))
        else:
            fields[name] = payload.decode("utf-8")
    return fields, files


def serve(port: int, workdir: Path, front: Path) -> None:
    env, tasks, cu = build_state(workdir)
    routes = env.routes
    HTTPException = env.HTTPException
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _send(self, status, body=b"", ctype="application/json", extra=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status, obj):
            self._send(status, json.dumps(obj, ensure_ascii=False, default=lambda o: o.__dict__).encode("utf-8"))

        def _authorized(self):
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self._json(401, {"detail": "Not authenticated"})
                return False
            return True

        def _body(self):
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def _static(self, path):
            rel = path[len("/ui/"):] or "index.html"
            file = (front / rel).resolve()
            if front.resolve() not in file.parents and file != front.resolve() or not file.is_file():
                return self._send(404, b"no", "text/plain")
            self._send(200, file.read_bytes(), MIME.get(file.suffix, "application/octet-stream"))

        def do_GET(self):
            path, _, query = self.path.partition("?")
            params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            if path.startswith("/ui/"):
                return self._static(path)
            if path == "/api/v1/auth/me":
                return self._json(200, ME) if self._authorized() else None
            if not self._authorized():
                return None
            with lock:
                try:
                    if path == "/api/v1/catalog/products":
                        from urllib.parse import unquote
                        rows = asyncio.run(routes.search_products(q=unquote(params.get("q", "")), limit=int(params.get("limit", 20)),
                                                                  offset=int(params.get("offset", 0)), sort=params.get("sort", "sku"),
                                                                  db=env.db, _user=None))
                        return self._json(200, [r.model_dump() if hasattr(r, "model_dump") else r.__dict__ for r in rows])
                    if path.startswith("/api/v1/catalog/products/tasks/"):
                        task = tasks.get(path.rsplit("/", 1)[1])
                        if task is None:
                            return self._json(200, {"state": "working", "status": "PENDING"})
                        task["polls"] += 1
                        if task["polls"] < 3:
                            return self._json(200, {"state": "working", "status": "PENDING"})
                        failed = isinstance(task["result"], Exception)
                        described = cu.describe_task("FAILURE" if failed else "SUCCESS", task["result"])
                        return self._json(200, {"status": "FAILURE" if failed else "SUCCESS", **described})
                    if path.startswith("/api/v1/catalog/products/") and path.endswith("/photo"):
                        product_id = path.split("/")[-2]
                        response = routes.product_photo(product_id=product_id, size=params.get("size", "thumb"), n=int(params.get("n", 0)),
                                                        db=env.db, _user=None)
                        return self._send(200, response.content, response.media_type, response.headers)
                except HTTPException as exc:
                    return self._json(exc.status_code, {"detail": exc.detail})
            self._json(404, {"detail": "no such route"})

        def do_POST(self):
            path = self.path.split("?")[0]
            if path == "/api/v1/auth/login":
                self._body()
                return self._json(200, {"access_token": TOKEN, "token_type": "bearer"})
            if not self._authorized():
                self._body()
                return
            body = self._body()
            fields, files = ({}, [])
            if "multipart/form-data" in (self.headers.get("Content-Type") or ""):
                fields, files = parse_multipart(self.headers["Content-Type"], body)
            uploads = [Upload(data) for name, data in files if name in ("files", "file")]
            with lock:
                try:
                    if path == "/api/v1/catalog/products/upload":
                        out = routes.upload_product(name=fields.get("name", ""), price=fields.get("price", ""), brand=fields.get("brand", ""),
                                                    category=fields.get("category", ""), barcode=fields.get("barcode", ""), files=uploads,
                                                    db=env.db, _user=None)
                        return self._json(202, out)
                    if path.startswith("/api/v1/catalog/products/") and path.endswith("/photos"):
                        out = routes.add_product_photos(product_id=path.split("/")[-2], files=uploads, db=env.db, _user=None)
                        return self._json(202, out)
                    if path == "/api/v1/catalog/identify":
                        return self._json(200, routes.identify_product(file=uploads[0] if uploads else Upload(b""), db=env.db, _user=None))
                except HTTPException as exc:
                    return self._json(exc.status_code, {"detail": exc.detail})
            self._json(404, {"detail": "no such route"})

        def do_PATCH(self):
            path = self.path.split("?")[0]
            body = self._body()
            if not self._authorized():
                return
            with lock:
                try:
                    if path.startswith("/api/v1/catalog/products/"):
                        update = routes.ProductUpdate(**json.loads(body or b"{}"))
                        out = routes.update_product(path.rsplit("/", 1)[1], update, env.db, None)
                        return self._json(200, out.model_dump() if hasattr(out, "model_dump") else out.__dict__)
                except HTTPException as exc:
                    return self._json(exc.status_code, {"detail": exc.detail})
            self._json(404, {"detail": "no such route"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"READY {server.server_address[1]}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workdir", default="/tmp/e2e-demo")
    parser.add_argument("--front", default=str(ROOT / "frontend"))
    args = parser.parse_args()
    work = Path(args.workdir)
    work.mkdir(parents=True, exist_ok=True)
    serve(args.port, work, Path(args.front))
