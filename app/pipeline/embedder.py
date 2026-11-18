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
import json
import logging
from pathlib import Path
from typing import Protocol, Sequence

import numpy as np

logger = logging.getLogger(__name__)

# Публичная модель по умолчанию, если дообученных весов (models.embedder.weights_path) нет на диске.
# hidden_size = 768 — совпадает с embedding_dim из configs/config.yaml. Скачивается один раз (нужен интернет).
DEFAULT_HUB_MODEL = "facebook/dinov2-base"
# SigLIP 2 base 224 — та же модель, что «вид товара» (app/pipeline/kind_classifier.py): по замеру 2026-11-18 ищет товар по виду
# точнее DINOv2-base (docs/benchmark/models_report_2026-11-18.md). Включается в config.yaml: models.embedder.name: "siglip2-base".
SIGLIP2_HUB_MODEL = "google/siglip2-base-patch16-224"
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
SIGLIP_PREPROCESS = "hf-auto-image-processor/siglip-pooler-output/v1"
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


SIGNATURE_FIELDS = ("name", "weights", "preprocess", "dim", "normalization")   # те же, что сверяет манифест индекса


def model_version(signature: dict | None) -> str | None:
    """Короткий номер версии модели для кэша эмбеддингов (image_embeddings.model_version): SHA-256 паспорта модели.
    Одинаковый паспорт — одна версия; другие веса, обработка или размерность — другая. None — паспорта нет."""
    if not isinstance(signature, dict) or not all(k in signature for k in SIGNATURE_FIELDS):
        return None
    canonical = json.dumps({k: signature[k] for k in SIGNATURE_FIELDS}, sort_keys=True, ensure_ascii=False)
    return "m1-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:40]


class ImageEmbedder(Protocol):
    embedding_dim: int
    model_signature: dict   # name, weights, preprocess, dim, normalization — индекс помнит, какой моделью построен

    def embed(self, crops: list[np.ndarray]) -> np.ndarray:
        """Возвращает L2-нормализованную матрицу эмбеддингов формы (N, embedding_dim)."""
        ...


def embedder_family(name: str | None) -> str:
    """Семейство модели по имени из config.yaml (models.embedder.name): «siglip…» — SigLIP 2, всё остальное — DINOv2."""
    return "siglip" if "siglip" in (name or "").lower() else "dinov2"


def hub_model_for(name: str | None) -> str:
    """Публичная модель по имени из config.yaml: полное имя репозитория («facebook/dinov2-small») — как есть;
    «siglip2-base» — SigLIP 2 base 224; иначе (в том числе «dinov2-base-retail-ft») — DINOv2-base."""
    text = (name or "").strip()
    if "/" in text:
        return text
    return SIGLIP2_HUB_MODEL if embedder_family(text) == "siglip" else DEFAULT_HUB_MODEL


def create_embedder(cfg, device: str = "cuda", int8: bool = False):
    """Эмбеддер по настройке models.embedder (name, weights_path, embedding_dim) — одно место выбора модели для программы,
    импорта, пересборки индекса и оценки. Индекс помнит паспорт модели: после смены модели его нужно пересобрать
    (python -m scripts.rebuild_index --compute-missing)."""
    name = getattr(cfg, "name", "") or ""
    cls = SiglipEmbedder if embedder_family(name) == "siglip" else DinoV2Embedder
    return cls(weights_path=getattr(cfg, "weights_path", "") or "", embedding_dim=int(getattr(cfg, "embedding_dim", 768) or 768),
               device=device, hub_model=hub_model_for(name), int8=int8)


