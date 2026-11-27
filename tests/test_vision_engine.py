"""Свой движок модели «зрение + текст» вместо LM Studio (scripts/vision_engine.py, scripts/start_demo.py). Без сети и без
настоящей модели: файлы заменены маленькими с теми же проверками."""
from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from app.core import hardware
from scripts import start_demo as sd
from scripts import vision_engine as ve
from tests.test_start_demo import Env

REAL_DOWNLOAD = ve.http_download          # до запрета скачивания в conftest: проверяется с подменённым urlopen
REAL_NVIDIA_INFO = ve.nvidia_info      # то же: в conftest подменён (видеокарта этого компьютера не влияет на тесты)


def _part(name: str, data: bytes) -> ve.Part:
    return ve.Part(name, "https://example.invalid/" + name, hashlib.sha256(data).hexdigest(), len(data))


def _engine_zip() -> bytes:
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("llama-server.exe", b"exe")
        z.writestr("ggml-vulkan.dll", b"dll")
        z.writestr("LICENSE-curl", b"lic")
        z.writestr("llama-cli.exe", b"not needed")
    return buf.getvalue()


@pytest.fixture
def small(monkeypatch):
    """Маленькие «модель», «проектор» и «архив движка»; скачивание — из словаря, со счётчиком."""
    blobs = {"model.gguf": b"model-bytes" * 10, "mmproj.gguf": b"mmproj", "engine.zip": _engine_zip()}
    parts = {k: _part(k, v) for k, v in blobs.items()}
    monkeypatch.setattr(ve, "MODEL", parts["model.gguf"])
    monkeypatch.setattr(ve, "MMPROJ", parts["mmproj.gguf"])
    monkeypatch.setattr(ve, "ENGINE", parts["engine.zip"])
    calls = []

    def download(url, part_file, total, tick):
        name = url.rsplit("/", 1)[-1]
        calls.append(name)
        part_file.write_bytes(blobs[name])
        tick(total, total)
    return NS(blobs=blobs, parts=parts, calls=calls, download=download)


def test_fetch_downloads_checks_and_unpacks_only_the_server(tmp_path, small):
    lines = []
    assert not ve.is_installed(tmp_path)
    assert ve.fetch(tmp_path, lines.append, download=small.download, home=tmp_path / "nohome", platform="win32")
    assert ve.is_installed(tmp_path)
    engine = ve.engine_exe(tmp_path).parent
    assert sorted(p.name for p in engine.iterdir()) == ["LICENSE-curl", "ggml-vulkan.dll", "llama-server.exe"]
    assert not list(ve.vision_dir(tmp_path).glob("*.zip")) and not list(ve.vision_dir(tmp_path).glob("*.part"))
    small.calls.clear()
    assert ve.fetch(tmp_path, lines.append, download=small.download, platform="win32")          # второй раз — ничего не качает
    assert small.calls == [] and "уже на месте" in lines[-1]


def test_a_damaged_download_is_removed_and_reported(tmp_path, small):
    lines = []

    def broken(url, part_file, total, tick):
        part_file.write_bytes(b"x" * total)
    assert not ve.fetch(tmp_path, lines.append, download=broken, home=tmp_path / "nohome", platform="win32")
    assert not ve.is_installed(tmp_path) and not list(ve.vision_dir(tmp_path).glob("*.part"))
    assert any("повреждён" in ln for ln in lines)


def test_a_broken_connection_is_retried_then_reported(tmp_path, small):
    lines, tries = [], []

    def flaky(url, part_file, total, tick):
        tries.append(url)
        if len(tries) == 1:
            raise OSError("reset")
        small.download(url, part_file, total, tick)
    assert ve.fetch(tmp_path, lines.append, download=flaky, home=tmp_path / "nohome", platform="win32")
    assert any("попытка 2" in ln for ln in lines)

    def dead(url, part_file, total, tick):
        raise OSError("no network")
    lines.clear()
    assert not ve.fetch(tmp_path / "other", lines.append, download=dead, home=tmp_path / "nohome", platform="win32")
    assert any("докачается" in ln for ln in lines)


