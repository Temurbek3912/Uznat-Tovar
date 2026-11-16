"""Обновление программы в магазине (scripts/update.py, Обновить.bat) и номер версии в архиве (scripts/make_package.py).

Папки «установленной» и «новой» версии — временные; установщик, резервная копия и процессы — подставные.
"""
from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from scripts import make_package
from scripts import update as up

ROOT = Path(__file__).resolve().parent.parent


def _write(folder: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = folder / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def _program(folder: Path, files: dict[str, str], *, commit: str | None, date: str = "2026-11-16 10:00") -> None:
    base = {"start_lite.bat": "@echo off", "scripts/start_demo.py": "# demo " + (commit or "old")}
    base.update(files)
    _write(folder, base)
    if commit:
        (folder / "version.json").write_text(json.dumps({"commit": commit, "date": date, "files": sorted(base)}),
                                             encoding="utf-8")


def _db(path: Path, rows: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path)) as c:
        c.execute("CREATE TABLE IF NOT EXISTS products (name TEXT)")
        c.execute("DELETE FROM products")
        c.executemany("INSERT INTO products VALUES (?)", [(r,) for r in rows])


def _names(path: Path) -> list[str]:
    with sqlite3.connect(str(path)) as c:
        return [r[0] for r in c.execute("SELECT name FROM products ORDER BY name")]


@pytest.fixture
def shop(tmp_path):
    """Установленная версия aaa (с данными магазина) и распакованная новая bbb."""
    target, source = tmp_path / "shop", tmp_path / "new" / "uznat-tovar"
    _program(target, {"app/main.py": "old main", "app/gone.py": "old only", "frontend/a.js": "old a",
                      "scripts/lite_restore.py": "# backup"}, commit="aaa1111")
    (target / ".venv" / "Scripts").mkdir(parents=True)
    (target / ".venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    _write(target, {".env": "AUTH_SECRET_KEY=shop", "data/lite/catalog-uploads/1.jpg": "photo",
                    "data/lite/index/catalog.faiss": "index-old", "data/run_mode.json": '{"port": 8000}',
                    "models/hf/m.bin": "model", "data/install_state.json": '{"requirements": "old"}'})
    _db(target / up.DB_REL, ["Кола", "Хлеб"])
    _program(source, {"app/main.py": "new main", "frontend/a.js": "new a", "app/added.py": "new file",
                      "data/lite/shop.db": "NOT DATA", ".env": "NOT KEYS"}, commit="bbb2222", date="2026-11-20 09:00")
    # .env и data в списке новой версии оказаться не должны, но даже если окажутся — не трогаются
    v = json.loads((source / "version.json").read_text(encoding="utf-8"))
    v["files"] += [".env", "data/lite/shop.db"]
    (source / "version.json").write_text(json.dumps(v), encoding="utf-8")
    return NS(target=target, source=source)


class Fake:
    def __init__(self, target: Path, *, install_rc: int = 0, running: bool = True, backup_rc: int = 0,
                 on_install=None, answers=()):
        self.target, self.install_rc, self.backup_rc, self.on_install = target, install_rc, backup_rc, on_install
        self.running, self.calls, self.lines, self.killed, self.started = running, [], [], [], []
        self.answers, self.asked = list(answers), []

    def run(self, argv, **kw):
        self.calls.append(argv)
        if "scripts.lite_restore" in argv:
            return NS(returncode=self.backup_rc, stdout="Готово — на этом компьютере: X", stderr="диск полон")
        if "scripts.install" in argv:
            if self.on_install:
                self.on_install(self)
            return NS(returncode=self.install_rc, stdout="", stderr="")
        return NS(returncode=0, stdout="", stderr="")

    def deps(self) -> up.Deps:
        return up.Deps(run=self.run, out=self.lines.append, ask=self._ask,
                       shortcut_folders=lambda: [self.target], port_busy=lambda port: self.running,
                       find_processes=lambda root: [(4242, 8124)] if self.running else [],
                       kill_tree=self._kill, start_program=lambda root, port: self.started.append(port),
                       sleep=lambda s: None, now=lambda: 1_800_000_000.0)

    def _ask(self, question):
        self.asked.append(question)
        return self.answers.pop(0) if self.answers else ""

    def _kill(self, pid):
        self.killed.append(pid)
        self.running = False

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _opts(shop, **kw) -> up.Options:
    return up.Options(source=shop.source, **{"yes": True, **kw})


