"""
Дообучение на Kaggle из командной строки (docs/KAGGLE_TRAINING.md). Нужен ключ Kaggle владельца: файл kaggle.json
в C:\\Users\\<имя>\\.kaggle\\ (его скачивает и кладёт сам владелец) и пакет kaggle (pip install kaggle).

    .venv/Scripts/python.exe -m ml.kaggle_push upload     # датасет (частный) — ~25 МБ, при 80 КБ/с около 5 минут
    .venv/Scripts/python.exe -m ml.kaggle_push run        # запустить ноутбук ml/kaggle/finetune.ipynb (GPU T4, ~30–40 мин)
    .venv/Scripts/python.exe -m ml.kaggle_push status     # идёт / готово / ошибка
    .venv/Scripts/python.exe -m ml.kaggle_push download   # результат (~22 МБ) -> D:/uznat_tovar/kaggle-embedder/kaggle-out

Датасет и ноутбук — частные (видит только владелец аккаунта). Ключ этот скрипт не читает и никуда не передаёт: имя
пользователя берётся из kaggle.json (поле username) или --user, остальное делает программа kaggle.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = Path("D:/uznat_tovar/kaggle-embedder")
DATASET = "uznat-tovar-photos"
KERNEL = "uznat-tovar-finetune"


def kaggle_user(given: str) -> str:
    if given:
        return given
    if os.environ.get("KAGGLE_USERNAME"):
        return os.environ["KAGGLE_USERNAME"]
    path = Path(os.environ.get("KAGGLE_CONFIG_DIR", Path.home() / ".kaggle")) / "kaggle.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))["username"]
    except (OSError, ValueError, KeyError):
        raise SystemExit(f"Нет {path} — скачайте ключ на kaggle.com (Settings -> API -> Create New Token) и положите туда, "
                         "или укажите --user") from None


def kaggle(*args: str) -> int:
    exe = shutil.which("kaggle") or str(Path(sys.executable).with_name("kaggle.exe"))
    if not Path(exe).exists() and not shutil.which("kaggle"):
        raise SystemExit("Нет программы kaggle: .venv/Scripts/python.exe -m pip install kaggle")
    print(">", "kaggle", *args, flush=True)
    return subprocess.call([exe, *args])


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["upload", "run", "status", "download"])
    ap.add_argument("--user", default="")
    ap.add_argument("--dataset", default=str(WORK / "dataset"), help="папка от ml/shop_dataset.py")
    a = ap.parse_args(argv)
    user = kaggle_user(a.user)
    if a.action == "upload":
        src, stage = Path(a.dataset), WORK / "upload"
        if not (src / "photos.csv").exists():
            raise SystemExit(f"Нет {src / 'photos.csv'} — сначала python -m ml.shop_dataset --out {src}")
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True)
        shutil.copy(src / "photos.csv", stage)
        shutil.copy(ROOT / "ml" / "finetune_embedder.py", stage)
        shutil.copytree(src / "images", stage / "images")
        meta = {"title": DATASET, "id": f"{user}/{DATASET}", "licenses": [{"name": "other"}]}
        (stage / "dataset-metadata.json").write_text(json.dumps(meta), encoding="utf-8")
        return kaggle("datasets", "create", "-p", str(stage), "--dir-mode", "zip")   # частный по умолчанию
    kid = f"{user}/{KERNEL}"
    if a.action == "run":
        stage = WORK / "kernel"
        stage.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / "ml" / "kaggle" / "finetune.ipynb", stage / "finetune.ipynb")
        meta = {"id": kid, "title": KERNEL, "code_file": "finetune.ipynb", "language": "python", "kernel_type": "notebook",
                "is_private": True, "enable_gpu": True, "enable_internet": True, "machine_shape": "NvidiaTeslaT4",
                "dataset_sources": [f"{user}/{DATASET}"], "competition_sources": [], "kernel_sources": []}
        (stage / "kernel-metadata.json").write_text(json.dumps(meta), encoding="utf-8")
        return kaggle("kernels", "push", "-p", str(stage))
    if a.action == "status":
        return kaggle("kernels", "status", kid)
    out = WORK / "kaggle-out"
    out.mkdir(parents=True, exist_ok=True)
    return kaggle("kernels", "output", kid, "-p", str(out))


if __name__ == "__main__":
    raise SystemExit(main())
