#!/usr/bin/env python3
"""
Резервная копия базы PostgreSQL одной командой, с ПРОВЕРКОЙ результата. Запускается на ХОСТЕ (Windows), нужен только
Python и Docker; сторонних пакетов не требует (работает и на Python 3.14).

    python scripts/backup_db.py                 # -> backups\\shelf-audit-ГГГГММДД-ЧЧММСС.dump
    python scripts/backup_db.py --dir D:\\backups

Что делает: `pg_dump -Fc` внутри контейнера db -> файл на хосте (сначала во временный *.part), затем проверка файла
через `pg_restore --list` (дамп должен читаться и содержать таблицы) и только после этого переименование в итоговое имя.
Если что-то не удалось — временный файл удаляется, код выхода 1, а потенциально разрушительные операции выполнять НЕЛЬЗЯ.

Скрипт ничего не удаляет (старые копии не трогает), не останавливает контейнеры и не меняет базу.
Копируются только данные БД. Снимки полок лежат в папке data\\, индекс каталога — в models\\index\\ (на диске хоста):
копируйте их вместе с базой (база и индекс согласованы между собой).

Восстановление (перезапишет текущие данные — только осознанно):
    docker compose cp backups\\ФАЙЛ.dump db:/tmp/restore.dump
    docker compose exec db sh -c 'pg_restore -U $POSTGRES_USER -d $POSTGRES_DB --clean --if-exists /tmp/restore.dump'
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

DUMP_SHELL = 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"'


def compose_prefix(compose_file: str | None = None) -> list[str]:
    return ["docker", "compose"] + (["-f", compose_file] if compose_file else [])


def dump_command(compose_file: str | None = None) -> list[str]:
    # -T: без псевдотерминала, иначе бинарный поток портится. $POSTGRES_* раскрывает shell контейнера, не хост.
    return compose_prefix(compose_file) + ["exec", "-T", "db", "sh", "-c", DUMP_SHELL]


def verify_command(compose_file: str | None = None) -> list[str]:
    return compose_prefix(compose_file) + ["exec", "-T", "db", "pg_restore", "--list"]


def backup_name(now: datetime) -> str:
    return f"shelf-audit-{now:%Y%m%d-%H%M%S}.dump"


def listing_is_valid(text: str) -> bool:
    """Вывод `pg_restore --list` настоящего дампа: заголовок архива и хотя бы один объект-таблица."""
    return "Archive created at" in text and " TABLE " in text


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run_backup(out_dir: Path, *, runner: Callable = subprocess.run, now: Callable[[], datetime] = datetime.now,
               compose_file: str | None = None, out: Callable[[str], None] = print) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / backup_name(now())
    part = final.with_name(final.name + ".part")

    def fail(message: str) -> int:
        part.unlink(missing_ok=True)          # удаляем только СВОЙ временный файл
        out(f"ОШИБКА: {message}")
        out("Резервная копия НЕ создана. Не выполняйте потенциально разрушительных операций (down -v, пересоздание базы).")
        return 1

    try:
        with part.open("wb") as handle:
            dump = runner(dump_command(compose_file), stdout=handle, stderr=subprocess.PIPE)
    except FileNotFoundError:
        return fail("команда docker не найдена: установите/запустите Docker Desktop")
    except OSError as exc:
        return fail(f"не удалось выполнить pg_dump: {exc}")
    if dump.returncode != 0:
        detail = (dump.stderr or b"").decode("utf-8", "replace").strip()[:400]
        return fail(f"pg_dump завершился с кодом {dump.returncode}. {detail} "
                    f"(запущены ли контейнеры? docker compose ps)")
    if not part.exists() or part.stat().st_size == 0:
        return fail("файл дампа пуст")

    try:
        with part.open("rb") as handle:
            check = runner(verify_command(compose_file), stdin=handle, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        return fail(f"не удалось проверить дамп: {exc}")
    listing = (check.stdout or b"").decode("utf-8", "replace")
    if check.returncode != 0 or not listing_is_valid(listing):
        return fail("полученный файл не прошёл проверку pg_restore --list (не похож на корректный дамп)")

    part.replace(final)
    size_mb = final.stat().st_size / (1024 * 1024)
    out(f"Готово: {final}  ({size_mb:.2f} МБ, проверен pg_restore --list)")
    out(f"SHA-256: {sha256_of(final)}")
    out("Вместе с базой сохраните папки data\\ (снимки) и models\\index\\ (индекс каталога).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверяемая резервная копия PostgreSQL (через docker compose)")
    parser.add_argument("--dir", default="backups", help="куда сохранять (по умолчанию .\\backups)")
    parser.add_argument("--compose-file", default=None, help="путь к docker-compose.yml, если запускаете не из папки проекта")
    args = parser.parse_args(argv)
    return run_backup(Path(args.dir), compose_file=args.compose_file)


if __name__ == "__main__":
    sys.exit(main())