def test_update_replaces_program_keeps_shop_data_and_restarts(shop):
    t = shop.target
    fake = Fake(t)
    assert up.run(_opts(shop), fake.deps()) == 0, fake.text
    assert (t / "app/main.py").read_text(encoding="utf-8") == "new main"
    assert (t / "app/added.py").read_text(encoding="utf-8") == "new file"
    assert not (t / "app/gone.py").exists()                         # был в прежней версии, в новой нет
    assert json.loads((t / "version.json").read_text(encoding="utf-8"))["commit"] == "bbb2222"
    # данные магазина, ключи, модели — как были
    assert _names(t / up.DB_REL) == ["Кола", "Хлеб"]
    assert (t / ".env").read_text(encoding="utf-8") == "AUTH_SECRET_KEY=shop"
    assert (t / "data/lite/catalog-uploads/1.jpg").read_text(encoding="utf-8") == "photo"
    assert (t / "models/hf/m.bin").is_file()
    # закрыли, сделали копию, проверили установщиком без ярлыков, запустили снова на прежнем порту
    assert fake.killed == [4242] and fake.started == [8124]
    backup = next(c for c in fake.calls if "scripts.lite_restore" in c)
    assert backup[0] == str(up.venv_python(t)) and "--backup-now" in backup
    install = next(c for c in fake.calls if "scripts.install" in c)
    assert install[0] == str(up.venv_python(t)) and "--no-shortcut" in install and "--no-autostart" in install
    snap = next((t / up.BACKUP_DIR).iterdir())
    assert (snap / "program/app/gone.py").read_text(encoding="utf-8") == "old only"
    assert _names(snap / up.DB_REL) == ["Кола", "Хлеб"]
    assert "ПРОГРАММА ОБНОВЛЕНА" in fake.text


def test_failed_check_returns_old_program_and_old_database(shop):
    t = shop.target
    before = {p.relative_to(t).as_posix(): p.read_bytes() for p in t.rglob("*")
              if p.is_file() and "data" not in p.relative_to(t).parts}

    def migrate_and_break(fake):                   # новая версия успела изменить базу и пакеты, потом упала
        _db(t / up.DB_REL, ["испорчено"])
        (t / up.INDEX_REL / "catalog.faiss").write_text("index-new", encoding="utf-8")
        (t / up.STATE_REL).write_text('{"requirements": "new"}', encoding="utf-8")
        fake.on_install = None
    fake = Fake(t, install_rc=1, on_install=migrate_and_break)
    assert up.run(_opts(shop), fake.deps()) == 1
    after = {p.relative_to(t).as_posix(): p.read_bytes() for p in t.rglob("*")
             if p.is_file() and "data" not in p.relative_to(t).parts}
    assert after == before                                            # файлы программы — байт в байт прежние
    assert _names(t / up.DB_REL) == ["Кола", "Хлеб"]
    assert (t / up.INDEX_REL / "catalog.faiss").read_text(encoding="utf-8") == "index-old"
    installs = [c for c in fake.calls if "scripts.install" in c]
    assert len(installs) == 2 and "--no-models" in installs[1]       # прежние пакеты поставлены обратно
    assert fake.started == [8124] and "ОБНОВЛЕНИЕ НЕ УСТАНОВЛЕНО" in fake.text


def test_failed_backup_changes_nothing(shop):
    t = shop.target
    fake = Fake(t, backup_rc=1)
    assert up.run(_opts(shop), fake.deps()) == 1
    assert (t / "app/main.py").read_text(encoding="utf-8") == "old main" and (t / "app/gone.py").is_file()
    assert not any("scripts.install" in c for c in fake.calls)
    assert "Резервная копия не получилась" in fake.text and fake.started == [8124]


def test_same_version_does_nothing(shop):
    (shop.source / "version.json").write_text((shop.target / "version.json").read_text(encoding="utf-8"),
                                              encoding="utf-8")
    fake = Fake(shop.target)
    assert up.run(_opts(shop), fake.deps()) == 0
    assert "уже установлена" in fake.text and not fake.killed and not fake.calls


def test_older_version_asks_and_can_be_cancelled(shop):
    v = json.loads((shop.source / "version.json").read_text(encoding="utf-8"))
    v["date"] = "2026-01-01 00:00"
    (shop.source / "version.json").write_text(json.dumps(v), encoding="utf-8")
    fake = Fake(shop.target, answers=[""])
    assert up.run(_opts(shop, yes=False), fake.deps()) == 1
    assert "СТАРШЕ" in fake.asked[0] and "Отменено" in fake.text and not fake.killed
    assert (shop.target / "app/main.py").read_text(encoding="utf-8") == "old main"


def test_not_running_program_is_not_started(shop):
    fake = Fake(shop.target, running=False)
    assert up.run(_opts(shop), fake.deps()) == 0
    assert not fake.killed and not fake.started and "ярлык «Узнать товар»" in fake.text


