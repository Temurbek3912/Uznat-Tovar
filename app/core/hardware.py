"""
Какой компьютер и что на нём включать (документ 11, §10 — замеры 2026-11-14). Только stdlib: проверяется офлайн
(tests/test_hardware.py), а при запуске не импортирует torch (проверка видеокарты — через nvidia-smi, около 0,1 с).

Правила выбраны по замерам на машине разработки, а не на глаз:
  * распознавание по виду (DINOv2-base) на процессоре — 0,13 с на снимок при 4 потоках (на видеокарте 0,015 с), ответ «Узнать
    товар» целиком — около 0,3 с в обоих случаях. Поэтому видеокарта для него НЕ нужна, модель не заменяется на меньшую;
  * модель «зрение + текст» (Qwen3-VL-4B; замер в LM Studio, свой движок программы llama.cpp — те же 1,0 с и около 4,4 ГБ,
    2026-11-16) на видеокарте — около 1,1 с на ответ, но занимает около 4,9 ГБ видеопамяти;
    на процессоре — 10–22 с даже на 24 ядрах и около 5–8 ГБ ОЗУ (LM Studio с моделью). На слабом ноутбуке это дольше предела
    ожидания (40 с) и не помещается в 8 ГБ — поэтому без видеокарты на 6 ГБ+ и 12 ГБ ОЗУ она выключается: товар вне базы
    называется только по штрихкоду (открытые базы), иначе честное «не удалось определить».
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

VISION_MIN_VRAM_GB = 6.0          # Qwen3-VL-4B в LM Studio: +4,9 ГБ видеопамяти (замер), плюс DINOv2 0,4 ГБ
VISION_MIN_RAM_GB = 12.0          # LM Studio с моделью: 3–5 ГБ ОЗУ даже на видеокарте (замер), плюс Windows и программа
MIN_RAM_GB = 6.0                  # ниже — программа (≈1,3 ГБ) плюс Windows (3–4 ГБ) не помещаются без подкачки
LOW_RAM_GB = 12.0                 # ниже — Docker (≈2,2 ГБ ОЗУ, замер) не стоит своей цены: облегчённый режим


@dataclass(frozen=True)
class Hardware:
    ram_gb: float
    cores: int
    gpu_name: str | None = None
    vram_gb: float | None = None

    def describe(self) -> str:
        gpu = f"{self.gpu_name}, {self.vram_gb:.0f} ГБ" if self.gpu_name else "нет видеокарты NVIDIA"
        return f"ОЗУ {self.ram_gb:.0f} ГБ, потоков процессора {self.cores}, видеокарта: {gpu}"


@dataclass
class LitePlan:
    device: str                     # auto | cpu — для ML_DEVICE
    vision: bool                    # включать ли модель «зрение + текст»
    notes: list[str] = field(default_factory=list)


def total_ram_gb() -> float:
    if sys.platform.startswith("win"):
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        if ctypes.WinDLL("kernel32").GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.ullTotalPhys / 2**30
        return 0.0
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
    except (ValueError, OSError, AttributeError):
        return 0.0


def nvidia_gpu(run=subprocess.run, which=shutil.which) -> tuple[str | None, float | None]:
    """(название, видеопамять ГБ) первой видеокарты NVIDIA или (None, None). Без драйвера nvidia-smi нет — значит, и CUDA нет."""
    exe = which("nvidia-smi")
    if not exe:
        return None, None
    try:
        res = run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"], capture_output=True, text=True,
                  timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None, None
    line = (res.stdout or "").strip().splitlines()[0] if res.returncode == 0 and (res.stdout or "").strip() else ""
    if "," not in line:
        return None, None
    name, mem = line.rsplit(",", 1)
    try:
        return name.strip(), float(mem.strip()) / 1024
    except ValueError:
        return name.strip(), None


def probe(run=subprocess.run, which=shutil.which) -> Hardware:
    name, vram = nvidia_gpu(run, which)
    return Hardware(ram_gb=round(total_ram_gb(), 1), cores=os.cpu_count() or 1, gpu_name=name, vram_gb=vram)


def plan_lite(hw: Hardware, *, force_cpu: bool = False, vision: str = "auto") -> LitePlan:
    """Что включить в облегчённом режиме. vision: auto | on | off (on — по желанию владельца, даже если медленно)."""
    notes: list[str] = []
    has_gpu = bool(hw.gpu_name) and not force_cpu
    device = "auto" if has_gpu else "cpu"
    if force_cpu and hw.gpu_name:
        notes.append("Видеокарта есть, но выбран режим «только процессор» (--cpu).")
    if hw.ram_gb and hw.ram_gb < MIN_RAM_GB:
        notes.append(f"ВНИМАНИЕ: ОЗУ {hw.ram_gb:.0f} ГБ — меньше минимума ({MIN_RAM_GB:.0f} ГБ). Закройте другие программы.")
    if vision == "on":
        use_vision = True
        if not (has_gpu and (hw.vram_gb or 0) >= VISION_MIN_VRAM_GB):
            notes.append("Модель «зрение + текст» включена вручную без подходящей видеокарты: ответ о товаре вне базы может "
                         "занимать десятки секунд.")
    elif vision == "off":
        use_vision = False
    else:
        use_vision = has_gpu and (hw.vram_gb or 0) >= VISION_MIN_VRAM_GB and hw.ram_gb >= VISION_MIN_RAM_GB
        if not use_vision:
            why = ("нет видеокарты NVIDIA на 6 ГБ и больше" if not (has_gpu and (hw.vram_gb or 0) >= VISION_MIN_VRAM_GB)
                   else f"ОЗУ меньше {VISION_MIN_RAM_GB:.0f} ГБ")
            notes.append(f"Модель «зрение + текст» выключена ({why}): товар вне базы называется только по штрихкоду, "
                         "иначе — только вид товара («это напиток») малой моделью. Включить: --vision on.")
    return LitePlan(device=device, vision=use_vision, notes=notes)
