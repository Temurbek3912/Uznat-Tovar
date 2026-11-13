"""
ml/evaluate_matching.py — оценка распознавания товаров и подбор порогов. Ядро проверяется на заранее посчитанных списках
кандидатов (точные ожидаемые числа), сбор выборки — на настоящих фото/разметке и настоящем NumpyCatalogIndex.
Не проверяется: настоящий DINOv2 и командная строка с загрузкой моделей.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from app.pipeline.matcher import NumpyCatalogIndex, ProductMatcher
from ml import evaluate_matching as em
from ml.evaluate_matching import (
    Sample, build_report, collect_samples, confusions, evaluate_point, percentile, recommend, score_distributions, sweep, top_k_accuracy,
)

ID2SKU = {"pa": "A", "pb": "B", "pc": "C"}


def S(truth, *ranked, src=""):
    return Sample(truth, list(ranked), src)


# ----------------------------------------------------------------------- точка работы

def test_evaluate_point_counts_every_outcome_exactly():
    samples = [
        S("A", ("pa", 0.90), ("pb", 0.50)),        # принято верно
        S("A", ("pb", 0.85), ("pa", 0.80)),        # принято НЕВЕРНО (самая дорогая ошибка)
        S("B", ("pb", 0.60), ("pa", 0.40)),        # между порогами -> ревью
        S("C", ("pa", 0.20)),                      # известный, но «неизвестен» -> потеря
        S(None, ("pa", 0.80)),                     # ЧУЖОЕ принято как своё
        S(None, ("pa", 0.20)),                     # чужое верно признано неизвестным
    ]
    p = evaluate_point(samples, 0.72, 0.45, 0.0, ID2SKU)
    assert (p.n, p.auto_correct, p.auto_wrong, p.unknown_accepted, p.review, p.rejected_known, p.unknown_correct) == (6, 1, 1, 1, 1, 1, 1)
    assert p.accepted == 3 and p.auto_rate == 0.5 and p.precision == 1 / 3 and p.error_rate == 2 / 6 and p.review_rate == 1 / 6


def test_margin_moves_a_confident_but_crowded_match_into_review():
    crowded = S("A", ("pa", 0.90), ("pb", 0.88))
    assert evaluate_point([crowded], 0.72, 0.45, 0.0, ID2SKU).auto_correct == 1
    p = evaluate_point([crowded], 0.72, 0.45, 0.05, ID2SKU)
    assert (p.auto_correct, p.review) == (0, 1)


def test_empty_sample_has_safe_rates():
    p = evaluate_point([], 0.7, 0.4, 0.0, ID2SKU)
    assert (p.auto_rate, p.precision, p.error_rate, p.review_rate) == (0.0, 1.0, 0.0, 0.0)


# ----------------------------------------------------------------------- таблица порогов и рекомендация

def _mixed_samples():
    right = [S("A", ("pa", 0.95 - i * 0.01), ("pb", 0.30)) for i in range(20)]            # уверенно верные
    shaky_right = [S("B", ("pb", 0.70), ("pa", 0.20)) for _ in range(10)]                 # верные, но с невысоким счётом
    wrong_high = [S("A", ("pb", 0.80), ("pa", 0.78)) for _ in range(5)]                    # ошибки с высоким счётом, но ТЕСНЫЕ
    return right + shaky_right + wrong_high


def test_sweep_covers_the_grid_and_respects_the_unknown_floor():
    pts = sweep(_mixed_samples(), ID2SKU, 0.45, match_grid=(0.40, 0.60, 0.80), margin_grid=(0.0, 0.1))
    assert [(p.match_threshold, p.margin) for p in pts] == [(0.60, 0.0), (0.80, 0.0), (0.60, 0.1), (0.80, 0.1)]     # 0.40 < unknown — отброшен


def test_recommend_prefers_the_highest_coverage_that_meets_target_precision():
    pts = sweep(_mixed_samples(), ID2SKU, 0.45)
    best, met = recommend(pts, target_precision=0.98)
    assert met and best.precision >= 0.98
    assert best.margin > 0                         # без запаса тесные ошибки (0.80 vs 0.78) принимаются -> точность ниже цели
    assert all(p.auto_rate <= best.auto_rate for p in pts if p.precision >= 0.98)
    best_nm, met_nm = recommend([p for p in pts if p.margin == 0.0], 0.98)         # без запаса цель достижима лишь высоким порогом -> охват ниже
    assert met_nm and best_nm.match_threshold > 0.80 and best_nm.auto_rate < best.auto_rate


def test_recommend_reports_an_unreachable_target_and_empty_input():
    bad = [S("A", ("pb", 0.9)), S("A", ("pb", 0.9)), S("A", ("pa", 0.9))]                   # 2 из 3 принятых — неверные
    best, met = recommend(sweep(bad, ID2SKU, 0.45), target_precision=0.99)
    assert met is False and best is not None and best.precision < 0.99
    assert recommend([], 0.9) == (None, False)
    assert recommend(sweep([S("A", ("pa", 0.1))], ID2SKU, 0.45), 0.9) == (None, False)      # никто не принят ни при каких порогах


def test_recommendation_tie_break_prefers_simpler_settings():
    samples = [S("A", ("pa", 0.9), ("pb", 0.1)) for _ in range(10)]                         # одинаково хорошо при любом запасе
    best, _ = recommend(sweep(samples, ID2SKU, 0.45), 0.98)
    assert best.margin == 0.0 and best.auto_rate == 1.0


# ----------------------------------------------------------------------- точность, путаницы, распределения

def test_top_k_accuracy_and_confusions_ignore_unknown_samples():
    samples = [S("A", ("pa", 0.9)), S("A", ("pb", 0.8), ("pa", 0.7)), S("B", ("pc", 0.9), ("pa", 0.5), ("pb", 0.4)), S(None, ("pa", 0.5))]
    assert top_k_accuracy(samples, ID2SKU, 1) == 1 / 3
    assert top_k_accuracy(samples, ID2SKU, 2) == 2 / 3
    assert top_k_accuracy(samples, ID2SKU, 3) == 1.0
    assert top_k_accuracy([S(None, ("pa", 0.5))], ID2SKU) is None
    assert confusions(samples, ID2SKU) == [("A", "B", 1), ("B", "C", 1)]                     # при равенстве — в порядке появления
    many = [S("A", ("pb", 0.5))] * 3 + [S("B", ("pa", 0.5))]
    assert confusions(many, ID2SKU) == [("A", "B", 3), ("B", "A", 1)] and len(confusions(many, ID2SKU, top=1)) == 1


def test_score_distributions_and_percentile():
    d = score_distributions([S("A", ("pa", 0.9)), S("A", ("pb", 0.7)), S(None, ("pa", 0.6)), S("B", ())], ID2SKU)
    assert d == {"correct": [0.9], "wrong": [0.7, 0.0], "unknown": [0.6]}
    assert percentile([], 95) is None and abs(percentile([1, 2, 3, 4, 5], 50) - 3.0) < 1e-9


# ----------------------------------------------------------------------- отчёт и советы

def test_report_warns_about_small_samples_and_suggests_better_thresholds():
    rep = build_report(_mixed_samples(), ID2SKU, match_threshold=0.72, unknown_threshold=0.45, margin=0.0)
    text = em.format_report(rep)
    assert "Выборка мала (35 рамок" in text and rep.n == 35 and rep.n_known == 35 and rep.n_unknown == 0
    assert any("Рекомендуется: match_threshold" in a for a in rep.advice) and "margin_threshold" in text
    assert any("не проверен" in a for a in rep.advice)                                      # чужих товаров в разметке нет
    assert "Что с чем путают" in text and "A -> B" in text


def test_report_gives_unknown_threshold_advice_only_with_enough_foreign_samples():
    foreign = [S(None, ("pa", 0.50 + i * 0.01)) for i in range(12)]
    rep = build_report(foreign + _mixed_samples(), ID2SKU, match_threshold=0.72, unknown_threshold=0.45, margin=0.0)
    assert any("unknown_threshold стоит держать не ниже" in a for a in rep.advice)


def test_report_states_when_the_target_is_not_reachable():
    bad = [S("A", ("pb", 0.9)), S("A", ("pb", 0.9)), S("A", ("pa", 0.9))] * 40
    rep = build_report(bad, ID2SKU, match_threshold=0.72, unknown_threshold=0.45, margin=0.0, target_precision=0.99)
    assert rep.target_met is False and any("недостижима" in a for a in rep.advice) and "НЕ достигнута" in em.format_report(rep)


def test_sweep_csv_is_written_with_header_and_rows(tmp_path):
    pts = sweep(_mixed_samples(), ID2SKU, 0.45, match_grid=(0.6, 0.8), margin_grid=(0.0,))
    em.write_sweep_csv(pts, tmp_path / "sweep.csv")
    rows = (tmp_path / "sweep.csv").read_text(encoding="utf-8").splitlines()
    assert rows[0] == "match_threshold,margin,n,auto_rate,precision,error_rate,review_rate" and len(rows) == 3


# ----------------------------------------------------------------------- сбор выборки из настоящих файлов

def _shelf(tmp_path: Path):
    """Фото 200x100: слева красный «товар» RED, справа зелёный GREEN, посередине серый «чужой» OTHER; разметка по классам."""
    images, labels = tmp_path / "img", tmp_path / "lbl"
    images.mkdir()
    labels.mkdir()
    arr = np.full((100, 200, 3), 128, np.uint8)
    arr[:, 0:60] = (220, 30, 30)
    arr[:, 140:200] = (30, 200, 60)
    Image.fromarray(arr).save(images / "s1.png")
    (labels / "s1.txt").write_text("0 0.15 0.5 0.30 1.0\n1 0.85 0.5 0.30 1.0\n2 0.5 0.5 0.30 1.0\n", encoding="utf-8")
    (labels / "classes.txt").write_text("RED\nGREEN\nunknown\n", encoding="utf-8")
    return images, labels


def _color_embed(crops):
    vecs = [np.asarray(c, dtype=np.float32).reshape(-1, 3).mean(axis=0) for c in crops]
    return np.stack([v / np.linalg.norm(v) for v in vecs])


def _index_and_ranker():
    index = NumpyCatalogIndex()
    index.add("pid-red", _color_embed([np.full((4, 4, 3), (220, 30, 30), np.uint8)])[0])
    index.add("pid-green", _color_embed([np.full((4, 4, 3), (30, 200, 60), np.uint8)])[0])
    matcher = ProductMatcher(index, top_k=3, match_threshold=0.9, unknown_threshold=0.5)
    return matcher.rank


def test_collect_samples_crops_by_label_maps_classes_and_marks_foreign_goods(tmp_path):
    images, labels = _shelf(tmp_path)
    samples, warns = collect_samples(images, labels, em.read_classes(labels / "classes.txt"), {"RED": "pid-red", "GREEN": "pid-green"},
                                     _color_embed, _index_and_ranker())
    assert [s.truth_sku for s in samples] == ["RED", "GREEN", None] and warns == []
    assert [s.source for s in samples] == ["s1.png#0", "s1.png#1", "s1.png#2"]
    assert samples[0].ranked[0][0] == "pid-red" and samples[1].ranked[0][0] == "pid-green"
    id2sku = {"pid-red": "RED", "pid-green": "GREEN"}
    assert top_k_accuracy(samples, id2sku, 1) == 1.0                                          # реальный конвейер кропа+поиска сошёлся
    rep = build_report(samples, id2sku, match_threshold=0.9, unknown_threshold=0.5, margin=0.0)
    assert rep.current.auto_correct == 2


def test_collect_samples_warns_about_skus_missing_from_the_catalog_and_skips_tiny_boxes(tmp_path):
    images, labels = _shelf(tmp_path)
    (labels / "s1.txt").write_text("0 0.15 0.5 0.30 1.0\n1 0.85 0.5 0.30 1.0\n0 0.5 0.5 0.01 0.02\n", encoding="utf-8")
    samples, warns = collect_samples(images, labels, ["RED", "GREEN", "unknown"], {"RED": "pid-red"}, _color_embed, _index_and_ranker())
    assert [s.truth_sku for s in samples] == ["RED", None]                                    # GREEN нет в каталоге -> «не из каталога»
    text = "\n".join(warns)
    assert "артикул «GREEN» из разметки не найден в каталоге" in text and "слишком мелкая" in text


def test_collect_samples_uses_exif_upright_images_and_rejects_bad_labels(tmp_path):
    images, labels = tmp_path / "img", tmp_path / "lbl"
    images.mkdir()
    labels.mkdir()
    exif = Image.Exif()
    exif[0x0112] = 6                                                  # пиксели альбомные 200x100, на экране портрет 100x200
    arr = np.full((100, 200, 3), 128, np.uint8)
    arr[:, 0:100] = (220, 30, 30)                                      # левая половина красная в хранимых пикселях
    Image.fromarray(arr).save(images / "p.jpg", exif=exif, quality=100, subsampling=0)
    (labels / "p.txt").write_text("0 0.5 0.5 1.0 1.0\n", encoding="utf-8")
    seen_shapes = []
    def embed(crops):
        seen_shapes.extend(c.shape for c in crops)
        return _color_embed(crops)
    collect_samples(images, labels, ["RED"], {"RED": "pid-red"}, embed, _index_and_ranker())
    assert seen_shapes == [(200, 100, 3)]                              # кроп нарезан из ПОВЁРНУТОГО снимка (высота 200, ширина 100)
    (labels / "p.txt").write_text("5 0.5 0.5 1.0 1.0\n", encoding="utf-8")
    try:
        collect_samples(images, labels, ["RED"], {"RED": "pid-red"}, embed, _index_and_ranker())
        raise AssertionError("ожидалось исключение")
    except ValueError as exc:
        assert "p.txt" in str(exc) and "класс 5" in str(exc)


def test_the_unknown_label_always_means_not_in_catalog_even_if_a_sku_carries_that_name(tmp_path):
    images, labels = _shelf(tmp_path)
    samples, warns = collect_samples(images, labels, ["unknown", "GREEN", "RED"], {"unknown": "pid-red", "GREEN": "pid-green"},
                                     _color_embed, _index_and_ranker())
    assert samples[0].truth_sku is None and samples[1].truth_sku == "GREEN"                    # «unknown» в разметке — чужой товар, всегда


def test_read_classes_ignores_blank_lines_and_bom(tmp_path):
    p = tmp_path / "classes.txt"
    p.write_text("\ufeffRED\n\n  GREEN  \n\n", encoding="utf-8")
    assert em.read_classes(p) == ["RED", "GREEN"]


def test_parser_defaults_match_the_documentation():
    ns = em.build_parser().parse_args(["--images", "a", "--labels", "b"])
    assert ns.target_precision == 0.98 and ns.manifest == "models/index/import_manifest.json" and ns.classes is None
    assert (em.DEFAULT_MATCH_GRID[0], em.DEFAULT_MATCH_GRID[-1]) == (0.30, 0.95)
