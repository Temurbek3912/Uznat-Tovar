"""
app/services/catalog_import.py — массовая загрузка каталога. Реально выполняется: настоящие фото (PIL), настоящий NumpyCatalogIndex
(сохранение/загрузка/поиск), настоящие манифест и файлы; подставлены только БД-репозиторий и эмбеддер (цвет -> вектор).
Не проверяется: реальная БД (SQLAlchemy) и DINOv2 — см. scripts/import_catalog.py.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.core.schemas import DetectionStatus as S
from app.pipeline.matcher import NumpyCatalogIndex, ProductMatcher
from app.services import catalog_import as ci
from app.services.catalog_import import (
    ExistingProduct, Manifest, ProductMeta, build_plan, catalog_report, check_photo, parse_products_csv, run_import, scan_catalog_folder,
    validate_sku,
)


# ----------------------------------------------------------------------- помощники

def _photo(path: Path, color=(200, 40, 40), size=240, seed=0, noise=25):
    """Однотонный «товар» с шумом (шум нужен, чтобы фото считалось резким)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    arr = np.clip(np.array(color, dtype=int) + rng.integers(-noise, noise + 1, (size, size, 3)), 0, 255).astype(np.uint8)
    Image.fromarray(arr).save(path)
    return path


def _catalog(root: Path, spec: dict[str, list[tuple]]) -> Path:
    """spec: sku -> список цветов (по фото на цвет)."""
    for sku, colors in spec.items():
        for i, color in enumerate(colors):
            _photo(root / sku / f"p{i}.png", color, seed=hash((sku, i)) % 10_000)
    return root


class ColorEmbedder:
    """Вектор = нормированный средний цвет (3 числа). Одинаковый цвет -> одинаковый вектор; разные товары = разные цвета."""
    def __init__(self, fail_on=None):
        self.calls, self.fail_on = 0, fail_on

    def embed(self, images):
        self.calls += 1
        if self.fail_on and self.calls == self.fail_on:
            raise self.fail_exc if hasattr(self, "fail_exc") else RuntimeError("embedder exploded")
        vecs = [np.asarray(im, dtype=np.float32).reshape(-1, 3).mean(axis=0) for im in images]
        return np.stack([v / np.linalg.norm(v) for v in vecs]).astype(np.float32)


class FakeRepo:
    def __init__(self, existing=None):
        self.products = dict(existing or {})
        self.created, self.updated = [], []
        self.fail_create_for = set()

    def existing_products(self):
        return dict(self.products)

    def create_product(self, meta):
        if meta.sku in self.fail_create_for:
            raise RuntimeError("db down")
        pid = f"pid-{len(self.products) + 1}"
        self.products[meta.sku] = ExistingProduct(pid, meta.name, meta.brand, meta.category, meta.expected_price)
        self.created.append(meta.sku)
        return pid

    def update_product(self, product_id, meta):
        self.updated.append((product_id, meta.sku))
        sku = next(s for s, p in self.products.items() if p.id == product_id)
        self.products[sku] = ExistingProduct(product_id, meta.name, meta.brand, meta.category, meta.expected_price)


class CountingIndex(NumpyCatalogIndex):
    def __init__(self):
        super().__init__()
        self.saves = 0

    def save(self, path):
        self.saves += 1
        super().save(path)


RED, GREEN, BLUE = (220, 40, 40), (40, 200, 60), (40, 60, 220)


def _plan(root: Path, metas=None, manifest=None, existing=None, **kw):
    folders, warns, errs = scan_catalog_folder(root)
    checks = {p: check_photo(p) for f in folders for p in f.photos}
    plan = build_plan(folders, metas or {}, manifest or Manifest(), checks, existing, **kw)
    plan.warnings = warns + plan.warnings
    plan.errors = errs + plan.errors
    return plan


def _run(plan, tmp_path, repo=None, embedder=None, index=None, manifest=None, **kw):
    repo = repo or FakeRepo()
    embedder = embedder or ColorEmbedder()
    index = index if index is not None else CountingIndex()
    manifest = manifest if manifest is not None else Manifest()
    lines = []
    res = run_import(plan, repo, embedder, index, index_path=str(tmp_path / "idx" / "cat.npz"), manifest=manifest,
                     manifest_path=tmp_path / "idx" / "manifest.json", out=lines.append, **kw)
    return res, repo, index, manifest, lines


