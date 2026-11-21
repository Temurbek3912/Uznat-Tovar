"""
Полнота индекса и восстановление (app/services/index_coverage.py, scripts/reindex_uploads.py, задача recover_index; этап 4).

Сценарий владельца: товар записан в PostgreSQL -> процесс погиб до сохранения векторов -> задача Celery потеряна -> программа
запущена снова. Система должна сама найти такой товар, точечно добавить недостающие векторы, не создать дублей при повторе,
честно сообщить о товаре, у которого нет фото. И отдельно — связь «фото <-> вектор»: что обнаруживается и что восстанавливается.
"""
from __future__ import annotations

import hashlib
import io
import sys
import types
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.pipeline import matcher as M
from app.services import catalog_add as CA
from app.services import index_coverage as C
from app.services import index_reconcile as R
from app.services.catalog_upload import folder_name, list_photo_files, record_indexed, upload_ref

ROOT = Path(__file__).resolve().parents[1]


class Embedder:
    """Вектор = средний цвет фото (как ColorEmbedder тестов распознавания); считает вызовы."""

    def __init__(self):
        self.calls = 0

    def embed(self, images):
        self.calls += len(images)
        out = []
        for img in images:
            v = np.asarray(img, dtype=np.float32).reshape(-1, 3).mean(axis=0)
            out.append(v / (np.linalg.norm(v) or 1))
        return np.stack(out)


def _jpeg(rgb):
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), rgb).save(buf, "JPEG", quality=95)
    return buf.getvalue()


def _save_photo(uploads: Path, sku: str, n: int, rgb) -> str:
    data = _jpeg(rgb)
    name = f"{n:03d}-{hashlib.sha256(data).hexdigest()[:12]}.jpg"
    folder = uploads / folder_name(sku)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(data)
    return upload_ref(folder.name, name)


