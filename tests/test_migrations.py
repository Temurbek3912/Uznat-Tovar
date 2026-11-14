"""Миграции схемы (Alembic, шаг Р-2): пустая база, повторный запуск, принятие старой базы без потери данных, отказ при устаревших
таблицах, итог миграций = модели, откат каждой ревизии кроме базовой. На SQLite всегда; на PostgreSQL — если задан
FULL_BACKUP_TEST_DB_URL (адрес служебной базы временного сервера: тест создаёт и удаляет свою базу)."""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("alembic")

from alembic import command  # noqa: E402
from alembic.autogenerate import compare_metadata  # noqa: E402
from alembic.runtime.migration import MigrationContext  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402

from app.db import migrate  # noqa: E402
from app.db.models import Base  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
AUTH_TABLES = ("user_security", "auth_session_meta", "auth_mfa_challenges", "auth_recovery_codes")


def _sqlite(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")


def _postgres_engines():
    url = os.environ.get("FULL_BACKUP_TEST_DB_URL")
    if not url:
        return []
    return [url]


@pytest.fixture(params=["sqlite", "postgresql"])
def engine(request, tmp_path):
    if request.param == "sqlite":
        eng = _sqlite(tmp_path)
        yield eng
        eng.dispose()
        return
    urls = _postgres_engines()
    if not urls:
        pytest.skip("нет FULL_BACKUP_TEST_DB_URL — временного PostgreSQL")
    from app.db.url import normalize_database_url
    admin = create_engine(normalize_database_url(urls[0]), isolation_level="AUTOCOMMIT")
    name = f"mig_{uuid.uuid4().hex[:10]}"
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    eng = create_engine(admin.url.set(database=name))
    try:
        yield eng
    finally:
        eng.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


def _legacy(eng, *, without=()):
    """База «как до миграций»: схема базовой ревизии (то, что делал create_all до 2026-11-13), без alembic_version и без таблиц
    without. Текущие модели для этого не годятся — в них уже есть колонки следующих ревизий."""
    with eng.begin() as conn:
        command.upgrade(migrate.alembic_config(conn), migrate.BASELINE_REVISION)
    with eng.begin() as conn:
        for table in ("alembic_version", *without):
            conn.execute(text(f"DROP TABLE {table}"))


def _diff(eng):
    with eng.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        return [d for d in compare_metadata(ctx, Base.metadata)]


def test_empty_database_gets_every_table_and_matches_the_models(engine):
    result = migrate.upgrade_to_head(engine)
    assert result.state == migrate.EMPTY and result.before is None and result.after == migrate.head_revision()
    assert set(result.created_tables) == set(Base.metadata.tables)
    assert _diff(engine) == [], "итог миграций расходится с app/db/models.py — нужна новая ревизия"


def test_second_run_changes_nothing(engine):
    migrate.upgrade_to_head(engine)
    again = migrate.upgrade_to_head(engine)
    assert again.state == migrate.VERSIONED and again.created_tables == [] and again.before == again.after


def test_legacy_database_is_adopted_without_touching_existing_tables_and_data(engine):
    _legacy(engine, without=AUTH_TABLES)                 # база, созданная bootstrap до миграций (у владельца — без таблиц входа)
    with engine.begin() as c:
        c.execute(text("INSERT INTO stores (id, name, address, format) VALUES ('s1', 'Магазин', '-', 'supermarket')"))
        c.execute(text("INSERT INTO users (id, username, password_hash, role, is_active, created_at) "
                       "VALUES ('u1', 'owner', 'h', 'admin', true, CURRENT_TIMESTAMP)"))
    result = migrate.upgrade_to_head(engine)
    assert result.state == migrate.LEGACY and result.applied and result.after == migrate.head_revision()
    assert set(AUTH_TABLES) <= set(result.created_tables) and "stores" not in result.created_tables
    with engine.connect() as c:
        assert c.execute(text("SELECT name FROM stores")).scalar_one() == "Магазин"
        assert c.execute(text("SELECT username FROM users")).scalar_one() == "owner"
    assert _diff(engine) == []


def test_legacy_database_with_outdated_tables_is_refused_and_left_exactly_as_it_was(engine):
    Base.metadata.create_all(engine, tables=[Base.metadata.tables["users"]])
    with engine.begin() as c:
        c.execute(text("ALTER TABLE users DROP COLUMN is_active"))     # старая таблица без колонки базовой ревизии
    tables_before = set(inspect(engine).get_table_names())
    result = migrate.upgrade_to_head(engine)
    assert not result.applied and result.drift == {"users": ["is_active"]} and result.after is None
    assert set(inspect(engine).get_table_names()) == tables_before        # даже alembic_version не создана


def test_ensure_current_raises_a_plain_message_on_outdated_tables(tmp_path):
    eng = _sqlite(tmp_path)
    Base.metadata.create_all(eng, tables=[Base.metadata.tables["stores"]])
    with eng.begin() as c:
        c.execute(text("ALTER TABLE stores DROP COLUMN format"))
    with pytest.raises(RuntimeError, match="stores: format"):
        migrate.ensure_current(eng)


def test_baseline_cannot_be_downgraded(tmp_path):
    eng = _sqlite(tmp_path)
    migrate.upgrade_to_head(eng)
    with eng.begin() as conn:
        command.downgrade(migrate.alembic_config(conn), migrate.BASELINE_REVISION)
    with eng.begin() as conn:
        with pytest.raises(RuntimeError, match="запрещён"):
            command.downgrade(migrate.alembic_config(conn), "base")
    assert _current(eng) == migrate.BASELINE_REVISION and "users" in inspect(eng).get_table_names()


def _current(eng):
    with eng.connect() as conn:
        return migrate.current_revision(conn)


def test_every_revision_after_the_baseline_goes_down_and_up_again(engine):
    """С последней ревизии вниз: каждая откатывается к предыдущей и применяется снова; в конце — снова последняя и = модели."""
    migrate.upgrade_to_head(engine)
    script = ScriptDirectory.from_config(migrate.alembic_config())
    revisions = [r for r in script.walk_revisions() if r.revision != migrate.BASELINE_REVISION]   # от новой к старой
    for rev in revisions:
        assert _current(engine) == rev.revision
        with engine.begin() as conn:
            command.downgrade(migrate.alembic_config(conn), "-1")
        assert _current(engine) == rev.down_revision, rev.revision
        with engine.begin() as conn:
            command.upgrade(migrate.alembic_config(conn), "+1")
        assert _current(engine) == rev.revision, rev.revision
        with engine.begin() as conn:
            command.downgrade(migrate.alembic_config(conn), "-1")
    assert _current(engine) == migrate.BASELINE_REVISION
    migrate.upgrade_to_head(engine)
    assert _current(engine) == migrate.head_revision() and _diff(engine) == []


def test_revision_chain_is_linear_and_files_are_named_by_revision():
    script = ScriptDirectory.from_config(migrate.alembic_config())
    assert len(script.get_heads()) == 1, "у миграций несколько концов — нужна ревизия-слияние"
    for path in (ROOT / "migrations" / "versions").glob("*.py"):
        rev = re.search(r'^revision: str = "(\w+)"', path.read_text(encoding="utf-8"), re.M).group(1)
        assert path.name.startswith(rev + "_"), path.name


def test_no_create_all_left_in_application_code():
    """Схема создаётся только миграциями: create_all в коде приложения и скриптах означал бы базу без ревизии."""
    offenders = []
    for folder in ("app", "scripts"):
        for path in (ROOT / folder).rglob("*.py"):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"\.create_all\(", line) and not line.lstrip().startswith("#"):
                    offenders.append(f"{path.relative_to(ROOT)}:{n}")
    assert offenders == []


def test_audit_journal_is_append_only_in_the_database_itself(engine):
    """Миграция 0004: изменить или удалить запись журнала не может никто — отказывает сама база (документ 05, §16.3)."""
    from sqlalchemy.exc import DBAPIError
    migrate.upgrade_to_head(engine)
    with engine.begin() as c:
        c.execute(text("INSERT INTO audit_events (id, at, via, action) VALUES ('e1', CURRENT_TIMESTAMP, 'api', 'user.created')"))
    for sql in ("UPDATE audit_events SET action = 'forged'", "DELETE FROM audit_events"):
        with pytest.raises(DBAPIError):
            with engine.begin() as c:
                c.execute(text(sql))
    with engine.connect() as c:
        assert c.execute(text("SELECT action FROM audit_events")).scalar_one() == "user.created"
