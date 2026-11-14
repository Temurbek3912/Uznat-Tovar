"""
Перенос товаров из полного режима (PostgreSQL в Docker) в облегчённый (data/lite, документ 11, §10). Полный режим не меняется.

    python -m scripts.lite_copy              # скопировать, если в облегчённом режиме ещё нет товаров и пользователей
    python -m scripts.lite_copy --replace    # прежние данные облегчённого режима — в папку data/lite.bak-<время>, затем копия

Что копируется: все таблицы базы (товары, штрихкоды, фото товаров, пользователи с паролями и вторым фактором, журнал), индекс
каталога (models/index -> data/lite/index) и фото товаров (data/catalog-uploads -> data/lite/catalog-uploads). Ничего не удаляется:
--replace переименовывает прежнюю папку data/lite, а не стирает её. Запускать при остановленной программе (оба режима).
Нужен работающий PostgreSQL (полный режим: docker compose up -d db).
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

from sqlalchemy import create_engine, func, insert, select

ROOT = Path(__file__).resolve().parent.parent
LITE = ROOT / "data" / "lite"
CHUNK = 500


def _lite_engine(path: Path):
    return create_engine("sqlite:///" + path.as_posix())


def lite_has_data(engine) -> bool:
    from sqlalchemy import inspect
    from app.db.models import Product, User
    names = set(inspect(engine).get_table_names())
    if "products" not in names:
        return False
    with engine.connect() as conn:
        products = conn.execute(select(func.count()).select_from(Product.__table__)).scalar() or 0
        users = conn.execute(select(func.count()).select_from(User.__table__)).scalar() if "users" in names else 0
    return bool(products or users)


def copy_rows(source, target, out=print) -> dict[str, int]:
    """Все таблицы приложения по порядку внешних ключей. Целевая база — пустая и с той же схемой (миграции до последней)."""
    from app.db.models import Base
    counts: dict[str, int] = {}
    with source.connect() as src, target.begin() as dst:
        for table in Base.metadata.sorted_tables:
            rows = [dict(r) for r in src.execute(select(table)).mappings()]
            for i in range(0, len(rows), CHUNK):
                dst.execute(insert(table), rows[i:i + CHUNK])
            counts[table.name] = len(rows)
            if rows:
                out(f"  {table.name}: {len(rows)}")
    return counts


def copy_files(src: Path, dst: Path, skip_suffixes: tuple[str, ...] = (".lock",)) -> int:
    n = 0
    if not src.is_dir():
        return 0
    for path in src.rglob("*"):
        if path.is_file() and not path.name.endswith(skip_suffixes):
            target = dst / path.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            n += 1
    return n


def main(argv: list[str] | None = None, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--replace", action="store_true", help="прежние данные облегчённого режима отложить в data/lite.bak-<время>")
    args = ap.parse_args(argv)

    from app.core.config import get_settings, resolve_project_path
    from app.db.migrate import upgrade_to_head
    from app.db.url import normalize_database_url, redact_database_url
    settings = get_settings()
    if settings.lite_mode or settings.database_url.startswith("sqlite"):
        out("Источник — уже облегчённый режим (SQLite). Запустите из обычного окна, без LITE_MODE и DATABASE_URL=sqlite.")
        return 2
    db_file = LITE / "shop.db"
    if db_file.exists() and lite_has_data(_lite_engine(db_file)):
        if not args.replace:
            out("В облегчённом режиме уже есть товары или пользователи (data/lite/shop.db). Ничего не изменено. "
                "Заменить копией из полного режима: python -m scripts.lite_copy --replace")
            return 1
    if args.replace and LITE.exists():
        backup = LITE.with_name("lite.bak-" + time.strftime("%Y%m%d-%H%M%S"))
        LITE.rename(backup)
        out(f"Прежние данные облегчённого режима отложены: {backup.relative_to(ROOT)}")
    LITE.mkdir(parents=True, exist_ok=True)

    source = create_engine(normalize_database_url(settings.database_url), pool_pre_ping=True)
    out(f"Источник: {redact_database_url(settings.database_url)}")
    try:
        with source.connect():
            pass
    except Exception as exc:     # noqa: BLE001
        out(f"PostgreSQL недоступен ({type(exc).__name__}). Запустите полный режим или: docker compose up -d db")
        return 1
    target = _lite_engine(db_file)
    upgrade_to_head(target)
    out("Копирую базу:")
    counts = copy_rows(source, target, out)
    index_src = resolve_project_path(settings.matcher.index_path).parent if settings.matcher else ROOT / "models" / "index"
    n_index = copy_files(index_src, LITE / "index")
    n_photos = copy_files(resolve_project_path(settings.media_root) / "catalog-uploads", LITE / "catalog-uploads")
    out(f"Готово: товаров {counts.get('products', 0)}, пользователей {counts.get('users', 0)}; файлов индекса {n_index}, "
        f"фото товаров {n_photos}. Запуск облегчённого режима: start_lite.bat")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
