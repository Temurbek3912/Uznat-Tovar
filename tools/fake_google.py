"""
Подставной Google (вход по коду + нужная часть Google Drive API v3) — для тестов и проверки на копии программы, без
интернета и без настоящего аккаунта. app/services/gdrive_backup.py ходит сюда, если задан GOOGLE_API_BASE.

    в тестах:   fake = FakeGoogle(); http = httpx.Client(transport=fake.transport())
    на копии:   python tools/fake_google.py --port 8299 --approve-after 3      (и GOOGLE_API_BASE=http://127.0.0.1:8299)

Хранит файлы в памяти. Умеет: обрыв связи и ошибки по заказу (fail_next), постраничную выдачу (page_limit),
загрузку по частям с докачкой, проверку ключа доступа.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import itertools
import json
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx

FOLDER = "application/vnd.google-apps.folder"


def _id_token(email: str) -> str:
    part = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{part}.sig"


class FakeGoogle:
    def __init__(self, *, email: str = "magazin@example.com", approve_after: int = 0, page_limit: int = 1000,
                 base: str = "https://fake.google"):
        self.email = email
        self.approve_after = approve_after         # сколько опросов «ещё не вошли» до входа
        self.approved = approve_after == 0
        self.page_limit = page_limit
        self.base = base
        self.files: dict[str, dict] = {}
        self.refresh_tokens: set[str] = set()
        self.access_tokens: set[str] = set()
        self.fail_next: list = []                  # "drop" — обрыв связи; число — ответ с этим кодом
        self.sessions: dict[str, dict] = {}
        self.polls = 0
        self.requests: list[tuple[str, str]] = []
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    # ----- удобства для тестов
    def tree(self) -> dict[str, list[str]]:
        """{путь папки: [имена]} — как выглядит Диск."""
        out: dict[str, list[str]] = {}
        for f in self.files.values():
            out.setdefault(self.path_of(f["parents"][0]) if f["parents"] else "", []).append(f["name"])
        return {k: sorted(v) for k, v in out.items()}

    def path_of(self, fid: str) -> str:
        if fid == "root":
            return ""
        f = self.files[fid]
        parent = self.path_of(f["parents"][0]) if f["parents"] else ""
        return (parent + "/" if parent else "") + f["name"]

    def find(self, path: str) -> dict | None:
        return next((f for f in self.files.values() if self.path_of(f["id"]) == path), None)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ----- ответы
    def handle(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            return self._handle(request)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        path = url.path
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.requests.append((request.method, path))
        if self.fail_next:
            what = self.fail_next.pop(0)
            if what == "drop":
                raise httpx.ConnectError("обрыв связи (подставной)", request=request)
            return httpx.Response(int(what), json={"error": {"message": "подставная ошибка"}})
        body = request.content
        if path.endswith("/device/code"):
            return httpx.Response(200, json={"device_code": "dev-1", "user_code": "ABCD-EFGH", "expires_in": 1800,
                                             "interval": 1, "verification_url": "https://www.google.com/device"})
        if path.endswith("/token"):
            form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            if form.get("grant_type", "").endswith("device_code"):
                self.polls += 1
                if not self.approved and self.polls > self.approve_after:
                    self.approved = True
                if not self.approved:
                    return httpx.Response(428, json={"error": "authorization_pending"})
                rt = f"rt-{next(self._ids)}"
                self.refresh_tokens.add(rt)
                return httpx.Response(200, json={"access_token": self._access(), "expires_in": 3599, "refresh_token": rt,
                                                 "id_token": _id_token(self.email), "scope": "drive.file"})
            if form.get("refresh_token") in self.refresh_tokens:
                return httpx.Response(200, json={"access_token": self._access(), "expires_in": 3599})
            return httpx.Response(400, json={"error": "invalid_grant"})
        if path.endswith("/revoke"):
            form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            self.refresh_tokens.discard(form.get("token", ""))
            return httpx.Response(200, json={})
        auth = request.headers.get("authorization", "")
        if auth.removeprefix("Bearer ") not in self.access_tokens:
            return httpx.Response(401, json={"error": {"message": "нет доступа"}})
        if "/upload/session/" in path:
            return self._chunk(path.rsplit("/", 1)[1], request)
        if path.endswith("/upload/drive/v3/files"):
            if params.get("uploadType") == "multipart":
                meta, data = self._multipart(request)
                return httpx.Response(200, json=self._public(self._create(meta, data)))
            meta = json.loads(body or b"{}")
            sid = f"s{next(self._ids)}"
            self.sessions[sid] = {"meta": meta, "total": int(request.headers.get("x-upload-content-length", "0")),
                                  "data": b""}
            return httpx.Response(200, headers={"Location": f"{self.base}/upload/session/{sid}"})
        m = re.search(r"/drive/v3/files(?:/([^/]+))?$", path)
        if not m:
            return httpx.Response(404, json={"error": {"message": "нет такого адреса"}})
        fid = m.group(1)
        if fid is None and request.method == "GET":
            return self._list(params)
        if fid is None and request.method == "POST":
            return httpx.Response(200, json=self._public(self._create(json.loads(body), None)))
        f = self.files.get(fid)
        if f is None:
            return httpx.Response(404, json={"error": {"message": "файла нет"}})
        if request.method == "DELETE":
            self._delete(fid)
            return httpx.Response(204)
        if request.method == "PATCH":
            f["name"] = json.loads(body).get("name", f["name"])
            return httpx.Response(200, json=self._public(f))
        if params.get("alt") == "media":
            return httpx.Response(200, content=f["data"] or b"")
        return httpx.Response(200, json=self._public(f))

    def _access(self) -> str:
        token = f"at-{next(self._ids)}"
        self.access_tokens.add(token)
        return token

    def _create(self, meta: dict, data: bytes | None) -> dict:
        fid = f"f{next(self._ids)}"
        f = {"id": fid, "name": meta.get("name", ""), "mimeType": meta.get("mimeType") or "application/octet-stream",
             "parents": list(meta.get("parents") or ["root"]), "data": data, "trashed": False,
             "createdTime": time.strftime("%Y-%m-%dT%H:%M:%SZ")}
        self.files[fid] = f
        return f

    def _delete(self, fid: str) -> None:
        for child in [c["id"] for c in self.files.values() if fid in c["parents"]]:
            self._delete(child)
        self.files.pop(fid, None)

    def _public(self, f: dict) -> dict:
        out = {"id": f["id"], "name": f["name"], "mimeType": f["mimeType"]}
        if f["data"] is not None:
            out["size"] = str(len(f["data"]))
            out["md5Checksum"] = hashlib.md5(f["data"]).hexdigest()  # noqa: S324
        return out

    def _multipart(self, request: httpx.Request) -> tuple[dict, bytes]:
        boundary = request.headers["content-type"].split("boundary=", 1)[1].encode()
        parts = request.content.split(b"--" + boundary)
        meta_part, data_part = parts[1], parts[2]
        meta = json.loads(meta_part.split(b"\r\n\r\n", 1)[1].rstrip(b"\r\n"))
        data = data_part.split(b"\r\n\r\n", 1)[1]
        return meta, data[:-2] if data.endswith(b"\r\n") else data

    def _chunk(self, sid: str, request: httpx.Request) -> httpx.Response:
        s = self.sessions.get(sid)
        if s is None:
            return httpx.Response(404, json={"error": {"message": "загрузка устарела"}})
        rng = request.headers.get("content-range", "")
        got = len(s["data"])
        if not rng.startswith("bytes */"):
            start, end = (int(x) for x in rng.split(" ", 1)[1].split("/", 1)[0].split("-"))
            if start == got:
                s["data"] += request.content
            got = len(s["data"])
        if got >= s["total"]:
            f = self._create(s["meta"], s["data"])
            del self.sessions[sid]
            return httpx.Response(200, json=self._public(f))
        return httpx.Response(308, headers={"Range": f"bytes=0-{got - 1}"} if got else {})

    def _list(self, params: dict) -> httpx.Response:
        q = params.get("q", "")
        items = [f for f in self.files.values() if not f["trashed"]]
        for clause in q.split(" and "):
            clause = clause.strip()
            m = re.fullmatch(r"'(.*)' in parents", clause)
            if m:
                items = [f for f in items if m.group(1) in f["parents"]]
                continue
            m = re.fullmatch(r"(name|mimeType)\s*(!?=)\s*'(.*)'", clause)
            if m:
                key, op, value = m.group(1), m.group(2), m.group(3).replace("\\'", "'").replace("\\\\", "\\")
                items = [f for f in items if (f[key] == value) == (op == "=")]
        items.sort(key=lambda f: f["id"])
        size = min(int(params.get("pageSize") or 100), self.page_limit)
        start = int(params.get("pageToken") or 0)
        page = items[start:start + size]
        out = {"files": [self._public(f) for f in page]}
        if start + size < len(items):
            out["nextPageToken"] = str(start + size)
        return httpx.Response(200, json=out)


def serve(port: int, fake: FakeGoogle) -> None:
    """Подставной Google как настоящий веб-сервер (для проверки на копии программы)."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    fake.base = f"http://127.0.0.1:{port}"

    class H(BaseHTTPRequestHandler):
        def _any(self):
            n = int(self.headers.get("Content-Length") or 0)
            req = httpx.Request(self.command, fake.base + self.path, headers=dict(self.headers.items()),
                                content=self.rfile.read(n) if n else b"")
            try:
                res = fake.handle(req)
            except httpx.ConnectError:
                self.close_connection = True
                return
            self.send_response(res.status_code)
            for k, v in res.headers.items():
                if k.lower() not in ("content-length", "transfer-encoding", "connection"):
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(res.content)))
            self.end_headers()
            self.wfile.write(res.content)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _any

        def log_message(self, *_a):
            pass

    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8299)
    ap.add_argument("--approve-after", type=int, default=3)
    a = ap.parse_args()
    print(f"Подставной Google: http://127.0.0.1:{a.port}")
    serve(a.port, FakeGoogle(approve_after=a.approve_after))
