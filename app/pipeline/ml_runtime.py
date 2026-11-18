"""
Запуск ML-компонентов воркера: детектор, эмбеддер, индекс/матчер, размытие лиц. Без Celery и без тяжёлых
импортов на уровне модуля — логику можно проверить офлайн (tests/test_ml_runtime.py), а реальные классы подставляются
фабриками по умолчанию лениво.

Что было не так. `_init_ml_components` создавал FaceBlurrer ПЕРВОЙ строкой и без защиты: любая ошибка прерывала функцию, остальные
компоненты (детектор, эмбеддер, матчер) оставались None, Celery только писал «Signal handler raised» и объявлял «ready».
Кроме того, при сбое YOLO детектор молча заменялся эвристическим — «работающая» система с другим, куда худшим детектором.

Политика теперь:
  * каждый компонент создаётся независимо — сбой одного не скрывает состояние остальных; в логе одна итоговая таблица;
  * ОБЯЗАТЕЛЬНЫЕ компоненты (размытие лиц, детектор, эмбеддер, индекс) не заменяются молча. Подмена — только по явному флагу
    (ALLOW_HEURISTIC_DETECTOR=1, ALLOW_DEBUG_EMBEDDER=1), помечается как «degraded» и пишется в лог уровнем ERROR;
  * необязательный компонент (сейчас таких нет) при сбое «skipped» — с причиной в логе и в статусе;
  * задачи перед работой вызывают runtime.require(...): не готов обязательный компонент — MLNotReadyError с перечнем причин,
    а не успешный ответ, которого не было;
  * результат пишется в файл статуса (ML_STATUS_FILE) — по нему healthcheck контейнера отличает «воркер жив» от «воркер готов».
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

STATUS_ENV = "ML_STATUS_FILE"
DEFAULT_STATUS_FILE = "/tmp/shelf_audit_ml_status.json"      # внутри контейнера воркера: пересоздание контейнера = чистый статус
ALLOW_HEURISTIC_ENV = "ALLOW_HEURISTIC_DETECTOR"
ALLOW_DEBUG_EMBEDDER_ENV = "ALLOW_DEBUG_EMBEDDER"

OK, DEGRADED, SKIPPED, FAILED = "ok", "degraded", "skipped", "failed"


class MLNotReadyError(RuntimeError):
    """Обязательный компонент распознавания не готов. problems — причины простыми словами (по одной на компонент)."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclass
class Component:
    name: str
    required: bool
    status: str = FAILED
    detail: str = ""
    error: str | None = None

    def as_dict(self) -> dict:
        return {"name": self.name, "required": self.required, "status": self.status, "detail": self.detail, "error": self.error}


@dataclass
class MLRuntime:
    components: list[Component] = field(default_factory=list)
    detector: Any = None
    embedder: Any = None
    matcher: Any = None
    face_blurrer: Any = None

    def component(self, name: str) -> Component:
        return next(c for c in self.components if c.name == name)

    def failed_required(self) -> list[Component]:
        return [c for c in self.components if c.required and c.status == FAILED]

    @property
    def ready(self) -> bool:
        return bool(self.components) and not self.failed_required()

    @property
    def degraded(self) -> bool:
        return any(c.status == DEGRADED for c in self.components)

    def problems(self) -> list[str]:
        return [f"{c.name}: {c.error}" for c in self.failed_required()]

    def require(self, *names: str) -> None:
        """Бросает MLNotReadyError, если не готов хоть один из названных (без имён — любой обязательный) компонентов."""
        wanted = [c for c in self.components if c.required and (not names or c.name in names)]
        bad = [f"{c.name}: {c.error or 'не инициализирован'}" for c in wanted if c.status == FAILED]
        if bad:
            raise MLNotReadyError(bad)

    def summary_lines(self) -> list[str]:
        lines = ["Компоненты распознавания:"]
        for c in self.components:
            mark = {OK: "OK      ", DEGRADED: "ЗАМЕНЁН ", SKIPPED: "пропущен", FAILED: "СБОЙ    "}[c.status]
            tail = c.error if c.error else c.detail
            lines.append(f"  [{mark}] {c.name}{'' if c.required else ' (необязательный)'}: {tail}")
        lines.append("ИТОГ: " + ("готов к работе" if self.ready else "НЕ ГОТОВ — задачи распознавания будут отклоняться с понятной причиной")
                     + (" (есть заменённые компоненты — результаты менее точны)" if self.ready and self.degraded else ""))
        return lines

    def to_dict(self) -> dict:
        return {"ready": self.ready, "degraded": self.degraded, "pid": os.getpid(), "updated_at": time.time(),
                "components": [c.as_dict() for c in self.components], "problems": self.problems()}


# --------------------------------------------------------------------------- фабрики (реальные классы — лениво)

