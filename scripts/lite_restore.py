"""
Вернуть данные магазина из резервной копии (облегчённый режим; копии делает сама программа раз в сутки —
app/services/lite_backup.py). Для человека: двойной щелчок по «Восстановить из копии.bat» в папке программы.

    python -m scripts.lite_restore                  # показать копии (на компьютере и в облаке) и спросить, какую вернуть
    python -m scripts.lite_restore --list           # только показать
    python -m scripts.lite_restore --copy "D:\\Копии\\Узнать товар — копии\\kopiya-2026-11-16_030000" --yes
    python -m scripts.lite_restore --google         # искать и в Google Диске (войти по коду, если не подключён)
    python -m scripts.lite_restore --backup-now     # сделать копию сейчас (программа может работать)

Нужно закрыть программу «Узнать товар». Текущие данные не удаляются: папка data/lite переименовывается в
data/lite.bak-<время>. Ключи входа из копии вписываются в .env (прежний .env сохраняется как .env.bak-<время>).
Для восстановления на НОВОМ компьютере: установите программу («Установить.bat»), закройте её и запустите
«Восстановить из копии.bat»: оно предложит войти в Google Диск магазина по коду и скачает копию оттуда.
"""
from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import gdrive_backup as G  # noqa: E402
from app.services import lite_backup as B  # noqa: E402

LITE = ROOT / "data" / "lite"
KIND = {"local": "на этом компьютере", "cloud": "в облаке", "google": "в Google Диске"}


