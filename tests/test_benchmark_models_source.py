"""ml.benchmark_models: веса из своей папки (скачаны curl) или из кэша Hugging Face."""
from ml import benchmark_models as bm


def test_source_prefers_local_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "MODELS_DIR", tmp_path)
    (tmp_path / "siglip2-large-patch16-256").mkdir()
    assert bm.source("google/siglip2-large-patch16-256") == "google/siglip2-large-patch16-256"   # нет config.json
    (tmp_path / "siglip2-large-patch16-256" / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "siglip2-large-patch16-256" / "model.safetensors").write_bytes(b"0" * 2**20)
    assert bm.source("google/siglip2-large-patch16-256") == str(tmp_path / "siglip2-large-patch16-256")
    assert round(bm.disk_mb("google/siglip2-large-patch16-256")) == 1


def test_large_candidates_listed():
    keys = {c.key: c for c in bm.CANDIDATES}
    assert keys["siglip2-large"].kind == keys["siglip2-so400m"].kind == "siglip"
    assert all(c.license for c in bm.CANDIDATES)
