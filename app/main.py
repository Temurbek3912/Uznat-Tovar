from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pathlib import Path

from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes_admin import router as admin_router
from app.api.routes_auth import router as auth_router
from app.api.routes_catalog import router as catalog_router
from app.api.routes_public import router as public_router
from app.api.routes_stores import router as stores_router
from app.core.config import get_settings, resolve_project_path
from app.core.cors import describe_cors, is_production, resolve_cors_origins
from app.core import auth_secrets
from app.core.mfa_policy import LISTENER_ADMIN, normalize_listener, startup_warnings
from app.core.secure_transport import SecureTransportMiddleware
from app.core.customer_gate import CUSTOMER_FILES, CustomerGateMiddleware
from app.core.readiness import CheckFailed, run_checks
from app.core.security_headers import SecurityHeadersMiddleware
from app.core.error_handlers import install_error_handlers
from app.services.frame_lifecycle import PeriodicSweeper, sweep_frames
from app.core.body_limit import DEFAULT_LIMIT, BodySizeLimitMiddleware, build_rules
from app.api.routes_catalog import IDENTIFY_MAX_MB, MAX_PHOTOS_PER_REQUEST

# Логгер uvicorn: собственного логирования у API нет, и INFO из logging.getLogger(__name__) в журнал контейнера не попал бы.
logger = logging.getLogger("uvicorn.error")
settings = get_settings()

# В production не публикуем Swagger/ReDoc/openapi.json: схема API — это карта эндпоинтов для атакующего.
_docs_enabled = settings.environment != "production"

for _line in startup_warnings(require_for_admins=settings.auth_require_2fa_for_admins,
                              key_configured=auth_secrets.is_configured(settings.auth_secret_key), listener=settings.app_listener):
    if (settings.app_listener or "").strip().lower() != "customer":     # на входе покупателя входа сотрудников нет — и ключ не нужен
        logger.warning("%s", _line)


@asynccontextmanager
async def lifespan(_app):
    """Уборка временных кадров «Узнать товар» (ТК-2, временная мера D-10а): сразу при запуске API и затем каждые 10 секунд,
    независимо от того, работает ли воркер. Кадры пишет API — API их и убирает."""
    media_root = resolve_project_path(settings.media_root)
    sweeper = PeriodicSweeper(lambda: sweep_frames(media_root))
    sweeper.start()
    backups = None
    if settings.lite_mode:
        # облегчённый режим (документ 11, §10): отдельного воркера нет — модели грузятся здесь, в фоне (API отвечает сразу)
        import threading

        from app.worker import start_in_process
        threading.Thread(target=start_in_process, name="lite-ml-init", daemon=True).start()
        # резервная копия магазина раз в сутки (app/services/lite_backup.py): в папку программы и в облако, если найдено
        from app.services import lite_backup
        try:
            backups = lite_backup.service_from_settings(settings)
        except lite_backup.BackupError as exc:
            logger.warning("Резервная копия выключена: %s", exc)
        if backups is not None:
            backups.start()
    elif settings.naming_enabled and settings.naming_kind_model:
        # полный режим: малая модель «вид товара» (документ 07, §10.7) — в фоне, чтобы первый снимок покупателя её не ждал
        import threading

        from app.pipeline.kind_classifier import preload
        threading.Thread(target=preload, args=(settings,), name="kind-model-init", daemon=True).start()
    try:
        yield
    finally:
        sweeper.stop()
        if backups is not None:
            backups.stop(timeout=1.0)


app = FastAPI(
    title="Retail Shelf Audit API",
    description="Автоматический учёт и классификация товаров на полках магазина по изображению",
    version="0.1.0",
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
    lifespan=lifespan,
)

# Предел размера тела запроса ДО разбора (app/core/body_limit.py): без него Starlette принимает и пишет на диск
# multipart любого размера раньше, чем маршрут проверит файл и права. Добавляется первым, то есть оказывается
# ВНУТРИ CORS и заголовков безопасности: ответ 413 получает и те, и другие заголовки.
app.add_middleware(
    BodySizeLimitMiddleware,
    default_limit=DEFAULT_LIMIT,
    rules=build_rules(identify_max_mb=IDENTIFY_MAX_MB, max_upload_mb=settings.max_upload_mb,
                      max_photos_per_request=MAX_PHOTOS_PER_REQUEST),
)

# CORS нужен только страницам, открытым как файл (origin "null") или на другом адресе (?api_base=...): страницы,
# раздаваемые самим API (/ui/...), ходят на тот же источник. Что разрешено — решает CORS_ALLOW_ORIGINS
# (app/core/cors.py): development без значения — любой источник (как раньше, для запуска дашборда из файла),
# production без значения — middleware не подключается вовсе (чужой источник браузер блокирует).
_cors_origins = resolve_cors_origins(settings.cors_allow_origins, settings.environment)
if normalize_listener(settings.app_listener) == LISTENER_ADMIN:
    # Административный вход (документ 18, §4): страницы управления открываются с него самого — чужим источникам доступ не нужен.
    _cors_origins = []
if _cors_origins == ["*"] and is_production(settings.environment):
    logger.warning("%s", describe_cors(_cors_origins, settings.environment))
else:
    logger.info("%s", describe_cors(_cors_origins, settings.environment))
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        # DELETE нужен админ-панели (удаление магазинов, полок, камер), если она открыта с другого источника.
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        # F13: заголовки перечислены явно. Спецификация CORS не позволяет подстановочному "*"
        # покрывать Authorization — с "*" поведение зависело бы от реализации middleware.
        # Токен передаётся заголовком (не cookie), поэтому allow_credentials не нужен.
        allow_headers=["Authorization", "Content-Type"],
        # Retry-After и Content-Disposition не входят в список заголовков ответа, доступных JS по
        # умолчанию (CORS-safelisted): без первого дашборд не смог бы показать «повторите через N минут»
        # при 429, без второго — не узнал бы имя файла экспорта (F14) из ответа /report/export.
        expose_headers=["Retry-After", "Content-Disposition"],
    )

