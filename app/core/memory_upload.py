"""
Приём кадра «Узнать товар» целиком в памяти, без временных файлов (решение D-10а).

Обычная загрузка файла в Starlette (UploadFile) складывает часть больше 1 МБ во временный файл на диске — фото с телефона почти
всегда больше. Здесь два способа передать кадр, оба без диска:
  * тело запроса — сама картинка (Content-Type: image/jpeg | image/png | image/webp | image/heic …) — так шлёт страница покупателя;
  * форма multipart/form-data с полем file — так шлёт прежняя страница сотрудника; разбирается тем же парсером Starlette, но с
    порогом «в память» выше предела кадра, поэтому временный файл не создаётся никогда.
Размер ограничен заранее (BodySizeLimitMiddleware) и ещё раз здесь — при чтении.
"""
from __future__ import annotations

from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartParser
from starlette.requests import Request

from app.core.errors import ApiError

IMAGE_TYPES = ("image/",)


class _InMemoryMultiPartParser(MultiPartParser):
    """MultiPartParser, который держит файл в памяти до spool_max_size (ставится больше предела кадра)."""


async def read_frame(request: Request, max_bytes: int, *, field: str = "file") -> bytes:
    """Байты кадра из тела запроса или поля формы. Больше max_bytes — 413; нет кадра — 422."""
    content_type = (request.headers.get("content-type") or "").lower()
    if content_type.startswith(IMAGE_TYPES):
        chunks, total = [], 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > max_bytes:
                raise _too_large(max_bytes)
            chunks.append(chunk)
        data = b"".join(chunks)
    elif content_type.startswith("multipart/form-data"):
        parser = _InMemoryMultiPartParser(request.headers, request.stream(), max_files=1, max_fields=5)
        parser.spool_max_size = max_bytes + 1024          # всё, что пропускает предел, — в памяти
        form = await parser.parse()
        try:
            upload = form.get(field)
            if not isinstance(upload, UploadFile):
                raise ApiError(422, "frame_required", "Пришлите фото товара.")
            if upload.file._rolled:                         # SpooledTemporaryFile ушёл на диск — не должно случиться
                raise ApiError(413, "frame_too_large", "Кадр слишком большой.", {"mb": max_bytes // (1024 * 1024)})
            data = upload.file.read(max_bytes + 1)
        finally:
            await form.close()
        if len(data) > max_bytes:
            raise _too_large(max_bytes)
    else:
        raise ApiError(415, "frame_type_unsupported", "Пришлите фото товара (картинку).")
    if not data:
        raise ApiError(422, "frame_required", "Пришлите фото товара.")
    return data


def _too_large(max_bytes: int) -> ApiError:
    mb = max_bytes // (1024 * 1024)
    return ApiError(413, "frame_too_large", f"Кадр: файл больше {mb} МБ.", {"mb": mb})