def test_old_install_without_version_keeps_unknown_files(shop):
    t = shop.target
    (t / "version.json").unlink()
    fake = Fake(t, running=False)
    assert up.run(_opts(shop), fake.deps()) == 0
    assert (t / "app/gone.py").is_file()                 # не знаем, чей файл, — не удаляем
    assert (t / "app/main.py").read_text(encoding="utf-8") == "new main"
    assert "прежняя (без номера версии)" in fake.text


def test_files_unpacked_into_program_folder_only_run_installer(shop):
    fake = Fake(shop.source)
    (shop.source / ".venv/Scripts").mkdir(parents=True)
    assert up.run(up.Options(source=shop.source, target=shop.source, yes=True), fake.deps()) == 0
    assert [c for c in fake.calls if "scripts.install" in c] and not fake.killed


def test_target_is_found_or_asked(shop, tmp_path):
    fake = Fake(shop.target)
    d = fake.deps()
    assert up.find_target(up.Options(source=shop.source), d) == shop.target
    d.shortcut_folders = lambda: []
    fake.answers = [f'"{shop.target}"']                  # перетащили папку в окно — путь в кавычках
    assert up.find_target(up.Options(source=shop.source), d) == shop.target
    fake.answers = [str(tmp_path)]
    with pytest.raises(up.UpdateError, match="нет start_lite.bat"):
        up.find_target(up.Options(source=shop.source), d)
    other = tmp_path / "other"
    _program(other, {}, commit="ccc3333")
    d.shortcut_folders = lambda: [shop.target, other, shop.target]
    fake.answers = ["2"]
    assert up.find_target(up.Options(source=shop.source), d) == other


def test_running_program_is_found_only_in_its_own_folder(tmp_path):
    root = tmp_path / "shop"
    py = str(root / ".venv" / "Scripts" / "python.exe")
    rows = [
        {"ProcessId": 10, "ParentProcessId": 1, "Name": "cmd.exe", "CommandLine": f'cmd /c ""{root}\\start_lite.bat" "'},
        {"ProcessId": 11, "ParentProcessId": 10, "Name": "python.exe", "ExecutablePath": py,
         "CommandLine": f"{py} -m scripts.start_demo --lite --no-browser --port 8301"},
        {"ProcessId": 20, "ParentProcessId": 1, "Name": "python.exe", "ExecutablePath": "C:\\other\\python.exe",
         "CommandLine": "python -m scripts.start_demo --lite"},                        # программа из другой папки
        {"ProcessId": 30, "ParentProcessId": 1, "Name": "python.exe", "ExecutablePath": py,
         "CommandLine": f"{py} -m scripts.update"},
    ]
    fake_run = lambda argv, **kw: NS(returncode=0, stdout=json.dumps(rows), stderr="")  # noqa: E731
    if up.sys.platform.startswith("win"):
        assert up.find_program_processes(root, fake_run) == [(10, 8301)]


def test_swap_undo_restores_everything(tmp_path):
    t, s = tmp_path / "t", tmp_path / "s"
    _write(t, {"a.py": "old a", "b.py": "old b"})
    _write(s, {"a.py": "new a", "c.py": "new c"})
    swap = up.Swap(t, tmp_path / "snap")
    swap.apply(s, ["a.py", "c.py"], ["a.py", "b.py"])
    assert sorted(p.name for p in t.iterdir()) == ["a.py", "c.py"]
    swap.undo()
    assert {p.name: p.read_text(encoding="utf-8") for p in t.iterdir()} == {"a.py": "old a", "b.py": "old b"}


def test_protected_paths_are_never_program_files():
    for rel in ("data/lite/shop.db", ".env", ".venv/x", "models/hf/a", "logs/a.log", "app/__pycache__/m.pyc",
                "version.json"):
        assert up.is_protected(rel)
    assert not up.is_protected("app/main.py") and not up.is_protected("Обновить.bat")


def test_old_update_backups_are_pruned(tmp_path):
    for name in ("2026-11-01_000000", "2026-11-02_000000", "2026-11-03_000000", "2026-11-04_000000"):
        (tmp_path / up.BACKUP_DIR / name).mkdir(parents=True)
    up.prune_backups(tmp_path)
    assert sorted(p.name for p in (tmp_path / up.BACKUP_DIR).iterdir()) == [
        "2026-11-02_000000", "2026-11-03_000000", "2026-11-04_000000"]


