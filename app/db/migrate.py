"""
Применение миграций схемы (Alembic) — одно место для bootstrap, служебных скриптов и тестов (шаг Р-2, docs/PLAN_REVIEW_2026-11-13.md).

Три состояния базы:
  * empty     — таблиц приложения нет: применяются все миграции;
  * versioned — есть alembic_version: применяются недостающие миграции;
  * legacy    — таблицы есть, alembic_version нет (база создана до миграций через create_all, в том числе база владельца на
                уровне 2026-10-31). Сначала проверка: в существующих таблицах есть все колонки базовой ревизии. Если нет —
                НИЧЕГО не меняется, возвращается список недостающих колонок (bootstrap останавливается с объяснением). Если да —
                базовая ревизия создаёт только недостающие таблицы, затем применяются следующие миграции.

Параллельный запуск (api, worker и bootstrap стартуют вместе) безопасен: в PostgreSQL всё выполняется в одной транзакции под
транзакционной рекомендательной блокировкой; DDL в PostgreSQL транзакционный — при ошибке не остаётся половины изменений.
На рабочей базе владельца — только после проверенной полной копии (этап 2А, отдельное разрешение).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"
BASELINE_REVISION = "0001"
ADVISORY_LOCK_KEY = 727_001_001          # тот же ключ, что был у bootstrap до миграций

EMPTY, VERSIONED, LEGACY = "empty", "versioned", "legacy"


@dataclass
class MigrationResult:
    state: str                              # empty | versioned | legacy — до запуска
    before: str | None                      # ревизия до запуска (None — не было)
    after: str | None                       # ревизия после (при отказе — как была)
    created_tables: list[str] = field(default_factory=list)
    drift: dict[str, list[str]] = field(default_factory=dict)   # legacy: {таблица: [нет колонок]} — тогда ничего не менялось

    @property
    def applied(self) -> bool:
        return not self.drift


def alembic_config(connection: Connection | None = None):
    from alembic.config import Config
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["configure_logging"] = False          # не перенастраивать журналы приложения
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def head_revision() -> str:
    from alembic.script import ScriptDirectory
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def baseline_columns() -> dict[str, frozenset[str]]:
    from alembic.script import ScriptDirectory
    return ScriptDirectory.from_config(alembic_config()).get_revision(BASELINE_REVISION).module.BASELINE_COLUMNS


def current_revision(connection: Connection) -> str | None:
    from alembic.runtime.migration import MigrationContext
    return MigrationContext.configure(connection).get_current_revision()


def schema_state(connection: Connection) -> str:
    tables = set(inspect(connection).get_table_names())
    if "alembic_version" in tables and current_revision(connection) is not None:
        return VERSIONED
    return LEGACY if tables & set(baseline_columns()) else EMPTY


def legacy_drift(connection: Connection) -> dict[str, list[str]]:
    """{таблица: [колонки базовой ревизии, которых нет]} для таблиц, которые уже есть в базе."""
    inspector = inspect(connection)
    existing = set(inspector.get_table_names())
    drift = {}
    for table, columns in baseline_columns().items():
        if table in existing:
            missing = sorted(columns - {c["name"] for c in inspector.get_columns(table)})
            if missing:
                drift[table] = missing
    return drift


def upgrade_to_head(engine: Engine) -> MigrationResult:
    """Привести схему к последней ревизии. Возвращает, что было и что сделано; при расхождениях старой базы — ничего не меняет."""
    from alembic import command
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ADVISORY_LOCK_KEY})
        state = schema_state(conn)
        before = current_revision(conn) if state == VERSIONED else None
        tables_before = set(inspect(conn).get_table_names())
        if state == LEGACY:
            drift = legacy_drift(conn)
            if drift:
                return MigrationResult(state, before, before, drift=drift)
        command.upgrade(alembic_config(conn), "head")
        after = current_revision(conn)
        created = sorted(set(inspect(conn).get_table_names()) - tables_before - {"alembic_version"})
    return MigrationResult(state, before, after, created_tables=created)


def is_current(connection: Connection) -> bool:
    """Схема на последней ревизии? (для проверки готовности /health/ready)."""
    return current_revision(connection) == head_revision()


def ensure_current(engine: Engine) -> MigrationResult:
    """Для служебных скриптов (create_user, setup_store, seed_catalog): то же, что bootstrap, но при расхождениях старой базы —
    исключение с понятным текстом (раньше эти скрипты вызывали create_all и оставляли базу без ревизии)."""
    result = upgrade_to_head(engine)
    if result.drift:
        missing = "; ".join(f"{table}: {', '.join(cols)}" for table, cols in sorted(result.drift.items()))
        raise RuntimeError(f"Схема базы старее базовой ревизии, ничего не изменено ({missing}). Запустите bootstrap — он объяснит, что делать.")
    return result