@dataclass
class Factories:
    face_blurrer: Callable[[], Any]
    detector: Callable[[str, Any], Any]                  # (абсолютный путь весов, DetectorConfig) -> детектор
    heuristic_detector: Callable[[], Any]
    embedder: Callable[[Any], Any]                       # EmbedderConfig -> эмбеддер
    debug_embedder: Callable[[], Any]
    index: Callable[..., Any]                            # (index_path, размерность, паспорт модели | None) -> индекс
    matcher: Callable[[Any, Any], Any]                   # (индекс, MatcherConfig|None) -> ProductMatcher


def default_factories() -> Factories:
    def face_blurrer():
        from app.pipeline.privacy import FaceBlurrer
        return FaceBlurrer()

    def detector(weights_path, cfg):
        from app.pipeline.detector import YoloShelfDetector
        return YoloShelfDetector(weights_path=weights_path, confidence_threshold=cfg.confidence_threshold,
                                 iou_threshold=cfg.iou_threshold, max_detections=getattr(cfg, "max_detections", 1000))

    def heuristic_detector():
        from app.pipeline.detector import HeuristicContourDetector
        return HeuristicContourDetector()

    def embedder(cfg):
        from app.pipeline.embedder import create_embedder
        device, threads = embedder_device(os.environ)
        if threads:
            import torch
            torch.set_num_threads(threads)
        return create_embedder(cfg, device=device)

    def debug_embedder():
        from app.pipeline.embedder import RandomDebugEmbedder
        return RandomDebugEmbedder()

    def index(index_path, dim, model=None):
        from app.worker import _load_or_create_index
        return _load_or_create_index(index_path, dim, model)

    def matcher(idx, cfg):
        from app.pipeline.matcher import ProductMatcher
        return ProductMatcher(
            index=idx, top_k=cfg.top_k if cfg else 5, match_threshold=cfg.match_threshold if cfg else 0.72,
            unknown_threshold=cfg.unknown_threshold if cfg else 0.45, margin_threshold=cfg.margin_threshold if cfg else 0.0,
        )

    return Factories(face_blurrer, detector, heuristic_detector, embedder, debug_embedder, index, matcher)


def embedder_device(env: Mapping[str, str]) -> tuple[str, int]:
    """(устройство, потоки) для модели по виду из ML_DEVICE (auto | cpu | cuda) и ML_THREADS. Неверное значение — auto / 0."""
    device = str(env.get("ML_DEVICE", "auto")).strip().lower()
    if device not in ("auto", "cpu", "cuda"):
        device = "auto"
    try:
        threads = max(0, int(str(env.get("ML_THREADS", "0")).strip() or 0))
    except ValueError:
        threads = 0
    return ("cuda" if device == "auto" else device), threads


def _flag(env: Mapping[str, str], name: str) -> bool:
    return str(env.get(name, "")).strip() == "1"


def _resolve(path: str, resolve_path: Callable[[str], Path] | None) -> str:
    if resolve_path is None:
        from app.core.config import resolve_project_path
        resolve_path = resolve_project_path
    return str(resolve_path(path))


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- сборка