def _load(path):
    return np.array(Image.open(path).convert("RGB"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "faiss", None)
    e = types.SimpleNamespace(tmp=tmp_path, uploads=tmp_path / "media" / "catalog-uploads", path=str(tmp_path / "idx" / "catalog.faiss"),
                              embedder=Embedder(), products={})
    e.index = lambda: M.open_catalog_index(e.path, 3)
    return e


def _recover(env, index=None, limit=200, manifest_keys=None):
    index = index or env.index()
    return index, C.recover_pending(index, env.products, env.uploads, embedder=env.embedder, index_path=env.path,
                                    folder_of=folder_name, list_photos=list_photo_files, upload_ref=upload_ref, load_image=_load,
                                    manifest_keys=manifest_keys, limit=limit)


def _coverage(env, index=None, **kw):
    return C.analyze_coverage(index or env.index(), env.products, env.uploads, folder_of=folder_name, list_photos=list_photo_files,
                              upload_ref=upload_ref, **kw)


# --------------------------------------------------------------------------- сценарий владельца

def test_product_committed_then_crash_and_lost_task_is_found_and_recovered_on_restart(env):
    refs = [_save_photo(env.uploads, "KOLA", 1, (200, 10, 10)), _save_photo(env.uploads, "KOLA", 2, (190, 20, 10))]
    env.products["id-kola"] = "KOLA"                       # 1) товар в базе; 2) векторы не сохранены; 3) задачи нет
    report = _coverage(env)                                 # 4) запуск: обнаружение
    assert [(p, r) for p, _s, r in report.photos_not_indexed] == [("id-kola", r) for r in refs]
    assert report.products_without_vectors == [("id-kola", "KOLA")]
    index, res = _recover(env)                              # автоматическое восстановление (то, что делает recover_index)
    assert sorted(r for _p, r in res.added) == sorted(refs) and res.remaining == 0
    reloaded = env.index()
    assert reloaded._ids == ["id-kola", "id-kola"] and sorted(reloaded._sources) == sorted(refs)
    assert _coverage(env).complete


def test_recovery_repeated_creates_no_duplicates(env):
    _save_photo(env.uploads, "KOLA", 1, (200, 10, 10))
    env.products["id-kola"] = "KOLA"
    _recover(env)
    calls = env.embedder.calls
    _index, again = _recover(env)                            # второй запуск воркера
    assert again.added == [] and env.embedder.calls == calls and len(env.index()._ids) == 1


def test_retry_of_the_add_task_after_recovery_adds_nothing(env):
    """Восстановление успело раньше, чем Celery повторил потерянную было задачу: повтор не должен добавить векторы ещё раз."""
    ref = _save_photo(env.uploads, "KOLA", 1, (200, 10, 10))
    env.products["id-kola"] = "KOLA"
    _recover(env)
    store = types.SimpleNamespace(find_id_by_sku=lambda sku: "id-kola", create=lambda p: pytest.fail("товар уже есть"))
    index = env.index()
    product_in = types.SimpleNamespace(sku="KOLA", reference_image_urls=[ref])
    out = CA.add_product(product_in, [_load(env.uploads / ref[len("upload:"):])], store=store, embedder=env.embedder, index=index,
                         index_path=env.path, not_indexed=C.not_indexed_filter(index))
    assert out.vectors_added == 0 and not out.created and len(env.index()._ids) == 1


def test_product_whose_photos_are_gone_is_reported_and_recovery_does_not_loop(env):
    env.products["id-gone"] = "GONE"                        # фото нет (удалены, или товар добавлен по ссылкам)
    report = _coverage(env)
    assert report.products_without_vectors_or_photos == [("id-gone", "GONE")]
    _index, res = _recover(env)
    assert res.added == [] and not res.progressed and res.remaining == 0     # задача не поставит себя снова


def test_unreadable_photo_is_reported_and_the_rest_is_recovered(env):
    good = _save_photo(env.uploads, "KOLA", 1, (200, 10, 10))
    bad = env.uploads / "KOLA" / "002-0123456789ab.jpg"
    bad.write_bytes(b"not a jpeg")
    env.products["id-kola"] = "KOLA"
    _index, res = _recover(env)
    assert [r for _p, r in res.added] == [good] and len(res.failed) == 1 and res.failed[0][1].endswith(bad.name)


def test_recovery_works_in_batches_and_reports_what_remains(env):
    for i in range(5):
        _save_photo(env.uploads, f"P{i}", 1, (40 * i + 10, 10, 10))
        env.products[f"id-{i}"] = f"P{i}"
    _index, first = _recover(env, limit=2)
    assert len(first.added) == 2 and first.remaining == 3
    _index, second = _recover(env, limit=10)
    assert len(second.added) == 3 and second.remaining == 0 and len(env.index()._ids) == 5


def test_photo_folder_without_a_product_in_the_database_is_ignored(env):
    _save_photo(env.uploads, "ORPHAN", 1, (10, 10, 200))   # фото сохранено API, а задача не дошла и товара нет
    _index, res = _recover(env)
    assert res.added == [] and env.index()._ids == []


# --------------------------------------------------------------------------- связь фото и векторов

def test_partial_loss_is_detected_and_restored_point_by_point(env):
    refs = [_save_photo(env.uploads, "KOLA", n, (200, 10 * n, 10)) for n in (1, 2, 3)]
    env.products["id-kola"] = "KOLA"
    index = env.index()
    index.add("id-kola", Embedder().embed([_load(env.uploads / refs[0][7:])]), [refs[0]])      # уцелел вектор одного фото
    index.save(env.path)
    report = _coverage(env)
    assert [r for _p, _s, r in report.photos_not_indexed] == refs[1:] and report.products_without_vectors == []
    _index, res = _recover(env)
    assert [r for _p, r in res.added] == refs[1:] and env.embedder.calls == 2                # только недостающие
    assert sorted(env.index()._sources) == sorted(refs)


def test_vector_whose_photo_was_deleted_is_detected_but_kept(env):
    ref = _save_photo(env.uploads, "KOLA", 1, (200, 10, 10))
    env.products["id-kola"] = "KOLA"
    _recover(env)
    (env.uploads / ref[7:]).unlink()
    report = _coverage(env)
    assert report.vectors_without_photo == [("id-kola", ref)]
    assert env.index()._ids == ["id-kola"]                   # вектор не удаляется: он по-прежнему описывает товар


def test_reindexing_the_same_photo_is_detected_and_removed_by_reconcile(env):
    ref = _save_photo(env.uploads, "KOLA", 1, (200, 10, 10))
    env.products["id-kola"] = "KOLA"
    index = env.index()
    vec = Embedder().embed([_load(env.uploads / ref[7:])])
    index.add("id-kola", vec, [ref])
    index.add("id-kola", vec, [ref])                         # повторное индексирование (старый журнал потерялся)
    index.save(env.path)
    assert _coverage(env).duplicate_sources == {ref: 2}
    rep = R.analyze(env.index(), env.products)
    assert rep.duplicate_vectors == 1 and not rep.clean
    res = R.apply(env.path, 3, env.products, out=lambda *_: None)
    assert res.removed_vectors == 1 and res.verified and env.index()._sources == [ref]


def test_photo_changed_after_indexing_is_detected_when_asked(env):
    ref = _save_photo(env.uploads, "KOLA", 1, (200, 10, 10))
    env.products["id-kola"] = "KOLA"
    _recover(env)
    (env.uploads / ref[7:]).write_bytes(_jpeg((10, 200, 10)))   # файл заменён другим содержимым под тем же именем
    assert _coverage(env).changed_photos == []                  # без проверки содержимого — не видно (дорого: читает всё)
    report = _coverage(env, verify_content=True)
    assert report.changed_photos == [("id-kola", ref)] and not report.complete


def test_vector_of_a_photo_from_another_products_folder_is_detected(env):
    ref = _save_photo(env.uploads, "FANTA", 1, (250, 120, 10))
    env.products.update({"id-kola": "KOLA", "id-fanta": "FANTA"})
    index = env.index()
    index.add("id-kola", Embedder().embed([_load(env.uploads / ref[7:])]), [ref])   # фото «Фанты» приписано «Коле»
    index.save(env.path)
    report = _coverage(env)
    assert report.foreign_photo_vectors == [("id-kola", ref)]


def test_legacy_vectors_without_source_trust_the_journal_and_are_reported(env):
    ref = _save_photo(env.uploads, "KOLA", 1, (200, 10, 10))
    env.products["id-kola"] = "KOLA"
    index = env.index()
    index.add("id-kola", Embedder().embed([_load(env.uploads / ref[7:])]))          # версия до 07.11: без источника
    index.save(env.path)
    assert record_indexed(env.path, [ref])
    keys = {ref[7:]}
    report = _coverage(env, manifest_keys=keys)
    assert report.photos_not_indexed == [] and report.legacy_vectors == {"id-kola": 1}
    _index, res = _recover(env, manifest_keys=keys)
    assert res.added == []                                                           # не дублирует
    assert [r for _p, _s, r in _coverage(env).photos_not_indexed] == [ref]           # без журнала — честно «не проверено»


# --------------------------------------------------------------------------- скрипт и воркер

def test_reindex_uploads_script_uses_the_index_not_the_journal(env, monkeypatch):
    from scripts import reindex_uploads
    ref = _save_photo(env.uploads, "KOLA", 1, (200, 10, 10))
    env.products["id-kola"] = "KOLA"
    assert record_indexed(env.path, [ref])                     # журнал ошибочно считает фото проиндексированным
    settings = types.SimpleNamespace(matcher=types.SimpleNamespace(index_path=env.path),
                                     embedder=types.SimpleNamespace(embedding_dim=3, weights_path="x"),
                                     media_root=str(env.tmp / "media"))
    lines = []
    common = dict(out=lines.append, load_products=lambda: dict(env.products), settings=settings, resolve_path=lambda p: Path(p),
                  make_embedder=lambda s: env.embedder, load_image=_load)
    assert reindex_uploads.main([], **common) == 0 and "1 фото" in "\n".join(lines) and env.index()._ids == []
    assert reindex_uploads.main(["--apply", "--yes"], **common) == 0
    assert env.index()._sources == [ref]
    lines.clear()
    assert reindex_uploads.main(["--apply", "--yes"], **common) == 0 and "Нечего добавлять" in "\n".join(lines)

def test_reindex_uploads_script_does_not_bring_back_excluded_photos(env):
    """Вектор выведенного из эталонов фото убран (app/services/index_prune.py) — скрипт не называет фото пропущенным и не
    добавляет его обратно."""
    from scripts import reindex_uploads
    gone = _save_photo(env.uploads, "KOLA", 1, (200, 10, 10))
    env.products["id-kola"] = "KOLA"
    settings = types.SimpleNamespace(matcher=types.SimpleNamespace(index_path=env.path),
                                     embedder=types.SimpleNamespace(embedding_dim=3, weights_path="x"),
                                     media_root=str(env.tmp / "media"))
    lines = []
    common = dict(out=lines.append, load_products=lambda: dict(env.products), settings=settings, resolve_path=lambda p: Path(p),
                  make_embedder=lambda s: env.embedder, load_image=_load, load_excluded=lambda: frozenset({("id-kola", gone)}))
    assert reindex_uploads.main(["--apply", "--yes"], **common) == 0 and "Нечего добавлять" in "\n".join(lines)
    assert env.embedder.calls == 0


def _func(name):
    import ast
    tree = ast.parse((ROOT / "app" / "worker.py").read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


def test_worker_schedules_recovery_on_every_start_and_never_raises_from_the_signal():
    import ast
    fn = _func("_schedule_recovery")
    assert any(isinstance(d, ast.Attribute) and d.attr == "connect" and getattr(d.value, "id", "") == "worker_ready"
               for d in fn.decorator_list)
    src = ast.unparse(fn)
    # с 2026-11-19 — через _enqueue_recovery (сразу при запуске) и затем по расписанию (tests/test_recovery_schedule.py)
    assert "_enqueue_recovery()" in src and "_start_recovery_schedule()" in src
    assert "except Exception" in src and "raise" not in src
    assert "recover_index_task.delay()" in ast.unparse(_func("_enqueue_recovery"))


def test_recovery_task_takes_products_from_the_database_and_stops_without_progress():
    src = ast_src = __import__("ast").unparse(_func("recover_index_task"))
    assert "db.query(Product.id, Product.sku)" in src and "recover_pending(" in src
    assert "if res.remaining and res.progressed:" in ast_src and "_resync_index_after_failure" in src


def test_worker_decides_what_is_indexed_by_the_index_itself():
    import ast
    for task in ("add_catalog_product_task", "add_catalog_photos_task"):
        src = ast.unparse(_func(task))
        assert "_not_indexed(index_path" in src and "not_yet_indexed" not in src, task


# --------------------------------------------------------------------------- фото импорта сверяются по записям о фото (2026-11-21)
# Раньше векторы фото импорта (на рабочей машине — все 1798) только считались: «Расхождений нет» ничего о них не говорило.

def _import_env(env):
    media = env.tmp / "media"
    photos = {}
    for n, (pid, rgb) in enumerate([("id-kola", (200, 10, 10)), ("id-kola", (190, 20, 10)), ("id-fanta", (250, 140, 0))]):
        data = _jpeg(rgb)
        sha = hashlib.sha256(data).hexdigest()
        rel = f"catalog-imported/{pid}/{sha[:16]}.jpg"
        (media / rel).parent.mkdir(parents=True, exist_ok=True)
        (media / rel).write_bytes(data)
        photos[n] = C.ImportPhoto(pid, f"import:{sha}", rel, sha, True)
    env.products.update({"id-kola": "KOLA", "id-fanta": "FANTA"})
    index = env.index()
    for rec in photos.values():
        index.add(rec.product_id, np.atleast_2d(_unit3(rec.sha256)), [rec.source_ref])
    index.save(env.path)
    return media, photos


def _unit3(seed: str):
    v = np.array([int(seed[i:i + 2], 16) + 1 for i in (0, 2, 4)], dtype=np.float32)
    return v / np.linalg.norm(v)


def test_import_photos_are_checked_against_their_records_and_a_clean_catalog_is_complete(env):
    media, photos = _import_env(env)
    rep = _coverage(env, import_photos=photos.values(), media_root=media, verify_content=True)
    assert rep.imports_checked and rep.import_vectors == 3 and rep.complete
    assert not (rep.vectors_without_photo or rep.changed_photos or rep.import_photos_not_indexed)
    assert "Векторы фото импорта: 3; без записи о фото у товара: 0" in "\n".join(C.describe(rep))


def test_missing_or_changed_import_photo_is_found_changed_only_with_verify(env):
    media, photos = _import_env(env)
    (media / photos[0].storage_path).unlink()
    p = media / photos[1].storage_path
    p.write_bytes(p.read_bytes() + b"x")
    rep = _coverage(env, import_photos=photos.values(), media_root=media)
    assert rep.vectors_without_photo == [("id-kola", photos[0].source_ref)] and rep.changed_photos == [] and not rep.complete
    rep = _coverage(env, import_photos=photos.values(), media_root=media, verify_content=True)
    assert rep.changed_photos == [("id-kola", photos[1].source_ref)]


def test_import_vector_of_another_product_or_without_a_record_is_reported(env):
    media, photos = _import_env(env)
    records = [photos[0], photos[1], C.ImportPhoto("id-kola", photos[2].source_ref, photos[2].storage_path, photos[2].sha256, True)]
    rep = _coverage(env, import_photos=records, media_root=media)
    assert rep.foreign_photo_vectors == [("id-fanta", photos[2].source_ref)]
    rep = _coverage(env, import_photos=[photos[0], photos[1]], media_root=media)
    assert rep.import_vectors_without_record == [("id-fanta", photos[2].source_ref)] and not rep.complete


def test_active_import_reference_without_a_vector_is_reported_and_product_is_not_called_photoless(env):
    media, photos = _import_env(env)
    env.products["id-water"] = "WATER"
    extra = C.ImportPhoto("id-water", "import:" + "ab" * 32, "catalog-imported/id-water/x.jpg", "ab" * 32, True)
    rep = _coverage(env, import_photos=[*photos.values(), extra], media_root=media)
    assert rep.import_photos_not_indexed == [("id-water", extra.source_ref)]
    assert ("id-water", "WATER") in rep.products_without_vectors and rep.products_without_vectors_or_photos == []


def test_excluded_inactive_and_copyless_import_photos_are_not_alarms(env):
    media, photos = _import_env(env)
    (media / photos[0].storage_path).unlink()
    rep = _coverage(env, import_photos=photos.values(), media_root=media, skip={("id-kola", photos[0].source_ref)})
    assert rep.vectors_without_photo == [] and rep.import_photos_not_indexed == []
    inactive = C.ImportPhoto("id-kola", "import:" + "cd" * 32, None, "cd" * 32, False)         # выведенный, вектора нет — норма
    copyless = C.ImportPhoto("id-fanta", photos[2].source_ref, None, photos[2].sha256, True)  # импорт до копирования фото
    rep = _coverage(env, import_photos=[photos[0], photos[1], inactive, copyless], media_root=media)
    assert rep.import_vectors_without_copy == 1 and rep.import_photos_not_indexed == []


def test_import_record_path_outside_the_data_folder_is_never_read(env, tmp_path):
    media, photos = _import_env(env)
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"secret")
    rec = photos[0]
    bad = [C.ImportPhoto(rec.product_id, rec.source_ref, "../outside.jpg", rec.sha256, True),
           C.ImportPhoto("id-fanta", photos[2].source_ref, str(outside), photos[2].sha256, True), photos[1]]
    rep = _coverage(env, import_photos=bad, media_root=media, verify_content=True)
    assert sorted(rep.vectors_without_photo) == sorted([("id-kola", rec.source_ref), ("id-fanta", photos[2].source_ref)])
    assert rep.changed_photos == []


def test_without_records_import_vectors_are_only_counted_as_before(env):
    media, photos = _import_env(env)
    (media / photos[0].storage_path).unlink()
    rep = _coverage(env)
    assert not rep.imports_checked and rep.import_vectors == 3 and rep.vectors_without_photo == []
    assert "не проверялись" in "\n".join(C.describe(rep))
