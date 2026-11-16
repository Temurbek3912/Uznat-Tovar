"""
Обновить программу «Узнать товар» в магазине до новой версии. Товары, фото, сотрудники, настройки и ключи остаются.

Как обновить: распакуйте новый архив программы в любую папку (например, «Загрузки») и дважды щёлкните в ней «Обновить.bat».
Программа сама найдёт установленную версию (по ярлыку «Узнать товар»), закроет её, сделает резервную копию, заменит файлы
программы, проверит пакеты и сделает пробный запуск. Если пробный запуск не прошёл — всё возвращается как было
(прежние файлы программы и прежняя база), и программа снова запускается.

    Обновить.bat                       # найти установленную программу и обновить
    Обновить.bat --target D:\\uznat-tovar   # если программа стоит не там, где ярлык
    Обновить.bat --yes                 # не спрашивать подтверждение

Что НЕ трогается никогда: data (база, фото, индекс, настройки), .env (ключи), .venv (Python программы), models (модели),
logs, backups. Файлы прежней версии и копия базы до обновления лежат в data/update-backup/<время> (три последних).

Только стандартная библиотека Python: запускается системным Python, пакеты — в .venv установленной программы.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent          # новая версия (распакованный архив)
VERSION_FILE = "version.json"
BACKUP_DIR = Path("data") / "update-backup"
KEEP_BACKUPS = 3
DB_RETRIES = 30                  # секунд ждать, пока закрытая программа отпустит файлы базы
DB_REL = Path("data") / "lite" / "shop.db"
INDEX_REL = Path("data") / "lite" / "index"
STATE_REL = Path("data") / "install_state.json"
# Никогда не заменяются и не удаляются: данные магазина, ключи, Python программы, модели, журналы, копии.
PROTECTED_TOP = {"data", ".env", ".venv", "venv", "models", "logs", "backups", "dist", ".git", ".claude", "secrets"}
SKIP_PARTS = {"__pycache__", ".pytest_cache"}
SHORTCUTS = ("Desktop", "Узнать товар"), ("Startup", "Узнать товар (автозапуск)")


class UpdateError(RuntimeError):
    """Обновление остановлено; текст — для человека."""


# ------------------------------------------------------------------ версии и список файлов


def read_version(folder: Path) -> dict:
    try:
        data = json.loads((folder / VERSION_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def describe_version(v: dict) -> str:
    if not v.get("commit"):
        return "прежняя (без номера версии)"
    return f"от {v.get('date') or '?'} ({str(v['commit'])[:7]})"


def is_protected(rel: str) -> bool:
    parts = Path(rel).parts
    return not parts or parts[0] in PROTECTED_TOP or any(p in SKIP_PARTS for p in parts) or rel == VERSION_FILE


def program_files(folder: Path, version: dict | None = None) -> list[str]:
    """Файлы программы относительно папки (через «/»). Есть список в version.json — он, иначе — всё, кроме данных."""
    version = read_version(folder) if version is None else version
    listed = version.get("files")
    if isinstance(listed, list) and listed:
        return sorted({str(f) for f in listed if isinstance(f, str) and not is_protected(f) and (folder / f).is_file()})
    found = []
    for p in folder.rglob("*"):
        if p.is_file():
            rel = p.relative_to(folder).as_posix()
            if not is_protected(rel):
                found.append(rel)
    return sorted(found)


def is_installation(folder: Path) -> bool:
    return (folder / "start_lite.bat").is_file() and (folder / "scripts" / "start_demo.py").is_file()


def venv_python(root: Path) -> Path:
    return root / ".venv" / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")


def same_folder(a: Path, b: Path) -> bool:
    try:
        return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))
    except OSError:
        return False


# ------------------------------------------------------------------ системные действия (подменяются в проверках)


def _ps(script: str, runner=subprocess.run):
    return runner(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script],
               capture_output=True, text=True, encoding="utf-8", errors="replace")


def shortcut_folders(runner=subprocess.run) -> list[Path]:
    """Папки, на которые указывают ярлыки «Узнать товар» (рабочий стол и автозагрузка)."""
    if not sys.platform.startswith("win"):
        return []
    parts = []
    for folder, name in SHORTCUTS:
        parts.append(f"$p=Join-Path ([Environment]::GetFolderPath('{folder}')) '{name}.lnk'; "
                     "if (Test-Path -LiteralPath $p) { (New-Object -ComObject WScript.Shell).CreateShortcut($p).TargetPath }")
    res = _ps("; ".join(parts), runner)
    out = []
    for line in (res.stdout or "").splitlines():
        line = line.strip()
        if line:
            out.append(Path(line).parent)
    return out


def port_busy(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def command_port(cmd: str, default: int = 8000) -> int:
    """Порт из командной строки start_demo (--port N или --port=N); нет — порт по умолчанию."""
    parts = cmd.replace("=", " ").split()
    for i, p in enumerate(parts[:-1]):
        if p == "--port":
            try:
                return int(parts[i + 1].strip('"'))
            except ValueError:
                break
    return default


def find_program_processes(root: Path, runner=subprocess.run) -> list[tuple[int, int]] | None:
    """Запущенная программа из этой папки: [(процесс, порт)] — окно start_lite.bat (cmd) или сам Python.
    Программы из других папок (например, демонстрация продавца) не трогаем. None — список процессов не получен."""
    if not sys.platform.startswith("win"):
        return None
    script = ("Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine "
              "| ConvertTo-Json -Compress")
    res = _ps(script, runner)
    try:
        rows = json.loads(res.stdout or "")
    except ValueError:
        return None
    rows = [rows] if isinstance(rows, dict) else rows
    if not isinstance(rows, list):
        return None
    by_pid = {r.get("ProcessId"): r for r in rows if isinstance(r, dict)}
    want = os.path.normcase(str(root.resolve()))
    found: dict[int, int] = {}
    for r in by_pid.values():
        cmd, exe = r.get("CommandLine") or "", r.get("ExecutablePath") or ""
        if "scripts.start_demo" not in cmd or "--lite" not in cmd:
            continue
        if not os.path.normcase(exe).startswith(want + os.sep):
            continue
        parent = by_pid.get(r.get("ParentProcessId")) or {}
        pcmd = parent.get("CommandLine") or ""
        if (parent.get("Name") or "").lower() == "cmd.exe" and "start_lite.bat" in pcmd.lower():
            found[parent["ProcessId"]] = command_port(cmd)   # окно программы: закрываем целиком, без «нажмите клавишу»
        else:
            found[r["ProcessId"]] = command_port(cmd)
    return sorted(found.items())


def kill_tree(pid: int, runner=subprocess.run) -> None:
    runner(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)


def start_program(root: Path, port: int, popen=subprocess.Popen) -> None:
    """Запустить программу снова в своём свёрнутом окне, как автозапуск. Своя консоль, а не «start»: так программа не
    наследует окно и вывод обновления (найдено проверкой: окно обновления не закрывалось, пока работает программа)."""
    argv = ["cmd", "/c", str(root / "start_lite.bat"), "--no-browser", "--port", str(port)]
    if not sys.platform.startswith("win"):
        popen(argv, cwd=str(root))
        return
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 7                                  # SW_SHOWMINNOACTIVE: свёрнуто, не отнимает фокус
    # без stdin/stdout: тогда Windows даёт окну программы его собственную консоль (вывод виден, как у ярлыка)
    popen(argv, cwd=str(root), creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=info, close_fds=True)


def _replace(src: Path, dst: Path, sleep=time.sleep) -> None:
    """os.replace с повторами: антивирус может ненадолго держать файл."""
    for attempt in range(5):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 4:
                raise
            sleep(0.5 * (attempt + 1))


def _ask(text: str) -> str:
    try:
        return input(text)
    except EOFError:
        return ""


@dataclass
class Deps:
    run: Callable = subprocess.run
    popen: Callable = subprocess.Popen
    out: Callable[[str], None] = lambda s: print(s, flush=True)
    ask: Callable[[str], str] = _ask
    shortcut_folders: Callable[[], list[Path]] = None        # type: ignore[assignment]
    port_busy: Callable[[int], bool] = port_busy
    find_processes: Callable[[Path], list[tuple[int, int]] | None] = None   # type: ignore[assignment]
    kill_tree: Callable[[int], None] = None                  # type: ignore[assignment]
    start_program: Callable[[Path, int], None] = None        # type: ignore[assignment]
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.time

    def __post_init__(self):
        if self.shortcut_folders is None:
            self.shortcut_folders = lambda: shortcut_folders(self.run)
        if self.find_processes is None:
            self.find_processes = lambda root: find_program_processes(root, self.run)
        if self.kill_tree is None:
            self.kill_tree = lambda pid: kill_tree(pid, self.run)
        if self.start_program is None:
            self.start_program = lambda root, port: start_program(root, port, self.popen)


@dataclass
class Options:
    source: Path = ROOT
    target: Path | None = None
    yes: bool = False
    force: bool = False
    cpu: bool = False
    models: bool = True
    check_timeout: float | None = None


# ------------------------------------------------------------------ шаги


def _yes(d: Deps, opts: Options, text: str) -> bool:
    if opts.yes:
        return True
    return d.ask(text + " Да — «д», нет — Enter: ").strip().lower() in ("д", "да", "y", "yes")


def find_target(opts: Options, d: Deps) -> Path:
    if opts.target:
        t = Path(opts.target)
        if not is_installation(t):
            raise UpdateError(f"В папке {t} программы «Узнать товар» нет (нет start_lite.bat).")
        return t
    found: list[Path] = []
    for f in d.shortcut_folders():
        if is_installation(f) and not any(same_folder(f, x) for x in found):
            found.append(f)
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        d.out("Найдено несколько установленных программ:")
        for i, f in enumerate(found, 1):
            d.out(f"  {i}. {f}  — версия {describe_version(read_version(f))}")
        answer = d.ask("Какую обновить? Номер (Enter — 1): ").strip()
        try:
            return found[(int(answer) if answer else 1) - 1]
        except (ValueError, IndexError):
            raise UpdateError("Такого номера нет. Ничего не изменилось.") from None
    d.out("Ярлык «Узнать товар» не найден, поэтому не знаю, где стоит программа.")
    answer = d.ask("Перетащите сюда папку установленной программы (или впишите путь) и нажмите Enter: ").strip().strip('"')
    if not answer:
        raise UpdateError("Папка не указана. Ничего не изменилось.")
    t = Path(answer)
    if not is_installation(t):
        raise UpdateError(f"В папке {t} программы «Узнать товар» нет (нет start_lite.bat).")
    return t


def running_port(target: Path) -> int:
    try:
        data = json.loads((target / "data" / "run_mode.json").read_text(encoding="utf-8"))
        return int(data.get("port") or 8000)
    except (OSError, ValueError, TypeError, AttributeError):
        return 8000


def stop_program(target: Path, d: Deps) -> int | None:
    """Закрыть программу из этой папки. Возвращает её порт (после обновления запустим снова) или None — не работала.
    Порт берётся из командной строки самой программы: в data/run_mode.json может быть порт другой программы."""
    procs = d.find_processes(target)
    if procs is None:                               # список процессов не получен — судим по порту последнего запуска
        port = running_port(target)
        if d.port_busy(port):
            raise UpdateError(f"Программа, похоже, работает (порт {port}). Закройте её окно и запустите обновление снова.")
        return None
    if not procs:
        return None
    d.out("Программа сейчас работает — закрываю её на время обновления (покупатели несколько минут не смогут "
          "ею пользоваться).")
    ports = sorted({port for _, port in procs})
    for pid, _ in procs:
        d.kill_tree(pid)
    for _ in range(60):
        if not d.find_processes(target) and not any(d.port_busy(p) for p in ports):
            return ports[0]
        d.sleep(0.5)
    raise UpdateError("Программа не закрылась. Закройте её окно сами и запустите обновление снова.")


def make_shop_backup(target: Path, d: Deps) -> None:
    """Обычная резервная копия магазина (как ежедневная) — средствами установленной версии."""
    if not (target / DB_REL).is_file():
        d.out("  базы магазина ещё нет — копировать нечего")
        return
    if not (target / "scripts" / "lite_restore.py").is_file():
        d.out("  в установленной версии нет резервных копий — сохраняю только копию базы (ниже)")
        return
    res = d.run([str(venv_python(target)), "-m", "scripts.lite_restore", "--backup-now"], cwd=str(target),
                env={**os.environ, "PYTHONIOENCODING": "utf-8"}, capture_output=True, text=True,
                encoding="utf-8", errors="replace")
    for line in (res.stdout or "").splitlines():
        d.out("  " + line)
    if res.returncode != 0:
        raise UpdateError("Резервная копия не получилась — обновление остановлено, ничего не изменилось.\n"
                          + "\n".join((res.stderr or "").strip().splitlines()[-5:]))


def save_db(target: Path, snap: Path) -> None:
    """Точная копия базы и индекса до обновления: новая версия может изменить базу при первом запуске."""
    db = target / DB_REL
    if db.is_file():
        dst = snap / DB_REL
        dst.parent.mkdir(parents=True, exist_ok=True)
        # closing(): «with sqlite3.connect()» сам соединение НЕ закрывает, а открытое соединение держит shop.db-wal —
        # потом базу не вернуть (найдено проверкой на копии: WinError 32).
        with closing(sqlite3.connect(str(db))) as src, closing(sqlite3.connect(str(dst))) as out:
            src.backup(out)
        with closing(sqlite3.connect(str(dst))) as check:
            ok = check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        if not ok:
            raise UpdateError("Копия базы не прошла проверку — обновление остановлено, ничего не изменилось.")
    index = target / INDEX_REL
    if index.is_dir():
        shutil.copytree(index, snap / INDEX_REL, ignore=shutil.ignore_patterns("*.lock"))


def restore_db(target: Path, snap: Path) -> None:
    db = snap / DB_REL
    if db.is_file():
        live = target / DB_REL
        for extra in (live.with_name(live.name + "-wal"), live.with_name(live.name + "-shm")):
            extra.unlink(missing_ok=True)
        shutil.copy2(db, live)
    index = snap / INDEX_REL
    if index.is_dir():
        live = target / INDEX_REL
        if live.exists():
            shutil.rmtree(live)
        shutil.copytree(index, live)


@dataclass
class Swap:
    """Замена файлов программы с журналом: прежние файлы переносятся в snap/program, при сбое возвращаются."""
    target: Path
    snap: Path
    moved: list[str] = field(default_factory=list)      # прежние файлы, перенесённые в копию
    added: list[str] = field(default_factory=list)      # новые файлы, которых раньше не было

    def _keep_old(self, rel: str, sleep) -> None:
        live = self.target / rel
        if live.exists():
            kept = self.snap / "program" / rel
            kept.parent.mkdir(parents=True, exist_ok=True)
            _replace(live, kept, sleep)
            self.moved.append(rel)

    def apply(self, source: Path, new_files: list[str], old_files: list[str], sleep=time.sleep) -> tuple[int, int]:
        new_set = set(new_files)
        removed = 0
        for rel in old_files:                          # файлы прежней версии, которых в новой нет
            if rel not in new_set and not is_protected(rel) and (self.target / rel).is_file():
                self._keep_old(rel, sleep)
                removed += 1
        for rel in new_files:
            src, live = source / rel, self.target / rel
            existed = live.exists()
            if existed:
                self._keep_old(rel, sleep)
            live.parent.mkdir(parents=True, exist_ok=True)
            tmp = live.with_name(live.name + ".new")
            shutil.copy2(src, tmp)
            _replace(tmp, live, sleep)
            if not existed:
                self.added.append(rel)
        return len(new_files), removed

    def undo(self, sleep=time.sleep) -> None:
        for rel in reversed(self.added):
            (self.target / rel).unlink(missing_ok=True)
        for rel in reversed(self.moved):
            kept, live = self.snap / "program" / rel, self.target / rel
            live.parent.mkdir(parents=True, exist_ok=True)
            if live.exists():
                live.unlink()
            _replace(kept, live, sleep)
        self.added, self.moved = [], []


def install_argv(target: Path, opts: Options, *, models: bool | None = None) -> list[str]:
    argv = [str(venv_python(target)), "-m", "scripts.install", "--no-shortcut", "--no-autostart"]
    if opts.cpu:
        argv.append("--cpu")
    if not (opts.models if models is None else models):
        argv.append("--no-models")
    if opts.check_timeout:
        argv += ["--check-timeout", str(opts.check_timeout)]
    return argv


def prune_backups(target: Path, keep: int = KEEP_BACKUPS) -> None:
    root = target / BACKUP_DIR
    if not root.is_dir():
        return
    snaps = sorted(p for p in root.iterdir() if p.is_dir())
    for old in snaps[:-keep]:
        shutil.rmtree(old, ignore_errors=True)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def run(opts: Options, d: Deps | None = None) -> int:
    d = d or Deps()
    out = d.out
    source = Path(opts.source)
    new_v = read_version(source)
    out("Обновление «Узнать товар»")
    out(f"  новая версия: {describe_version(new_v)}  (папка {source})")
    try:
        target = find_target(opts, d)
    except UpdateError as exc:
        out(str(exc))
        return 1
    old_v = read_version(target)
    out(f"  установлена:  {describe_version(old_v)}  (папка {target})")

    if same_folder(source, target):
        out("\nНовые файлы уже распакованы прямо в папку программы — проверяю пакеты и делаю пробный запуск.")
        return d.run(install_argv(target, opts), cwd=str(target)).returncode
    if not venv_python(target).is_file():
        out(f"\nВ папке {target} программа не доустановлена (нет .venv). Запустите там «Установить.bat».")
        return 1
    if not opts.force and new_v.get("commit") and new_v.get("commit") == old_v.get("commit"):
        out("\nЭта версия уже установлена. Обновлять нечего.")
        return 0
    if not opts.force and new_v.get("date") and old_v.get("date") and str(new_v["date"]) < str(old_v["date"]):
        if not _yes(d, opts, "\nВНИМАНИЕ: распакованная версия СТАРШЕ установленной. Всё равно поставить её?"):
            out("Отменено. Ничего не изменилось.")
            return 1
    new_files = program_files(source, new_v)
    if not new_files or "scripts/start_demo.py" not in new_files:
        out("\nВ этой папке нет файлов программы — распакуйте архив целиком и запустите «Обновить.bat» из него.")
        return 1
    if not _yes(d, opts, "\nОбновить? Товары, фото, сотрудники и настройки останутся."):
        out("Отменено. Ничего не изменилось.")
        return 1

    try:
        port = stop_program(target, d)
        was_running = port is not None
    except UpdateError as exc:
        out(str(exc))
        return 1

    stamp = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(d.now()))
    snap = target / BACKUP_DIR / stamp
    swap = Swap(target, snap)
    state_before = _read_text(target / STATE_REL)
    try:
        old_version_bytes: bytes | None = (target / VERSION_FILE).read_bytes()
    except OSError:
        old_version_bytes = None

    def restart() -> None:
        if was_running:
            out("Запускаю программу снова...")
            d.start_program(target, port)

    try:
        out("\n[1/4] Резервная копия магазина")
        make_shop_backup(target, d)
        save_db(target, snap)
        if old_version_bytes is not None:
            (snap / VERSION_FILE).write_bytes(old_version_bytes)
        out(f"  копия базы до обновления: {snap}")

        out("\n[2/4] Замена файлов программы")
        old_files = program_files(target, old_v) if old_v.get("files") else []
        n_new, n_removed = swap.apply(source, new_files, old_files, d.sleep)
        if (source / VERSION_FILE).is_file():
            shutil.copy2(source / VERSION_FILE, target / VERSION_FILE)
        out(f"  файлов программы: {n_new}" + (f", убрано устаревших: {n_removed}" if n_removed else ""))
    except (UpdateError, OSError, sqlite3.Error) as exc:
        swap.undo(d.sleep)
        out(f"\nНе получилось: {exc}\nВсё оставлено как было.")
        restart()
        return 1

    out("\n[3/4] Пакеты и пробный запуск новой версии")
    ok = d.run(install_argv(target, opts), cwd=str(target)).returncode == 0
    if not ok:
        out("\n[4/4] Новая версия не прошла проверку — возвращаю прежнюю")
        try:
            swap.undo(d.sleep)
            if old_version_bytes is not None:
                (target / VERSION_FILE).write_bytes(old_version_bytes)
            else:
                (target / VERSION_FILE).unlink(missing_ok=True)
            # Пробный запуск только что закрыт: Windows отпускает файлы базы не сразу (найдено проверкой на копии).
            for attempt in range(DB_RETRIES):
                try:
                    restore_db(target, snap)
                    break
                except PermissionError:
                    if attempt == DB_RETRIES - 1:
                        raise
                    d.sleep(1.0)
        except OSError as exc:
            out(f"Вернуть не получилось: {exc}\nПрежние файлы и база лежат в папке {snap} — позвоните продавцу программы.")
            return 1
        if _read_text(target / STATE_REL) != state_before:
            out("  пакеты Python менялись — возвращаю прежние (может занять время)...")
            d.run(install_argv(target, opts, models=False), cwd=str(target))
        out("\n" + "=" * 60 + "\n  ОБНОВЛЕНИЕ НЕ УСТАНОВЛЕНО — работает прежняя версия\n" + "=" * 60)
        out("Сообщите продавцу программы, что было написано выше. Данные магазина не изменились.")
        restart()
        return 1

    out("\n[4/4] Готово")
    prune_backups(target)
    out("=" * 60 + f"\n  ПРОГРАММА ОБНОВЛЕНА: версия {describe_version(read_version(target))}\n" + "=" * 60)
    out("Товары, фото, сотрудники и настройки на месте. Распакованную папку обновления можно удалить.")
    if was_running:
        restart()
    else:
        out("Запустите программу как обычно — ярлык «Узнать товар» на рабочем столе.")
    return 0


def main(argv: list[str] | None = None, deps: Deps | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", type=Path, help="папка установленной программы (если ярлыка нет)")
    ap.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    ap.add_argument("--force", action="store_true", help="ставить, даже если эта версия уже стоит")
    ap.add_argument("--cpu", action="store_true", help="пакеты без видеокарты (как Установить.bat --cpu)")
    ap.add_argument("--no-models", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--check-timeout", type=float, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--source", type=Path, default=ROOT, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    return run(Options(source=a.source, target=a.target, yes=a.yes, force=a.force, cpu=a.cpu, models=not a.no_models,
                       check_timeout=a.check_timeout), deps)


if __name__ == "__main__":
    raise SystemExit(main())