# ----------------------------------------------------------------------- сканирование

def test_scan_finds_sku_folders_and_ignores_hidden_and_files(tmp_path):
    _catalog(tmp_path, {"COLA-05": [RED, RED], "FANTA-05": [GREEN]})
    _photo(tmp_path / ".git" / "x.png")
    _photo(tmp_path / "__MACOSX" / "x.png")
    (tmp_path / "readme.txt").write_text("x")
    folders, warns, errs = scan_catalog_folder(tmp_path)
    assert [(f.sku, len(f.photos)) for f in folders] == [("COLA-05", 2), ("FANTA-05", 1)] and warns == [] and errs == []


def test_scan_warns_about_stray_photos_heic_nested_folders_and_empty_sku(tmp_path):
    _catalog(tmp_path, {"A": [RED]})
    _photo(tmp_path / "loose.png")
    (tmp_path / "A" / "iphone.HEIC").write_bytes(b"x")
    (tmp_path / "A" / "sub").mkdir()
    (tmp_path / "EMPTY").mkdir()
    folders, warns, errs = scan_catalog_folder(tmp_path)
    text = "\n".join(warns)
    assert "1 фото лежат прямо в" in text and "HEIC" in text and "вложенные папки" in text and "EMPTY: нет фото" in text
    assert [f.sku for f in folders] == ["A"] and errs == []


def test_scan_rejects_bad_sku_names_and_empty_or_missing_roots(tmp_path):
    # Папку с «:» в имени можно создать только на Linux (Docker); в Windows само правило проверяется напрямую.
    if os.name == "nt":
        assert "недопустимые символы" in validate_sku("bad:name")
    else:
        _photo(tmp_path / "bad:name" / "a.png")
    _photo(tmp_path / ("x" * 70) / "a.png")
    _photo(tmp_path / "OK" / "a.png")
    folders, warns, errs = scan_catalog_folder(tmp_path)
    assert [f.sku for f in folders] == ["OK"] and any("длиннее" in e for e in errs)
    assert len(errs) == (1 if os.name == "nt" else 2)
    assert os.name == "nt" or any("недопустимые символы" in e for e in errs)
    assert "нет папки" in scan_catalog_folder(tmp_path / "nope")[2][0]
    empty = tmp_path / "e"
    empty.mkdir()
    assert "не найдено ни одной папки" in scan_catalog_folder(empty)[2][0]


@pytest.mark.skipif(os.name == "nt", reason="в Windows папки Cola и COLA — одна и та же папка; ситуация возможна только на Linux/Docker")
def test_scan_warns_when_two_skus_differ_only_by_case(tmp_path):
    _catalog(tmp_path, {"Cola": [RED], "COLA": [GREEN]})
    _, warns, _ = scan_catalog_folder(tmp_path)
    assert any("только регистром" in w for w in warns)


# ----------------------------------------------------------------------- products.csv

def test_csv_parses_comma_semicolon_bom_russian_headers_and_price_formats():
    metas, errs = parse_products_csv("sku,name,brand,category,price\nC1,Кола 0.5,Brand,Напитки,99.90\n")
    assert errs == [] and metas["C1"] == ProductMeta("C1", "Кола 0.5", "Brand", "Напитки", 99.9)
    metas, errs = parse_products_csv("\ufeffАртикул;Название;Бренд;Категория;Цена\r\nC2;Фанта;B;Напитки;1 200,50\r\nC3;Спрайт;;;\r\n")
    assert errs == [] and metas["C2"].expected_price == 1200.5
    assert metas["C3"] == ProductMeta("C3", "Спрайт", ci.DEFAULT_BRAND, ci.DEFAULT_CATEGORY, None)       # пустые поля -> значения по умолчанию


