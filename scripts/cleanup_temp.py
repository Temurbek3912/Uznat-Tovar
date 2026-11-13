"""
Очистка временных файлов, оставленных оборванной записью индекса, журналов и фото (app/services/temp_cleanup.py).

    python -m scripts.cleanup_temp            # только показать, что будет удалено (ничего не меняет)
    python -m scripts.cleanup_temp --apply    # удалить

Удаляются только временные файлы программы по строгим шаблонам; фото, индекс, журналы и копии backup-* не трогаются.
Временные файлы индекса — только под блокировкой индекса и когда индекс исправен; остальные — только старше часа.
Каждое действие пишется в logs/cleanup-temp.log; после удаления проверяется результат. Воркер можно не останавливать.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path


def main(argv: list[str] | None = None, out=print, settings=None, resolve_path=None, log_dir: Path | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="удалить (без флага — только показать)")
    ap.add_argument("--min-age-index", type=float, default=None, help="минимальный возраст временных файлов индекса, с (по умолчанию 600)")
    ap.add_argument("--min-age-other", type=float, default=None, help="минимальный возраст прочих временных файлов, с (по умолчанию 3600)")
    args = ap.parse_args(argv)

    from app.services import temp_cleanup as T                          # noqa: PLC0415
    from app.services.catalog_upload import uploads_root                 # noqa: PLC0415
    if settings is None or resolve_path is None:
        from app.core.config import get_settings, resolve_project_path   # noqa: PLC0415
        settings = settings or get_settings()
        resolve_path = resolve_path or resolve_project_path
    if settings.matcher is None or settings.embedder is None:
        out("В configs/config.yaml не заданы models.embedder / models.matcher")
        return 2
    log_dir = Path(log_dir) if log_dir is not None else resolve_path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "cleanup-temp.log"
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")

    with open(log_file, "a", encoding="utf-8") as fh:
        def log(line: str) -> None:
            fh.write(f"{stamp} {'apply' if args.apply else 'check'} {line}\n")
            out(line)

        res = T.cleanup(settings.matcher.index_path, settings.embedder.embedding_dim,
                        uploads_root(resolve_path(settings.media_root)), apply=args.apply,
                        index_min_age=T.INDEX_MIN_AGE_SECONDS if args.min_age_index is None else args.min_age_index,
                        other_min_age=T.OTHER_MIN_AGE_SECONDS if args.min_age_other is None else args.min_age_other, log=log)
        summary = (f"Найдено временных файлов: {len(res.candidates)}; удалено: {len(res.deleted)}; оставлено: {len(res.kept)}; "
                   f"ошибок: {len(res.errors)}; проверка результата: {'пройдена' if res.verified else 'НЕ пройдена'}")
        log(summary)
        for e in res.errors:
            log(f"ОШИБКА: {e}")
    out(f"Журнал: {log_file}")
    if not args.apply and res.candidates:
        out("Это только проверка. Удалить: python -m scripts.cleanup_temp --apply")
    return 0 if res.verified and not res.errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
