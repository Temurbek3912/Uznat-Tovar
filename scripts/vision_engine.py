"""
Свой движок модели «зрение + текст» (документ 07, §10.6): программа запускает его сама, отдельная программа (LM Studio) не нужна.

    python -m scripts.vision_engine            # скачать движок и модель в models/vision (установщик и «Обновить.bat» делают это сами)
    python -m scripts.vision_engine --check    # всё ли на месте (код 0 — да)

Что это. Движок — llama.cpp (`llama-server.exe`, сборка Vulkan: одна сборка для видеокарт NVIDIA, AMD, Intel, без CUDA, 37,5 МБ
архив; без видеокарты считает на процессоре). Модель — Qwen3-VL-4B (та же, что была в LM Studio: те же файлы GGUF, 2,5 ГБ + 0,8 ГБ).
Движок отвечает на том же OpenAI-совместимом входе, что LM Studio (app/services/product_namer.py не меняется), только на своём
свободном порту и только на 127.0.0.1 — кадр с этого компьютера не уходит.

Скачивание: с GitHub (движок, закреплённая версия) и Hugging Face (модель); каждый файл проверяется по SHA-256; оборвалось —
следующий запуск докачивает с того же места (файл .part). Если на компьютере уже есть эти файлы модели от LM Studio — копируются
оттуда, без интернета. Только стандартная библиотека: работает и из системного Python установщика.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
VISION_DIR = Path("models") / "vision"
ENGINE_TAG = "b12022"           # llama.cpp от 2026-10-31: проверен с Qwen3-VL-4B на RTX 5060 (документ 11, §10)
ENGINE_DIR_NAME = f"llama-{ENGINE_TAG}"
MODEL_NAME = "qwen3-vl-4b"
CTX_SIZE = 4096                 # снимок (≤ 1024 px, около 1000 токенов) + подсказка + ответ
CHUNK = 1 << 20


@dataclass(frozen=True)
class Part:
    name: str
    url: str
    sha256: str
    size: int


ENGINE = Part(f"llama-{ENGINE_TAG}-bin-win-vulkan-x64.zip",
              f"https://github.com/ggml-org/llama.cpp/releases/download/{ENGINE_TAG}/llama-{ENGINE_TAG}-bin-win-vulkan-x64.zip",
              "d77921a0084e2d326cd78d0a1c9a166958e2678a370c4ff0504c9bf8bbfede56", 37515510)
_HF = "https://huggingface.co/lmstudio-community/Qwen3-VL-4B-Instruct-GGUF/resolve/main/"
MODEL = Part("Qwen3-VL-4B-Instruct-Q4_K_M.gguf", _HF + "Qwen3-VL-4B-Instruct-Q4_K_M.gguf",
             "aa18af1fdda59081005319536eebff9406d556ce8a753d9a3aa81cf635eadde7", 2497281568)
MMPROJ = Part("mmproj-Qwen3-VL-4B-Instruct-F16.gguf", _HF + "mmproj-Qwen3-VL-4B-Instruct-F16.gguf",
              "1b70e983d69dd424eb16e0ed2104d0b2a58bf01d866657f4de5da6c963620f19", 836180160)
DOWNLOAD_GB = (ENGINE.size + MODEL.size + MMPROJ.size) / 2**30      # ≈ 3,1 ГБ; на диске ≈ 3,3 ГБ (движок распакован)
DISK_GB = 4.0


def vision_dir(root: Path) -> Path:
    return root / VISION_DIR


def engine_exe(root: Path) -> Path:
    return vision_dir(root) / ENGINE_DIR_NAME / "llama-server.exe"


def model_path(root: Path, part: Part) -> Path:
    return vision_dir(root) / part.name


def is_installed(root: Path) -> bool:
    """Всё на месте (размер — быстрая проверка при каждом запуске; SHA-256 проверяется при скачивании)."""
    if not engine_exe(root).is_file():
        return False
    for part in (MODEL, MMPROJ):
        p = model_path(root, part)
        if not p.is_file() or p.stat().st_size != part.size:
            return False
    return True


def lmstudio_copy(part: Part, home: Path | None = None) -> Path | None:
    """Тот же файл модели, уже скачанный LM Studio (обычные места её папки моделей), или None."""
    home = home or Path.home()
    for base in (home / ".lmstudio" / "models", home / ".cache" / "lm-studio" / "models"):
        p = base / "lmstudio-community" / "Qwen3-VL-4B-Instruct-GGUF" / part.name
        if p.is_file() and p.stat().st_size == part.size:
            return p
    return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _progress(out: Callable[[str], None], name: str):
    shown = {"step": -1}

    def tick(done: int, total: int) -> None:
        step = int(done * 10 / total) if total else 0       # каждые 10 %
        if step != shown["step"]:
            shown["step"] = step
            out(f"    {name}: {done / 2**20:.0f} из {total / 2**20:.0f} МБ")
    return tick


def http_download(url: str, part_file: Path, total: int, tick: Callable[[int, int], None], timeout: float = 60.0) -> None:
    """Докачать url в part_file с того места, где остановились (заголовок Range)."""
    have = part_file.stat().st_size if part_file.is_file() else 0
    if have >= total:
        return
    req = urllib.request.Request(url, headers={"User-Agent": "uznat-tovar-installer", **({"Range": f"bytes={have}-"} if have else {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:            # noqa: S310 — адреса закреплены выше
        mode = "ab" if have and resp.status == 206 else "wb"
        done = have if mode == "ab" else 0
        with open(part_file, mode) as f:
            while True:
                block = resp.read(CHUNK)
                if not block:
                    break
                f.write(block)
                done += len(block)
                tick(done, total)


def fetch_part(part: Part, dest: Path, out: Callable[[str], None], *, download=None,
               local: Path | None = None) -> bool:
    """Файл в dest с проверкой SHA-256. True — готово."""
    if dest.is_file() and dest.stat().st_size == part.size:
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    download = download or http_download
    if local is not None:
        out(f"  {part.name}: беру уже скачанный LM Studio ({local.parent})")
        shutil.copyfile(local, tmp)
    else:
        out(f"  {part.name}: скачиваю {part.size / 2**20:.0f} МБ...")
        for attempt in range(3):
            try:
                download(part.url, tmp, part.size, _progress(out, part.name))
                break
            except OSError as exc:      # обрыв связи: докачать
                out(f"    связь прервалась ({exc.__class__.__name__}), попытка {attempt + 2} из 3...")
        else:
            out("    не скачалось. Запустите установку ещё раз — докачается с того же места.")
            return False
    if not tmp.is_file() or sha256_file(tmp) != part.sha256:
        out(f"    {part.name}: файл повреждён, удаляю — при следующем запуске скачается заново")
        tmp.unlink(missing_ok=True)
        return False
    os.replace(tmp, dest)
    return True


def unpack_engine(zip_path: Path, target: Path) -> None:
    """Только сам движок: llama-server.exe и его библиотеки (остальные программы архива не нужны)."""
    tmp = target.with_name(target.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = Path(info.filename).name
            if info.is_dir() or not (name == "llama-server.exe" or name.lower().endswith(".dll") or name.startswith("LICENSE")):
                continue
            with z.open(info) as src, open(tmp / name, "wb") as dst:
                shutil.copyfileobj(src, dst)
    shutil.rmtree(target, ignore_errors=True)
    os.replace(tmp, target)


def fetch(root: Path, out: Callable[[str], None] = print, *, download=None, home: Path | None = None,
          platform: str = sys.platform) -> bool:
    """Движок и модель в models/vision. True — всё на месте."""
    if not platform.startswith("win"):
        out("  свой движок модели «зрение + текст» есть только для Windows — пропускаю")
        return False
    if is_installed(root):
        out("  движок и модель «зрение + текст» уже на месте")
        return True
    vdir = vision_dir(root)
    ok = True
    if not engine_exe(root).is_file():
        zip_path = vdir / ENGINE.name
        if fetch_part(ENGINE, zip_path, out, download=download):
            unpack_engine(zip_path, vdir / ENGINE_DIR_NAME)
            zip_path.unlink(missing_ok=True)
        else:
            ok = False
    for part in (MODEL, MMPROJ):
        ok = fetch_part(part, model_path(root, part), out, download=download, local=lmstudio_copy(part, home)) and ok
    for old in vdir.glob("llama-b*"):          # прежние версии движка после обновления
        if old.is_dir() and old.name != ENGINE_DIR_NAME:
            shutil.rmtree(old, ignore_errors=True)
    return ok and is_installed(root)


def pick_device(exe: Path, gpu_name: str | None, run=subprocess.run) -> str:
    """Устройство для -dev: видеокарта с этим названием (на ноутбуке первой в списке стоит встроенная Intel — она медленнее и
    отнимает общую память) или "none" (только процессор)."""
    if not gpu_name:
        return "none"
    try:
        res = run([str(exe), "--list-devices"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        text = (res.stdout or "") + (res.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return "none"
    for line in text.splitlines():
        head, _, rest = line.strip().partition(":")
        if head.startswith("Vulkan") and gpu_name.lower() in rest.lower():
            return head
    return "none"


def server_argv(root: Path, port: int, device: str) -> list[str]:
    gpu = device != "none"
    return [str(engine_exe(root)), "-m", str(model_path(root, MODEL)), "--mmproj", str(model_path(root, MMPROJ)),
            "--host", "127.0.0.1", "--port", str(port), "--alias", MODEL_NAME, "-c", str(CTX_SIZE), "-np", "1",
            "-dev", device, "-ngl", "99" if gpu else "0", *([] if gpu else ["--no-mmproj-offload"]), "--no-ui"]


_JOBS: list = []                # задание Windows живёт, пока жива программа


def tie_to_this_process(proc, platform: str = sys.platform) -> bool:
    """Движок закрывается вместе с программой, даже если её окно закрыли крестиком или она упала (задание Windows с
    «закрыть всё при закрытии»): иначе он остался бы висеть и занимать видеопамять, а следующий запуск поднял бы второй."""
    if not platform.startswith("win"):
        return False
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.OpenProcess.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return False
    info = ExtendedLimits()
    info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info)):
        return False
    handle = kernel32.OpenProcess(0x0101, False, int(proc.pid))  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
    if not handle:
        return False
    try:
        ok = bool(kernel32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(handle)))
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))
    if ok:
        _JOBS.append(job)
    return ok


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="Свой движок модели «зрение + текст» (без LM Studio)")
    ap.add_argument("--check", action="store_true", help="только проверить, всё ли на месте")
    a = ap.parse_args(argv)
    if a.check:
        ok = is_installed(ROOT)
        print("движок и модель на месте" if ok else "движка или модели нет — python -m scripts.vision_engine")
        return 0 if ok else 1
    print(f"Движок и модель «зрение + текст» (около {DOWNLOAD_GB:.1f} ГБ, в папку {VISION_DIR}):", flush=True)
    return 0 if fetch(ROOT, lambda s: print(s, flush=True)) else 1


if __name__ == "__main__":
    sys.exit(main())
