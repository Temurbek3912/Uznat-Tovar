"""Единые обработчики ошибок (app/core/error_handlers.py) на настоящем приложении Starlette — основе FastAPI.

В облачной среде нет FastAPI и httpx (TestClient), поэтому запрос подаётся прямо в ASGI-приложение. Проверяется:
формат тела {"detail", "code", "params"}, HTTP-статусы по видам ошибок, заголовки, старые HTTPException без кода,
очистка ошибок проверки от присланных значений и главное — неожиданное исключение не раскрывает клиенту ни текст,
ни SQL, ни пути, ни трассировку (только журнал сервера).

Не проверено здесь (нет FastAPI): подключение RequestValidationError в настоящем приложении — обработчик вызывается напрямую.
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.routing import Route

from app.core.app_errors import AppError, INTERNAL_ERROR_MESSAGE, code_of, message_of, params_of
from app.core.error_handlers import install_error_handlers, validation_error_handler
from app.core.errors import ApiError, from_app_error
from app.services.catalog_upload import NotReadyError, PhotoError
from app.services.store_service import StoreInputError
from app.core.security import PolicyViolation



def _auth_errors():
    """Классы ошибок app/services/auth_service.py — их настоящий исходный код. Сам модуль требует SQLAlchemy, которой в облачной
    среде нет, поэтому определения классов берутся из файла (ast) и выполняются отдельно; в полной среде — обычный импорт."""
    try:
        from app.services import auth_service as module
        return module.UnknownStoreError, module.UserAlreadyExistsError, module.UserNotFoundError
    except ImportError:
        pass
    source = (Path(__file__).resolve().parents[1] / "app/services/auth_service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name.endswith("Error")]
    namespace = {"AppError": AppError, "__name__": "app.services.auth_service"}
    exec(compile(ast.Module(body=classes, type_ignores=[]), "auth_service.py", "exec"), namespace)
    return namespace["UnknownStoreError"], namespace["UserAlreadyExistsError"], namespace["UserNotFoundError"]


UnknownStoreError, UserAlreadyExistsError, UserNotFoundError = _auth_errors()

SECRET_TEXT = 'psycopg.errors.UniqueViolation: SELECT * FROM users WHERE password_hash=\'$argon2id$abc\' /srv/app/secret.env'


class _Kaboom(RuntimeError):
    pass


def _raiser(exc):
    async def endpoint(_request):
        raise exc
    return endpoint


def _app(routes: dict) -> Starlette:
    app = Starlette(routes=[Route(path, _raiser(exc)) for path, exc in routes.items()])
    install_error_handlers(app)
    return app


def _call(app, path: str, method: str = "GET"):
    """Один запрос к ASGI-приложению: (статус, заголовки, тело JSON). Исключение после ответа (так делает
    ServerErrorMiddleware для 500) глотается — клиент уже получил ответ."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "headers": [], "client": ("127.0.0.1", 1),
             "server": ("testserver", 80), "root_path": ""}

    async def run():
        try:
            await app(scope, receive, send)
        except Exception:
            pass

    asyncio.run(run())
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    return start["status"], headers, json.loads(body)


def test_api_error_gives_status_code_params_and_headers():
    app = _app({"/x": ApiError(429, "login_throttled", "Слишком много попыток", {"seconds": 30}, headers={"Retry-After": "30"})})
    status, headers, body = _call(app, "/x")
    assert status == 429
    assert body == {"detail": "Слишком много попыток", "code": "login_throttled", "params": {"seconds": 30}}
    assert headers["retry-after"] == "30"


def test_structured_detail_is_kept_for_old_pages():
    detail = {"message": "Товар «Чай» уже есть в каталоге.", "product_id": "p1", "product_name": "Чай"}
    status, _, body = _call(_app({"/x": ApiError(409, "product_duplicate", detail, {"name": "Чай"})}), "/x")
    assert status == 409 and body["detail"] == detail and body["code"] == "product_duplicate" and body["params"] == {"name": "Чай"}


@pytest.mark.parametrize("exc, status, code", [
    (PhotoError("Файл пустой", code="photo_empty"), 422, "photo_empty"),
    (StoreInputError("Название магазина не может быть пустым", "store_name_required"), 422, "store_name_required"),
    (StoreInputError("старый вызов без кода"), 422, "store_input_invalid"),
    (PolicyViolation("Пароль короткий", "password_too_short", {"min": 10}), 422, "password_too_short"),
    (UserAlreadyExistsError("Пользователь «a» уже существует", params={"username": "a"}), 409, "user_exists"),
    (UserNotFoundError("Пользователь 5 не найден"), 404, "user_not_found"),
    (UnknownStoreError("Магазины не найдены: s9", params={"ids": "s9"}), 404, "stores_not_found"),
    (NotReadyError("Модель ещё загружается"), 503, "ml_not_ready"),
])
def test_service_errors_reaching_the_app_get_status_by_kind(exc, status, code):
    got_status, _, body = _call(_app({"/x": exc}), "/x")
    assert got_status == status and body["code"] == code and body["detail"] == str(exc)
    assert body["params"] == exc.params


