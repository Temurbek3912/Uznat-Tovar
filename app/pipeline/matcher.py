"""
Матчинг эмбеддинга товара с каталогом через поиск ближайших соседей.

Реализовано два бэкенда с одинаковым интерфейсом:
  - FaissCatalogIndex   — продакшн (быстрый ANN-поиск, десятки/сотни тысяч SKU);
  - NumpyCatalogIndex   — brute-force поиск на чистом numpy, без внешних
                          зависимостей. Достаточно быстрый для каталогов до
                          ~50k SKU и полностью пригоден для unit-тестов и
                          небольших пилотов без установки faiss.

Это сознательное архитектурное решение: бизнес-логика матчинга (пороги,
статусы matched/uncertain/unknown) не зависит от конкретного ANN-бэкенда.

ПЕРСИСТЕНТНОСТЬ: оба бэкенда умеют save()/load(). FAISS хранит сам индекс через нативный faiss.write_index, id товаров —
отдельным JSON рядом (FAISS не хранит метаданные). Numpy-бэкенд сериализуется в один .npz-файл целиком.

ЦЕЛОСТНОСТЬ (2026-11-07; правила записи на диск — app/pipeline/index_store.py):
  - запись атомарная, под межпроцессной блокировкой, с новым «поколением» при каждом сохранении;
  - JSON рядом с FAISS-индексом хранит поколение, число векторов, размерность и SHA-256 файла индекса. При загрузке файл
    индекса читается ОДИН раз: контрольная сумма считается и индекс разбирается из одних и тех же байтов, поэтому замена файла
    другим процессом посреди загрузки не может склеить векторы одного сохранения с id другого;
  - обрыв между заменой индекса и заменой JSON: новый индекс уже на месте, а новый JSON остался во временном файле. Загрузка
    находит временный JSON с контрольной суммой текущего индекса и завершает замену (а не отказывает и не путает товары);
  - повреждённый индекс никогда не подменяется пустым (open_catalog_index): иначе первое же добавление товара стёрло бы эталоны;
  - процесс не перезаписывает индекс, записанный другим процессом после его загрузки (сравнение поколений);
  - refresh_index_if_changed / resync_index — воркер подхватывает чужие изменения и сбрасывает несохранённые векторы после сбоя;
  - новые .npz пишутся без pickle (id — строковый массив); старый формат с pickle читается только из настоящего .npz.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from app.core.schemas import DetectionStatus
from app.pipeline.index_store import (REBUILD_HINT, IndexBusyError, IndexChangedError, IndexIntegrityError, IndexLock, discard,
                                      file_probe, fsync_dir, fsync_file, new_generation, replace_with_retry, sha256_bytes,
                                      tmp_name)

__all__ = ["CatalogIndex", "FaissCatalogIndex", "IndexBusyError", "IndexChangedError", "IndexIntegrityError", "MatchResult",
           "NumpyCatalogIndex", "ProductMatcher", "decide_match", "index_size", "open_catalog_index", "rank_products",
           "refresh_index_if_changed", "resync_index", "DEFAULT_NAMESPACE", "check_index_model", "index_manifest", "model_mismatch"]

logger = logging.getLogger(__name__)

IDS_FORMAT = 2                                   # формат JSON рядом с FAISS-индексом (1 — просто список id, до 2026-11-07)
UNLOCKED_LOAD_ATTEMPTS = 5                       # без блокировки: повторы чтения, пока параллельная запись не закончит замену
_NO_GUARD = object()                             # индекс создан напрямую (полная пересборка): первое сохранение может перезаписать файл

# Манифест индекса (документ 07, §8 и §10.1): какой моделью построены векторы и для какого каталога. Векторы разных моделей
# несовместимы, даже если размерность одна (DINOv2-base и дообученная версия — обе 768): поиск дал бы правдоподобные, но
# неверные ответы. Поэтому индекс помнит модель, а загрузка с другой моделью — отказ с причиной.
DEFAULT_NAMESPACE = "catalog"                    # пространство имён: один каталог магазина. Несколько каталогов — позже
MODEL_FIELDS = ("name", "weights", "preprocess", "dim", "normalization")


def model_mismatch(stored: dict | None, current: dict | None) -> str | None:
    """Чем модель индекса отличается от загруженной (простыми словами) или None, если совпадает или сравнить не с чем."""
    if not isinstance(stored, dict) or not isinstance(current, dict):
        return None
    labels = {"name": "модель", "weights": "веса", "preprocess": "обработка кадра", "dim": "размерность",
              "normalization": "нормализация"}
    diff = [f"{labels[k]}: в индексе {stored.get(k)!r}, сейчас {current.get(k)!r}"
            for k in MODEL_FIELDS if stored.get(k) != current.get(k)]
    return "; ".join(diff) or None


def index_manifest(index) -> dict:
    """Манифест индекса в памяти: модель, пространство имён, число векторов; verified=False — модель векторов не записана
    (индекс построен до 2026-11-14), совместимость с текущей моделью не проверить."""
    model = getattr(index, "model", None)
    return {"model": model, "namespace": getattr(index, "namespace", None) or DEFAULT_NAMESPACE, "count": index_size(index),
            "verified": model is not None}


def _manifest_fields(index) -> dict:
    return {"model": getattr(index, "model", None), "namespace": getattr(index, "namespace", None) or DEFAULT_NAMESPACE}


def _read_manifest(raw) -> tuple[dict | None, str | None]:
    """(модель, пространство имён) из сохранённого манифеста; неверный формат — повреждение, а не «модели нет»."""
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        raise ValueError("манифест индекса не словарь")
    model, namespace = raw.get("model"), raw.get("namespace")
    if model is not None and not (isinstance(model, dict) and all(k in model for k in MODEL_FIELDS)):
        raise ValueError("в манифесте индекса неполное описание модели")
    if namespace is not None and not isinstance(namespace, str):
        raise ValueError("в манифесте индекса неверное пространство имён")
    return model, namespace


def _npz_path(path: str) -> str:
    return path if path.endswith(".npz") else f"{path}.npz"


def lock_target(index_path: str) -> Path:
    """Файл, рядом с которым берётся блокировка индекса: ОДИН для FAISS и numpy, для записи, копии, сверки и очистки —
    настроенный путь индекса без «.npz» (блокировка — «catalog.faiss.lock»). Раньше numpy-запись брала «catalog.faiss.npz.lock»,
    а копия и очистка — «catalog.faiss.lock»: взаимоисключения между ними не было."""
    return Path(index_path[:-4] if index_path.endswith(".npz") else index_path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _changed_message(final: Path) -> str:
    return (f"Файл индекса {final} изменён другой программой после того, как этот процесс его загрузил (например, импортом "
            f"каталога при работающем воркере). Перезапись стёрла бы чужие изменения, поэтому файл не перезаписан. "
            f"Воркер перечитает индекс и повторит действие сам; скрипт импорта запустите снова.")


@dataclass
class MatchResult:
    product_id: str | None
    score: float
    status: DetectionStatus
    runner_up_id: str | None = None        # ближайший ДРУГОЙ товар (для контекста ревью и оценки)
    runner_up_score: float | None = None


def rank_products(candidates: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Кандидаты индекса (на каждый эталонный вектор своя запись) -> лучший счёт на каждый ТОВАР, по убыванию.
    У товара 5–8 эталонных фото, поэтому первые k векторов запросто принадлежат одному товару — без группировки
    «второго по близости» товара в выдаче могло бы не оказаться."""
    best: dict[str, float] = {}
    for product_id, score in candidates:
        if product_id not in best or score > best[product_id]:
            best[product_id] = score
    return sorted(best.items(), key=lambda kv: kv[1], reverse=True)