def test_resume_keeps_the_already_downloaded_beginning(tmp_path, monkeypatch):
    """Обрыв на середине: следующий запуск просит только остаток (Range) и дописывает его."""
    data = b"0123456789"
    part_file = tmp_path / "f.part"
    part_file.write_bytes(data[:4])
    seen = {}

    class Resp:
        status = 206

        def __init__(self):
            self.left = [data[4:], b""]

        def read(self, n):
            return self.left.pop(0)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout):
        seen["range"] = req.get_header("Range")
        return Resp()
    monkeypatch.setattr(ve.urllib.request, "urlopen", urlopen)
    REAL_DOWNLOAD("https://example.invalid/f", part_file, len(data), lambda done, total: None)
    assert seen["range"] == "bytes=4-" and part_file.read_bytes() == data


def test_model_files_from_lm_studio_are_copied_instead_of_downloaded(tmp_path, small, monkeypatch):
    monkeypatch.undo()                     # снять запрет копирования из conftest, но вернуть маленькие файлы
    monkeypatch.setattr(ve, "MODEL", small.parts["model.gguf"])
    monkeypatch.setattr(ve, "MMPROJ", small.parts["mmproj.gguf"])
    monkeypatch.setattr(ve, "ENGINE", small.parts["engine.zip"])
    home = tmp_path / "home"
    lm = home / ".lmstudio" / "models" / "lmstudio-community" / "Qwen3-VL-4B-Instruct-GGUF"
    lm.mkdir(parents=True)
    (lm / "model.gguf").write_bytes(small.blobs["model.gguf"])
    (lm / "mmproj.gguf").write_bytes(small.blobs["mmproj.gguf"])
    lines = []
    root = tmp_path / "app"
    assert ve.fetch(root, lines.append, download=small.download, home=home, platform="win32", cuda=False)
    assert small.calls == ["engine.zip"] and any("LM Studio" in ln for ln in lines)
    assert (lm / "model.gguf").is_file()                                      # у LM Studio ничего не забрали


def test_other_platforms_are_skipped(tmp_path, small):
    lines = []
    assert not ve.fetch(tmp_path, lines.append, download=small.download, platform="linux")
    assert small.calls == [] and "Windows" in lines[0]


def test_device_is_the_named_gpu_not_the_built_in_intel_one(tmp_path):
    listing = ("Available devices:\n  Vulkan0: Intel(R) Graphics (18347 MiB, 17579 MiB free)\n"
               "  Vulkan1: NVIDIA GeForce RTX 5060 Laptop GPU (7899 MiB, 6414 MiB free)\n")
    run = lambda argv, **kw: NS(stdout=listing, stderr="", returncode=0)
    assert ve.pick_device(tmp_path / "x.exe", "NVIDIA GeForce RTX 5060 Laptop GPU", run=run) == "Vulkan1"
    assert ve.pick_device(tmp_path / "x.exe", "NVIDIA GeForce GTX 1060", run=run) == "none"
    assert ve.pick_device(tmp_path / "x.exe", None, run=run) == "none"

    def boom(argv, **kw):
        raise OSError("no exe")
    assert ve.pick_device(tmp_path / "x.exe", "NVIDIA GeForce RTX 5060 Laptop GPU", run=boom) == "none"


def test_server_listens_only_on_this_computer_and_uses_the_gpu_when_given(tmp_path):
    gpu = ve.server_argv(tmp_path, 51234, "Vulkan1")
    assert gpu[0].endswith("llama-server.exe") and gpu[gpu.index("--host") + 1] == "127.0.0.1"
    assert gpu[gpu.index("--port") + 1] == "51234" and gpu[gpu.index("-ngl") + 1] == "99"
    assert gpu[gpu.index("-dev") + 1] == "Vulkan1" and "--mmproj" in gpu and "--no-ui" in gpu
    cpu = ve.server_argv(tmp_path, 51234, "none")
    assert cpu[cpu.index("-ngl") + 1] == "0" and "--no-mmproj-offload" in cpu


