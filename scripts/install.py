"""
Установка «Узнать товар» в магазине — двойной щелчок по «Установить.bat» в папке программы (этап 7 генерального плана).

    Установить.bat                      # всё по шагам; повторный запуск продолжает с того, что не сделано
    Установить.bat --cpu                # пакеты без видеокарты (меньше скачивать), даже если она есть
    Установить.bat --no-autostart       # не запускать программу при входе в Windows
    Установить.bat --remove-autostart   # убрать автозапуск и выйти
    Установить.bat --no-shortcut --no-check

Шаги:
  1. Проверка компьютера: Windows 64 бит, память, процессор, видеокарта NVIDIA и её драйвер, место на диске. По ним — что
     включится (app/core/hardware.py, те же правила, что при запуске).
  2. Своя копия Python программы (.venv) и пакеты. PyTorch — под видеокарту (CUDA по версии драйвера) или без неё.
     Пакеты не кэшируются на системном диске (--no-cache-dir).
  3. Файл настроек .env: ключи шифрования создаются, если пустые; заполненное не меняется.
  4. Модели распознавания — в папку программы models/hf (scripts/fetch_models.py), чтобы магазин работал без интернета. Если
     компьютер тянет модель «зрение + текст» — ещё и её с её движком в models/vision (scripts/vision_engine.py, около 3,1 ГБ;
     с видеокартой NVIDIA — ещё ~0,6 ГБ быстрый движок CUDA, остальным хватает Vulkan):
     программа запускает её сама, LM Studio и другие программы не нужны.
  5. Ярлык «Узнать товар» на рабочем столе и автозапуск при входе в Windows (окно свёрнуто, без браузера). Запуск —
     облегчённый режим (start_lite.bat: без Docker, база в data/lite): он сам подстраивается под компьютер при каждом запуске.
  6. Пробный запуск на свободном порту: программа поднялась, страница покупателя открывается, распознавание отвечает.

Этот файл выполняет системный Python (только стандартная библиотека): .venv ещё может не быть. Внешний мир (команды, диск,
сеть) подаётся через Deps — логика проверяется тестами без установки (tests/test_install.py).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.core import hardware  # noqa: E402 — только стандартная библиотека
from scripts import make_shortcut, vision_engine  # noqa: E402

MIN_PYTHON = (3, 12)
MIN_FREE_GB = 10.0                 # пакеты без видеокарты ≈ 2 ГБ, модели ≈ 0,8 ГБ, база и фото товаров
MIN_FREE_GB_GPU = 14.0             # PyTorch с CUDA ≈ 4–5 ГБ
TORCH_INDEX = "https://download.pytorch.org/whl/"
# Драйвер NVIDIA -> сборка PyTorch (таблица совместимости CUDA): чем новее драйвер, тем новее CUDA. Видеокарты RTX 50xx
# (Blackwell) работают только с CUDA 12.8 и новее.
TORCH_BY_DRIVER = [(580, "cu130"), (570, "cu128"), (560, "cu126")]
AUTOSTART_NAME = "Узнать товар (автозапуск)"
STATE_FILE = Path("data") / "install_state.json"
CHECK_TIMEOUT = 300.0              # первый запуск грузит модели: до нескольких минут на слабом компьютере
OK, WARN, FAIL = "OK", "ВНИМАНИЕ", "НЕЛЬЗЯ"


# ------------------------------------------------------------------ правила (чистые функции)


def driver_major(version: str | None) -> int | None:
    try:
        return int(str(version).strip().split(".")[0])
    except (TypeError, ValueError):
        return None


def torch_variant(gpu_name: str | None, driver: str | None, force_cpu: bool = False) -> tuple[str, str]:
    """(сборка PyTorch, пояснение). cpu — без видеокарты; cuXYZ — с CUDA, подходящей к драйверу."""
    if force_cpu:
        return "cpu", "выбрано «без видеокарты» (--cpu)"
    if not gpu_name:
        return "cpu", "видеокарты NVIDIA нет — всё работает на процессоре"
    major = driver_major(driver)
    if major is None:
        return "cpu", "версия драйвера видеокарты не определилась — ставлю без видеокарты"
    blackwell = "RTX 50" in gpu_name.upper() or "BLACKWELL" in gpu_name.upper()
    for need, tag in TORCH_BY_DRIVER:
        if major >= need:
            if blackwell and tag == "cu126":
                break
            return tag, f"видеокарта {gpu_name}, драйвер {driver}"
    return "cpu", (f"драйвер видеокарты {driver} устарел — ставлю без видеокарты. Обновите драйвер NVIDIA и запустите "
                   "установку ещё раз, чтобы включить видеокарту")


def check_computer(hw: "hardware.Hardware", *, free_gb: float, windows: tuple[int, int] | None, is_64bit: bool,
                   need_gb: float) -> list[tuple[str, str]]:
    """Пункты проверки (уровень, текст). FAIL — устанавливать нельзя."""
    items: list[tuple[str, str]] = []
    if windows is None:
        items.append((WARN, "это не Windows: установщик рассчитан на Windows 10 и 11"))
    elif windows[0] < 10:
        items.append((FAIL, "нужна Windows 10 или 11"))
    else:
        items.append((OK, f"Windows {11 if windows[1] >= 22000 else 10}"))
    items.append((OK, "64 бит") if is_64bit else (FAIL, "нужна 64-битная Windows"))
    if not hw.ram_gb:
        items.append((WARN, "объём памяти не определился"))
    elif hw.ram_gb < hardware.MIN_RAM_GB:
        items.append((FAIL, f"памяти {hw.ram_gb:.0f} ГБ — нужно не меньше 8 ГБ"))
    elif hw.ram_gb < 7.5:
        items.append((WARN, f"памяти {hw.ram_gb:.0f} ГБ — будет медленно, лучше 8 ГБ и больше"))
    else:
        items.append((OK, f"память {hw.ram_gb:.0f} ГБ"))
    items.append((OK, f"процессор: {hw.cores} потоков") if hw.cores >= 4 else
                 (WARN, f"процессор: {hw.cores} потока — будет медленно, лучше 4 и больше"))
    if hw.gpu_name:
        items.append((OK, f"видеокарта {hw.gpu_name}" + (f", {hw.vram_gb:.0f} ГБ" if hw.vram_gb else "")))
    else:
        items.append((OK, "видеокарты NVIDIA нет — не страшно, товары из базы узнаются и на процессоре"))
    if free_gb < need_gb:
        items.append((FAIL, f"на диске свободно {free_gb:.0f} ГБ — нужно не меньше {need_gb:.0f} ГБ"))
    else:
        items.append((OK, f"на диске свободно {free_gb:.0f} ГБ"))
    return items


def what_will_work(plan: "hardware.LitePlan", hw: "hardware.Hardware") -> list[str]:
    lines = ["товар из базы магазина (по штрихкоду и по виду) — да",
             "товар не из базы по штрихкоду (открытые базы в интернете) — да",
             "вид любого товара («это напиток», «это обувь») — да"]
    if plan.vision:
        lines.append("название товара не из базы по фото — да (модель скачается сейчас, около "
                     f"{vision_engine.DOWNLOAD_GB:.1f} ГБ, с NVIDIA ещё {vision_engine.CUDA_DOWNLOAD_GB:.1f} ГБ движка CUDA; "
                     "программа запускает её сама)")
    else:
        lines.append("название товара не из базы по фото — нет (нужна видеокарта NVIDIA от 6 ГБ и память от 12 ГБ)")
    lines.append("распознавание по виду на " + ("видеокарте" if plan.device == "auto" and hw.gpu_name else "процессоре"))
    return lines


def new_secret() -> str:
    """Ключ того же вида, что Fernet.generate_key() (app/core/crypto.py): 32 случайных байта в base64url."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def fill_env(text: str, keys: tuple[str, ...] = ("AUTH_SECRET_KEY", "CAMERA_SECRET_KEY"),
             gen: Callable[[], str] = new_secret) -> tuple[str, list[str]]:
    """Заполнить ПУСТЫЕ ключи в тексте .env (нет строки — добавить). Заполненные не трогаются. -> (текст, какие ключи созданы)."""
    lines = text.splitlines()
    made: list[str] = []
    for key in keys:
        idx = next((i for i, ln in enumerate(lines) if ln.strip().startswith(key + "=")), None)
        if idx is None:
            lines.append(f"{key}={gen()}")
            made.append(key)
        elif not lines[idx].split("=", 1)[1].strip():
            lines[idx] = f"{key}={gen()}"
            made.append(key)
    return "\n".join(lines) + "\n", made