class DinoV2Embedder:
    """Продакшн-реализация поверх HuggingFace transformers (DINOv2 или дообученный чекпоинт)."""

    PREPROCESS = DINOV2_PREPROCESS

    def __init__(self, weights_path: str, embedding_dim: int = 768, device: str = "cuda", hub_model: str = DEFAULT_HUB_MODEL,
                 int8: bool = False):
        """device: "cuda" (или "auto") — видеокарта, если она есть, иначе процессор; "cpu" — всегда процессор (облегчённый режим,
        документ 11, §10). hub_model — публичная модель, если дообученных весов нет (DINOv2-small для слабых компьютеров).
        int8 — квантование линейных слоёв для процессора (быстрее, векторы немного другие: индекс строится той же моделью)."""
        try:
            import torch
            from transformers import AutoImageProcessor
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "transformers/torch не установлены. Установите зависимости из requirements.txt."
            ) from exc

        self._torch = torch
        self.device = "cpu" if device == "cpu" or not torch.cuda.is_available() else "cuda"
        source, from_hub = resolve_embedder_source(weights_path, hub_model)
        if from_hub:
            logger.warning(
                "Дообученные веса эмбеддера не найдены (%s) — используется публичная модель %s "
                "(при первом запуске скачивается из интернета). Если позже подключите другие веса, "
                "ПЕРЕСОБЕРИТЕ индекс каталога: эмбеддинги разных моделей несовместимы.", weights_path, source,
            )
        self._processor = AutoImageProcessor.from_pretrained(source)
        self._model = self._load_model(source).to(self.device).eval()
        hidden = int(getattr(self._model.config, "hidden_size", 0) or 0)
        self.embedding_dim = hidden or int(embedding_dim)      # размер вектора — по самой модели (small: 384, base: 768)
        self.int8 = bool(int8) and self.device == "cpu"
        if self.int8:
            self._model = torch.ao.quantization.quantize_dynamic(self._model, {torch.nn.Linear}, dtype=torch.qint8)
        if from_hub:
            # ревизия (коммит) репозитория, из которого взяты веса; transformers записывает её и при чтении из кэша
            revision = getattr(self._model.config, "_commit_hash", None)
            weights = f"hub:{source}@{revision}" if revision else f"hub:{source}"
        else:
            weights = weights_fingerprint(Path(source))
        name = source if from_hub else Path(source).name
        self.model_signature = {"name": name + ("+int8" if self.int8 else ""), "weights": weights,
                                "preprocess": self.PREPROCESS, "dim": int(self.embedding_dim), "normalization": L2}

    @staticmethod
    def _load_model(source: str):
        from transformers import AutoModel  # noqa: PLC0415
        return AutoModel.from_pretrained(source)

    @staticmethod
    def _vectors(outputs):
        return outputs.last_hidden_state[:, 0, :]          # CLS-токен как эмбеддинг изображения

    def embed(self, crops: list[np.ndarray]) -> np.ndarray:
        if not crops:
            return np.zeros((0, self.embedding_dim), dtype=np.float32)

        inputs = self._processor(images=crops, return_tensors="pt").to(self.device)
        with self._torch.no_grad():
            outputs = self._model(**inputs)
            vectors = self._vectors(outputs).float().cpu().numpy()

        return _l2_normalize(vectors)


class SiglipEmbedder(DinoV2Embedder):
    """SigLIP 2: только визуальная часть модели (текстовая не нужна), вектор — pooler_output, как у «вида товара»
    (app/pipeline/kind_classifier.py). Шкала сходства у неё другая, чем у DINOv2: пороги matcher подбираются заново."""

    PREPROCESS = SIGLIP_PREPROCESS

    def __init__(self, weights_path: str, embedding_dim: int = 768, device: str = "cuda", hub_model: str = SIGLIP2_HUB_MODEL,
                 int8: bool = False):
        super().__init__(weights_path, embedding_dim=embedding_dim, device=device, hub_model=hub_model, int8=int8)

    @staticmethod
    def _load_model(source: str):
        from transformers import SiglipVisionModel  # noqa: PLC0415
        return SiglipVisionModel.from_pretrained(source)

    @staticmethod
    def _vectors(outputs):
        return outputs.pooler_output


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