def test_package_carries_version_and_file_list(tmp_path):
    def fake_run(argv, **kw):
        if argv[1] == "rev-parse":
            return NS(returncode=0, stdout="bbb2222ffff\n", stderr="")
        if argv[1] == "status":
            return NS(returncode=0, stdout="", stderr="")
        if argv[1] == "log":
            return NS(returncode=0, stdout="2026-11-20 09:00\n", stderr="")
        with zipfile.ZipFile(argv[argv.index("-o") + 1], "w") as z:
            z.writestr("uznat-tovar/", "")
            z.writestr("uznat-tovar/app/main.py", "x")
            z.writestr("uznat-tovar/Обновить.bat", "x")
        return NS(returncode=0, stdout="", stderr="")
    lines: list[str] = []
    assert make_package.main(["--out", str(tmp_path)], run=fake_run, out=lines.append, root=tmp_path) == 0
    archive = next(tmp_path.glob("*.zip"))
    with zipfile.ZipFile(archive) as z:
        v = json.loads(z.read("uznat-tovar/version.json"))
    assert v == {"commit": "bbb2222ffff", "date": "2026-11-20 09:00", "files": ["app/main.py", "Обновить.bat"]}
    assert "Обновить.bat" in lines[-1]


def test_update_bat_runs_the_updater():
    raw = (ROOT / "Обновить.bat").read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    text = raw.decode("utf-8")
    assert "-m scripts.update" in text and "chcp 65001" in text and "PYTHONIOENCODING=utf-8" in text
    assert "/version.json" in (ROOT / ".gitignore").read_text(encoding="utf-8")


def test_other_program_on_the_port_does_not_block_or_get_closed(shop):
    """Порт из run_mode.json занят чужой программой (у продавца — демонстрация на 8000): обновляем, никого не закрываем."""
    fake = Fake(shop.target, running=False)
    d = fake.deps()
    d.port_busy = lambda port: port == 8000
    assert up.run(_opts(shop), d) == 0
    assert not fake.killed and not fake.started and "ПРОГРАММА ОБНОВЛЕНА" in fake.text


def test_unknown_processes_fall_back_to_the_last_port(shop):
    fake = Fake(shop.target)
    d = fake.deps()
    d.find_processes = lambda root: None
    d.port_busy = lambda port: port == 8000
    assert up.run(_opts(shop), d) == 1
    assert "Закройте её окно" in fake.text and (shop.target / "app/main.py").read_text(encoding="utf-8") == "old main"
    d.port_busy = lambda port: False
    assert up.run(_opts(shop), d) == 0


def test_port_is_read_from_the_command_line():
    assert up.command_port(r'"x\python.exe" -m scripts.start_demo --lite --no-browser --port 8301') == 8301
    assert up.command_port("python -m scripts.start_demo --lite --port=8302") == 8302
    assert up.command_port("python -m scripts.start_demo --lite") == 8000


def test_database_restore_waits_while_windows_holds_the_file(shop, monkeypatch):
    """Найдено на копии: сразу после пробного запуска shop.db-wal ещё занят — возврат ждёт, а не сдаётся."""
    real, tries = up.restore_db, []

    def busy_twice(target, snap):
        tries.append(1)
        if len(tries) <= 2:
            raise PermissionError(32, "файл занят другим процессом")
        real(target, snap)
    monkeypatch.setattr(up, "restore_db", busy_twice)
    fake = Fake(shop.target, install_rc=1, on_install=lambda f: _db(shop.target / up.DB_REL, ["испорчено"]))
    assert up.run(_opts(shop), fake.deps()) == 1
    assert len(tries) == 3 and _names(shop.target / up.DB_REL) == ["Кола", "Хлеб"]
    assert "ОБНОВЛЕНИЕ НЕ УСТАНОВЛЕНО" in fake.text


def test_database_copy_leaves_no_open_connection(tmp_path):
    """В магазине база в режиме WAL: после копии файл базы должен быть свободен (иначе возврат не заменит его)."""
    from contextlib import closing
    db = tmp_path / up.DB_REL
    db.parent.mkdir(parents=True)
    with closing(sqlite3.connect(str(db))) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE products (name TEXT)")
        c.execute("INSERT INTO products VALUES ('Кола')")
        c.commit()
    up.save_db(tmp_path, tmp_path / "snap")
    db.rename(db.with_name("moved.db"))                 # в Windows не получится, пока соединение открыто
    assert _names(tmp_path / "snap" / up.DB_REL) == ["Кола"]


def test_program_is_restarted_in_its_own_minimized_window(tmp_path):
    calls = []
    up.start_program(tmp_path, 8301, popen=lambda argv, **kw: calls.append((argv, kw)))
    argv, kw = calls[0]
    assert argv[-3:] == ["--no-browser", "--port", "8301"] and argv[2].endswith("start_lite.bat")
    if up.sys.platform.startswith("win"):
        assert kw["creationflags"] & up.subprocess.CREATE_NEW_CONSOLE and kw["startupinfo"].wShowWindow == 7
        assert "stdout" not in kw and "stdin" not in kw          # своя консоль, а не вывод окна обновления