def requirements_digest(root: Path) -> str:
    h = hashlib.sha256()
    for name in ("requirements.txt", "constraints-opencv.txt"):
        p = root / name
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def venv_python(root: Path) -> Path:
    return root / ".venv" / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")


# ------------------------------------------------------------------ внешний мир (подменяется в тестах)


def _driver_version(runner=subprocess.run, which=shutil.which) -> str | None:
    exe = which("nvidia-smi")
    if not exe:
        return None
    try:
        res = runner([exe, "--query-gpu=driver_version", "--format=csv,noheader"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    line = (res.stdout or "").strip().splitlines()
    return line[0].strip() if res.returncode == 0 and line else None


def _windows_version() -> tuple[int, int] | None:
    if not sys.platform.startswith("win"):
        return None
    v = sys.getwindowsversion()
    return v.major, v.build


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http(url: str, data: bytes | None = None, ctype: str = "", timeout: float = 30.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                 headers={"Content-Type": ctype} if ctype else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — только localhost
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0, b""


def _kill_tree(proc) -> None:
    if proc.poll() is not None:
        return
    if sys.platform.startswith("win"):
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()


@dataclass
class Deps:
    run: Callable = subprocess.run
    popen: Callable = subprocess.Popen
    which: Callable = shutil.which
    out: Callable[[str], None] = lambda s: print(s, flush=True)   # сразу: вывод pip идёт в то же окно
    probe_hw: Callable = hardware.probe
    driver: Callable[[], str | None] = _driver_version
    windows: Callable[[], tuple[int, int] | None] = _windows_version
    is_64bit: bool = sys.maxsize > 2**32
    free_gb: Callable[[Path], float] = lambda p: shutil.disk_usage(p).free / 2**30
    free_port: Callable[[], int] = _free_port
    fetch_vision: Callable = vision_engine.fetch
    http: Callable = _http
    kill_tree: Callable = _kill_tree
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    python: str = sys.executable
    version: tuple = tuple(sys.version_info[:2])


@dataclass
class Options:
    root: Path = ROOT
    cpu: bool = False
    autostart: bool = True
    shortcut: bool = True
    check: bool = True
    models: bool = True
    remove_autostart: bool = False
    shortcut_folder: str | Path = "Desktop"       # для проверки установщика — временная папка
    autostart_folder: str | Path = "Startup"
    check_timeout: float = CHECK_TIMEOUT


@dataclass
class Report:
    done: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ шаги


def _read_state(root: Path) -> dict:
    try:
        return json.loads((root / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(root: Path, state: dict) -> None:
    p = root / STATE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _step(d: Deps, n: int, title: str) -> None:
    d.out(f"\n[{n}/6] {title}")


def _ok(res) -> bool:
    return getattr(res, "returncode", 1) == 0


def step_packages(opts: Options, d: Deps, variant: str, rep: Report) -> bool:
    root = opts.root
    vpy = venv_python(root)
    if not vpy.is_file():
        d.out("  создаю свою копию Python программы (папка .venv)...")
        if not _ok(d.run([d.python, "-m", "venv", str(root / ".venv")], cwd=str(root))):
            d.out("  не удалось создать .venv — переустановите Python (python.org) и запустите установку снова")
            return False
    state = _read_state(root)
    digest = requirements_digest(root)
    have = d.run([str(vpy), "-c", "import torch, fastapi, transformers, faiss; print(torch.__version__)"],
                 cwd=str(root), capture_output=True, text=True)
    installed = (getattr(have, "stdout", "") or "").strip()
    want_suffix = "+cpu" if variant == "cpu" else "+" + variant
    torch_ok = _ok(have) and (installed.endswith(want_suffix) or (variant == "cpu" and "+" not in installed))
    if torch_ok and state.get("requirements") == digest and state.get("torch") == variant:
        d.out(f"  пакеты уже установлены (PyTorch {installed})")
        return True
    pip = [str(vpy), "-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check"]
    if not torch_ok:
        if installed:                     # стоит другая сборка (например, без видеокарты, а видеокарта есть): заменить
            d.run([str(vpy), "-m", "pip", "uninstall", "-y", "torch", "torchvision"], cwd=str(root))
        d.out(f"  PyTorch ({'без видеокарты' if variant == 'cpu' else 'для видеокарты, ' + variant}) — это самая большая "
              "часть, может занять 5–30 минут...")
        res = d.run(pip + ["torch", "torchvision", "--index-url", TORCH_INDEX + variant], cwd=str(root))
        if not _ok(res):
            d.out("  PyTorch не установился. Проверьте интернет и запустите установку ещё раз — она продолжит с этого места.")
            return False
    d.out("  остальные пакеты...")
    res = d.run(pip + ["-r", "requirements.txt", "-c", "constraints-opencv.txt"], cwd=str(root))
    if not _ok(res):
        d.out("  пакеты не установились. Проверьте интернет и запустите установку ещё раз.")
        return False
    _write_state(root, {**state, "requirements": digest, "torch": variant})
    rep.done.append("пакеты программы")
    return True


def step_env(opts: Options, d: Deps, rep: Report) -> None:
    env, example = opts.root / ".env", opts.root / ".env.example"
    text = env.read_text(encoding="utf-8") if env.is_file() else (example.read_text(encoding="utf-8")
                                                                  if example.is_file() else "")
    new, made = fill_env(text)
    if made or not env.is_file():
        env.write_text(new, encoding="utf-8")
    d.out("  ключи шифрования созданы" if made else "  настройки на месте, ничего не изменено")
    if made:
        rep.warnings.append("В файле .env созданы ключи шифрования. Сохраните копию .env отдельно (флешка): без неё "
                            "резервную копию базы не восстановить полностью.")


def step_models(opts: Options, d: Deps, rep: Report, vision: bool = False) -> None:
    env = {**os.environ, "HF_HOME": str(opts.root / "models" / "hf")}
    res = d.run([str(venv_python(opts.root)), "-m", "scripts.fetch_models"], cwd=str(opts.root), env=env)
    if _ok(res):
        rep.done.append("модели распознавания (models/hf)")
    else:
        rep.warnings.append("Модели не скачались (нет интернета?). Программа скачает их сама при первом запуске с интернетом.")
    if not vision:
        return
    d.out(f"  модель «зрение + текст» и её движок (около {vision_engine.DOWNLOAD_GB:.1f} ГБ, с NVIDIA ещё "
          f"{vision_engine.CUDA_DOWNLOAD_GB:.1f} ГБ, в models/vision) — "
          "при медленном интернете это долго; оборвётся — следующий запуск установки докачает:")
    if d.fetch_vision(opts.root, d.out):
        rep.done.append("модель «зрение + текст» (models/vision): название товара не из базы по фото")
    else:
        rep.warnings.append("Модель «зрение + текст» не скачалась целиком. Запустите «Установить.bat» ещё раз — докачается. "
                            "Без неё товар не из базы называется только по штрихкоду.")


def step_shortcuts(opts: Options, d: Deps, rep: Report) -> None:
    root = opts.root
    target, icon = root / "start_lite.bat", root / "frontend" / "icons" / "app.ico"
    if opts.shortcut:
        res = make_shortcut.run_ps(make_shortcut.build_ps_script(
            target=target, workdir=root, icon=icon, folder=opts.shortcut_folder,
            description="Узнать товар: страница покупателя и управление магазином"), run=d.run)
        if _ok(res):
            rep.done.append("ярлык «Узнать товар» на рабочем столе")
        else:
            rep.warnings.append("Ярлык не создался: " + ((res.stderr or res.stdout or "").strip()[:300]))
    if opts.autostart:
        res = make_shortcut.run_ps(make_shortcut.build_ps_script(
            target=target, workdir=root, icon=icon, name=AUTOSTART_NAME, folder=opts.autostart_folder,
            arguments="--no-browser", window_style=make_shortcut.WINDOW_MINIMIZED,
            description="Запуск «Узнать товар» при входе в Windows"), run=d.run)
        if _ok(res):
            rep.done.append("автозапуск при входе в Windows (убрать: Установить.bat --remove-autostart)")
        else:
            rep.warnings.append("Автозапуск не настроился: " + ((res.stderr or res.stdout or "").strip()[:300]))
    if not opts.shortcut and not opts.autostart:
        d.out("  пропущено")


def remove_autostart(opts: Options, d: Deps) -> int:
    res = make_shortcut.run_ps(make_shortcut.build_remove_script(name=AUTOSTART_NAME, folder=opts.autostart_folder), run=d.run)
    if not _ok(res):
        d.out("Не удалось убрать автозапуск: " + (res.stderr or res.stdout or "").strip())
        return 1
    d.out("Автозапуск убран." if (res.stdout or "").strip() else "Автозапуска не было.")
    return 0


def step_check(opts: Options, d: Deps, rep: Report) -> bool:
    """Пробный запуск облегчённого режима на свободном порту: готов, страница покупателя, ответ распознавания."""
    root = opts.root
    port = d.free_port()
    log = root / "logs" / "install_check.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    d.out(f"  запускаю программу на время проверки (порт {port}; первый запуск грузит модели — до нескольких минут)...")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    with open(log, "w", encoding="utf-8", errors="replace") as fh:
        proc = d.popen([str(venv_python(root)), "-m", "scripts.start_demo", "--lite", "--no-browser", "--public", "off",
                        "--port", str(port), "--timeout", str(int(opts.check_timeout))],
                       cwd=str(root), env=env, stdout=fh, stderr=subprocess.STDOUT)
        try:
            return _probe(opts, d, port, proc, log, rep)
        finally:
            d.kill_tree(proc)


def _probe(opts: Options, d: Deps, port: int, proc, log: Path, rep: Report) -> bool:
    base = f"http://127.0.0.1:{port}"
    deadline = d.clock() + opts.check_timeout
    image = (opts.root / "frontend" / "icons" / "icon-192.png").read_bytes()
    stage = "start"
    while d.clock() < deadline:
        if proc.poll() is not None:
            break
        if stage == "start":
            if d.http(base + "/health/ready", timeout=5)[0] == 200:
                stage = "page"
        if stage == "page":
            if d.http(base + "/ui/shop.html", timeout=10)[0] != 200:
                d.out("  страница покупателя не открылась")
                break
            stage = "identify"
        if stage == "identify":
            code, body = d.http(base + "/api/v1/public/identify", data=image, ctype="image/png", timeout=120)
            if code == 200:
                try:
                    status = json.loads(body.decode("utf-8")).get("status")
                except ValueError:
                    status = None
                if status:
                    d.out("  программа запустилась, страница покупателя открывается, распознавание отвечает")
                    rep.done.append("пробный запуск")
                    return True
        d.sleep(2.0)
    tail = ""
    try:
        tail = "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-15:])
    except OSError:
        pass
    d.out("  пробный запуск не удался" + (" (программа остановилась)" if proc.poll() is not None else " (не дождался)")
          + f". Журнал: {log}\n" + tail)
    return False


def run(opts: Options, d: Deps | None = None) -> int:
    d = d or Deps()
    out = d.out
    if opts.remove_autostart:
        return remove_autostart(opts, d)
    rep = Report()
    out("Установка «Узнать товар»")
    if d.version < MIN_PYTHON:
        out(f"Нужен Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} или новее (сейчас {d.version[0]}.{d.version[1]}). "
            "Установите с python.org и запустите установку снова.")
        return 1

    _step(d, 1, "Проверка компьютера")
    try:
        hw = d.probe_hw()
    except Exception:            # noqa: BLE001
        hw = hardware.Hardware(ram_gb=0.0, cores=os.cpu_count() or 1)
    driver = d.driver() if hw.gpu_name else None
    variant, why = torch_variant(hw.gpu_name, driver, opts.cpu)
    need = MIN_FREE_GB if variant == "cpu" else MIN_FREE_GB_GPU
    plan = hardware.plan_lite(hw, force_cpu=variant == "cpu")
    if plan.vision:             # модель «зрение + текст» только на NVIDIA; CUDA — при драйвере от 580
        major = driver_major(driver)
        need += vision_engine.disk_need_gb(opts.root, cuda=major is not None and major >= vision_engine.CUDA_MIN_DRIVER)
    items = check_computer(hw, free_gb=d.free_gb(opts.root), windows=d.windows(), is_64bit=d.is_64bit, need_gb=need)
    for level, text in items:
        out(f"  [{level}] {text}")
    if any(level == FAIL for level, _ in items):
        out("\nНа этом компьютере программу установить нельзя (см. «НЕЛЬЗЯ» выше).")
        return 1
    out(f"  PyTorch: {'без видеокарты' if variant == 'cpu' else variant} — {why}")
    out("  Что будет работать:")
    for line in what_will_work(plan, hw):
        out("    - " + line)

    _step(d, 2, "Python программы и пакеты")
    if not step_packages(opts, d, variant, rep):
        return 1

    _step(d, 3, "Настройки (.env)")
    step_env(opts, d, rep)

    _step(d, 4, "Модели распознавания")
    if opts.models:
        step_models(opts, d, rep, vision=plan.vision)
    else:
        out("  пропущено")

    _step(d, 5, "Ярлык и автозапуск")
    step_shortcuts(opts, d, rep)

    _step(d, 6, "Пробный запуск")
    checked = step_check(opts, d, rep) if opts.check else (out("  пропущено") or True)

    out("\n" + "=" * 60)
    out("  УСТАНОВКА ЗАВЕРШЕНА" if checked else "  УСТАНОВКА ЗАВЕРШЕНА, НО ПРОБНЫЙ ЗАПУСК НЕ ПРОШЁЛ")
    out("=" * 60)
    for item in rep.done:
        out("  сделано: " + item)
    for w in rep.warnings:
        out("  " + w)
    out("Запуск: ярлык «Узнать товар» на рабочем столе (или start_lite.bat в папке программы). При первом запуске откроется "
        "страница «Придумайте ПИН» для владельца магазина.")
    return 0 if checked else 1


def main(argv: list[str] | None = None, deps: Deps | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cpu", action="store_true", help="пакеты без видеокарты, даже если она есть")
    ap.add_argument("--no-autostart", action="store_true", help="не запускать программу при входе в Windows")
    ap.add_argument("--remove-autostart", action="store_true", help="убрать автозапуск и выйти")
    ap.add_argument("--no-shortcut", action="store_true", help="не создавать ярлык на рабочем столе")
    ap.add_argument("--no-models", action="store_true", help="не скачивать модели заранее")
    ap.add_argument("--no-check", action="store_true", help="без пробного запуска")
    ap.add_argument("--check-timeout", type=float, default=CHECK_TIMEOUT)
    a = ap.parse_args(argv)
    return run(Options(cpu=a.cpu, autostart=not a.no_autostart, shortcut=not a.no_shortcut, check=not a.no_check,
                       models=not a.no_models, remove_autostart=a.remove_autostart, check_timeout=a.check_timeout), deps)


if __name__ == "__main__":
    raise SystemExit(main())
