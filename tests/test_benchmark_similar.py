"""ml/benchmark_similar.py — замер «похожие товары и объёмы»: деление набора без копий, группы, виды ошибок, сетка порогов."""
import numpy as np

from ml import benchmark_similar as bs


def _unit(*xs):
    v = np.array(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


ITEMS = {
    "c05": bs.Item("c05", "Coca-Cola Classic 0,5 л", "Coca-Cola"),
    "c15": bs.Item("c15", "Coca-Cola Classic 1,5 л", "Coca-Cola"),
    "cz": bs.Item("cz", "Coca-Cola Zero 0,5 л", "Coca-Cola"),
    "w": bs.Item("w", "Вода Tassay 0,5 л", "Не указан"),
}


def test_size_siblings_and_brand_families():
    sib = bs.size_siblings(list(ITEMS.values()))
    assert sib == {"c05": {"c15"}, "c15": {"c05"}}                      # Zero — другой вкус, не объём
    assert bs.brand_families(list(ITEMS.values())) == {"c05", "c15", "cz"}  # «Не указан» — не марка


def test_leave_one_out_drops_copies_of_the_same_product_only():
    vectors = np.stack([_unit(1, 0, 0), _unit(1, 0.01, 0), _unit(1, 0.6, 0), _unit(0.95, 0.3, 0)])
    owners = ["a", "a", "a", "b"]
    queries = bs.leave_one_out(vectors, owners, copy=0.93)
    first = next(q for q in queries if q.index == 0)
    # копия (сходство ~1) своего товара убрана, остаётся дальнее фото «a» — чужой «b» ближе: честная ошибка
    assert [pid for pid, _ in first.ranked] == ["b", "a"]
    assert all(q.index != 3 for q in queries)                         # у «b» нет второго фото — снимок не считается


def test_summarize_splits_errors_into_size_brand_other():
    queries = [bs.Query(0, "c05", [("c15", 0.9), ("c05", 0.89)]),     # другой объём
               bs.Query(1, "c05", [("cz", 0.9), ("c05", 0.8)]),       # та же марка
               bs.Query(2, "c05", [("w", 0.9), ("c05", 0.8)]),        # прочее
               bs.Query(3, "c15", [("c15", 0.95), ("c05", 0.7)])]     # верно
    s = bs.summarize(queries, ITEMS, [True, False, False, True], bs.size_siblings(list(ITEMS.values())), {"c05", "c15", "cz"})
    c = s.counts
    assert (c["all", "n"], c["all", "top1"], c["all", "err_size"], c["all", "err_brand"], c["all", "err_other"]) == (4, 1, 1, 1, 1)
    assert (c["front", "n"], c["front", "top1"]) == (2, 1) and c["size", "n"] == 4
    assert s.rate("all", "top5") == 1.0


def test_decision_grid_margin_turns_close_wrong_answer_into_doubt():
    sib = bs.size_siblings(list(ITEMS.values()))
    queries = [bs.Query(0, "c05", [("c15", 0.90), ("c05", 0.89)]), bs.Query(1, "c15", [("c15", 0.95), ("c05", 0.70)])]
    strangers = [bs.Query(0, "x", [("c05", 0.91), ("c15", 0.90)])]
    no_margin, margin = bs.decision_grid(queries, strangers, [False, True], sib, 0.70, match_grid=(0.88,), margin_grid=(0.0, 0.04))
    assert (no_margin.confident, no_margin.wrong, no_margin.wrong_size, no_margin.stranger_accepted) == (2, 1, 1, 1.0)
    assert (margin.confident, margin.wrong, margin.stranger_accepted, margin.front_confident) == (1, 0, 0.0, 1)
    text = bs.render(bs.summarize(queries, ITEMS, [False, True], sib, set()), [no_margin, margin], (0.88, 0.04), 0.70, 4, 4,
                     sib, ITEMS)
    assert "**(сейчас)**" in text and "Coca-Cola Classic 0,5 л — Coca-Cola Classic 1,5 л" in text


def test_projected_index_is_flagged_as_not_honest(tmp_path):
    # 2026-11-22: проекция обучена на фото каталога — замер «оставить одно» на её индексе завышен
    import json
    from ml.benchmark_similar import projected_index
    path = tmp_path / "catalog.faiss"
    model = {"name": "m", "weights": "w", "dim": 3, "normalization": "l2", "preprocess": "p/v1"}
    (tmp_path / "catalog.faiss.ids.json").write_text(json.dumps({"manifest": {"model": model}}), encoding="utf-8")
    assert not projected_index(str(path))
    model["preprocess"] = "p/v1+proj-abc"
    (tmp_path / "catalog.faiss.ids.json").write_text(json.dumps({"manifest": {"model": model}}), encoding="utf-8")
    assert projected_index(str(path))
    assert not projected_index(str(tmp_path / "нет.faiss"))