def decide_match(ranked: list[tuple[str, float]], match_threshold: float, unknown_threshold: float,
                 margin_threshold: float = 0.0) -> MatchResult:
    """Решение «товар / не уверен / неизвестен» по списку (товар, счёт) от лучшего к худшему.

    Три правила: счёт >= match_threshold -> MATCHED; между unknown_threshold и match_threshold -> UNCERTAIN (в очередь
    ревью); ниже unknown_threshold -> UNKNOWN. Правило ЗАПАСА (margin_threshold > 0): даже при высоком счёте, если второй
    по близости ДРУГОЙ товар отстаёт меньше чем на margin_threshold, результат — UNCERTAIN. Это защита от уверенных ошибок
    на похожих товарах (один бренд, разные вкусы/объёмы): у обоих сходство высокое, и выбор «лучшего из двух» — подбрасывание
    монетки. 0.0 — правило выключено (прежнее поведение). Единственная реализация решения: оценка и подбор порогов
    (ml/evaluate_matching.py) вызывают ровно эту функцию, поэтому подобранные значения соответствуют боевому поведению."""
    if not ranked:
        return MatchResult(product_id=None, score=0.0, status=DetectionStatus.UNKNOWN)
    best_id, best_score = ranked[0]
    runner_id, runner_score = ranked[1] if len(ranked) > 1 else (None, None)
    if best_score >= match_threshold:
        crowded = margin_threshold > 0 and runner_score is not None and best_score - runner_score < margin_threshold
        status = DetectionStatus.UNCERTAIN if crowded else DetectionStatus.MATCHED
        return MatchResult(best_id, best_score, status, runner_id, runner_score)
    if best_score >= unknown_threshold:
        return MatchResult(best_id, best_score, DetectionStatus.UNCERTAIN, runner_id, runner_score)
    return MatchResult(product_id=None, score=best_score, status=DetectionStatus.UNKNOWN)


class CatalogIndex(Protocol):
    def search(self, query: np.ndarray, top_k: int) -> list[tuple[str, float]]:
        """Возвращает [(product_id, cosine_similarity), ...] отсортировано по убыванию score."""
        ...

    def add(self, product_id: str, vectors: np.ndarray, sources: list[str | None] | None = None) -> None:
        """sources — откуда каждый вектор (ссылка на фото: «upload:папка/файл», «import:<sha256>», «file:папка/файл»)."""
        ...

    def save(self, path: str) -> None:
        ...


def _ids_sidecar_path(index_path: str) -> str:
    """Путь к JSON-файлу с id товаров, лежащему рядом с FAISS-индексом."""
    return f"{index_path}.ids.json"


