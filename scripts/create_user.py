"""
Создаёт пользователя напрямую в БД — в первую очередь ПЕРВОГО администратора:
через API это невозможно по построению (создавать пользователей может только admin).

Примеры (PowerShell, из корня проекта, venv активирован):

    python -m scripts.create_user --username admin --role admin
    python -m scripts.create_user --username m.karimov --role merchandiser --store-id <store_id>
    python -m scripts.create_user --username region1 --role regional_manager --store-id <id1> --store-id <id2>

Пароль НЕ передаётся аргументом командной строки (остался бы в истории оболочки):
он запрашивается интерактивно, либо берётся из переменной окружения
SHELF_AUDIT_PASSWORD (для автоматизации/CI).

Скрипт сам создаёт недостающие таблицы (users, user_store_access, api_tokens) —
существующие таблицы и данные не трогает, поэтому подходит и для БД, созданной
до появления F13.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from app.core.config import get_settings
from app.core.security import PolicyViolation, Role
from app.db.session import SessionLocal, engine
from app.services.audit import CONSOLE  # журнал: действие с консоли сервера
from app.services.auth_service import AuthService, UnknownStoreError, UserAlreadyExistsError


def _read_password() -> str:
    from_env = os.environ.get("SHELF_AUDIT_PASSWORD")
    if from_env:
        return from_env
    if not sys.stdin.isatty():
        raise SystemExit("Нет терминала для ввода пароля: задайте переменную окружения SHELF_AUDIT_PASSWORD")
    first = getpass.getpass("Пароль: ")
    second = getpass.getpass("Повторите пароль: ")
    if first != second:
        raise SystemExit("Пароли не совпадают")
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Создать пользователя Retail Shelf Audit")
    parser.add_argument("--username", required=True)
    parser.add_argument("--role", required=True, choices=[r.value for r in Role])
    parser.add_argument(
        "--store-id", action="append", default=[], metavar="STORE_ID",
        help="магазин, доступный пользователю (можно несколько раз); для admin/analyst не нужен — у них вся сеть",
    )
    parser.add_argument("--permanent", action="store_true",
                        help="пароль вводит сам владелец учётной записи; без флага пароль временный — смена при первом входе")
    args = parser.parse_args(argv)

    password = _read_password()

    from app.db.migrate import ensure_current
    ensure_current(engine)          # миграции Alembic вместо create_all (шаг Р-2)
    db = SessionLocal()
    try:
        service = AuthService(db, actor=CONSOLE)
        user = service.create_user(args.username, password, Role(args.role), args.store_id)
        if not args.permanent:                   # пароль знает тот, кто его ввёл, — временный (документ 05, §12.2)
            service.set_temporary_password(user.id, password, hours=get_settings().auth_temp_password_hours)
        print(f"Готово: user_id={user.id}, username={user.username}, role={user.role}, "
              f"stores={sorted(a.store_id for a in user.store_access)}"
              + ("" if args.permanent else "; пароль временный — при первом входе пользователь задаст свой"))
        return 0
    except (PolicyViolation, UserAlreadyExistsError, UnknownStoreError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
