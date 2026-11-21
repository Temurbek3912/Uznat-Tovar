"""Облегчённый режим и индекс другой модели (2026-11-21).

В рабочем data/lite индекс был построен DINOv2, а в настройках уже SigLIP 2: «matcher» не загружался, «Узнать товар» по фото
не работал, владельцу — технический текст со ссылкой на служебный скрипт, который он не может запустить с переменными
облегчённого режима. Теперь облегчённый режим при запуске сам пересобирает индекс текущей моделью из активных эталонов базы
(scripts/rebuild_index.py — та же реализация), прежние файлы — копией рядом, векторы прежней модели остаются в кэше базы.
Полный режим сам не пересобирает (несколько процессов воркера, большой каталог) — там понятная команда.

Здесь база и пересборка НАСТОЯЩИЕ (SQLite с миграциями, numpy-индекс, scripts/rebuild_index.main), подделана только модель."""
from __future__ import annotations

import types
from pathlib import Path

import numpy as np
import pytest

from app.db.models import ImageEmbedding
from app.pipeline import matcher as M
from app.pipeline import ml_runtime as mr
from app.pipeline.embedder import model_version
from app.pipeline.index_store import IndexIntegrityError, IndexModelMismatchError
from tests.test_product_images import SIG, Embedder, _add_with_photos, _load, database, env  # noqa: F401 — фикстуры

NEW_SIG = {**SIG, "name": "new-model", "weights": "sha256:bbb", "preprocess": "pooler/v1"}


class NewEmbedder(Embedder):
    """Другая модель той же размерности: другой паспорт и другие векторы."""

    def embed(self, images):
        return super().embed(images)[:, ::-1].copy()


def _settings(env):
    return types.SimpleNamespace(detector=None, embedder=types.SimpleNamespace(name="e", embedding_dim=3, weights_path="x"),
                                 matcher=types.SimpleNamespace(index_path=env.path), media_root=str(env.media))


def _factories(env, embedder, rebuild=None):
    calls = []

    def real_rebuild(settings, emb):
        calls.append(emb)
        return mr._rebuild_for_model(settings, emb, make_session=env.Session, resolve_path=lambda p: Path(p),
                                     load_image=_load)

    f = mr.Factories(face_blurrer=lambda: "blur", detector=lambda *a: "yolo", heuristic_detector=lambda: "heur",
                     embedder=lambda cfg: embedder, debug_embedder=lambda: embedder,
                     index=M.open_catalog_index, matcher=lambda index, cfg: types.SimpleNamespace(index=index),
                     lock_check=lambda target: True, rebuild_for_model=rebuild or real_rebuild)
    return f, calls


def _old_index(env):
    """Каталог из двух товаров, проиндексированный ПРЕЖНЕЙ моделью (SIG): записи о фото и кэш — тоже её."""
    _add_with_photos(env, "p1", "KOLA", [(200, 10, 10), (190, 20, 10)])
    _add_with_photos(env, "p2", "FANTA", [(250, 150, 0)])
    return Path(env.path + ".npz").read_bytes()


def test_lite_mode_rebuilds_an_index_of_another_model_with_the_current_one(env):
    before = _old_index(env)
    new = NewEmbedder(NEW_SIG)
    f, calls = _factories(env, new)
    rt = mr.build_runtime(_settings(env), {"LITE_MODE": "1"}, f)
    comp = rt.component("matcher")
    assert comp.status == mr.OK and rt.ready, comp.error
    assert mr.INDEX_REBUILT_MARK in comp.detail and "посчитано 3" in comp.detail and "Копия прежнего индекса" in comp.detail
    assert calls == [new]                                           # модель — уже загруженная, вторая не грузится
    index = rt.matcher.index
    assert M.model_mismatch(index.model, NEW_SIG) is None and sorted(index._ids) == ["p1", "p1", "p2"]
    assert new.calls == 3                                           # векторы посчитаны новой моделью
    # «Узнать товар»: фото товара находит свой товар в новом индексе
    img = _load(next((env.uploads / "FANTA").iterdir()))
    hits = index.search(new.embed([img])[0], 1)
    assert hits[0][0] == "p2"
    # прежний индекс — копией рядом, байт в байт; векторы прежней модели остались в кэше базы (вернуться — без пересчёта)
    backups = [p for b in Path(env.path).parent.glob("backup-*") for p in b.iterdir() if p.name.endswith(".npz")]
    assert len(backups) == 1 and backups[0].read_bytes() == before
    env.db.expire_all()
    versions = {v for (v,) in env.db.query(ImageEmbedding.model_version).all()}
    assert versions == {model_version(SIG), model_version(NEW_SIG)}
    # следующий запуск — индекс уже текущей модели: без второй пересборки
    rt2 = mr.build_runtime(_settings(env), {"LITE_MODE": "1"}, f)
    assert rt2.ready and mr.INDEX_REBUILT_MARK not in rt2.component("matcher").detail and len(calls) == 1