def test_from_app_error_matches_what_the_handler_would_send():
    exc = PolicyViolation("Пароль длинный", "password_too_long", {"max": 128})
    api = from_app_error(exc)
    assert (api.status_code, api.code, api.detail, api.params) == (422, "password_too_long", "Пароль длинный", {"max": 128})


def test_plain_http_exception_keeps_detail_and_gets_a_generic_code():
    app = _app({"/s": HTTPException(404, detail="Нет такого"), "/d": HTTPException(400, detail={"message": "м", "code": "custom_code"}),
                "/odd": HTTPException(418, detail="чайник")})
    assert _call(app, "/s")[2] == {"detail": "Нет такого", "code": "not_found", "params": {}}
    assert _call(app, "/d")[2] == {"detail": {"message": "м", "code": "custom_code"}, "code": "custom_code", "params": {}}
    assert _call(app, "/odd")[2]["code"] == "client_error"


def test_unknown_address_and_wrong_method_use_the_same_format():
    app = _app({"/x": ApiError(400, "bad_request", "—")})
    status, _, body = _call(app, "/nope")
    assert status == 404 and body["code"] == "not_found" and set(body) == {"detail", "code", "params"}
    status, _, body = _call(app, "/x", method="DELETE")
    assert status == 405 and body["code"] == "method_not_allowed"


def test_unexpected_exception_leaks_nothing_and_is_logged(caplog):
    app = _app({"/boom": _Kaboom(SECRET_TEXT)})
    with caplog.at_level(logging.ERROR, logger="uvicorn.error"):
        status, _, body = _call(app, "/boom")
    assert status == 500
    assert body == {"detail": INTERNAL_ERROR_MESSAGE, "code": "internal_error", "params": {}}
    raw = json.dumps(body, ensure_ascii=False)
    for leak in ("psycopg", "SELECT", "argon2", "/srv/", "secret", "Traceback", "_Kaboom", "RuntimeError"):
        assert leak not in raw, leak
    assert any("_Kaboom" in r.getMessage() for r in caplog.records)      # подробности — в журнале сервера
    assert any(r.exc_info for r in caplog.records)


def test_internal_kind_app_error_is_logged(caplog):
    class Broken(AppError):
        kind = "internal"
        default_code = "internal_error"

    with caplog.at_level(logging.ERROR, logger="uvicorn.error"):
        status, _, body = _call(_app({"/x": Broken("Сбой хранилища")}), "/x")
    assert status == 500 and body["code"] == "internal_error"
    assert any("Broken" in r.getMessage() for r in caplog.records)


def test_validation_errors_drop_the_submitted_values():
    class FakeValidation:
        def errors(self):
            return [{"loc": ("body", "password"), "msg": "too short", "type": "string_too_short", "input": "hunter2-secret",
                     "ctx": {"min_length": 10}}, "мусор"]

    response = asyncio.run(validation_error_handler(None, FakeValidation()))
    body = json.loads(response.body)
    assert response.status_code == 422 and body["code"] == "validation_error"
    assert body["detail"] == [{"loc": ["body", "password"], "msg": "too short", "type": "string_too_short"}]
    assert "hunter2" not in response.body.decode()


@pytest.mark.parametrize("exc", [
    PhotoError("Фото слишком маленькое", code="photo_too_small", params={"width": 10, "height": 20}),
    StoreInputError("Код полки «A 1» недопустим", "shelf_code_invalid", {"code": "A 1"}),
    NotReadyError("Модель ещё загружается"),
    UserAlreadyExistsError("Пользователь «a» уже существует", params={"username": "a"}),
])
def test_celery_style_reconstruction_keeps_code_params_and_text(exc):
    """Celery передаёт ошибку задачи как тип + args (через JSON) и восстанавливает вызовом Класс(*args)."""
    args = json.loads(json.dumps(exc.args))
    rebuilt = type(exc)(*args)
    assert (str(rebuilt), rebuilt.code, rebuilt.params) == (str(exc), exc.code, exc.params)
    api = from_app_error(rebuilt, fallback_code="catalog_error")
    assert api.code == exc.code and api.detail == str(exc)


def test_reconstruction_into_a_foreign_class_still_reads_text_code_and_params():
    """Если Celery пересоздал исключение не нашим классом (например, без импорта), текст — первый аргумент, а не кортеж."""
    foreign = Exception("Фото повреждено", "photo_corrupted", {"n": 2})
    assert (message_of(foreign), code_of(foreign), params_of(foreign)) == ("Фото повреждено", "photo_corrupted", {"n": 2})
    assert code_of(Exception("текст", "DROP TABLE x;")) is None          # мусор вместо кода не уходит клиенту
    api = from_app_error(Exception("старое"), status_code=422, fallback_code="catalog_error")
    assert (api.status_code, api.code, api.detail) == (422, "catalog_error", "старое")
