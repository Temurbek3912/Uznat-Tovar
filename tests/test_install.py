"""Установщик магазина (scripts/install.py, Установить.bat), ярлыки (scripts/make_shortcut.py), архив (scripts/make_package.py).

Логика — на подставных командах; ярлык в Windows — настоящим PowerShell во временной папке (не рабочий стол и не автозагрузка).
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import zipfile
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from app.core.hardware import Hardware
from scripts import install as ins
from scripts import make_package, make_shortcut
from scripts import start_demo as sd

ROOT = Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------------- правила

@pytest.mark.parametrize("gpu, driver, cpu, want", [
    (None, None, False, "cpu"),
    ("NVIDIA GeForce RTX 5060", "581.57", False, "cu130"),
    ("NVIDIA GeForce RTX 5060", "575.10", False, "cu128"),
    ("NVIDIA GeForce RTX 5060", "565.90", False, "cpu"),       # Blackwell не работает с CUDA 12.6
    ("NVIDIA GeForce RTX 3060", "565.90", False, "cu126"),
    ("NVIDIA GeForce GTX 1050", "472.12", False, "cpu"),       # старый драйвер
    ("NVIDIA GeForce RTX 3060", None, False, "cpu"),
    ("NVIDIA GeForce RTX 3060", "581.57", True, "cpu"),
])
def test_torch_variant_follows_the_driver(gpu, driver, cpu, want):
    tag, why = ins.torch_variant(gpu, driver, cpu)
    assert tag == want and why
    if gpu and driver and driver.startswith(("565", "472")) and want == "cpu":
        assert "Обновите драйвер" in why


def _levels(items):
    return [lvl for lvl, _ in items]


def test_check_computer_blocks_only_what_cannot_work():
    good = Hardware(ram_gb=16, cores=8, gpu_name=None)
    items = ins.check_computer(good, free_gb=50, windows=(10, 26100), is_64bit=True, need_gb=10)
    assert ins.FAIL not in _levels(items) and any("Windows 11" in t for _, t in items)
    assert any("видеокарты NVIDIA нет" in t and "не страшно" in t for _, t in items)
    assert ins.FAIL in _levels(ins.check_computer(good, free_gb=5, windows=(10, 19045), is_64bit=True, need_gb=10))
    assert ins.FAIL in _levels(ins.check_computer(good, free_gb=50, windows=(6, 9600), is_64bit=True, need_gb=10))
    assert ins.FAIL in _levels(ins.check_computer(good, free_gb=50, windows=(10, 19045), is_64bit=False, need_gb=10))
    weak = Hardware(ram_gb=4, cores=2)
    lv = _levels(ins.check_computer(weak, free_gb=50, windows=(10, 19045), is_64bit=True, need_gb=10))
    assert ins.FAIL in lv and ins.WARN in lv
    seven = Hardware(ram_gb=7.0, cores=4)          # «8 ГБ» с памятью под встроенную графику
    assert ins.FAIL not in _levels(ins.check_computer(seven, free_gb=50, windows=(10, 19045), is_64bit=True, need_gb=10))


def test_fill_env_creates_only_empty_keys_and_never_changes_filled_ones():
    text = "ADMIN_USERNAME=admin\nAUTH_SECRET_KEY=\nCAMERA_SECRET_KEY=keep-me\n"
    new, made = ins.fill_env(text, gen=lambda: "NEW")
    assert made == ["AUTH_SECRET_KEY"] and "AUTH_SECRET_KEY=NEW" in new and "CAMERA_SECRET_KEY=keep-me" in new
    again, made2 = ins.fill_env(new, gen=lambda: "OTHER")
    assert made2 == [] and again == new
    added, made3 = ins.fill_env("ADMIN_USERNAME=admin", gen=lambda: "K")
    assert made3 == ["AUTH_SECRET_KEY", "CAMERA_SECRET_KEY"] and added.endswith("CAMERA_SECRET_KEY=K\n")


def test_secret_is_a_fernet_key():
    key = ins.new_secret()
    assert len(base64.urlsafe_b64decode(key)) == 32 and key != ins.new_secret()
    pytest.importorskip("cryptography")
    from cryptography.fernet import Fernet
    Fernet(key.encode())                            # принимается как ключ шифрования программы


# ----------------------------------------------------------------------- шаги на подставных командах

class Recorder:
    def __init__(self, torch_installed=""):
        self.calls, self.torch_installed = [], torch_installed

    def __call__(self, argv, **kw):
        self.calls.append([str(a) for a in argv])
        if "-c" in argv and any("import torch" in str(a) for a in argv):
            return NS(returncode=0 if self.torch_installed else 1, stdout=self.torch_installed, stderr="")
        return NS(returncode=0, stdout="", stderr="")

    def joined(self):
        return [" ".join(c) for c in self.calls]


def _root(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    (tmp_path / "constraints-opencv.txt").write_text("opencv-python<5\n")
    vpy = ins.venv_python(tmp_path)
    vpy.parent.mkdir(parents=True)
    vpy.write_text("")
    return tmp_path


def test_packages_install_torch_for_the_gpu_then_the_rest_and_remember_it(tmp_path):
    root = _root(tmp_path)
    rec = Recorder()
    d = ins.Deps(run=rec, out=lambda s: None)
    assert ins.step_packages(ins.Options(root=root), d, "cu130", ins.Report())
    j = rec.joined()
    torch_call = next(c for c in j if "torch torchvision --index-url" in c)
    assert torch_call.endswith("https://download.pytorch.org/whl/cu130") and "--no-cache-dir" in torch_call
    assert any("-r requirements.txt -c constraints-opencv.txt" in c for c in j)
    state = json.loads((root / ins.STATE_FILE).read_text(encoding="utf-8"))
    assert state["torch"] == "cu130" and state["requirements"] == ins.requirements_digest(root)

    rec2 = Recorder(torch_installed="2.14.1+cu130")             # повторный запуск: ничего не ставится
    assert ins.step_packages(ins.Options(root=root), ins.Deps(run=rec2, out=lambda s: None), "cu130", ins.Report())
    assert not any("pip install" in c for c in rec2.joined())

    rec3 = Recorder(torch_installed="2.14.1+cpu")               # стояла сборка без видеокарты — заменяется
    assert ins.step_packages(ins.Options(root=root), ins.Deps(run=rec3, out=lambda s: None), "cu130", ins.Report())
    assert any("uninstall -y torch torchvision" in c for c in rec3.joined())


def test_packages_failure_stops_with_a_plain_message(tmp_path):
    root = _root(tmp_path)
    lines = []
    bad = lambda argv, **kw: NS(returncode=1, stdout="", stderr="")    # noqa: E731
    assert not ins.step_packages(ins.Options(root=root), ins.Deps(run=bad, out=lines.append), "cpu", ins.Report())
    assert any("интернет" in ln for ln in lines)
    assert not (root / ins.STATE_FILE).exists()


def test_env_step_writes_keys_once(tmp_path):
    (tmp_path / ".env.example").write_text("ADMIN_USERNAME=admin\nADMIN_PASSWORD=\n", encoding="utf-8")
    rep = ins.Report()
    ins.step_env(ins.Options(root=tmp_path), ins.Deps(out=lambda s: None), rep)
    first = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "AUTH_SECRET_KEY=" in first and "ADMIN_USERNAME=admin" in first and rep.warnings
    ins.step_env(ins.Options(root=tmp_path), ins.Deps(out=lambda s: None), ins.Report())
    assert (tmp_path / ".env").read_text(encoding="utf-8") == first


def test_shortcuts_point_to_lite_mode_and_autostart_is_minimized_without_browser(tmp_path):
    scripts = []

    def fake_run(argv, **kw):
        scripts.append(Path(argv[-1]).read_text(encoding="utf-8-sig"))
        return NS(returncode=0, stdout="x.lnk", stderr="")
    rep = ins.Report()
    ins.step_shortcuts(ins.Options(root=tmp_path), ins.Deps(run=fake_run, out=lambda s: None), rep)
    desktop, startup = scripts
    assert "GetFolderPath('Desktop')" in desktop and "start_lite.bat'" in desktop and "Arguments" not in desktop
    assert "GetFolderPath('Startup')" in startup and "$s.Arguments = '--no-browser'" in startup
    assert "$s.WindowStyle = 7" in startup and "автозапуск" in startup
    assert len(rep.done) == 2

    scripts.clear()
    ins.step_shortcuts(ins.Options(root=tmp_path, shortcut=False, autostart=False), ins.Deps(run=fake_run, out=lambda s: None),
                       ins.Report())
    assert scripts == []


def test_remove_autostart_reports_both_cases():
    lines = []
    yes = lambda argv, **kw: NS(returncode=0, stdout="C:/x.lnk\n", stderr="")   # noqa: E731
    no = lambda argv, **kw: NS(returncode=0, stdout="", stderr="")               # noqa: E731
    assert ins.run(ins.Options(remove_autostart=True), ins.Deps(run=yes, out=lines.append)) == 0
    assert ins.run(ins.Options(remove_autostart=True), ins.Deps(run=no, out=lines.append)) == 0
    assert lines == ["Автозапуск убран.", "Автозапуска не было."]


class FakeProc:
    def __init__(self, dies=False):
        self.dies, self.pid = dies, 4242

    def poll(self):
        return 1 if self.dies else None


def _check(tmp_path, http, proc=None, timeout=30.0):
    (tmp_path / "frontend" / "icons").mkdir(parents=True)
    (tmp_path / "frontend" / "icons" / "icon-192.png").write_bytes(b"\x89PNG")
    clock = iter(range(0, 10_000, 2))
    started, killed, lines = [], [], []
    p = proc or FakeProc()

    def popen(argv, **kw):
        started.append(argv)
        return p
    d = ins.Deps(popen=popen, http=http, kill_tree=killed.append, sleep=lambda s: None, clock=lambda: next(clock),
                 free_port=lambda: 8765, out=lines.append)
    ok = ins.step_check(ins.Options(root=tmp_path, check_timeout=timeout), d, ins.Report())
    return ok, started, killed, lines


def test_check_starts_lite_mode_on_a_free_port_and_waits_for_a_real_answer(tmp_path):
    seen = []

    def http(url, data=None, ctype="", timeout=30.0):
        seen.append(url)
        if url.endswith("/health/ready"):
            return (200 if seen.count(url) >= 3 else 503), b""         # готов не сразу
        if url.endswith("/ui/shop.html"):
            return 200, b"<html>"
        assert data == b"\x89PNG" and ctype == "image/png"
        return 200, json.dumps({"status": "unknown"}).encode()
    ok, started, killed, lines = _check(tmp_path, http)
    argv = " ".join(map(str, started[0]))
    assert ok and "scripts.start_demo --lite --no-browser --public off --port 8765" in argv
    assert killed and "распознавание отвечает" in lines[-1]


def test_check_fails_with_the_log_when_the_program_dies(tmp_path):
    ok, _, killed, lines = _check(tmp_path, lambda *a, **k: (0, b""), proc=FakeProc(dies=True))
    assert not ok and killed and "программа остановилась" in lines[-1] and "install_check.log" in lines[-1]
    ok, _, _, lines = _check(tmp_path / "b", lambda *a, **k: (503, b""), timeout=10)
    assert not ok and "не дождался" in lines[-1]


def test_full_run_stops_on_a_computer_that_cannot_work(tmp_path):
    lines = []
    d = ins.Deps(probe_hw=lambda: Hardware(ram_gb=16, cores=8), windows=lambda: (10, 19045), is_64bit=False,
                 free_gb=lambda p: 100, out=lines.append, run=lambda *a, **k: pytest.fail("ничего не ставить"))
    assert ins.run(ins.Options(root=tmp_path), d) == 1
    assert any("[НЕЛЬЗЯ] нужна 64-битная Windows" in ln for ln in lines)


def test_full_run_goes_through_all_steps(tmp_path):
    root = _root(tmp_path)
    rec, lines = Recorder(), []
    fetched = []
    d = ins.Deps(run=rec, probe_hw=lambda: Hardware(ram_gb=16, cores=8, gpu_name="NVIDIA GeForce RTX 5060", vram_gb=8),
                 driver=lambda: "581.57", windows=lambda: (10, 26100), free_gb=lambda p: 100, out=lines.append,
                 fetch_vision=lambda root, out: fetched.append(root) or True)
    opts = ins.Options(root=root, check=False)
    assert ins.run(opts, d) == 0
    text = "\n".join(lines)
    for n in range(1, 7):
        assert f"[{n}/6]" in text
    assert "cu130" in text and "УСТАНОВКА ЗАВЕРШЕНА" in text and "LM Studio" not in text
    assert "название товара не из базы по фото — да" in text and fetched == [root]       # своя модель, без LM Studio
    assert "модель «зрение + текст» (models/vision)" in text
    assert any("scripts.fetch_models" in c for c in rec.joined())
    assert (root / ".env").is_file()


def test_old_python_is_refused():
    lines = []
    assert ins.run(ins.Options(), ins.Deps(version=(3, 10), out=lines.append)) == 1 and "3.12" in lines[-1]


# ----------------------------------------------------------------------- настоящий PowerShell (только Windows)

@pytest.mark.skipif(not sys.platform.startswith("win"), reason="ярлыки — только Windows")
def test_real_shortcut_in_a_temp_folder_and_its_removal(tmp_path):
    target = ROOT / "start_lite.bat"
    script = make_shortcut.build_ps_script(target=target, workdir=ROOT, icon=ROOT / "frontend" / "icons" / "app.ico",
                                           name="Проверка О'Нил", folder=tmp_path, arguments="--no-browser",
                                           window_style=make_shortcut.WINDOW_MINIMIZED)
    res = make_shortcut.run_ps(script)
    assert res.returncode == 0, res.stderr
    lnk = tmp_path / "Проверка О'Нил.lnk"
    assert lnk.is_file()
    read = make_shortcut.run_ps("\r\n".join([
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut(" + make_shortcut.ps_quote(str(lnk)) + ")",
        "Write-Output $s.TargetPath; Write-Output $s.Arguments; Write-Output $s.WindowStyle"]))
    target_path, args, style = read.stdout.strip().splitlines()
    assert Path(target_path) == target and args == "--no-browser" and style == "7"
    gone = make_shortcut.run_ps(make_shortcut.build_remove_script(name="Проверка О'Нил", folder=tmp_path))
    assert gone.returncode == 0 and gone.stdout.strip() and not lnk.exists()
    again = make_shortcut.run_ps(make_shortcut.build_remove_script(name="Проверка О'Нил", folder=tmp_path))
    assert again.returncode == 0 and not again.stdout.strip()


# ----------------------------------------------------------------------- Установить.bat, архив, папка моделей

def test_install_bat_is_crlf_finds_python_and_runs_the_installer():
    raw = (ROOT / "Установить.bat").read_bytes()
    assert b"\n" not in raw.replace(b"\r\n", b"")
    text = raw.decode("utf-8")
    assert text.startswith("@echo off") and "chcp 65001" in text and '%~dp0' in text
    assert ".venv\\Scripts\\python.exe" in text and "-m scripts.install %*" in text
    assert "Python.Python.3.14" in text and "--scope user" in text and "pause" in text
    for v in ("314", "313", "312"):
        assert v in text


def test_shortcut_target_and_icon_exist():
    assert (ROOT / "start_lite.bat").is_file() and (ROOT / "frontend" / "icons" / "app.ico").is_file()
    assert (ROOT / "frontend" / "icons" / "icon-192.png").is_file()      # снимок для пробного запуска


def test_local_model_folder_is_used_only_when_present_and_not_overridden(tmp_path):
    assert sd.local_hf_home(tmp_path, {}) == {}
    (tmp_path / "models" / "hf").mkdir(parents=True)
    assert sd.local_hf_home(tmp_path, {}) == {"HF_HOME": str(tmp_path / "models" / "hf")}
    assert sd.local_hf_home(tmp_path, {"HF_HOME": "D:/AI-cache"}) == {}


def test_package_name_and_dirty_tree_warning(tmp_path):
    assert make_package.package_name("92b0103abcdef", "20261116") == "uznat-tovar-20261116-92b0103.zip"
    calls, lines = [], []

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[1] == "rev-parse":
            return NS(returncode=0, stdout="92b0103abcdef\n", stderr="")
        if argv[1] == "status":
            return NS(returncode=0, stdout=" M app/main.py\n?? new.txt\n", stderr="")
        if argv[1] == "log":
            return NS(returncode=0, stdout="2026-11-16 10:00\n", stderr="")
        with zipfile.ZipFile(argv[argv.index("-o") + 1], "w") as z:
            z.writestr("uznat-tovar/app/main.py", "x")
        return NS(returncode=0, stdout="", stderr="")
    assert make_package.main(["--out", str(tmp_path)], run=fake_run, out=lines.append, root=tmp_path) == 0
    assert "1 изменённых" in lines[0] and "Установить.bat" in lines[-1]
    assert calls[2][:4] == ["git", "archive", "--format=zip", "--prefix=uznat-tovar/"]


def test_package_leaves_out_tests_and_old_docs():
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    for p in ("/tests", "/docs/archive", "/docs/mockups"):
        assert f"{p}/** " in attrs and "export-ignore" in attrs
    if shutil.which("git") is None:          # копия без git (прогон в Docker-образе программы) — проверяем только .gitattributes
        return
    res = subprocess.run(["git", "check-attr", "export-ignore", "tests/test_install.py", "app/main.py"], cwd=str(ROOT),
                         capture_output=True, text=True)
    if res.returncode == 0:
        out = res.stdout.splitlines()
        assert out[0].endswith("set") and out[1].endswith("unspecified")


def test_weak_computer_does_not_download_the_vision_model_and_a_failed_download_is_a_warning(tmp_path):
    root = _root(tmp_path)
    lines = []
    d = ins.Deps(run=Recorder(), probe_hw=lambda: Hardware(ram_gb=8, cores=4), windows=lambda: (10, 26100),
                 free_gb=lambda p: 100, out=lines.append, fetch_vision=lambda root, out: pytest.fail("не скачивать"))
    assert ins.run(ins.Options(root=root, check=False), d) == 0
    assert "название товара не из базы по фото — нет" in "\n".join(lines)

    lines.clear()
    d = ins.Deps(run=Recorder(), probe_hw=lambda: Hardware(ram_gb=16, cores=8, gpu_name="NVIDIA GeForce RTX 5060", vram_gb=8),
                 driver=lambda: "581.57", windows=lambda: (10, 26100), free_gb=lambda p: 100, out=lines.append,
                 fetch_vision=lambda root, out: False)
    assert ins.run(ins.Options(root=root, check=False), d) == 0                 # без модели программа всё равно работает
    text = "\n".join(lines)
    assert "не скачалась целиком" in text and "Установить.bat" in text


def test_disk_space_counts_the_vision_model_only_when_it_will_be_downloaded(tmp_path):
    root = _root(tmp_path)
    lines = []
    gpu = Hardware(ram_gb=16, cores=8, gpu_name="NVIDIA GeForce RTX 5060", vram_gb=8)
    d = ins.Deps(run=Recorder(), probe_hw=lambda: gpu, driver=lambda: "581.57", windows=lambda: (10, 26100),
                 free_gb=lambda p: ins.MIN_FREE_GB_GPU + 1, out=lines.append, fetch_vision=lambda root, out: True)
    assert ins.run(ins.Options(root=root, check=False), d) == 1                 # 15 ГБ мало: ещё 5 ГБ на модель и CUDA
    need = ins.MIN_FREE_GB_GPU + ins.vision_engine.DISK_GB + ins.vision_engine.CUDA_DISK_GB      # 14 + 4 + 1 = 19
    assert any("[НЕЛЬЗЯ]" in ln and f"не меньше {need:.0f} ГБ" in ln for ln in lines)


def _need_line(lines):
    return next(ln for ln in lines if "на диске свободно" in ln)


def test_disk_space_counts_cuda_only_when_it_will_be_downloaded(tmp_path, monkeypatch):
    ve = ins.vision_engine
    root = _root(tmp_path)
    gpu = Hardware(ram_gb=16, cores=8, gpu_name="NVIDIA GeForce RTX 4060", vram_gb=8)   # PyTorch есть и для драйвера 566

    def run_with(driver):
        lines = []
        d = ins.Deps(run=Recorder(), probe_hw=lambda: gpu, driver=lambda: driver, windows=lambda: (10, 26100),
                     free_gb=lambda p: ins.MIN_FREE_GB_GPU + 4.5, out=lines.append, fetch_vision=lambda root, out: True)
        return ins.run(ins.Options(root=root, check=False), d), lines

    # старый драйвер (ниже 580): CUDA не качается — хватает 14 + 4
    code, lines = run_with("566.36")
    assert "[НЕЛЬЗЯ]" not in _need_line(lines)
    assert "название товара не из базы по фото — да" in "\n".join(lines)          # модель ставится, только без CUDA
    # новый драйвер: нужно ещё 1 ГБ под CUDA
    code, lines = run_with("581.57")
    assert code == 1 and "[НЕЛЬЗЯ]" in _need_line(lines)
    # модель (Vulkan) уже стоит, а CUDA нет: считается только CUDA
    monkeypatch.setattr(ve, "is_installed", lambda root: True)
    code, lines = run_with("581.57")
    assert "[НЕЛЬЗЯ]" not in _need_line(lines)
    assert ve.disk_need_gb(root, cuda=True) == ve.CUDA_DISK_GB
    # всё на месте — ничего не добавляется
    ve.cuda_exe(root).parent.mkdir(parents=True)
    ve.cuda_exe(root).write_bytes(b"x")
    assert ve.disk_need_gb(root, cuda=True) == 0