def _faiss_disk_generation(final: Path, sidecar: Path):
    """Поколение FAISS-индекса на диске: None — индекса нет; строка — формат 2; кортеж — старый формат или повреждение
    (любой кортеж отличается от сохранённого ранее поколения, поэтому перезапись в таких случаях запрещена)."""
    if not sidecar.exists():
        return None if not final.exists() else ("no-ids-file", file_probe(final))
    try:
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ("unreadable", file_probe(sidecar))
    if isinstance(meta, dict) and isinstance(meta.get("generation"), str):
        return meta["generation"]
    try:                                                    # старый формат: поколение по содержимому (не по времени и inode)
        return _legacy_generation(sidecar.read_bytes(), _sha256_file(final))
    except OSError:
        return ("unreadable", file_probe(sidecar))


def _legacy_generation(sidecar_bytes: bytes, index_sha: str):
    return ("legacy", sha256_bytes(sidecar_bytes), index_sha)


def _npz_disk_generation(final: Path):
    if not final.exists():
        return None
    try:
        if not zipfile.is_zipfile(final):
            return ("unreadable", file_probe(final))
        with np.load(final, allow_pickle=False) as data:
            if "generation" in data.files:
                return str(data["generation"])
        return ("legacy", _sha256_file(final))
    except (OSError, ValueError, zipfile.BadZipFile, EOFError):
        return ("unreadable", file_probe(final))


def _sources_for(vectors_count: int, sources) -> list[str | None]:
    if sources is None:
        return [None] * vectors_count
    sources = list(sources)
    if len(sources) != vectors_count or not all(s is None or (isinstance(s, str) and s) for s in sources):
        raise ValueError(f"Источников {len(sources)} на {vectors_count} векторов (или источник не строка)")
    return sources


def _check_generation(index, current, final: Path) -> None:
    expected = index._disk_gen
    if expected is not _NO_GUARD and current != expected:
        raise IndexChangedError(_changed_message(final))


