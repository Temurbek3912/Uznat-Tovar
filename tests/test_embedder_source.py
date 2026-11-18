"""
app/pipeline/embedder.py::resolve_embedder_source — реально выполняется (без torch/transformers) — и статическая
проверка, что воркер больше не подставляет случайный эмбеддер молча.

Регрессия, которую защищаем: путь из config.yaml по умолчанию (models/embedder/dinov2_base_retail_ft) не
существует; раньше он уходил в from_pretrained как «идентификатор репозитория», и без дообученной модели не
работали ни каталог, ни воркер.
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.pipeline.embedder import DEFAULT_HUB_MODEL, resolve_embedder_source

ROOT = Path(__file__).resolve().parent.parent


def test_missing_weights_fall_back_to_public_hub_model(tmp_path):
    src, from_hub = resolve_embedder_source("models/embedder/dinov2_base_retail_ft", roots=[tmp_path])
    assert (src, from_hub) == (DEFAULT_HUB_MODEL, True)


def test_empty_or_none_path_uses_hub_model(tmp_path):
    assert resolve_embedder_source(None, roots=[tmp_path]) == (DEFAULT_HUB_MODEL, True)
    assert resolve_embedder_source("", roots=[tmp_path]) == (DEFAULT_HUB_MODEL, True)


def test_existing_local_directory_is_used(tmp_path):
    weights = tmp_path / "models" / "embedder" / "ft"
    weights.mkdir(parents=True)
    src, from_hub = resolve_embedder_source("models/embedder/ft", roots=[tmp_path])
    assert Path(src) == weights and from_hub is False


def test_a_file_with_the_same_name_is_not_a_model_directory(tmp_path):
    (tmp_path / "ft").write_text("not a dir", encoding="utf-8")
    assert resolve_embedder_source("ft", roots=[tmp_path])[1] is True


def test_first_root_that_has_the_directory_wins(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    (a / "w").mkdir(parents=True)
    (b / "w").mkdir(parents=True)
    assert Path(resolve_embedder_source("w", roots=[a, b])[0]) == a / "w"
    (a / "w").rmdir()
    assert Path(resolve_embedder_source("w", roots=[a, b])[0]) == b / "w"


def test_hub_model_dimension_matches_default_config():
    # facebook/dinov2-base -> hidden_size 768; в configs/config.yaml embedding_dim тоже 768.
    # Если кто-то поменяет DEFAULT_HUB_MODEL на small/large, индекс и конфиг разойдутся по размерности.
    assert DEFAULT_HUB_MODEL.endswith("dinov2-base")
    assert "embedding_dim: 768" in (ROOT / "configs" / "config.yaml").read_text(encoding="utf-8")


def test_worker_uses_random_embedder_only_behind_explicit_flag():
    # Подмена эмбеддера случайным теперь живёт в app/pipeline/ml_runtime.py (воркер её не делает): вызов debug_embedder()
    # допустим только после проверки флага ALLOW_DEBUG_EMBEDDER, которая иначе бросает исключение.
    assert "RandomDebugEmbedder" not in (ROOT / "app" / "worker.py").read_text(encoding="utf-8")
    tree = ast.parse((ROOT / "app" / "pipeline" / "ml_runtime.py").read_text(encoding="utf-8"))
    make = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "make_embedder")
    calls = [n for n in ast.walk(make) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "debug_embedder"]
    assert len(calls) == 1
    guards = [n for n in ast.walk(make) if isinstance(n, ast.If) and "ALLOW_DEBUG_EMBEDDER_ENV" in ast.unparse(n.test)
              and any(isinstance(x, ast.Raise) for x in n.body)]
    assert guards and guards[0].lineno < calls[0].lineno, "debug_embedder() должен включаться только флагом ALLOW_DEBUG_EMBEDDER"


# ----------------------------------------------------------------------- выбор модели по config.yaml (models.embedder.name)

def test_hub_model_follows_embedder_name():
    from app.pipeline.embedder import SIGLIP2_HUB_MODEL, embedder_family, hub_model_for
    assert hub_model_for("dinov2-base-retail-ft") == DEFAULT_HUB_MODEL           # нынешнее имя в config.yaml — DINOv2, как было
    assert hub_model_for("") == hub_model_for(None) == DEFAULT_HUB_MODEL
    assert hub_model_for("siglip2-base") == SIGLIP2_HUB_MODEL
    assert hub_model_for("facebook/dinov2-small") == "facebook/dinov2-small"     # полное имя репозитория — как есть
    assert embedder_family("SigLIP2-base") == "siglip" and embedder_family("dinov2-base") == "dinov2"


def test_create_embedder_picks_class_by_name(monkeypatch):
    import types
    from app.pipeline import embedder as E
    made = []

    def fake(kind):
        def init(self, weights_path, embedding_dim=768, device="cuda", hub_model="", int8=False):
            made.append((kind, weights_path, embedding_dim, device, hub_model, int8))
        return init
    monkeypatch.setattr(E.DinoV2Embedder, "__init__", fake("dinov2"))
    monkeypatch.setattr(E.SiglipEmbedder, "__init__", fake("siglip"))
    dino = E.create_embedder(types.SimpleNamespace(name="dinov2-base-retail-ft", weights_path="models/x", embedding_dim=768))
    sig = E.create_embedder(types.SimpleNamespace(name="siglip2-base", weights_path="", embedding_dim=768), device="cpu", int8=True)
    assert type(dino) is E.DinoV2Embedder and type(sig) is E.SiglipEmbedder
    assert made == [("dinov2", "models/x", 768, "cuda", DEFAULT_HUB_MODEL, False),
                    ("siglip", "", 768, "cpu", E.SIGLIP2_HUB_MODEL, True)]


def test_siglip_signature_differs_from_dinov2():
    # Другая модель — другой паспорт: индекс DINOv2 программа с SigLIP 2 не загрузит, нужна пересборка (а не тихая смесь).
    from app.pipeline import embedder as E
    assert E.SiglipEmbedder.PREPROCESS != E.DinoV2Embedder.PREPROCESS == E.DINOV2_PREPROCESS


def test_no_direct_dinov2_construction_outside_benchmarks():
    # Модель выбирается в одном месте (create_embedder): прямое DinoV2Embedder( — только в замерах самой DINOv2.
    allowed = {"app/pipeline/embedder.py", "ml/benchmark_identify.py"}
    found = []
    for folder in ("app", "scripts", "ml"):
        for path in (ROOT / folder).rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if rel not in allowed and "DinoV2Embedder(" in path.read_text(encoding="utf-8"):
                found.append(rel)
    assert not found, found
