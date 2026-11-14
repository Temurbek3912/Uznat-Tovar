"""ml/benchmark_identify.py — сравнение распознавания по виду (документ 07, §5–6). Разделение по съёмкам, метрики (точные числа на
заранее заданных кандидатах), интервалы bootstrap по товарам, подбор порогов на отдельной съёмке, синтетический прогон и режим
настоящих фото — на подставном эмбеддере и картинках из tmp_path. Не проверяется: настоящие DINOv2 / SigLIP 2 / YOLO (их нет в среде)."""
from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.core.schemas import DetectionStatus
from ml import benchmark_identify as bi
from ml.benchmark_identify import Item, Query


def items(spec: dict[str, dict[str, int]], unknown=()):
    out = []
    for product, sessions in spec.items():
        for s, n in sessions.items():
            out += [Item(product, s, f"{product}/{s}/{k}.jpg", product not in unknown) for k in range(n)]
    return out


# ------------------------------------------------------------------ разделение по съёмкам

def test_split_never_puts_one_session_in_gallery_and_queries():
    data = items({"кола": {"1": 3, "2": 4}, "фанта": {"1": 2, "2": 2, "3": 2}, "чужой_1": {"1": 2, "2": 2}}, unknown={"чужой_1"})
    sp = bi.split_by_sessions(data)
    assert {(i.product, i.session) for i in sp.gallery} == {("кола", "1"), ("фанта", "1")}
    assert {(i.product, i.session) for i in sp.test} == {("кола", "2"), ("фанта", "2"), ("фанта", "3"), ("чужой_1", "1"),
                                                         ("чужой_1", "2")}
    assert not sp.tune
    assert all(i.known for i in sp.gallery)                                   # чужой товар в каталог не попадает


def test_split_explicit_gallery_and_tune_sessions():
    data = items({"a": {"day1": 2, "day2": 2, "day3": 2}, "b": {"day2": 2, "day3": 2}})
    sp = bi.split_by_sessions(data, gallery_session="day1", tune_sessions=["day2"])
    assert {(i.product, i.session) for i in sp.gallery} == {("a", "day1"), ("b", "day2")}       # у b нет day1 — взята первая
    assert {(i.product, i.session) for i in sp.tune} == {("a", "day2")}
    assert {(i.product, i.session) for i in sp.test} == {("a", "day3"), ("b", "day3")}
    assert any("нет съёмки «day1»" in w for w in sp.warnings)
    assert any("в подбор порогов не идёт" in w for w in sp.warnings)


def test_split_warns_about_product_with_single_session():
    sp = bi.split_by_sessions(items({"a": {"1": 3}, "b": {"1": 1, "2": 1}}))
    assert any("«a»: только одна съёмка" in w for w in sp.warnings)


@pytest.mark.parametrize("name,unknown", [("чужой_1", True), ("Unknown_3", True), ("неизвестный", True), ("нет_в_каталоге_2", True),
                                          ("кола", False), ("сок_чужого_бренда", False)])
def test_unknown_names(name, unknown):
    assert bi.is_unknown_name(name) is unknown


# ------------------------------------------------------------------ решение и метрики

def Q(product, ranked, known=True, session="2"):
    return Query(Item(product, session, f"{product}/{session}/{len(ranked)}{ranked[:1]}", known), list(ranked))


def test_judge_uses_decide_match_and_marks_wrong_confident_answers():
    ok = bi.judge(Q("a", [("a", 0.9), ("b", 0.5)]), 0.72, 0.45, 0.0)
    assert ok.status == DetectionStatus.MATCHED and ok.top1 and ok.top5 and not ok.wrong_matched
    wrong = bi.judge(Q("a", [("b", 0.9), ("a", 0.8)]), 0.72, 0.45, 0.0)
    assert wrong.matched and wrong.wrong_matched and not wrong.top1 and wrong.top5
    crowded = bi.judge(Q("a", [("b", 0.9), ("a", 0.88)]), 0.72, 0.45, 0.05)             # запас спасает от уверенной ошибки
    assert crowded.status == DetectionStatus.UNCERTAIN and not crowded.wrong_matched
    alien = bi.judge(Q("x", [("a", 0.8)], known=False), 0.72, 0.45, 0.0)
    assert alien.wrong_matched and not alien.top1 and not alien.top5                   # «узнал» чужой товар — уверенная ошибка
    far = bi.judge(Q("a", [(f"p{i}", 0.6 - i / 100) for i in range(6)] + [("a", 0.1)]), 0.72, 0.45, 0.0)
    assert not far.top5 and far.status == DetectionStatus.UNCERTAIN


