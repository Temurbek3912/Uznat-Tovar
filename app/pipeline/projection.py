"""
Лёгкая проекция поверх векторов модели поиска по виду (замер 2026-11-22, docs/benchmark/projection_report_2026-11-22.md).

Что это. Модель SigLIP 2 не меняется; к её вектору x добавляется поправка: y = norm(x + x·A), где A — матрица 768×768,
обученная на фото каталога (ml/train_projection.py) так, чтобы разные фото ОДНОГО товара (лицевая сторона, бок, состав)
стали ближе друг к другу, а фото разных товаров — дальше. Обучение — несколько секунд на процессоре, по готовым векторам
из кэша базы; видеокарта не нужна.

Где применяется. ProjectedEmbedder оборачивает эмбеддер: и эталоны каталога, и кадр покупателя проходят одну и ту же
проекцию. Паспорт модели получает отметку «+proj-<отпечаток>» (поле preprocess): индекс без проекции с проекцией не
смешивается — программа попросит пересобрать индекс (scripts/rebuild_index.py --apply --compute-missing), а векторы для
пересборки берутся из кэша базы (векторы модели без проекции -> проекция), без пересчёта фото.

Включается настройкой models.embedder.projection_path (пусто — выключено, как раньше). Проекция обучена для одной модели
(паспорт записан в файле): у другой модели (облегчённый режим с DINOv2 или int8) она не применяется — в журнал
предупреждение, работа как без проекции.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

PROJ_MARK = "+proj-"
SIGNATURE_FIELDS = ("name", "weights", "preprocess", "dim", "normalization")


def _normalize(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (vectors / norms).astype(np.float32)


class Projection:
    """Матрица поправки A и паспорт модели, для векторов которой она обучена."""

    def __init__(self, matrix: np.ndarray, base_signature: dict, info: dict | None = None):
        matrix = np.asarray(matrix, dtype=np.float32)
        if matrix.ndim != 2 or matrix.shape[0] != matrix.shape[1]:
            raise ValueError(f"матрица проекции должна быть квадратной, а она {matrix.shape}")
        if not isinstance(base_signature, dict) or not all(k in base_signature for k in SIGNATURE_FIELDS):
            raise ValueError("в файле проекции нет паспорта модели")
        if int(base_signature["dim"]) != matrix.shape[0]:
            raise ValueError(f"размерность проекции {matrix.shape[0]}, а модели {base_signature['dim']}")
        if PROJ_MARK in str(base_signature.get("preprocess", "")):
            raise ValueError("проекция обучена на векторах с другой проекцией — нужны векторы самой модели")
        self.matrix = matrix
        self.base_signature = {k: base_signature[k] for k in SIGNATURE_FIELDS}
        self.info = dict(info or {})
        self.fingerprint = hashlib.sha256(matrix.tobytes()).hexdigest()[:16]

    @property
    def dim(self) -> int:
        return int(self.matrix.shape[0])

    def applies_to(self, signature: dict | None) -> bool:
        """Обучена ли проекция для векторов этой модели (паспорт совпадает полностью)."""
        return isinstance(signature, dict) and all(signature.get(k) == self.base_signature[k] for k in SIGNATURE_FIELDS)

    def signature(self) -> dict:
        """Паспорт векторов С проекцией: другой, чем у модели, — индекс и кэш их не смешают."""
        sig = dict(self.base_signature)
        sig["preprocess"] = f"{sig['preprocess']}{PROJ_MARK}{self.fingerprint}"
        return sig

    def apply(self, vectors: np.ndarray) -> np.ndarray:
        """Векторы модели (N, d) -> векторы с проекцией, L2-нормализованные."""
        x = np.atleast_2d(np.asarray(vectors, dtype=np.float32))
        if x.shape[0] == 0:
            return np.zeros((0, self.dim), dtype=np.float32)
        if x.shape[1] != self.dim:
            raise ValueError(f"размерность векторов {x.shape[1]}, а проекции {self.dim}")
        return _normalize(x + x @ self.matrix)

    # файл -----------------------------------------------------------------
    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp.npz")
        np.savez(tmp, matrix=self.matrix,
                 base_signature=np.array(json.dumps(self.base_signature, ensure_ascii=False, sort_keys=True)),
                 info=np.array(json.dumps(self.info, ensure_ascii=False, sort_keys=True)))
        tmp.replace(path)
        return path

    @classmethod
    def load(cls, path: str | Path) -> "Projection":
        with np.load(Path(path), allow_pickle=False) as data:
            return cls(data["matrix"], json.loads(str(data["base_signature"])),
                       json.loads(str(data["info"])) if "info" in data.files else {})


class ProjectedEmbedder:
    """Эмбеддер модели + проекция: embed() даёт векторы с проекцией, паспорт — с отметкой проекции."""

    def __init__(self, base, projection: Projection):
        self.base = base
        self.projection = projection
        self.embedding_dim = projection.dim
        self.model_signature = projection.signature()

    def embed(self, crops) -> np.ndarray:
        return self.projection.apply(self.base.embed(crops))

    def vectors_from_base(self, registry, image_ids=None) -> dict:
        """Векторы с проекцией для фото, у которых в кэше базы есть вектор самой модели (без пересчёта фото); image_ids=None —
        для всех таких фото."""
        from app.pipeline.embedder import model_version  # noqa: PLC0415
        version = model_version(self.projection.base_signature)
        if version is None:
            return {}
        cache = registry.cached_vectors(version)
        ids = list(cache) if image_ids is None else [i for i in set(image_ids) if i in cache]
        if not ids:
            return {}
        projected = self.projection.apply(np.stack([cache[i] for i in ids]))
        return dict(zip(ids, projected, strict=True))

    def __getattr__(self, name):            # device, int8 и прочее — от модели
        return getattr(self.base, name)


def resolve_projection_path(path: str) -> Path:
    from app.core.config import resolve_project_path  # noqa: PLC0415
    return Path(resolve_project_path(path))


def with_projection(embedder, projection_path: str | None):
    """Обернуть эмбеддер проекцией из настройки. Пусто — как есть. Файла нет, он испорчен или обучен для другой модели —
    как есть, с записью в журнал (распознавание работает без проекции; индекс с проекцией тогда попросит пересборки)."""
    if not projection_path:
        return embedder
    path = resolve_projection_path(projection_path)
    try:
        projection = Projection.load(path)
    except FileNotFoundError:
        logger.error("Файл проекции %s не найден — распознавание без проекции (models.embedder.projection_path)", path)
        return embedder
    except Exception as exc:  # noqa: BLE001 — испорченный файл не должен останавливать программу
        logger.error("Файл проекции %s не читается (%s) — распознавание без проекции", path, exc)
        return embedder
    signature = getattr(embedder, "model_signature", None)
    if not projection.applies_to(signature):
        logger.warning("Проекция %s обучена для модели %s, а загружена %s — распознавание без проекции", path,
                       projection.base_signature.get("name"), (signature or {}).get("name"))
        return embedder
    return ProjectedEmbedder(embedder, projection)
