"""app/services/catalog_upload.py — приём фото, артикул из названия, безопасные ссылки. Выполняется офлайн (PIL/numpy/cv2)."""
from __future__ import annotations

import io
import os
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.services import catalog_upload as cu
from app.services.catalog_import import validate_sku
from app.services.catalog_upload import (CatalogAddError, InputError, PhotoError, clean_name, clean_text, describe_task,
                                         find_duplicate, make_sku, make_slug, normalize_photo, parse_price, save_photos)


def _noise_jpeg(w=400, h=300, seed=1, exif_orientation=None, fmt="JPEG") -> bytes:
    rng = np.random.default_rng(seed)
    image = Image.fromarray(rng.integers(0, 255, (h, w, 3), dtype=np.uint8))
    buf = io.BytesIO()
    kwargs = {}
    if exif_orientation:
        exif = Image.Exif()
        exif[0x0112] = exif_orientation
        kwargs["exif"] = exif
    image.save(buf, fmt, **kwargs)
    return buf.getvalue()


def _flat_jpeg(w=400, h=300) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (120, 130, 140)).save(buf, "JPEG")
    return buf.getvalue()


# ------------------------------------------------------------------ текст и цена

def test_clean_name_trims_collapses_and_rejects_empty_or_huge():
    assert clean_name("  Кола \n 0,5   л ") == "Кола 0,5 л"
    assert clean_name("A\x00B\x1fC") == "A B C"
    for bad in ("", "   ", None, "\n\t"):
        with pytest.raises(InputError):
            clean_name(bad)
    assert clean_name("я" * cu.MAX_NAME_LEN) == "я" * cu.MAX_NAME_LEN
    with pytest.raises(InputError):
        clean_name("я" * (cu.MAX_NAME_LEN + 1))


def test_optional_text_falls_back_to_default():
    assert clean_text("  ", label="бренд", max_len=60, default="Не указан") == "Не указан"
    assert clean_text(None, label="бренд", max_len=60, default="X") == "X"
    assert clean_text("Coca-Cola", label="бренд", max_len=60, default="X") == "Coca-Cola"
    with pytest.raises(InputError):
        clean_text("я" * 61, label="бренд", max_len=60)


def test_price_formats_people_actually_type():
    cases = {"9900": 9900.0, "9 900": 9900.0, "9\u00a0900": 9900.0, "99,90": 99.9, "99.90": 99.9, "1 200,50": 1200.5,
             "1.200,50": 1200.5, "1,200.50": 1200.5, "1.200.500": 1200500.0, "9900 сум": 9900.0, "9900 сум.": 9900.0,
             "1,5": 1.5, 5000: 5000.0, 12.345: 12.35}
    for raw, expected in cases.items():
        assert parse_price(raw) == expected, raw


def test_empty_price_means_not_set_but_zero_negative_and_garbage_are_errors():
    for empty in (None, "", "   ", "сум"):
        assert parse_price(empty) is None, repr(empty)
    for bad in ("0", 0, "-5", -1, "abc", "1e5", "12..3", "1.2.3", "1.200.50", "NaN", "inf", True, 2e9, "9999999999"):
        with pytest.raises(InputError):
            parse_price(bad)


# ------------------------------------------------------------------ артикул

def test_slug_transliterates_russian_and_uzbek_and_decimal_comma():
    assert make_slug("Кола 0,5 л") == "KOLA-0.5-L"
    assert make_slug("Фанта Апельсин 1.5л") == "FANTA-APELSIN-1.5L"
    assert make_slug("Чипсы «Lay's» классик") == "CHIPSY-LAY-S-KLASSIK"
    assert make_slug("Ўзбекистон нони қаттиқ ғалла ҳосил") == "UZBEKISTON-NONI-QATTIQ-GALLA-HOSIL"
    assert make_slug("Щётка ёжик") == "SHCHYOTKA-YOZHIK"
    assert make_slug("Pepsi+ Max") == "PEPSI+-MAX"


