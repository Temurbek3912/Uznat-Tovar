"""
Ссылка «Придумайте ПИН» — первый вход и «забыли ПИН» без командной строки (решение владельца 2026-11-15, app/core/local_pin.py).

Обычно её вызывать не нужно:
  * программа при запуске (scripts/start_demo.py) сама открывает страницу «Придумайте ПИН», если ПИН ещё ни у кого нет;
  * забыли ПИН — двойной щелчок по «Забыли ПИН.bat» в папке программы: откроется та же страница.

    python -m scripts.setup_link                 # для владельца (или первого администратора), открыть в браузере
    python -m scripts.setup_link --user kassir   # для другого сотрудника
    python -m scripts.setup_link --json --if-needed --no-open   # (для start_demo) ссылка, только если ПИН ещё ни у кого нет

Ссылка одноразовая, действует 30 минут и открывается ТОЛЬКО на этом компьютере (адрес localhost). Создать её может лишь тот,
у кого есть доступ к папке программы на этом компьютере — то же доверие, что и к самой базе.
Какая база (полная или облегчённая) — из data/run_mode.json, который пишет последний запуск программы.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN_MODE_FILE = ROOT / "data" / "run_mode.json"


def read_run_mode(path: Path = RUN_MODE_FILE) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_run_mode(*, lite: bool, port: int, scheme: str, database_url: str | None, path: Path = RUN_MODE_FILE) -> None:
    """Пишет start_demo: каким был последний запуск. Адрес базы — только для облегчённого режима (файл SQLite, без пароля)."""
    data = {"lite": lite, "port": port, "scheme": scheme}
    if lite and database_url:
        data["database_url"] = database_url
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def link_url(token: str, *, port: int = 8000, scheme: str = "http") -> str:
    return f"{scheme}://localhost:{port}/ui/login.html#setup={token}"


def make_link(username: str | None = None, *, if_needed: bool = False) -> dict:
    """{"needed": bool, "token": str|None, "username": str|None, "error": str|None}. needed — ПИН ещё ни у кого нет."""
    from app.db.session import SessionLocal
    from app.services.auth_service import AuthService

    with SessionLocal() as db:
        service = AuthService(db)
        try:
            needed = not service.pin_users()
        except Exception as exc:        # noqa: BLE001 — нет таблиц: программа ещё не запускалась на этой версии
            return {"needed": False, "token": None, "username": None,
                    "error": f"База не готова ({exc.__class__.__name__}) — сначала запустите программу"}
        if if_needed and not needed:
            return {"needed": False, "token": None, "username": None, "error": None}
        user = service.setup_link_target(username)
        if user is None:
            return {"needed": needed, "token": None, "username": None,
                    "error": "Нет такого сотрудника" if username else "В программе нет ни одного администратора"}
        token, _ = service.create_setup_link(user)
        return {"needed": needed, "token": token, "username": user.username, "error": None}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", default=None)
    ap.add_argument("--if-needed", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args(argv)

    mode = read_run_mode()
    if "DATABASE_URL" not in os.environ and mode.get("lite") and mode.get("database_url"):
        os.environ.update({"LITE_MODE": "1", "DATABASE_URL": str(mode["database_url"])})
    port, scheme = int(mode.get("port") or 8000), str(mode.get("scheme") or "http")

    result = make_link(a.user, if_needed=a.if_needed)
    if a.json:
        print(json.dumps(result, ensure_ascii=False))
        return 0 if not result["error"] else 1
    if result["error"]:
        print(result["error"])
        return 1
    url = link_url(result["token"], port=port, scheme=scheme)
    print(f"Страница «Придумайте ПИН» для {result['username']} (действует 30 минут, открывается только на этом компьютере):\n\n    {url}\n")
    print("Программа должна быть запущена. Если страница не открылась — запустите программу и повторите.")
    if not a.no_open:
        try:
            webbrowser.open(url)
        except Exception:               # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
