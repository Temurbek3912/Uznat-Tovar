"""
Резервная копия магазина в облегчённом режиме (база SQLite в data/lite; полный режим на Docker — scripts/full_backup.py).

Что входит в копию: база (товары, штрихкоды, фото товаров в базе, сотрудники, журнал) — согласованный снимок SQLite, сжатый;
индекс каталога; фото товаров (`<media>/catalog-uploads` — добавленные в браузере, `<media>/catalog-imported` — из импорта
каталога; вторая папка — с 2026-11-21, формат 2); ключи входа и камер из .env (без них после восстановления на другом
компьютере не расшифровать второй фактор и адреса камер). Снимки полок (`shelf-images`) и временные кадры не входят.

Куда: всегда в папку на этом компьютере — выбранную администратором в «Настройках» (`<media>/backup_settings.json`,
например D:/Копии или флешка), иначе BACKUP_LOCAL_DIR, иначе `backups/magazin` программы. Выбранный диск недоступен
(флешку вынули) — копия всё равно делается, в стандартную папку, а в «Настройках» видно предупреждение.
Облако — Google Диск напрямую (кнопка «Подключить Google Диск», app/services/gdrive_backup.py): готовая копия с компьютера
отправляется туда отдельным шагом в фоне; нет интернета — копия на компьютере всё равно есть, отправка повторится.
Для знающих: BACKUP_CLOUD_DIR — любая синхронизируемая папка (Яндекс Диск, OneDrive), туда копия кладётся как в папку.

Устройство папки (одинаково на компьютере и в облаке):

    Узнать товар — копии/
      photos/ab/abcdef…            ← общее хранилище фото: файл назван по SHA-256 и хранится один раз на все копии,
                                     поэтому в облако каждый день уходят только новые фото и база
      kopiya-2026-11-16_030000/
        manifest.json              ← когда, сколько товаров и сотрудников, SHA-256 каждого файла
        shop.db.gz                 ← база
        index/…                    ← индекс каталога (его можно и пересобрать из базы: scripts/rebuild_index.py)
        photos.list                ← «sha256<TAB>размер<TAB>путь» — какие фото у этой копии (путь от папки данных:
                                     «catalog-imported/…»; в копиях формата 1 — от catalog-uploads)
        keys.env                   ← AUTH_SECRET_KEY, CAMERA_SECRET_KEY

Копия собирается в папке «….part» и получает имя только после проверки (SHA-256 записанных файлов, проверка базы
PRAGMA integrity_check). Хранится KEEP_LOCAL копий на компьютере и KEEP_CLOUD в облаке; фото, на которые не ссылается ни одна
оставшаяся копия, удаляются из хранилища. Удаляются только папки и файлы с нашими именами внутри папки копий.
Восстановление — scripts/lite_restore.py («Восстановить из копии.bat»).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import string
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

FOLDER_NAME = "Узнать товар — копии"
COPY_PREFIX = "kopiya-"
PART_SUFFIX = ".part"
PHOTOS_DIR = "photos"
MANIFEST = "manifest.json"
PHOTOS_LIST = "photos.list"
KEYS_FILE = "keys.env"
DB_FILE = "shop.db.gz"
INDEX_DIR = "index"
UPLOADS_DIR = "catalog-uploads"
IMPORTED_DIR = "catalog-imported"     # копии фото импорта каталога (app/services/product_images.py, IMPORTED_SUBDIR)
# Все папки фото товаров внутри media_root. До 2026-11-21 (формат 1) копия брала только catalog-uploads, и фото импорта
# каталога в неё не попадали. Как в полной копии (scripts/full_backup.py, PHOTO_SUBDIRS).
PHOTO_SUBDIRS = (UPLOADS_DIR, IMPORTED_DIR)
STATE_FILE = "backup_state.json"
# Формат копии. 1 — пути в photos.list от папки catalog-uploads (копии до 2026-11-21); 2 — от папки данных (media_root):
# «catalog-uploads/…» или «catalog-imported/…», как product_images.storage_path. Восстанавливаются оба.
FORMAT = 2
FORMATS = (1, 2)

KEEP_LOCAL = 14
KEEP_CLOUD = 7
INTERVAL = timedelta(hours=24)        # копия раз в сутки
RETRY_AFTER = timedelta(hours=1)      # не получилось — повтор через час
FIRST_DELAY_SECONDS = 600             # после запуска программы — не раньше чем через 10 минут (модели, первые покупатели)
CHECK_SECONDS = 900                   # проверять «пора ли» раз в 15 минут
SPARE_BYTES = 200 * 1024 * 1024       # запас места сверх размера копии

ENV_LOCAL_DIR = "BACKUP_LOCAL_DIR"
ENV_CLOUD_DIR = "BACKUP_CLOUD_DIR"
KEY_NAMES = ("AUTH_SECRET_KEY", "CAMERA_SECRET_KEY")
SETTINGS_FILE = "backup_settings.json"

_COPY_RE = re.compile(r"^" + re.escape(COPY_PREFIX) + r"\d{4}-\d{2}-\d{2}_\d{6}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class BackupError(RuntimeError):
    """Копию сделать или проверить нельзя. Текст — для администратора магазина; code/params — для перевода в «Настройках»."""

    def __init__(self, message: str, code: str = "", params: dict | None = None):
        super().__init__(message)
        self.code = code
        self.params = dict(params or {})


@dataclass(frozen=True)
class Destination:
    kind: str          # "local" | "cloud"
    root: Path         # папка «Узнать товар — копии»
    keep: int
    fallback_from: str = ""     # выбранная папка недоступна (флешку вынули) — копия идёт в стандартную, это — какая была


@dataclass
class Photo:
    sha256: str
    size: int
    rel: str           # через «/»: формат 2 — от папки данных («catalog-imported/…»); формат 1 — внутри catalog-uploads
    source: Path | None = None


@dataclass
class CopyInfo:
    path: Path
    name: str
    created_at: str
    counts: dict = field(default_factory=dict)


# ----------------------------------------------------------------------------------------------------------- куда копировать

_settings_lock = threading.Lock()
# Чтение и запись маленьких файлов настроек и состояния — по очереди: в Windows файл нельзя заменить, пока его читает
# другой поток («Настройки» опрашивают состояние во время отправки в облако). Найдено повторными прогонами
# tests/test_gdrive_backup.py 2026-11-16 (PermissionError примерно 1 раз из 10). Программа — один процесс, замка хватает;
# повторы замены остаются на случай антивируса.
_file_lock = threading.RLock()


def read_json(path: Path) -> dict:
    """Словарь из JSON-файла; нет файла или он испорчен — пустой."""
    try:
        with _file_lock:
            data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def read_settings(media_root: Path | None) -> dict:
    """Выбор администратора из «Настроек» (`<media>/backup_settings.json`): local_dir — своя папка, google — подключение."""
    if media_root is None:
        return {}
    return read_json(media_root / SETTINGS_FILE)


def write_json(path: Path, data) -> None:
    """Записать JSON целиком или никак (под _file_lock; замена с повторами, если файл держит антивирус)."""
    from app.pipeline.index_store import replace_with_retry
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with _file_lock:
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            replace_with_retry(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


def update_settings(media_root: Path, change) -> dict:
    """Прочитать — изменить (change(dict) -> dict) — записать целиком, под замком (папка и Google пишутся из разных потоков)."""
    with _settings_lock:
        data = change(read_settings(media_root))
        write_json(media_root / SETTINGS_FILE, data)
        return data


def _folder_of(path: Path) -> Path:
    return path if path.name == FOLDER_NAME else path / FOLDER_NAME


def _anchor_ok(path: Path) -> bool:
    try:
        return Path(path.anchor or path).exists()
    except OSError:
        return False


def destinations(root: Path, environ: dict | None = None, *, chosen: str = "") -> list[Destination]:
    """Первая — всегда папка на этом компьютере: выбранная в «Настройках» (chosen), иначе BACKUP_LOCAL_DIR, иначе
    backups/magazin программы. Выбранный диск недоступен (флешку вынули) — стандартная папка, с пометкой fallback_from.
    Вторая — папка облака BACKUP_CLOUD_DIR (любая синхронизируемая папка), если задана. Google Диск напрямую —
    app/services/gdrive_backup.py, он идёт отдельным шагом после копии на компьютере."""
    env = os.environ if environ is None else environ
    default = Path(env.get(ENV_LOCAL_DIR) or (root / "backups" / "magazin"))
    fallback = ""
    local = default
    if chosen.strip():
        wanted = Path(chosen.strip())
        if _anchor_ok(wanted):
            local = wanted
        else:
            fallback = chosen.strip()
    out = [Destination("local", _folder_of(local), KEEP_LOCAL, fallback)]
    cloud_raw = (env.get(ENV_CLOUD_DIR) or "").strip()
    if cloud_raw and cloud_raw.lower() not in ("off", "no", "0", "нет"):
        out.append(Destination("cloud", _folder_of(Path(cloud_raw)), KEEP_CLOUD))
    return out


def destinations_from_settings(root: Path, settings, media_root: Path | None = None) -> list[Destination]:
    """Места копий: выбор в «Настройках» (backup_settings.json в media_root) и .env (BACKUP_LOCAL_DIR, BACKUP_CLOUD_DIR)."""
    return destinations(root, {ENV_LOCAL_DIR: getattr(settings, "backup_local_dir", "") or "",
                               ENV_CLOUD_DIR: getattr(settings, "backup_cloud_dir", "") or ""},
                        chosen=str(read_settings(media_root).get("local_dir") or ""))


def shop_bytes(media_root: Path, db_file: Path | None = None) -> int:
    """Сколько места займёт первая копия: база + фото + индекс (сжатие базы не учитываем — с запасом)."""
    total = 0
    for p in ([db_file] if db_file else []):
        try:
            total += p.stat().st_size
        except OSError:
            pass
    for sub in (*PHOTO_SUBDIRS, INDEX_DIR):
        d = media_root / sub
        if d.is_dir():
            for f in d.rglob("*"):
                try:
                    if f.is_file():
                        total += f.stat().st_size
                except OSError:
                    pass
    return total


def check_folder(raw: str, *, media_root: Path, need_bytes: int = 0) -> Path:
    """Проверить папку, которую администратор вписал в «Настройках»: полный путь, диск есть, писать можно, места хватает.
    Папки нет — создаётся. -> путь (как вписан). Ошибка — BackupError с понятным текстом."""
    text = (raw or "").strip().strip('"').strip()
    if not text:
        raise BackupError("Впишите папку, например D:\\Копии.", "backup_folder_empty")
    path = Path(text)
    if not path.is_absolute() or (os.name == "nt" and not path.drive):
        raise BackupError("Нужен полный путь с буквой диска, например D:\\Копии или E:\\.", "backup_folder_relative")
    if not _anchor_ok(path):
        raise BackupError(f"Диска {path.anchor or text} нет. Вставьте флешку или выберите другой диск.", "backup_folder_no_drive",
                          {"drive": path.anchor or text})
    try:
        target = path.resolve()
        media = media_root.resolve()
    except OSError as exc:
        raise BackupError("Путь не читается — проверьте, как он вписан.", "backup_folder_relative") from exc
    if target == media or media in target.parents:
        raise BackupError("Эта папка внутри данных программы — выберите другую.", "backup_folder_inside_data")
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".uznat-probe-{os.getpid()}"
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        if isinstance(exc, PermissionError):
            raise BackupError("В эту папку нельзя записывать — выберите другую.", "backup_folder_readonly") from exc
        raise BackupError(_safe_error(exc), "backup_folder_error", {"reason": _safe_error(exc)}) from exc
    free = _free_bytes(path)
    if free is not None and need_bytes and free < need_bytes + SPARE_BYTES:
        need_mb, free_mb = (need_bytes + SPARE_BYTES) // 2**20, free // 2**20
        raise BackupError(f"Мало места: для копии нужно около {need_mb} МБ, свободно {free_mb} МБ.", "backup_folder_no_space",
                          {"need": need_mb, "free": free_mb})
    return path


def list_drives() -> list[dict]:
    """Диски компьютера для подсказки в «Настройках»: [{path: "D:\\", free, total, removable}]. Не Windows — пусто."""
    if os.name != "nt":
        return []
    import ctypes
    out = []
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except (AttributeError, OSError):
        return out
    for i, letter in enumerate(string.ascii_uppercase):
        if not mask & (1 << i):
            continue
        path = f"{letter}:\\"
        kind = ctypes.windll.kernel32.GetDriveTypeW(path)
        if kind not in (2, 3):            # 2 — флешка/съёмный, 3 — обычный диск; сетевые и CD не предлагаем
            continue
        try:
            usage = shutil.disk_usage(path)
        except OSError:                   # пустой картридер — диска «нет»
            continue
        out.append({"path": path, "free": usage.free, "total": usage.total, "removable": kind == 2})
    return out


def sqlite_path(database_url: str) -> Path:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise BackupError("Копия этой программой — только для облегчённого режима (база SQLite).")
    return Path(database_url[len(prefix):])


# ----------------------------------------------------------------------------------------------------------- мелочи

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _now_local(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone()


def copy_name(now: datetime | None = None) -> str:
    return COPY_PREFIX + _now_local(now).strftime("%Y-%m-%d_%H%M%S")


def _db_counts(conn: sqlite3.Connection) -> dict:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    counts = {}
    for key, table in (("products", "products"), ("users", "users"), ("barcodes", "product_barcodes")):
        if table in tables:
            counts[key] = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    return counts


def snapshot_db(src: Path, dst: Path) -> dict:
    """Согласованный снимок SQLite (штатное копирование SQLite: программа может работать). Возвращает число записей."""
    if not src.is_file():
        raise BackupError(f"Нет файла базы: {src}")
    source = sqlite3.connect(str(src), timeout=30)
    target = sqlite3.connect(str(dst))
    try:
        source.backup(target)
        check = target.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise BackupError(f"База повреждена (integrity_check: {check}) — копия не сделана.")
        return _db_counts(target)
    finally:
        target.close()
        source.close()


def check_db_file(path: Path) -> dict:
    conn = sqlite3.connect(str(path))
    try:
        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise BackupError(f"База в копии повреждена (integrity_check: {check}).")
        return _db_counts(conn)
    finally:
        conn.close()


def _gzip(src: Path, dst: Path) -> None:
    with open(src, "rb") as fin, gzip.open(dst, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout, 1024 * 1024)


def gunzip(src: Path, dst: Path) -> None:
    with gzip.open(src, "rb") as fin, open(dst, "wb") as fout:
        shutil.copyfileobj(fin, fout, 1024 * 1024)


def scan_photos(media_root: Path) -> list[Photo]:
    """Все фото товаров из папок PHOTO_SUBDIRS; путь — от media_root. Недописанные временные файлы («.part», «.tmp-…») — нет."""
    photos: list[Photo] = []
    for sub in PHOTO_SUBDIRS:
        base = media_root / sub
        if not base.is_dir():
            continue
        for path in sorted(p for p in base.rglob("*") if p.is_file()):
            if ".part" in path.name or path.name.startswith(".tmp-"):
                continue
            try:
                photos.append(Photo(sha256_file(path), path.stat().st_size, path.relative_to(media_root).as_posix(), path))
            except OSError as exc:       # фото удалили во время копии — без него
                logger.warning("Копия: фото %s пропущено (%s)", path, type(exc).__name__)
    return photos


def db_photos_missing(db: Path, photos: list[Photo]) -> list[str]:
    """Обратная сверка: файлы фото, записанные в базе (product_images.storage_path), которых нет среди фото копии.
    Такие товары после восстановления останутся без фото. Нет таблицы или столбца (старая база) — пусто."""
    conn = sqlite3.connect(str(db))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(product_images)")}
        if "storage_path" not in cols:
            return []
        paths = {r[0] for r in conn.execute("SELECT storage_path FROM product_images "
                                            "WHERE storage_path IS NOT NULL AND storage_path <> ''")}
    finally:
        conn.close()
    return sorted(paths - {p.rel for p in photos})


def photo_target(root: Path, photo: Photo, fmt: int) -> Path:
    """Куда класть фото при восстановлении (root — будущая папка данных). Формат 1 — в catalog-uploads, как раньше;
    формат 2 — по его пути, только внутри папок фото (иначе строка списка могла бы заменить, например, базу)."""
    if fmt == 1:
        return root / UPLOADS_DIR / Path(photo.rel)
    parts = Path(photo.rel).parts
    if len(parts) < 2 or parts[0] not in PHOTO_SUBDIRS:
        raise BackupError(f"Фото {photo.rel} — вне папок фото; копия испорчена. Выберите другую копию.")
    return root / Path(photo.rel)


def copy_format(copy_dir: Path) -> int:
    try:
        return int(json.loads((copy_dir / MANIFEST).read_text(encoding="utf-8")).get("format"))
    except (OSError, ValueError, TypeError) as exc:
        raise BackupError("Нет или не читается manifest.json — выберите другую копию.") from exc


def read_photos_list(path: Path) -> list[Photo]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        sha, size, rel = line.split("\t", 2)
        if not _SHA_RE.match(sha) or rel.startswith("/") or ".." in Path(rel).parts:
            raise BackupError(f"Неверная строка в {path.name}: {line[:80]}")
        out.append(Photo(sha, int(size), rel))
    return out


def store_path(store: Path, sha: str) -> Path:
    return store / sha[:2] / sha


def _free_bytes(path: Path) -> int | None:
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


# ----------------------------------------------------------------------------------------------------------- создание

def build_copy(work: Path, *, db_file: Path, media_root: Path, keys: dict[str, str], now: datetime | None = None,
               app_version: str = "") -> tuple[dict, list[Photo]]:
    """Собирает копию во временной папке work (без фото — они идут в общее хранилище). Возвращает manifest и список фото."""
    work.mkdir(parents=True, exist_ok=True)
    raw_db = work / "shop.db"
    counts = snapshot_db(db_file, raw_db)
    photos = scan_photos(media_root)
    missing = db_photos_missing(raw_db, photos)
    if missing:
        logger.warning("Копия: в базе записано фото, которого нет на диске: %d (например, %s) — после восстановления "
                       "у этих товаров фото не будет", len(missing), ", ".join(missing[:3]))
    _gzip(raw_db, work / DB_FILE)
    raw_db.unlink()
    index_src = media_root / INDEX_DIR
    if index_src.is_dir():
        (work / INDEX_DIR).mkdir()
        for f in sorted(index_src.iterdir()):
            if f.is_file() and not f.name.endswith(".lock") and ".tmp" not in f.name:
                shutil.copy2(f, work / INDEX_DIR / f.name)
    (work / PHOTOS_LIST).write_text("".join(f"{p.sha256}\t{p.size}\t{p.rel}\n" for p in photos), encoding="utf-8")
    (work / KEYS_FILE).write_text("".join(f"{k}={keys[k]}\n" for k in KEY_NAMES if keys.get(k)), encoding="utf-8")
    files = {p.relative_to(work).as_posix(): sha256_file(p) for p in sorted(work.rglob("*")) if p.is_file()}
    counts["photos"] = len(photos)
    manifest = {"format": FORMAT, "created_at": _now_local(now).isoformat(timespec="seconds"), "app_version": app_version,
                "counts": counts, "photos_bytes": sum(p.size for p in photos), "files": files,
                "db_photos_missing": len(missing), "db_photos_missing_examples": missing[:10]}
    (work / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return manifest, photos


def _put_photo(store: Path, photo: Photo) -> bool:
    """Кладёт фото в хранилище, если его там ещё нет. True — записано новое."""
    target = store_path(store, photo.sha256)
    try:
        if target.is_file() and target.stat().st_size == photo.size:
            return False
    except OSError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    shutil.copyfile(photo.source, tmp)
    if sha256_file(tmp) != photo.sha256:
        tmp.unlink(missing_ok=True)
        raise BackupError(f"Фото {photo.rel} изменилось во время копии — повторю позже.")
    os.replace(tmp, target)
    return True


def publish(dest: Destination, work: Path, manifest: dict, photos: list[Photo], name: str) -> Path:
    """Переносит собранную копию в папку назначения: сначала новые фото, затем папка копии (через «.part»), затем уборка."""
    dest.root.mkdir(parents=True, exist_ok=True)
    store = dest.root / PHOTOS_DIR
    new_bytes = sum(p.size for p in photos if not store_path(store, p.sha256).is_file())
    work_bytes = sum(p.stat().st_size for p in work.rglob("*") if p.is_file())
    free = _free_bytes(dest.root)
    if free is not None and free < new_bytes + work_bytes + SPARE_BYTES:
        raise BackupError(f"Мало места: нужно около {(new_bytes + work_bytes + SPARE_BYTES) // 2**20} МБ, "
                          f"свободно {free // 2**20} МБ.")
    for photo in photos:
        _put_photo(store, photo)
    final = dest.root / name
    part = dest.root / (name + PART_SUFFIX)
    if part.exists():
        shutil.rmtree(part)
    shutil.copytree(work, part)
    for rel, sha in manifest["files"].items():
        if sha256_file(part / rel) != sha:
            shutil.rmtree(part, ignore_errors=True)
            raise BackupError(f"Файл {rel} записался с ошибкой — копия не сохранена.")
    os.replace(part, final)
    prune(dest)
    return final


def list_copies(folder: Path) -> list[CopyInfo]:
    """Готовые копии в папке, новые первыми. Папки без manifest.json и «.part» не считаются."""
    out = []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return out
    for d in entries:
        if not (d.is_dir() and _COPY_RE.match(d.name)):
            continue
        try:
            m = json.loads((d / MANIFEST).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append(CopyInfo(d, d.name, str(m.get("created_at", "")), dict(m.get("counts") or {})))
    out.sort(key=lambda c: c.name, reverse=True)
    return out


def prune(dest: Destination, *, now: float | None = None) -> dict:
    """Оставляет dest.keep новых копий; удаляет фото, на которые никто не ссылается, и брошенные «.part» старше суток."""
    copies = list_copies(dest.root)
    removed_copies = 0
    for old in copies[dest.keep:]:
        shutil.rmtree(old.path, ignore_errors=True)
        removed_copies += 1
    now = time.time() if now is None else now
    for d in dest.root.iterdir():
        if d.is_dir() and d.name.endswith(PART_SUFFIX) and _COPY_RE.match(d.name[:-len(PART_SUFFIX)]):
            try:
                if now - d.stat().st_mtime > 86400:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass
    kept = copies[:dest.keep]
    referenced: set[str] = set()
    for c in kept:
        try:
            referenced.update(p.sha256 for p in read_photos_list(c.path / PHOTOS_LIST))
        except (OSError, BackupError, ValueError):
            return {"copies": removed_copies, "photos": 0}      # список не читается — фото не трогаем вовсе
    removed_photos = 0
    store = dest.root / PHOTOS_DIR
    if store.is_dir():
        for sub in store.iterdir():
            if not (sub.is_dir() and re.fullmatch(r"[0-9a-f]{2}", sub.name)):
                continue
            for f in sub.iterdir():
                if f.is_file() and _SHA_RE.match(f.name) and f.name not in referenced:
                    f.unlink(missing_ok=True)
                    removed_photos += 1
    return {"copies": removed_copies, "photos": removed_photos}


def verify_copy(copy_dir: Path, *, full: bool = False) -> list[str]:
    """Проблемы копии (пусто — всё в порядке). full — сверить SHA-256 каждого фото (читает все фото: в облаке — скачивает)."""
    problems = []
    try:
        manifest = json.loads((copy_dir / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ["нет или не читается manifest.json"]
    fmt = manifest.get("format")
    if fmt not in FORMATS:
        problems.append(f"неизвестный формат копии: {fmt}")
    for rel, sha in (manifest.get("files") or {}).items():
        p = copy_dir / rel
        if not p.is_file():
            problems.append(f"нет файла {rel}")
        elif sha256_file(p) != sha:
            problems.append(f"файл {rel} повреждён")
    store = copy_dir.parent / PHOTOS_DIR
    try:
        photos = read_photos_list(copy_dir / PHOTOS_LIST)
    except (OSError, BackupError, ValueError) as exc:
        return problems + [f"список фото не читается ({exc})"]
    for p in photos:
        if fmt in FORMATS:
            try:
                photo_target(Path("."), p, fmt)
            except BackupError:
                problems.append(f"фото {p.rel} — вне папок фото")
                continue
        target = store_path(store, p.sha256)
        try:
            if not target.is_file() or target.stat().st_size != p.size:
                problems.append(f"нет фото {p.rel}")
            elif full and sha256_file(target) != p.sha256:
                problems.append(f"фото {p.rel} повреждено")
        except OSError:
            problems.append(f"фото {p.rel} не читается")
    return problems


def create_backup(*, root: Path, media_root: Path, db_file: Path, keys: dict[str, str], dests: list[Destination],
                  now: datetime | None = None, app_version: str = "") -> dict:
    """Одна копия во все места. Итог: {"name", "counts", "destinations": [{kind, path, ok, error}], "ok"}."""
    if not dests:
        raise BackupError("Нет папки для копий.")
    name = copy_name(now)
    work = dests[0].root / (".work-" + name)
    results = []
    try:
        if work.exists():
            shutil.rmtree(work)
        manifest, photos = build_copy(work, db_file=db_file, media_root=media_root, keys=keys, now=now,
                                      app_version=app_version)
        for dest in dests:
            entry = {"kind": dest.kind, "path": str(dest.root), "ok": False, "error": ""}
            try:
                publish(dest, work, manifest, photos, name)
                entry["ok"] = True
            except (OSError, BackupError) as exc:
                entry["error"] = _safe_error(exc)
                logger.warning("Копия в %s (%s) не удалась: %s", dest.root, dest.kind, entry["error"])
            results.append(entry)
        return {"name": name, "counts": manifest["counts"], "destinations": results,
                "ok": bool(results) and results[0]["ok"] and all(r["ok"] for r in results)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _safe_error(exc: BaseException) -> str:
    if isinstance(exc, BackupError):
        return str(exc)[:300]
    if isinstance(exc, PermissionError):
        return "нет доступа к папке (её занял антивирус или другая программа)"
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return "закончилось место на диске"
    return f"{type(exc).__name__}: {str(exc)[:200]}"


# ----------------------------------------------------------------------------------------------------------- восстановление

def read_keys(copy_dir: Path) -> dict[str, str]:
    keys = {}
    try:
        text = (copy_dir / KEYS_FILE).read_text(encoding="utf-8")
    except OSError:
        return keys
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() in KEY_NAMES and value.strip():
            keys[name.strip()] = value.strip()
    return keys


def apply_keys(env_text: str, keys: dict[str, str]) -> tuple[str, list[str]]:
    """Вписать ключи из копии в текст .env (строки с другими значениями заменяются). -> (текст, какие ключи изменены)."""
    lines = env_text.splitlines()
    changed = []
    for name, value in keys.items():
        idx = next((i for i, ln in enumerate(lines) if ln.strip().startswith(name + "=")), None)
        if idx is None:
            lines.append(f"{name}={value}")
            changed.append(name)
        elif lines[idx].split("=", 1)[1].strip() != value:
            lines[idx] = f"{name}={value}"
            changed.append(name)
    return ("\n".join(lines) + "\n" if lines else ""), changed


def restore_copy(copy_dir: Path, *, media_root: Path, db_name: str = "shop.db", env_file: Path | None = None,
                 now: datetime | None = None) -> dict:
    """Вернуть данные из копии в media_root. Прежняя папка не удаляется — переименовывается в «<имя>.bak-<время>».

    Сначала копия полностью проверяется (SHA-256 всех файлов и фото), собирается рядом в «<имя>.restore-part», и только потом
    папки меняются местами. Программа должна быть закрыта: иначе Windows не даст переименовать папку с открытой базой.
    """
    problems = verify_copy(copy_dir, full=True)
    if problems:
        raise BackupError("Копия неполная или повреждена: " + "; ".join(problems[:5]) +
                          ("…" if len(problems) > 5 else "") + " Выберите другую копию.")
    stamp = _now_local(now).strftime("%Y%m%d-%H%M%S")
    part = media_root.with_name(media_root.name + ".restore-part")
    if part.exists():
        shutil.rmtree(part)
    part.mkdir(parents=True)
    try:
        gunzip(copy_dir / DB_FILE, part / db_name)
        counts = check_db_file(part / db_name)
        if (copy_dir / INDEX_DIR).is_dir():
            shutil.copytree(copy_dir / INDEX_DIR, part / INDEX_DIR)
        store = copy_dir.parent / PHOTOS_DIR
        photos = read_photos_list(copy_dir / PHOTOS_LIST)
        fmt = copy_format(copy_dir)
        for p in photos:
            target = photo_target(part, p, fmt)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(store_path(store, p.sha256), target)
        (part / UPLOADS_DIR).mkdir(exist_ok=True)
        missing = db_photos_missing(part / db_name, photos if fmt != 1 else
                                    [Photo(p.sha256, p.size, f"{UPLOADS_DIR}/{p.rel}") for p in photos])
    except BaseException:
        shutil.rmtree(part, ignore_errors=True)
        raise
    backup_of_old = None
    if media_root.exists():
        backup_of_old = media_root.with_name(f"{media_root.name}.bak-{stamp}")
        try:
            os.rename(media_root, backup_of_old)
        except OSError as exc:
            shutil.rmtree(part, ignore_errors=True)
            raise BackupError("Не получилось убрать текущие данные: программа «Узнать товар» ещё работает. "
                              "Закройте её окно и повторите.") from exc
    os.rename(part, media_root)
    changed_keys: list[str] = []
    keys = read_keys(copy_dir)
    old = env_file.read_text(encoding="utf-8") if env_file is not None and env_file.is_file() else ""
    old_auth = next((ln.split("=", 1)[1].strip() for ln in old.splitlines() if ln.strip().startswith("AUTH_SECRET_KEY=")), "")
    if env_file is not None and keys:
        text, changed_keys = apply_keys(old, keys)
        if changed_keys:
            if old:
                shutil.copyfile(env_file, env_file.with_name(f"{env_file.name}.bak-{stamp}"))
            env_file.write_text(text, encoding="utf-8")
    _carry_settings(backup_of_old, media_root, old_auth, keys.get("AUTH_SECRET_KEY") or old_auth)
    counts["photos"] = len(photos)
    return {"counts": counts, "old_data": str(backup_of_old) if backup_of_old else "", "keys_changed": changed_keys,
            "db_photos_missing": missing}


def _carry_gemini(old_media: Path, new_media: Path, old_key: str, new_key: str) -> None:
    """Ключ Gemini (app/services/gemini.py) тоже переходит в восстановленные данные; AUTH_SECRET_KEY из копии — перешифровать."""
    from app.services import gemini
    data = read_json(old_media / gemini.SETTINGS_FILE)
    if not data.get("key") or (new_media / gemini.SETTINGS_FILE).exists():
        return
    if old_key and new_key and old_key != new_key:
        from app.core.auth_secrets import decrypt_text, encrypt_text
        try:
            data["key"] = encrypt_text(decrypt_text(data["key"], old_key, gemini.KEY_LABEL), new_key, gemini.KEY_LABEL)
        except Exception:  # noqa: BLE001 — не расшифровался: ключ владелец введёт заново
            return
    write_json(new_media / gemini.SETTINGS_FILE, data)


def _carry_settings(old_media: Path | None, new_media: Path, old_key: str, new_key: str) -> None:
    """Выбор папки и подключение Google Диска не входят в копию — они переходят из прежних данных в восстановленные.
    Ключ Google перешифровывается, если AUTH_SECRET_KEY взят из копии."""
    if old_media is None:
        return
    _carry_gemini(old_media, new_media, old_key, new_key)
    data = read_settings(old_media)
    if not data:
        return
    if isinstance(data.get("google"), dict) and old_key and new_key:
        from app.services.gdrive_backup import rekey_token
        data["google"] = rekey_token(data["google"], old_key, new_key)
    update_settings(new_media, lambda current: {**data, **current})


# ----------------------------------------------------------------------------------------------------------- расписание

def read_state(media_root: Path) -> dict:
    return read_json(media_root / STATE_FILE)


def write_state(media_root: Path, state: dict) -> None:
    write_json(media_root / STATE_FILE, state)


def _parse(iso: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None


def due(state: dict, now: datetime) -> bool:
    """Пора ли делать копию: удачной не было сутки, а после неудачной попытки прошёл час."""
    last_ok = _parse(state.get("last_ok"))
    last_attempt = _parse(state.get("last_attempt"))
    if last_attempt is not None and now - last_attempt < RETRY_AFTER and (last_ok is None or last_ok < last_attempt):
        return False
    return last_ok is None or now - last_ok >= INTERVAL


class BackupService:
    """Ежедневная копия в фоне (только облегчённый режим) и «Сделать копию сейчас». Одновременно — одна копия."""

    def __init__(self, *, root: Path, media_root: Path, db_file: Path, keys: dict[str, str],
                 dests_fn=None, clock=None, app_version: str = "", google=None):
        self.root = root
        self.media_root = media_root
        self.db_file = db_file
        self.keys = keys
        self.app_version = app_version
        self.google = google              # app/services/gdrive_backup.GoogleBackup или None
        if google is not None:
            google.on_connected = self.start_cloud      # подключили — последняя копия уходит в облако сразу
        self._dests_fn = dests_fn or (lambda: destinations(root, chosen=str(read_settings(media_root).get("local_dir") or "")))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._running = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at = time.monotonic()

    @property
    def running(self) -> bool:
        return self._running

    def run_once(self) -> dict:
        """Сделать копию сейчас (в этом потоке). Если копия уже идёт — BackupError."""
        if not self._lock.acquire(blocking=False):
            raise BackupError("Копия уже делается.")
        self._running = True
        now = self._clock()
        state = read_state(self.media_root)
        state["last_attempt"] = now.isoformat(timespec="seconds")
        try:
            write_state(self.media_root, state)
            report = create_backup(root=self.root, media_root=self.media_root, db_file=self.db_file, keys=self.keys,
                                   dests=self._dests_fn(), now=now, app_version=self.app_version)
            local_ok = bool(report["destinations"]) and report["destinations"][0]["ok"]
            if local_ok:
                state["last_ok"] = now.isoformat(timespec="seconds")
            state["last_error"] = "" if report["ok"] else next((d["error"] for d in report["destinations"] if d["error"]), "")
            state["last"] = report
            if local_ok:
                self.start_cloud()         # Google Диск — отдельно, в фоне: копия на компьютере уже готова
            return report
        except (OSError, BackupError, sqlite3.Error) as exc:
            state["last_error"] = _safe_error(exc)
            state["last"] = {"name": "", "counts": {}, "destinations": [], "ok": False}
            logger.warning("Резервная копия не сделана: %s", state["last_error"])
            raise BackupError(state["last_error"]) from exc
        finally:
            try:
                write_state(self.media_root, state)
            finally:
                self._running = False
                self._lock.release()

    def start_now(self) -> bool:
        """Копия в отдельном потоке. False — копия уже идёт."""
        if self._running or self._lock.locked():
            return False
        threading.Thread(target=self._run_quiet, name="backup-now", daemon=True).start()
        return True

    def _run_quiet(self) -> None:
        try:
            self.run_once()
        except BackupError:
            pass
        except Exception:  # noqa: BLE001 — фоновая копия не должна ронять программу
            logger.exception("Сбой резервной копии")

    def tick(self) -> bool:
        """Проверка по расписанию: сделала ли копию. Копия есть, а в Google Диск ещё не ушла (не было связи) — отправить."""
        if time.monotonic() - self._started_at < FIRST_DELAY_SECONDS:
            return False
        if not due(read_state(self.media_root), self._clock()):
            self.start_cloud()
            return False
        self._run_quiet()
        return True

    # ----- Google Диск (app/services/gdrive_backup.py)
    def newest_local(self) -> tuple[Path, str] | None:
        local = self._dests_fn()[0]
        copies = list_copies(local.root)
        return (local.root, copies[0].name) if copies else None

    def start_cloud(self, *, wait: bool = False) -> bool:
        """Отправить новейшую копию с компьютера в Google Диск, если пора (в отдельном потоке; wait — в этом)."""
        g = self.google
        if g is None or g.running:
            return False
        newest = self.newest_local()
        if newest is None or not g.due(newest[1]):
            return False
        if wait:
            self._cloud_quiet(*newest)
        else:
            threading.Thread(target=self._cloud_quiet, args=newest, name="backup-gdrive", daemon=True).start()
        return True

    def _cloud_quiet(self, local_root: Path, name: str) -> None:
        from app.services.gdrive_backup import CloudError
        try:
            self.google.sync(local_root, name, keep=KEEP_CLOUD)
        except CloudError as exc:
            logger.warning("Копия в Google Диск не отправлена: %s", exc)
        except Exception:  # noqa: BLE001 — фоновая отправка не должна ронять программу
            logger.exception("Сбой отправки копии в Google Диск")

    # ----- своя папка
    def set_local_dir(self, raw: str) -> str:
        """Папка для копий из «Настроек». Пусто — стандартная (backups/magazin). -> сохранённый путь."""
        text = (raw or "").strip().strip('"').strip()
        if text:
            path = check_folder(text, media_root=self.media_root,
                                need_bytes=shop_bytes(self.media_root, self.db_file))
            text = str(path)

        def change(data: dict) -> dict:
            if text:
                data["local_dir"] = text
            else:
                data.pop("local_dir", None)
            return data
        update_settings(self.media_root, change)
        return text

    def _loop(self) -> None:
        while not self._stop.wait(CHECK_SECONDS):
            self.tick()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="backup-daily", daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def status(self) -> dict:
        state = read_state(self.media_root)
        dests = []
        places = self._dests_fn()
        for d in places:
            copies = list_copies(d.root)
            dests.append({"kind": d.kind, "path": str(d.root), "copies": len(copies),
                          "newest": copies[0].created_at if copies else ""})
        last = state.get("last") or {}
        last_ok = _parse(state.get("last_ok"))
        google = self.google.status() if self.google is not None else {"state": "unavailable"}
        chosen = str(read_settings(self.media_root).get("local_dir") or "")
        local = places[0]
        free = _free_bytes(local.root)
        return {"available": True, "running": self._running, "last_ok": state.get("last_ok") or "",
                "last_attempt": state.get("last_attempt") or "", "last_error": state.get("last_error") or "",
                "next_due": (last_ok + INTERVAL).isoformat(timespec="seconds") if last_ok else "",
                "cloud": any(d["kind"] == "cloud" for d in dests) or google.get("state") == "connected",
                "destinations": dests,
                "last_destinations": last.get("destinations") or [], "counts": last.get("counts") or {},
                "local": {"chosen": chosen, "path": str(local.root), "fallback_from": local.fallback_from,
                          "free": free, "need": shop_bytes(self.media_root, self.db_file)},
                "drives": list_drives(), "google": google}


_service: BackupService | None = None


def service_from_settings(settings) -> BackupService | None:
    """Служба копий для облегчённого режима (одна на процесс); в полном режиме — None."""
    global _service
    if not settings.lite_mode:
        return None
    if _service is None:
        from app.core.config import PROJECT_ROOT, resolve_project_path
        from app.services.gdrive_backup import GoogleBackup, load_client
        root = PROJECT_ROOT
        media_root = resolve_project_path(settings.media_root)
        _service = BackupService(root=root, media_root=media_root,
                                 db_file=sqlite_path(settings.database_url),
                                 keys={"AUTH_SECRET_KEY": settings.auth_secret_key or "",
                                       "CAMERA_SECRET_KEY": settings.camera_secret_key or ""},
                                 dests_fn=lambda: destinations_from_settings(root, settings, media_root),
                                 app_version=os.environ.get("APP_VERSION", ""),
                                 google=GoogleBackup(media_root=media_root, client=load_client(root, settings),
                                                     secret_key=settings.auth_secret_key or ""))
    return _service
