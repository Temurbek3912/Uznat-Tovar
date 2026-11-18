"""
Малая быстрая модель «вид товара» (документ 07, §10.7): кадр -> «это напиток / обувь / лекарство …» без названия.

Нужна там, где большой модели «зрение + текст» нет (слабый компьютер, LM Studio не запущен). Модель «картинка + текст»
(SigLIP 2 или CLIP) сравнивает кадр с готовыми описаниями видов (app/services/product_kinds.py, PROMPTS) и с описаниями
«не товар» (NOT_PRODUCT_PROMPTS). Обучать ничего не нужно.

Экономия памяти: описания переводятся в векторы один раз (полная модель, ~1,4 ГБ), векторы сохраняются в файл рядом с
данными программы (kind_text_<модель>_<отпечаток>.npz); дальше грузится только «картинная» половина модели (~0,4 ГБ).
Отпечаток зависит от модели и текста описаний — поменяли описания, файл пересчитается сам.

Кадр обрабатывается только в памяти (D-10а).
"""
from __future__ import annotations

import hashlib
import io
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.services.product_kinds import NOT_PRODUCT_PROMPTS, PROMPTS

logger = logging.getLogger(__name__)

NONE = "_none"


@dataclass(frozen=True)
class KindScore:
    kind: str | None          # None — «не товар» (ближе всего описания «не товар»)
    score: float              # доля уверенности 0..1 (softmax по видам и «не товар»)
    runner_up: str | None = None


def _family(repo: str) -> str:
    return "siglip" if "siglip" in repo.lower() else "clip"


def _template(family: str, text: str) -> str:
    return f"This is a photo of {text}." if family == "siglip" else f"a photo of {text}."


def label_texts(family: str) -> tuple[list[str], list[str]]:
    """(подписи групп по порядку текстов, тексты) — все описания видов и «не товар»."""
    groups, texts = [], []
    for kind, prompts in PROMPTS.items():
        for p in prompts:
            groups.append(kind)
            texts.append(_template(family, p))
    for p in NOT_PRODUCT_PROMPTS:
        groups.append(NONE)
        texts.append(_template(family, p))
    return groups, texts


def _fingerprint(repo: str, texts: list[str]) -> str:
    return hashlib.sha256(("\n".join([repo, *texts])).encode("utf-8")).hexdigest()[:16]


def _load(cls, repo: str):
    """Сначала с диска (без обращения к интернету: запуск не ждёт сети), нет на диске — скачать."""
    try:
        return cls.from_pretrained(repo, local_files_only=True)
    except OSError:
        return cls.from_pretrained(repo)


def _as_tensor(out):
    """transformers 4.x отдаёт тензор, 5.x — иногда объект с pooler_output."""
    if hasattr(out, "pooler_output") and not hasattr(out, "shape"):
        return out.pooler_output
    return out


def scores_from_logits(groups: list[str], logits: np.ndarray) -> KindScore:
    """Логиты по каждому описанию -> лучший вид. По группе берётся лучшее описание; затем softmax по группам."""
    best: dict[str, float] = {}
    for g, v in zip(groups, logits.tolist(), strict=True):
        if g not in best or v > best[g]:
            best[g] = v
    names = list(best)
    vals = np.array([best[n] for n in names], dtype=np.float64)
    probs = np.exp(vals - vals.max())
    probs /= probs.sum()
    order = np.argsort(-probs)
    top = names[order[0]]
    second = names[order[1]] if len(order) > 1 else None
    return KindScore(kind=None if top == NONE else top, score=float(probs[order[0]]),
                     runner_up=None if second in (None, NONE) else second)


