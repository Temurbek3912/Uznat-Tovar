"""
Изменённые после индексирования фото — обнаружение в плановой проверке (2026-11-21).

Вектор фото в индексе описывает то содержимое файла, которое было при индексировании; в записи о фото (product_images)
хранится его SHA-256. Если файл потом подменили или испортили (редактор фото, сбой диска, ручная замена), поиск по виду
продолжает работать по старому вектору, а пересборка индекса из файлов даст другой. Раньше это находил только
`scripts/reconcile_index.py --verify-photos` (и только для фото из браузера).

Здесь — проверка для задачи восстановления индекса (app/worker.py, recover_index_task: при запуске и по расписанию):
  - берутся записи об активных эталонах (участвуют в распознавании) с файлом внутри папки данных — и фото из браузера
    (catalog-uploads), и фото импорта (catalog-imported);
  - SHA-256 файла сверяется с записью. Чтобы не читать все фото каждые 30 минут, запоминаются размер и время изменения
    уже проверенного файла: пока они те же, файл не перечитывается. Новых для проверки файлов — не больше `budget` за
    запуск, остальные — в следующий раз (первый проход по большому каталогу растягивается на несколько запусков);
  - найденное только сообщается (журнал и итог задачи); вектор НЕ заменяется: что верно — файл или вектор, решает человек
    (вернуть фото из копии или переиндексировать товар).
Отсутствующий файл здесь не считается: это другая проблема (видна в scripts/reconcile_index.py и пробном восстановлении копии).
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

DEFAULT_BUDGET = 2000         # файлов на SHA-256 за один запуск (замер 2026-11-21: 300 фото каталога ≈ 0,26 с)


@dataclass(frozen=True)
class PhotoRecord:
    image_id: str
    product_id: str
    storage_path: str         # внутри папки данных, через «/»
    sha256: str


@dataclass
class WatchResult:
    changed: list[PhotoRecord] = field(default_factory=list)       # содержимое не совпадает с записью (все известные сейчас)
    newly_changed: list[PhotoRecord] = field(default_factory=list)  # найдены в этом запуске (для журнала — без повторов)
    hashed: int = 0                                                 # файлов прочитано в этом запуске
    pending: int = 0                                                # ещё не проверены (предел за запуск)
    skipped: int = 0                                                # нет файла или путь вне папки данных


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class PhotoContentWatch:
    """Память о проверенных файлах — на процесс (воркер или API в облегчённом режиме). После перезапуска первый проход
    начинается заново, тоже частями по budget."""

    def __init__(self, budget: int = DEFAULT_BUDGET):
        self.budget = max(0, int(budget))
        self._seen: dict[tuple[str, str], tuple[int, int, bool]] = {}   # (id записи, sha) -> (размер, mtime_ns, совпало)

    def check(self, records: Iterable[PhotoRecord], media_root: Path) -> WatchResult:
        res = WatchResult()
        root = Path(media_root).resolve()
        budget = self.budget
        alive: set[tuple[str, str]] = set()
        for rec in sorted(records, key=lambda r: r.storage_path):
            key = (rec.image_id, (rec.sha256 or "").lower())
            path = (root / rec.storage_path).resolve()
            try:
                if not path.is_relative_to(root):
                    raise FileNotFoundError(rec.storage_path)
                st = os.stat(path)
            except OSError:
                res.skipped += 1
                continue
            alive.add(key)
            known = self._seen.get(key)
            if known is not None and known[:2] == (st.st_size, st.st_mtime_ns):
                if not known[2]:
                    res.changed.append(rec)
                continue
            if budget <= 0:
                res.pending += 1
                continue
            budget -= 1
            try:
                same = _sha256(path) == key[1]
            except OSError:
                res.skipped += 1
                continue
            res.hashed += 1
            self._seen[key] = (st.st_size, st.st_mtime_ns, same)
            if not same:
                res.changed.append(rec)
                if known is None or known[2]:
                    res.newly_changed.append(rec)
        for key in [k for k in self._seen if k not in alive]:      # записи удалены или выведены — забыть
            del self._seen[key]
        return res


def reference_records(db) -> list[PhotoRecord]:
    """Записи об активных эталонах с файлом внутри папки данных (product_images)."""
    from app.db.models import ProductImage
    from app.services.product_images import REFERENCE

    rows = (db.query(ProductImage.id, ProductImage.product_id, ProductImage.storage_path, ProductImage.sha256)
            .filter(ProductImage.is_active.is_(True), ProductImage.role == REFERENCE,
                    ProductImage.storage_path.isnot(None)).all())
    return [PhotoRecord(str(i), str(p), str(s), str(h or "")) for i, p, s, h in rows]
