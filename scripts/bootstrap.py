"""
Первый запуск одной командой: готовит БД и папки, создаёт первого администратора. Безопасно запускать сколько угодно
раз — ничего существующего не перезаписывает и не удаляет.

    docker compose up -d                 # bootstrap запускается САМ перед api и worker (сервис `bootstrap`)
    docker compose run --rm bootstrap    # то же вручную; в терминале спросит логин/пароль админа, если его ещё нет

Что делает, по шагам:
  1. ждёт, пока Postgres начнёт принимать подключения;
  2. применяет миграции схемы Alembic (app/db/migrate.py) под блокировкой Postgres — параллельный запуск не ломает схему.
     Пустая база — создаются все таблицы; база с миграциями — применяются недостающие. Старая база, созданная до миграций
     (create_all), принимается: сначала проверка, что в её таблицах есть все колонки базовой ревизии; если нет — скрипт
     останавливается с понятным сообщением и НИЧЕГО не меняет; если да — создаются только недостающие таблицы. Данные не трогаются;
  3. создаёт папки data/, models/index, models/detector, models/embedder, logs/;
  4. создаёт первого администратора — только если в БД нет ни одного активного admin И владелец ещё не назначен; созданный
     администратор становится ВЛАДЕЛЬЦЕМ системы (документ 05, §8; документ 18, §4.2). Если владелец есть, но все администраторы
     отключены, новый администратор НЕ создаётся (иначе любой, кто впишет ADMIN_PASSWORD в .env, получил бы управление):
     включите владельца с консоли — python -m scripts.user_admin activate --username …;
       • ADMIN_USERNAME + ADMIN_PASSWORD заданы (файл .env)  -> создаёт без вопросов (пароль после этого можно убрать из .env);
       • иначе, если есть терминал                            -> спрашивает логин и пароль;
       • иначе (up -d без .env)                               -> ничего не создаёт и печатает точную команду.
     Пароль из ADMIN_PASSWORD НЕ перезаписывает пароль существующего пользователя;
  5. печатает, что делать дальше (войдите администратором и добавьте товары: /ui/products.html).

Магазины и demo-данные НЕ создаются: «Узнать товар» магазины не нужны, товары добавляет администратор.

Коды выхода: 0 — готово; 1 — ошибка (БД недоступна, админ не создан); 2 — устаревшая схема БД.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from app.core.security import PolicyViolation, check_password_policy, validate_username

# Произвольное константное число — общий ключ advisory-замка bootstrap'а.
ADVISORY_LOCK_KEY = 727_001_001

# Эти пароли не принимаются для первого админа: типичные значения, которые остаются из примеров и шпаргалок.
WEAK_PASSWORDS = frozenset({
    "password", "password1", "12345678", "123456789", "1234567890", "qwerty123", "qwertyui", "admin123",
    "adminadmin", "administrator", "changeme", "change-me", "change_me", "change-me-please", "letmein1",
})

RUNTIME_DIRS = ("data", "logs", "models/index", "models/detector", "models/embedder")


# ------------------------------------------------------------------ чистая логика (тестируется офлайн)


@dataclass(frozen=True)
class AdminPlan:
    action: str           # "exists" | "owner_exists" | "from_env" | "prompt" | "manual"
    username: str = ""
    password: str = ""


def read_admin_env(env: Mapping[str, str]) -> tuple[str, str]:
    """(логин, пароль) из окружения. Пароль без логина -> логин «admin»; логин без пароля -> оба пустые."""
    username = (env.get("ADMIN_USERNAME") or "").strip()
    password = env.get("ADMIN_PASSWORD") or ""
    if password and not username:
        username = "admin"
    if username and not password:
        return username, ""
    return username, password


def plan_admin(active_admins: int, env: Mapping[str, str], interactive: bool, owner_exists: bool = False) -> AdminPlan:
    if active_admins > 0:
        return AdminPlan("exists")
    if owner_exists:
        return AdminPlan("owner_exists")
    username, password = read_admin_env(env)
    if username and password:
        return AdminPlan("from_env", username, password)
    if interactive:
        return AdminPlan("prompt", username or "admin")
    return AdminPlan("manual", username or "admin")


def weak_password_reason(username: str, password: str) -> str | None:
    """Причина, по которой пароль нельзя использовать для первого админа, либо None."""
    if password.strip().lower() in WEAK_PASSWORDS:
        return "это типичный пароль из примеров"
    if password.strip().lower() == (username or "").strip().lower():
        return "пароль совпадает с логином"
    return None


def validate_credentials(username: str, password: str) -> tuple[str, str | None]:
    """-> (каноничный логин, None) либо (логин, текст ошибки). Политика — та же, что у API (app/core/security.py)."""
    try:
        canon = validate_username(username)
        check_password_policy(password)
    except PolicyViolation as exc:
        return username, str(exc)
    reason = weak_password_reason(canon, password)
    if reason:
        return canon, f"Пароль не подходит: {reason}. Задайте свой."
    return canon, None


def prompt_credentials(default_username: str, input_fn: Callable[[str], str] = input,
                       getpass_fn: Callable[[str], str] = getpass.getpass, attempts: int = 3,
                       out: Callable[[str], None] = print) -> tuple[str, str] | None:
    """Спрашивает логин и пароль (с повтором). None — не удалось за `attempts` попыток."""
    for _ in range(attempts):
        entered = input_fn(f"Логин администратора [{default_username}]: ").strip()
        username = entered or default_username
        password = getpass_fn("Пароль (не короче 8 символов): ")
        if password != getpass_fn("Повторите пароль: "):
            out("  Пароли не совпадают, попробуйте ещё раз.")
            continue
        canon, error = validate_credentials(username, password)
        if error:
            out(f"  {error}")
            continue
        return canon, password
    return None


def ensure_directories(root: Path, relative_dirs: tuple[str, ...] = RUNTIME_DIRS,
                       extra: tuple[Path, ...] = ()) -> list[Path]:
    """Создаёт недостающие папки; возвращает только действительно созданные."""
    created = []
    for path in [root / rel for rel in relative_dirs] + list(extra):
        if not path.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            created.append(path)
    return created


def wait_until(check: Callable[[], None], timeout: float, interval: float = 2.0,
               sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
               on_retry: Callable[[int, Exception], None] | None = None) -> bool:
    """Вызывает check() (бросает исключение, пока «не готово») до успеха или истечения timeout секунд."""
    deadline = clock() + timeout
    attempt = 0
    while True:
        try:
            check()
            return True
        except Exception as exc:  # noqa: BLE001 — любая ошибка подключения = «ещё не готово»
            attempt += 1
            if on_retry:
                on_retry(attempt, exc)
            if clock() >= deadline:
                return False
            sleep(interval)


def drift_report_lines(drift: Mapping[str, list[str]]) -> list[str]:
    """Сообщение об устаревшей схеме. НИКОГДА не советует удалять базу/тома: пользователь запретил это без его явного согласия.
    Порядок действий — резервная копия, ручное добавление колонок, повторный bootstrap."""
    lines = ["ОШИБКА: схема БД устарела — в существующих таблицах нет колонок, которые ожидает код:"]
    lines += [f"      {table}: {', '.join(cols)}" for table, cols in sorted(drift.items())]
    lines += [
        "      Это база, созданная до миграций, и её таблицы старее базовой ревизии. Ничего не изменено, ваши данные НЕ тронуты.",
        "      1) Сначала резервная копия (на хосте): python scripts\\backup_db.py — и убедитесь, что она создана и проверена.",
        "      2) Затем добавьте недостающие колонки вручную: ALTER TABLE <таблица> ADD COLUMN <колонка> <тип>;",
        "         (или пришлите этот вывод разработчику — он подготовит команды).",
        "      3) Запустите bootstrap снова: docker compose run --rm bootstrap",
        "      Базу не пересоздавайте и тома не удаляйте — это необратимо.",
    ]
    return lines


def schema_summary(created_tables: list[str]) -> str:
    if not created_tables:
        return "уже актуальна"
    return (f"создано таблиц — {len(created_tables)}: {', '.join(sorted(created_tables))} "
            f"(существующие таблицы и данные не затронуты)")


def find_schema_drift(expected: Mapping[str, set[str]], actual: Mapping[str, set[str]]) -> dict[str, list[str]]:
    """{таблица: [колонки, которых нет в БД]} для таблиц, уже существующих в БД. create_all их не добавит."""
    drift = {}
    for table, columns in expected.items():
        if table in actual:
            missing = sorted(columns - actual[table])
            if missing:
                drift[table] = missing
    return drift


# ------------------------------------------------------------------ работа с БД (проверяется на реальном Postgres)


def _connect_once(engine) -> None:
    from sqlalchemy import text
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))


def ensure_schema(engine) -> tuple[list[str], dict[str, list[str]]]:
    """Миграции до последней ревизии (app/db/migrate.py). -> (созданные таблицы, расхождения старой базы — тогда ничего не менялось)."""
    from app.db.migrate import upgrade_to_head
    result = upgrade_to_head(engine)
    return result.created_tables, result.drift


def count_active_admins(db) -> int:
    from sqlalchemy import func, select
    from app.db.models import User
    return int(db.execute(
        select(func.count()).select_from(User).where(User.role == "admin", User.is_active.is_(True))
    ).scalar_one())


def count_stores(db) -> int:
    from sqlalchemy import func, select
    from app.db.models import Store
    return int(db.execute(select(func.count()).select_from(Store)).scalar_one())


def owner_exists(db) -> bool:
    from app.services.auth_service import AuthService
    return AuthService(db).owner_id() is not None


def create_admin(db, username: str, password: str, *, temporary: bool = False) -> str:
    """Создаёт admin и делает его владельцем, если владельца ещё нет. -> «created» | «exists» (логин занят — пароль НЕ меняется).

    temporary=True — пароль из .env (лежит в файле открытым текстом): он годится только для первой смены пароля (документ 05,
    §12.2), обычного входа с ним нет; срок не ограничен, чтобы владелец не оказался заперт, если войдёт не в первые сутки."""
    from app.core.security import Role
    from app.services.auth_service import AuthService, UserAlreadyExistsError
    service = AuthService(db)
    try:
        user = service.create_user(username, password, Role.ADMIN)
    except UserAlreadyExistsError:
        return "exists"
    if service.owner_id() is None:
        service.set_owner(user.id)
    if temporary:
        service.set_temporary_password(user.id, password, hours=None)
    return "created"


# ------------------------------------------------------------------ сценарий


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None, out: Callable[[str], None] = print,
         interactive: bool | None = None) -> int:
    parser = argparse.ArgumentParser(description="Подготовка Retail Shelf Audit к работе (БД, папки, первый админ)")
    parser.add_argument("--wait-timeout", type=float, default=90.0, help="сколько секунд ждать Postgres (по умолчанию 90)")
    parser.add_argument("--non-interactive", action="store_true", help="не задавать вопросов, даже если есть терминал")
    args = parser.parse_args(argv)

    env = os.environ if env is None else env
    if interactive is None:
        interactive = (not args.non_interactive) and sys.stdin.isatty()

    from app.core.config import PROJECT_ROOT, get_settings, resolve_project_path
    from app.db.session import SessionLocal, engine

    settings = get_settings()
    out("== Retail Shelf Audit: подготовка к работе ==")

    # 1. База данных
    out(f"[1/4] Жду Postgres: {engine.url.render_as_string(hide_password=True)}")
    if not wait_until(lambda: _connect_once(engine), args.wait_timeout,
                      on_retry=lambda n, exc: out(f"      попытка {n}: {type(exc).__name__}")):
        out(f"ОШИБКА: Postgres не отвечает за {args.wait_timeout:.0f} с. Проверьте: docker compose ps; docker compose logs db")
        return 1
    out("      Postgres отвечает.")

    # 2. Схема
    created_tables, drift = ensure_schema(engine)
    if drift:
        for line in drift_report_lines(drift):
            out(line)
        return 2
    out(f"[2/4] Схема БД: {schema_summary(created_tables)}.")

    # 3. Папки
    extra: list[Path] = [resolve_project_path(settings.media_root) / "shelf-images"]
    if settings.matcher is not None:
        extra.append(resolve_project_path(settings.matcher.index_path).parent)
    made = ensure_directories(PROJECT_ROOT, extra=tuple(extra))
    out(f"[3/4] Папки: {'создано — ' + ', '.join(str(p.relative_to(PROJECT_ROOT)) for p in made) if made else 'все на месте'}.")

    # 4. Администратор
    db = SessionLocal()
    try:
        has_owner = owner_exists(db)
        plan = plan_admin(count_active_admins(db), env, bool(interactive), owner_exists=has_owner)
        if plan.action == "exists":
            out("[4/4] Администратор: уже есть (ничего не меняю).")
            if not has_owner:
                out("      Владелец системы не назначен. Назначьте его с консоли (только так, не через сеть):")
                out("          docker compose exec api python -m scripts.user_admin owner --username ЛОГИН")
        elif plan.action == "owner_exists":
            out("[4/4] Нет ни одного активного администратора, но владелец назначен — нового администратора НЕ создаю.")
            out("      Включите владельца: docker compose exec api python -m scripts.user_admin activate --username ЛОГИН_ВЛАДЕЛЬЦА")
        elif plan.action == "from_env":
            canon, error = validate_credentials(plan.username, plan.password)
            if error:
                out(f"ОШИБКА: ADMIN_USERNAME/ADMIN_PASSWORD из .env не приняты: {error}")
                return 1
            result = create_admin(db, canon, plan.password, temporary=True)
            out(f"[4/4] Администратор «{canon}»: " + ("создан и назначен владельцем (логин и пароль — из .env). Пароль из .env — "
                "ВРЕМЕННЫЙ: при первом входе программа попросит придумать свой, обычного входа с ним нет. Уберите ADMIN_PASSWORD из "
                ".env и при первом входе настройте второй фактор (обязателен администраторам)." if result == "created" else
                "пользователь с таким логином уже есть, но это не активный admin — пароль не менял. Задайте другой ADMIN_USERNAME."))
        elif plan.action == "prompt":
            out("[4/4] Администратора ещё нет — создаём.")
            creds = prompt_credentials(plan.username)
            if creds is None:
                out("ОШИБКА: не удалось ввести корректные логин и пароль. Запустите: docker compose run --rm bootstrap")
                return 1
            result = create_admin(db, *creds)
            if result == "exists":
                out(f"ОШИБКА: пользователь «{creds[0]}» уже существует. Выберите другой логин.")
                return 1
            out(f"      Администратор «{creds[0]}» создан и назначен владельцем. При первом входе (на этом компьютере, "
                "административный адрес) настройте второй фактор — он обязателен администраторам.")
        else:  # manual
            out("[4/4] Администратор НЕ создан: в .env нет ADMIN_PASSWORD, а терминала для вопросов нет.")
            out("      Создайте его одной командой (спросит логин и пароль):")
            out("          docker compose run --rm bootstrap")
            out("      или задайте ADMIN_USERNAME и ADMIN_PASSWORD в файле .env и выполните: docker compose up -d")

        stores = count_stores(db)
    finally:
        db.close()

    port = env.get("API_PORT") or "8000"
    out("")
    out(f"Готово. Адрес: http://localhost:{port}   (с телефона — http://<IP-компьютера>:{port})")
    if stores == 0:
        out(f"Магазины для «Узнать товар» не нужны. Войдите как администратор и откройте http://localhost:{port}/ui/products.html,")
        out("там добавляются товары и их фотографии.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
