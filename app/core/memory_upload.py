"""
Приём кадра «Узнать товар» целиком в памяти, без временных файлов (решение D-10а).

Обычная загрузка файла в Starlette (UploadFile) складывает часть больше 1 МБ во временный файл на диске — фото с телефона почти
всегда больше. Здесь два способа передать кадр, оба без диска:
  * тело запроса — сама картинка (Content-Type: image/jpeg | image/png | image/webp | image/heic …) — так шлёт страница покупателя;
  * форма multipart/form-data с полем file — так шлёт прежняя страница сотрудника. Тело формы сначала читается целиком в память
    с пределом (кадр + FORM_OVERHEAD на заголовки формы) и только потом разбирается парсером Starlette с порогом «в память» больше
    всего тела — файловая часть не может быть больше тела, поэтому временный файл не создаётся никогда, в том числе для кадра
    чуть больше предела (до 2026-11-21 такой кадр успевал уйти во временный файл до отказа 413).
Размер ограничен заранее (BodySizeLimitMiddleware) и ещё раз здесь — при чтении.
"""
from __future__ import annotations

from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartParser
from starlette.requests import Request

from app.core.errors import ApiError

IMAGE_TYPES = ("image/",)
FORM_OVERHEAD = 64 * 1024        # заголовки частей и границы формы сверх самого кадра


class _InMemoryMultiPartParser(MultiPartParser):
    """MultiPartParser, который держит файл в памяти до spool_max_size (ставится больше предела кадра)."""


async def read_frame(request: Request, max_bytes: int, *, field: str = "file") -> bytes:
    """Байты кадра из тела запроса или поля формы. Больше max_bytes — 413; нет кадра — 422."""
    content_type = (request.headers.get("content-type") or "").lower()
    if content_type.startswith(IMAGE_TYPES):
        data = await _read_body(request, max_bytes, max_bytes)
    elif content_type.startswith("multipart/form-data"):
        body = await _read_body(request, max_bytes + FORM_OVERHEAD, max_bytes)

        async def body_stream():
            yield body

        parser = _InMemoryMultiPartParser(request.headers, body_stream(), max_files=1, max_fields=5)
        parser.spool_max_size = len(body) + 1             # часть формы не больше всего тела — только память
        form = await parser.parse()
        del body
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


async def _read_body(request: Request, limit: int, max_bytes: int) -> bytes:
    """Тело запроса в память; больше limit — 413 сразу, дочитывать не нужно."""
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise _too_large(max_bytes)
        chunks.append(chunk)
    return b"".join(chunks)


def _too_large(max_bytes: int) -> ApiError:
    mb = max_bytes // (1024 * 1024)
    return ApiError(413, "frame_too_large", f"Кадр: файл больше {mb} МБ.", {"mb": mb})
