"""
Сверка индекса визуального поиска с базой данных и безопасное восстановление (2026-11-07).

База — единственный источник истины о товарах. Индекс — производные данные: векторы эталонных фото с id товара. После сбоев
(или в данных, записанных версиями до 2026-11-07) возможны расхождения:
  - «призраки» — векторы с id, которого нет в базе (товар удалён; старая версия сохраняла индекс до commit базы).
    Неверного ответа они не дают (распознавание проверяет базу), но занимают место и время поиска;
  - товары без векторов — есть в базе, но по виду не распознаются (сбой после commit, потеря индекса). Если их фото лежат
    в data/catalog-uploads, векторы добавит scripts/reindex_uploads.py;
  - устаревшие отметки журнала uploads_manifest.json — фото отмечено проиндексированным, а у товара нет ни одного вектора
    (индекс заменён, восстановлен из старой копии, пересобран). Из-за такой отметки reindex_uploads пропускал бы фото.

Восстановление НЕ удаляет ни фото, ни товары, ни векторы товаров, которые есть в базе:
  1. копия текущих файлов индекса и журнала — в папку backup-<время> рядом с индексом (проверка SHA-256 копий);
  2. новый индекс = те же векторы без «призраков» и без повторов одного фото (байт в байт, с источниками, в прежнем порядке;
     модель не нужна). НЕДОСТАЮЩИЕ векторы этот шаг НЕ добавляет — для этого нужна модель: задача воркера recover_index
     (при каждом его запуске) или scripts/reindex_uploads.py --apply;
  3. сохранение с проверкой поколения: если индекс за это время записал другой процесс — отказ, ничего не изменено;
  4. проверка: индекс перечитан, id и векторы совпадают с ожидаемыми;
  5. из журнала снимаются отметки только у товаров без единого вектора (затем reindex_uploads добавит их фото).
Отказ без изменений: база пуста при непустом индексе; «призраков» больше заданной доли (вероятно, индекс от другой базы или
построен с артикулами вместо id — ml/build_catalog_embeddings.py по папкам с именами артикулов).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

import numpy as np

from app.pipeline import matcher as M
from app.pipeline.index_store import IndexLock, fsync_dir

MAX_GHOST_SHARE = 0.2


@dataclass
class ReconcileReport:
    index_vectors: int
    index_products: int
    db_products: int
    ghosts: dict[str, int]                                   # id -> число векторов (id нет в базе)
    without_vectors: list[tuple[str, str]]                   # (id, артикул) товаров базы без векторов
    recoverable: list[tuple[str, str, int]]                  # из них — с фото на диске: (id, артикул, число фото)
    stale_journal_keys: list[str]                            # отметки журнала у товаров без векторов
    blockers: list[str] = field(default_factory=list)        # почему восстановление не выполняется
    duplicate_vectors: int = 0                               # лишние векторы одного и того же фото (повторное индексирование)

    @property
    def ghost_vectors(self) -> int:
        return sum(self.ghosts.values())

    @property
    def clean(self) -> bool:
        return not self.ghosts and not self.without_vectors and not self.stale_journal_keys and not self.duplicate_vectors


def analyze(index, products: Mapping[str, str], *, photo_counts: Mapping[str, int] | None = None,
            journal_keys: set[str] | None = None, folder_of=None, max_ghost_share: float = MAX_GHOST_SHARE) -> ReconcileReport:
    """products: {id товара: артикул} из базы. photo_counts: {артикул: число фото в data/catalog-uploads}. journal_keys —
    отметки uploads_manifest.json («папка/файл»); folder_of(артикул) -> имя папки (catalog_upload.folder_name)."""
    photo_counts = photo_counts or {}
    journal_keys = journal_keys or set()
    counts = Counter(index._ids)
    ghosts = {pid: n for pid, n in counts.items() if pid not in products}
    without = sorted((pid, sku) for pid, sku in products.items() if counts.get(pid, 0) == 0)
    recoverable = [(pid, sku, photo_counts.get(sku, 0)) for pid, sku in without if photo_counts.get(sku, 0) > 0]
    stale: list[str] = []
    if folder_of is not None and journal_keys:
        folders = {folder_of(sku) for _pid, sku in without}
        stale = sorted(k for k in journal_keys if k.split("/", 1)[0] in folders)
    report = ReconcileReport(len(index._ids), len(counts), len(products), ghosts, without, recoverable, stale)
    report.duplicate_vectors = len(_duplicate_rows(index, set(products)))
    total = report.index_vectors
    if total and not products:
        report.blockers.append("в базе нет ни одного товара, а индекс не пуст — похоже, подключена не та база; ничего не изменено")
    elif total and report.ghost_vectors / total > max_ghost_share:
        report.blockers.append(
            f"векторов без товара в базе {report.ghost_vectors} из {total} (больше {max_ghost_share:.0%}) — вероятно, индекс "
            f"от другой базы или построен по папкам с артикулами вместо id товаров; ничего не изменено. Проверьте и, если уверены, "
            f"запустите с --max-ghost-share")
    return report


def _vectors_of(index) -> np.ndarray:
    if isinstance(index, M.FaissCatalogIndex):
        n = index._index.ntotal
        return index._index.reconstruct_n(0, n) if n else np.zeros((0, index.dimension), dtype=np.float32)
    return np.stack(index._vectors) if index._vectors else np.zeros((0, 0), dtype=np.float32)


def _duplicate_rows(index, keep: set[str]) -> set[int]:
    """Строки-повторы: вектор того же фото (источника) у того же товара уже есть выше. Векторы без источника не сравниваются."""
    sources = getattr(index, "_sources", None) or [None] * len(index._ids)
    seen: set[tuple[str, str]] = set()
    dup: set[int] = set()
    for row, (pid, source) in enumerate(zip(index._ids, sources, strict=True)):
        if pid not in keep or source is None:
            continue
        if (pid, source) in seen:
            dup.add(row)
        seen.add((pid, source))
    return dup


def _kept_rows(index, keep: set[str]) -> list[int]:
    dup = _duplicate_rows(index, keep)
    return [row for row, pid in enumerate(index._ids) if pid in keep and row not in dup]


def compact(index, keep: set[str]):
    """Новый индекс того же типа: векторы с id из keep, байт в байт и в прежнем порядке. Поколение переносится, чтобы при
    сохранении сработала проверка «файл не менялся с момента загрузки»."""
    vectors = _vectors_of(index)
    fresh = M.FaissCatalogIndex(embedding_dim=index.dimension) if isinstance(index, M.FaissCatalogIndex) else M.NumpyCatalogIndex()
    sources = getattr(index, "_sources", None) or [None] * len(index._ids)
    for row in _kept_rows(index, keep):                     # без «призраков» и без повторов одного фото
        fresh.add(index._ids[row], np.asarray(vectors[row], dtype=np.float32)[None, :], [sources[row]])
    for attr in ("_disk_gen", "_disk_probe", "_disk_sha"):
        if attr in vars(index):
            setattr(fresh, attr, getattr(index, attr))
    return fresh


def _index_files(index_path: str) -> list[Path]:
    base = Path(index_path)
    candidates = [base, Path(f"{index_path}.ids.json"), Path(index_path if index_path.endswith(".npz") else f"{index_path}.npz")]
    return [p for p in dict.fromkeys(candidates) if p.is_file()]


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_files(index_path: str, extra: list[Path] | tuple = ()) -> Path:
    """Копия файлов индекса (и extra, например журнала) в backup-<время> рядом с индексом. Под блокировкой индекса — пара
    «индекс + список id» копируется согласованной. Копии сверяются по SHA-256; не совпали — исключение."""
    base = Path(index_path)
    target = base.parent / f"backup-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    with IndexLock(M.lock_target(index_path)):
        files = _index_files(index_path) + [p for p in extra if Path(p).is_file()]
        target.mkdir(parents=True)
        for src in files:
            dst = target / Path(src).name
            shutil.copy2(src, dst)
            if _sha(Path(src)) != _sha(dst):
                raise OSError(f"копия {dst} не совпала с {src}")
        fsync_dir(target)
    return target


@dataclass
class ApplyResult:
    backup_dir: Path
    removed_vectors: int
    kept_vectors: int
    journal_keys_removed: int
    verified: bool


def apply(index_path: str, embedding_dim: int, products: Mapping[str, str], *, journal_path: Path | None = None,
          folder_of=None, max_ghost_share: float = MAX_GHOST_SHARE, out=print) -> ApplyResult:
    """Восстановление по шагам из описания модуля. Любая ошибка до шага 3 ничего не меняет; ошибка проверки (шаг 4) —
    исключение с адресом копии."""
    index = M.open_catalog_index(index_path, embedding_dim)            # строгая загрузка + поколение
    journal_keys = _load_journal_keys(journal_path)
    report = analyze(index, products, journal_keys=journal_keys, folder_of=folder_of, max_ghost_share=max_ghost_share)
    if report.blockers:
        raise RuntimeError("; ".join(report.blockers))
    backup = backup_files(index_path, [journal_path] if journal_path else [])
    out(f"Копия файлов индекса и журнала: {backup}")
    keep = set(products)
    rows = _kept_rows(index, keep)
    expected_ids = [index._ids[r] for r in rows]
    expected_vectors = _vectors_of(index)[rows]
    all_sources = getattr(index, "_sources", None) or [None] * len(index._ids)
    expected_sources = [all_sources[r] for r in rows]
    removed = len(index._ids) - len(rows)
    if removed:
        fresh = compact(index, keep)
        fresh.save(index_path)                                          # IndexChangedError, если файл записал другой процесс
        check = M.open_catalog_index(index_path, embedding_dim)
        got = _vectors_of(check)
        verified = (check._ids == expected_ids and check._sources == expected_sources and got.shape == expected_vectors.shape
                    and np.array_equal(got, expected_vectors))
        if not verified:
            raise RuntimeError(f"проверка после записи не прошла: индекс не совпал с ожидаемым. Прежние файлы — в {backup}")
    else:
        verified = True
    journal_removed = 0
    if journal_path is not None and report.stale_journal_keys:
        journal_removed = _drop_journal_keys(journal_path, set(report.stale_journal_keys))
    return ApplyResult(backup, removed, len(expected_ids), journal_removed, verified)


def _load_journal_keys(journal_path: Path | None) -> set[str]:
    if journal_path is None or not Path(journal_path).is_file():
        return set()
    data = json.loads(Path(journal_path).read_text(encoding="utf-8"))
    return set(data.get("photos", []))


def _drop_journal_keys(journal_path: Path, keys: set[str]) -> int:
    path = Path(journal_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    before = list(data.get("photos", []))
    data["photos"] = sorted(k for k in before if k not in keys)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:12]}.part")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return len(before) - len(data["photos"])


def prepare_full_rebuild(index_path: str, replace_existing: bool, journal_path: Path | None = None) -> Path | None:
    """Перед полной пересборкой (ml/build_catalog_embeddings.py) в рабочий путь индекса. Индекса нет — можно строить (None).
    Есть, а замена не разрешена явно — отказ: пересборка заменяет индекс ЦЕЛИКОМ, и векторы фото, добавленных из браузера,
    пропали бы, а журнал продолжал бы считать их проиндексированными. Замена разрешена — сначала проверенная копия; возвращает
    папку копии."""
    if not _index_files(index_path):
        return None
    if not replace_existing:
        raise SystemExit(
            f"Индекс {index_path} уже есть. Полная пересборка заменит его целиком: векторы фото, добавленных из браузера, пропадут. "
            f"Чтобы построить индекс рядом — передайте --output-index в другой путь; чтобы заменить рабочий — --replace-existing "
            f"(сначала будет сделана копия), затем: python -m scripts.reconcile_index --apply и python -m scripts.reindex_uploads --apply")
    return backup_files(index_path, [journal_path] if journal_path else [])
