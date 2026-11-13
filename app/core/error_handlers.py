"""
Единые обработчики ошибок API: любой ответ-ошибка имеет вид {"detail", "code", "params"} (app/core/app_errors.py).

  * ApiError                 — как задано в маршруте (статус, код, параметры, заголовки Retry-After / WWW-Authenticate);
  * AppError из сервиса      — статус по виду ошибки, её код и параметры (маршрут может не ловить её сам);
  * HTTPException без кода   — detail как раньше, код по статусу (404 неизвестного адреса → not_found, 405 → method_not_allowed);
  * ошибка проверки запроса  — 422, code=validation_error, detail — список ошибок БЕЗ присланных значений (там мог быть пароль);
  * неожиданное исключение   — 500, code=internal_error, общий текст; подробности (тип, текст, трассировка) — только в журнал
    сервера. Клиенту не уходят ни SQL, ни пути, ни секреты.

Зависит только от Starlette (основа FastAPI): обработчики проверяются в облачной среде без FastAPI (tests/test_error_handlers.py).
"""
from __future__ import annotations

import logging

from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.app_errors import (INTERNAL_ERROR_CODE, INTERNAL_ERROR_MESSAGE, AppError, error_payload, generic_code,
                                 sanitize_validation_errors, status_for)
from app.core.errors import ApiError, error_body
from app.core.security_headers import headers_for

logger = logging.getLogger("uvicorn.error")


async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(error_body(exc), status_code=exc.status_code, headers=exc.headers)


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    status = status_for(exc)
    if status >= 500:
        logger.error("Ошибка сервиса %s на %s %s", type(exc).__name__, request.method, request.url.path, exc_info=exc)
    return JSONResponse(error_payload(str(exc), exc.code or generic_code(status), exc.params), status_code=status)


async def http_exception_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if isinstance(exc, ApiError):
        return await api_error_handler(_request, exc)
    code = generic_code(exc.status_code)
    if isinstance(exc.detail, dict) and isinstance(exc.detail.get("code"), str):
        code = exc.detail["code"]
    return JSONResponse(error_payload(exc.detail, code), status_code=exc.status_code, headers=getattr(exc, "headers", None))


async def validation_error_handler(_request: Request, exc) -> JSONResponse:
    errors = exc.errors() if hasattr(exc, "errors") else []
    return JSONResponse(error_payload(sanitize_validation_errors(errors), "validation_error"), status_code=422)


_cors_origins: list[str] = []          # те же разрешённые источники, что у CORSMiddleware (install_error_handlers)


def cors_headers_for(origin: str | None, allowed: list[str]) -> dict[str, str]:
    """Заголовки CORS для ответа 500: его отдаёт ServerErrorMiddleware снаружи CORSMiddleware, и без них страница с другого адреса
    не увидела бы даже код ошибки (видела бы «сеть недоступна»). Только для разрешённого источника."""
    if not origin or not allowed:
        return {}
    if allowed == ["*"]:
        return {"Access-Control-Allow-Origin": "*"}
    return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"} if origin in allowed else {}


async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Необработанная ошибка %s на %s %s", type(exc).__name__, request.method, request.url.path, exc_info=exc)
    # Этот ответ отдаёт ServerErrorMiddleware — самое внешнее звено, мимо SecurityHeadersMiddleware и CORS: заголовки ставятся здесь.
    headers = {**headers_for(request.scope), **cors_headers_for(request.headers.get("origin"), _cors_origins)}
    return JSONResponse(error_payload(INTERNAL_ERROR_MESSAGE, INTERNAL_ERROR_CODE), status_code=500, headers=headers)


def install_error_handlers(app, cors_origins: list[str] | None = None) -> None:
    """Подключает обработчики к приложению FastAPI/Starlette. RequestValidationError есть только в FastAPI.
    cors_origins — разрешённые источники CORS (для заголовков на ответе 500)."""
    global _cors_origins
    _cors_origins = list(cors_origins or [])
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    try:
        from fastapi.exceptions import RequestValidationError
    except ImportError:              # чистый Starlette (проверки в облачной среде)
        pass
    else:
        app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unexpected_error_handler)
