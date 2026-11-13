"""
Безопасная очистка временных файлов, оставленных оборванной записью (2026-11-07).

Что считается временным (строгие шаблоны; всё остальное не трогается никогда):
  - рядом с индексом: «<файл индекса>.tmp-<pid>-<8 hex>» (app/pipeline/index_store.tmp_name) — для catalog.faiss,
    catalog.faiss.ids.json и catalog.faiss.npz;
  - журналы рядом с индексом и фото товаров: «<имя>.<12 hex>.part» (catalog_upload._atomic_write), а также прежние имена
    «<имя>.part» и «import_manifest.json.tmp» (версии до 2026-11-07).

Правила «не удалить то, чем пользуется работающий процесс»:
  1. Временные файлы индекса удаляются только под блокировкой индекса (каждая запись индекса держит её всё время записи —
     значит, под блокировкой ни один из этих файлов не пишется) и только после того, как индекс успешно загружен (загрузка
     сама завершает прерванную запись по временному списку id) и пара «индекс + список id» под блокировкой согласована.
     Если блокировка не поддерживается файловой системой или индекс не загружается — ничего из этого не удаляется.
  2. Журналы и фото пишутся без общей блокировки, поэтому их временные файлы удаляются только старше заданного возраста
     (по умолчанию час; сама запись длится доли секунды). Для файлов индекса тоже действует возраст (по умолчанию 10 минут).
  3. Только обычные файлы (не ссылки, не папки), только в папке индекса и на один уровень в папках товаров.
  4. Каждое действие пишется в журнал; после удаления проверяется: файл исчез, фото товаров (не временные) на месте,
     индекс загружается с теми же векторами.
"""
from __future__ import annotations

import json
import logging
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

INDEX_MIN_AGE_SECONDS = 600
OTHER_MIN_AGE_SECONDS = 3600
_INDEX_TMP = re.compile(r"^(?P<base>.+)\.tmp-\d+-[0-9a-f]{8}$")
_ATOMIC_PART = re.compile(r"^(?P<base>.+)\.[0-9a-f]{12}\.part$")
_LEGACY_PART = re.compile(r"^(?P<base>\d{3}-[0-9a-f]{12}\.jpg|uploads_manifest\.json)\.part$")
_LEGACY_IMPORT_TMP = "import_manifest.json.tmp"


@dataclass
class TempFile:
    path: Path
    kind: str                     # "index" | "other"
    age_seconds: float
    size: int


@dataclass
class CleanupResult:
    candidates: list[TempFile] = field(default_factory=list)
    deleted: list[Path] = field(default_factory=list)
    kept: list[tuple[Path, str]] = field(default_factory=list)        # (файл, почему оставлен)
    errors: list[str] = field(default_factory=list)
    verified: bool = True


def _regular_file(path: Path):
    try:
        st = path.lstat()
    except FileNotFoundError:
        return None
    return st if stat.S_ISREG(st.st_mode) else None


def _index_names(index_path: str) -> set[str]:
    name = Path(index_path).name
    npz = name if name.endswith(".npz") else f"{name}.npz"
    return {name, f"{name}.ids.json", npz}


def find_temp_files(index_path: str, uploads_root: Path | None, now: float | None = None) -> list[TempFile]:
    now = time.time() if now is None else now
    found: list[TempFile] = []
    index_dir = Path(index_path).parent
    names = _index_names(index_path)

    def consider(path: Path, kind: str):
        st = _regular_file(path)
        if st is not None:
            found.append(TempFile(path, kind, max(0.0, now - st.st_mtime), st.st_size))

    if index_dir.is_dir():
        for p in sorted(index_dir.iterdir()):
            m = _INDEX_TMP.match(p.name)
            if m and m.group("base") in names:
                consider(p, "index")
            elif _ATOMIC_PART.match(p.name) or _LEGACY_PART.match(p.name) or p.name == _LEGACY_IMPORT_TMP:
                consider(p, "other")
    if uploads_root is not None and Path(uploads_root).is_dir():
        for folder in sorted(Path(uploads_root).iterdir()):
            if folder.is_symlink() or not folder.is_dir():
                continue
            for p in sorted(folder.iterdir()):
                if _ATOMIC_PART.match(p.name) or _LEGACY_PART.match(p.name):
                    consider(p, "other")
    return found


def _photos_listing(uploads_root: Path | None) -> dict[str, int]:
    if uploads_root is None or not Path(uploads_root).is_dir():
        return {}
    out = {}
    for p in Path(uploads_root).rglob("*"):
        st = _regular_file(p)
        if st is not None and not (_ATOMIC_PART.match(p.name) or _LEGACY_PART.match(p.name)):
            out[str(p)] = st.st_size
    return out


