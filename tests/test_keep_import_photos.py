"""Копии фото импорта внутри программы (app/services/product_images.keep_import_copy, scripts/keep_import_photos.py)."""
from __future__ import annotations

import hashlib

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db.models import Base, Product, ProductImage  # noqa: E402
from app.services.product_images import IMPORTED_SUBDIR, keep_import_copy  # noqa: E402
from scripts.keep_import_photos import plan_and_apply  # noqa: E402


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_copy_is_byte_for_byte_idempotent_and_refuses_a_different_file(tmp_path):
    src = tmp_path / "import" / "A" / "1.JPG"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"photo-bytes")
    media = tmp_path / "media"
    rel = keep_import_copy(media, "A", src, _sha(b"photo-bytes"))
    assert rel == f"{IMPORTED_SUBDIR}/A/{_sha(b'photo-bytes')[:16]}.jpg"
    assert (media / rel).read_bytes() == b"photo-bytes" and src.is_file()            # папку импорта не трогает
    assert keep_import_copy(media, "A", src, _sha(b"photo-bytes")) == rel             # второй раз — то же, без перезаписи
    assert keep_import_copy(media, "A", src, _sha(b"other")) is None                  # содержимое не совпало с записью
    assert not list((media / IMPORTED_SUBDIR / "A").glob("*.part"))


def test_script_fills_storage_path_only_for_import_photos_with_matching_files(tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    folder = tmp_path / "catalog"
    (folder / "UZ-1").mkdir(parents=True)
    (folder / "UZ-1" / "1.jpg").write_bytes(b"one")
    (folder / "UZ-1" / "2.jpg").write_bytes(b"changed")
    p = Product(sku="UZ-1", name="A", brand="", category="")
    db.add(p)
    db.commit()
    good = ProductImage(product_id=p.id, sha256=_sha(b"one"), source_ref=f"import:{_sha(b'one')}", original_name="UZ-1/1.jpg")
    bad = ProductImage(product_id=p.id, sha256=_sha(b"two"), source_ref=f"import:{_sha(b'two')}", original_name="UZ-1/2.jpg")
    gone = ProductImage(product_id=p.id, sha256=_sha(b"x"), source_ref=f"import:{_sha(b'x')}", original_name="../../etc/x.jpg")
    upload = ProductImage(product_id=p.id, sha256=_sha(b"u"), source_ref="upload:UZ-1/001-aaaaaaaaaaaa.jpg", original_name="UZ-1/1.jpg")
    db.add_all([good, bad, gone, upload])
    db.commit()
    media = tmp_path / "media"
    assert plan_and_apply(db, folder, media, apply=False) == {"found": 2, "copied": 0, "missing": 1, "mismatch": 1}
    assert db.get(ProductImage, good.id).storage_path is None and not media.exists()   # проверка ничего не меняет
    assert plan_and_apply(db, folder, media, apply=True) == {"found": 2, "copied": 1, "missing": 1, "mismatch": 1}
    assert db.get(ProductImage, good.id).storage_path.startswith(f"{IMPORTED_SUBDIR}/UZ-1/")
    assert db.get(ProductImage, bad.id).storage_path is None and db.get(ProductImage, upload.id).storage_path is None
    assert plan_and_apply(db, folder, media, apply=True)["copied"] == 0                  # повторный запуск безопасен