def test_csv_reports_every_problem_with_line_numbers_and_missing_columns():
    metas, errs = parse_products_csv("sku,name,price\nA,Один,10\nA,Дубль,5\n,Без артикула,1\nB,,2\nC,Цена,abc\nD,Минус,-5\nE,Ок,7\n")
    assert list(metas) == ["A", "E"]
    text = "\n".join(errs)
    assert "строка 3: артикул «A» повторяется" in text and "строка 4: пустой артикул" in text and "строка 5" in text and "пустое название" in text
    assert "строка 6" in text and "«abc» не число" in text and "строка 7" in text
    assert "нет колонки «sku»" in parse_products_csv("name,brand\nX,Y\n")[1][0]
    assert "нет колонки «name»" in parse_products_csv("sku\nX\n")[1][0]
    assert parse_products_csv("") == ({}, ["products.csv пуст"])


# ----------------------------------------------------------------------- проверка фото

def test_check_photo_accepts_a_good_photo_and_flags_small_blurry_and_broken_ones(tmp_path):
    good = check_photo(_photo(tmp_path / "g.png", size=300))
    assert good.ok and good.warnings == [] and good.width == good.height == 300 and len(good.sha256) == 64 and good.sharpness > 15
    low = check_photo(_photo(tmp_path / "low.png", size=100))
    assert low.ok and any("низкое разрешение" in w for w in low.warnings)
    tiny = check_photo(_photo(tmp_path / "tiny.png", size=40))
    assert not tiny.ok and "слишком мелкое" in tiny.errors[0]
    flat = tmp_path / "flat.png"
    Image.new("RGB", (300, 300), (120, 120, 120)).save(flat)
    assert any("размытое или однотонное" in w for w in check_photo(flat).warnings)
    (tmp_path / "bad.jpg").write_bytes(b"not an image")
    assert "не читается" in check_photo(tmp_path / "bad.jpg").errors[0]


def test_check_photo_uses_the_exif_upright_size(tmp_path):
    exif = Image.Exif()
    exif[0x0112] = 6
    arr = np.random.default_rng(0).integers(0, 255, (240, 320, 3), dtype=np.uint8)           # альбомные пиксели 320x240
    path = tmp_path / "phone.jpg"
    Image.fromarray(arr).save(path, exif=exif)
    chk = check_photo(path)
    assert (chk.width, chk.height) == (240, 320)                                             # после поворота — портрет


def test_same_content_gives_same_hash_and_different_content_differs(tmp_path):
    a, b = _photo(tmp_path / "a.png", seed=1), _photo(tmp_path / "b.png", seed=2)
    (tmp_path / "c.png").write_bytes(a.read_bytes())
    assert check_photo(a).sha256 == check_photo(tmp_path / "c.png").sha256 != check_photo(b).sha256


# ----------------------------------------------------------------------- план

def test_plan_classifies_create_add_unchanged_and_skip(tmp_path):
    root = _catalog(tmp_path / "cat", {"NEW": [RED, RED], "OLD": [GREEN, GREEN], "SAME": [BLUE]})
    (root / "BAD").mkdir()
    _photo(root / "BAD" / "tiny.png", size=30)
    first = {p: check_photo(p) for p in (root / "SAME").iterdir()}
    manifest = Manifest(photos={c.sha256: {"sku": "SAME"} for c in first.values()}, products={"SAME": "pid-s"})
    existing = {"OLD": ExistingProduct("pid-o", "OLD", ci.DEFAULT_BRAND, ci.DEFAULT_CATEGORY, None),
                "SAME": ExistingProduct("pid-s", "SAME", ci.DEFAULT_BRAND, ci.DEFAULT_CATEGORY, None)}
    plan = _plan(root, manifest=manifest, existing=existing)
    actions = {s.sku: s.action for s in plan.skus}
    assert actions == {"BAD": "skip", "NEW": "create", "OLD": "add_photos", "SAME": "unchanged"}
    assert next(s for s in plan.skus if s.sku == "SAME").known_photos == 1
    assert any("tiny.png" in w and "пропущено" in w for w in plan.warnings) and plan.errors == []


def test_plan_handles_duplicates_within_and_across_skus(tmp_path):
    root = tmp_path / "cat"
    a = _photo(root / "A" / "1.png", seed=1)
    (root / "A" / "1_copy.png").write_bytes(a.read_bytes())                    # копия внутри товара — просто пропуск
    (root / "B").mkdir()
    (root / "B" / "same_as_A.png").write_bytes(a.read_bytes())                 # то же фото в ДРУГОМ товаре — ошибка
    _photo(root / "B" / "own.png", seed=9)
    plan = _plan(root)
    text = " ".join(plan.warnings)
    assert "точная копия" in text and any("ТО ЖЕ фото" in e and "«A»" in e for e in plan.errors)
    assert [len(s.new_photos) for s in plan.skus] == [1, 1]