# ----------------------------------------------------------------------- запуск вместе с программой (start_demo)

def _installed(root: Path, small) -> None:
    assert ve.fetch(root, lambda s: None, download=small.download, home=root / "nohome", platform="win32")


VISION_SETTINGS = NS(database_url="postgresql://u:p@dbhost:5432/x", queue_broker_url="redis://redishost:6379/0",
                     naming_enabled=True, naming_vision_url="http://127.0.0.1:1234/v1")


def _vision(env):
    return [(argv, kw) for _, argv, kw in env.started if argv[0].endswith("llama-server.exe")]


def test_lite_mode_starts_the_own_engine_and_points_the_program_at_it(tmp_path, small):
    _installed(tmp_path, small)
    tied = []
    env = Env(tmp_path, settings=VISION_SETTINGS, pick_engine=lambda root, gpu: (ve.engine_exe(root), "Vulkan1"), free_port=lambda: 51234,
              find_lms=lambda which: pytest.fail("LM Studio не нужна"), tie_child=tied.append)
    assert sd.run(sd.Options(no_browser=True, lite=True), env.deps) == 0
    (argv, kw), = _vision(env)
    assert tied == [env.procs["api"], env.procs["worker"]]   # и API, и движок привязаны к программе (FakeProc зовётся worker)
    assert "Vulkan1" in argv and "51234" in argv and (tmp_path / "logs" / "vision.log").exists()
    api_env = env.started[0][2]["env"]
    assert api_env["NAMING_VISION_URL"] == "http://127.0.0.1:51234/v1"
    assert "свой движок программы, на видеокарте (Vulkan)" in env.text
    assert "terminate" in env.procs["worker"].events or env.procs["worker"].returncode is not None   # остановлен с программой


def test_lite_mode_on_a_weak_computer_does_not_start_the_engine(tmp_path, small):
    _installed(tmp_path, small)
    env = Env(tmp_path, settings=VISION_SETTINGS, probe_hw=lambda: hardware.Hardware(ram_gb=8, cores=4),
              pick_engine=lambda root, gpu: pytest.fail("не запускать"))
    assert sd.run(sd.Options(no_browser=True, lite=True), env.deps) == 0
    assert _vision(env) == [] and env.started[0][2]["env"]["NAMING_VISION_URL"] == ""


def test_without_the_engine_lm_studio_is_still_used_and_a_remote_model_is_left_alone(tmp_path, small):
    env = Env(tmp_path, settings=VISION_SETTINGS, find_lms=lambda which: "C:/lms.exe")
    assert sd.run(sd.Options(no_browser=True, lite=True), env.deps) == 0
    assert _vision(env) == [] and ["C:/lms.exe", "server", "start"] in [a for a, _ in env.docker_calls]

    _installed(tmp_path, small)
    remote = NS(**{**vars(VISION_SETTINGS), "naming_vision_url": "https://example.org/v1"})
    env = Env(tmp_path, settings=remote, pick_engine=lambda root, gpu: pytest.fail("не запускать"))
    assert sd.run(sd.Options(no_browser=True, lite=True), env.deps) == 0
    assert _vision(env) == []


def test_full_mode_starts_the_own_engine_next_to_api_and_worker(tmp_path, small):
    _installed(tmp_path, small)
    env = Env(tmp_path, settings=VISION_SETTINGS, pick_engine=lambda root, gpu: (ve.engine_exe(root), "Vulkan0"), free_port=lambda: 51235)
    assert sd.run(sd.Options(no_browser=True), env.deps) == 0
    assert len(_vision(env)) == 1
    assert all(kw["env"]["NAMING_VISION_URL"] == "http://127.0.0.1:51235/v1" for _, _, kw in env.started)


