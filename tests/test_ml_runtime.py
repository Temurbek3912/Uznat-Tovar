"""app/pipeline/ml_runtime.py — запуск компонентов воркера без молчаливых подмен — и загрузка YOLO (YoloShelfDetector).

Фабрики и ultralytics ПОДДЕЛЬНЫЕ: проверяется политика (что обязательно, что можно заменить только по флагу, что видно в статусе),
а не настоящие модели. Настоящий запуск в Docker (Celery, YOLO, DINOv2) этими тестами не проверяется."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from app.pipeline import ml_runtime as mr

SETTINGS = types.SimpleNamespace(
    detector=types.SimpleNamespace(name="d", weights_path="models/detector/x.pt", confidence_threshold=0.35, iou_threshold=0.5, max_detections=1000),
    embedder=types.SimpleNamespace(name="e", weights_path="dinov2", embedding_dim=8),
    matcher=types.SimpleNamespace(index_path="models/index/c.faiss", top_k=5, match_threshold=0.7, unknown_threshold=0.4, margin_threshold=0.0),
)


def _factories(**broken):
    def maker(name, value):
        def make(*args):
            if name in broken:
                raise broken[name]
            return value
        return make
    return mr.Factories(
        face_blurrer=maker("face_blurrer", "blur"), detector=maker("detector", "yolo"), heuristic_detector=maker("heuristic", "heur"),
        embedder=maker("embedder", types.SimpleNamespace(embedding_dim=8)), debug_embedder=maker("debug", types.SimpleNamespace(embedding_dim=8)),
        index=maker("index", "index"), matcher=maker("matcher", "matcher"),
    )


def _build(env=None, **broken):
    return mr.build_runtime(SETTINGS, env or {}, _factories(**broken), resolve_path=lambda p: Path("/srv/app") / p)


def test_everything_ok_means_ready_and_every_component_is_reported():
    rt = _build()
    assert rt.ready and not rt.degraded and rt.problems() == []
    assert [c.name for c in rt.components] == ["face_blurrer", "detector", "embedder", "matcher"]       # OCR ценников больше не компонент
    assert not hasattr(rt, "price_ocr") and not hasattr(rt, "price_localizer")
    assert rt.component("detector").detail == f"YOLO: {Path('/srv/app') / 'models/detector/x.pt'}"            # путь из конфига разрешён от корня проекта
    assert "готов к работе" in "\n".join(rt.summary_lines())
    rt.require()                                                                               # не бросает


def test_a_failing_face_blurrer_no_longer_hides_the_other_components():
    rt = _build(face_blurrer=AttributeError("module 'cv2' has no attribute 'CascadeClassifier'"))
    assert not rt.ready
    assert rt.component("face_blurrer").status == mr.FAILED and "CascadeClassifier" in rt.component("face_blurrer").error
    assert [rt.component(n).status for n in ("detector", "embedder", "matcher")] == [mr.OK] * 3  # раньше после этой ошибки остальное было None
    assert rt.detector == "yolo" and rt.embedder is not None and rt.matcher == "matcher"
    with pytest.raises(mr.MLNotReadyError) as err:
        rt.require()
    assert "face_blurrer" in str(err.value) and "CascadeClassifier" in str(err.value)
    rt.require("detector", "embedder")                                                          # задачам, которым блюр не нужен, он не мешает


def test_yolo_failure_is_NOT_silently_replaced_by_the_heuristic_detector():
    rt = _build(detector=FileNotFoundError("weights"))
    assert not rt.ready and rt.detector is None
    assert "YOLO-детектор не загрузился" in rt.component("detector").error and mr.ALLOW_HEURISTIC_ENV in rt.component("detector").error


def test_the_heuristic_detector_only_by_an_explicit_flag_and_it_is_marked_degraded():
    rt = _build({mr.ALLOW_HEURISTIC_ENV: "1"}, detector=FileNotFoundError("weights"))
    assert rt.ready and rt.degraded and rt.detector == "heur"
    comp = rt.component("detector")
    assert comp.status == mr.DEGRADED and comp.detail.startswith("ЭВРИСТИКА вместо YOLO")
    assert "заменённые компоненты" in "\n".join(rt.summary_lines())


def test_embedder_failure_same_policy_random_embeddings_only_by_flag():
    assert not _build(embedder=RuntimeError("no internet")).ready
    rt = _build({mr.ALLOW_DEBUG_EMBEDDER_ENV: "1"}, embedder=RuntimeError("no internet"))
    assert rt.ready and rt.component("embedder").status == mr.DEGRADED and rt.component("embedder").detail.startswith("СЛУЧАЙНЫЕ")


def test_matcher_cannot_be_built_without_an_embedder():
    rt = _build(embedder=RuntimeError("x"))
    assert rt.component("matcher").status == mr.FAILED and "эмбеддер" in rt.component("matcher").error


def test_missing_detector_section_is_a_reported_failure_not_an_exception():
    settings = types.SimpleNamespace(detector=None, embedder=SETTINGS.embedder, matcher=SETTINGS.matcher)
    rt = mr.build_runtime(settings, {}, _factories(), resolve_path=lambda p: Path(p))
    assert not rt.ready and "models.detector не задан" in rt.component("detector").error


def test_status_file_round_trip_and_a_broken_runtime_is_not_ready(tmp_path):
    path = tmp_path / "s" / "status.json"
    mr.write_status(_build(), path)
    status = mr.read_status(path)
    assert status["ready"] is True and status["problems"] == [] and len(status["components"]) == 4 and status["pid"] > 0
    mr.write_status(_build(face_blurrer=RuntimeError("x")), path)
    assert mr.read_status(path)["ready"] is False and "face_blurrer" in mr.read_status(path)["problems"][0]
    mr.clear_status(path)
    assert mr.read_status(path) is None
    mr.clear_status(path)                                                                       # повторное удаление — не ошибка
    assert mr.status_path({mr.STATUS_ENV: "/x/y.json"}) == Path("/x/y.json") and mr.status_path({}) == Path(mr.DEFAULT_STATUS_FILE)


def test_preflight_stops_a_worker_with_broken_opencv_or_missing_weights(tmp_path):
    good_cv2 = types.SimpleNamespace(__version__="4.10.0")
    broken_cv2 = types.SimpleNamespace(__version__="5.0.0")                                     # как в контейнере: нет CascadeClassifier
    weights = tmp_path / "models" / "detector"
    weights.mkdir(parents=True)
    resolve = lambda p: tmp_path / p
    problems = mr.preflight(SETTINGS, {}, cv2_module=broken_cv2, resolve_path=resolve)
    assert any("CascadeClassifier" in p for p in problems) and any("файл весов детектора не найден" in p for p in problems)
    (weights / "x.pt").write_bytes(b"w")
    cascade_dir = tmp_path / "cv"
    cascade_dir.mkdir()
    (cascade_dir / "haarcascade_frontalface_default.xml").write_text("<x/>")
    ok_cv2 = types.SimpleNamespace(__version__="4.10.0", data=types.SimpleNamespace(haarcascades=str(cascade_dir)),
                                   CascadeClassifier=lambda path: types.SimpleNamespace(empty=lambda: False))
    assert mr.preflight(SETTINGS, {}, cv2_module=ok_cv2, resolve_path=resolve) == []
    (weights / "x.pt").unlink()
    assert mr.preflight(SETTINGS, {mr.ALLOW_HEURISTIC_ENV: "1"}, cv2_module=ok_cv2, resolve_path=resolve) == []   # осознанный режим без весов
    assert good_cv2 is not None


# ------------------------------------------------------------------ загрузка YOLO

class _FakeYOLO:
    loaded: list = []
    task = "detect"

    def __init__(self, path):
        _FakeYOLO.loaded.append(path)

    def predict(self, image, **kwargs):
        _FakeYOLO.kwargs = kwargs
        return []


def _with_fake_ultralytics(fn):
    saved = sys.modules.get("ultralytics")
    sys.modules["ultralytics"] = types.SimpleNamespace(YOLO=_FakeYOLO)
    _FakeYOLO.loaded = []
    try:
        return fn()
    finally:
        if saved is None:
            sys.modules.pop("ultralytics", None)
        else:
            sys.modules["ultralytics"] = saved


def test_yolo_detector_refuses_a_missing_file_before_ultralytics_could_download_something_else(tmp_path):
    from app.pipeline.detector import YoloShelfDetector
    with pytest.raises(FileNotFoundError) as err:
        _with_fake_ultralytics(lambda: YoloShelfDetector(str(tmp_path / "nope.pt")))
    assert "не найден" in str(err.value) and _FakeYOLO.loaded == []                            # YOLO() даже не вызывался


def test_yolo_detector_loads_exactly_the_requested_file_and_passes_max_det(tmp_path):
    import numpy as np
    from app.pipeline.detector import YoloShelfDetector
    weights = tmp_path / "sku110k_yolo11n.pt"
    weights.write_bytes(b"w")
    detector = _with_fake_ultralytics(lambda: YoloShelfDetector(str(weights), confidence_threshold=0.3, max_detections=900))
    assert _FakeYOLO.loaded == [str(weights)] and detector.weights_path == str(weights)
    detector.detect(np.zeros((10, 10, 3), np.uint8))
    assert _FakeYOLO.kwargs["max_det"] == 900 and _FakeYOLO.kwargs["conf"] == 0.3


def test_a_model_for_another_task_is_refused(tmp_path):
    from app.pipeline.detector import YoloShelfDetector
    weights = tmp_path / "seg.pt"
    weights.write_bytes(b"w")

    class SegYOLO(_FakeYOLO):
        task = "segment"

    saved = sys.modules.get("ultralytics")
    sys.modules["ultralytics"] = types.SimpleNamespace(YOLO=SegYOLO)
    try:
        with pytest.raises(RuntimeError) as err:
            YoloShelfDetector(str(weights))
    finally:
        if saved is None:
            sys.modules.pop("ultralytics", None)
        else:
            sys.modules["ultralytics"] = saved
    assert "segment" in str(err.value)


# ------------------------------------------------------------------ healthcheck и compose

def test_worker_health_script_reads_the_status_file(tmp_path):
    from scripts import worker_health as wh
    path = tmp_path / "status.json"
    saved = mr.os.environ.get(mr.STATUS_ENV)
    mr.os.environ[mr.STATUS_ENV] = str(path)
    try:
        lines = []
        assert wh.main(out=lines.append) == 1 and "нет файла статуса" in lines[0]           # воркер ещё стартует или упал
        mr.write_status(_build(face_blurrer=RuntimeError("no cascade")), path)
        lines.clear()
        assert wh.main(out=lines.append) == 1 and "НЕ готов" in lines[0] and "no cascade" in "\n".join(lines)
        mr.write_status(_build({mr.ALLOW_HEURISTIC_ENV: "1"}, detector=RuntimeError("x")), path)
        lines.clear()
        assert wh.main(out=lines.append) == 0 and "заменённые компоненты" in lines[0]
        mr.write_status(_build(), path)
        lines.clear()
        assert wh.main(out=lines.append) == 0 and lines == ["воркер готов"]
    finally:
        if saved is None:
            mr.os.environ.pop(mr.STATUS_ENV, None)
        else:
            mr.os.environ[mr.STATUS_ENV] = saved


def test_compose_worker_healthcheck_requires_readiness_not_just_ping():
    text = (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text(encoding="utf-8")
    worker = text[text.index("  worker:"):text.index("\nvolumes:\n  pgdata:")]            # воркер — последний сервис перед томами
    assert "python -m scripts.worker_health &&" in worker and "inspect ping" in worker
    assert "ALLOW_HEURISTIC_DETECTOR: ${ALLOW_HEURISTIC_DETECTOR:-0}" in text                  # по умолчанию подмена детектора выключена