def test_plan_warns_about_too_few_photos_missing_csv_rows_and_orphan_csv_rows(tmp_path):
    root = _catalog(tmp_path / "cat", {"A": [RED], "B": [GREEN, GREEN, GREEN]})
    metas = {"A": ProductMeta("A", "Товар А"), "GHOST": ProductMeta("GHOST", "Нет папки")}
    plan = _plan(root, metas=metas)
    text = " ".join(plan.warnings)
    assert "A: всего 1 фото" in text and "«GHOST» есть в products.csv, но нет папки" in text and "папка «B» не описана" in text
    assert next(s for s in plan.skus if s.sku == "B").meta.name == "B"        # без строки в CSV: название = артикул
    strict = _plan(root, metas=metas, require_csv=True)
    assert any("папка «B» не описана" in e for e in strict.errors)
    assert any("обязателен" in e for e in _plan(root, require_csv=True).errors)


def test_plan_detects_metadata_changes_only_for_existing_products_described_in_csv(tmp_path):
    root = _catalog(tmp_path / "cat", {"A": [RED], "B": [GREEN]})
    m = Manifest()
    existing = {"A": ExistingProduct("pa", "Старое имя", "Brand", "Cat", 10.0), "B": ExistingProduct("pb", "B", ci.DEFAULT_BRAND, ci.DEFAULT_CATEGORY, None)}
    metas = {"A": ProductMeta("A", "Новое имя", "Brand", "Cat", 10.0)}
    plan = _plan(root, metas=metas, manifest=m, existing=existing)
    assert [(s.sku, s.meta_changed) for s in plan.skus] == [("A", True), ("B", False)]


# ----------------------------------------------------------------------- выполнение

def test_import_creates_products_adds_vectors_saves_index_and_manifest_and_matching_works(tmp_path):
    root = _catalog(tmp_path / "cat", {"RED": [RED, RED, RED], "GREEN": [GREEN, GREEN], "BLUE": [BLUE]})
    res, repo, index, manifest, _ = _run(_plan(root), tmp_path)
    assert (res.created, res.extended, res.photos_added, res.failed) == (3, 0, 6, [])
    assert sorted(repo.created) == ["BLUE", "GREEN", "RED"] and set(manifest.products) == {"RED", "GREEN", "BLUE"}
    assert len(manifest.photos) == 6 and json.loads((tmp_path / "idx" / "manifest.json").read_text())["version"] == 1
    loaded = NumpyCatalogIndex.load(str(tmp_path / "idx" / "cat.npz"))                       # индекс реально лёг на диск
    pid = manifest.products["GREEN"]
    query = ColorEmbedder().embed([np.full((10, 10, 3), GREEN, np.uint8)])[0]
    result = ProductMatcher(loaded, top_k=3, match_threshold=0.9, unknown_threshold=0.5).match(query)
    assert result.status == S.MATCHED and result.product_id == pid


def test_second_run_is_idempotent_and_adds_only_new_photos(tmp_path):
    root = _catalog(tmp_path / "cat", {"RED": [RED, RED], "GREEN": [GREEN]})
    repo, index, manifest = FakeRepo(), CountingIndex(), Manifest()
    _run(_plan(root), tmp_path, repo, index=index, manifest=manifest)
    vectors_before = len(index._ids)
    plan2 = _plan(root, manifest=manifest, existing=repo.existing_products())
    assert {s.action for s in plan2.skus} == {"unchanged"}
    res2, _, _, _, _ = _run(plan2, tmp_path, repo, index=index, manifest=manifest)
    assert res2.photos_added == 0 and res2.unchanged == 2 and len(index._ids) == vectors_before     # повтор ничего не добавил
    assert sorted(repo.created) == ["GREEN", "RED"]
    _photo(root / "RED" / "new_angle.png", RED, seed=777)
    plan3 = _plan(root, manifest=manifest, existing=repo.existing_products())
    assert {s.sku: s.action for s in plan3.skus} == {"GREEN": "unchanged", "RED": "add_photos"}
    res3, _, _, _, _ = _run(plan3, tmp_path, repo, index=index, manifest=manifest)
    assert (res3.extended, res3.created, res3.photos_added) == (1, 0, 1) and len(index._ids) == vectors_before + 1
    assert len(repo.created) == 2                                                          # новые продукты не создавались