def test_the_engine_dies_with_the_program_even_after_a_crash():
    """Настоящий процесс в задании Windows: родитель убит жёстко — дочерний процесс закрывается сам."""
    import subprocess
    import sys
    import time
    if not sys.platform.startswith("win"):
        pytest.skip("только Windows")
    code = ("import subprocess, sys, time; from scripts import vision_engine as ve; "
            "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "print(c.pid, ve.tie_to_this_process(c), flush=True); time.sleep(60)")
    parent = subprocess.Popen([sys.executable, "-c", code], cwd=str(Path(__file__).resolve().parent.parent),
                              stdout=subprocess.PIPE, text=True)
    try:
        pid, ok = parent.stdout.readline().split()
        assert ok == "True"
        parent.kill()                                        # как аварийный выход
        parent.wait(10)
        for _ in range(50):
            alive = str(pid) in subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True,
                                               text=True).stdout
            if not alive:
                break
            time.sleep(0.1)
        assert not alive
    finally:
        parent.kill()
        parent.stdout.close()


def test_not_windows_is_not_tied():
    assert ve.tie_to_this_process(NS(pid=1), platform="linux") is False


# ----------------------------------------------------------------------- CUDA для NVIDIA, Vulkan запасной, процессор

def _fake_exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"exe")
    return path


def _lister(cuda: str = "", vulkan: str = ""):
    """llama-server --list-devices: свой список у каждой сборки."""
    def run(argv, **kw):
        return NS(stdout=cuda if ve.CUDA_DIR_NAME in argv[0] else vulkan, stderr="", returncode=0)
    return run


RTX = "NVIDIA GeForce RTX 5060 Laptop GPU"
VULKAN_LIST = f"Available devices:\n  Vulkan0: Intel(R) Graphics (18347 MiB)\n  Vulkan1: {RTX} (7899 MiB)\n"
CUDA_LIST = f"Available devices:\n  CUDA0: {RTX} (8150 MiB, 7000 MiB free)\n"


def test_engine_choice_cuda_then_vulkan_then_cpu(tmp_path):
    _fake_exe(ve.engine_exe(tmp_path))
    # нет CUDA-сборки — Vulkan на NVIDIA (не встроенная Intel)
    assert ve.pick_engine(tmp_path, RTX, run=_lister(vulkan=VULKAN_LIST)) == (ve.engine_exe(tmp_path), "Vulkan1")
    _fake_exe(ve.cuda_exe(tmp_path))
    # есть CUDA-сборка и она видит видеокарту — CUDA
    assert ve.pick_engine(tmp_path, RTX, run=_lister(CUDA_LIST, VULKAN_LIST)) == (ve.cuda_exe(tmp_path), "CUDA0")
    # CUDA не запустилась (старый драйвер, библиотеки не грузятся: устройств нет) — запасной Vulkan
    assert ve.pick_engine(tmp_path, RTX, run=_lister("", VULKAN_LIST)) == (ve.engine_exe(tmp_path), "Vulkan1")

    def cuda_crashes(argv, **kw):
        if ve.CUDA_DIR_NAME in argv[0]:
            raise OSError("не запускается")
        return NS(stdout=VULKAN_LIST, stderr="", returncode=0)
    assert ve.pick_engine(tmp_path, RTX, run=cuda_crashes) == (ve.engine_exe(tmp_path), "Vulkan1")
    # видеокарты нет — процессор (CUDA даже не спрашиваем)
    assert ve.pick_engine(tmp_path, None, run=lambda a, **k: pytest.fail("не спрашивать")) == (ve.engine_exe(tmp_path), "none")
    # видеокарта есть, но ни одна сборка её не видит — процессор
    assert ve.pick_engine(tmp_path, RTX, run=_lister()) == (ve.engine_exe(tmp_path), "none")
    argv = ve.server_argv(tmp_path, 51234, "CUDA0", ve.cuda_exe(tmp_path))
    assert argv[0] == str(ve.cuda_exe(tmp_path)) and argv[argv.index("-dev") + 1] == "CUDA0"