def build_runtime(settings, env: Mapping[str, str] | None = None, factories: Factories | None = None,
                  resolve_path: Callable[[str], Path] | None = None) -> MLRuntime:
    """Создаёт ВСЕ компоненты независимо друг от друга. Не бросает исключений (кроме BaseException): итог — в runtime.components."""
    env = os.environ if env is None else env
    f = factories or default_factories()
    rt = MLRuntime()

    def attempt(name: str, required: bool, make: Callable[[], tuple[Any, str]]) -> Any:
        comp = Component(name, required)
        rt.components.append(comp)
        try:
            value, detail = make()
            comp.status, comp.detail = OK, detail
            return value
        except Exception as exc:  # noqa: BLE001 — граница компонента: причина уходит в статус и лог, остальные компоненты продолжают
            comp.status, comp.error = (FAILED if required else SKIPPED), _describe(exc)
            logger.error("Компонент «%s»: %s", name, comp.error, exc_info=True)
            return None

    # 1) Приватность: размытие лиц обязательно и ничем не заменяется
    rt.face_blurrer = attempt("face_blurrer", True, lambda: (f.face_blurrer(), "каскад лиц загружен"))

    # 2) Детектор
    def make_detector():
        cfg = settings.detector
        if cfg is None:
            raise RuntimeError("models.detector не задан в config.yaml")
        weights = _resolve(cfg.weights_path, resolve_path)
        try:
            return f.detector(weights, cfg), f"YOLO: {weights}"
        except Exception as exc:  # noqa: BLE001
            if not _flag(env, ALLOW_HEURISTIC_ENV):
                raise RuntimeError(
                    f"YOLO-детектор не загрузился ({_describe(exc)}). Проверьте файл весов {weights}; сознательная работа без "
                    f"обученной модели (только демо, качество низкое) — {ALLOW_HEURISTIC_ENV}=1."
                ) from exc
            logger.error("YOLO-детектор недоступен (%s). %s=1: ВМЕСТО НЕГО работает эвристический детектор контуров — "
                         "он не предназначен для реальных полок, результаты ненадёжны!", _describe(exc), ALLOW_HEURISTIC_ENV)
            return f.heuristic_detector(), f"ЭВРИСТИКА вместо YOLO ({ALLOW_HEURISTIC_ENV}=1; причина: {_describe(exc)})"

    # Детектор полок больше не обязателен (2026-11-14): «Узнать товар» работает с одним товаром в кадре и детектор не вызывает
    # (документ 07, §3.1, §7 — лицензия YOLO). Не загрузился — компонент «пропущен», воркер всё равно готов.
    rt.detector = attempt("detector", False, make_detector)
    if rt.detector is not None and rt.component("detector").detail.startswith("ЭВРИСТИКА"):
        rt.component("detector").status = DEGRADED

    # 3) Эмбеддер
    def make_embedder():
        cfg = settings.embedder
        try:
            if cfg is None:
                raise RuntimeError("models.embedder не задан в config.yaml")
            emb = f.embedder(cfg)
            device = getattr(emb, "device", None)
            return emb, f"DINOv2: {cfg.weights_path}" + (f" (считает: {'видеокарта' if device == 'cuda' else 'процессор'})"
                                                         if isinstance(device, str) else "")
        except Exception as exc:  # noqa: BLE001
            if not _flag(env, ALLOW_DEBUG_EMBEDDER_ENV):
                raise RuntimeError(
                    f"DINOv2-эмбеддер недоступен ({_describe(exc)}). Проверьте интернет (первый запуск скачивает веса) или "
                    f"models.embedder.weights_path; для отладки без весов — {ALLOW_DEBUG_EMBEDDER_ENV}=1."
                ) from exc
            logger.error("DINOv2-эмбеддер недоступен (%s). %s=1: используются СЛУЧАЙНЫЕ эмбеддинги — результаты сопоставления "
                         "товаров бессмысленны, только для отладки!", _describe(exc), ALLOW_DEBUG_EMBEDDER_ENV)
            return f.debug_embedder(), f"СЛУЧАЙНЫЕ эмбеддинги ({ALLOW_DEBUG_EMBEDDER_ENV}=1; причина: {_describe(exc)})"

    rt.embedder = attempt("embedder", True, make_embedder)
    if rt.embedder is not None and rt.component("embedder").detail.startswith("СЛУЧАЙНЫЕ"):
        rt.component("embedder").status = DEGRADED

    # 4) Индекс каталога + матчер (пустой индекс — нормальное состояние до первого товара)
    def make_matcher():
        if rt.embedder is None:
            raise RuntimeError("не создан эмбеддер — размерность индекса неизвестна")
        cfg = settings.matcher
        index_path = cfg.index_path if cfg else None
        from app.pipeline.embedder import model_signature  # noqa: PLC0415 — лёгкий модуль, без torch
        model = model_signature(rt.embedder)
        index = f.index(index_path, getattr(rt.embedder, "embedding_dim", 64), model)
        return f.matcher(index, cfg), f"индекс: {index_path or 'в памяти'}; {_model_note(index, model)}"

    rt.matcher = attempt("matcher", True, make_matcher)
    return rt


def _model_note(index, model: dict | None) -> str:
    """Строка для таблицы компонентов: какой моделью построен индекс (или что это не проверить)."""
    stored = getattr(index, "model", None)
    if isinstance(stored, dict):
        return f"модель индекса: {stored.get('name')} ({stored.get('weights')})"
    if model is None:
        return "модель индекса не проверена: эмбеддер не сообщает свою версию"
    return "модель индекса НЕ ЗАПИСАНА (индекс до 2026-11-14) — рекомендуется полная пересборка"


# --------------------------------------------------------------------------- проверка до старта воркера

def preflight(settings, env: Mapping[str, str] | None = None, cv2_module=None,
              resolve_path: Callable[[str], Path] | None = None) -> list[str]:
    """Детерминированные причины, по которым воркеру НЕЛЬЗЯ становиться готовым (проверяются в главном процессе до пула):
    неисправный OpenCV (нет CascadeClassifier), не задан/не найден детектор. Пустой список — можно стартовать."""
    env = os.environ if env is None else env
    problems: list[str] = []
    if cv2_module is None:
        try:
            import cv2 as cv2_module  # noqa: PLC0415
        except ImportError as exc:
            return [f"cv2 не импортируется: {exc}"]
    from app.core.opencv_check import capability_problems
    problems += [f"OpenCV: {p}" for p in capability_problems(cv2_module)]
    # Детектор полок не обязателен (см. build_runtime): нет весов — не повод не запускать воркер «Узнать товар».
    return problems


# --------------------------------------------------------------------------- файл статуса (для healthcheck)

def status_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get(STATUS_ENV) or DEFAULT_STATUS_FILE)


def write_status(runtime: MLRuntime, path: Path | None = None) -> None:
    target = path or status_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".part")
        tmp.write_text(json.dumps(runtime.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        logger.warning("Не удалось записать файл статуса %s: %s (healthcheck не увидит готовность)", target, exc)


def read_status(path: Path | None = None) -> dict | None:
    try:
        return json.loads((path or status_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def clear_status(path: Path | None = None) -> None:
    try:
        (path or status_path()).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Не удалось удалить старый файл статуса: %s", exc)