class NumpyCatalogIndex:
    """Brute-force cosine-similarity поиск. Векторы должны быть L2-нормализованы."""

    _disk_gen: Any = _NO_GUARD     # поколение файла, которое этот процесс загрузил или записал сам
    _disk_probe: Any = None        # дешёвый признак изменения файла (размер, время, inode) — для refresh_index_if_changed
    model: dict | None = None      # паспорт модели, которой построены векторы (None — не записан: индекс до 2026-11-14)
    namespace: str | None = None

    def __init__(self):
        self._ids: list[str] = []
        self._vectors: list[np.ndarray] = []
        self._sources: list[str | None] = []

    def add(self, product_id: str, vectors: np.ndarray, sources: list[str | None] | None = None) -> None:
        rows = np.atleast_2d(vectors)
        if self._vectors and rows.shape[-1] != self._vectors[0].shape[-1]:
            raise ValueError(f"Размерность векторов {rows.shape[-1]} не совпадает с индексом ({self._vectors[0].shape[-1]})")
        src = _sources_for(len(rows), sources)
        for vec, source in zip(rows, src, strict=True):
            self._ids.append(product_id)
            self._vectors.append(vec)
            self._sources.append(source)

    def search(self, query: np.ndarray, top_k: int = 5) -> list[tuple[str, float]]:
        if not self._vectors:
            return []
        matrix = np.stack(self._vectors)          # (N, dim)
        sims = matrix @ query                      # cosine similarity, т.к. векторы нормализованы
        top_k = min(top_k, len(sims))
        top_idx = np.argpartition(-sims, top_k - 1)[:top_k]
        top_idx = top_idx[np.argsort(-sims[top_idx])]
        return [(self._ids[i], float(sims[i])) for i in top_idx]

    def search_sources(self, query: np.ndarray, top_k: int = 5) -> list[tuple[str, float, str | None]]:
        """Как search, но с источником каждого вектора — чтобы распознавание пропускало выведенные из эталонов фото."""
        if not self._vectors:
            return []
        sims = np.stack(self._vectors) @ query
        top_k = min(top_k, len(sims))
        top_idx = np.argpartition(-sims, top_k - 1)[:top_k]
        top_idx = top_idx[np.argsort(-sims[top_idx])]
        return [(self._ids[i], float(sims[i]), self._sources[i]) for i in top_idx]

    @property
    def dimension(self) -> int | None:
        """Размерность векторов; None — индекс пуст (размерность ещё не определена)."""
        return int(self._vectors[0].shape[-1]) if self._vectors else None

    def save(self, path: str) -> None:
        """Весь индекс (id + векторы + поколение) в один .npz — атомарно, под блокировкой и без pickle."""
        if len(self._ids) != len(self._vectors):
            raise IndexIntegrityError(f"Индекс в памяти не согласован: векторов {len(self._vectors)}, id {len(self._ids)} — не сохраняю")
        final = Path(_npz_path(path))
        final.parent.mkdir(parents=True, exist_ok=True)
        vectors = np.stack(self._vectors) if self._vectors else np.zeros((0, 0), dtype=np.float32)
        with IndexLock(lock_target(path)):
            _check_generation(self, _npz_disk_generation(final), final)
            gen = new_generation()
            tmp = tmp_name(final)
            try:
                with open(tmp, "wb") as fh:                  # файловый объект: np.savez не дописывает своё «.npz» к имени
                    np.savez(fh, ids=np.array([str(i) for i in self._ids], dtype=str), vectors=vectors,
                             generation=np.array(gen), sources=np.array([s or "" for s in self._sources], dtype=str),
                             manifest=np.array(json.dumps(_manifest_fields(self), ensure_ascii=False)))
                    fh.flush()
                    os.fsync(fh.fileno())
                replace_with_retry(tmp, final)
            except BaseException:
                discard(tmp)
                raise
            fsync_dir(final.parent)
            self._disk_gen, self._disk_probe = gen, file_probe(final)

    @classmethod
    def load(cls, path: str) -> "NumpyCatalogIndex":
        final = Path(_npz_path(path))
        if not final.exists():
            raise FileNotFoundError(f"Файл индекса не найден: {final}")
        with IndexLock(lock_target(path)):
            probe = file_probe(final)
            if not zipfile.is_zipfile(final):                # не .npz вообще: np.load попытался бы прочитать его как pickle
                raise IndexIntegrityError(f"Индекс {final} не является файлом .npz. {REBUILD_HINT}")
            try:
                gen = None
                try:
                    with np.load(final, allow_pickle=False) as data:     # все части — из одного открытия файла
                        ids, vectors = [str(i) for i in data["ids"]], data["vectors"]
                        gen = str(data["generation"]) if "generation" in data.files else None
                        sources = [str(x) or None for x in data["sources"]] if "sources" in data.files else None
                        model, namespace = _read_manifest(json.loads(str(data["manifest"])) if "manifest" in data.files else None)
                except ValueError as exc:
                    if "Object arrays cannot be loaded" not in str(exc):
                        raise
                    # формат до 2026-11-07: id сохранялись массивом объектов (pickle). Файл пишет только сама программа,
                    # поэтому читаем его ради совместимости; следующее сохранение запишет новый формат без pickle.
                    logger.warning("Индекс %s в старом формате (pickle); при следующем сохранении он будет переписан без pickle", final)
                    with np.load(final, allow_pickle=True) as data:
                        ids, vectors = [str(i) for i in data["ids"]], data["vectors"]
                        sources, model, namespace = None, None, None
            except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile) as exc:
                raise IndexIntegrityError(f"Индекс {final} не читается ({type(exc).__name__}: {exc}). {REBUILD_HINT}") from exc
        vectors = np.asarray(vectors)
        count = 0 if vectors.size == 0 else (vectors.shape[0] if vectors.ndim == 2 else -1)
        if count < 0 or count != len(ids):
            raise IndexIntegrityError(f"Индекс {final} повреждён: векторов {vectors.shape}, id {len(ids)}. {REBUILD_HINT}")
        if sources is not None and len(sources) != len(ids):
            raise IndexIntegrityError(f"Индекс {final} повреждён: источников {len(sources)}, id {len(ids)}. {REBUILD_HINT}")
        instance = cls()
        instance._ids = ids
        instance._vectors = list(vectors) if count else []
        instance._sources = sources if sources is not None else [None] * len(ids)
        instance._disk_gen = gen if gen is not None else ("legacy", _sha256_file(final))
        instance._disk_probe = probe
        instance.model, instance.namespace = model, namespace
        return instance


