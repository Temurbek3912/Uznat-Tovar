"""scripts/fix_product_names.py: план правок проверяет всё до записи (как страница «Товары»)."""
from pathlib import Path

from scripts.fix_product_names import Row, main, plan, read_rows


def _row(sku, action="rename", old="", new="", old_brand="", new_brand="", why=""):
    return Row(sku, action, old, new, old_brand, new_brand, why)


PRODUCTS = {
    "A": ("id-a", "дошuрак", "Доширак", "active"),
    "B": ("id-b", "Choco-Pie 30 г", "Orion", "active"),
    "C": ("id-c", "Choco_Pie 30 gramms", "Orion", "active"),
    "D": ("id-d", "zoirjon", "Не указан", "active"),
    "E": ("id-e", "Old", "X", "archived"),
}


def test_rename_and_brand():
    [s] = plan([_row("A", old="дошuрак", new="Доширак", new_brand="Доширак")], PRODUCTS)
    assert s.outcome == "apply" and s.name == "Доширак" and s.brand is None      # бренд уже такой — не меняется


def test_duplicate_name_is_skipped():
    [s] = plan([_row("C", old="Choco_Pie 30 gramms", new="Choco-Pie 30 г")], PRODUCTS)
    assert s.outcome == "skip" and "уже есть товар" in s.note


def test_duplicate_counts_earlier_rows_of_same_list():
    steps = plan([_row("A", old="дошuрак", new="Новое"), _row("C", old="Choco_Pie 30 gramms", new="новое")], PRODUCTS)
    assert [s.outcome for s in steps] == ["apply", "skip"]


def test_changed_by_hand_is_skipped():
    [s] = plan([_row("A", old="что-то другое", new="Доширак 2")], PRODUCTS)
    assert s.outcome == "skip" and "уже другое" in s.note


def test_repeat_is_same():
    [s] = plan([_row("B", old="Choco-Pie 30 g", new="Choco-Pie 30 г")], PRODUCTS)
    assert s.outcome == "same"


def test_archive():
    steps = plan([_row("D", "archive", old="zoirjon"), _row("E", "archive", old="Old"), _row("Z", "archive", old="?")],
                 PRODUCTS)
    assert [s.outcome for s in steps] == ["apply", "same", "skip"]


def test_bad_action_and_empty_name():
    steps = plan([_row("A", "delete", old="дошuрак"), _row("A", old="дошuрак", new="   ", new_brand="  ")], PRODUCTS)
    assert steps[0].outcome == "skip"
    assert steps[1].outcome == "same"                       # пусто — значит «не менять»


def test_read_rows_and_missing_file(tmp_path: Path):
    p = tmp_path / "x.csv"
    p.write_text("sku,action,old_name,new_name,old_brand,new_brand,why\nA, rename ,дошuрак,Доширак,,,опечатка\n",
                 encoding="utf-8")
    [r] = read_rows(p)
    assert r.action == "rename" and r.new_name == "Доширак"
    assert main(["--csv", str(tmp_path / "nope.csv")]) == 2


def test_committed_list_is_valid():
    rows = read_rows(Path(__file__).resolve().parents[1] / "docs/catalog_cleanup/names_2026-11-18.csv")
    assert len(rows) == 107 and {r.action for r in rows} == {"rename", "archive"}
    assert len({r.sku for r in rows}) == len(rows)
