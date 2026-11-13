"""
Ошибки приложения без привязки к веб-фреймворку (сервисы, воркер, скрипты): единый базовый класс, вид ошибки и стабильный код.

Единая система ошибок (2026-11-07):

  * `AppError` (здесь) — исключение сервиса: текст по-русски (для журнала и прежних клиентов), стабильный машинный `code`
    (не зависит от языка интерфейса), параметры сообщения `params` и вид `kind`. Сервис не знает про HTTP — только вид ошибки.
  * `kind` → HTTP-статус (`KIND_STATUS`): invalid 422, not_found 404, conflict 409, forbidden 403, unauthorized 401,
    too_many 429, unavailable 503, bad_request 400, internal 500.
  * `app/core/errors.py` — `ApiError` (HTTP-ошибка с кодом) и перевод `AppError` → `ApiError`;
    `app/core/error_handlers.py` — единые обработчики FastAPI: тело ответа всегда
    {"detail": <как раньше>, "code": <код>, "params": {...}}.
  * Страница показывает сообщение по ключу `server.<code>` на языке человека (frontend/i18n.*.js); перевода нет — прежний `detail`.

Коды — только латиница в нижнем регистре и «_». Новый код без перевода во всех словарях роняет tests/test_error_codes.py.

Аргументы исключения хранятся позиционно (message, code, params): Celery передаёт ошибку задачи из воркера в API как
тип + args и восстанавливает её вызовом `Класс(*args)`; так код и параметры не теряются по дороге.
"""
from __future__ import annotations

KIND_STATUS: dict[str, int] = {
    "invalid": 422,
    "not_found": 404,
    "conflict": 409,
    "forbidden": 403,
    "unauthorized": 401,
    "too_many": 429,
    "unavailable": 503,
    "bad_request": 400,
    "internal": 500,
}

# Код по HTTP-статусу — для ошибок без собственного кода (404 неизвестного адреса, 405 и т. п. от самого фреймворка).
GENERIC_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "request_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "too_many_requests",
    500: "internal_error",
    502: "bad_gateway",
    503: "service_unavailable",
    504: "timeout",
}

INTERNAL_ERROR_CODE = "internal_error"
INTERNAL_ERROR_MESSAGE = "Внутренняя ошибка сервера. Попробуйте ещё раз позже."


class AppError(Exception):
    """Ошибка сервиса, понятная человеку. Повтор с теми же данными не поможет (кроме вида unavailable)."""

    kind = "invalid"
    default_code: str | None = None

    def __init__(self, message: str = "", code: str | None = None, params: dict | None = None):
        super().__init__(message, code or self.default_code, dict(params or {}))

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ""

    @property
    def message(self) -> str:
        return str(self)

    @property
    def code(self) -> str | None:
        return self.args[1] if len(self.args) > 1 else self.default_code

    @property
    def params(self) -> dict:
        value = self.args[2] if len(self.args) > 2 else None
        return dict(value) if isinstance(value, dict) else {}


_CODE_RE = __import__("re").compile(r"^[a-z][a-z0-9_]{0,63}$")


def message_of(exc: BaseException) -> str:
    """Текст ошибки приложения. У класса, пересозданного Celery по имени (без нашего __str__), — первый аргумент, а не кортеж."""
    if isinstance(exc, AppError):
        return str(exc)
    args = getattr(exc, "args", ())
    return args[0] if args and isinstance(args[0], str) else str(exc)


def code_of(exc: BaseException) -> str | None:
    code = getattr(exc, "code", None)
    if code is None:
        args = getattr(exc, "args", ())
        code = args[1] if len(args) > 1 else None
    return code if isinstance(code, str) and _CODE_RE.match(code) else None


def params_of(exc: BaseException) -> dict:
    params = getattr(exc, "params", None)
    if params is None:
        args = getattr(exc, "args", ())
        params = args[2] if len(args) > 2 else None
    return dict(params) if isinstance(params, dict) else {}


def status_for(exc: BaseException) -> int:
    """HTTP-статус ошибки приложения по её виду; не AppError — 500."""
    return KIND_STATUS.get(getattr(exc, "kind", "internal"), 500) if isinstance(exc, AppError) else 500


def generic_code(status: int) -> str:
    if status in GENERIC_CODES:
        return GENERIC_CODES[status]
    return "client_error" if 400 <= status < 500 else INTERNAL_ERROR_CODE


def error_payload(detail, code: str, params: dict | None = None) -> dict:
    """Тело ответа ошибки. detail — как раньше (строка по-русски или структура), code и params — для перевода."""
    return {"detail": detail, "code": code, "params": dict(params or {})}


def sanitize_validation_errors(errors) -> list[dict]:
    """Ошибки проверки запроса (FastAPI/pydantic) без присланных значений: в `input` мог оказаться пароль."""
    clean = []
    for item in errors or []:
        if not isinstance(item, dict):
            continue
        clean.append({
            "loc": [str(part) if not isinstance(part, int) else part for part in item.get("loc", ())],
            "msg": str(item.get("msg", "")),
            "type": str(item.get("type", "")),
        })
    return clean