class FaissCatalogIndex:
    """Продакшн ANN-индекс поверх FAISS (IndexFlatIP по нормализованным векторам = cosine sim)."""

    _disk_gen: Any = _NO_GUARD
    _disk_probe: Any = None
    _disk_sha: str | None = None   # SHA-256 файла индекса, который этот процесс загрузил или записал сам
    model: dict | None = None      # паспорт модели векторов (см. NumpyCatalogIndex.model)
    namespace: str | None = None

    def __init__(self, embedding_dim: int):
        self._faiss = _import_faiss()
        self._index = self._faiss.IndexFlatIP(embedding_dim)
        self._ids: list[str] = []
        self._sources: list[str | None] = []

    def add(self, product_id: str, vectors: np.ndarray, sources: list[str | None] | None = None) -> None:
        vectors = np.atleast_2d(vectors).astype("float32")
        if vectors.shape[1] != self._index.d:
            raise ValueError(f"Размерность векторов {vectors.shape[1]} не совпадает с индексом ({self._index.d})")
        src = _sources_for(len(vectors), sources)
        self._index.add(vectors)
        self._ids.extend([product_id] * len(vectors))
        self._sources.extend(src)

    def search(self, query: np.ndarray, top_k: int = 5) -> list[tuple[str, float]]:
        if self._index.ntotal == 0:
            return []
        query = np.atleast_2d(query).astype("float32")
        scores, indices = self._index.search(query, min(top_k, self._index.ntotal))
        return [
            (self._ids[idx], float(score))
            for idx, score in zip(indices[0], scores[0])
            if idx != -1
        ]

    def search_sources(self, query: np.ndarray, top_k: int = 5) -> list[tuple[str, float, str | None]]:
        """Как search, но с источником каждого вектора (см. NumpyCatalogIndex.search_sources)."""
        if self._index.ntotal == 0:
            return []
        query = np.atleast_2d(query).astype("float32")
        scores, indices = self._index.search(query, min(top_k, self._index.ntotal))
        return [(self._ids[idx], float(score), self._sources[idx]) for idx, score in zip(indices[0], scores[0]) if idx != -1]

    @property
    def dimension(self) -> int:
        return int(self._index.d)

    def save(self, path: str) -> None:
        """Пишет FAISS-индекс и JSON с id рядом. Порядок: оба во временные файлы (fsync) -> замена индекса -> замена JSON.
        Обрыв до замены индекса — прежняя пара цела. Обрыв между заменами — временный JSON остаётся, load() завершит замену."""
        if len(self._ids) != self._index.ntotal or len(self._sources) != len(self._ids):
            raise IndexIntegrityError(f"Индекс в памяти не согласован: векторов {self._index.ntotal}, id {len(self._ids)}, "
                                      f"источников {len(self._sources)} — не сохраняю")
        final = Path(path)
        final.parent.mkdir(parents=True, exist_ok=True)
        sidecar = Path(_ids_sidecar_path(path))
        with IndexLock(lock_target(path)):
            _check_generation(self, _faiss_disk_generation(final, sidecar), final)
            # список id мог остаться прежним, а файл индекса — смениться (чужая запись оборвалась между заменами, файл
            # подложили вручную): это тоже чужое изменение, перезаписывать нельзя
            if self._disk_gen is not _NO_GUARD and not _index_file_is_ours(self, final):
                raise IndexChangedError(_changed_message(final))
            gen = new_generation()
            tmp_index, tmp_ids = tmp_name(final), tmp_name(sidecar)
            try:
                self._faiss.write_index(self._index, str(tmp_index))
                fsync_file(tmp_index)
                meta = {"format": IDS_FORMAT, "generation": gen, "count": len(self._ids), "dim": self.dimension,
                        "index_sha256": _sha256_file(tmp_index), "ids": self._ids, "sources": self._sources,
                        "manifest": _manifest_fields(self)}
                with open(tmp_ids, "w", encoding="utf-8") as fh:
                    json.dump(meta, fh, ensure_ascii=False)
                    fh.flush()
                    os.fsync(fh.fileno())
                replace_with_retry(tmp_index, final)
            except BaseException:
                discard(tmp_index)
                discard(tmp_ids)
                raise
            try:
                replace_with_retry(tmp_ids, sidecar)
            except Exception as exc:
                # новый индекс уже на месте, а список id — прежний. Временный список НЕ удаляем: по нему load() завершит замену.
                raise IndexIntegrityError(
                    f"Индекс {final} записан, но список id не удалось заменить ({type(exc).__name__}: {exc}). Новый список "
                    f"сохранён в {tmp_ids.name}; при следующей загрузке замена будет завершена автоматически.") from exc
            fsync_dir(final.parent)
            self._disk_gen, self._disk_probe, self._disk_sha = gen, (file_probe(final), file_probe(sidecar)), meta["index_sha256"]

    @classmethod
    def load(cls, path: str) -> "FaissCatalogIndex":
        faiss = _import_faiss()
        final = Path(path)
        if not final.exists():
            raise FileNotFoundError(f"Файл индекса не найден: {final}")
        sidecar = Path(_ids_sidecar_path(path))
        with IndexLock(lock_target(path)) as lock:
            attempts = 1 if lock.locked else UNLOCKED_LOAD_ATTEMPTS
            for attempt in range(1, attempts + 1):
                try:
                    instance = cls._load_once(faiss, final, sidecar)
                    break
                except _PairMismatch as mismatch:
                    if attempt < attempts:                   # без блокировки: возможно, параллельная запись между заменами
                        time.sleep(0.2)
                        continue
                    if not _roll_forward(final, sidecar, mismatch.index_sha):
                        raise IndexIntegrityError(str(mismatch)) from None
                    instance = cls._load_once(faiss, final, sidecar)
                    break
            else:  # pragma: no cover
                raise AssertionError("unreachable")
            if lock.locked:                                  # под блокировкой чужая запись не идёт: оставшееся — брошено
                _warn_leftovers(final, sidecar)
            return instance

    @classmethod
    def _load_once(cls, faiss, final: Path, sidecar: Path) -> "FaissCatalogIndex":
        data = final.read_bytes()                           # ОДНО чтение: и контрольная сумма, и разбор — из этих байтов
        index_sha = sha256_bytes(data)
        if not sidecar.exists():
            raise _PairMismatch(f"Рядом с индексом {final} нет файла id товаров {sidecar.name}: векторы не с чем сопоставить. "
                                f"{REBUILD_HINT}", index_sha)
        try:
            sidecar_bytes = sidecar.read_bytes()
            meta = json.loads(sidecar_bytes.decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise IndexIntegrityError(f"Файл id товаров {sidecar} не читается ({exc}). {REBUILD_HINT}") from exc
        sources, model, namespace = None, None, None
        if isinstance(meta, list):                              # формат 1 (до 2026-11-07): только список id
            ids, expected_sha, expected_count = meta, None, len(meta)
            gen = _legacy_generation(sidecar_bytes, index_sha)
        elif isinstance(meta, dict) and meta.get("format") == IDS_FORMAT and isinstance(meta.get("ids"), list):
            ids, expected_sha, expected_count = meta["ids"], meta.get("index_sha256"), meta.get("count")
            sources = meta.get("sources")
            try:
                model, namespace = _read_manifest(meta.get("manifest"))
            except ValueError as exc:
                raise IndexIntegrityError(f"Файл id товаров {sidecar}: {exc}. {REBUILD_HINT}") from exc
            gen = meta.get("generation") if isinstance(meta.get("generation"), str) else ("no-generation", file_probe(sidecar))
        else:
            raise IndexIntegrityError(f"Файл id товаров {sidecar} в неизвестном формате. {REBUILD_HINT}")
        if expected_sha is not None and index_sha != expected_sha:
            raise _PairMismatch(f"Индекс {final} не совпадает со своим файлом id (контрольная сумма другая: файлы от разных "
                                f"сохранений или запись оборвалась). {REBUILD_HINT}", index_sha)
        raw = _deserialize(faiss, data, final)
        if not all(isinstance(i, str) for i in ids) or len(ids) != expected_count or len(ids) != raw.ntotal:
            raise IndexIntegrityError(f"Индекс {final} не согласован: векторов {raw.ntotal}, id {len(ids)}. {REBUILD_HINT}")
        if sources is not None and (not isinstance(sources, list) or len(sources) != len(ids)
                                    or not all(s is None or isinstance(s, str) for s in sources)):
            raise IndexIntegrityError(f"Индекс {final}: список источников векторов не согласован с id. {REBUILD_HINT}")
        instance = cls.__new__(cls)  # пропускаем __init__: не создаём лишний пустой IndexFlatIP
        instance._faiss = faiss
        instance._index = raw
        instance._ids = list(ids)
        instance._sources = list(sources) if sources is not None else [None] * len(ids)
        instance._disk_gen = gen
        instance._disk_probe = (file_probe(final), file_probe(sidecar))
        instance._disk_sha = index_sha
        instance.model, instance.namespace = model, namespace
        return instance


class _PairMismatch(Exception):
    def __init__(self, message: str, index_sha: str):
        super().__init__(message)
        self.index_sha = index_sha


def _import_faiss():
    try:
        import faiss  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("faiss-cpu не установлен. См. requirements.txt.") from exc
    return faiss


def _deserialize(faiss, data: bytes, final: Path):
    """Индекс из уже прочитанных байтов (faiss.deserialize_index). Если такой функции нет — чтение файла и повторная сверка
    контрольной суммы файла: файл не должен был смениться между двумя чтениями."""
    try:
        if hasattr(faiss, "deserialize_index"):
            return faiss.deserialize_index(np.frombuffer(data, dtype=np.uint8))
        raw = faiss.read_index(str(final))
    except RuntimeError as exc:                                 # FAISS сообщает о повреждённых данных именно так
        raise IndexIntegrityError(f"Индекс {final} не читается ({exc}). {REBUILD_HINT}") from exc
    if _sha256_file(final) != sha256_bytes(data):
        raise _PairMismatch(f"Индекс {final} сменился во время чтения. {REBUILD_HINT}", sha256_bytes(data))
    return raw


def _roll_forward(final: Path, sidecar: Path, index_sha: str) -> bool:
    """Завершение прерванной записи: среди временных файлов списка id ищется тот, что записан для ТЕКУЩЕГО файла индекса
    (совпадает SHA-256), и он ставится на место. Чужие временные файлы не трогаются."""
    for candidate in sorted(final.parent.glob(sidecar.name + ".tmp-*")):
        try:
            meta = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (isinstance(meta, dict) and meta.get("format") == IDS_FORMAT and meta.get("index_sha256") == index_sha
                and isinstance(meta.get("ids"), list) and len(meta["ids"]) == meta.get("count")):
            replace_with_retry(candidate, sidecar)
            fsync_dir(final.parent)
            logger.warning("Индекс %s: завершена прерванная запись списка id (из %s)", final, candidate.name)
            return True
    return False


def _warn_leftovers(final: Path, sidecar: Path) -> None:
    """Временные файлы, брошенные оборванной записью, занимают место (файл индекса — до сотен МБ). Удаление — решение человека:
    здесь только предупреждение в журнале."""
    leftovers = sorted(p.name for pattern in (final.name + ".tmp-*", sidecar.name + ".tmp-*") for p in final.parent.glob(pattern))
    if leftovers:
        logger.warning("Рядом с индексом %s остались временные файлы от оборванной записи (не удалены, можно удалить вручную, "
                       "когда воркер и импорт остановлены): %s", final, ", ".join(leftovers))


def _index_file_is_ours(index, final: Path) -> bool:
    """Файл индекса на диске — тот, что этот процесс загрузил или записал? Быстро — по размеру, времени и inode; если они
    разошлись, точно — по SHA-256. На общих папках Docker Desktop (Windows) время и номер файла могут меняться без изменения
    содержимого: без проверки по содержимому воркер отказывался бы сохранять при каждом добавлении товара."""
    if index._disk_probe is not None and file_probe(final) == index._disk_probe[0]:
        return True
    if index._disk_sha is None:                                 # при открытии файла не было
        return not final.exists()
    try:
        same = _sha256_file(final) == index._disk_sha
    except FileNotFoundError:
        return False
    if same and index._disk_probe is not None:                  # то же содержимое — запоминаем новый отпечаток
        index._disk_probe = (file_probe(final), index._disk_probe[1])
    return same


def index_size(index) -> int:
    """Число векторов в индексе (в памяти)."""
    return len(getattr(index, "_ids", ()))


def open_catalog_index(index_path: str | None, embedding_dim: int, model: dict | None = None, namespace: str | None = None):
    """Индекс каталога для работы — ОДНА реализация для воркера и скриптов импорта.

    - файла нет -> новый пустой индекс (FAISS, без него — numpy): нормальное состояние до первого товара;
    - файл есть и читается -> он, после сверки размерности с эмбеддером;
    - файл есть, но не читается или не сходится -> исключение. Пустой индекс вместо повреждённого НЕ создаётся: первое же
      добавление товара записало бы его поверх файла и стёрло все эталоны. Воркер в этом случае помечает компонент
      «matcher» как неготовый (build_runtime), скрипты останавливаются с причиной.
    Открытый так индекс помнит поколение файла и при сохранении не перезапишет файл, записанный другим процессом.

    model — паспорт загруженной модели эмбеддера (embedder.model_signature). Индекс другой модели или другого каталога
    (namespace) — отказ с причиной. Индекс без записанной модели (до 2026-11-14) загружается с предупреждением и остаётся
    «непроверенным» до полной пересборки: дописать ему текущую модель нельзя — неизвестно, какой построены старые векторы.
    Без model (служебные скрипты без эмбеддера) модель не сверяется, а записанная в файле сохраняется как есть."""
    index = None
    if index_path:
        npz_path = _npz_path(index_path)
        if not index_path.endswith(".npz") and Path(index_path).exists():
            index = FaissCatalogIndex.load(index_path)          # RuntimeError, если FAISS не установлен: тоже отказ
        elif Path(npz_path).exists():
            index = NumpyCatalogIndex.load(index_path)
        if index is not None:
            dim = index.dimension
            if dim is not None and dim != embedding_dim:
                raise IndexIntegrityError(
                    f"Индекс {index_path} построен для векторов размерности {dim}, а эмбеддер даёт {embedding_dim}: индекс от "
                    f"другой модели. {REBUILD_HINT}")
            _check_manifest(index, index_path, model, namespace)
            index._expected = (model, namespace)
            return index
    try:
        index = FaissCatalogIndex(embedding_dim=embedding_dim)
    except RuntimeError as exc:
        logger.warning("FAISS недоступен (%s), используется numpy-индекс", exc)
        index = NumpyCatalogIndex()
    index.model, index.namespace = model, namespace or DEFAULT_NAMESPACE
    index._expected = (model, namespace)
    if index_path:                                              # файла не было: появление чужого файла тоже обнаружится
        index._disk_gen = None
        index._disk_probe = _probe_files(index, index_path)
        if isinstance(index, FaissCatalogIndex):
            index._disk_sha = None
    return index


def _check_manifest(index, index_path: str, model: dict | None, namespace: str | None) -> None:
    if namespace and index.namespace and index.namespace != namespace:
        raise IndexIntegrityError(f"Индекс {index_path} построен для каталога «{index.namespace}», а нужен «{namespace}». "
                                  f"{REBUILD_HINT}")
    index.namespace = index.namespace or namespace or DEFAULT_NAMESPACE
    if model is None:
        return
    diff = model_mismatch(index.model, model)
    if diff:
        raise IndexIntegrityError(
            f"Индекс {index_path} построен другой моделью распознавания ({diff}). Векторы разных моделей несравнимы: ответы "
            f"были бы неверными. Пересоберите индекс текущей моделью или верните прежнюю модель. {REBUILD_HINT}")
    if index.model is None:
        if index_size(index) == 0:
            index.model = dict(model)                       # пустой индекс: модель векторов — та, что будет их добавлять
        else:
            logger.warning("Индекс %s построен до записи версии модели (%d векторов): совместимость с моделью %s не проверить. "
                           "Рекомендуется полная пересборка индекса.", index_path, index_size(index), model.get("name"))


def check_index_model(index, index_path: str | None, model: dict | None) -> None:
    """Сверка уже открытого индекса с моделью, загруженной позже (скрипт открыл индекс до загрузки модели): другая модель —
    IndexIntegrityError; пустой индекс без модели получает её. Дальнейшие перечитывания сверяют с ней же."""
    _check_manifest(index, index_path or "в памяти", model, None)
    expected = getattr(index, "_expected", (None, None))
    index._expected = (model if model is not None else expected[0], expected[1])


def _probe_files(index, index_path: str):
    """Отпечатки файлов индекса на диске: для FAISS — (файл индекса, список id), для numpy — файл .npz."""
    if isinstance(index, FaissCatalogIndex):
        return (file_probe(Path(index_path)), file_probe(Path(_ids_sidecar_path(index_path))))
    return file_probe(Path(_npz_path(index_path)))


def _disk_generation_for(index, index_path: str):
    if isinstance(index, FaissCatalogIndex):
        return _faiss_disk_generation(Path(index_path), Path(_ids_sidecar_path(index_path)))
    return _npz_disk_generation(Path(_npz_path(index_path)))


def refresh_index_if_changed(matcher, index_path: str | None, embedding_dim: int) -> str:
    """Перед работой воркера: если индекс на диске записан другим процессом (импорт), загрузить его. Дешёвая проверка — по
    размеру и времени файла; решение — по поколению. Ошибка загрузки нового файла не ломает работу: остаётся прежняя копия
    в памяти (она согласована с прежним состоянием диска), а сохранение поверх чужого файла всё равно будет запрещено.
    Возвращает "unchanged" | "reloaded" | "error: …"."""
    index = matcher.index
    if not index_path or getattr(index, "_disk_gen", _NO_GUARD) is _NO_GUARD:
        return "unchanged"
    probe: Any = _probe_files(index, index_path)
    if probe == index._disk_probe:
        return "unchanged"
    if _disk_generation_for(index, index_path) == index._disk_gen and (
            not isinstance(index, FaissCatalogIndex) or _index_file_is_ours(index, Path(index_path))):
        index._disk_probe = probe                           # отпечаток сменился, содержимое — нет (копирование, общая папка)
        return "unchanged"
    try:
        matcher.index = open_catalog_index(index_path, embedding_dim, *getattr(index, "_expected", (None, None)))
    except Exception as exc:  # noqa: BLE001 — прежняя копия продолжает работать; причина — в журнал
        logger.error("Индекс %s изменён другим процессом, но не загружается (%s); работаю с прежней копией", index_path, exc)
        return f"error: {type(exc).__name__}: {exc}"
    logger.info("Индекс %s изменён другим процессом — загружен заново (%d векторов)", index_path, index_size(matcher.index))
    return "reloaded"


def resync_index(matcher, index_path: str | None, embedding_dim: int) -> str | None:
    """После сбоя добавления: векторы уже в памяти, а на диск (или в базу) не попали. Индекс в памяти заменяется
    сохранённым на диске — он и есть правда. None — успешно; иначе причина (тогда пользоваться индексом в памяти нельзя)."""
    try:
        matcher.index = open_catalog_index(index_path, embedding_dim, *getattr(matcher.index, "_expected", (None, None)))
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    return None


ARCHIVED_DEPTH_PER_PRODUCT = 64     # насколько глубже искать на каждый товар в архиве (векторов одного товара обычно меньше)


class ProductMatcher:
    """Применяет пороги match_threshold/unknown_threshold к результатам поиска по индексу."""

    def __init__(self, index: CatalogIndex, top_k: int = 5, match_threshold: float = 0.72,
                 unknown_threshold: float = 0.45, margin_threshold: float = 0.0, candidates_k: int = 25):
        self.index = index
        self.top_k = top_k
        self.match_threshold = match_threshold
        self.unknown_threshold = unknown_threshold
        self.margin_threshold = margin_threshold
        self.candidates_k = candidates_k          # сколько ВЕКТОРОВ просматривать, чтобы найти соперника среди других товаров
        # (товар, источник) векторов, которые распознавание пропускает: фото выведены из эталонов или сделаны витринными
        # (app/services/product_images.PhotoRegistry.excluded_sources; воркер обновляет перед каждым «Узнать товар»).
        self.excluded: frozenset[tuple[str, str]] = frozenset()
        # Товары в архиве (products.status, миграция 0006): все их векторы пропускаются, товар не распознаётся. Воркер обновляет
        # из базы перед каждым «Узнать товар»; индекс не пересобирается — товар можно вернуть из архива.
        self.excluded_products: frozenset[str] = frozenset()

    def rank(self, embedding: np.ndarray, k: int | None = None) -> list[tuple[str, float]]:
        """Лучший счёт на каждый товар; k — сколько ВЕКТОРОВ просмотреть (по умолчанию max(top_k, candidates_k)).

        Исключённые векторы (self.excluded) отбрасываются, а просмотр углубляется на их число: k векторов остаются k
        участвующими. Индекс без источников у векторов (search_sources нет) — прежний поиск без исключений."""
        k = k or max(self.top_k, self.candidates_k)
        search_sources = getattr(self.index, "search_sources", None)
        archived = self.excluded_products
        if not archived and (not self.excluded or search_sources is None):
            return rank_products(self.index.search(embedding, k))
        depth = k + len(self.excluded) + ARCHIVED_DEPTH_PER_PRODUCT * len(archived)
        if search_sources is None:                       # индекс без источников: исключить можно только товар целиком
            kept = [(pid, score) for pid, score in self.index.search(embedding, depth) if pid not in archived]
        else:
            kept = [(pid, score) for pid, score, source in search_sources(embedding, depth)
                    if pid not in archived and (pid, source) not in self.excluded]
        return rank_products(kept[:k])

    def match(self, embedding: np.ndarray) -> MatchResult:
        return decide_match(self.rank(embedding), self.match_threshold, self.unknown_threshold, self.margin_threshold)

    def match_batch(self, embeddings: np.ndarray) -> list[MatchResult]:
        return [self.match(vec) for vec in embeddings]