def test_embedding_failure_does_not_create_a_product_and_other_skus_continue(tmp_path):
    root = _catalog(tmp_path / "cat", {"A": [RED], "B": [GREEN], "C": [BLUE]})
    res, repo, index, manifest, lines = _run(_plan(root), tmp_path, embedder=ColorEmbedder(fail_on=2))
    assert len(res.failed) == 1 and res.failed[0].startswith("B: RuntimeError") and (res.created, res.photos_added) == (2, 2)
    assert "B" not in repo.created and "B" not in manifest.products                       # эмбеддинг упал ДО создания товара
    assert any("ОШИБКА" in l for l in lines)


def test_db_failure_for_one_sku_is_isolated_and_leaves_no_vectors_for_it(tmp_path):
    root = _catalog(tmp_path / "cat", {"A": [RED], "B": [GREEN]})
    repo = FakeRepo()
    repo.fail_create_for = {"A"}
    res, _, index, manifest, _ = _run(_plan(root), tmp_path, repo)
    assert res.failed[0].startswith("A: RuntimeError: db down") and res.created == 1 and set(manifest.products) == {"B"}
    assert set(index._ids) == {manifest.products["B"]}                                     # у A векторов нет — «призраков» в индексе нет


def test_crash_between_product_creation_and_index_save_is_repaired_by_the_next_run(tmp_path):
    root = _catalog(tmp_path / "cat", {"A": [RED, RED]})
    # состояние после обрыва: товар уже в БД, а фото в манифесте нет (индекс/манифест не успели сохраниться)
    repo = FakeRepo({"A": ExistingProduct("pid-A", "A", ci.DEFAULT_BRAND, ci.DEFAULT_CATEGORY, None)})
    plan = _plan(root, existing=repo.existing_products())
    assert plan.skus[0].action == "add_photos"
    res, repo2, index, manifest, _ = _run(plan, tmp_path, repo)
    assert (res.created, res.extended, res.photos_added) == (0, 1, 2) and repo.created == []
    assert set(index._ids) == {"pid-A"} and manifest.products == {"A": "pid-A"}


def test_ctrl_c_saves_progress_so_far(tmp_path):
    root = _catalog(tmp_path / "cat", {"A": [RED], "B": [GREEN], "C": [BLUE]})
    emb = ColorEmbedder(fail_on=3)
    emb.fail_exc = KeyboardInterrupt()
    res, repo, index, manifest, lines = _run(_plan(root), tmp_path, embedder=emb)
    assert res.interrupted and res.created == 2 and set(manifest.products) == {"A", "B"}
    assert (tmp_path / "idx" / "cat.npz").exists() and json.loads((tmp_path / "idx" / "manifest.json").read_text())["products"].keys() == {"A", "B"}
    assert any("Прервано" in l for l in lines)


def test_checkpoints_save_periodically_and_once_at_the_end(tmp_path):
    root = _catalog(tmp_path / "cat", {f"S{i}": [RED] for i in range(5)})
    _, _, index, _, _ = _run(_plan(root), tmp_path, checkpoint_every=2)
    assert index.saves == 3                                                                # после 2-го, 4-го и финальная


def test_index_is_always_saved_before_the_manifest(tmp_path):
    # обрыв между двумя сохранениями должен давать лишь повторное добавление пары векторов, а не «потерянные» фото
    log = []
    class LoggingIndex(CountingIndex):
        def save(self, path):
            log.append("index")
            super().save(path)
    class LoggingManifest(Manifest):
        def save(self, path):
            log.append("manifest")
            super().save(path)
    root = _catalog(tmp_path / "cat", {f"S{i}": [RED] for i in range(4)})
    _run(_plan(root), tmp_path, index=LoggingIndex(), manifest=LoggingManifest(), checkpoint_every=2)
    assert log == ["index", "manifest"] * 3                                              # две контрольные точки + финальная


