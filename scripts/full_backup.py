#!/usr/bin/env python3
"""
Полная резервная копия «Узнать товар» с проверкой (этап 2А генерального плана; документ 09, §3–§5, §8 п. 1; документ 05, §13.3.6).

Только стандартная библиотека Python (как scripts/backup_db.py): запускается на компьютере с Docker без установки пакетов.

Состав (документ 09, §3):

    <место копий>/
      photos/ab/abcdef…            ← ОБЩЕЕ хранилище фото всех копий: файл назван по SHA-256 содержимого и хранится один раз
                                     (фото из data/catalog-uploads и data/catalog-imported)
      Backup-2026-11-07_213000/
        manifest.json              ← что в копии, SHA-256 каждого файла, результат проверки
        photos.list                ← «sha256<TAB>размер<TAB>путь» — фото, на которые ссылается эта копия
        db.dump                    ← pg_dump -Fc (согласованный снимок базы)
        index/                     ← файл индекса и его список id (производные данные: экономят время восстановления)
        models/projection/         ← проекция поверх векторов модели, обученная на этом каталоге (если есть)
        config/                    ← configs/config.yaml, docker-compose.yml, requirements.txt (БЕЗ .env и паролей камер)

Порядок (документ 09, §3): сначала дамп базы, затем фото (каждое новое — с проверкой хеша), затем индекс.
Копия собирается в папке «…​.part» и получает итоговое имя только после проверки. Временные кадры «Узнать товар»
(`identify-tmp`) в копию не входят никогда (D-10а, ТК-7).

Команды:
    python scripts/full_backup.py create        --dest D:\\backups-uznat           # через docker compose (профиль Docker)
    python scripts/full_backup.py verify        --dest D:\\backups-uznat Backup-…  [--full-store]
    python scripts/full_backup.py restore-check --dest D:\\backups-uznat Backup-…  # восстановление во ВРЕМЕННУЮ базу и проверка
    python scripts/full_backup.py restore       --dest D:\\backups-uznat Backup-… --target-db uznat_restored --out D:\\restored
                                                 # в НОВУЮ базу и НОВУЮ пустую папку; рабочая система не меняется
    python scripts/full_backup.py prune         --dest D:\\backups-uznat           # только план; удаление — с --apply

Скрипт ничего не удаляет в рабочей системе. `restore-check` создаёт временную базу с уникальным именем и удаляет только её.
Перед созданием временной (и новой, для `restore`) базы проверяется свободное место на диске сервера базы: временная база
размером с рабочую; не хватает — отказ до создания базы, узнать место нельзя (сервер на другом компьютере) — предупреждение.
`prune` по умолчанию лишь показывает, какие копии и фото стали лишними по правилу 7 ежедневных + 4 еженедельных +
6 ежемесячных (последняя проверенная — всегда); удаляет — только с `--apply`. Фото удаляется из хранилища копий, только
когда на него не ссылается ни одна оставшаяся копия.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

FORMAT = 1
BACKUP_PREFIX = "Backup-"
PART_SUFFIX = ".part"
STORE_DIR = "photos"
LOCK_NAME = ".full_backup.lock"
PHOTO_SUBDIR = "catalog-uploads"                 # фото каталога внутри media_root (загруженные из браузера)
# Все папки фото товаров внутри media_root. catalog-imported — копии фото импорта каталога (app/services/product_images.py,
# IMPORTED_SUBDIR): в них лежат фото товаров из открытой базы и из папок импорта. До 2026-11-21 копия брала только
# catalog-uploads, и на рабочей машине все 1798 фото (они в catalog-imported) в копию не попадали.
PHOTO_SUBDIRS = (PHOTO_SUBDIR, "catalog-imported")
NEVER_COPY = {"identify-tmp"}                    # временные кадры покупателей — никогда
CONFIG_FILES = ("configs/config.yaml", "docker-compose.yml", "requirements.txt")
# Файлы моделей, которые обучены на каталоге этого магазина и в git не хранятся: без проекции (models.embedder.projection_path,
# 2026-11-22) индекс, построенный с ней, после восстановления не открылся бы. Большие общие модели (SigLIP 2, YOLO) не копируются.
MODEL_DIRS = ("models/projection",)
CHUNK = 1024 * 1024
KEEP_DAILY, KEEP_WEEKLY, KEEP_MONTHLY = 7, 4, 6
# Срок ПОДКЛЮЧЕНИЯ утилит PostgreSQL к серверу (не срок всей копии: большой дамп может идти долго). Без него при недоступном
# сервере pg_dump/psql ждут столько, сколько ждёт система (минуты). Свой PGCONNECT_TIMEOUT или connect_timeout в адресе — главнее.
CONNECT_TIMEOUT_S = 15
# Проверка места перед созданием новой базы на сервере (restore-check, restore): дамп -Fc сжат — восстановленная база больше
# его примерно в несколько раз (оценка, когда размер рабочей базы не прочитался); запас — на журнал WAL и рост рабочей базы.
DUMP_EXPANSION = 5
SPACE_RESERVE_BYTES = 512 * 1024 * 1024

Runner = Callable[..., subprocess.CompletedProcess]


class BackupError(Exception):
    """Копия не создана или не прошла проверку; сообщение — для человека."""


# --------------------------------------------------------------------------- файлы

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _fsync_dir(path: Path) -> None:
    """Сброс записи о файлах в папке. На POSIX — fsync открытой папки; в Windows переносимого способа в Python нет (документ 05,
    §13.3.1), шаг пропускается — надёжность там держится на проверке хешей и повторной копии."""
    if os.name == "nt":
        return
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_with_hash(src: Path, dst: Path) -> tuple[str, int]:
    """Копирует src в dst (dst не должен существовать), считая SHA-256 прочитанного. fsync до возврата."""
    h, size = hashlib.sha256(), 0
    with Path(src).open("rb") as fin, Path(dst).open("xb") as fout:
        for chunk in iter(lambda: fin.read(CHUNK), b""):
            h.update(chunk)
            fout.write(chunk)
            size += len(chunk)
        fout.flush()
        os.fsync(fout.fileno())
    return h.hexdigest(), size


def _replace_with_retry(src: Path, dst: Path, *, attempts: int = 5, pause: float = 0.5) -> None:
    """os.replace с несколькими повторами: в Windows антивирус или индексатор могут ненадолго держать файл или папку
    (документ 05, §13.3.1). Окончательная ошибка пробрасывается как BackupError с понятным текстом."""
    import time

    for attempt in range(1, attempts + 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            if attempt == attempts:
                raise BackupError(f"не удалось переименовать {Path(src).name} → {Path(dst).name}: файл занят другой программой "
                                  f"({exc}). Копия осталась в папке {Path(src).name}; повторите позже.") from exc
            time.sleep(pause * attempt)


def _write_text_durable(path: Path, text: str) -> None:
    """Текстовый файл копии: временный файл → fsync → замена. Иначе после отключения питания мог бы остаться пустой файл."""
    tmp = Path(f"{path}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def store_path(store: Path, sha: str) -> Path:
    return Path(store) / sha[:2] / sha


def store_photo(src: Path, store: Path) -> tuple[str, int, bool]:
    """Кладёт фото в общее хранилище по SHA-256. Возвращает (sha, размер, новое ли). Новое — перечитывается и сверяется."""
    store = Path(store)
    store.mkdir(parents=True, exist_ok=True)
    tmp = store / f".tmp-{os.getpid()}-{secrets.token_hex(6)}"
    try:
        sha, size = _copy_with_hash(src, tmp)
        final = store_path(store, sha)
        if final.exists():
            if final.stat().st_size != size:
                raise BackupError(f"в хранилище копий файл {final.name} другого размера — хранилище повреждено, нужна проверка "
                                  f"`verify --full-store`")
            return sha, size, False
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, final)
        _fsync_dir(final.parent)
        if sha256_file(final) != sha:
            raise BackupError(f"фото {src} записалось в хранилище копий с ошибкой (хеш не совпал)")
        return sha, size, True
    finally:
        tmp.unlink(missing_ok=True)


def iter_photo_files(media_root: Path) -> Iterable[tuple[Path, str]]:
    """(путь, относительный путь в стиле POSIX от media_root) для всех файлов фото каталога; временные кадры исключены."""
    for sub in PHOTO_SUBDIRS:
        base = Path(media_root) / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            rel = path.relative_to(media_root)
            if any(part in NEVER_COPY for part in rel.parts) or not path.is_file():
                continue
            if ".part" in path.name or path.name.startswith(".tmp-"):
                continue                                   # недописанные временные файлы самой программы
            yield path, rel.as_posix()


def read_photo_list(path: Path) -> list[tuple[str, int, str]]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        sha, size, rel = line.split("\t", 2)
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise BackupError(f"повреждённая строка в {path.name}: {line[:80]}")
        rows.append((sha, int(size), rel))
    return rows


# --------------------------------------------------------------------------- база данных

def listing_tables(listing: str) -> list[str]:
    """Имена таблиц из `pg_restore --list` (строки вида «; 215 1259 16390 TABLE public products owner»)."""
    tables = []
    for line in listing.splitlines():
        if line.startswith(";"):
            continue
        m = re.search(r"\bTABLE (\S+) (\S+) ", line + " ")
        if m and " TABLE DATA " not in line:
            tables.append(f"{m.group(1)}.{m.group(2)}")
    return sorted(set(tables))


def _check_db_name(name: str) -> str:
    """Временная база пробного восстановления: только такие базы скрипт может удалить."""
    if not re.fullmatch(r"restore_check_[0-9a-z_]{6,40}", name):
        raise BackupError(f"недопустимое имя временной базы: {name}")
    return name


def _check_target_db_name(name: str) -> str:
    """Новая база для восстановления: простое имя (буквы, цифры, «_»), не служебная база PostgreSQL."""
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name) or name in {"postgres", "template0", "template1"}:
        raise BackupError(f"недопустимое имя новой базы: {name} (только строчные латинские буквы, цифры и «_»)")
    return name


@dataclass
class PgTools:
    """Команды PostgreSQL. Два режима: через `docker compose exec -T db` (профиль Docker) или прямой адрес базы (тесты, Desktop)."""

    compose_file: str | None = None
    database_url: str | None = None
    use_compose: bool = True
    runner: Runner = subprocess.run
    _versions_checked: bool = field(default=False, init=False, repr=False)

    # --- построение команд
    def _compose(self) -> list[str]:
        return ["docker", "compose"] + (["-f", self.compose_file] if self.compose_file else [])

    def _exec_db(self) -> list[str]:
        """`docker compose exec` в контейнер базы со сроком подключения (переменная окружения внутри контейнера)."""
        return self._compose() + ["exec", "-T", "-e", f"PGCONNECT_TIMEOUT={self._connect_timeout()}", "db"]

    def _exec(self, shell: str) -> list[str]:
        return self._exec_db() + ["sh", "-c", shell]

    @staticmethod
    def _connect_timeout() -> str:
        own = os.environ.get("PGCONNECT_TIMEOUT", "").strip()
        return own if own.isdigit() and int(own) > 0 else str(CONNECT_TIMEOUT_S)

    def _url_for(self, dbname: str) -> str:
        """Тот же сервер и учётная запись, другая база (параметры адреса вроде ?sslmode= сохраняются)."""
        from urllib.parse import urlsplit, urlunsplit

        if not self.database_url:
            raise BackupError("не задан адрес базы")
        parts = urlsplit(self.database_url)
        return urlunsplit(parts._replace(path=f"/{dbname}"))

    def _run(self, cmd: list[str], **kw) -> subprocess.CompletedProcess:
        if not self.use_compose and "env" not in kw:
            kw["env"] = {**os.environ, "PGCONNECT_TIMEOUT": self._connect_timeout()}
        try:
            return self.runner(cmd, **kw)
        except FileNotFoundError as exc:
            raise BackupError(f"команда не найдена: {cmd[0]} (Docker Desktop запущен? утилиты PostgreSQL установлены?)") from exc

    @staticmethod
    def _err(proc) -> str:
        return (proc.stderr or b"").decode("utf-8", "replace").strip()[:400] if isinstance(proc.stderr, bytes) else str(proc.stderr or "")[:400]

    def check_versions(self, tool: str = "pg_dump") -> None:
        """Режим прямого адреса: утилиты на этом компьютере должны быть той же основной версии, что и сервер базы. Старее сервера —
        pg_dump откажется сам; новее — копия снимется, но не восстановится на этот же сервер (найдено 2026-11-20 на настоящем
        PostgreSQL: pg_restore 17 пишет «SET transaction_timeout», сервер 16 такого параметра не знает). В режиме Docker утилиты
        берутся из контейнера базы — там версии совпадают всегда. Проверка один раз на объект; версия не прочиталась — не мешаем."""
        if self.use_compose or self._versions_checked:
            return
        proc = self._run([tool, "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        found = re.search(r"PostgreSQL\)\s*(\d+)", (proc.stdout or b"").decode("utf-8", "replace")) if proc.returncode == 0 else None
        server_proc = self._run(["psql", "-v", "ON_ERROR_STOP=1", "-At", "-d", self.database_url],
                                input=b"SHOW server_version_num", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        server_text = (server_proc.stdout or b"").decode("utf-8", "replace").strip() if server_proc.returncode == 0 else ""
        client = int(found.group(1)) if found else None
        server = int(server_text) // 10000 if server_text.isdigit() else None
        if client and server and client != server:
            raise BackupError(
                f"утилиты PostgreSQL на этом компьютере — версии {client}, а сервер базы — версии {server}: копия, снятая или "
                f"восстановленная утилитами другой версии, может не восстановиться на этот сервер. Установите утилиты версии "
                f"{server} (Linux: пакет postgresql-client-{server}) или делайте копию в режиме Docker (без --database-url)")
        self._versions_checked = True

    # --- операции
    def dump(self, out: Path) -> None:
        self.check_versions("pg_dump")
        with Path(out).open("xb") as fh:
            if self.use_compose:
                proc = self._run(self._exec('pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"'), stdout=fh, stderr=subprocess.PIPE)
            else:
                proc = self._run(["pg_dump", "-Fc", "-d", self.database_url], stdout=fh, stderr=subprocess.PIPE)
            fh.flush()
            os.fsync(fh.fileno())
        if proc.returncode != 0:
            err = self._err(proc)
            hint = ""
            if self.use_compose and re.search(r"is not running|no such service|Cannot connect to the Docker daemon|"
                                              r"error during connect", err, re.IGNORECASE):
                hint = (" — база в Docker не запущена: запустите Docker Desktop и программу (или `docker compose up -d db` "
                        "в папке программы) и повторите копию")
            raise BackupError(f"pg_dump завершился с кодом {proc.returncode}: {err}{hint}")
        if Path(out).stat().st_size == 0:
            raise BackupError("дамп базы пуст")

    def listing(self, dump: Path) -> str:
        with Path(dump).open("rb") as fh:
            cmd = self._exec_db() + ["pg_restore", "--list"] if self.use_compose else ["pg_restore", "--list"]
            proc = self._run(cmd, stdin=fh, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        text = (proc.stdout or b"").decode("utf-8", "replace")
        if proc.returncode != 0 or "Archive created at" not in text:
            raise BackupError("дамп не читается pg_restore --list — копия неполноценна")
        return text

    def _psql(self, dbname: str, sql: str) -> str:
        # SQL — через стандартный ввод: без экранирования кавычек внутри `sh -c` контейнера
        if self.use_compose:
            if not re.fullmatch(r"[A-Za-z0-9_]+", dbname):
                raise BackupError(f"недопустимое имя базы: {dbname}")
            cmd = self._exec(f'psql -v ON_ERROR_STOP=1 -At -U "$POSTGRES_USER" -d {dbname}')
        else:
            cmd = ["psql", "-v", "ON_ERROR_STOP=1", "-At", "-d", self._url_for(dbname)]
        proc = self._run(cmd, input=sql.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise BackupError(f"psql: {self._err(proc)}")
        return (proc.stdout or b"").decode("utf-8", "replace")

    def schema_revision(self) -> str | None:
        """Ревизия схемы (Alembic) рабочей базы — в манифест копии. None — база создана до миграций (нет alembic_version)."""
        sql_has = "SELECT to_regclass('public.alembic_version') IS NOT NULL"
        sql_rev = "SELECT version_num FROM alembic_version LIMIT 1"
        if self.use_compose:
            cmd = self._exec('psql -v ON_ERROR_STOP=1 -At -U "$POSTGRES_USER" -d "$POSTGRES_DB"')
        else:
            cmd = ["psql", "-v", "ON_ERROR_STOP=1", "-At", "-d", self.database_url]

        def run(sql: str) -> str:
            proc = self._run(cmd, input=sql.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if proc.returncode != 0:
                raise BackupError(f"psql: {self._err(proc)}")
            return (proc.stdout or b"").decode("utf-8", "replace").strip()

        if run(sql_has) != "t":
            return None
        return run(sql_rev) or None

    def working_db_size(self) -> int | None:
        """Размер рабочей базы в байтах (pg_database_size). Не прочитался — None: проверка места тогда идёт по размеру дампа."""
        sql = "SELECT pg_database_size(current_database())"
        if self.use_compose:
            cmd = self._exec('psql -v ON_ERROR_STOP=1 -At -U "$POSTGRES_USER" -d "$POSTGRES_DB"')
        else:
            cmd = ["psql", "-v", "ON_ERROR_STOP=1", "-At", "-d", self.database_url]
        proc = self._run(cmd, input=sql.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        text = (proc.stdout or b"").decode("utf-8", "replace").strip() if proc.returncode == 0 else ""
        return int(text) if text.isdigit() else None

    def server_free_space(self) -> int | None:
        """Свободное место (байты) на диске, где сервер PostgreSQL хранит базы. None — узнать нельзя (сервер на другом
        компьютере, нет прав читать data_directory, df не сработал): тогда проверка места только предупреждает.
        Docker: `df` внутри контейнера базы по папке данных ($PGDATA). Прямой адрес: только если сервер на этом же компьютере
        (localhost) и его папка данных видна отсюда — по ней shutil.disk_usage."""
        if self.use_compose:
            proc = self._run(self._exec('df -Pk "${PGDATA:-/var/lib/postgresql/data}"'),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if proc.returncode != 0:
                return None
            lines = [ln.split() for ln in (proc.stdout or b"").decode("utf-8", "replace").splitlines() if ln.strip()]
            # df -P: «Filesystem 1024-blocks Used Available Capacity Mounted»; последняя строка — наша папка
            if len(lines) < 2 or len(lines[-1]) < 4 or not lines[-1][3].isdigit():
                return None
            return int(lines[-1][3]) * 1024
        from urllib.parse import urlsplit

        try:
            host = (urlsplit(self.database_url or "").hostname or "").lower()
        except ValueError:
            return None
        if host not in {"", "localhost", "127.0.0.1", "::1"}:
            return None
        proc = self._run(["psql", "-v", "ON_ERROR_STOP=1", "-At", "-d", self.database_url],
                         input=b"SELECT current_setting('data_directory')", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        data_dir = (proc.stdout or b"").decode("utf-8", "replace").strip() if proc.returncode == 0 else ""
        if not data_dir or not Path(data_dir).is_dir():     # например, сервер в Docker, а адрес — через проброшенный порт
            return None
        return shutil.disk_usage(data_dir).free

    def db_exists(self, name: str) -> bool:
        _check_target_db_name(name)
        return self._psql("postgres", f"SELECT 1 FROM pg_database WHERE datname = '{name}'").strip() == "1"

    def create_db(self, name: str) -> None:
        if not name.startswith("restore_check_"):
            _check_target_db_name(name)
        else:
            _check_db_name(name)
        if self.use_compose:
            proc = self._run(self._exec(f'createdb -U "$POSTGRES_USER" {name}'), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if proc.returncode != 0:
                raise BackupError(f"не удалось создать временную базу: {self._err(proc)}")
        else:
            self._psql("postgres", f"CREATE DATABASE {name}")

    def drop_db(self, name: str) -> None:
        _check_db_name(name)                       # удаляем только СВОЮ временную базу с таким шаблоном имени
        if self.use_compose:
            self._run(self._exec(f'dropdb -U "$POSTGRES_USER" --if-exists {name}'), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        else:
            self._psql("postgres", f"DROP DATABASE IF EXISTS {name}")

    def restore_into(self, dump: Path, name: str) -> None:
        if not name.startswith("restore_check_"):
            _check_target_db_name(name)
        else:
            _check_db_name(name)
        self.check_versions("pg_restore")
        with Path(dump).open("rb") as fh:
            if self.use_compose:
                cmd = self._exec(f'pg_restore -U "$POSTGRES_USER" --no-owner --exit-on-error -d {name}')
            else:
                cmd = ["pg_restore", "--no-owner", "--exit-on-error", "-d", self._url_for(name)]
            proc = self._run(cmd, stdin=fh, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise BackupError(f"восстановление во временную базу не удалось: {self._err(proc)}")

    def table_counts(self, name: str) -> dict[str, int]:
        tables = [t for t in self._psql(name, "SELECT schemaname||'.'||tablename FROM pg_tables "
                                              "WHERE schemaname NOT IN ('pg_catalog','information_schema') ORDER BY 1").split()]
        counts = {}
        for table in tables:
            schema, _, tname = table.partition(".")
            if not re.fullmatch(r"[A-Za-z0-9_]+", schema + tname):
                raise BackupError(f"необычное имя таблицы: {table}")
            # имена проверены выше (только буквы, цифры, «_») и взяты из каталога самой временной базы
            counts[table] = int(self._psql(name, f'SELECT count(*) FROM "{schema}"."{tname}"').strip())  # noqa: S608
        return counts


# --------------------------------------------------------------------------- индекс

def copy_index(index_path: Path, out_dir: Path, *, attempts: int = 3) -> dict:
    """Копирует файл индекса и его список id. Индекс могут переписать во время копии — тогда повтор (до attempts раз).
    Согласованность — по полю index_sha256 списка id (формат 2). Не согласован — копия всё равно годна: индекс пересобирается
    из фото при восстановлении (документ 09, §3), статус записывается в манифест."""
    index_path = Path(index_path)
    sidecar = Path(f"{index_path}.ids.json")
    if not index_path.exists():
        return {"status": "absent"}
    out_dir.mkdir(parents=True, exist_ok=True)
    status = "unverified"
    for _ in range(max(1, attempts)):
        for name in (index_path.name, sidecar.name):
            (out_dir / name).unlink(missing_ok=True)
        idx_sha, _ = _copy_with_hash(index_path, out_dir / index_path.name)
        if not sidecar.exists():
            return {"status": "unverified", "reason": "нет списка id", "index_sha256": idx_sha}
        _copy_with_hash(sidecar, out_dir / sidecar.name)
        try:
            meta = json.loads((out_dir / sidecar.name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = None
        expected = meta.get("index_sha256") if isinstance(meta, dict) else None
        if expected is None:
            status = "unverified"
            break
        if expected == idx_sha:
            return {"status": "consistent", "index_sha256": idx_sha, "count": meta.get("count"), "generation": meta.get("generation")}
        status = "inconsistent"
    return {"status": status, "reason": "файл индекса и список id не совпали (индекс переписывали во время копии) — "
                                          "при восстановлении пересобрать из фото" if status == "inconsistent" else "старый формат списка id"}


# --------------------------------------------------------------------------- создание

@dataclass
class Layout:
    project: Path
    media_root: Path
    index_path: Path
    dest: Path

    @property
    def store(self) -> Path:
        return self.dest / STORE_DIR


class _Lock:
    """Одна операция с местом копий за раз (создание / очистка). Файл блокировки создаётся атомарно (O_EXCL)."""

    def __init__(self, dest: Path):
        self.path = Path(dest) / LOCK_NAME

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise BackupError(f"место копий занято другой операцией (файл {self.path}). Если вы уверены, что другой копии "
                              f"сейчас нет (прошлая прервалась), удалите этот файл вручную.") from exc
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"pid": os.getpid(), "started": datetime.now().isoformat(timespec="seconds")}))
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


def backup_name(now: datetime) -> str:
    return f"{BACKUP_PREFIX}{now:%Y-%m-%d_%H%M%S}"


def _files_with_hashes(root: Path, skip: set[str]) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): sha256_file(p) for p in sorted(root.rglob("*"))
            if p.is_file() and p.relative_to(root).as_posix() not in skip}


def same_disk_warning(dest: Path, *sources: Path) -> str | None:
    """Документ 09, §4: копии на том же диске, что и рабочие данные, не спасут при поломке диска — предупредить (не запрещать)."""
    try:
        dest_dev = os.stat(Path(dest)).st_dev
    except OSError:
        return None
    for src in sources:
        try:
            if os.stat(Path(src)).st_dev == dest_dev:
                return (f"копии лежат на том же диске, что и рабочие данные ({src}): при поломке диска пропадут и данные, и копии. "
                        "Храните копии на другом диске или флешке (документ 09, §4).")
        except OSError:
            continue
    return None


def _no_space_message(dest: Path, exc: OSError) -> str:
    try:
        free = shutil.disk_usage(Path(dest)).free / (1024 ** 3)
        where = f"свободно {free:.1f} ГБ"
    except OSError:
        where = "свободное место не определить"
    return (f"нет места на диске с копиями ({dest}; {where}). Освободите место или выберите другой диск; "
            f"недописанная папка «….part» оставлена — её можно удалить вручную. ({exc.strerror or exc})")


def create_backup(layout: Layout, pg: PgTools, *, now: Callable[[], datetime] = datetime.now,
                  out: Callable[[str], None] = print) -> Path:
    """Создаёт и проверяет полную копию. Возвращает путь к готовой папке копии."""
    try:
        return _create_backup(layout, pg, now=now, out=out)
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise BackupError(_no_space_message(layout.dest, exc)) from exc
        raise


def _create_backup(layout: Layout, pg: PgTools, *, now: Callable[[], datetime], out: Callable[[str], None]) -> Path:
    with _Lock(layout.dest):
        stamp = now()
        name = backup_name(stamp)
        final = layout.dest / name
        suffix = 1
        while final.exists() or Path(f"{final}{PART_SUFFIX}").exists():
            suffix += 1
            final = layout.dest / f"{name}-{suffix}"
        work = Path(f"{final}{PART_SUFFIX}")
        work.mkdir(parents=True)
        out(f"Копия: {final.name}")
        warnings = []
        disk_note = same_disk_warning(layout.dest, layout.media_root, layout.project)
        if disk_note:
            warnings.append(disk_note)
            out(f"  внимание: {disk_note}")

        # 1. база — согласованный снимок. Не снялся — в папке «.part» только наш недописанный дамп: убираем её, иначе после
        # каждой неудачи (база остановлена, Docker не запущен) оставалась бы пустая «незавершённая копия» (испытано 2026-11-21).
        try:
            pg.dump(work / "db.dump")
        except BackupError:
            shutil.rmtree(work, ignore_errors=True)
            raise
        tables = listing_tables(pg.listing(work / "db.dump"))
        if not tables:
            raise BackupError("в дампе нет ни одной таблицы")
        schema_revision = pg.schema_revision()
        out(f"  база: таблиц {len(tables)}")

        # 2. фото — после дампа (фото, добавленные позже, просто окажутся лишними)
        rows, new, new_bytes = [], 0, 0
        for path, rel in iter_photo_files(layout.media_root):
            try:
                sha, size, is_new = store_photo(path, layout.store)
            except FileNotFoundError:
                warnings.append(f"фото исчезло во время копии: {rel}")
                out(f"  внимание: {warnings[-1]}")
                continue
            rows.append((sha, size, rel))
            new += int(is_new)
            new_bytes += size if is_new else 0
        _write_text_durable(work / "photos.list", "".join(f"{s}\t{n}\t{r}\n" for s, n, r in rows))
        out(f"  фото: {len(rows)} (новых в хранилище {new}, {new_bytes / (1024 * 1024):.1f} МБ)")

        # 3. индекс (производные данные)
        index_info = copy_index(layout.index_path, work / "index")
        out(f"  индекс: {index_info['status']}")

        # 4. настройки без секретов
        config_files = []
        for rel in CONFIG_FILES:
            src = layout.project / rel
            if src.is_file():
                dst = work / "config" / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                _copy_with_hash(src, dst)
                config_files.append(rel)

        # 5. обученные на каталоге файлы моделей (MODEL_DIRS)
        model_files = []
        for rel_dir in MODEL_DIRS:
            root = layout.project / rel_dir
            for src in sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else ():
                rel = src.relative_to(layout.project).as_posix()
                dst = work / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                _copy_with_hash(src, dst)
                model_files.append(rel)

        manifest = {
            "format": FORMAT,
            "kind": "full",
            "created_at": stamp.isoformat(timespec="seconds"),
            "tool": "scripts/full_backup.py",
            "schema_revision": schema_revision,     # ревизия Alembic рабочей базы; None — база создана до миграций
            "database": {"file": "db.dump", "tables": tables, "size": (work / "db.dump").stat().st_size},
            "photos": {"list": "photos.list", "count": len(rows), "new_in_store": new, "new_bytes": new_bytes,
                       "store": f"../{STORE_DIR}"},
            "index": index_info,
            "config": config_files,
            "models": model_files,
            "excluded": sorted(NEVER_COPY) + [".env", "configs/agent.yaml", "cameras.yaml"],
            "warnings": warnings,
            "files": _files_with_hashes(work, skip=set()),
        }
        problems = verify_backup(work, layout.store, manifest=manifest)
        manifest["verification"] = {"ok": not problems, "problems": problems,
                                    "checked_at": datetime.now().isoformat(timespec="seconds")}
        _write_text_durable(work / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        if problems:
            raise BackupError("копия не прошла проверку: " + "; ".join(problems[:5]) + f" (папка {work.name} оставлена для разбора)")
        _replace_with_retry(work, final)
        _fsync_dir(layout.dest)
        out(f"Готово и проверено: {final}")
        return final


# --------------------------------------------------------------------------- проверка

def load_manifest(backup_dir: Path) -> dict:
    try:
        return json.loads((Path(backup_dir) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupError(f"нет или повреждён manifest.json в {Path(backup_dir).name}") from exc


def verify_backup(backup_dir: Path, store: Path, *, manifest: dict | None = None, full_store: bool = False) -> list[str]:
    """Список проблем (пустой — копия цела): хеши файлов копии по манифесту, наличие фото в хранилище (размер; с full_store — хеш)."""
    backup_dir = Path(backup_dir)
    manifest = manifest if manifest is not None else load_manifest(backup_dir)
    problems: list[str] = []
    if manifest.get("format") != FORMAT:
        problems.append(f"неизвестный формат манифеста: {manifest.get('format')}")
    files = manifest.get("files") or {}
    for rel, sha in files.items():
        path = backup_dir / rel
        if not path.is_file():
            problems.append(f"нет файла {rel}")
        elif sha256_file(path) != sha:
            problems.append(f"файл {rel} изменён или повреждён (хеш не совпадает)")
    for required in ("db.dump", "photos.list"):
        if required not in files:
            problems.append(f"в манифесте нет {required}")
    if (backup_dir / "photos.list").is_file():
        try:
            for sha, size, rel in read_photo_list(backup_dir / "photos.list"):
                stored = store_path(store, sha)
                if not stored.is_file():
                    problems.append(f"в хранилище копий нет фото {rel}")
                elif stored.stat().st_size != size:
                    problems.append(f"фото {rel} в хранилище другого размера")
                elif full_store and sha256_file(stored) != sha:
                    problems.append(f"фото {rel} в хранилище повреждено")
        except BackupError as exc:
            problems.append(str(exc))
    return problems


def control_checks(pg: PgTools, name: str, counts: dict[str, int], photo_rows: list[tuple[str, int, str]]) -> tuple[dict, list[str]]:
    """Контрольные запросы к восстановленной базе (документ 09, §5): действующий администратор, число товаров, фото без товара.
    Возвращает (числа, предупреждения). Предупреждения не делают копию негодной, но показываются человеку."""
    checks: dict = {}
    warnings: list[str] = []
    if "public.products" in counts:
        checks["products"] = counts["public.products"]
    if "public.users" in counts:
        checks["active_admins"] = int(pg._psql(name, "SELECT count(*) FROM users WHERE role = 'admin' AND is_active").strip() or 0)
        if checks["active_admins"] == 0:
            warnings.append("в копии нет действующего администратора: после восстановления войти в управление будет нельзя, "
                            "пока не включить его (python -m scripts.user_admin activate --username …) или не создать нового "
                            "(python -m scripts.create_user --username … --role admin)")
    has_sku = pg._psql(name, "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' "
                             "AND table_name = 'products' AND column_name = 'sku'").strip() == "1"
    if has_sku and photo_rows:
        skus = set(pg._psql(name, "SELECT sku FROM products").split("\n")) - {""}
        folders = {rel.split("/")[1] for _, _, rel in photo_rows if rel.count("/") >= 2 and rel.split("/")[0] in PHOTO_SUBDIRS}
        orphans = sorted(folders - skus)
        checks["photo_folders"] = len(folders)
        checks["photo_folders_without_product"] = len(orphans)
        if orphans:
            warnings.append(f"фото без товара в базе: папок {len(orphans)} (например, {', '.join(orphans[:3])}) — "
                            "товар удалён после съёмки или копия базы старее фото")
    # Обратная сверка: файлы фото, записанные в базе (product_images.storage_path), должны быть в копии. Иначе после
    # восстановления у товаров не будет фото для показа и пересборки индекса.
    has_storage = pg._psql(name, "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' "
                                 "AND table_name = 'product_images' AND column_name = 'storage_path'").strip() == "1"
    if has_storage:
        db_paths = set(pg._psql(name, "SELECT storage_path FROM product_images WHERE storage_path IS NOT NULL "
                                      "AND storage_path <> ''").split("\n")) - {""}
        in_copy = {rel for _, _, rel in photo_rows}
        lost = sorted(db_paths - in_copy)
        checks["db_photos"] = len(db_paths)
        checks["db_photos_missing_in_backup"] = len(lost)
        if lost:
            warnings.append(f"в базе записано фото, которого нет в копии: {len(lost)} (например, {', '.join(lost[:3])}) — "
                            "файл удалён с диска или лежит вне папок фото; после восстановления у этих товаров фото не будет")
    return checks, warnings


def _gb(n: int) -> str:
    if n < 1024 ** 3:
        return f"{max(1, round(n / 1024 ** 2))} МБ"
    return f"{n / 1024 ** 3:.1f}".replace(".", ",") + " ГБ"


def check_server_space(pg: PgTools, manifest: dict, *, purpose: str, out: Callable[[str], None] = print) -> dict:
    """Перед созданием новой базы на сервере PostgreSQL (пробное восстановление — временная база размером с рабочую): хватит ли
    места на диске сервера. Не хватает — BackupError ДО создания базы (иначе восстановление заполнило бы диск, и рабочая база
    встала бы с ошибкой «нет места»). Нужно: размер рабочей базы (не прочитался — размер дампа × DUMP_EXPANSION) × 1,2 +
    запас SPACE_RESERVE_BYTES (журнал WAL во время восстановления, сама рабочая база). Место узнать нельзя — предупреждение,
    не отказ. Возвращает {"free_bytes", "need_bytes", "note"} (free_bytes — None, если место узнать нельзя)."""
    db_size = pg.working_db_size()
    dump_size = int((manifest.get("database") or {}).get("size") or 0)
    estimate = db_size if db_size else dump_size * DUMP_EXPANSION
    need = int(estimate * 1.2) + SPACE_RESERVE_BYTES
    free = pg.server_free_space()
    if free is None:
        note = (f"не удалось узнать свободное место на диске сервера базы — {purpose} займёт около {_gb(estimate)}; "
                f"проверьте место сами (нужно не меньше {_gb(need)})")
        out(f"  внимание: {note}")
        return {"free_bytes": None, "need_bytes": need, "note": note}
    if free < need:
        raise BackupError(
            f"на диске сервера базы свободно {_gb(free)}, а для {purpose} нужно около {_gb(need)} "
            f"(новая база размером с рабочую — {_gb(estimate)} — и запас {_gb(SPACE_RESERVE_BYTES)}). Ничего не создано. "
            f"Освободите место на диске, где хранится база (в Docker Desktop — диск его виртуальной машины), и повторите")
    out(f"  место на сервере базы: свободно {_gb(free)}, нужно около {_gb(need)}")
    return {"free_bytes": free, "need_bytes": need, "note": None}


def restore_check(backup_dir: Path, pg: PgTools, *, out: Callable[[str], None] = print) -> dict:
    """Пробное восстановление дампа во ВРЕМЕННУЮ базу, сверка списка таблиц, подсчёт строк, контрольные запросы (§5);
    временная база удаляется. Итог записывается в restore_check.json рядом с манифестом (манифест не меняется)."""
    backup_dir = Path(backup_dir)
    manifest = load_manifest(backup_dir)
    name = f"restore_check_{datetime.now():%Y%m%d%H%M%S}_{secrets.token_hex(3)}"
    result: dict = {"database": name, "started_at": datetime.now().isoformat(timespec="seconds")}
    result["space"] = check_server_space(pg, manifest, purpose="пробного восстановления", out=out)
    pg.create_db(name)
    try:
        pg.restore_into(backup_dir / "db.dump", name)
        counts = pg.table_counts(name)
        expected = set(manifest["database"]["tables"])
        missing = sorted(expected - set(counts))
        photo_list = backup_dir / "photos.list"
        rows = read_photo_list(photo_list) if photo_list.is_file() else []
        checks, warnings = control_checks(pg, name, counts, rows)
        result.update({"ok": not missing, "missing_tables": missing, "row_counts": counts, "checks": checks, "warnings": warnings})
    except BackupError as exc:
        result.update({"ok": False, "error": str(exc)})
    finally:
        pg.drop_db(name)
    result["finished_at"] = datetime.now().isoformat(timespec="seconds")
    _write_text_durable(backup_dir / "restore_check.json", json.dumps(result, ensure_ascii=False, indent=2))
    out(("Восстановление проверено: " if result["ok"] else "ВОССТАНОВЛЕНИЕ НЕ ПРОШЛО: ") + str(backup_dir.name))
    for warning in result.get("warnings", []):
        out(f"  внимание: {warning}")
    return result


# --------------------------------------------------------------------------- восстановление (без перезаписи)

def _safe_relpath(rel: str) -> Path:
    """Путь из photos.list → относительный путь без «..» и без абсолютных частей (повреждённая копия не пишет вне папки)."""
    from pathlib import PurePosixPath

    pure = PurePosixPath(rel)
    if pure.is_absolute() or not pure.parts or any(part in ("..", "") for part in pure.parts) or "\\" in rel or ":" in rel:
        raise BackupError(f"недопустимый путь в списке фото: {rel!r}")
    return Path(*pure.parts)


def materialize_files(backup_dir: Path, store: Path, out_dir: Path, *, out: Callable[[str], None] = print) -> dict:
    """Раскладывает файлы копии в НОВУЮ пустую папку: data/<путь фото>, models/index/…, models/projection/…, config/…; каждое
    фото сверяется по хешу.
    Существующую непустую папку не трогает (ничего не перезаписывается)."""
    backup_dir, store, out_dir = Path(backup_dir), Path(store), Path(out_dir)
    problems = verify_backup(backup_dir, store)
    if problems:
        raise BackupError("копия не прошла проверку, восстанавливать её нельзя: " + "; ".join(problems[:5]))
    if out_dir.exists() and any(out_dir.iterdir()):
        raise BackupError(f"папка {out_dir} не пуста — выберите новую пустую папку (существующие файлы не перезаписываются)")
    rows = read_photo_list(backup_dir / "photos.list")
    targets = [(sha, size, out_dir / "data" / _safe_relpath(rel)) for sha, size, rel in rows]   # все пути — до первой записи
    out_dir.mkdir(parents=True, exist_ok=True)
    for sha, _size, target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        got, _ = _copy_with_hash(store_path(store, sha), target)
        if got != sha:
            raise BackupError(f"фото {target.relative_to(out_dir)} в хранилище копий повреждено (хеш не совпал)")
    for sub, dest in (("index", out_dir / "models" / "index"), ("config", out_dir / "config"),
                      ("models", out_dir / "models")):
        src_root = backup_dir / sub
        if src_root.is_dir():
            for src in sorted(p for p in src_root.rglob("*") if p.is_file()):
                target = dest / src.relative_to(src_root)
                target.parent.mkdir(parents=True, exist_ok=True)
                _copy_with_hash(src, target)
    out(f"  файлы: фото {len(targets)} → {out_dir / 'data'}")
    return {"photos": len(targets), "out_dir": str(out_dir)}


def restore_to_new(backup_dir: Path, pg: PgTools, *, target_db: str, out_dir: Path,
                   out: Callable[[str], None] = print) -> dict:
    """Восстановление копии БЕЗ перезаписи рабочей системы (документ 09, §4: «восстановление в новую базу → проверки → переключение»):
    база — в НОВУЮ базу target_db (если такая есть — отказ), файлы — в новую пустую папку. Переключение на восстановленную систему
    этот скрипт не делает: это отдельный шаг человека (после проверки)."""
    backup_dir = Path(backup_dir)
    manifest = load_manifest(backup_dir)
    _check_target_db_name(target_db)
    if pg.db_exists(target_db):
        raise BackupError(f"база {target_db} уже существует — восстановление делается только в новую базу")
    check_server_space(pg, manifest, purpose="восстановления в новую базу", out=out)
    files = materialize_files(backup_dir, backup_dir.parent / STORE_DIR, out_dir, out=out)
    pg.create_db(target_db)
    pg.restore_into(backup_dir / "db.dump", target_db)
    counts = pg.table_counts(target_db)
    missing = sorted(set(manifest["database"]["tables"]) - set(counts))
    report = {"backup": backup_dir.name, "database": target_db, "ok": not missing, "missing_tables": missing,
              "row_counts": counts, "files": files, "index": manifest.get("index"),
              "finished_at": datetime.now().isoformat(timespec="seconds")}
    _write_text_durable(Path(out_dir) / "restore_report.json", json.dumps(report, ensure_ascii=False, indent=2))
    out(("Восстановлено в новую базу " if report["ok"] else "ВОССТАНОВЛЕНИЕ НЕПОЛНОЕ: ") + target_db)
    return report


# --------------------------------------------------------------------------- ротация

@dataclass(frozen=True)
class BackupInfo:
    name: str
    created: datetime
    verified: bool


@dataclass
class PrunePlan:
    keep: list[str] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)
    orphan_photos: list[str] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)


def list_backups(dest: Path) -> tuple[list[BackupInfo], list[str]]:
    """Готовые копии (с манифестом) и незавершённые/повреждённые папки — последние только сообщаются."""
    good, other = [], []
    for path in sorted(Path(dest).glob(f"{BACKUP_PREFIX}*")):
        if not path.is_dir():
            continue
        if path.name.endswith(PART_SUFFIX):
            other.append(path.name)
            continue
        try:
            manifest = load_manifest(path)
            created = datetime.fromisoformat(manifest["created_at"])
        except (BackupError, KeyError, ValueError):
            other.append(path.name)
            continue
        good.append(BackupInfo(path.name, created, bool((manifest.get("verification") or {}).get("ok"))))
    return good, other


def choose_kept(backups: list[BackupInfo]) -> set[str]:
    """7 ежедневных + 4 еженедельных + 6 ежемесячных (самая свежая копия периода) + последняя проверенная — всегда."""
    ordered = sorted(backups, key=lambda b: b.created, reverse=True)
    keep: set[str] = set()
    for key_fn, limit in ((lambda d: d.date(), KEEP_DAILY), (lambda d: d.isocalendar()[:2], KEEP_WEEKLY),
                          (lambda d: (d.year, d.month), KEEP_MONTHLY)):
        seen = []
        for b in ordered:
            k = key_fn(b.created)
            if k not in seen:
                seen.append(k)
                if len(seen) > limit:
                    break
                keep.add(b.name)
    verified = [b for b in ordered if b.verified]
    if verified:
        keep.add(verified[0].name)
    return keep


def plan_prune(dest: Path) -> PrunePlan:
    dest = Path(dest)
    backups, incomplete = list_backups(dest)
    kept = choose_kept(backups)
    plan = PrunePlan(keep=sorted(kept), delete=sorted(b.name for b in backups if b.name not in kept), incomplete=incomplete)
    referenced: set[str] = set()
    # фото, на которые ссылаются оставшиеся копии И незавершённые/повреждённые папки (их фото не трогаем)
    for name in plan.keep + incomplete:
        lst = dest / name / "photos.list"
        if not lst.is_file():
            if name in incomplete and (dest / name / "db.dump").exists():
                # папка с дампом, но без списка фото: неясно, на что она ссылается — фото не удаляем вовсе
                return PrunePlan(keep=plan.keep, delete=plan.delete, orphan_photos=[], incomplete=incomplete)
            continue
        try:
            referenced.update(sha for sha, _size, _rel in read_photo_list(lst))
        except BackupError:
            return PrunePlan(keep=plan.keep, delete=[], orphan_photos=[], incomplete=incomplete)  # не уверены — ничего не удаляем
    store = dest / STORE_DIR
    if store.is_dir():
        for path in sorted(store.glob("??/*")):
            if path.is_file() and re.fullmatch(r"[0-9a-f]{64}", path.name) and path.name not in referenced:
                plan.orphan_photos.append(path.name)
    return plan


def apply_prune(dest: Path, plan: PrunePlan, *, out: Callable[[str], None] = print) -> None:
    """Удаляет копии из plan.delete и фото без ссылок. Вызывается только явно (--apply), под блокировкой места копий."""
    dest = Path(dest)
    with _Lock(dest):
        fresh = plan_prune(dest)                 # план пересчитывается под блокировкой: удаляется только то, что лишнее сейчас
        for name in fresh.delete:
            if name in plan.delete:
                shutil.rmtree(dest / name)
                out(f"удалена копия {name}")
        shown = set(plan.orphan_photos)
        removed = 0
        for sha in fresh.orphan_photos:
            if sha in shown:                     # только то, что было в показанном плане и всё ещё без ссылок
                store_path(dest / STORE_DIR, sha).unlink(missing_ok=True)
                removed += 1
        out(f"удалено фото без ссылок: {removed}")


# --------------------------------------------------------------------------- командная строка

def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _pg_from_args(args) -> PgTools:
    if args.database_url:
        return PgTools(database_url=args.database_url, use_compose=False)
    return PgTools(compose_file=args.compose_file, use_compose=True)


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(description="Полная резервная копия «Узнать товар» с проверкой")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p, db=True):
        p.add_argument("--dest", default=str(_project_root() / "backups" / "full"),
                       help="место копий (лучше другой диск или флешка)")
        if db:
            p.add_argument("--compose-file", default=None)
            p.add_argument("--database-url", default=None, help="вместо docker compose — прямой адрес базы")

    p_create = sub.add_parser("create")
    common(p_create)
    p_create.add_argument("--project", default=str(_project_root()))
    p_create.add_argument("--media", default="data")
    p_create.add_argument("--index", default="models/index/catalog.faiss")
    p_create.add_argument("--restore-check", action="store_true", help="сразу проверить восстановление во временную базу")
    p_verify = sub.add_parser("verify")
    common(p_verify, db=False)
    p_verify.add_argument("name")
    p_verify.add_argument("--full-store", action="store_true", help="пересчитать хеш каждого фото в хранилище копий")
    p_rc = sub.add_parser("restore-check")
    common(p_rc)
    p_rc.add_argument("name")
    p_restore = sub.add_parser("restore", help="восстановить в НОВУЮ базу и новую пустую папку (рабочая система не меняется)")
    common(p_restore)
    p_restore.add_argument("name")
    p_restore.add_argument("--target-db", required=True, help="имя НОВОЙ базы (существующая — отказ)")
    p_restore.add_argument("--out", required=True, help="новая пустая папка для фото, индекса и настроек")
    p_prune = sub.add_parser("prune")
    common(p_prune, db=False)
    p_prune.add_argument("--apply", action="store_true", help="действительно удалить (без флага — только план)")
    args = parser.parse_args(argv)
    dest = Path(args.dest)
    try:
        if args.cmd == "create":
            project = Path(args.project)
            layout = Layout(project=project, media_root=project / args.media, index_path=project / args.index, dest=dest)
            pg = _pg_from_args(args)
            final = create_backup(layout, pg, out=out)
            if args.restore_check and not restore_check(final, pg, out=out)["ok"]:
                return 1
            return 0
        if args.cmd == "verify":
            problems = verify_backup(dest / args.name, dest / STORE_DIR, full_store=args.full_store)
            for p in problems:
                out(f"ПРОБЛЕМА: {p}")
            out("Копия цела." if not problems else f"Найдено проблем: {len(problems)}")
            return 0 if not problems else 1
        if args.cmd == "restore":
            report = restore_to_new(dest / args.name, _pg_from_args(args), target_db=args.target_db, out_dir=Path(args.out), out=out)
            return 0 if report["ok"] else 1
        if args.cmd == "restore-check":
            return 0 if restore_check(dest / args.name, _pg_from_args(args), out=out)["ok"] else 1
        if args.cmd == "prune":
            plan = plan_prune(dest)
            out(f"Оставить: {len(plan.keep)}; удалить копий: {len(plan.delete)}; фото без ссылок: {len(plan.orphan_photos)}")
            for name in plan.delete:
                out(f"  лишняя копия: {name}")
            for name in plan.incomplete:
                out(f"  незавершённая или повреждённая папка (не трогаем): {name}")
            if args.apply:
                apply_prune(dest, plan, out=out)
            else:
                out("Это только план. Для удаления запустите с --apply.")
            return 0
    except BackupError as exc:
        out(f"ОШИБКА: {exc}")
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
