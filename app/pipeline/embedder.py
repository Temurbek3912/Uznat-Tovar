"""
Получение векторных эмбеддингов для вырезанных изображений товаров.

Ключевая идея: одна и та же модель эмбеддера используется и для эталонных
фото каталога (offline, при добавлении SKU — см. ml/build_catalog_embeddings.py),
и для кропов с реальных снимков полки (online, в пайплайне). Совпадение SKU
определяется близостью векторов, а не classifier'ом — см. matcher.py и
раздел 5 ТЗ (docs/TZ_technical_specification.md) про open-set retrieval.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Protocol, Sequence

import numpy as np

logger = logging.getLogger(__name__)

# Публичная модель по умолчанию, если дообученных весов (models.embedder.weights_path) нет на диске.
# hidden_size = 768 — совпадает с embedding_dim из configs/config.yaml. Скачивается один раз (нужен интернет).
DEFAULT_HUB_MODEL = "facebook/dinov2-base"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def resolve_embedder_source(
    weights_path: str | None,
    hub_model: str = DEFAULT_HUB_MODEL,
    roots: Sequence[Path] | None = None,
) -> tuple[str, bool]:
    """Что передать в from_pretrained: (источник, взят_ли_из_хаба).

    БАГФИКС: раньше weights_path (по умолчанию «models/embedder/dinov2_base_retail_ft», такой папки нет)
    уходил прямо в from_pretrained. Для несуществующего локального пути HuggingFace считает строку
    идентификатором репозитория и падает («Repo id must be in the form…»), так что без дообученной модели
    не работали ни каталог (scripts/add_product_from_local_file.py), ни воркер.
    Теперь: существующая локальная папка (относительно текущего каталога или корня проекта) — используем её,
    иначе — публичная модель из хаба.
    """
    if weights_path:
        for base in (roots if roots is not None else (Path.cwd(), _PROJECT_ROOT)):
            candidate = Path(base) / weights_path
            if candidate.is_dir():
                return str(candidate), False
    return hub_model, True


# Как из выхода модели получается вектор. Меняется вместе с кодом embed() — тогда индекс нужно пересобрать.
DINOV2_PREPROCESS = "hf-auto-image-processor/cls-token/v1"
L2 = "l2"


def weights_fingerprint(folder: Path) -> str:
    """SHA-256 всех файлов папки весов (имя + содержимое, по порядку имён): другие веса — другой отпечаток.
    Для ~350 МБ DINOv2-base — около секунды при запуске; считается один раз на процесс."""
    digest = hashlib.sha256()
    for path in sorted(p for p in Path(folder).rglob("*") if p.is_file()):
        digest.update(path.relative_to(folder).as_posix().encode("utf-8") + b"\0")
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def model_signature(embedder) -> dict | None:
    """Паспорт модели эмбеддера для манифеста индекса (документ 07, §8) или None, если эмбеддер его не сообщает."""
    sig = getattr(embedder, "model_signature", None)
    return dict(sig) if isinstance(sig, dict) else None


class ImageEmbedder(Protocol):
    embedding_dim: int
    model_signature: dict   # name, weights, preprocess, dim, normalization — индекс помнит, какой моделью построен

    def embed(self, crops: list[np.ndarray]) -> np.ndarray:
        """Возвращает L2-нормализованную матрицу эмбеддингов формы (N, embedding_dim)."""
        ...


class DinoV2Embedder:
    """Продакшн-реализация поверх HuggingFace transformers (DINOv2 или дообученный чекпоинт)."""

    def __init__(self, weights_path: str, embedding_dim: int = 768, device: str = "cuda"):
        try:
            import torch
            from transformers import AutoImageProcessor, AutoModel
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "transformers/torch не установлены. Установите зависимости из requirements.txt."
            ) from exc

        self._torch = torch
        self.device = device if torch.cuda.is_available() else "cpu"
        self.embedding_dim = embedding_dim
        source, from_hub = resolve_embedder_source(weights_path)
        if from_hub:
            logger.warning(
                "Дообученные веса эмбеддера не найдены (%s) — используется публичная модель %s "
                "(при первом запуске скачивается из интернета). Если позже подключите другие веса, "
                "ПЕРЕСОБЕРИТЕ индекс каталога: эмбеддинги разных моделей несовместимы.", weights_path, source,
            )
        self._processor = AutoImageProcessor.from_pretrained(source)
        self._model = AutoModel.from_pretrained(source).to(self.device).eval()
        if from_hub:
            # ревизия (коммит) репозитория, из которого взяты веса; transformers записывает её и при чтении из кэша
            revision = getattr(self._model.config, "_commit_hash", None)
            weights = f"hub:{source}@{revision}" if revision else f"hub:{source}"
        else:
            weights = weights_fingerprint(Path(source))
        self.model_signature = {"name": source if from_hub else Path(source).name, "weights": weights,
                                "preprocess": DINOV2_PREPROCESS, "dim": int(embedding_dim), "normalization": L2}

    def embed(self, crops: list[np.ndarray]) -> np.ndarray:
        if not crops:
            return np.zeros((0, self.embedding_dim), dtype=np.float32)

        inputs = self._processor(images=crops, return_tensors="pt").to(self.device)
        with self._torch.no_grad():
            outputs = self._model(**inputs)
            # CLS-токен / pooled-выход как эмбеддинг изображения
            vectors = outputs.last_hidden_state[:, 0, :].cpu().numpy()

        return _l2_normalize(vectors)


class RandomDebugEmbedder:
    """
    Детерминированный псевдо-эмбеддер без ML-зависимостей — для unit-тестов
    и локальной разработки логики matcher/planogram/osa без скачивания моделей.

    ВАЖНО: не имеет отношения к реальному визуальному сходству, использовать
    только в тестах.
    """

    def __init__(self, embedding_dim: int = 64):
        self.embedding_dim = embedding_dim
        self.model_signature = {"name": "random-debug", "weights": "none", "preprocess": "mean-seed/v1",
                                "dim": int(embedding_dim), "normalization": L2}

    def embed(self, crops: list[np.ndarray]) -> np.ndarray:
        vectors = np.stack(
            [self._hash_to_vector(crop) for crop in crops]
        ) if crops else np.zeros((0, self.embedding_dim), dtype=np.float32)
        return _l2_normalize(vectors)

    def _hash_to_vector(self, crop: np.ndarray) -> np.ndarray:
        rng = np.random.default_rng(seed=int(crop.mean() * 1000) if crop.size else 0)
        return rng.normal(size=self.embedding_dim).astype(np.float32)


def _l2_normalize(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms
