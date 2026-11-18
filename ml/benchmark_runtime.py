"""
Замер скорости и памяти на ЭТОМ компьютере (документ 11, §1, §3, §4, §10): честные цифры вместо оценок.

    python -m ml.benchmark_runtime embedder --device cuda
    python -m ml.benchmark_runtime embedder --device cpu --threads 4                       # как на 4-ядерном ноутбуке
    python -m ml.benchmark_runtime embedder --device cpu --threads 4 --model facebook/dinov2-small
    python -m ml.benchmark_runtime embedder --device cpu --threads 4 --int8               # квантование линейных слоёв
    python -m ml.benchmark_runtime http --url http://127.0.0.1:8000 --photos data/catalog-uploads --n 8

``embedder`` — модель распознавания по виду отдельно: время загрузки, первого и следующих кадров, пиковая память процесса (ОЗУ)
и видеопамяти. ``http`` — «Узнать товар» целиком через публичный вход (как телефон покупателя): время каждого ответа и его статус.
Между запросами — пауза (публичный вход пропускает не больше 12 снимков в минуту с одного устройства).

Что эти цифры НЕ показывают: скорость на настоящем слабом ноутбуке. ``--threads 4`` ограничивает число потоков, но ядра этого
процессора (Core Ultra 9 275HX) быстрее, чем у дешёвого ноутбука, — на нём будет медленнее (во сколько — не измерено).
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import statistics
import sys
import time
import urllib.request
from pathlib import Path

PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def peak_rss_mb() -> float | None:
    """Пиковый рабочий набор процесса (ОЗУ, реально занятая), МБ. Только Windows; иначе — ru_maxrss."""
    if sys.platform.startswith("win"):
        class PMC(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong), ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        kernel32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(PMC), ctypes.c_ulong]
        if psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
            return round(pmc.PeakWorkingSetSize / 2**20, 1)
        return None
    import resource
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


def photos_in(folder: Path, limit: int) -> list[Path]:
    files = sorted(p for p in folder.rglob("*") if p.suffix.lower() in PHOTO_SUFFIXES and not p.name.startswith("thumb"))
    return files[:limit]


def _summary(times: list[float]) -> dict:
    ordered = sorted(times)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return {"n": len(times), "median_s": round(statistics.median(times), 3), "p95_s": round(p95, 3), "max_s": round(max(times), 3)}


def bench_embedder(args) -> dict:
    t0 = time.perf_counter()
    import numpy as np
    import torch
    if args.threads:
        torch.set_num_threads(args.threads)
    from types import SimpleNamespace
    from app.pipeline.embedder import create_embedder
    from app.pipeline.imageio import load_rgb
    t_import = time.perf_counter() - t0
    t1 = time.perf_counter()
    emb = create_embedder(SimpleNamespace(name=args.model, weights_path="", embedding_dim=args.dim), device=args.device,
                          int8=args.int8)          # --model google/siglip2-base-patch16-224 — SigLIP 2
    t_load = time.perf_counter() - t1
    images = [load_rgb(str(p)) for p in photos_in(Path(args.photos), args.n)]
    if not images:
        raise SystemExit(f"нет фото в {args.photos}")
    t2 = time.perf_counter()
    emb.embed([images[0]])
    first = time.perf_counter() - t2
    times = []
    for _ in range(args.rounds):
        for img in images:
            t = time.perf_counter()
            vec = emb.embed([img])
            times.append(time.perf_counter() - t)
    cuda_peak = round(torch.cuda.max_memory_reserved() / 2**20, 1) if emb.device == "cuda" else 0.0
    return {"mode": "embedder", "model": args.model, "device": emb.device, "int8": bool(args.int8),
            "threads": torch.get_num_threads(), "import_s": round(t_import, 2), "load_s": round(t_load, 2),
            "first_s": round(first, 3), **_summary(times), "peak_ram_mb": peak_rss_mb(), "cuda_reserved_mb": cuda_peak,
            "dim": int(np.asarray(vec).shape[1])}


def bench_http(args) -> dict:
    url = args.url.rstrip("/") + "/api/v1/public/identify"
    rows, times = [], []
    for path in photos_in(Path(args.photos), args.n):
        req = urllib.request.Request(url, data=path.read_bytes(), headers={"Content-Type": "image/jpeg"}, method="POST")
        t = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                body = json.loads(r.read().decode("utf-8"))
            status = body.get("status") + (f"/{body['by']}" if body.get("by") else "")
            name = (body.get("product") or {}).get("name") or (body.get("guess") or {}).get("name") or ""
        except Exception as exc:     # noqa: BLE001 — ошибка ответа тоже результат замера
            status, name = f"error: {exc}", ""
        dt = time.perf_counter() - t
        times.append(dt)
        rows.append({"photo": path.name, "seconds": round(dt, 2), "status": status, "name": name})
        print(f"  {dt:6.2f} с  {status:<15} {name}  ({path.name})", flush=True)
        time.sleep(args.pause)
    return {"mode": "http", "url": args.url, **_summary(times), "rows": rows}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("embedder")
    e.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    e.add_argument("--threads", type=int, default=0, help="потоков процессора (0 — как решит torch)")
    e.add_argument("--model", default="facebook/dinov2-base")
    e.add_argument("--dim", type=int, default=0, help="размер вектора (0 — по модели)")
    e.add_argument("--int8", action="store_true", help="квантование линейных слоёв в int8 (только CPU)")
    e.add_argument("--photos", default="data/catalog-uploads")
    e.add_argument("--n", type=int, default=12)
    e.add_argument("--rounds", type=int, default=2)
    h = sub.add_parser("http")
    h.add_argument("--url", default="http://127.0.0.1:8000")
    h.add_argument("--photos", default="data/catalog-uploads")
    h.add_argument("--n", type=int, default=8)
    h.add_argument("--pause", type=float, default=5.5)
    ap.add_argument("--json", help="куда записать итог (JSON)")
    args = ap.parse_args(argv)
    result = bench_embedder(args) if args.cmd == "embedder" else bench_http(args)
    text = json.dumps(result, ensure_ascii=False, indent=1)
    print(text if args.cmd == "embedder" else json.dumps({k: v for k, v in result.items() if k != "rows"}, ensure_ascii=False))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main())
