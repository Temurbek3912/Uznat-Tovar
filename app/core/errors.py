"""
HTTP-ошибка с кодом (единая система ошибок; основа — app/core/app_errors.py).

Ответ ошибки: {"detail": <текст по-русски или структура, как раньше>, "code": "<код>", "params": {...}}.
  * `detail` сохранён — прежние страницы и скрипты работают без изменений (в том числе структурированные 409 вида
    {"message", "product_id", "product_name"});
  * `code` и `params` позволяют странице показать сообщение на языке человека (ключ `server.<code>`);
    значение параметра вида {"key": "...", "params": {...}} страница переводит само (вложенная причина: «Фото 2: <причина>»).

ApiError наследует HTTPException Starlette (FastAPI использует её же), поэтому без особого обработчика она всё равно даёт
{"detail": ...}; с обработчиками app/core/error_handlers.py — полный формат.
"""
from __future__ import annotations

from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.app_errors import AppError, code_of, error_payload, message_of, params_of, status_for


class ApiError(StarletteHTTPException):
    """HTTP-ошибка с кодом и параметрами сообщения."""

    def __init__(self, status_code: int, code: str, message, params: dict | None = None,
                 headers: dict[str, str] | None = None):
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.code = code
        self.params = dict(params or {})


def error_body(exc: ApiError) -> dict:
    return error_payload(exc.detail, exc.code, exc.params)


def reason(code: str | None, params: dict | None = None) -> dict | None:
    """Вложенная причина для параметра сообщения: страница переведёт её по ключу server.<code>."""
    return {"key": f"server.{code}", "params": dict(params or {})} if code else None


def from_app_error(exc: BaseException, *, status_code: int | None = None, fallback_code: str = "invalid_input") -> ApiError:
    """Ошибка сервиса (AppError) -> ApiError: статус по виду ошибки (или заданный), код и параметры — из исключения.
    Восстановленная Celery ошибка старого вида (без кода) получает fallback_code; её текст — наш, по-русски."""
    status = status_code or status_for(exc)
    return ApiError(status, code_of(exc) or fallback_code, message_of(exc), params_of(exc))


def from_catalog_error(status_code: int, exc: Exception, *, fallback_code: str = "catalog_error") -> ApiError:
    """Прежнее имя (маршруты каталога): то же, что from_app_error с явным статусом."""
    return from_app_error(exc, status_code=status_code, fallback_code=fallback_code)


__all__ = ["ApiError", "AppError", "error_body", "from_app_error", "from_catalog_error", "reason"]
