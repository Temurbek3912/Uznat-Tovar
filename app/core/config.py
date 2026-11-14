"""
Загрузка конфигурации приложения.

Приоритет: переменные окружения (.env) > configs/config.yaml > дефолты.
Используем pydantic-settings, чтобы конфиг был провалидирован при старте,
а не падал где-то в середине обработки снимка.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_YAML_PATH = PROJECT_ROOT / "configs" / "config.yaml"


def resolve_project_path(relative_or_absolute: str) -> Path:
    """Резолвит путь из конфига/настроек относительно корня проекта, если он не абсолютный.

    Нужно потому, что uvicorn и celery-воркер (см. RUNBOOK.md, шаг 6) обычно
    запускаются как два отдельных процесса, возможно, из разных текущих
    директорий — простой относительный Path() без якоря к корню проекта был
    бы недетерминирован (создал/искал бы файл в разных местах в зависимости
    от того, откуда запущена команда).
    """
    p = Path(relative_or_absolute)
    return p if p.is_absolute() else PROJECT_ROOT / p


class DetectorConfig(BaseModel):
    name: str
    weights_path: str
    confidence_threshold: float = 0.35
    iou_threshold: float = 0.5
    max_detections: int = 1000      # потолок рамок на кадр: у Ultralytics умолчание 300, а на плотной полке SKU-110K бывает до ~700


class EmbedderConfig(BaseModel):
    name: str
    weights_path: str
    embedding_dim: int = 768


class MatcherConfig(BaseModel):
    index_path: str
    top_k: int = 5
    match_threshold: float = 0.72
    unknown_threshold: float = 0.45
    # Правило запаса: при высоком счёте, но малом отрыве от ВТОРОГО по близости другого товара — в ревью. 0 = выключено.
    # Значение подбирается по реальным данным: python -m ml.evaluate_matching (docs/pilot_plan.md).
    margin_threshold: float = 0.0


class OSAConfig(BaseModel):
    empty_slot_confidence: float = 0.6


class Settings(BaseSettings):
    """Настройки, которые чаще всего переопределяются через ENV (секреты, инфраструктура)."""

    model_config = SettingsConfigDict(env_file=".env", env_nested_delimiter="__", extra="ignore")

    database_url: str = "postgresql://postgres:postgres@localhost:5432/shelf_audit"
    queue_broker_url: str = "redis://localhost:6379/0"
    environment: str = "development"

    # Локальное хранилище снимков полок (см. app/api/routes_upload.py).
    # Относительный путь резолвится через resolve_project_path() относительно
    # корня проекта — общий для API и Celery-воркера, если оба работают на
    # одной машине (см. RUNBOOK.md) или через общий volume (docker-compose.yml:
    # `.:/srv/app` у обоих сервисов). Уже в .gitignore (data/).
    # TODO(прод): заменить на S3-совместимое хранилище — см. TODO в routes_upload.py.
    media_root: str = "data"

    # Максимальный размер загружаемого снимка, МБ (ENV MAX_UPLOAD_MB). Фото с телефона 12 Мп — 3–8 МБ;
    # без лимита один кривой клиент мог бы забить память и диск API.
    max_upload_mb: int = 25

    # Время жизни токена доступа (F13), минуты. 720 = 12 часов — одна рабочая смена.
    # Задаётся через config.yaml (auth.token_ttl_minutes) или ENV AUTH_TOKEN_TTL_MINUTES.
    auth_token_ttl_minutes: int = 720

    # Лимит неудачных попыток входа (см. app/core/throttle.py): по логину и по IP в скользящем окне.
    auth_max_failed_logins_per_user: int = 5
    auth_max_failed_logins_per_ip: int = 20
    auth_lockout_window_minutes: int = 15
    # Где считать неудачные попытки входа: auto — в Redis очереди (один счётчик для всех входов программы), если адрес очереди
    # redis://, иначе в памяти; memory — всегда в памяти процесса; redis — всегда Redis (при сбое — память, app/core/throttle_redis.py).
    auth_throttle_backend: str = "auto"

    # Ключ шифрования секретов камер (RTSP-URL с паролем хранятся в БД только зашифрованными). Умолчания НЕТ намеренно:
    # без ключа камеры добавить нельзя. Создать: python -m scripts.generate_secret_key; положить в .env (CAMERA_SECRET_KEY=...).
    # Потеряете ключ — адреса камер в БД не расшифровать (камеры придётся ввести заново), поэтому сохраните его отдельно.
    camera_secret_key: str = ""

    # Секрет сервера для входа (документ 18): шифрует секреты второго фактора (TOTP) в базе и «солит» отпечатки IP-адресов
    # сеансов. Умолчания НЕТ намеренно: без ключа второй фактор включить нельзя (вход по паролю работает как раньше).
    # Создать: python -m scripts.generate_secret_key; положить в .env (AUTH_SECRET_KEY=...). Потеря ключа = все включённые
    # вторые факторы перестают проверяться (сбросить: python -m scripts.user_admin reset-2fa --username …).
    auth_secret_key: str = ""

    # Второй шаг входа: сколько минут ждать код из приложения и сколько неверных кодов допускается на один пропуск.
    auth_mfa_challenge_minutes: int = 5
    auth_mfa_max_attempts: int = 5

    # Решения владельца 2026-11-08 (документ 18, §7; правила — app/core/mfa_policy.py):
    # В-2, В-3 — второй фактор обязателен всем администраторам, везде. Выключать — только осознанно (например, на время
    # восстановления), в configs/config.yaml: auth.require_2fa_for_admins: false.
    auth_require_2fa_for_admins: bool = True
    # В-4 — предел жизни сеанса администратора и любого сеанса административного входа, часов (продление его не сдвигает).
    auth_admin_session_max_hours: int = 8
    # Важные операции (пользователи, роли, пароли, удаление магазинов, сброс второго фактора) — только если пароль и код
    # подтверждены не раньше чем столько минут назад.
    auth_reauth_minutes: int = 10
    # Срок временного пароля в часах (документ 05, §12.2; вопрос Q-19 открыт, рекомендация — 24). 0 — без срока.
    auth_temp_password_hours: int = 24
    # Пароли, коды и токены — только по HTTPS или через VPN/административный вход (решение владельца 2026-11-08, документ 18, §14).
    auth_require_secure_transport: bool = True
    # Защищённый адрес для сотрудников, который страница покажет вместо входа по HTTP (например, https://192.168.1.10:8000).
    public_https_url: str = ""

    # В-1 — какой это вход программы (переменная APP_LISTENER): public — порт для сотрудников (локальная сеть), разрешения
    # управления на нём НЕ действуют; admin — административный порт (публикуется только на 127.0.0.1 и адресе VPN);
    # all — один процесс без разделения (запуск без Docker). docker-compose.yml задаёт public для api и admin для admin-api.
    app_listener: str = "all"
    # Имена, по которым открывают административный вход (кроме localhost/127.0.0.1/::1): адрес компьютера в VPN (VPN_BIND_IP — тот же,
    # что в docker-compose.vpn.yml) и, через запятую, другие имена (ADMIN_ALLOWED_HOSTS, например имя в Tailscale). Запрос
    # с другим именем в заголовке Host прав управления не получает (защита от подмены DNS — mfa_policy.effective_listener).
    vpn_bind_ip: str = ""
    admin_allowed_hosts: str = ""

    # Источники, которым браузер разрешено ходить в API с другого адреса (CORS), через запятую. Пусто: development — любой
    # источник, production — кросс-доменный доступ закрыт. Разбор и проверка — app/core/cors.py (ENV CORS_ALLOW_ORIGINS).
    cors_allow_origins: str = ""

    # Часовой пояс отчётов и «календарных суток» (Узбекистан: UTC+5, перехода на летнее время нет).
    app_timezone: str = "Asia/Tashkent"

    # ML-конфиги подтягиваются из YAML (см. load_yaml_config), но могут быть
    # переопределены через ENV вида MODELS__DETECTOR__CONFIDENCE_THRESHOLD=0.4
    detector: DetectorConfig | None = None
    embedder: EmbedderConfig | None = None
    matcher: MatcherConfig | None = None
    osa: OSAConfig | None = None


def _load_yaml() -> dict[str, Any]:
    if not CONFIG_YAML_PATH.exists():
        return {}
    with open(CONFIG_YAML_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache
def get_settings() -> Settings:
    raw = _load_yaml()
    models = raw.get("models", {})
    database = raw.get("database", {})
    queue = raw.get("queue", {})
    osa = raw.get("osa", {})
    app_cfg = raw.get("app", {})
    auth_cfg = raw.get("auth", {})

    # БАГФИКС (найдено при повторном ревью, не выполнением — pydantic по-прежнему
    # недоступен в sandbox): раньше YAML-значения передавались как явные kwargs
    # в конструктор Settings(...). В pydantic-settings явный kwarg конструктора
    # ВСЕГДА побеждает над источником ENV/.env — то есть переменные окружения
    # DATABASE_URL / QUEUE_BROKER_URL / ENVIRONMENT тихо игнорировались,
    # несмотря на докстринг модуля («ENV (.env) > config.yaml > дефолты»).
    # Чтобы порядок приоритетов реально соблюдался, YAML-значения должны быть
    # ДЕФОЛТАМИ полей (через динамический подкласс), а не явными kwargs —
    # тогда обычный порядок источников pydantic-settings (kwargs > env >
    # dotenv > дефолт поля) сам расставит всё как в докстринге: если ENV/.env
    # не заданы, в дело вступит именно YAML-дефолт, а не хардкод.
    class _SettingsWithYamlDefaults(Settings):
        database_url: str = database.get("url", Settings.model_fields["database_url"].default)
        queue_broker_url: str = queue.get("broker_url", Settings.model_fields["queue_broker_url"].default)
        environment: str = app_cfg.get("environment", Settings.model_fields["environment"].default)
        auth_token_ttl_minutes: int = auth_cfg.get(
            "token_ttl_minutes", Settings.model_fields["auth_token_ttl_minutes"].default
        )
        auth_max_failed_logins_per_user: int = auth_cfg.get(
            "max_failed_logins_per_user", Settings.model_fields["auth_max_failed_logins_per_user"].default
        )
        auth_max_failed_logins_per_ip: int = auth_cfg.get(
            "max_failed_logins_per_ip", Settings.model_fields["auth_max_failed_logins_per_ip"].default
        )
        auth_lockout_window_minutes: int = auth_cfg.get(
            "lockout_window_minutes", Settings.model_fields["auth_lockout_window_minutes"].default
        )
        auth_mfa_challenge_minutes: int = auth_cfg.get(
            "mfa_challenge_minutes", Settings.model_fields["auth_mfa_challenge_minutes"].default
        )
        auth_mfa_max_attempts: int = auth_cfg.get("mfa_max_attempts", Settings.model_fields["auth_mfa_max_attempts"].default)
        auth_require_2fa_for_admins: bool = auth_cfg.get(
            "require_2fa_for_admins", Settings.model_fields["auth_require_2fa_for_admins"].default
        )
        auth_admin_session_max_hours: int = auth_cfg.get(
            "admin_session_max_hours", Settings.model_fields["auth_admin_session_max_hours"].default
        )
        auth_reauth_minutes: int = auth_cfg.get("reauth_minutes", Settings.model_fields["auth_reauth_minutes"].default)
        auth_temp_password_hours: int = auth_cfg.get(
            "temp_password_hours", Settings.model_fields["auth_temp_password_hours"].default
        )
        auth_require_secure_transport: bool = auth_cfg.get(
            "require_secure_transport", Settings.model_fields["auth_require_secure_transport"].default
        )

    return _SettingsWithYamlDefaults(
        detector=DetectorConfig(**models["detector"]) if "detector" in models else None,
        embedder=EmbedderConfig(**models["embedder"]) if "embedder" in models else None,
        matcher=MatcherConfig(**models["matcher"]) if "matcher" in models else None,
        osa=OSAConfig(**osa) if osa else None,
    )