def test_bootstrap_point_values_are_exact():
    outs = [bi.judge(q, 0.72, 0.45, 0.0) for q in [
        Q("a", [("a", 0.9)]), Q("a", [("a", 0.6)]),                                   # верно уверенно; верно, но не уверенно
        Q("b", [("a", 0.9), ("b", 0.8)]),                                              # уверенная ошибка, top-5 есть
        Q("b", [("b", 0.5)]),                                                          # не уверенно
        Q("x", [("a", 0.8)], known=False), Q("x", [("a", 0.3)], known=False), Q("y", [("b", 0.4)], known=False),
    ]]
    e = bi.bootstrap(outs, rounds=200)
    assert (e["top1"].num, e["top1"].den) == (3, 4)
    assert (e["top5"].num, e["top5"].den) == (4, 4)
    assert (e["coverage"].num, e["coverage"].den) == (2, 4)
    assert (e["risk"].num, e["risk"].den) == (2, 3)                                    # (1 ошибка + 1 чужой) / 3 уверенных
    assert (e["reject"].num, e["reject"].den) == (2, 3)
    assert (e["false_accept"].num, e["false_accept"].den) == (1, 3)
    for est in e.values():
        assert est.low <= est.value <= est.high


def test_bootstrap_is_by_product_not_by_photo():
    """10 фото одного товара все неверны, у 9 других товаров — по одному верному фото. По фото интервал был бы узким;
    по товарам он широкий — фото одного товара не независимы."""
    qs = [Q("bad", [("other", 0.9)], session=str(k)) for k in range(10)]
    qs += [Q(f"g{i}", [(f"g{i}", 0.9)]) for i in range(9)]
    e = bi.bootstrap([bi.judge(q, 0.72, 0.45, 0.0) for q in qs], rounds=2000, seed=1)["top1"]
    assert e.value == pytest.approx(9 / 19)
    assert e.high - e.low > 0.6                                                       # товар «bad» то есть в выборке, то нет
    assert e.low < 0.25 and e.high > 0.9                                              # по фото было бы около [0.25; 0.70]


def test_bootstrap_is_reproducible_with_seed():
    qs = [bi.judge(Q(f"p{i}", [(f"p{i % 3}", 0.8)]), 0.72, 0.45, 0.0) for i in range(12)]
    assert bi.bootstrap(qs, 300, seed=5) == bi.bootstrap(qs, 300, seed=5)


def test_zero_errors_upper_bound_is_rule_of_three_not_zero():
    outs = [bi.judge(Q(f"p{i}", [(f"p{i}", 0.95)]), 0.72, 0.45, 0.0) for i in range(100)]
    risk = bi.bootstrap(outs, 500)["risk"]
    assert risk.value == 0.0 and risk.low == 0.0
    assert risk.high == pytest.approx(bi.zero_error_upper(100)) and 0.029 < risk.high < 0.031
    assert "нужно не меньше 300" in bi.risk_verdict(risk)
    assert bi.zero_error_upper(300) <= 0.01 + 1e-9                                    # отсюда и «300 уверенных без ошибки» (§5.2)
    top1 = bi.bootstrap(outs, 500)["top1"]
    assert top1.value == 1.0 and top1.low == pytest.approx(1 - bi.zero_error_upper(100))


def test_empty_denominator_gives_dash():
    e = bi.bootstrap([bi.judge(Q("a", [("a", 0.9)]), 0.72, 0.45, 0.0)], 50)
    assert e["reject"].value is None and bi.cell(e["reject"]) == "—"


# ------------------------------------------------------------------ подбор порогов

def test_tune_thresholds_reaches_target_on_tune_set_and_prefers_strict_on_ties():
    qs = [Q(f"p{i}", [(f"p{i}", 0.80), ("z", 0.50)]) for i in range(50)]
    qs += [Q("x", [("p1", 0.70)], known=False, session=str(k)) for k in range(5)]     # чужой товар со счётом 0.70
    th = bi.tune_thresholds(qs)
    assert 0.70 < th.match <= 0.80                                                    # чужой отсечён, свои приняты
    assert th.match == 0.80 and th.margin == bi.MARGIN_GRID[-1]                       # при равной доле — самые строгие
    assert bi.tune_thresholds([]) is None


def test_tune_thresholds_reports_when_target_unreachable():
    qs = [Q("a", [("b", 0.99)]), Q("b", [("a", 0.99)])]
    th = bi.tune_thresholds(qs)
    assert "не нашёл" in th.how and th.match == bi.MATCH_GRID[-1]