def test_embedding_is_done_in_batches_of_the_requested_size(tmp_path):
    root = _catalog(tmp_path / "cat", {"A": [RED] * 5})
    emb = ColorEmbedder()
    for i in range(5):
        _photo(root / "A" / f"p{i}.png", RED, seed=50 + i)
    _run(_plan(root), tmp_path, embedder=emb, batch_size=2)
    assert emb.calls == 3                                                                  # 5 фото по 2 -> 3 вызова


def test_metadata_update_only_with_the_flag(tmp_path):
    root = _catalog(tmp_path / "cat", {"A": [RED]})
    existing = {"A": ExistingProduct("pa", "Старое", "B", "C", 1.0)}
    metas = {"A": ProductMeta("A", "Новое", "B", "C", 2.0)}
    known = {check_photo(next((root / "A").iterdir())).sha256: {"sku": "A"}}
    res, repo, _, _, lines = _run(_plan(root, metas=metas, manifest=Manifest(photos=dict(known), products={"A": "pa"}), existing=existing),
                                  tmp_path, FakeRepo(existing), manifest=Manifest(photos=dict(known), products={"A": "pa"}))
    assert res.metadata_updated == 0 and repo.updated == []
    assert any("--update-metadata" in l for l in lines)                                      # пользователю сказано, что делать
    res, repo, _, _, lines = _run(_plan(root, metas=metas, manifest=Manifest(photos=dict(known), products={"A": "pa"}), existing=existing),
                                  tmp_path, FakeRepo(existing), manifest=Manifest(photos=dict(known), products={"A": "pa"}), update_metadata=True)
    assert res.metadata_updated == 1 and repo.updated == [("pa", "A")] and any("данные обновлены" in l for l in lines)


def test_manifest_roundtrip_missing_file_and_atomic_save(tmp_path):
    assert Manifest.load(tmp_path / "none.json").photos == {}
    m = Manifest(photos={"abc": {"sku": "A"}}, products={"A": "pid"})
    path = tmp_path / "deep" / "m.json"
    m.save(path)
    loaded = Manifest.load(path)
    assert loaded.photos == m.photos and loaded.products == m.products
    assert not (tmp_path / "deep" / "m.json.tmp").exists()


# ----------------------------------------------------------------------- отчёт о каталоге

def _emb(*rows):
    a = np.array(rows, dtype=np.float32)
    return a / np.linalg.norm(a, axis=1, keepdims=True)


def test_catalog_report_flags_confusable_skus_and_computes_leave_one_out_accuracy():
    embeddings = {
        "LEMON": _emb([1, 0, 0], [0.99, 0.05, 0]), "LIME": _emb([0.98, 0.15, 0], [0.97, 0.2, 0]),     # почти неотличимы
        "CHERRY": _emb([0, 1, 0], [0.02, 0.99, 0]),
    }
    rep = catalog_report(embeddings)
    assert [(a, b) for a, b, _ in rep.confusable] == [("LEMON", "LIME")] and rep.confusable[0][2] > 0.98
    assert rep.loo_accuracy is not None and 0.5 <= rep.loo_accuracy <= 1.0
    text = ci.format_catalog_report(rep)
    assert "LEMON  ~  LIME" in text and "margin_threshold" in text


def test_catalog_report_perfectly_separated_catalog_and_worst_skus():
    rep = catalog_report({"A": _emb([1, 0, 0], [0.99, 0.01, 0]), "B": _emb([0, 1, 0], [0.01, 0.99, 0]), "C": _emb([0, 0, 1], [0, 0.01, 0.99])})
    assert rep.confusable == [] and rep.loo_accuracy == 1.0 and rep.worst == []
    assert "Явно похожих товаров" in ci.format_catalog_report(rep)
    bad = catalog_report({"A": _emb([1, 0, 0], [0, 1, 0]), "B": _emb([1, 0.01, 0], [0.01, 1, 0])})
    assert bad.loo_accuracy == 0.0 and {s for s, _ in bad.worst} == {"A", "B"}


