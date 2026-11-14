"""
Безопасное администрирование пользователей из командной строки (когда войти в веб-интерфейс нельзя —
например, забыт пароль единственного администратора).

    docker compose exec api python -m scripts.user_admin list
    docker compose exec api python -m scripts.user_admin reset-password --username admin
    docker compose exec api python -m scripts.user_admin deactivate --username m.karimov
    docker compose exec api python -m scripts.user_admin activate --username m.karimov
    docker compose exec api python -m scripts.user_admin reset-2fa --username admin      # потерян телефон с приложением
    docker compose exec api python -m scripts.user_admin reset-2fa --username admin --new-password   # телефон украден: и пароль
    docker compose exec api python -m scripts.user_admin logout-all --username m.karimov  # завершить все сеансы
    docker compose exec api python -m scripts.user_admin owner --username admin          # назначить владельца (если его нет)
    docker compose exec api python -m scripts.user_admin owner --username boss --transfer  # передать права владельца

Правила безопасности:
  • пароль вводится скрыто (getpass) дважды и НИКОГДА не принимается аргументом командной строки, не печатается
    и не пишется в журнал; хеши паролей не выводятся нигде;
  • сброс пароля завершает все сессии пользователя (токены отзываются);
  • пароль не меняется «автоматически» — только по явной команде и подтверждению имени пользователя;
  • последнего активного администратора отключить нельзя;
  • сброс второго фактора (reset-2fa) — только отсюда, с консоли сервера (документ 18): через сеть второй фактор снимается
    лишь самим пользователем с паролем и действующим кодом; после сброса все сеансы пользователя завершаются;
  • пользователей этот скрипт НЕ создаёт и не удаляет (создание — scripts.create_user).

Доступ к этой команде = доступ к серверу (docker compose exec): то есть тот же уровень доверия, что и к самой БД.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable, Sequence

from app.core.security import PolicyViolation, check_password_policy, validate_username


def active_admins_other_than(users: Sequence[tuple[str, str, bool]], username: str) -> int:
    """Сколько АКТИВНЫХ админов останется, если не считать username. users: (логин, роль, активен)."""
    return sum(1 for name, role, active in users if role == "admin" and active and name != username)


def deactivate_blocker(users: Sequence[tuple[str, str, bool]], username: str) -> str | None:
    """Причина, по которой отключать нельзя, либо None."""
    target = next(((n, r, a) for n, r, a in users if n == username), None)
    if target is None:
        return f"пользователь «{username}» не найден"
    if target[1] == "admin" and target[2] and active_admins_other_than(users, username) == 0:
        return "это последний активный администратор — отключив его, вы потеряете доступ к управлению системой"
    return None


def read_new_password(getpass_fn: Callable[[str], str] = getpass.getpass, attempts: int = 3,
                      out: Callable[[str], None] = print) -> str | None:
    for _ in range(attempts):
        first = getpass_fn("Новый пароль (не короче 8 символов): ")
        if first != getpass_fn("Повторите пароль: "):
            out("  Пароли не совпадают, попробуйте ещё раз.")
            continue
        try:
            check_password_policy(first)
        except PolicyViolation as exc:
            out(f"  {exc}")
            continue
        return first
    return None


def confirm_username(username: str, input_fn: Callable[[str], str] = input) -> bool:
    """Подтверждение вводом имени: случайное «y» не должно менять чужой пароль."""
    answer = input_fn(f"Для подтверждения введите логин пользователя ещё раз ({username}): ").strip().lower()
    return answer == username


def _find(db, username: str):
    from sqlalchemy import select

    from app.db.models import User
    try:
        canon = validate_username(username)
    except PolicyViolation:
        return None
    return db.execute(select(User).where(User.username == canon)).scalar_one_or_none()


def cmd_list(db, out=print) -> int:
    from sqlalchemy import select

    from app.db.models import User
    users = db.execute(select(User).order_by(User.username)).scalars().all()
    if not users:
        out("Пользователей нет. Создать администратора: docker compose run --rm bootstrap")
        return 0
    out(f"{'логин':<24} {'роль':<18} {'статус':<10} магазинов")
    for u in users:
        state = "В АРХИВЕ" if u.archived_at is not None else ("активен" if u.is_active else "ОТКЛЮЧЁН")
        out(f"{u.username:<24} {u.role:<18} {state:<10} {len(u.store_access)}")
    return 0


def cmd_reset_password(db, username: str, *, getpass_fn=getpass.getpass, input_fn=input, assume_yes: bool = False,
                       permanent: bool = False, out=print) -> int:
    """Новый пароль с консоли. По умолчанию — временный (документ 05, §12.4): при первом входе пользователь задаёт свой. --permanent —
    пароль вводит сам владелец учётной записи (например, владелец системы, забывший пароль)."""
    from app.services.auth_service import AuthService

    user = _find(db, username)
    if user is None:
        out(f"Ошибка: пользователь «{username}» не найден. Список: python -m scripts.user_admin list")
        return 1
    if not assume_yes and not confirm_username(user.username, input_fn):
        out("Отменено: логин не совпал, пароль не изменён.")
        return 1
    password = read_new_password(getpass_fn, out=out)
    if password is None:
        out("Ошибка: не удалось ввести корректный пароль, пароль не изменён.")
        return 1
    service = AuthService(db)
    if permanent:
        service.update_user(user.id, password=password)              # хеширует и отзывает все токены пользователя
        out(f"Пароль пользователя «{user.username}» изменён; все его активные сессии завершены.")
    else:
        from app.core.config import get_settings
        _value, deadline = service.set_temporary_password(user.id, password, hours=get_settings().auth_temp_password_hours)
        until = f" до {deadline:%Y-%m-%d %H:%M} UTC" if deadline else ""
        out(f"Пользователю «{user.username}» задан временный пароль (действует{until or ' без срока'}); все его сессии завершены. "
            "При первом входе он придумает свой пароль.")
    return 0


def cmd_reset_2fa(db, username: str, *, input_fn=input, assume_yes: bool = False, new_password: bool = False,
                  getpass_fn=getpass.getpass, out=print) -> int:
    """Потерян телефон. После сброса первый, кто войдёт с паролем на административном входе, настроит второй фактор на СВОЙ
    телефон — поэтому при краже телефона (или если пароль мог узнать кто-то ещё) меняйте и пароль: --new-password (аудит
    2026-11-08, документ 18, §12)."""
    from app.services.auth_service import AuthService

    user = _find(db, username)
    if user is None:
        out(f"Ошибка: пользователь «{username}» не найден. Список: python -m scripts.user_admin list")
        return 1
    if not assume_yes and not confirm_username(user.username, input_fn):
        out("Отменено: логин не совпал, второй фактор не изменён.")
        return 1
    password = None
    if new_password:
        password = read_new_password(getpass_fn, out=out)
        if password is None:
            out("Ошибка: не удалось ввести корректный пароль — ничего не изменено.")
            return 1
    service = AuthService(db)
    if password is not None:
        service.update_user(user.id, password=password)
    service.reset_totp(user.id)
    out(f"Второй фактор пользователя «{user.username}» выключен{', пароль изменён' if password else ''}; все его сеансы "
        "завершены. Пусть войдёт и сразу настроит второй фактор заново («Настройки» → «Безопасность входа»; администратор — "
        "на самом компьютере, http://localhost:8001).")
    if password is None:
        out("Если телефон украден или пароль мог узнать кто-то ещё — смените и пароль: добавьте --new-password.")
    return 0


def cmd_owner(db, username: str, *, transfer: bool = False, input_fn=input, assume_yes: bool = False, out=print) -> int:
    """Владелец назначается или передаётся ТОЛЬКО отсюда (консоль сервера): через сеть признака владельца не изменить."""
    from app.services.auth_service import AuthService, AuthServiceError

    user = _find(db, username)
    if user is None:
        out(f"Ошибка: пользователь «{username}» не найден.")
        return 1
    if not assume_yes and not confirm_username(user.username, input_fn):
        out("Отменено: логин не совпал, владелец не изменён.")
        return 1
    try:
        AuthService(db).set_owner(user.id, transfer=transfer)
    except AuthServiceError as exc:
        out(f"Ошибка: {exc}")
        return 1
    out(f"Владелец системы — «{user.username}». Прежний владелец (если был) стал обычным администратором.")
    return 0


def cmd_logout_all(db, username: str, out=print) -> int:
    from app.services.auth_service import AuthService

    user = _find(db, username)
    if user is None:
        out(f"Ошибка: пользователь «{username}» не найден.")
        return 1
    AuthService(db).revoke_all_tokens(user.id)
    out(f"Все сеансы пользователя «{user.username}» завершены.")
    return 0


def cmd_set_active(db, username: str, active: bool, out=print) -> int:
    from sqlalchemy import select

    from app.db.models import User
    from app.services.auth_service import AuthService

    user = _find(db, username)
    if user is None:
        out(f"Ошибка: пользователь «{username}» не найден.")
        return 1
    service = AuthService(db)
    if not active:
        rows = [(u.username, u.role, bool(u.is_active)) for u in db.execute(select(User)).scalars().all()]
        blocker = deactivate_blocker(rows, user.username)
        if blocker:
            out(f"Отказано: {blocker}.")
            return 1
    if user.archived_at is not None:           # документ 05, §12.7: из архива не возвращают
        out(f"Отказано: «{user.username}» в архиве (доступ отозван {user.archived_at:%Y-%m-%d}). Заведите новую учётную запись "
            "с другим логином.")
        return 1
    service.update_user(user.id, is_active=active)
    out(f"Пользователь «{user.username}»: {'включён' if active else 'отключён, его сессии завершены'}.")
    if not active and service.is_owner(user.id):
        out("Это владелец системы. Пока он отключён, никто не сможет назначать администраторов и сбрасывать им второй фактор. "
            "Передайте права: python -m scripts.user_admin owner --username ЛОГИН --transfer")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="показать пользователей (без паролей и хешей)")
    p = sub.add_parser("reset-password", help="задать новый пароль (скрытый ввод, сессии пользователя завершаются); по умолчанию — временный")
    p.add_argument("--username", required=True)
    p.add_argument("--permanent", action="store_true", help="пароль вводит сам владелец учётной записи — без смены при первом входе")
    p.add_argument("--yes", action="store_true", help="не просить повторного ввода логина (для скриптов)")
    for name in ("activate", "deactivate"):
        p = sub.add_parser(name, help="включить/отключить пользователя")
        p.add_argument("--username", required=True)
    p = sub.add_parser("reset-2fa", help="выключить второй фактор (потерян телефон); сеансы пользователя завершаются")
    p.add_argument("--username", required=True)
    p.add_argument("--new-password", action="store_true", help="заодно задать новый пароль (скрытый ввод) — если телефон украден")
    p.add_argument("--yes", action="store_true", help="не просить повторного ввода логина (для скриптов)")
    p = sub.add_parser("logout-all", help="завершить все сеансы пользователя")
    p.add_argument("--username", required=True)
    p = sub.add_parser("owner", help="назначить владельца системы (если его нет) или передать права (--transfer)")
    p.add_argument("--username", required=True)
    p.add_argument("--transfer", action="store_true", help="передать права владельца от нынешнего владельца")
    p.add_argument("--yes", action="store_true", help="не просить повторного ввода логина (для скриптов)")
    args = parser.parse_args(argv)

    from app.db.session import SessionLocal
    db = SessionLocal()
    try:
        if args.cmd == "list":
            return cmd_list(db)
        if args.cmd == "reset-password":
            return cmd_reset_password(db, args.username, assume_yes=args.yes, permanent=args.permanent)
        if args.cmd == "reset-2fa":
            return cmd_reset_2fa(db, args.username, assume_yes=args.yes, new_password=args.new_password)
        if args.cmd == "logout-all":
            return cmd_logout_all(db, args.username)
        if args.cmd == "owner":
            return cmd_owner(db, args.username, transfer=args.transfer, assume_yes=args.yes)
        return cmd_set_active(db, args.username, active=(args.cmd == "activate"))
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