def test_slug_is_never_empty_never_too_long_and_always_a_valid_sku():
    assert make_slug("!!!") == "TOVAR" and make_slug("   ") == "TOVAR" and make_slug("«»") == "TOVAR"
    long = make_slug("А" * 200)
    assert len(long) == cu.MAX_SKU_LEN
    for name in ("../../etc/passwd", "a/b\\c:d*e?f\"g<h>i|j", "  .скрытый ", "Товар №5", "x" * 300, "-", "…", "🥤 сок", "漢字"):
        slug = make_slug(name)
        assert validate_sku(slug) is None, (name, slug)
        assert slug == slug.upper() and not slug.startswith((".", "-")) and not slug.endswith((".", "-")), (name, slug)
    assert make_slug("a..b") == "A.B" and ".." not in make_slug("x...y....z")


def test_make_sku_adds_numbers_when_taken_ignoring_case_and_stays_short():
    assert make_sku("Кола 0,5 л", []) == "KOLA-0.5-L"
    assert make_sku("Кола 0,5 л", ["KOLA-0.5-L"]) == "KOLA-0.5-L-2"
    assert make_sku("Кола 0,5 л", ["kola-0.5-l", "Kola-0.5-L-2"]) == "KOLA-0.5-L-3"
    assert make_sku("Кола 0,5 л", ["ДРУГОЕ"]) == "KOLA-0.5-L"
    long_taken = [make_slug("А" * 200)]
    sku = make_sku("А" * 200, long_taken)
    assert sku != long_taken[0] and len(sku) <= cu.MAX_SKU_LEN and sku.endswith("-2") and validate_sku(sku) is None


def test_duplicates_are_found_regardless_of_case_yo_spaces_and_punctuation():
    rows = [("1", "Кола 0,5 л"), ("2", "Ёлочка"), ("3", "Чипсы")]
    assert find_duplicate("кола  0,5 Л", rows) == ("1", "Кола 0,5 л")
    assert find_duplicate("КОЛА 0.5 л", rows) == ("1", "Кола 0,5 л")
    assert find_duplicate("елочка", rows) == ("2", "Ёлочка")
    assert find_duplicate("Кола 1 л", rows) is None
    assert find_duplicate("Кола 0,5 л", []) is None
    assert find_duplicate("!!!", rows) is None


# ------------------------------------------------------------------ фото

def test_normalize_photo_makes_a_small_valid_jpeg_and_hash():
    photo = normalize_photo(_noise_jpeg(3000, 2000))
    image = Image.open(io.BytesIO(photo.jpeg))                                  # exif-ok: проверяем итоговый файл
    assert image.format == "JPEG" and image.size == (photo.width, photo.height) == (1600, 1067)
    assert len(photo.sha12) == 12 and photo.small is False and photo.blurry is False and photo.warning() is None
    assert normalize_photo(_noise_jpeg(3000, 2000)).sha12 == photo.sha12        # тот же вход -> тот же хеш (дедупликация)
    assert normalize_photo(_noise_jpeg(400, 300, seed=2)).sha12 != normalize_photo(_noise_jpeg(400, 300, seed=3)).sha12


def test_normalize_photo_keeps_small_photos_unscaled_and_accepts_png_and_webp():
    photo = normalize_photo(_noise_jpeg(300, 200))
    assert (photo.width, photo.height) == (300, 200)
    for fmt in ("PNG", "WEBP"):
        got = normalize_photo(_noise_jpeg(300, 200, fmt=fmt))
        assert Image.open(io.BytesIO(got.jpeg)).format == "JPEG"                 # exif-ok


def test_phone_portrait_photo_is_turned_upright_and_exif_is_stripped():
    photo = normalize_photo(_noise_jpeg(300, 200, exif_orientation=6))
    assert (photo.width, photo.height) == (200, 300)                            # как увидит человек: портрет
    stored = Image.open(io.BytesIO(photo.jpeg))                                  # exif-ok
    assert stored.size == (200, 300) and not stored.getexif()                    # поворот «запечён», метаданных (GPS) нет


def test_small_and_blurry_photos_are_accepted_with_a_warning_tiny_ones_are_not():
    small = normalize_photo(_noise_jpeg(200, 150))
    assert small.small is True and "маленькое" in small.warning()
    flat = normalize_photo(_flat_jpeg())
    assert flat.blurry is True and "размыт" in flat.warning()
    assert normalize_photo(_flat_jpeg(), check_quality=False).blurry is False
    with pytest.raises(PhotoError) as err:
        normalize_photo(_noise_jpeg(40, 300))
    assert "слишком маленькое" in str(err.value) and "40×300" in str(err.value)