class KindClassifier:
    def __init__(self, repo: str, cache_dir: Path, device: str = "cpu"):
        import torch  # noqa: PLC0415 — тяжёлый импорт только при включённой модели
        self.repo = repo
        self.family = _family(repo)
        self.device = device
        self.groups, texts = label_texts(self.family)
        cache_dir.mkdir(parents=True, exist_ok=True)
        slug = repo.replace("/", "__")
        path = cache_dir / f"kind_text_{slug}_{_fingerprint(repo, texts)}.npz"
        if path.is_file():
            data = np.load(path, allow_pickle=False)
            self.text = data["text"]
            self.scale, self.bias = float(data["scale"]), float(data["bias"])
        else:
            self.text, self.scale, self.bias = self._encode_texts(texts)
            tmp = path.with_suffix(".tmp.npz")
            np.savez(tmp, text=self.text, scale=np.float64(self.scale), bias=np.float64(self.bias))
            tmp.replace(path)
            logger.info("Вид товара: описания переведены в векторы и сохранены (%s)", path.name)
        from transformers import AutoImageProcessor  # noqa: PLC0415
        if self.family == "siglip":
            # та же копия, что у поиска по виду, если он тоже SigLIP 2 и в этом процессе (облегчённый режим)
            from app.pipeline.embedder import shared_siglip_vision  # noqa: PLC0415
            self.vision = shared_siglip_vision(repo, device)
        else:
            from transformers import CLIPVisionModelWithProjection  # noqa: PLC0415
            self.vision = _load(CLIPVisionModelWithProjection, repo)
        self.vision = self.vision.eval().to(device)
        self.processor = _load(AutoImageProcessor, repo)
        self._torch = torch
        self._lock = threading.Lock()

    def _encode_texts(self, texts: list[str]):
        import torch  # noqa: PLC0415
        from transformers import AutoModel, AutoTokenizer  # noqa: PLC0415
        model = _load(AutoModel, self.repo).eval()
        tok = _load(AutoTokenizer, self.repo)
        kwargs = {"padding": "max_length", "max_length": 64} if self.family == "siglip" else {"padding": True}
        with torch.no_grad():
            enc = tok(texts, return_tensors="pt", truncation=True, **kwargs)
            feats = _as_tensor(model.get_text_features(**enc))
            feats = torch.nn.functional.normalize(feats, dim=-1).cpu().numpy().astype(np.float32)
        scale = float(model.logit_scale.exp().item())
        bias = float(model.logit_bias.item()) if getattr(model, "logit_bias", None) is not None else 0.0
        del model
        return feats, scale, bias

    def image_vector(self, image) -> np.ndarray:
        torch = self._torch
        with self._lock, torch.no_grad():
            inputs = self.processor(images=image, return_tensors="pt").to(self.device)
            out = self.vision(**inputs)
            feats = out.pooler_output if self.family == "siglip" else out.image_embeds
            feats = torch.nn.functional.normalize(feats, dim=-1)
            return feats[0].float().cpu().numpy()

    def classify_image(self, image) -> KindScore:
        vec = self.image_vector(image)
        logits = self.scale * (self.text @ vec) + self.bias
        return scores_from_logits(self.groups, logits)

    def classify_jpeg(self, jpeg: bytes) -> KindScore:
        from PIL import Image  # noqa: PLC0415
        with Image.open(io.BytesIO(jpeg)) as im:  # exif-ok: кадр уже повёрнут по EXIF и пересохранён (normalize_photo)
            return self.classify_image(im.convert("RGB"))


_instance: KindClassifier | None = None
_failed: str | None = None
_init_lock = threading.Lock()


def get_classifier(settings) -> KindClassifier | None:
    """Одна модель на процесс; грузится при первом обращении. Сбой загрузки запоминается — каждый кадр не ждёт новой попытки."""
    global _instance, _failed
    repo = str(getattr(settings, "naming_kind_model", "") or "")
    if not repo:
        return None
    if _instance is not None and _instance.repo == repo:
        return _instance
    with _init_lock:
        if _instance is not None and _instance.repo == repo:
            return _instance
        if _failed == repo:
            return None
        try:
            device = str(getattr(settings, "ml_device", "") or "cpu")
            device = "cuda" if device in ("auto", "cuda") and _cuda() else "cpu"
            cache = Path(getattr(settings, "naming_kind_cache_dir", "") or "data/models")
            _instance = KindClassifier(repo, cache, device)
            logger.info("Вид товара: модель %s загружена (%s)", repo, device)
        except Exception as exc:  # noqa: BLE001 — нет модели на диске, нет torch: вид товара просто не называется
            _failed = repo
            logger.warning("Вид товара: модель %s не загружена (%s: %s)", repo, type(exc).__name__, exc)
            return None
    return _instance


def _cuda() -> bool:
    try:
        import torch  # noqa: PLC0415
        return bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001
        return False


def preload(settings) -> None:
    """Загрузить модель заранее (в фоне при запуске API). Ошибки — только в журнал."""
    try:
        get_classifier(settings)
    except Exception:  # noqa: BLE001
        logger.exception("Вид товара: предварительная загрузка не удалась")


def classify(jpeg: bytes, settings, min_score: float | None = None) -> KindScore | None:
    """Кадр -> вид товара или None (модель выключена/не загрузилась, «не товар», уверенность ниже порога).
    min_score — порог; None — из настроек (naming_kind_min_score). 0 — без порога (для сверки с большой моделью)."""
    model = get_classifier(settings)
    if model is None:
        return None
    try:
        result = model.classify_jpeg(jpeg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Вид товара: ошибка модели (%s)", type(exc).__name__)
        return None
    floor = float(getattr(settings, "naming_kind_min_score", 0.6)) if min_score is None else min_score
    if result.kind is None or result.score < floor:
        return None
    return result
