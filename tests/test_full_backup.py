"""Полная резервная копия (scripts/full_backup.py), этап 2А.

Две группы:
  * логика без базы (хранилище фото по хешам, проверка, индекс, ротация, блокировка, команды docker compose);
  * НАСТОЯЩИЕ pg_dump / pg_restore / psql против PostgreSQL 16 — только если задан FULL_BACKUP_TEST_DB_URL (адрес сервера,
    где можно создавать и удалять базы). Без него эти тесты пропускаются. Это облачная проверка, не Docker Desktop и не Windows.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from scripts import full_backup as fb
from tests.pg_cli import psql

PG_URL = os.environ.get("FULL_BACKUP_TEST_DB_URL")
needs_pg = pytest.mark.skipif(not PG_URL or shutil.which("pg_dump") is None,
                              reason="нет FULL_BACKUP_TEST_DB_URL или утилит PostgreSQL")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- хранилище фото

def test_photo_is_stored_once_by_content_hash(tmp_path):
    src = tmp_path / "a.jpg"
    src.write_bytes(b"photo-1")
    store = tmp_path / "store"
    sha, size, new = fb.store_photo(src, store)
    assert (sha, size, new) == (_sha(b"photo-1"), 7, True)
    assert fb.store_path(store, sha).read_bytes() == b"photo-1"
    copy = tmp_path / "b.jpg"
    copy.write_bytes(b"photo-1")
    assert fb.store_photo(copy, store) == (sha, 7, False)
    assert not list(store.glob(".tmp-*"))                     # временные файлы не остаются


def test_existing_store_file_of_another_size_is_reported_not_overwritten(tmp_path):
    store = tmp_path / "store"
    src = tmp_path / "a.jpg"
    src.write_bytes(b"photo-1")
    damaged = fb.store_path(store, _sha(b"photo-1"))
    damaged.parent.mkdir(parents=True)
    damaged.write_bytes(b"xx")
    with pytest.raises(fb.BackupError):
        fb.store_photo(src, store)
    assert damaged.read_bytes() == b"xx"


def test_new_store_file_is_reread_and_a_bad_write_is_an_error(tmp_path, monkeypatch):
    """Новое фото перечитывается после записи: если прочитанное не совпало с записанным (сбой диска), копия не считается годной."""
    src = tmp_path / "a.jpg"
    src.write_bytes(b"photo-1")
    monkeypatch.setattr(fb, "sha256_file", lambda path: "0" * 64)
    with pytest.raises(fb.BackupError, match="хеш не совпал"):
        fb.store_photo(src, tmp_path / "store")


def test_customer_frames_and_unfinished_files_are_never_copied(tmp_path):
    media = tmp_path / "data"
    (media / "catalog-uploads" / "SKU1").mkdir(parents=True)
    (media / "catalog-uploads" / "SKU1" / "p1.jpg").write_bytes(b"1")
    (media / "catalog-uploads" / "SKU1" / "p2.jpg.0a1b2c3d4e5f.part").write_bytes(b"half")
    (media / "catalog-uploads" / "identify-tmp").mkdir()
    (media / "catalog-uploads" / "identify-tmp" / "x.jpg").write_bytes(b"frame")
    (media / "identify-tmp").mkdir()
    (media / "identify-tmp" / ("a" * 32 + ".jpg")).write_bytes(b"frame")
    rels = [rel for _p, rel in fb.iter_photo_files(media)]
    assert rels == ["catalog-uploads/SKU1/p1.jpg"]


def test_listing_tables_reads_table_entries_only():
    listing = """;
