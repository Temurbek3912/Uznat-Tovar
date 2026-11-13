"""app/services/store_service.py — чистая функция, выполняется офлайн."""
from __future__ import annotations

from types import SimpleNamespace as NS

from app.services.store_service import build_store_listing


def _store(i, name):
    return NS(id=i, name=name, address="addr", format="supermarket")


def test_registered_shelves_listed_with_slot_counts_and_sorted_by_name():
    stores = [_store("s2", "Бета"), _store("s1", "алфа")]
    shelves = [NS(store_id="s1", shelf_id="a-1", name="Соки"), NS(store_id="s1", shelf_id="a-2", name="Вода")]
    out = build_store_listing(stores, shelves, [], {"a-1": 6})
    assert [s["id"] for s in out] == ["s1", "s2"]                      # без учёта регистра: «алфа» < «Бета»
    # сортировка по ИМЕНИ, а не по коду: «Вода» (a-2) раньше «Соки» (a-1), хотя код a-1 < a-2
    assert [(x["shelf_id"], x["name"], x["planogram_slots"], x["registered"]) for x in out[0]["shelves"]] == \
           [("a-2", "Вода", 0, True), ("a-1", "Соки", 6, True)]
    assert out[1]["shelves"] == []


def test_shelf_known_only_from_snapshots_is_added_unregistered():
    out = build_store_listing([_store("s1", "A")], [], [("s1", "legacy-shelf")], {"legacy-shelf": 4})
    assert out[0]["shelves"] == [
        {"shelf_id": "legacy-shelf", "name": "legacy-shelf", "registered": False, "planogram_slots": 4},
    ]


def test_registry_wins_over_snapshot_pair_and_no_duplicates():
    shelves = [NS(store_id="s1", shelf_id="x", name="Красивое имя")]
    out = build_store_listing([_store("s1", "A")], shelves, [("s1", "x"), ("s1", "x")], {})
    assert len(out[0]["shelves"]) == 1
    assert out[0]["shelves"][0]["name"] == "Красивое имя" and out[0]["shelves"][0]["registered"] is True


def test_snapshot_pair_of_unlisted_store_is_ignored():
    # пара относится к магазину вне переданного списка (вне скоупа пользователя) — наружу не попадает
    out = build_store_listing([_store("s1", "A")], [], [("other-store", "sh")], {})
    assert [s["id"] for s in out] == ["s1"] and out[0]["shelves"] == []


def test_empty_inputs():
    assert build_store_listing([], [], [], {}) == []


# ---------------------------------------------------------------------------------------------------------
# Создание магазинов и полок: slugify / validate_store_fields / plan_shelves
# ---------------------------------------------------------------------------------------------------------

import pytest  # noqa: E402

from app.services.store_service import (  # noqa: E402
    MAX_SHELVES_PER_REQUEST, StoreInputError, plan_shelves, slugify, validate_store_fields,
)


def test_slugify_transliterates_russian_and_uzbek_cyrillic():
    assert slugify("Напитки, стеллаж 1") == "napitki-stellazh-1"
    assert slugify("Макро Чиланзар") == "makro-chilanzar"
    assert slugify("Қўй гўшти") == "qoy-goshti"


def test_slugify_collapses_junk_and_returns_empty_when_no_letters():
    assert slugify("  --Соки!!  100% ") == "soki-100"
    assert slugify("!!!") == "" and slugify("") == "" and slugify(None) == ""


def test_slugify_respects_max_len_without_trailing_dash():
    out = slugify("а" * 10 + " " + "б" * 10, max_len=11)
    assert len(out) <= 11 and not out.endswith("-")


def test_store_fields_defaults_and_trimming():
    assert validate_store_fields("  Макро  ", None, None) == ("Макро", "", "supermarket")
    assert validate_store_fields("A", " ул. 1 ", "HYPERMARKET") == ("A", "ул. 1", "hypermarket")


@pytest.mark.parametrize("name,address,fmt", [("", "x", "supermarket"), ("   ", None, None), ("A", "x", "kiosk"),
                                               ("A" * 201, "x", "supermarket")])
def test_store_fields_rejects_bad_input(name, address, fmt):
    with pytest.raises(StoreInputError):
        validate_store_fields(name, address, fmt)


def test_auto_codes_are_store_prefixed_unique_and_stable_order():
    out = plan_shelves([(None, "Напитки"), ("", "Напитки"), (None, "Снеки, стеллаж 1")], set(), "Макро Чиланзар")
    assert out == [("makro-chilanzar-napitki", "Напитки"), ("makro-chilanzar-napitki-2", "Напитки"),
                   ("makro-chilanzar-sneki-stellazh-1", "Снеки, стеллаж 1")]


def test_auto_code_skips_codes_already_taken_in_the_database():
    out = plan_shelves([(None, "Соки")], {"a-soki", "a-soki-2"}, "A")
    assert out == [("a-soki-3", "Соки")]


def test_cyrillic_only_name_without_store_still_gets_a_code():
    assert plan_shelves([(None, "!!!")], set(), "") == [("shelf", "!!!")]


def test_explicit_code_is_kept_and_reserved_before_auto_codes():
    # автокод первой полки не должен занять код, который явно задан у второй
    out = plan_shelves([(None, "Соки"), ("a-soki", "Другая")], set(), "A")
    assert out == [("a-soki-2", "Соки"), ("a-soki", "Другая")]


def test_explicit_code_that_is_taken_or_duplicated_is_an_error_not_silently_renamed():
    with pytest.raises(StoreInputError):
        plan_shelves([("x-1", "A")], {"x-1"})
    with pytest.raises(StoreInputError):
        plan_shelves([("x-1", "A"), ("x-1", "B")], set())


@pytest.mark.parametrize("bad", ["с пробелом", "../etc", "кириллица", ".hidden", "a/b", "x" * 129])
def test_explicit_code_must_be_a_safe_path_segment(bad):
    with pytest.raises(StoreInputError):
        plan_shelves([(bad, "A")], set())


def test_empty_shelf_name_is_rejected():
    with pytest.raises(StoreInputError):
        plan_shelves([(None, "  ")], set())


def test_large_supermarket_hundreds_of_shelves_get_unique_codes():
    items = [(None, f"Стеллаж {i % 40}") for i in range(MAX_SHELVES_PER_REQUEST)]   # 500 полок, имена повторяются
    codes = [c for c, _ in plan_shelves(items, set(), "Большой супермаркет")]
    assert len(codes) == MAX_SHELVES_PER_REQUEST and len(set(codes)) == MAX_SHELVES_PER_REQUEST


def test_more_shelves_than_the_limit_is_an_error():
    with pytest.raises(StoreInputError):
        plan_shelves([(None, f"П{i}") for i in range(MAX_SHELVES_PER_REQUEST + 1)], set())