def program_running(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def found_copies(dests: list[B.Destination], google: list[dict] | None = None) -> list[tuple[str, B.CopyInfo]]:
    """Все копии из всех мест, новые первыми; одинаковые (одно время) — один раз, предпочтительно с этого компьютера."""
    seen: dict[str, tuple[str, B.CopyInfo]] = {}
    for d in dests:
        for c in B.list_copies(d.root):
            seen.setdefault(c.name, (d.kind, c))
    for g in google or []:
        seen.setdefault(g["name"], ("google", B.CopyInfo(Path(g["name"]), g["name"], g.get("created_at") or "",
                                                         dict(g.get("counts") or {}))))
    return sorted(seen.values(), key=lambda kc: kc[1].name, reverse=True)


def describe(kind: str, c: B.CopyInfo) -> str:
    when = c.created_at.replace("T", " ")[:16] or c.name
    n = c.counts
    return (f"{when}  ({KIND.get(kind, kind)}): товаров {n.get('products', '?')}, сотрудников {n.get('users', '?')}, "
            f"фото {n.get('photos', '?')}")


def _yes(text: str) -> bool:
    return input(text).strip().lower() in ("д", "да", "y", "yes")


class Cloud:
    """Google Диск для восстановления: подключённый в программе или вход по коду прямо здесь (новый компьютер)."""

    def __init__(self, media_root: Path, settings):
        self.client = G.load_client(ROOT, settings)
        self.backup = G.GoogleBackup(media_root=media_root, client=self.client, secret_key=settings.auth_secret_key or "")
        self.token = self.backup.refresh_token() if self.client else ""
        self.new_answer: dict | None = None
        self.http = None

    def drive(self) -> G.Drive:
        if self.http is None:
            self.http = G._default_http()
        return G.Drive(self.http, self.client, self.token)

    def sign_in(self) -> None:
        self.http = self.http or G._default_http()
        self.new_answer = G.console_sign_in(self.http, self.client)
        self.token = str(self.new_answer["refresh_token"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Вернуть данные из резервной копии")
    ap.add_argument("--list", action="store_true", help="только показать копии")
    ap.add_argument("--copy", help="папка копии (kopiya-…)")
    ap.add_argument("--google", action="store_true", help="искать копии и в Google Диске")
    ap.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    ap.add_argument("--backup-now", action="store_true", help="сделать копию сейчас и выйти")
    ap.add_argument("--media-root", default=str(LITE), help=argparse.SUPPRESS)
    ap.add_argument("--env-file", default=str(ROOT / ".env"), help=argparse.SUPPRESS)
    ap.add_argument("--port", type=int, default=None, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    media_root = Path(a.media_root)
    from app.core.config import get_settings
    settings = get_settings()
    dests = B.destinations_from_settings(ROOT, settings, media_root)

    if a.backup_now:
        report = B.create_backup(root=ROOT, media_root=media_root,
                                 db_file=B.sqlite_path("sqlite:///" + (media_root / "shop.db").as_posix()),
                                 keys={"AUTH_SECRET_KEY": settings.auth_secret_key or "",
                                       "CAMERA_SECRET_KEY": settings.camera_secret_key or ""}, dests=dests)
        for d in report["destinations"]:
            print(f"{'Готово' if d['ok'] else 'НЕ ПОЛУЧИЛОСЬ'} — {KIND.get(d['kind'], d['kind'])}: {d['path']}"
                  + (f" ({d['error']})" if d["error"] else ""))
        return 0 if report["ok"] else 1

    cloud = Cloud(media_root, settings)
    if a.copy:
        chosen, kind = Path(a.copy), "local"
    else:
        print("Где ищу копии:")
        for d in dests:
            print(f"  {KIND.get(d.kind, d.kind)}: {d.root}")
        local_copies = found_copies(dests)
        google: list[dict] = []
        if cloud.client:
            if not cloud.token and (a.google or (not local_copies and not a.list)):
                if a.yes or _yes("\nНа этом компьютере копий нет. Поискать их в Google Диске магазина? Да — «д»: "
                                   if not local_copies else "\nВойти в Google Диск магазина, чтобы найти копии там? Да — «д»: "):
                    try:
                        cloud.sign_in()
                    except G.CloudError as exc:
                        print(f"Не получилось: {exc}")
            if cloud.token:
                print("  Google Диск: ищу копии…")
                try:
                    google = G.list_cloud_copies(cloud.drive(), B.FOLDER_NAME)
                except G.CloudError as exc:
                    print(f"  Google Диск недоступен: {exc}")
        copies = found_copies(dests, google)
        if not copies:
            print("Копий не найдено.")
            return 1
        print("\nКопии (новые сверху):")
        for i, (kind, c) in enumerate(copies, 1):
            print(f"  {i}. {describe(kind, c)}")
        if a.list:
            return 0
        answer = input("\nНомер копии, которую вернуть (Enter — 1, «н» — отмена): ").strip().lower()
        if answer in ("н", "n", "нет", "no"):
            print("Отменено. Ничего не изменилось.")
            return 1
        try:
            kind, info = copies[(int(answer) if answer else 1) - 1]
        except (ValueError, IndexError):
            print("Такого номера нет. Ничего не изменилось.")
            return 1
        chosen = info.path

    port = a.port
    if port is None and media_root.resolve() == LITE.resolve():
        from scripts.setup_link import read_run_mode
        port = int(read_run_mode().get("port") or 8000)
    if port and program_running(port):
        print(f"Программа «Узнать товар» сейчас работает (порт {port}). Закройте её окно и запустите восстановление снова.")
        return 2
    if not a.yes:
        if not _yes(f"Вернуть данные из копии {chosen.name}? Текущие данные сохранятся в папке "
                    f"{media_root.name}.bak-<время>. Да — «д»: "):
            print("Отменено. Ничего не изменилось.")
            return 1
    if kind == "google":
        print("Скачиваю копию из Google Диска (фото, которые уже есть на компьютере, не скачиваются)…")
        try:
            chosen = G.download_copy(cloud.drive(), chosen.name, dests[0].root, folder_name=B.FOLDER_NAME,
                                     progress=lambda i, n: print(f"  фото {i} из {n}", end="\r"))
        except (G.CloudError, B.BackupError, OSError) as exc:
            print(f"\nНе получилось скачать: {exc}")
            return 1
        print()
    print("Проверяю копию и возвращаю данные...")
    try:
        result = B.restore_copy(chosen, media_root=media_root, env_file=Path(a.env_file))
    except B.BackupError as exc:
        print(f"Не получилось: {exc}")
        return 1
    if cloud.new_answer is not None:            # вошли здесь — подключение остаётся и в программе
        key = B.read_keys(chosen).get("AUTH_SECRET_KEY") or settings.auth_secret_key or ""
        try:
            G.GoogleBackup(media_root=media_root, client=cloud.client, secret_key=key).finish_connect(cloud.new_answer)
            print("Google Диск подключён и в программе: копии снова будут уходить туда.")
        except Exception:  # noqa: BLE001 — нет ключа: подключат в «Настройках»
            print("Google Диск подключите заново в «Настройках» программы.")
    n = result["counts"]
    print(f"Готово. Возвращено: товаров {n.get('products', 0)}, сотрудников {n.get('users', 0)}, фото {n.get('photos', 0)}.")
    if result["old_data"]:
        print(f"Прежние данные сохранены в папке: {result['old_data']}")
    if result["keys_changed"]:
        print("Ключи входа взяты из копии (прежний .env сохранён рядом).")
    print("Запустите программу как обычно (ярлык «Узнать товар»).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
