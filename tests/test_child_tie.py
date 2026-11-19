"""
scripts/child_tie.py — службы закрываются вместе с программой. Настоящий процесс в задании Windows (только Windows).
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from scripts import child_tie, vision_engine

ROOT = Path(__file__).resolve().parent.parent


def _alive(pid: str) -> bool:
    return pid in subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout


def test_the_real_python_behind_the_venv_launcher_dies_with_the_program():
    """Как в жизни: службы запускаются через .venv\\Scripts\\python.exe (посредник, внутри — настоящий Python, он и держит
    порт). Программу убили жёстко — настоящий процесс службы тоже закрылся."""
    if not sys.platform.startswith("win"):
        pytest.skip("только Windows")
    code = ("import subprocess, sys, time; from scripts import child_tie; "
            "c = subprocess.Popen([sys.executable, '-c', 'import os, time; print(os.getpid(), flush=True); time.sleep(60)'], "
            "stdout=subprocess.PIPE, text=True); ok = child_tie.tie_to_this_process(c); "
            "print(c.stdout.readline().strip(), ok, flush=True); time.sleep(60)")
    parent = subprocess.Popen([sys.executable, "-c", code], cwd=str(ROOT), stdout=subprocess.PIPE, text=True)
    try:
        real_pid, ok = parent.stdout.readline().split()
        assert ok == "True" and _alive(real_pid)
        subprocess.run(["taskkill", "/F", "/PID", str(parent.pid)], capture_output=True)   # как жёсткое завершение
        parent.wait(10)
        for _ in range(50):
            if not _alive(real_pid):
                break
            time.sleep(0.1)
        assert not _alive(real_pid)
    finally:
        parent.kill()


def test_not_windows_is_not_tied_and_the_old_name_still_works():
    assert child_tie.tie_to_this_process(NS(pid=1), platform="linux") is False
    assert vision_engine.tie_to_this_process is child_tie.tie_to_this_process