# ------------------------------------------------------------------ синтетика: проверка инструмента

def test_synthetic_run_produces_report_with_intervals_and_orders_difficulty(tmp_path):
    split, results = bi.run_synthetic(rounds=200, seed=0)
    assert [r.name for r in results] == ["synthetic-easy", "synthetic-medium", "synthetic-hard"]
    top1 = [r.estimates[0]["top1"].value for r in results]
    assert top1[0] >= top1[1] >= top1[2] and top1[0] > top1[2]                         # трудная действительно труднее
    for r in results:
        assert len(r.thresholds) == 2 and "подобраны" in r.thresholds[1].how or "не нашёл" in r.thresholds[1].how
        assert {o.query.item.session for outs in r.outcomes for o in outs} == {"C"}     # итог только на контрольной съёмке
    report = bi.build_report("synthetic", split, results, rounds=200)
    assert "Это не оценка моделей" in report and "[" in report and "правило трёх" in report
    rep, rows = bi.save(tmp_path, report, results)
    assert rep.read_text(encoding="utf-8") == report
    with open(rows, encoding="utf-8-sig") as fh:
        table = list(csv.reader(fh, delimiter=";"))
    assert table[0][0] == "конфигурация" and len(table) > 100


def test_synthetic_dataset_is_deterministic_and_unit_norm():
    spec = bi.SYNTHETIC_SPECS[1]
    a_items, a = bi.synthetic_dataset(spec, products=6, unknown=2, seed=3)
    b_items, b = bi.synthetic_dataset(spec, products=6, unknown=2, seed=3)
    assert a_items == b_items and np.array_equal(a, b)
    assert np.allclose(np.linalg.norm(a, axis=1), 1.0, atol=1e-5)
    assert not any(not i.known and i.session == "A" for i in a_items)                 # чужие — только в запросах


def test_cli_synthetic_writes_files(tmp_path, capsys):
    assert bi.main(["synthetic", "--out", str(tmp_path / "s"), "--rounds", "50"]) == 0
    assert (tmp_path / "s" / "report.md").is_file() and (tmp_path / "s" / "queries.csv").is_file()
    assert "Сводка" in capsys.readouterr().out


# ------------------------------------------------------------------ обработка кадра

def test_center_crop_and_pad_square_shapes():
    img = np.zeros((200, 100, 3), dtype=np.uint8)
    img[80:120, 40:60] = 255
    c = bi.center_crop(img, 0.6)
    assert c.shape == (120, 60, 3) and c.max() == 255
    p = bi.pad_square(img)
    assert p.shape == (200, 200, 3) and p[0, 0, 0] == 128 and p[100, 100, 0] == 255
    assert bi.full_frame(img) is img


# ------------------------------------------------------------------ режим настоящих фото (подставной эмбеддер)

COLORS = {"кола": (200, 30, 30), "кола_зеро": (40, 40, 40), "фанта": (240, 140, 20), "спрайт": (30, 180, 60),
          "чужой_1": (30, 60, 220)}


def make_dataset(root: Path, sessions=("1", "2"), per=3):
    rng = np.random.default_rng(0)
    for product, color in COLORS.items():
        for s in sessions:
            d = root / product / s
            d.mkdir(parents=True)
            for k in range(per):
                arr = np.clip(np.array(color) + rng.normal(0, 12, (40, 30, 3)), 0, 255).astype(np.uint8)
                Image.fromarray(arr).save(d / f"{k}.jpg")
    (root / "кола" / "1" / "заметка.txt").write_text("не фото", encoding="utf-8")
    (root / "кола" / "1" / "битое.jpg").write_bytes(b"not a jpeg")