def test_confusable_pairs_are_listed_from_most_to_least_similar_and_truncated_to_top():
    # порядок обхода пар (A,B),(A,C),(B,C) намеренно НЕ совпадает с порядком по убыванию сходства: A~C > B~C > A~B
    emb = {"A": _emb([1, 0, 0]), "B": _emb([0.92, 0.39, 0]), "C": _emb([0.999, 0.045, 0])}
    pairs = catalog_report(emb, confusable_threshold=0.9).confusable
    assert [(a, b) for a, b, _ in pairs] == [("A", "C"), ("B", "C"), ("A", "B")]
    scores = [sc for _, _, sc in pairs]
    assert scores == sorted(scores, reverse=True)
    assert [(a, b) for a, b, _ in catalog_report(emb, confusable_threshold=0.9, top=2).confusable] == [("A", "C"), ("B", "C")]


def test_catalog_report_edge_cases():
    assert "минимум 2" in (catalog_report({"A": _emb([1, 0, 0])}).skipped_reason or "")
    assert "минимум 2" in ci.format_catalog_report(catalog_report({}))
    single = catalog_report({"A": _emb([1, 0, 0]), "B": _emb([0, 1, 0])})                 # по одному фото: LOO считать не из чего
    assert single.loo_accuracy is None and single.confusable == []
    huge = {f"S{i}": np.tile(_emb([1, 0, 0]), (3, 1)) for i in range(2100)}               # 6300 фото > лимита
    assert "слишком много" in (catalog_report(huge).skipped_reason or "")


# ----------------------------------------------------------------------- 2026-11-07: CSV — те же правила, что у формы

@pytest.mark.parametrize("bad", ["nan", "NaN", "inf", "-inf", "infinity", "1e999", "1e3", "0", "-5", "12..3", "9900$"])
def test_csv_price_rejects_values_the_form_rejects(bad):
    """Раньше float(): «nan» и «inf» становились ценой, а NaN в базе ломает JSON-ответ со списком товаров для всех."""
    metas, errs = parse_products_csv(f"sku,name,price\nA,Кола,{bad}\n")
    assert metas == {} and len(errs) == 1 and "строка 2" in errs[0], (bad, errs)


@pytest.mark.parametrize("good, value", [("9 900", 9900.0), ("1.200,50", 1200.5), ("9900 сум", 9900.0), ("99,90", 99.9), ("", None)])
def test_csv_price_accepts_the_same_formats_as_the_form(good, value):
    metas, errs = parse_products_csv(f"sku;name;price\nA;Кола;{good}\n")
    assert errs == [] and metas["A"].expected_price == value


def test_csv_names_follow_the_form_limits_and_are_cleaned():
    long_name = "Ж" * 121
    metas, errs = parse_products_csv(f"sku,name,brand\nA,{long_name},B\nB,  Кола\t 0.5 ,{'Б' * 61}\nC,  Кола   1л ,  Coca  Cola \n")
    assert "A" not in metas and "B" not in metas
    assert any("строка 2" in e and "120" in e for e in errs) and any("строка 3" in e and "60" in e for e in errs)
    assert metas["C"].name == "Кола 1л" and metas["C"].brand == "Coca Cola"


def test_cp1251_csv_from_excel_is_read_as_cyrillic_with_a_warning_not_garbled():
    raw = "Артикул;Название;Бренд\r\nA1;Чай зелёный;Ахмад\r\n".encode("cp1251")
    text, warnings = ci.decode_products_csv(raw)
    metas, errs = parse_products_csv(text)
    assert errs == [] and metas["A1"].name == "Чай зелёный" and metas["A1"].brand == "Ахмад"
    assert warnings and "Windows-1251" in warnings[0]
    assert "�" not in text


def test_utf8_csv_with_or_without_bom_and_uzbek_letters_is_read_without_warnings():
    for raw in ["sku,name\nA,Oʻzbek choyi\n".encode("utf-8"), "﻿sku,name\nA,Gʻalla\n".encode("utf-8")]:
        text, warnings = ci.decode_products_csv(raw)
        assert warnings == [] and "﻿" not in parse_products_csv(text)[0]["A"].name


def test_import_script_reads_csv_bytes_through_the_decoder():
    src = (Path(__file__).resolve().parents[1] / "scripts" / "import_catalog.py").read_text(encoding="utf-8")
    assert "decode_products_csv(csv_path.read_bytes())" in src and 'errors="replace"' not in src
