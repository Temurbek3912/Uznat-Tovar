"""app/core/memory_upload.py и app/core/frame_store.py — кадр «Узнать товар» только в памяти (D-10а)."""
from __future__ import annotations

import asyncio
import tempfile

import pytest

pytest.importorskip("starlette")

from starlette.requests import Request  # noqa: E402

from app.core import frame_store as fs  # noqa: E402
from app.core.errors import ApiError  # noqa: E402
from app.core.memory_upload import read_frame  # noqa: E402

MB = 1024 * 1024


def _request(body: bytes, content_type: str) -> Request:
    chunks = [body[i:i + 65536] for i in range(0, len(body), 65536)] or [b""]

    async def receive():
        if chunks:
            chunk = chunks.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "method": "POST", "path": "/", "headers": [(b"content-type", content_type.encode())], "query_string": b""}
    return Request(scope, receive)


def _multipart(data: bytes, field: str = "file") -> tuple[bytes, str]:
    boundary = "xBOUNDARYx"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"a.jpg\"\r\n"
            f"Content-Type: image/jpeg\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def _read(body, ctype, limit=10 * MB):
    return asyncio.run(read_frame(_request(body, ctype), limit))


def test_raw_image_body_is_read_into_memory():
    data = b"\xff\xd8" + b"x" * (3 * MB)
    assert _read(data, "image/jpeg") == data


def test_large_multipart_frame_never_spills_to_a_temporary_file(monkeypatch):
    """Обычный UploadFile Starlette уходит во временный файл после 1 МБ; здесь — нет."""
    created = []
    real = tempfile.SpooledTemporaryFile

    class Watch(real):
        def rollover(self):
            created.append(True)
            return super().rollover()

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", Watch)
    data = b"\xff\xd8" + b"y" * (6 * MB)
    body, ctype = _multipart(data)
    assert _read(body, ctype) == data and created == []


def test_too_large_missing_and_wrong_type_are_plain_errors():
    with pytest.raises(ApiError) as big:
        _read(b"z" * (2 * MB), "image/jpeg", limit=1 * MB)
    assert (big.value.status_code, big.value.code, big.value.params) == (413, "frame_too_large", {"mb": 1})
    body, ctype = _multipart(b"z" * (2 * MB))
    with pytest.raises(ApiError) as big_form:
        _read(body, ctype, limit=1 * MB)
    assert big_form.value.code == "frame_too_large"
    with pytest.raises(ApiError) as empty:
        _read(b"", "image/jpeg")
    assert empty.value.code == "frame_required"
    body, ctype = _multipart(b"abc", field="other")
    with pytest.raises(ApiError) as no_field:
        _read(body, ctype)
    assert no_field.value.code == "frame_required"
    with pytest.raises(ApiError) as wrong:
        _read(b"{}", "application/json")
    assert (wrong.value.status_code, wrong.value.code) == (415, "frame_type_unsupported")


# ------------------------------------------------------------------ хранилище кадров


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_memory_store_gives_a_frame_once_and_forgets_it_after_two_minutes():
    clock = Clock()
    store = fs.MemoryFrameStore(clock=clock)
    first, second = store.put(b"one"), store.put(b"two")
    assert store.take(first) == b"one" and store.take(first) is None          # забрали — кадра больше нет
    clock.t += fs.FRAME_TTL_SECONDS
    assert store.take(second) is None and store.count() == 0                 # 2 минуты — и кадра нет, даже если не забрали
    third = store.put(b"three")
    store.discard(third)
    assert store.take(third) is None


def test_memory_store_is_bounded():
    store = fs.MemoryFrameStore(max_frames=3, max_bytes=10)
    ids = [store.put(b"12345") for _ in range(4)]
    assert store.count() == 2 and store.take(ids[0]) is None and store.take(ids[-1]) == b"12345"


class FakeRedis:
    def __init__(self, save="", appendonly="no", fail=False):
        self.conf = {"save": save, "appendonly": appendonly}
        self.data, self.ex = {}, {}
        self.fail = fail

    def config_get(self, name):
        if self.fail:
            raise ConnectionError("down")
        return {name: self.conf[name]}

    def set(self, key, value, ex=None, nx=False):
        self.data[key], self.ex[key] = value, ex

    def getdel(self, key):
        return self.data.pop(key, None)

    def delete(self, key):
        self.data.pop(key, None)


def test_redis_store_refuses_a_redis_that_writes_to_disk():
    for conf in ({"save": "3600 1"}, {"appendonly": "yes"}):
        store = fs.RedisFrameStore(FakeRedis(**conf))
        with pytest.raises(fs.FrameStoreError, match="D-10а"):
            store.put(b"frame")
    with pytest.raises(fs.FrameStoreError):
        fs.RedisFrameStore(FakeRedis(fail=True)).put(b"frame")


def test_redis_store_keeps_a_frame_two_minutes_at_most_and_take_deletes_it():
    redis = FakeRedis()
    store = fs.RedisFrameStore(redis)
    frame_id = store.put(b"frame")
    key = next(iter(redis.data))
    assert redis.ex[key] == fs.FRAME_TTL_SECONDS and frame_id in key
    assert store.take(frame_id) == b"frame" and redis.data == {}


def test_frame_refs():
    ref = fs.frame_ref(fs.new_frame_id())
    assert fs.is_frame_ref(ref) and not fs.is_frame_ref("identify:abc.jpg") and not fs.is_frame_ref("frame:x")
    assert fs.frame_id_of(ref) == ref[len("frame:"):]
    assert isinstance(fs.build_frame_store(type("S", (), {"frames_redis_url": ""})()), fs.MemoryFrameStore)