def test_cuda_build_only_for_nvidia_with_a_new_enough_driver():
    smi = lambda out: (lambda argv, **kw: NS(stdout=out, stderr="", returncode=0))
    assert REAL_NVIDIA_INFO(smi("621.24, 12.0" + chr(10))) == (621, 12.0)
    assert ve.wants_cuda((621, 12.0)) and ve.wants_cuda((580, 7.5))
    assert not ve.wants_cuda((552, 8.6))          # драйвер старше 580 — CUDA 13 не пойдёт
    assert not ve.wants_cuda((621, 6.1))          # GTX 10xx — CUDA 13 её не поддерживает
    assert not ve.wants_cuda(None)

    def missing(argv, **kw):
        raise FileNotFoundError("nvidia-smi")
    assert ve.nvidia_info(missing) is None and REAL_NVIDIA_INFO(smi("")) is None


def _cuda_zip(name: str) -> bytes:
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, b"x")
        if name == "llama-server.exe":
            z.writestr("ggml-cuda.dll", b"x")
    return buf.getvalue()


def test_installer_on_nvidia_also_fetches_the_cuda_build(tmp_path, small, monkeypatch):
    small.blobs.update({"cuda.zip": _cuda_zip("llama-server.exe"), "cudart.zip": _cuda_zip("cublas64_13.dll")})
    monkeypatch.setattr(ve, "CUDA_ENGINE", _part("cuda.zip", small.blobs["cuda.zip"]))
    monkeypatch.setattr(ve, "CUDA_RT", _part("cudart.zip", small.blobs["cudart.zip"]))
    assert ve.fetch(tmp_path, lambda s: None, download=small.download, home=tmp_path / "nohome", platform="win32", cuda=False)
    assert not ve.cuda_exe(tmp_path).exists() and "cuda.zip" not in small.calls       # без NVIDIA — не качаем
    # NVIDIA: докачивается только CUDA-сборка, в свою папку, рядом с Vulkan (её не удаляет чистка старых версий)
    assert ve.fetch(tmp_path, lambda s: None, download=small.download, home=tmp_path / "nohome", platform="win32", cuda=True)
    assert sorted(p.name for p in ve.cuda_exe(tmp_path).parent.iterdir()) == ["cublas64_13.dll", "ggml-cuda.dll",
                                                                            "llama-server.exe"]
    assert ve.engine_exe(tmp_path).is_file() and not list(ve.vision_dir(tmp_path).glob("*.zip"))
    small.calls.clear()
    assert ve.fetch(tmp_path, lambda s: None, download=small.download, platform="win32", cuda=True) and small.calls == []


def test_cuda_download_failure_leaves_vulkan_working(tmp_path, small, monkeypatch):
    monkeypatch.setattr(ve, "CUDA_ENGINE", _part("cuda.zip", b"z"))
    monkeypatch.setattr(ve, "CUDA_RT", _part("cudart.zip", b"z"))

    def no_cuda(url, part_file, total, tick):
        if "cuda" in url:
            raise OSError("обрыв")
        small.download(url, part_file, total, tick)
    lines = []
    assert ve.fetch(tmp_path, lines.append, download=no_cuda, home=tmp_path / "nohome", platform="win32", cuda=True)
    assert ve.is_installed(tmp_path) and not ve.cuda_exe(tmp_path).exists()
    assert any("Vulkan" in line for line in lines)


def test_start_demo_says_it_runs_on_cuda(tmp_path, small):
    _installed(tmp_path, small)
    cuda = _fake_exe(ve.cuda_exe(tmp_path))
    env = Env(tmp_path, settings=VISION_SETTINGS, pick_engine=lambda root, gpu: (cuda, "CUDA0"), free_port=lambda: 51236)
    assert sd.run(sd.Options(no_browser=True, lite=True), env.deps) == 0
    (argv, _), = _vision(env)
    assert argv[0] == str(cuda) and "CUDA0" in argv and "на видеокарте (CUDA)" in env.text