# Пароли, коды и токены — только по защищённому каналу (app/core/secure_transport.py, документ 18, §14): /api/v1/* по HTTP через
# Wi-Fi магазина отклоняется. Внутри заголовков безопасности: ответ 403 получает и их.
app.add_middleware(SecureTransportMiddleware, settings_fn=get_settings)

# Вход покупателя из интернета (app/core/customer_gate.py, docs/PUBLIC_ACCESS.md): на сокете CUSTOMER_PORT (127.0.0.1, к нему
# подключён туннель) — только страница покупателя и публичный API; служебное — 404. Снаружи SecureTransport: до него не доходит.
app.add_middleware(CustomerGateMiddleware, port_fn=lambda: get_settings().customer_port)

# Заголовки безопасности на всех ответах (app/core/security_headers.py): nosniff, запрет встраивания в чужие сайты и др.
app.add_middleware(SecurityHeadersMiddleware)


# Единые ошибки (app/core/error_handlers.py): каждый ответ-ошибка — {"detail": как раньше, "code": код, "params": параметры};
# неожиданные исключения — 500 с общим текстом, подробности только в журнал сервера.
install_error_handlers(app, cors_origins=_cors_origins)

# Вход покупателя (APP_LISTENER=customer, документ 04, §3.5; В-5): ТОЛЬКО публичный API и страница покупателя — маршрутов
# сотрудников и управления на нём нет вовсе (их нельзя открыть даже с украденным токеном). Остальные входы — как раньше, плюс
# публичный API (он без входа и с ограничением частоты), кроме административного.
CUSTOMER_ENTRANCE = (settings.app_listener or "").strip().lower() == "customer"
if CUSTOMER_ENTRANCE:
    app.include_router(public_router)
else:
    app.include_router(auth_router)
    app.include_router(catalog_router)
    app.include_router(stores_router)
    app.include_router(admin_router)
    if normalize_listener(settings.app_listener) != LISTENER_ADMIN:
        app.include_router(public_router)

# Фронтенд раздаётся самим API: телефон не может открыть HTML-файл с ноутбука, а на странице «localhost» —
# это сам телефон. Страницы открываются как http://<адрес-сервера>:8000/ui/index.html и ходят в API
# того же источника (apiBaseUrl() в страницах берёт window.location.origin). Только статика — данных тут нет,
# доступ к ним по-прежнему закрыт токеном.
_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
# Вход покупателя раздаёт ТОЛЬКО страницу покупателя и то, что она подключает: страниц сотрудников и управления на нём нет.
# CUSTOMER_FILES — app/core/customer_gate.py (тот же список для входа покупателя из интернета).


class _CustomerStaticFiles(StaticFiles):
    """StaticFiles со списком разрешённых файлов: остальное — 404, как будто его нет."""

    def get_path(self, scope) -> str:
        path = super().get_path(scope).replace("\\", "/").lstrip("/")
        if path not in CUSTOMER_FILES:
            from starlette.exceptions import HTTPException as StarletteHTTPException
            raise StarletteHTTPException(status_code=404)
        return path


if _FRONTEND_DIR.is_dir():
    _static = _CustomerStaticFiles if CUSTOMER_ENTRANCE else StaticFiles
    app.mount("/ui", _static(directory=str(_FRONTEND_DIR), html=not CUSTOMER_ENTRANCE), name="ui")


@app.get("/", include_in_schema=False)
def root_redirect() -> RedirectResponse:
    """Адрес сервера без пути (http://192.168.1.50:8000) открывает главную: крупные кнопки «Сфотографировать», «Результаты» и т.д.
    На входе покупателя (адрес из QR-кода) — страницу покупателя."""
    return RedirectResponse(url="/ui/shop.html" if CUSTOMER_ENTRANCE else "/ui/index.html")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "environment": settings.environment}


def _check_database() -> None:
    from sqlalchemy import text

    from app.db.session import engine

    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))


def _check_schema() -> None:
    from sqlalchemy import inspect

    from app.db.models import Base
    from app.db.session import engine

    missing = sorted(set(Base.metadata.tables) - set(inspect(engine).get_table_names()))
    if missing:
        raise CheckFailed(f"нет таблиц: {', '.join(missing)} — выполните: docker compose run --rm bootstrap")
    # Таблицы на месте, но миграции не применены (код новее базы: нет колонок) — тоже «не готов», а не 500 на каждом запросе.
    from app.db.migrate import current_revision, head_revision
    with engine.connect() as conn:
        current = current_revision(conn)
    if current != head_revision():
        raise CheckFailed(f"схема базы не обновлена (ревизия {current or 'нет'}, нужна {head_revision()}) — выполните: "
                          "docker compose run --rm bootstrap")


def _check_queue() -> None:
    if settings.lite_mode:       # облегчённый режим: очереди нет, задачи выполняются в этом процессе
        return
    import redis

    redis.Redis.from_url(settings.queue_broker_url, socket_connect_timeout=2, socket_timeout=2).ping()


# def, а не async def: проверки синхронные и блокирующие (сеть/БД) — FastAPI выполнит их в пуле потоков, не в event loop.
@app.get("/health/ready", include_in_schema=False)
def health_ready() -> JSONResponse:
    """Готовность к работе: БД, схема, очередь. 200 — всё в порядке, 503 — чего-то нет (см. тело ответа)."""
    result = run_checks({"database": _check_database, "schema": _check_schema, "queue": _check_queue})
    return JSONResponse(result, status_code=200 if result["status"] == "ok" else 503)