def test_bad_files_get_plain_russian_reasons():
    heic = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic"
    cases = [(b"", "пустой"), (heic, "HEIC"), (b"MZ\x90\x00 not an image at all", "Не удалось открыть"),
             (b"%PDF-1.4 ...", "Не удалось открыть"), (_noise_jpeg()[: len(_noise_jpeg()) // 2], "повреждено")]
    for data, fragment in cases:
        with pytest.raises(PhotoError) as err:
            normalize_photo(data)
        assert fragment in str(err.value), (fragment, str(err.value))


def test_huge_pixel_count_is_refused_before_decoding():
    buf = io.BytesIO()
    Image.new("L", (9000, 7000), 128).save(buf, "PNG")                           # 63 Мп, но сжимается в десятки КБ
    with pytest.raises(PhotoError) as err:
        normalize_photo(buf.getvalue())
    assert "слишком большое" in str(err.value)


def test_thumbnail_is_small():
    thumb = cu.make_thumbnail(normalize_photo(_noise_jpeg(1200, 900)).jpeg)
    image = Image.open(io.BytesIO(thumb))                                        # exif-ok
    assert max(image.size) == cu.THUMB_SIDE and len(thumb) < 60_000


# ------------------------------------------------------------------ сохранение

def test_save_photos_numbers_files_and_returns_refs(tmp_path):
    photos = [normalize_photo(_noise_jpeg(seed=s)) for s in (1, 2, 3)]
    saved = save_photos(tmp_path, "KOLA-0.5-L", photos)
    assert saved.new_files == 3 and saved.already_had == 0
    names = sorted(p.name for p in (tmp_path / "KOLA-0.5-L").iterdir())
    assert [n[:4] for n in names] == ["001-", "002-", "003-"] and all(n.endswith(".jpg") for n in names)
    assert saved.refs == [f"upload:KOLA-0.5-L/{p.name}" for p in cu.list_photo_files(tmp_path, "KOLA-0.5-L")]
    assert [cu.resolve_upload_ref(tmp_path, r).read_bytes() for r in saved.refs] == [p.jpeg for p in photos]
    assert not [p for p in (tmp_path / "KOLA-0.5-L").iterdir() if p.name.endswith(".part")]


def test_same_photo_twice_is_not_stored_twice_but_still_returns_its_ref(tmp_path):
    a, b = normalize_photo(_noise_jpeg(seed=1)), normalize_photo(_noise_jpeg(seed=2))
    first = save_photos(tmp_path, "X", [a, a, b])
    assert first.new_files == 2 and first.already_had == 1 and first.refs[0] == first.refs[1] != first.refs[2]
    again = save_photos(tmp_path, "X", [b])                                      # повтор после сбоя находит свой файл
    assert again.new_files == 0 and again.already_had == 1 and again.refs == [first.refs[2]]
    assert len(cu.list_photo_files(tmp_path, "X")) == 2
    third = save_photos(tmp_path, "X", [normalize_photo(_noise_jpeg(seed=3))])
    assert third.refs[0].split("/")[1].startswith("003-")                        # нумерация продолжается


def test_photo_limit_per_product_counts_only_new_photos(tmp_path):
    batch = [normalize_photo(_noise_jpeg(w=120, h=100, seed=s), check_quality=False) for s in range(cu.MAX_PHOTOS_PER_PRODUCT)]
    save_photos(tmp_path, "FULL", batch)
    save_photos(tmp_path, "FULL", batch[:3])                                     # уже лежат — лимит не нарушен
    with pytest.raises(PhotoError) as err:
        save_photos(tmp_path, "FULL", [normalize_photo(_noise_jpeg(w=120, h=100, seed=999), check_quality=False)])
    assert str(cu.MAX_PHOTOS_PER_PRODUCT) in str(err.value)
    with pytest.raises(PhotoError):
        save_photos(tmp_path, "EMPTY", [])


def test_odd_skus_get_a_safe_hashed_folder(tmp_path):
    for sku in ("../../etc", "Кола 0,5", "a/b", "..", ".hidden", "", "x" * 200):
        folder = cu.folder_name(sku)
        assert cu._SAFE_PART.match(folder) and ".." not in folder and "/" not in folder, sku
        assert folder == cu.folder_name(sku)                                      # детерминированно
    assert cu.folder_name("KOLA-0.5-L") == "KOLA-0.5-L"
    saved = save_photos(tmp_path, "../../etc", [normalize_photo(_noise_jpeg())])
    assert (tmp_path / cu.folder_name("../../etc")).is_dir() and not (tmp_path.parent / "etc").exists()
    assert cu.resolve_upload_ref(tmp_path, saved.refs[0]).is_file()


def test_list_photo_files_ignores_thumbnail_temp_and_foreign_files(tmp_path):
    save_photos(tmp_path, "P", [normalize_photo(_noise_jpeg())])
    folder = tmp_path / "P"
    (folder / "thumb.jpg").write_bytes(b"x")
    (folder / "003-abc.jpg.part").write_bytes(b"x")
    (folder / "notes.txt").write_bytes(b"x")
    assert len(cu.list_photo_files(tmp_path, "P")) == 1
    assert cu.list_photo_files(tmp_path, "NOPE") == []


# ------------------------------------------------------------------ ссылки: только внутри папки

def test_resolve_upload_ref_accepts_only_files_inside_the_uploads_folder(tmp_path):
    root = tmp_path / "uploads"
    saved = save_photos(root, "P", [normalize_photo(_noise_jpeg())])
    assert cu.resolve_upload_ref(root, saved.refs[0]).is_file()
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(b"secret")
    bad = ["upload:../secret.jpg", "upload:P/../../secret.jpg", "upload:/etc/passwd", "upload:P\\x.jpg", "upload:P/..", "upload:..",
           "upload:", "upload:P", "upload:P/a/b.jpg", "http://example.com/a.jpg", "file:///etc/passwd", "", "upload:%2e%2e/secret.jpg",
           "upload:.hidden/x.jpg", "upload:P/.x.jpg", "identify:abc.jpg", None, 5]
    (root / "P" / "a..b.jpg").write_bytes(b"x")              # файл РЕАЛЬНО есть: отказ должен прийти из-за «..», а не из-за «не найден»
    bad.append("upload:P/a..b.jpg")                         # «..» не бывает в именах вообще — единая политика проекта (is_safe_path_segment)
    for ref in bad:
        with pytest.raises(PhotoError):
            cu.resolve_upload_ref(root, ref)
    with pytest.raises(PhotoError) as err:
        cu.resolve_upload_ref(root, "upload:P/001-aaaaaaaaaaaa.jpg")
    assert "не найден" in str(err.value)


def test_symlink_escaping_the_folder_is_refused(tmp_path):
    root = tmp_path / "uploads"
    (root / "P").mkdir(parents=True)
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(b"secret")
    link = root / "P" / "001-aaaaaaaaaaaa.jpg"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        return                                                                    # ФС без символьных ссылок — проверять нечего
    with pytest.raises(PhotoError):
        cu.resolve_upload_ref(root, "upload:P/001-aaaaaaaaaaaa.jpg")


def test_is_ref_helpers():
    assert cu.is_upload_ref("upload:a/b.jpg") and not cu.is_upload_ref("http://a/b.jpg") and not cu.is_upload_ref(None)
    assert cu.is_identify_ref("identify:abc.jpg") and not cu.is_identify_ref("upload:a/b.jpg")


# ------------------------------------------------------------------ временные кадры

def test_identify_frames_are_saved_resolved_and_swept(tmp_path):
    root = tmp_path / "tmp"
    token = "a" * 32
    ref = cu.save_identify_frame(root, token, b"jpegbytes")
    assert ref == f"identify:{token}.jpg"
    assert cu.resolve_ref(root, cu.IDENTIFY_REF_PREFIX, ref).read_bytes() == b"jpegbytes"
    for bad_token in ("../x", "short", "G" * 32, "a" * 31, ""):
        with pytest.raises(PhotoError):
            cu.save_identify_frame(root, bad_token, b"x")
    with pytest.raises(PhotoError):
        cu.resolve_ref(root, cu.IDENTIFY_REF_PREFIX, "identify:../a.jpg")
    old = root / ("b" * 32 + ".jpg")
    old.write_bytes(b"old")
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    assert cu.sweep_old_files(root, max_age_seconds=3600, now=time.time()) == 1
    assert not old.exists() and (root / f"{token}.jpg").exists()
    assert cu.sweep_old_files(tmp_path / "нет-такой", max_age_seconds=1, now=time.time()) == 0


# ------------------------------------------------------------------ состояние задачи

def test_describe_task_maps_celery_states_to_simple_ones():
    done = describe_task("SUCCESS", "product-id-1")
    assert done["state"] == "done" and done["product_id"] == "product-id-1"
    for status in ("PENDING", "STARTED", "RETRY", "RECEIVED"):
        assert describe_task(status, None)["state"] == "working"
    ours = describe_task("FAILURE", PhotoError("Фото повреждено", code="photo_corrupted"))
    assert ours == {"state": "failed", "message": "Фото повреждено", "product_id": None, "detail": None,
                    "code": "photo_corrupted", "params": {}}
    other = describe_task("FAILURE", RuntimeError("connection refused: postgres:5432 at /srv/app/data"))
    assert other["state"] == "failed" and other["message"] == cu.GENERIC_FAILURE and other["code"] == "task_failed"
    # единая система ошибок: текст чужого исключения клиенту не отдаётся вовсе (раньше уходил в detail «для специалиста»)
    assert "postgres" not in str(other) and "/srv" not in str(other) and other["detail"] is None
    assert describe_task("REVOKED", None)["state"] == "failed"


def test_describe_task_recognises_our_errors_even_when_celery_rebuilt_the_class():
    Rebuilt = type("CatalogAddError", (Exception,), {})                          # Celery при сбое импорта создаёт класс с тем же именем
    assert describe_task("FAILURE", Rebuilt("Товар уже есть"))["message"] == "Товар уже есть"
    assert describe_task("FAILURE", Rebuilt(""))["message"] == cu.GENERIC_FAILURE
    # пересозданный по имени класс с аргументами новой системы (текст, код, параметры): текст — первый аргумент, код сохраняется
    rebuilt = describe_task("FAILURE", Rebuilt("Фото повреждено", "photo_corrupted", {}))
    assert rebuilt["message"] == "Фото повреждено" and rebuilt["code"] == "photo_corrupted"


def test_error_classes_form_one_family_so_the_worker_can_skip_retries():
    assert issubclass(PhotoError, CatalogAddError) and issubclass(InputError, CatalogAddError)


# ------------------------------------------------------------------ журнал проиндексированных фото и план доиндексации

def test_manifest_round_trip_atomic_and_ref_prefix_is_optional(tmp_path):
    path = tmp_path / "models" / "index" / "uploads_manifest.json"
    manifest = cu.UploadsManifest.load(path)
    assert manifest.keys == set() and not manifest.has("A/001-aaaaaaaaaaaa.jpg")
    manifest.add(["upload:A/001-aaaaaaaaaaaa.jpg", "B/001-bbbbbbbbbbbb.jpg"])
    manifest.save()
    again = cu.UploadsManifest.load(path)
    assert again.keys == {"A/001-aaaaaaaaaaaa.jpg", "B/001-bbbbbbbbbbbb.jpg"}
    assert again.has("upload:A/001-aaaaaaaaaaaa.jpg") and again.has("A/001-aaaaaaaaaaaa.jpg") and not again.has("A/002-cccccccccccc.jpg")
    assert not list(path.parent.glob("*.part"))
    assert cu.manifest_path_for("models/index/catalog.faiss") == Path("models/index/uploads_manifest.json")


def test_corrupt_manifest_is_reported_plainly_and_never_overwritten_by_the_worker_helper(tmp_path):
    path = tmp_path / "uploads_manifest.json"
    for bad in ("{not json", "[]", '{"photos": "x"}', '{"photos": [1, 2]}', '{"version": 1}'):
        path.write_text(bad, encoding="utf-8")
        with pytest.raises(CatalogAddError) as err:
            cu.UploadsManifest.load(path)
        assert "повреждён" in str(err.value), bad
        assert cu.record_indexed(tmp_path / "catalog.faiss", ["upload:A/001-aaaaaaaaaaaa.jpg"]) is False, bad
        assert path.read_text(encoding="utf-8") == bad                       # повреждённый журнал не затираем молча


def test_record_indexed_adds_to_the_manifest_next_to_the_index(tmp_path):
    index_path = tmp_path / "models" / "index" / "catalog.faiss"
    assert cu.record_indexed(index_path, ["upload:A/001-aaaaaaaaaaaa.jpg"]) is True
    assert cu.record_indexed(index_path, ["upload:A/002-bbbbbbbbbbbb.jpg"]) is True
    assert cu.UploadsManifest.load(cu.manifest_path_for(index_path)).keys == {"A/001-aaaaaaaaaaaa.jpg", "A/002-bbbbbbbbbbbb.jpg"}
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert cu.record_indexed(blocker / "inside" / "catalog.faiss", ["upload:A/1.jpg"]) is False         # нельзя создать папку — не падаем


def test_plan_reindex_lists_only_photos_missing_from_the_manifest(tmp_path):
    root = tmp_path / "uploads"
    a = save_photos(root, "KOLA", [normalize_photo(_noise_jpeg(seed=s)) for s in (1, 2, 3)])
    save_photos(root, "LIME", [normalize_photo(_noise_jpeg(seed=4))])
    manifest = cu.UploadsManifest(tmp_path / "m.json")
    products = [("id-kola", "KOLA", "Кола"), ("id-lime", "LIME", "Лайм"), ("id-none", "NOFOTO", "Без фото")]
    plan = cu.plan_reindex(products, root, manifest)
    assert [(i.product_id, len(i.files)) for i in plan] == [("id-kola", 3), ("id-lime", 1)]
    manifest.add(a.refs[:2])                                                    # два фото Колы уже проиндексированы
    plan = cu.plan_reindex(products, root, manifest)
    assert [(i.product_id, [f.name for f in i.files]) for i in plan] == [
        ("id-kola", [a.refs[2].split("/")[1]]), ("id-lime", [cu.list_photo_files(root, "LIME")[0].name])]
    manifest.add(a.refs[2:])
    manifest.add([f"LIME/{cu.list_photo_files(root, 'LIME')[0].name}"])
    assert cu.plan_reindex(products, root, manifest) == []
    assert cu.plan_reindex([("id-x", "ORPHAN", "Нет в базе")], root, manifest) == []     # папка без товара в списке игнорируется


def test_manifest_save_merges_with_marks_written_by_another_process_meanwhile(tmp_path):
    """2026-11-19: два процесса загрузили журнал, каждый добавил своё — раньше второй save() стирал отметки первого."""
    path = tmp_path / "uploads_manifest.json"
    worker, script = cu.UploadsManifest.load(path), cu.UploadsManifest.load(path)
    worker.add(["upload:A/001-aaaaaaaaaaaa.jpg"])
    script.add(["upload:B/001-bbbbbbbbbbbb.jpg"])
    worker.save()
    script.save()
    both = {"A/001-aaaaaaaaaaaa.jpg", "B/001-bbbbbbbbbbbb.jpg"}
    assert cu.UploadsManifest.load(path).keys == both
    assert script.keys == both                                 # после save() объект видит и чужие отметки
    assert not list(tmp_path.glob("*.part"))


def test_manifest_save_keeps_marks_dropped_meanwhile_by_reconcile(tmp_path):
    """Сверка сняла устаревшую отметку, пока воркер держал журнал в памяти: save() воркера не должен её вернуть."""
    path = tmp_path / "uploads_manifest.json"
    first = cu.UploadsManifest.load(path)
    first.add(["A/001-aaaaaaaaaaaa.jpg", "OLD/001-cccccccccccc.jpg"])
    first.save()
    worker = cu.UploadsManifest.load(path)
    assert cu.drop_manifest_keys(path, ["OLD/001-cccccccccccc.jpg"]) == 1
    worker.add(["upload:B/001-bbbbbbbbbbbb.jpg"])
    worker.save()
    assert cu.UploadsManifest.load(path).keys == {"A/001-aaaaaaaaaaaa.jpg", "B/001-bbbbbbbbbbbb.jpg"}
    assert cu.drop_manifest_keys(path, ["NOPE/1.jpg"]) == 0
    assert cu.drop_manifest_keys(tmp_path / "missing.json", ["A/001-aaaaaaaaaaaa.jpg"]) == 0


def test_manifest_waits_for_the_lock_and_gives_up_without_breaking_the_add(tmp_path, monkeypatch):
    """Журнал занят другим процессом: запись ждёт блокировку; дольше срока — record_indexed возвращает False, файл не тронут."""
    from app.pipeline.index_store import IndexBusyError
    index_path = tmp_path / "catalog.faiss"
    journal = cu.manifest_path_for(index_path)
    assert cu.record_indexed(index_path, ["upload:A/001-aaaaaaaaaaaa.jpg"]) is True
    before = journal.read_bytes()
    monkeypatch.setattr(cu, "MANIFEST_LOCK_TIMEOUT_SECONDS", 0.2)
    with cu.manifest_lock(journal) as held:
        if not held.locked:
            pytest.skip("файловая система без блокировок")
        assert cu.record_indexed(index_path, ["upload:B/001-bbbbbbbbbbbb.jpg"]) is False
        with pytest.raises(IndexBusyError):
            cu.drop_manifest_keys(journal, ["A/001-aaaaaaaaaaaa.jpg"])
    assert journal.read_bytes() == before
    assert cu.record_indexed(index_path, ["upload:B/001-bbbbbbbbbbbb.jpg"]) is True       # блокировка снята — пишется


def test_manifest_lock_is_its_own_file_not_the_index_lock(tmp_path):
    """Журнал пишут и под блокировкой индекса: общая блокировка зависла бы при вложенном взятии."""
    journal = cu.manifest_path_for(tmp_path / "catalog.faiss")
    assert cu.manifest_lock(journal).path.name == "uploads_manifest.json.lock"


def test_plan_reindex_uses_the_hashed_folder_for_odd_skus(tmp_path):
    root = tmp_path / "uploads"
    saved = save_photos(root, "Кола 0,5", [normalize_photo(_noise_jpeg(seed=1))])
    folder = saved.refs[0].split("/")[0].split(":")[1]
    assert folder.startswith("x-")
    manifest = cu.UploadsManifest(tmp_path / "m.json")
    assert len(cu.plan_reindex([("id", "Кола 0,5", "Кола")], root, manifest)) == 1
    manifest.add(saved.refs)
    assert cu.plan_reindex([("id", "Кола 0,5", "Кола")], root, manifest) == []


# ----------------------------------------------------------------------- 2026-11-07: запись файлов фото

def test_atomic_write_uses_a_unique_temp_file_per_call(tmp_path, monkeypatch):
    """Два одновременных запроса с одним фото не должны делить временный файл (раньше — всегда «<имя>.part»)."""
    import os as _os
    from app.services import catalog_upload as cu
    sources = []
    real_replace = _os.replace

    def spy(src, dst):
        sources.append(str(src))
        real_replace(src, dst)

    monkeypatch.setattr(cu.os, "replace", spy)
    cu._atomic_write(tmp_path / "001-abc.jpg", b"one")
    cu._atomic_write(tmp_path / "001-abc.jpg", b"two")
    assert len(set(sources)) == 2 and (tmp_path / "001-abc.jpg").read_bytes() == b"two"


def test_atomic_write_failure_keeps_the_old_file_and_leaves_no_temp_file(tmp_path, monkeypatch):
    from app.services import catalog_upload as cu
    target = tmp_path / "001-abc.jpg"
    cu._atomic_write(target, b"old")

    def disk_full(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cu.os, "fsync", disk_full)
    import pytest as _pytest
    with _pytest.raises(OSError):
        cu._atomic_write(target, b"new")
    assert target.read_bytes() == b"old" and sorted(p.name for p in tmp_path.iterdir()) == ["001-abc.jpg"]


def test_concurrent_writes_of_one_file_never_produce_a_mixed_file(tmp_path):
    import threading
    from app.services import catalog_upload as cu
    target = tmp_path / "001-abc.jpg"
    payloads = [bytes([65 + i]) * (200_000 + i * 50_000) for i in range(4)]
    errors = []

    def writer(data):
        try:
            for _ in range(20):
                cu._atomic_write(target, data)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(p,)) for p in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and target.read_bytes() in payloads
    assert sorted(p.name for p in tmp_path.iterdir()) == ["001-abc.jpg"]
