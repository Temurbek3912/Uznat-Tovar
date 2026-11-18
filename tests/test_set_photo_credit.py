"""scripts/set_photo_credit: подпись ставится по содержимому фото (SHA-256), своя подпись не затирается."""
from types import SimpleNamespace as NS

from app.services.product_images import PhotoSummary, summarize
from scripts import set_photo_credit as spc


def test_plan_matches_by_content_and_keeps_other_credit():
    rows = [NS(sha256="a", credit=None), NS(sha256="b", credit=spc.OFF_CREDIT), NS(sha256="c", credit="Своё"),
            NS(sha256="z", credit=None)]                                       # z — своё фото магазина, его нет в наборе
    todo, same, other = spc.plan(rows, {"a", "b", "c"}, spc.OFF_CREDIT)
    assert [r.sha256 for r in todo] == ["a"] and (same, other) == (1, 1)
    todo, _, other = spc.plan(rows, {"a", "b", "c"}, spc.OFF_CREDIT, overwrite=True)
    assert [r.sha256 for r in todo] == ["a", "c"] and other == 0


def test_set_digests_reads_sku_folders(tmp_path):
    (tmp_path / "SKU1").mkdir()
    (tmp_path / "SKU1" / "front_ru.jpg").write_bytes(b"1")
    (tmp_path / "SKU1" / "notes.txt").write_text("-", encoding="utf-8")
    (tmp_path / "products.csv").write_text("sku", encoding="utf-8")
    assert len(spc.set_digests([tmp_path])) == 1


def test_summary_carries_cover_credit():
    img = NS(id="1", role="reference", is_active=True, storage_path="x.jpg", credit="Open Food Facts, CC BY-SA")
    assert summarize([img]).cover_credit == "Open Food Facts, CC BY-SA"
    assert PhotoSummary(total=0, references=0, display=0, inactive=0, cover_id=None).cover_credit is None