class ColorEmbedder:
    """Подставная модель: средний цвет по сетке 2×2 — похожие цвета дают близкие векторы."""

    def __init__(self):
        self.calls = 0

    def embed(self, images):
        self.calls += 1
        out = []
        for im in images:
            h, w = im.shape[:2]
            cells = [im[y:y + h // 2, x:x + w // 2].reshape(-1, 3).mean(0) for y in (0, h // 2) for x in (0, w // 2)]
            v = np.concatenate(cells).astype(np.float32) - 120.0
            out.append(v / np.linalg.norm(v))
        return np.stack(out)


def test_photos_mode_runs_available_configs_and_skips_missing_honestly(tmp_path):
    make_dataset(tmp_path)
    loaded = []

    def loader(key, device):
        loaded.append(key)
        return ColorEmbedder()

    def availability(key):
        return None if key == "dinov2-base" else "не установлены библиотеки: torch"

    configs = [c for c in bi.PHOTO_CONFIGS if c.transform != "yolo"]
    split, results, warnings = bi.run_photos(tmp_path, configs, rounds=100, loader=loader, availability=availability,
                                             detector_weights=tmp_path / "нет.pt")
    assert loaded == ["dinov2-base"]                                                  # одна загрузка на все её конфигурации
    by = {r.name: r for r in results}
    assert by["dinov2-small/full"].skipped and "torch" in by["dinov2-small/full"].skipped
    assert by["siglip2-base/full"].skipped
    full = by["dinov2-base/full"]
    assert not full.skipped and full.ms_per_image is not None
    e = full.estimates[0]
    assert e["top1"].den == 4 * 3 and e["top1"].value == 1.0                          # 4 известных товара × 3 фото съёмки «2»
    assert e["reject"].den == 6                                                       # чужой товар: обе съёмки — запросы
    assert any("не прочитано фото: 1" in n for n in full.notes)
    assert any("не JPG/PNG" in w for w in warnings)
    report = bi.build_report("photos", split, results, warnings, 100, source=str(tmp_path))
    assert "**Пропущено:** не установлены библиотеки: torch" in report and "## Сводка" in report


def test_photos_mode_yolo_row_crops_only_single_box(tmp_path):
    make_dataset(tmp_path, per=2)
    from types import SimpleNamespace as NS

    class OneBoxDetector:
        def __init__(self):
            self.n = 0

        def detect(self, image):
            self.n += 1
            if self.n % 2:
                return [NS(bbox=NS(x1=2, y1=2, x2=20, y2=30), confidence=0.9)]
            return []

    yolo = [c for c in bi.PHOTO_CONFIGS if c.transform == "yolo"]
    _, results, _ = bi.run_photos(tmp_path, yolo, rounds=50, loader=lambda k, d: ColorEmbedder(), availability=lambda k: None,
                                  detector_loader=lambda w: OneBoxDetector())
    note = " ".join(results[0].notes)
    assert not results[0].skipped and "одна рамка на" in note and "нет рамки" in note


def test_photos_mode_without_detector_weights_skips_yolo_row(tmp_path):
    make_dataset(tmp_path, per=1)
    yolo = [c for c in bi.PHOTO_CONFIGS if c.transform == "yolo"]
    _, results, _ = bi.run_photos(tmp_path, yolo, rounds=20, loader=lambda k, d: ColorEmbedder(), availability=lambda k: None,
                                  detector_weights=tmp_path / "нет.pt")
    assert results[0].skipped and "нет файла весов детектора" in results[0].skipped


def test_photos_mode_needs_two_sessions(tmp_path):
    make_dataset(tmp_path, sessions=("1",), per=1)
    with pytest.raises(ValueError, match="две съёмки"):
        bi.run_photos(tmp_path, bi.PHOTO_CONFIGS[:1], loader=lambda k, d: ColorEmbedder(), availability=lambda k: None)


def test_photos_mode_does_not_touch_photos(tmp_path):
    make_dataset(tmp_path, per=1)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    bi.run_photos(tmp_path, bi.PHOTO_CONFIGS[:1], rounds=20, loader=lambda k, d: ColorEmbedder(), availability=lambda k: None)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_cli_photos_reports_missing_folder_and_bad_config(tmp_path, capsys):
    assert bi.main(["photos", "--photos", str(tmp_path / "нет"), "--out", str(tmp_path / "o")]) == 1
    assert "не найдена" in capsys.readouterr().out
    assert bi.main(["photos", "--photos", str(tmp_path), "--configs", "gpt/full"]) == 2


def test_model_availability_without_ml_libraries(monkeypatch):
    monkeypatch.setattr(bi.importlib.util, "find_spec", lambda name: None)
    assert "не установлены библиотеки: torch, transformers" in bi.model_availability("siglip2-base", allow_download=True)
    assert "ultralytics" in (bi.detector_availability(Path(__file__)) or "")


def test_cli_list_prints_every_config(capsys):
    assert bi.main(["list"]) == 0
    out = capsys.readouterr().out
    assert all(c.name in out for c in bi.PHOTO_CONFIGS)


def test_hub_cache_detection(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    assert not bi._hub_cached("facebook/dinov2-small")
    snap = tmp_path / "models--facebook--dinov2-small" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}", encoding="utf-8")
    assert bi._hub_cached("facebook/dinov2-small")
    monkeypatch.setattr(bi, "_missing_libs", lambda libs: [])
    assert bi.model_availability("dinov2-small", allow_download=False) is None
    assert "не скачаны" in bi.model_availability("siglip2-base", allow_download=False)
