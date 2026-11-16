"""Копия магазина в Google Диск напрямую и своя папка для копий (2026-11-16): вход по коду, загрузка только нового,
обрывы связи, уборка, восстановление из облака; выбор папки в «Настройках». Google — подставной (tools/fake_google.py)."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from app.services import gdrive_backup as G
from app.services import lite_backup as B
from tests.test_lite_backup import KEYS, T0, make_shop
from tools.fake_google import FakeGoogle

CLIENT = ("cid.apps.googleusercontent.com", "client-secret")


@pytest.fixture(autouse=True)
def _no_real_google(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_BASE", raising=False)


def auth_key() -> str:
    import base64
    return base64.urlsafe_b64encode(b"k" * 32).decode()


def google(media: Path, fake: FakeGoogle, clock=None) -> G.GoogleBackup:
    return G.GoogleBackup(media_root=media, client=CLIENT, secret_key=auth_key(),
                          http_factory=lambda: httpx.Client(transport=fake.transport()), sleep=lambda _s: None,
                          clock=clock or (lambda: T0))


def connect(g: G.GoogleBackup) -> None:
    code = g.start_connect()
    assert code["user_code"] == "ABCD-EFGH" and "google.com/device" in code["verification_url"]
    for _ in range(200):
        if g.status()["state"] != "waiting":
            break
        import time
        time.sleep(0.01)


def local_copy(tmp: Path, media: Path, db: Path, now=T0) -> tuple[Path, str]:
    dest = B.Destination("local", tmp / "local" / B.FOLDER_NAME, B.KEEP_LOCAL)
    report = B.create_backup(root=tmp, media_root=media, db_file=db, keys=dict(KEYS, AUTH_SECRET_KEY=auth_key()),
                             dests=[dest], now=now)
    assert report["ok"]
    return dest.root, report["name"]


def test_connect_by_code_keeps_the_key_encrypted(tmp_path):
    media = tmp_path / "lite"
    media.mkdir()
    fake = FakeGoogle(approve_after=2)
    g = google(media, fake)
    assert g.status()["state"] == "off"
    connect(g)
    st = g.status()
    assert st["state"] == "connected" and st["email"] == "magazin@example.com"
    raw = (media / B.SETTINGS_FILE).read_text(encoding="utf-8")
    assert "rt-" not in raw, "ключ Google в файле только зашифрованным"
    assert g.refresh_token().startswith("rt-") and fake.polls == 3
    other = G.GoogleBackup(media_root=media, client=CLIENT, secret_key="", sleep=lambda _s: None)
    assert other.status()["state"] == "lost", "другой AUTH_SECRET_KEY — не расшифровать, просим подключить заново"


def test_connect_needs_the_programs_google_client_and_a_server_key(tmp_path):
    g = G.GoogleBackup(media_root=tmp_path, client=None, secret_key=auth_key())
    assert g.status()["state"] == "unavailable"
    with pytest.raises(G.CloudError, match="продавцу"):
        g.start_connect()
    g = G.GoogleBackup(media_root=tmp_path, client=CLIENT, secret_key="")
    with pytest.raises(G.CloudError, match="AUTH_SECRET_KEY"):
        g.start_connect()


def test_client_comes_from_the_downloaded_file_or_env(tmp_path):
    assert G.load_client(tmp_path) is None
    (tmp_path / G.CLIENT_FILE).write_text(json.dumps({"installed": {"client_id": "a", "client_secret": "b"}}),
                                          encoding="utf-8")
    assert G.load_client(tmp_path) == ("a", "b")

    class S:
        google_client_id = "env-id"
        google_client_secret = "env-secret"
    assert G.load_client(tmp_path, S()) == ("env-id", "env-secret")


def test_copy_goes_to_google_and_next_day_only_new_photos(tmp_path):
    media = tmp_path / "lite"
    db = make_shop(media)
    fake = FakeGoogle(page_limit=2)                 # постраничная выдача тоже проверяется
    g = google(media, fake)
    connect(g)
    root, name = local_copy(tmp_path, media, db)
    result = g.sync(root, name, keep=7)
    assert result["photos_uploaded"] == 3
    tree = fake.tree()
    assert tree[""] == [B.FOLDER_NAME]
    assert sorted(tree[B.FOLDER_NAME]) == sorted([B.PHOTOS_DIR, name])
    assert set(tree[f"{B.FOLDER_NAME}/{name}"]) == {"manifest.json", "shop.db.gz", "photos.list", "keys.env", "index"}
    assert len(tree[f"{B.FOLDER_NAME}/{B.PHOTOS_DIR}"]) == 3
    st = g.status()
    assert st["copy"] == name and st["last_ok"] and not st["last_error"] and not g.due(name)

    (media / "catalog-uploads" / "P9").mkdir()
    (media / "catalog-uploads" / "P9" / "1.jpg").write_bytes(b"new-photo" * 100)
    root, name2 = local_copy(tmp_path, media, db, now=T0 + timedelta(days=1))
    assert g.due(name2)
    assert g.sync(root, name2, keep=7)["photos_uploaded"] == 1, "в облако уходят только новые фото"
    assert len(fake.tree()[f"{B.FOLDER_NAME}/{B.PHOTOS_DIR}"]) == 4


def test_cloud_keeps_the_newest_copies_and_drops_orphan_photos(tmp_path):
    media = tmp_path / "lite"
    db = make_shop(media)
    fake = FakeGoogle()
    g = google(media, fake)
    connect(g)
    names = []
    for day in range(3):
        photo = media / "catalog-uploads" / "P0" / "1.jpg"
        photo.write_bytes(b"day-%d" % day * 100)               # одно фото меняется каждый день
        root, name = local_copy(tmp_path, media, db, now=T0 + timedelta(days=day))
        names.append(name)
        g.sync(root, name, keep=2)
    folder = fake.tree()[B.FOLDER_NAME]
    assert names[0] not in folder and names[1] in folder and names[2] in folder
    assert len(fake.tree()[f"{B.FOLDER_NAME}/{B.PHOTOS_DIR}"]) == 4, "3 неизменных + 2 версии P0; версия дня 0 удалена"


def test_dropped_connection_is_retried_and_a_big_file_resumes(tmp_path, monkeypatch):
    media = tmp_path / "lite"
    db = make_shop(media)
    (media / "catalog-uploads" / "P0" / "big.jpg").write_bytes(bytes(range(256)) * 400)      # 100 КБ
    monkeypatch.setattr(G, "SMALL_UPLOAD", 10_000)
    monkeypatch.setattr(G, "CHUNK", 32_768)
    fake = FakeGoogle()
    g = google(media, fake)
    connect(g)
    root, name = local_copy(tmp_path, media, db)
    original = fake._chunk
    calls = {"n": 0}

    def flaky_chunk(sid, request):
        calls["n"] += 1
        if calls["n"] == 2:
            raise httpx.ConnectError("обрыв посреди большого файла", request=request)
        return original(sid, request)
    monkeypatch.setattr(fake, "_chunk", flaky_chunk)
    fake.fail_next = ["drop", 503]
    g.sync(root, name, keep=7)
    big = [f for f in fake.files.values() if f["data"] == bytes(range(256)) * 400]
    assert len(big) == 1, "большой файл догружен с места обрыва и целиком"
    assert B.verify_copy(root / name, full=True) == []


def test_no_internet_keeps_the_local_copy_and_retries_later_from_where_it_stopped(tmp_path):
    media = tmp_path / "lite"
    db = make_shop(media)
    fake = FakeGoogle()
    clock = {"now": T0}
    g = google(media, fake, clock=lambda: clock["now"])
    connect(g)
    root, name = local_copy(tmp_path, media, db)
    real = fake._handle
    uploads = {"n": 0}

    def cut_after_two_photos(request):
        if "/upload/" in str(request.url):
            uploads["n"] += 1
            if uploads["n"] > 2:
                raise httpx.ConnectError("интернет пропал", request=request)
        return real(request)
    fake._handle = cut_after_two_photos
    with pytest.raises(G.CloudError):
        g.sync(root, name, keep=7)
    assert B.list_copies(root)[0].name == name, "копия на компьютере на месте"
    st = g.status()
    assert st["last_error"] and not st["last_ok"]
    assert not g.due(name), "после неудачи — пауза час"
    clock["now"] = T0 + timedelta(hours=1, seconds=1)
    assert g.due(name)
    fake._handle = real
    assert g.sync(root, name, keep=7)["photos_uploaded"] == 1, "два фото уже были — догружено третье"


def test_lost_access_asks_to_connect_again(tmp_path):
    media = tmp_path / "lite"
    db = make_shop(media)
    fake = FakeGoogle()
    g = google(media, fake)
    connect(g)
    root, name = local_copy(tmp_path, media, db)
    fake.refresh_tokens.clear()                          # в Google отключили доступ (или 7 дней «на проверке» прошли)
    with pytest.raises(G.AccessLost):
        g.sync(root, name, keep=7)
    assert g.status()["state"] == "lost" and not g.connected and not g.due(name)


def test_disconnect_forgets_the_key_and_asks_google_to_revoke_it(tmp_path):
    media = tmp_path / "lite"
    media.mkdir()
    fake = FakeGoogle()
    g = google(media, fake)
    connect(g)
    g.disconnect()
    assert g.status()["state"] == "off" and not fake.refresh_tokens
    assert "google" not in B.read_settings(media)


def test_restore_downloads_the_copy_from_google_onto_a_new_computer(tmp_path):
    media = tmp_path / "lite"
    db = make_shop(media)
    fake = FakeGoogle()
    g = google(media, fake)
    connect(g)
    root, name = local_copy(tmp_path, media, db)
    g.sync(root, name, keep=7)
    drive = g.drive(httpx.Client(transport=fake.transport()))
    found = G.list_cloud_copies(drive, B.FOLDER_NAME)
    assert [c["name"] for c in found] == [name] and found[0]["counts"]["products"] == 3
    new_pc = tmp_path / "new-pc" / B.FOLDER_NAME
    got = G.download_copy(drive, name, new_pc, folder_name=B.FOLDER_NAME)
    assert B.verify_copy(got, full=True) == []
    new_media = tmp_path / "new-pc" / "lite"
    result = B.restore_copy(got, media_root=new_media)
    assert result["counts"]["products"] == 3 and result["counts"]["photos"] == 3
    assert (new_media / "catalog-uploads" / "P1" / "1.jpg").read_bytes() == b"photo-1" * 100
    def no_download(*_a):
        raise AssertionError("копия уже скачана и цела — второй раз не качается")
    drive.get_file = no_download
    assert G.download_copy(drive, name, new_pc, folder_name=B.FOLDER_NAME) == got


def test_restore_keeps_the_folder_choice_and_the_google_connection(tmp_path):
    media = tmp_path / "lite"
    db = make_shop(media)
    fake = FakeGoogle()
    g = google(media, fake)
    connect(g)
    B.update_settings(media, lambda d: dict(d, local_dir=str(tmp_path / "flash")))
    root, name = local_copy(tmp_path, media, db)
    env = tmp_path / ".env"
    env.write_text(f"AUTH_SECRET_KEY={auth_key()}\n", encoding="utf-8")
    import base64
    new_key = base64.urlsafe_b64encode(b"n" * 32).decode()
    keys_file = root / name / B.KEYS_FILE                    # копия сделана на компьютере с другим ключом
    keys_file.write_text(f"AUTH_SECRET_KEY={new_key}\n", encoding="utf-8")
    manifest = json.loads((root / name / B.MANIFEST).read_text(encoding="utf-8"))
    manifest["files"][B.KEYS_FILE] = B.sha256_file(keys_file)
    (root / name / B.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    B.restore_copy(root / name, media_root=media, env_file=env)
    settings = B.read_settings(media)
    assert settings["local_dir"] == str(tmp_path / "flash")
    after = G.GoogleBackup(media_root=media, client=CLIENT, secret_key=new_key)
    assert after.refresh_token().startswith("rt-"), "ключ Google перешифрован новым AUTH_SECRET_KEY"


def test_service_sends_to_google_after_the_local_copy_and_catches_up_later(tmp_path, monkeypatch):
    media = tmp_path / "lite"
    db = make_shop(media)
    fake = FakeGoogle()
    g = google(media, fake)
    dest = B.Destination("local", tmp_path / "local" / B.FOLDER_NAME, B.KEEP_LOCAL)
    svc = B.BackupService(root=tmp_path, media_root=media, db_file=db, keys=KEYS, dests_fn=lambda: [dest],
                          clock=lambda: T0, google=g)
    sent = []
    monkeypatch.setattr(svc, "start_cloud", lambda **kw: sent.append(1))
    svc.run_once()
    assert sent == [1], "после копии на компьютере — отправка в облако"
    monkeypatch.undo()
    assert not svc.start_cloud(wait=True), "Google не подключён — отправлять нечего"
    connect(g)                                           # подключили — последняя копия уходит сразу, в фоне
    import time
    for _ in range(300):
        if g.status()["copy"] and not g.running:
            break
        time.sleep(0.01)
    assert g.status()["copy"] == B.list_copies(dest.root)[0].name
    assert not svc.start_cloud(wait=True), "уже в облаке"
    st = svc.status()
    assert st["cloud"] and st["google"]["state"] == "connected" and st["local"]["path"] == str(dest.root)


# ----------------------------------------------------------------------------------------------------------- своя папка

def test_own_folder_is_checked_saved_and_used(tmp_path):
    media = tmp_path / "lite"
    db = make_shop(media)
    svc = B.BackupService(root=tmp_path, media_root=media, db_file=db, keys=KEYS, clock=lambda: T0)
    with pytest.raises(B.BackupError, match="полный путь"):
        svc.set_local_dir("Копии")
    with pytest.raises(B.BackupError, match="внутри данных"):
        svc.set_local_dir(str(media / "x"))
    own = tmp_path / "Мои копии"
    assert svc.set_local_dir(f'"{own}"') == str(own)
    assert own.is_dir(), "папки не было — создана"
    assert B.read_settings(media)["local_dir"] == str(own)
    svc.run_once()
    assert len(B.list_copies(own / B.FOLDER_NAME)) == 1
    st = svc.status()
    assert st["local"]["chosen"] == str(own) and st["local"]["path"] == str(own / B.FOLDER_NAME)
    assert svc.set_local_dir("") == "" and "local_dir" not in B.read_settings(media)
    assert svc.status()["local"]["path"] == str(tmp_path / "backups" / "magazin" / B.FOLDER_NAME)


def test_not_enough_space_is_refused(tmp_path, monkeypatch):
    media = tmp_path / "lite"
    make_shop(media)
    monkeypatch.setattr(B, "_free_bytes", lambda _p: 10 * 2**20)
    with pytest.raises(B.BackupError, match="Мало места"):
        B.check_folder(str(tmp_path / "flash"), media_root=media, need_bytes=2**20)


def test_unplugged_flash_drive_falls_back_to_the_standard_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "_anchor_ok", lambda p: "flash" not in str(p))
    ds = B.destinations(tmp_path, {}, chosen=str(tmp_path / "flash"))
    assert ds[0].root == tmp_path / "backups" / "magazin" / B.FOLDER_NAME and ds[0].fallback_from == str(tmp_path / "flash")


def test_drive_list_has_free_space():
    drives = B.list_drives()
    import os
    if os.name == "nt":
        assert drives and all(d["path"].endswith(":\\") and d["total"] >= d["free"] >= 0 for d in drives)
    else:
        assert drives == []


# ----------------------------------------------------------------------------------------------------------- API

def _client(tmp_path, monkeypatch, svc):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import deps, routes_admin
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

    monkeypatch.setattr(routes_admin, "_backups", lambda: svc)
    monkeypatch.setattr(deps, "request_listener", lambda _request: "admin")
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(routes_admin.router)
    app.dependency_overrides[get_db] = lambda: FakeDb()
    client = TestClient(app, base_url="http://localhost:8000")
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(id="u2", username="kassir", role=Role.MERCHANDISER)
    assert client.put("/api/v1/admin/backup/folder", json={"path": ""}).status_code == 403
    assert client.post("/api/v1/admin/backup/google/connect").status_code == 403
    assert client.delete("/api/v1/admin/backup/google").status_code == 403
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(id="u1", username="owner", role=Role.ADMIN,
                                                                      session_listener="admin")
    return client


def test_routes_choose_the_folder_and_connect_google(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    media = tmp_path / "lite"
    db = make_shop(media)
    fake = FakeGoogle(approve_after=1)
    svc = B.BackupService(root=tmp_path, media_root=media, db_file=db, keys=KEYS, clock=lambda: T0,
                          google=google(media, fake))
    client = _client(tmp_path, monkeypatch, svc)
    bad = client.put("/api/v1/admin/backup/folder", json={"path": "Копии"})
    assert bad.status_code == 400 and bad.json()["code"] == "backup_folder_relative"
    ok = client.put("/api/v1/admin/backup/folder", json={"path": str(tmp_path / "own")})
    assert ok.status_code == 200 and ok.json()["local"]["chosen"] == str(tmp_path / "own")
    st = client.get("/api/v1/admin/backup").json()
    assert st["google"]["state"] == "off" and isinstance(st["drives"], list)
    code = client.post("/api/v1/admin/backup/google/connect").json()
    assert code["user_code"] == "ABCD-EFGH"
    for _ in range(200):
        st = client.get("/api/v1/admin/backup").json()["google"]
        if st["state"] != "waiting":
            break
        import time
        time.sleep(0.01)
    assert st["state"] == "connected" and st["email"] == "magazin@example.com"
    assert "token" not in json.dumps(st), "ключ доступа не уходит в браузер"
    assert client.delete("/api/v1/admin/backup/google").json()["google"]["state"] == "off"
    svc.google = None
    assert client.post("/api/v1/admin/backup/google/connect").status_code == 409


def test_restore_script_signs_in_to_google_on_a_new_computer(tmp_path, monkeypatch, capsys):
    from scripts import lite_restore as R

    media = tmp_path / "lite"
    db = make_shop(media)
    fake = FakeGoogle()
    g = google(media, fake)
    connect(g)
    root, name = local_copy(tmp_path, media, db)
    g.sync(root, name, keep=7)
    new_media = tmp_path / "new" / "lite"
    new_local = B.Destination("local", tmp_path / "new" / "backups" / B.FOLDER_NAME, B.KEEP_LOCAL)
    monkeypatch.setattr(B, "destinations_from_settings", lambda *_a: [new_local])
    monkeypatch.setattr(G, "load_client", lambda *_a: CLIENT)
    monkeypatch.setattr(G, "_default_http", lambda: httpx.Client(transport=fake.transport()))
    monkeypatch.setattr(G.time, "sleep", lambda _s: None)
    answers = iter(["д", "1", "д"])
    monkeypatch.setattr("builtins.input", lambda _p="": next(answers))
    monkeypatch.setattr(R, "program_running", lambda port: False)
    env = tmp_path / "new" / ".env"
    assert R.main(["--media-root", str(new_media), "--port", "8000", "--env-file", str(env)]) == 0
    out = capsys.readouterr().out
    assert "ABCD-EFGH" in out and "в Google Диске" in out and "Готово. Возвращено: товаров 3" in out
    assert (new_media / "catalog-uploads" / "P2" / "1.jpg").is_file()
    assert "google" in B.read_settings(new_media), "вошли при восстановлении — Google Диск подключён и в программе"


def test_package_carries_the_google_client_file(tmp_path):
    import zipfile
    from types import SimpleNamespace as NS

    from scripts import make_package

    def fake_run(argv, **kw):
        if argv[1] == "rev-parse":
            return NS(returncode=0, stdout="91b1d16abcdef\n", stderr="")
        if argv[1] == "status":
            return NS(returncode=0, stdout="", stderr="")
        with zipfile.ZipFile(argv[argv.index("-o") + 1], "w") as z:
            z.writestr("uznat-tovar/app/main.py", "x")
        return NS(returncode=0, stdout="", stderr="")
    lines: list[str] = []
    assert make_package.main(["--out", str(tmp_path / "dist")], run=fake_run, out=lines.append, root=tmp_path) == 0
    assert any(G.CLIENT_FILE in ln and "не будет" in ln for ln in lines)
    (tmp_path / G.CLIENT_FILE).write_text('{"installed": {"client_id": "a", "client_secret": "b"}}', encoding="utf-8")
    lines.clear()
    assert make_package.main(["--out", str(tmp_path / "dist2")], run=fake_run, out=lines.append, root=tmp_path) == 0
    archive = next((tmp_path / "dist2").glob("*.zip"))
    assert "uznat-tovar/" + G.CLIENT_FILE in zipfile.ZipFile(archive).namelist()
