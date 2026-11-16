"""Резервная копия магазина в облегчённом режиме (2026-11-16): копия на компьютер и в облако, проверка, уборка, восстановление."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services import lite_backup as B

KEYS = {"AUTH_SECRET_KEY": "auth-key-1", "CAMERA_SECRET_KEY": "cam-key-1"}
T0 = datetime(2026, 11, 16, 3, 0, 0, tzinfo=timezone.utc)


def make_shop(media: Path, products: int = 3) -> Path:
    media.mkdir(parents=True)
    db = media / "shop.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE products (id TEXT PRIMARY KEY, name TEXT)")
    conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT)")
    conn.executemany("INSERT INTO products VALUES (?, ?)", [(f"p{i}", f"Товар {i}") for i in range(products)])
    conn.execute("INSERT INTO users VALUES ('u1', 'owner')")
    conn.commit()
    conn.close()
    for i in range(products):
        d = media / "catalog-uploads" / f"P{i}"
        d.mkdir(parents=True)
        (d / "1.jpg").write_bytes(b"photo-%d" % i * 100)
    (media / "index").mkdir()
    (media / "index" / "catalog.faiss").write_bytes(b"faiss" * 50)
    (media / "index" / "catalog.faiss.ids.json").write_text("[]", encoding="utf-8")
    (media / "index" / "catalog.faiss.lock").write_text("", encoding="utf-8")
    return db


def dests(tmp: Path, cloud: bool = True) -> list[B.Destination]:
    out = [B.Destination("local", tmp / "local" / B.FOLDER_NAME, B.KEEP_LOCAL)]
    if cloud:
        out.append(B.Destination("cloud", tmp / "gdrive" / B.FOLDER_NAME, B.KEEP_CLOUD))
    return out


def backup(tmp: Path, media: Path, db: Path, now=T0, ds=None) -> dict:
    return B.create_backup(root=tmp, media_root=media, db_file=db, keys=KEYS, dests=ds or dests(tmp), now=now)


def test_backup_goes_to_the_computer_and_the_cloud_and_checks_out(tmp_path):
    media = tmp_path / "lite"
    db = make_shop(media)
    report = backup(tmp_path, media, db)
    assert report["ok"] and [d["kind"] for d in report["destinations"]] == ["local", "cloud"]
    assert report["counts"] == {"products": 3, "users": 1, "photos": 3}
    for d in dests(tmp_path):
        copies = B.list_copies(d.root)
        assert len(copies) == 1 and copies[0].counts["products"] == 3
        assert B.verify_copy(copies[0].path, full=True) == []
        names = {p.name for p in (copies[0].path / "index").iterdir()}
        assert names == {"catalog.faiss", "catalog.faiss.ids.json"}, "файл блокировки в копию не входит"
        assert (copies[0].path / "keys.env").read_text(encoding="utf-8") == "AUTH_SECRET_KEY=auth-key-1\nCAMERA_SECRET_KEY=cam-key-1\n"
        assert not [p for p in d.root.iterdir() if p.name.startswith(".work-") or p.name.endswith(".part")]
    assert not (tmp_path / "local" / B.FOLDER_NAME / (".work-" + B.copy_name(T0))).exists()


def test_photos_are_stored_once_and_only_new_ones_are_added(tmp_path):
    media = tmp_path / "lite"
    db = make_shop(media)
    backup(tmp_path, media, db)
    store = tmp_path / "gdrive" / B.FOLDER_NAME / "photos"
    first = sorted(p for p in store.rglob("*") if p.is_file())
    mtimes = {p: p.stat().st_mtime_ns for p in first}
    (media / "catalog-uploads" / "P9").mkdir()
    (media / "catalog-uploads" / "P9" / "1.jpg").write_bytes(b"new photo")
    backup(tmp_path, media, db, now=T0 + timedelta(days=1))
    second = sorted(p for p in store.rglob("*") if p.is_file())
    assert len(second) == len(first) + 1
    assert all(p.stat().st_mtime_ns == mtimes[p] for p in first), "прежние фото не переписываются (в облако не уходят снова)"


def test_only_the_newest_copies_are_kept_and_orphan_photos_go(tmp_path):
    media = tmp_path / "lite"
    db = make_shop(media)
    d = [B.Destination("local", tmp_path / "local" / B.FOLDER_NAME, 2)]
    backup(tmp_path, media, db, ds=d)
    (media / "catalog-uploads" / "P0" / "1.jpg").unlink()          # фото удалили из каталога
    backup(tmp_path, media, db, now=T0 + timedelta(days=1), ds=d)
    backup(tmp_path, media, db, now=T0 + timedelta(days=2), ds=d)
    copies = B.list_copies(d[0].root)
    assert [c.name for c in copies] == [B.copy_name(T0 + timedelta(days=2)), B.copy_name(T0 + timedelta(days=1))]
    store_files = [p for p in (d[0].root / "photos").rglob("*") if p.is_file()]
    assert len(store_files) == 2, "фото, которого нет ни в одной оставшейся копии, удалено"
    other = d[0].root / "мои файлы"
    other.mkdir()
    B.prune(d[0])
    assert other.exists(), "чужие папки не трогаются"


def test_cloud_failure_does_not_lose_the_local_copy(tmp_path):
    media = tmp_path / "lite"
    db = make_shop(media)
    blocker = tmp_path / "gdrive"
    blocker.write_text("это файл, а не папка", encoding="utf-8")
    report = backup(tmp_path, media, db)
    local, cloud = report["destinations"]
    assert local["ok"] and not cloud["ok"] and cloud["error"]
    assert report["ok"] is False
    assert len(B.list_copies(tmp_path / "local" / B.FOLDER_NAME)) == 1


def test_damaged_copy_is_found(tmp_path):
    media = tmp_path / "lite"
    db = make_shop(media)
    backup(tmp_path, media, db)
    copy_dir = B.list_copies(tmp_path / "local" / B.FOLDER_NAME)[0].path
    photo = next(p for p in (copy_dir.parent / "photos").rglob("*") if p.is_file())
    photo.write_bytes(b"x" * photo.stat().st_size)
    assert B.verify_copy(copy_dir) == []                      # быстрая проверка: размер на месте
    assert any("повреждено" in p for p in B.verify_copy(copy_dir, full=True))
    (copy_dir / "shop.db.gz").write_bytes(b"broken")
    assert "файл shop.db.gz повреждён" in B.verify_copy(copy_dir)


def test_restore_brings_everything_back_and_keeps_the_old_data(tmp_path):
    media = tmp_path / "lite"
    db = make_shop(media)
    backup(tmp_path, media, db)
    copy_dir = B.list_copies(tmp_path / "gdrive" / B.FOLDER_NAME)[0].path
    # «сломался диск»: данные пропали, на новом компьютере другие ключи
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM products")
    conn.commit()
    conn.close()
    env = tmp_path / ".env"
    env.write_text("LITE_MODE=1\nAUTH_SECRET_KEY=other\n", encoding="utf-8")
    result = B.restore_copy(copy_dir, media_root=media, env_file=env, now=T0)
    assert result["counts"] == {"products": 3, "users": 1, "photos": 3}
    assert sorted(result["keys_changed"]) == ["AUTH_SECRET_KEY", "CAMERA_SECRET_KEY"]
    assert env.read_text(encoding="utf-8") == "LITE_MODE=1\nAUTH_SECRET_KEY=auth-key-1\nCAMERA_SECRET_KEY=cam-key-1\n"
    assert list(tmp_path.glob(".env.bak-*")), "прежний .env сохранён"
    conn = sqlite3.connect(media / "shop.db")
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 3
    conn.close()
    assert (media / "catalog-uploads" / "P2" / "1.jpg").read_bytes() == b"photo-2" * 100
    assert (media / "index" / "catalog.faiss").is_file()
    old = Path(result["old_data"])
    assert old.name.startswith("lite.bak-") and (old / "shop.db").is_file(), "прежние данные не удалены"
    assert not (tmp_path / "lite.restore-part").exists()


def test_restore_refuses_a_damaged_copy_and_changes_nothing(tmp_path):
    media = tmp_path / "lite"
    db = make_shop(media)
    backup(tmp_path, media, db)
    copy_dir = B.list_copies(tmp_path / "local" / B.FOLDER_NAME)[0].path
    next(p for p in (copy_dir.parent / "photos").rglob("*") if p.is_file()).unlink()
    with pytest.raises(B.BackupError, match="Выберите другую копию"):
        B.restore_copy(copy_dir, media_root=media, env_file=None)
    assert not list(tmp_path.glob("lite.bak-*")) and (media / "shop.db").is_file()


def test_backup_of_a_running_database_with_a_write_in_progress(tmp_path):
    media = tmp_path / "lite"
    db = make_shop(media)
    writer = sqlite3.connect(db)
    writer.execute("INSERT INTO products VALUES ('p-new', 'ещё не сохранён')")      # транзакция открыта
    report = backup(tmp_path, media, db)
    writer.commit()
    writer.close()
    assert report["ok"] and report["counts"]["products"] == 3, "в копии — согласованное состояние базы"


def test_state_file_survives_reads_and_writes_from_several_threads(tmp_path):
    """Windows: замена файла, который в этот миг читают, давала PermissionError (1 раз из 10 в test_gdrive_backup)."""
    import threading
    stop = threading.Event()
    errors = []

    def reader():
        while not stop.is_set():
            B.read_state(tmp_path)

    def writer(n):
        try:
            for i in range(150):
                B.write_state(tmp_path, {"n": n, "i": i})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    readers = [threading.Thread(target=reader) for _ in range(2)]
    writers = [threading.Thread(target=writer, args=(n,)) for n in range(2)]
    for t in readers + writers:
        t.start()
    for t in writers:
        t.join()
    stop.set()
    for t in readers:
        t.join()
    assert not errors
    assert B.read_state(tmp_path)["i"] == 149
    assert not list(tmp_path.glob("*.tmp")), "временные файлы не остаются"


def test_due_once_a_day_and_retry_after_an_hour():
    assert B.due({}, T0)
    assert not B.due({"last_ok": T0.isoformat(), "last_attempt": T0.isoformat()}, T0 + timedelta(hours=23))
    assert B.due({"last_ok": T0.isoformat(), "last_attempt": T0.isoformat()}, T0 + timedelta(hours=24))
    failed = {"last_ok": T0.isoformat(), "last_attempt": (T0 + timedelta(hours=24)).isoformat()}
    assert not B.due(failed, T0 + timedelta(hours=24, minutes=30))
    assert B.due(failed, T0 + timedelta(hours=25))


def test_places_default_env_and_the_choice_in_settings(tmp_path):
    root = tmp_path / "prog"
    ds = B.destinations(root, {})
    assert [(d.kind, d.root) for d in ds] == [("local", root / "backups" / "magazin" / B.FOLDER_NAME)], \
        "облачная папка сама не ищется: Google Диск подключается кнопкой (app/services/gdrive_backup.py)"
    assert B.destinations(root, {B.ENV_CLOUD_DIR: "off"})[1:] == []
    own = tmp_path / "Yandex.Disk"
    ds = B.destinations(root, {B.ENV_CLOUD_DIR: str(own), B.ENV_LOCAL_DIR: str(tmp_path / "copies")})
    assert [d.root for d in ds] == [tmp_path / "copies" / B.FOLDER_NAME, own / B.FOLDER_NAME]
    ds = B.destinations(root, {B.ENV_LOCAL_DIR: str(tmp_path / "copies")}, chosen=str(tmp_path / "flash"))
    assert ds[0].root == tmp_path / "flash" / B.FOLDER_NAME and not ds[0].fallback_from, "выбор в «Настройках» важнее .env"


def test_service_runs_by_schedule_and_reports_status(tmp_path, monkeypatch):
    media = tmp_path / "lite"
    db = make_shop(media)
    clock = {"now": T0}
    svc = B.BackupService(root=tmp_path, media_root=media, db_file=db, keys=KEYS, dests_fn=lambda: dests(tmp_path),
                          clock=lambda: clock["now"])
    assert not svc.tick(), "сразу после запуска — не копирует"
    monkeypatch.setattr(B, "FIRST_DELAY_SECONDS", 0)
    assert svc.tick()
    assert not svc.tick(), "второй раз в те же сутки — нет"
    st = svc.status()
    assert st["available"] and st["last_ok"] == T0.isoformat(timespec="seconds") and st["cloud"] and not st["last_error"]
    assert [d["copies"] for d in st["destinations"]] == [1, 1] and st["counts"]["products"] == 3
    clock["now"] = T0 + timedelta(days=1, seconds=1)
    assert svc.tick()
    assert [d["copies"] for d in svc.status()["destinations"]] == [2, 2]


def test_service_records_a_failure(tmp_path):
    media = tmp_path / "lite"
    media.mkdir()
    svc = B.BackupService(root=tmp_path, media_root=media, db_file=media / "нет.db", keys=KEYS,
                          dests_fn=lambda: dests(tmp_path, cloud=False), clock=lambda: T0)
    with pytest.raises(B.BackupError):
        svc.run_once()
    st = svc.status()
    assert st["last_ok"] == "" and "Нет файла базы" in st["last_error"] and st["last_attempt"]
    assert not B.due(json.loads((media / B.STATE_FILE).read_text(encoding="utf-8")), T0 + timedelta(minutes=30))


def test_routes_are_for_the_admin_only_and_start_a_copy(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import routes_admin
    from app.api.deps import get_current_user
    from app.core.error_handlers import install_error_handlers
    from app.core.security import CurrentUser, Role
    from app.db.session import get_db

    class FakeDb:
        def add(self, _x):
            pass

        def flush(self):
            pass

        def commit(self):
            pass

    media = tmp_path / "lite"
    db = make_shop(media)
    svc = B.BackupService(root=tmp_path, media_root=media, db_file=db, keys=KEYS, dests_fn=lambda: dests(tmp_path),
                          clock=lambda: T0)
    monkeypatch.setattr(routes_admin, "_backups", lambda: svc)
    from app.api import deps
    monkeypatch.setattr(deps, "request_listener", lambda _request: "admin")    # как запрос с самого компьютера магазина
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(routes_admin.router)
    app.dependency_overrides[get_db] = lambda: FakeDb()
    client = TestClient(app, base_url="http://localhost:8000")
    assert client.get("/api/v1/admin/backup").status_code == 401
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(id="u2", username="kassir", role=Role.MERCHANDISER)
    assert client.get("/api/v1/admin/backup").status_code == 403
    assert client.post("/api/v1/admin/backup").status_code == 403
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(id="u1", username="owner", role=Role.ADMIN, session_listener="admin")
    assert client.get("/api/v1/admin/backup").json()["last_ok"] == ""
    started = []
    monkeypatch.setattr(svc, "start_now", lambda: started.append(1) or len(started) == 1)
    res = client.post("/api/v1/admin/backup")
    assert res.status_code == 202 and res.json() == {"started": True}
    assert client.post("/api/v1/admin/backup").status_code == 409
    monkeypatch.setattr(routes_admin, "_backups", lambda: None)
    assert client.get("/api/v1/admin/backup").json() == {"available": False}
    assert client.post("/api/v1/admin/backup").status_code == 409


def test_restore_script_lists_copies_and_waits_for_the_program_to_close(tmp_path, monkeypatch, capsys):
    from scripts import lite_restore as R

    media = tmp_path / "lite"
    db = make_shop(media)
    backup(tmp_path, media, db)
    monkeypatch.setattr(B, "destinations_from_settings", lambda *_a: dests(tmp_path))
    assert R.main(["--list", "--media-root", str(media)]) == 0
    out = capsys.readouterr().out
    assert "товаров 3, сотрудников 1, фото 3" in out and out.count("1. ") == 1, "одна копия в двух местах — одной строкой"
    monkeypatch.setattr(R, "program_running", lambda port: True)
    copy_dir = B.list_copies(tmp_path / "local" / B.FOLDER_NAME)[0].path
    assert R.main(["--copy", str(copy_dir), "--yes", "--media-root", str(media), "--port", "8000",
                   "--env-file", str(tmp_path / ".env")]) == 2
    assert "Закройте её окно" in capsys.readouterr().out
    monkeypatch.setattr(R, "program_running", lambda port: False)
    assert R.main(["--copy", str(copy_dir), "--yes", "--media-root", str(media), "--port", "8000",
                   "--env-file", str(tmp_path / ".env")]) == 0
    assert "Готово. Возвращено: товаров 3" in capsys.readouterr().out


def test_places_come_from_the_program_settings(tmp_path):
    class S:
        backup_local_dir = str(tmp_path / "copies")
        backup_cloud_dir = "off"
    assert [(d.kind, d.root) for d in B.destinations_from_settings(tmp_path, S())] == [
        ("local", tmp_path / "copies" / B.FOLDER_NAME)]
    S.backup_cloud_dir = str(tmp_path / "Yandex.Disk")
    assert B.destinations_from_settings(tmp_path, S())[1].root == tmp_path / "Yandex.Disk" / B.FOLDER_NAME