def _pair_consistent(index_path: str) -> bool:
    """Под блокировкой: список id формата 2 указывает на ТЕКУЩИЙ файл индекса (иначе временный список может быть нужен для
    завершения прерванной записи). numpy-индекс — один файл, проверять нечего."""
    from app.pipeline.matcher import _sha256_file  # noqa: PLC0415
    final, sidecar = Path(index_path), Path(f"{index_path}.ids.json")
    if not final.is_file() or not sidecar.is_file():
        return not final.is_file() and not sidecar.is_file()
    try:
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if isinstance(meta, list):                     # старый формат: контрольной суммы нет — временные списки не трогаем
        return False
    return isinstance(meta, dict) and meta.get("index_sha256") == _sha256_file(final)


def cleanup(index_path: str, embedding_dim: int, uploads_root: Path | None, *, apply: bool = False,
            index_min_age: float = INDEX_MIN_AGE_SECONDS, other_min_age: float = OTHER_MIN_AGE_SECONDS,
            now: float | None = None, log: Callable[[str], None] = logger.info) -> CleanupResult:
    from app.pipeline.index_store import IndexBusyError, IndexLock  # noqa: PLC0415
    from app.pipeline.matcher import lock_target, open_catalog_index  # noqa: PLC0415

    res = CleanupResult(candidates=find_temp_files(index_path, uploads_root, now))
    photos_before = _photos_listing(uploads_root)
    eligible: list[TempFile] = []
    for tf in res.candidates:
        limit = index_min_age if tf.kind == "index" else other_min_age
        if tf.age_seconds < limit:
            res.kept.append((tf.path, f"моложе {limit:.0f} с — возможно, ещё пишется"))
        else:
            eligible.append(tf)

    index_files = [tf for tf in eligible if tf.kind == "index"]
    others = [tf for tf in eligible if tf.kind == "other"]
    ids_before = None
    if index_files:
        try:
            loaded = open_catalog_index(index_path, embedding_dim)      # завершает прерванную запись, если нужно
            ids_before = list(loaded._ids)
        except Exception as exc:  # noqa: BLE001
            why = ("индекс занят другим процессом — повторите позже" if isinstance(exc, IndexBusyError) else
                   f"индекс не загружается ({type(exc).__name__}) — файл может понадобиться для восстановления")
            for tf in index_files:
                res.kept.append((tf.path, why))
            index_files = []

    def remove(tf: TempFile):
        if not apply:
            log(f"[проверка] будет удалён: {tf.path} ({tf.size} байт, возраст {tf.age_seconds:.0f} с)")
            return
        try:
            tf.path.unlink()
        except FileNotFoundError:
            log(f"уже удалён другим процессом: {tf.path}")
            return
        except OSError as exc:
            res.errors.append(f"{tf.path}: {exc}")
            log(f"НЕ удалён: {tf.path}: {exc}")
            return
        res.deleted.append(tf.path)
        log(f"удалён: {tf.path} ({tf.size} байт, возраст {tf.age_seconds:.0f} с)")

    lock_cm = None
    if index_files:
        try:
            lock_cm = IndexLock(lock_target(index_path)).__enter__()
        except IndexBusyError:
            for tf in index_files:
                res.kept.append((tf.path, "индекс занят другим процессом — повторите позже"))
            index_files = []
    if index_files and lock_cm is not None:
        lock = lock_cm
        try:
            if not lock.locked:
                for tf in index_files:
                    res.kept.append((tf.path, "файловая система не поддерживает блокировку — нельзя исключить запись в этот момент"))
            elif not _pair_consistent(index_path):
                for tf in index_files:
                    res.kept.append((tf.path, "индекс и список id не согласованы — файл может понадобиться для восстановления"))
            else:
                for tf in index_files:
                    remove(tf)
        finally:
            lock.__exit__(None, None, None)
    for tf in others:
        remove(tf)

    if apply:                                                     # проверка результата
        for p in res.deleted:
            if _regular_file(p) is not None:
                res.verified = False
                res.errors.append(f"файл всё ещё на месте: {p}")
        if _photos_listing(uploads_root) != photos_before:
            res.verified = False
            res.errors.append("список фото товаров изменился во время очистки — проверьте вручную")
        if ids_before is not None:
            try:
                after = open_catalog_index(index_path, embedding_dim)
                if after._ids[:len(ids_before)] != ids_before:        # мог дописать воркер — но не потерять
                    res.verified = False
                    res.errors.append("индекс после очистки не содержит прежних векторов")
            except Exception as exc:  # noqa: BLE001
                res.verified = False
                res.errors.append(f"индекс после очистки не загружается: {exc}")
    for p, why in res.kept:
        log(f"оставлен: {p} — {why}")
    return res
