"""
Дочерние службы закрываются вместе с программой (start_demo): API, воркер, туннель, движок модели «зрение + текст».

Как. Каждый запущенный процесс кладётся в задание Windows (Job Object) с флагом «закрыть всё при закрытии»
(JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE). Описатель задания держит только эта программа; когда она завершается любым путём —
Ctrl+C, крестик окна, taskkill /F, сбой — Windows закрывает описатель и завершает все процессы задания и их потомков.
Без этого после жёсткого завершения оставался `uvicorn` и держал порт («порт занят» при следующем запуске).

Про Python из .venv: `.venv/Scripts/python.exe` — посредник, он запускает настоящий Python и сам держит его в своём задании
с тем же флагом. Поэтому достаточно привязать посредника: закрылся он — закрылся и настоящий процесс (проверено вживую).
На Linux/macOS ничего не делает (там программа для магазина не запускается).
"""
from __future__ import annotations

import sys

_JOBS: list = []                # задание Windows живёт, пока жива программа


def tie_to_this_process(proc, platform: str = sys.platform) -> bool:
    """Дочерний процесс закрывается вместе с программой, даже если её окно закрыли крестиком, процесс убили (taskkill /F)
    или он упал: задание Windows с «закрыть всё при закрытии». True — привязан; не Windows или не вышло — False."""
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
