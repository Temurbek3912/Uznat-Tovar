"""
Скачать модели распознавания заранее (установщик магазина, scripts/install.py), чтобы первый запуск в магазине не ждал загрузки
и работал без интернета. Запускается Python-ом программы (.venv): нужен пакет huggingface_hub (ставится вместе с transformers).

    .venv\\Scripts\\python.exe -m scripts.fetch_models            # в папку models/hf программы (HF_HOME)

Что скачивается: модель распознавания по виду (по config.yaml models.embedder.name: DINOv2-base, около 350 МБ, или SigLIP 2)
и малая модель «вид товара» (SigLIP 2, около 400 МБ; если поиск тоже SigLIP 2 — это одна и та же модель, скачивается один раз),
только файлы, которые читает программа (safetensors и настройки). Уже скачанное не скачивается заново.
Модель «зрение + текст» с её движком сюда не входит: её скачивает scripts/vision_engine.py (установщик зовёт его сам).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCAL_HF = ROOT / "models" / "hf"
ALLOW = ["*.json", "*.safetensors", "*.txt", "tokenizer.model"]


def model_repos() -> list[str]:
    from app.core.config import Settings  # noqa: PLC0415
    kind = os.environ.get("NAMING_KIND_MODEL", Settings.model_fields["naming_kind_model"].default) or ""
    return list(dict.fromkeys([embedder_hub_model()] + ([kind] if kind else [])))


def embedder_hub_model() -> str:
    """Публичная модель поиска по виду из configs/config.yaml (models.embedder.name); настройки не читаются — DINOv2-base."""
    from app.pipeline.embedder import DEFAULT_HUB_MODEL, hub_model_for  # noqa: PLC0415
    try:
        import yaml  # noqa: PLC0415
        cfg = yaml.safe_load((ROOT / "configs" / "config.yaml").read_text(encoding="utf-8")) or {}
        return hub_model_for(((cfg.get("models") or {}).get("embedder") or {}).get("name"))
    except Exception:  # noqa: BLE001 — нет файла / нет yaml: модель по умолчанию
        return DEFAULT_HUB_MODEL


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("HF_HOME", str(LOCAL_HF))
    Path(os.environ["HF_HOME"]).mkdir(parents=True, exist_ok=True)
    from huggingface_hub import snapshot_download  # noqa: PLC0415 — после HF_HOME: библиотека читает его при импорте
    failed = 0
    for repo in model_repos():
        print(f"Модель {repo}...", flush=True)
        try:
            snapshot_download(repo_id=repo, allow_patterns=ALLOW)
            print("  готово", flush=True)
        except Exception as exc:  # noqa: BLE001 — нет интернета и т. п.: программа скачает модель сама при первом запуске
            failed += 1
            print(f"  не скачалась ({exc.__class__.__name__}): программа попробует ещё раз при первом запуске", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