; Archive created at 2026-11-07 21:00:00 +05
;
215; 1259 16390 TABLE public products pgtest
216; 1259 16400 TABLE public product_barcodes pgtest
3400; 0 16390 TABLE DATA public products pgtest
3401; 2606 16410 CONSTRAINT public products products_pkey pgtest
"""
    assert fb.listing_tables(listing) == ["public.product_barcodes", "public.products"]


# --------------------------------------------------------------------------- индекс

def _index(tmp_path, *, sidecar_sha="match", fmt=2):
    idx = tmp_path / "models" / "index" / "catalog.faiss"
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.write_bytes(b"faiss-bytes")
    if sidecar_sha is not None:
        meta = {"format": fmt, "count": 3, "generation": "g1",
                "index_sha256": _sha(b"faiss-bytes") if sidecar_sha == "match" else sidecar_sha}
        Path(f"{idx}.ids.json").write_text(json.dumps(meta), encoding="utf-8")
    return idx


def test_consistent_index_is_marked_consistent(tmp_path):
    info = fb.copy_index(_index(tmp_path), tmp_path / "out")
    assert info["status"] == "consistent" and info["count"] == 3
    assert (tmp_path / "out" / "catalog.faiss").read_bytes() == b"faiss-bytes"


def test_index_rewritten_during_copy_is_marked_inconsistent_but_backup_still_proceeds(tmp_path):
    info = fb.copy_index(_index(tmp_path, sidecar_sha="0" * 64), tmp_path / "out", attempts=2)
    assert info["status"] == "inconsistent" and "пересобрать" in info["reason"]


def test_missing_or_legacy_index_states(tmp_path):
    assert fb.copy_index(tmp_path / "none.faiss", tmp_path / "out")["status"] == "absent"
    assert fb.copy_index(_index(tmp_path, sidecar_sha=None), tmp_path / "out2")["status"] == "unverified"


# --------------------------------------------------------------------------- создание с поддельной базой

class FakePg(fb.PgTools):
    def __init__(self, fail_dump=False):
        super().__init__(use_compose=False, database_url="postgresql://x@h/db")
        self.fail_dump = fail_dump

    def dump(self, out):
        if self.fail_dump:
            raise fb.BackupError("pg_dump завершился с кодом 1")
        Path(out).write_bytes(b"PGDMP-fake")

    def listing(self, dump):
        return ";\n; Archive created at now\n215; 1259 1 TABLE public products u\n"

    def schema_revision(self):
        return "0002"


def _project(tmp_path):
    project = tmp_path / "proj"
    (project / "configs").mkdir(parents=True)
    (project / "configs" / "config.yaml").write_text("models: {}\n", encoding="utf-8")
    (project / "configs" / "agent.yaml").write_text("password: secret\n", encoding="utf-8")
    (project / ".env").write_text("POSTGRES_PASSWORD=secret\n", encoding="utf-8")
    (project / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    media = project / "data"
    (media / "catalog-uploads" / "SKU1").mkdir(parents=True)
    (media / "catalog-uploads" / "SKU1" / "p1.jpg").write_bytes(b"p1")
    (media / "catalog-uploads" / "SKU2").mkdir()
    (media / "catalog-uploads" / "SKU2" / "p1.jpg").write_bytes(b"p1")        # то же содержимое — хранится один раз
    (media / "identify-tmp").mkdir()
    (media / "identify-tmp" / ("f" * 32 + ".jpg")).write_bytes(b"customer-frame")
    _index(project)
    return fb.Layout(project=project, media_root=media, index_path=project / "models/index/catalog.faiss",
                     dest=tmp_path / "backups")


def _clock(*stamps):
    it = iter(stamps)
    return lambda: next(it)


def test_full_backup_layout_manifest_and_exclusions(tmp_path):
    layout = _project(tmp_path)
    final = fb.create_backup(layout, FakePg(), now=_clock(datetime(2026, 11, 7, 21, 30, 0)), out=lambda s: None)
    assert final.name == "Backup-2026-11-07_213000" and final.is_dir()
    manifest = json.loads((final / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["verification"]["ok"] is True and manifest["database"]["tables"] == ["public.products"]
    assert manifest["photos"]["count"] == 2 and manifest["photos"]["new_in_store"] == 1
    assert manifest["index"]["status"] == "consistent"
    assert manifest["schema_revision"] == "0002"                 # ревизия Alembic рабочей базы (шаг Р-2)
    assert sorted(manifest["config"]) == ["configs/config.yaml", "docker-compose.yml"]
    every_file = b"".join(p.read_bytes() for p in layout.dest.rglob("*") if p.is_file())
    assert b"customer-frame" not in every_file and b"secret" not in every_file          # ни кадров, ни секретов
    assert not (layout.dest / fb.LOCK_NAME).exists()
    assert fb.verify_backup(final, layout.store) == []


def test_second_backup_adds_only_new_photos(tmp_path):
    layout = _project(tmp_path)
    fb.create_backup(layout, FakePg(), now=_clock(datetime(2026, 11, 7, 21, 0)), out=lambda s: None)
    (layout.media_root / "catalog-uploads" / "SKU3").mkdir()
    (layout.media_root / "catalog-uploads" / "SKU3" / "p1.jpg").write_bytes(b"p3")
    second = fb.create_backup(layout, FakePg(), now=_clock(datetime(2026, 11, 8, 21, 0)), out=lambda s: None)
    manifest = fb.load_manifest(second)
    assert manifest["photos"]["count"] == 3 and manifest["photos"]["new_in_store"] == 1
    assert len([p for p in layout.store.glob("??/*")]) == 2


def test_failed_dump_leaves_no_finished_backup_and_releases_the_lock(tmp_path):
    layout = _project(tmp_path)
    with pytest.raises(fb.BackupError):
        fb.create_backup(layout, FakePg(fail_dump=True), now=_clock(datetime(2026, 11, 7, 21, 0)), out=lambda s: None)
    assert not list(layout.dest.glob("Backup-*[0-9]"))                     # готовой копии нет
    assert list(layout.dest.glob("Backup-*.part"))                         # незавершённая оставлена для разбора
    assert not (layout.dest / fb.LOCK_NAME).exists()


def test_concurrent_operation_is_refused_by_the_lock(tmp_path):
    layout = _project(tmp_path)
    layout.dest.mkdir(parents=True)
    (layout.dest / fb.LOCK_NAME).write_text("{}", encoding="utf-8")
    with pytest.raises(fb.BackupError, match="занято"):
        fb.create_backup(layout, FakePg(), out=lambda s: None)


def test_same_second_backups_get_distinct_names(tmp_path):
    layout = _project(tmp_path)
    t = datetime(2026, 11, 7, 21, 0)
    a = fb.create_backup(layout, FakePg(), now=_clock(t), out=lambda s: None)
    b = fb.create_backup(layout, FakePg(), now=_clock(t), out=lambda s: None)
    assert a != b and b.name.endswith("-2")


# --------------------------------------------------------------------------- проверка копии

def test_verify_detects_tampering_missing_and_corrupted_store_photos(tmp_path):
    layout = _project(tmp_path)
    final = fb.create_backup(layout, FakePg(), now=_clock(datetime(2026, 11, 7, 21, 0)), out=lambda s: None)
    (final / "db.dump").write_bytes(b"tampered")
    assert any("db.dump" in p for p in fb.verify_backup(final, layout.store))

    final2 = fb.create_backup(layout, FakePg(), now=_clock(datetime(2026, 11, 7, 22, 0)), out=lambda s: None)
    sha = fb.read_photo_list(final2 / "photos.list")[0][0]
    stored = fb.store_path(layout.store, sha)
    stored.write_bytes(b"P1")                                               # тот же размер, другое содержимое
    assert fb.verify_backup(final2, layout.store) == []                     # быстрая проверка — по размеру
    assert any("повреждено" in p for p in fb.verify_backup(final2, layout.store, full_store=True))
    stored.unlink()
    assert any("нет фото" in p for p in fb.verify_backup(final2, layout.store))


# --------------------------------------------------------------------------- ротация

def _infos(days, verified=lambda i: True):
    start = datetime(2026, 1, 1, 22, 0)
    return [fb.BackupInfo(f"B{i:03d}", start + timedelta(days=i), verified(i)) for i in range(days)]


def test_retention_keeps_dailies_weeklies_monthlies_and_newest():
    backups = _infos(400)
    kept = fb.choose_kept(backups)
    assert "B399" in kept and len(kept) <= fb.KEEP_DAILY + fb.KEEP_WEEKLY + fb.KEEP_MONTHLY
    assert {f"B{i}" for i in range(393, 400)} <= kept                      # 7 последних дней


def test_last_verified_backup_is_always_kept_even_if_old():
    backups = _infos(100, verified=lambda i: i == 3)
    assert "B003" in fb.choose_kept(backups)


def _write_backup(dest: Path, name: str, created: datetime, shas: list[str], ok=True):
    d = dest / name
    d.mkdir(parents=True)
    (d / "photos.list").write_text("".join(f"{s}\t1\tcatalog-uploads/x/{s[:6]}.jpg\n" for s in shas), encoding="utf-8")
    (d / "manifest.json").write_text(json.dumps({"format": 1, "created_at": created.isoformat(),
                                                 "verification": {"ok": ok}}), encoding="utf-8")


def _store_file(dest: Path, data: bytes) -> str:
    sha = _sha(data)
    p = fb.store_path(dest / fb.STORE_DIR, sha)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return sha


def test_prune_plan_removes_photos_only_when_no_kept_backup_references_them(tmp_path):
    dest = tmp_path / "b"
    shared, old_only = _store_file(dest, b"shared"), _store_file(dest, b"old-only")
    start = datetime(2025, 1, 1, 22, 0)
    _write_backup(dest, "Backup-old", start, [shared, old_only])
    for i in range(1, 400, 1):                                              # много свежих копий: старая станет лишней
        _write_backup(dest, f"Backup-{i:03d}", start + timedelta(days=i), [shared])
    plan = fb.plan_prune(dest)
    assert "Backup-old" in plan.delete and old_only in plan.orphan_photos and shared not in plan.orphan_photos


def test_prune_never_touches_photos_of_unfinished_backups(tmp_path):
    dest = tmp_path / "b"
    sha = _store_file(dest, b"in-progress")
    part = dest / "Backup-2026-11-07_210000.part"
    part.mkdir(parents=True)
    (part / "photos.list").write_text(f"{sha}\t1\tcatalog-uploads/x.jpg\n", encoding="utf-8")
    plan = fb.plan_prune(dest)
    assert plan.orphan_photos == [] and plan.incomplete == [part.name]


def test_prune_keeps_all_photos_when_a_damaged_backup_has_no_photo_list(tmp_path):
    dest = tmp_path / "b"
    _store_file(dest, b"unknown-owner")
    broken = dest / "Backup-2026-11-01_210000"
    broken.mkdir(parents=True)
    (broken / "db.dump").write_bytes(b"x")                                  # манифеста нет — повреждённая копия
    assert fb.plan_prune(dest).orphan_photos == []


def test_prune_without_apply_deletes_nothing_and_apply_deletes_only_what_was_shown(tmp_path):
    dest = tmp_path / "b"
    orphan = _store_file(dest, b"orphan")
    keep = _store_file(dest, b"keep")
    _write_backup(dest, "Backup-2026-11-07_210000", datetime(2026, 11, 7, 21), [keep])
    lines = []
    assert fb.main(["prune", "--dest", str(dest)], out=lines.append) == 0
    assert fb.store_path(dest / fb.STORE_DIR, orphan).exists() and any("только план" in s for s in lines)
    plan = fb.plan_prune(dest)
    late = _store_file(dest, b"appeared-after-plan")
    fb.apply_prune(dest, plan, out=lambda s: None)
    assert not fb.store_path(dest / fb.STORE_DIR, orphan).exists()
    assert fb.store_path(dest / fb.STORE_DIR, keep).exists()
    assert fb.store_path(dest / fb.STORE_DIR, late).exists()               # не было в показанном плане — не удалено


# --------------------------------------------------------------------------- команды для docker compose

class Recorder:
    def __init__(self, rc=0, stdout=b""):
        self.calls, self.rc, self.stdout = [], rc, stdout

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, self.rc, stdout=self.stdout, stderr=b"")


def test_compose_mode_runs_tools_inside_the_db_container_and_passes_sql_on_stdin():
    rec = Recorder(stdout=b"1\n")
    pg = fb.PgTools(compose_file="dc.yml", runner=rec)
    pg.create_db("restore_check_20261107_abcdef")
    cmd, kw = rec.calls[-1]
    assert cmd[:6] == ["docker", "compose", "-f", "dc.yml", "exec", "-T"] and "createdb" in cmd[-1]
    pg._psql("restore_check_20261107_abcdef", "SELECT 'x'")
    cmd, kw = rec.calls[-1]
    assert kw["input"] == b"SELECT 'x'" and "SELECT" not in " ".join(cmd)


def test_only_temporary_databases_with_the_reserved_prefix_can_be_dropped():
    pg = fb.PgTools(use_compose=True, runner=Recorder())
    for bad in ("shelf_audit", "postgres", "restore_check_x; drop", "restore_check_"):
        with pytest.raises(fb.BackupError):
            pg.drop_db(bad)


class _VersionRunner:
    """Поддельный запуск: «pg_dump --version» и «SHOW server_version_num» через psql."""

    def __init__(self, client="pg_dump (PostgreSQL) 17.12 (Debian 17.12-0+deb13u1)\n", server="160013\n", rc=0):
        self.client, self.server, self.rc, self.calls = client, server, rc, []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        out = self.client if cmd[-1] == "--version" else self.server
        return subprocess.CompletedProcess(cmd, self.rc, out.encode("utf-8"), b"")


def test_tools_newer_than_the_server_are_refused_with_a_clear_reason():
    run = _VersionRunner()
    pg = fb.PgTools(use_compose=False, database_url="postgresql://u@h/db", runner=run)
    with pytest.raises(fb.BackupError, match="версии 17, а сервер базы — версии 16.*postgresql-client-16"):
        pg.check_versions("pg_dump")
    assert run.calls[0] == ["pg_dump", "--version"] and run.calls[1][-1] == "postgresql://u@h/db"


def test_matching_or_unreadable_versions_do_not_block_and_are_checked_once():
    run = _VersionRunner(client="pg_restore (PostgreSQL) 16.4\n")
    pg = fb.PgTools(use_compose=False, database_url="postgresql://u@h/db", runner=run)
    pg.check_versions("pg_restore")
    pg.check_versions("pg_restore")
    assert len(run.calls) == 2                                       # второй раз не спрашивает
    fb.PgTools(use_compose=False, database_url="postgresql://u@h/db", runner=_VersionRunner(rc=1)).check_versions()
    fb.PgTools(use_compose=False, database_url="postgresql://u@h/db", runner=_VersionRunner(server="?")).check_versions()
    docker_mode = _VersionRunner()
    fb.PgTools(use_compose=True, runner=docker_mode).check_versions()    # в Docker утилиты из контейнера базы
    assert docker_mode.calls == []


def test_url_for_keeps_server_and_query_and_changes_only_the_database():
    pg = fb.PgTools(use_compose=False, database_url="postgresql://u@h:5432/main?sslmode=disable")
    assert pg._url_for("restore_check_x1y2z3") == "postgresql://u@h:5432/restore_check_x1y2z3?sslmode=disable"


# --------------------------------------------------------------------------- настоящий PostgreSQL 16 (облако)

def _psql(url, sql):
    return psql("-v", "ON_ERROR_STOP=1", "-At", "-d", url, "-c", sql)     # с тайм-аутами (tests/pg_cli.py)


@pytest.fixture()
def live_db():
    name = f"fbtest_{os.getpid()}_{datetime.now():%H%M%S%f}"
    admin = fb.PgTools(use_compose=False, database_url=PG_URL)._url_for("postgres")
    _psql(admin, f"CREATE DATABASE {name}")
    url = fb.PgTools(use_compose=False, database_url=PG_URL)._url_for(name)
    _psql(url, "CREATE TABLE products (id text PRIMARY KEY, name text NOT NULL, expected_price double precision);"
               "CREATE TABLE product_barcodes (code text PRIMARY KEY, product_id text REFERENCES products(id));"
               "INSERT INTO products VALUES ('p1','Кола 0,5 л',9900.1),('p2','Чай',NULL);"
               "INSERT INTO product_barcodes VALUES ('4600000000001','p1');")
    try:
        yield url
    finally:
        _psql(admin, f"DROP DATABASE IF EXISTS {name}")


def _restore_dbs(url):
    admin = fb.PgTools(use_compose=False, database_url=url)._url_for("postgres")
    return [n for n in _psql(admin, "SELECT datname FROM pg_database").split() if n.startswith("restore_check_")]


@needs_pg
def test_real_backup_and_restore_check_with_postgresql(tmp_path, live_db):
    layout = _project(tmp_path)
    pg = fb.PgTools(use_compose=False, database_url=live_db)
    final = fb.create_backup(layout, pg, out=lambda s: None)
    manifest = fb.load_manifest(final)
    assert manifest["database"]["tables"] == ["public.product_barcodes", "public.products"]
    result = fb.restore_check(final, pg, out=lambda s: None)
    assert result["ok"] is True
    assert result["row_counts"] == {"public.product_barcodes": 1, "public.products": 2}
    assert json.loads((final / "restore_check.json").read_text(encoding="utf-8"))["ok"] is True
    assert _restore_dbs(live_db) == []                                     # временная база удалена
    assert fb.verify_backup(final, layout.store, full_store=True) == []


@needs_pg
def test_real_restore_check_of_a_corrupted_dump_fails_and_still_drops_the_temporary_database(tmp_path, live_db):
    layout = _project(tmp_path)
    pg = fb.PgTools(use_compose=False, database_url=live_db)
    final = fb.create_backup(layout, pg, out=lambda s: None)
    data = (final / "db.dump").read_bytes()
    (final / "db.dump").write_bytes(data[: len(data) // 2])                 # обрезанный дамп
    assert fb.verify_backup(final, layout.store)                            # проверка по хешам уже видит порчу
    result = fb.restore_check(final, pg, out=lambda s: None)
    assert result["ok"] is False and _restore_dbs(live_db) == []


@needs_pg
def test_real_cli_create_verify_and_prune_plan(tmp_path, live_db):
    layout = _project(tmp_path)
    lines = []
    args = ["--dest", str(layout.dest)]
    assert fb.main(["create", *args, "--database-url", live_db, "--project", str(layout.project), "--restore-check"],
                   out=lines.append) == 0
    name = next(p.name for p in layout.dest.glob("Backup-*"))
    assert fb.main(["verify", *args, name, "--full-store"], out=lines.append) == 0
    assert fb.main(["prune", *args], out=lines.append) == 0
    assert any("Восстановление проверено" in s for s in lines) and any("Копия цела" in s for s in lines)


def test_final_rename_is_retried_when_the_folder_is_briefly_locked(tmp_path, monkeypatch):
    calls = {"n": 0}
    real = os.replace

    def flaky(src, dst):
        if str(src).endswith(".part"):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError("занято антивирусом")
        return real(src, dst)

    monkeypatch.setattr(fb.os, "replace", flaky)
    monkeypatch.setattr("time.sleep", lambda s: None)
    layout = _project(tmp_path)
    final = fb.create_backup(layout, FakePg(), now=_clock(datetime(2026, 11, 7, 21, 0)), out=lambda s: None)
    assert final.is_dir() and calls["n"] == 3


def test_final_rename_gives_up_with_a_clear_error_and_keeps_the_part_folder(tmp_path, monkeypatch):
    real = os.replace
    monkeypatch.setattr(fb.os, "replace",
                        lambda s, d: (_ for _ in ()).throw(PermissionError("занято")) if str(s).endswith(".part") else real(s, d))
    monkeypatch.setattr("time.sleep", lambda s: None)
    layout = _project(tmp_path)
    with pytest.raises(fb.BackupError, match="занят"):
        fb.create_backup(layout, FakePg(), now=_clock(datetime(2026, 11, 7, 21, 0)), out=lambda s: None)
    assert list(layout.dest.glob("Backup-*.part")) and not (layout.dest / fb.LOCK_NAME).exists()


# --------------------------------------------------------------------------- восстановление в новую базу и новую папку

def test_materialize_rebuilds_the_photo_tree_index_and_config_with_verified_hashes(tmp_path):
    layout = _project(tmp_path)
    final = fb.create_backup(layout, FakePg(), now=_clock(datetime(2026, 11, 7, 21, 0)), out=lambda s: None)
    out_dir = tmp_path / "restored"
    info = fb.materialize_files(final, layout.store, out_dir, out=lambda s: None)
    assert info["photos"] == 2
    assert (out_dir / "data/catalog-uploads/SKU1/p1.jpg").read_bytes() == b"p1"
    assert (out_dir / "data/catalog-uploads/SKU2/p1.jpg").read_bytes() == b"p1"
    assert (out_dir / "models/index/catalog.faiss").read_bytes() == b"faiss-bytes"
    assert (out_dir / "config/configs/config.yaml").is_file()
    assert not (out_dir / "data" / "identify-tmp").exists()


def test_materialize_refuses_a_non_empty_folder_and_overwrites_nothing(tmp_path):
    layout = _project(tmp_path)
    final = fb.create_backup(layout, FakePg(), now=_clock(datetime(2026, 11, 7, 21, 0)), out=lambda s: None)
    out_dir = tmp_path / "restored"
    out_dir.mkdir()
    (out_dir / "keep.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(fb.BackupError, match="не пуста"):
        fb.materialize_files(final, layout.store, out_dir, out=lambda s: None)
    assert [p.name for p in out_dir.iterdir()] == ["keep.txt"]


@pytest.mark.parametrize("bad", ["../escape.jpg", "/etc/passwd", "catalog-uploads/../../x.jpg", "C:/x.jpg", "a\\..\\b.jpg"])
def test_paths_from_a_tampered_photo_list_cannot_escape_the_target_folder(bad):
    with pytest.raises(fb.BackupError):
        fb._safe_relpath(bad)


def test_materialize_refuses_a_backup_that_fails_verification(tmp_path):
    layout = _project(tmp_path)
    final = fb.create_backup(layout, FakePg(), now=_clock(datetime(2026, 11, 7, 21, 0)), out=lambda s: None)
    (final / "db.dump").write_bytes(b"tampered")
    with pytest.raises(fb.BackupError, match="не прошла проверку"):
        fb.materialize_files(final, layout.store, tmp_path / "restored", out=lambda s: None)
    assert not (tmp_path / "restored").exists()


def test_restore_never_touches_service_databases():
    for bad in ("postgres", "template1", "Shelf", "a-b", "x" * 64):
        with pytest.raises(fb.BackupError):
            fb._check_target_db_name(bad)


@needs_pg
def test_real_restore_into_a_new_database_and_folder(tmp_path, live_db):
    layout = _project(tmp_path)
    pg = fb.PgTools(use_compose=False, database_url=live_db)
    final = fb.create_backup(layout, pg, out=lambda s: None)
    target = f"restored_{os.getpid()}_{datetime.now():%H%M%S%f}"
    try:
        report = fb.restore_to_new(final, pg, target_db=target, out_dir=tmp_path / "restored", out=lambda s: None)
        assert report["ok"] is True
        assert report["row_counts"] == {"public.product_barcodes": 1, "public.products": 2}
        url = pg._url_for(target)
        assert _psql(url, "SELECT name, expected_price FROM products ORDER BY id").split("\n")[0] == "Кола 0,5 л|9900.1"
        assert (tmp_path / "restored" / "restore_report.json").is_file()
        with pytest.raises(fb.BackupError, match="уже существует"):                 # повторно в ту же базу — отказ
            fb.restore_to_new(final, pg, target_db=target, out_dir=tmp_path / "restored2", out=lambda s: None)
        assert not (tmp_path / "restored2").exists()
    finally:
        _psql(pg._url_for("postgres"), f"DROP DATABASE IF EXISTS {target}")


@needs_pg
def test_real_restore_refuses_the_live_database_name(tmp_path, live_db):
    layout = _project(tmp_path)
    pg = fb.PgTools(use_compose=False, database_url=live_db)
    final = fb.create_backup(layout, pg, out=lambda s: None)
    live_name = live_db.rsplit("/", 1)[1]
    with pytest.raises(fb.BackupError, match="уже существует"):
        fb.restore_to_new(final, pg, target_db=live_name, out_dir=tmp_path / "restored", out=lambda s: None)
    assert _psql(live_db, "SELECT count(*) FROM products").strip() == "2"            # рабочая база не тронута


# --------------------------------------------------------------------------- сверка с документом 09 (2026-11-08)

def test_same_disk_is_warned_in_output_and_manifest_but_not_refused(tmp_path):
    layout = _project(tmp_path)
    lines = []
    final = fb.create_backup(layout, FakePg(), out=lines.append)
    note = fb.load_manifest(final)["warnings"]
    assert any("том же диске" in w for w in note) and any("том же диске" in line for line in lines)


def test_different_disks_give_no_warning(tmp_path, monkeypatch):
    real_stat = os.stat
    dest = tmp_path / "dest"
    dest.mkdir()

    class Fake:
        def __init__(self, dev):
            self.st_dev = dev

    def fake_stat(p, *a, **k):
        result = real_stat(p, *a, **k)                                # несуществующий путь — та же ошибка, что и без подмены
        return Fake(2) if str(p) == str(dest) else Fake(1) if result else result

    monkeypatch.setattr(fb.os, "stat", fake_stat)
    assert fb.same_disk_warning(dest, tmp_path) is None
    assert fb.same_disk_warning(tmp_path / "missing-dest", tmp_path) is None              # нет места копий — нечего сравнивать


def test_full_disk_gives_an_understandable_error_and_releases_the_lock(tmp_path, monkeypatch):
    import errno

    layout = _project(tmp_path)

    def no_space(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(fb, "store_photo", no_space)
    with pytest.raises(fb.BackupError) as err:
        fb.create_backup(layout, FakePg(), out=lambda s: None)
    assert "нет места на диске" in str(err.value) and "ГБ" in str(err.value)
    assert not (layout.dest / fb.LOCK_NAME).exists()
    assert not [p for p in layout.dest.iterdir() if p.name.startswith(fb.BACKUP_PREFIX) and not p.name.endswith(fb.PART_SUFFIX)]


def test_other_os_errors_are_not_disguised_as_a_full_disk(tmp_path, monkeypatch):
    layout = _project(tmp_path)
    monkeypatch.setattr(fb, "store_photo", lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "denied")))
    with pytest.raises(PermissionError):
        fb.create_backup(layout, FakePg(), out=lambda s: None)


@needs_pg
def test_real_restore_check_runs_control_queries_admin_products_and_orphan_photos(tmp_path, live_db):
    _psql(live_db, "ALTER TABLE products ADD COLUMN sku text;"
                   "UPDATE products SET sku = 'SKU1' WHERE id = 'p1'; UPDATE products SET sku = 'X2' WHERE id = 'p2';"
                   "CREATE TABLE users (id text PRIMARY KEY, username text, role text, is_active boolean);"
                   "INSERT INTO users VALUES ('u1','boss','admin',false),('u2','m','merchandiser',true);")
    layout = _project(tmp_path)                                     # фото: SKU1 (товар есть) и SKU2 (товара нет)
    pg = fb.PgTools(use_compose=False, database_url=live_db)
    final = fb.create_backup(layout, pg, out=lambda s: None)
    lines = []
    result = fb.restore_check(final, pg, out=lines.append)
    assert result["ok"] is True                                     # предупреждения не делают копию негодной
    assert result["checks"] == {"products": 2, "active_admins": 0, "photo_folders": 2, "photo_folders_without_product": 1}
    assert any("нет действующего администратора" in w for w in result["warnings"])
    assert any("SKU2" in w for w in result["warnings"]) and any("внимание" in line for line in lines)
    assert _restore_dbs(live_db) == []
    _psql(live_db, "UPDATE users SET is_active = true WHERE id = 'u1'; UPDATE products SET sku = 'SKU2' WHERE id = 'p2';")
    final2 = fb.create_backup(layout, pg, out=lambda s: None)
    result2 = fb.restore_check(final2, pg, out=lambda s: None)
    assert result2["checks"]["active_admins"] == 1 and result2["warnings"] == []