def test_full_mode_does_not_rebuild_by_itself_and_says_which_command_does(env):
    before = _old_index(env)
    f, calls = _factories(env, NewEmbedder(NEW_SIG))
    rt = mr.build_runtime(_settings(env), {}, f)
    comp = rt.component("matcher")
    assert comp.status == mr.FAILED and not rt.ready and calls == []
    assert "IndexModelMismatchError" in comp.error and "scripts.rebuild_index --apply --compute-missing" in comp.error
    assert Path(env.path + ".npz").read_bytes() == before and not list(Path(env.path).parent.glob("backup-*"))


def test_a_failed_rebuild_leaves_the_index_untouched_and_explains_in_plain_words(env):
    before = _old_index(env)

    def broken(settings, emb):
        raise RuntimeError("Отказ: нет места на диске")
    f, _ = _factories(env, NewEmbedder(NEW_SIG), rebuild=broken)
    rt = mr.build_runtime(_settings(env), {"LITE_MODE": "1"}, f)
    comp = rt.component("matcher")
    assert comp.status == mr.FAILED and not rt.ready
    assert "построен другой моделью" in comp.error and "сам не получилось" in comp.error and "нет места на диске" in comp.error
    assert "Прежний индекс не тронут" in comp.error
    assert Path(env.path + ".npz").read_bytes() == before


def test_a_damaged_index_is_not_rebuilt_over_in_lite_mode(env):
    """Пересборка — только при другой модели. Повреждённый файл остаётся как есть (его восстанавливают из копии)."""
    _old_index(env)
    Path(env.path + ".npz").write_bytes(b"not a zip")
    f, calls = _factories(env, Embedder(SIG))
    rt = mr.build_runtime(_settings(env), {"LITE_MODE": "1"}, f)
    assert rt.component("matcher").status == mr.FAILED and calls == []
    assert Path(env.path + ".npz").read_bytes() == b"not a zip"


def test_other_dimension_is_also_a_model_mismatch(env):
    _old_index(env)
    with pytest.raises(IndexModelMismatchError) as exc:
        M.open_catalog_index(env.path, 4, None)
    assert isinstance(exc.value, IndexIntegrityError)               # прежние обработчики «индекс неисправен» его ловят
    with pytest.raises(IndexModelMismatchError):
        M.open_catalog_index(env.path, 3, NEW_SIG)
    assert np.asarray(M.open_catalog_index(env.path, 3, SIG)._ids).size == 3


def test_start_demo_tells_the_owner_the_index_was_rebuilt():
    from scripts import start_demo as sd
    status = {"ready": True, "components": [
        {"name": "embedder", "detail": "siglip2-base"},
        {"name": "matcher", "detail": f"индекс: x; {mr.INDEX_REBUILT_MARK} под текущую модель (Готово: векторов 11 "
                                      f"(из кэша 0, посчитано 11); Копия прежнего индекса: D:/x/backup-1)"}]}
    notes = sd._rebuilt_index_notes(status)
    assert len(notes) == 1 and "пересобран под текущую модель" in notes[0] and "«Узнать товар» работает" in notes[0]
    assert "backup-1" in notes[0]
    assert sd._rebuilt_index_notes({"components": [{"name": "matcher", "detail": "индекс: x"}]}) == []
    assert sd._rebuilt_index_notes(None) == []
