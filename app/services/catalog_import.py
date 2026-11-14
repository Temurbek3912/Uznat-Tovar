"""
Массовая загрузка каталога из папки: `catalog/<артикул>/*.jpg` + необязательный `products.csv`.

Модуль чистый: без SQLAlchemy и без загрузки моделей — база, эмбеддер и индекс подаются снаружи (интерфейсы ниже), поэтому
вся логика проверяется тестами без Postgres и GPU. Реальная «проводка» — scripts/import_catalog.py.

Принципы (все они — ответ на конкретный способ, которым пакетная загрузка ломается в реальности):
  * Идемпотентность. Повторный запуск не плодит дубли векторов: каждое фото запоминается в манифесте по SHA-256 файла.
    Новые фото к существующему товару — добавляются; уже импортированные — пропускаются.
  * Устойчивость к обрывам. Сначала считаются эмбеддинги, и только потом создаётся товар в БД (упало вычисление — в БД
    ничего лишнего). Индекс и манифест сохраняются контрольными точками и при Ctrl+C. Если процесс упал между созданием товара и
    сохранением индекса, товар в БД есть, а фото в манифесте нет — следующий запуск просто добавит вектора (это же «починка»).
  * Один сбойный товар не останавливает остальные: ошибка записывается в итог, импорт идёт дальше.
  * Фото с телефона читаются через load_rgb (EXIF-поворот), иначе портретные эталоны оказались бы повёрнутыми.
  * Каталог не «слепой»: после загрузки отчёт показывает ПОХОЖИЕ товары (у которых фото почти неотличимы) и товары, которые
    система путает уже внутри каталога — именно они в магазине дают ошибки и уходят в ревью.

Индекс не умеет удалять векторы (docs/HANDOFF.md, №14), поэтому «заменить фото» = удалить файлы индекса и манифест и
запустить импорт заново: товары уже в БД, фото из папки добавятся в чистый индекс (SKU вне папки потеряют вектора).
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Protocol

import numpy as np

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
UNREADABLE_EXTS = (".heic", ".heif")           # iPhone «высокая эффективность»: PIL без плагина не читает
MAX_SKU_LEN = 64
MIN_SIDE_ERROR = 64                            # меньше — фото бесполезно
MIN_SIDE_WARN = 224                            # DINOv2 приводит вход к 224 px: меньшее увеличивается и размывается
MIN_SHARPNESS_WARN = 15.0
MIN_PHOTOS_WARN = 3
DEFAULT_BRAND, DEFAULT_CATEGORY = "Не указан", "Без категории"
CONFUSABLE_THRESHOLD = 0.90
MAX_PHOTOS_FOR_SELF_CHECK = 6000               # O(N^2) по памяти: свыше — самопроверку пропускаем

_SKU_OK = re.compile(r"^[^\x00-\x1f\\/:*?\"<>|]+$")


# --------------------------------------------------------------------------- данные


@dataclass(frozen=True)
class ProductMeta:
    sku: str
    name: str
    brand: str = DEFAULT_BRAND
    category: str = DEFAULT_CATEGORY
    expected_price: float | None = None


@dataclass
class ExistingProduct:
    id: str
    name: str
    brand: str
    category: str
    expected_price: float | None


class CatalogRepo(Protocol):
    def existing_products(self) -> dict[str, ExistingProduct]: ...      # sku -> товар
    def create_product(self, meta: ProductMeta) -> str: ...             # -> product_id (фиксирует в БД)
    def update_product(self, product_id: str, meta: ProductMeta) -> None: ...


@dataclass(frozen=True)
class SkuFolder:
    sku: str
    photos: list[Path]


# --------------------------------------------------------------------------- сканирование папки


def validate_sku(sku: str) -> str | None:
    if sku != sku.strip() or not sku:
        return "пробелы по краям или пустое имя"
    if len(sku) > MAX_SKU_LEN:
        return f"длиннее {MAX_SKU_LEN} символов"
    if not _SKU_OK.match(sku):
        return "недопустимые символы (управляющие и \\ / : * ? \" < > |)"
    return None


def scan_catalog_folder(root: Path) -> tuple[list[SkuFolder], list[str], list[str]]:
    """-> (папки SKU, предупреждения, ошибки). Папка SKU = прямая подпапка; скрытые (.git, __MACOSX) пропускаются."""
    warnings: list[str] = []
    errors: list[str] = []
    if not root.is_dir():
        return [], warnings, [f"нет папки {root}"]
    stray = [p.name for p in root.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    if stray:
        warnings.append(f"{len(stray)} фото лежат прямо в {root.name}/ и проигнорированы — фото каждого товара должны быть в папке с его артикулом")
    folders: list[SkuFolder] = []
    seen_lower: dict[str, str] = {}
    for d in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
        problem = validate_sku(d.name)
        if problem:
            errors.append(f"папка «{d.name}»: {problem}")
            continue
        low = d.name.lower()
        if low in seen_lower:
            warnings.append(f"артикулы «{seen_lower[low]}» и «{d.name}» отличаются только регистром — в базе это разные товары; уверены?")
        seen_lower.setdefault(low, d.name)
        photos = sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS)
        heic = [p.name for p in d.iterdir() if p.is_file() and p.suffix.lower() in UNREADABLE_EXTS]
        nested = [p.name for p in d.iterdir() if p.is_dir()]
        if heic:
            warnings.append(f"{d.name}: {len(heic)} файлов HEIC пропущено (не читаются) — сохраните фото как JPG")
        if nested:
            warnings.append(f"{d.name}: вложенные папки ({', '.join(nested[:3])}) проигнорированы — фото должны лежать прямо в папке артикула")
        if not photos:
            warnings.append(f"{d.name}: нет фото ({', '.join(IMAGE_EXTS)}) — товар пропущен")
            continue
        folders.append(SkuFolder(d.name, photos))
    if not folders and not errors:
        errors.append(f"в {root} не найдено ни одной папки товара с фото")
    return folders, warnings, errors


# --------------------------------------------------------------------------- products.csv

_ALIASES = {
    "sku": {"sku", "артикул", "код", "article"},
    "name": {"name", "название", "наименование", "товар"},
    "brand": {"brand", "бренд", "производитель"},
    "category": {"category", "категория", "группа"},
    "price": {"price", "expected_price", "цена", "ожидаемая цена"},
}


def _parse_price(raw: str) -> float | None:
    """Те же правила, что у формы добавления товара (catalog_upload.parse_price): «9 900», «1.200,50», «9900 сум».
    Раньше здесь был float(): «nan», «inf», «1e999» проходили как цена, а NaN в базе ломает ответ API со списком товаров
    (JSON не допускает NaN — сервер отвечал бы ошибкой всем пользователям). ValueError — с причиной для человека."""
    from app.services.catalog_upload import CatalogAddError, parse_price  # noqa: PLC0415 — catalog_upload сам импортирует этот модуль
    try:
        return parse_price(raw)
    except CatalogAddError as exc:
        raise ValueError(str(exc)) from None


CSV_ENCODINGS_HINT = ("сохраните таблицу в Excel как «CSV UTF-8 (разделитель — запятая)» — тогда узбекские буквы oʻ, gʻ и кириллица "
                      "сохранятся без искажений")


def decode_products_csv(data: bytes) -> tuple[str, list[str]]:
    """Байты products.csv -> текст и предупреждения. UTF-8 (с BOM или без) — как есть. Иначе это почти наверняка «CSV» из
    Excel в русской/узбекской Windows, то есть Windows-1251: раньше файл читался как UTF-8 с заменой непонятных байтов, и все
    кириллические названия МОЛЧА превращались в «�����». Теперь — Windows-1251 с предупреждением."""
    try:
        return data.decode("utf-8-sig"), []
    except UnicodeDecodeError:
        pass
    try:
        text = data.decode("cp1251")
    except UnicodeDecodeError:
        raise ValueError(f"products.csv не в UTF-8 и не в Windows-1251 — {CSV_ENCODINGS_HINT}") from None
    return text, [f"products.csv в кодировке Windows-1251 (не UTF-8): прочитан как кириллица, проверьте названия; лучше {CSV_ENCODINGS_HINT}"]


def _clean_texts(name: str, brand: str, category: str) -> tuple[str, str, str]:
    from app.services.catalog_upload import MAX_TEXT_LEN, CatalogAddError, clean_name, clean_text  # noqa: PLC0415
    try:
        return (clean_name(name), clean_text(brand, label="бренд", max_len=MAX_TEXT_LEN, default=DEFAULT_BRAND),
                clean_text(category, label="категорию", max_len=MAX_TEXT_LEN, default=DEFAULT_CATEGORY))
    except CatalogAddError as exc:
        raise ValueError(str(exc)) from None


def parse_products_csv(text: str) -> tuple[dict[str, ProductMeta], list[str]]:
    """CSV с колонками sku, name (обязательны) и brand, category, price (по желанию); имена колонок — русские или английские;
    разделитель `,` или `;` (Excel в русской/узбекской локали сохраняет с `;`); BOM допустим."""
    text = text.lstrip("\ufeff")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return {}, ["products.csv пуст"]
    delimiter = ";" if lines[0].count(";") > lines[0].count(",") else ","
    reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter)
    header = [h.strip().lower() for h in next(reader)]
    col: dict[str, int] = {}
    for field_name, names in _ALIASES.items():
        for i, h in enumerate(header):
            if h in names:
                col.setdefault(field_name, i)
    errors: list[str] = []
    for required in ("sku", "name"):
        if required not in col:
            errors.append(f"в заголовке нет колонки «{required}» (допустимо: {', '.join(sorted(_ALIASES[required]))}); найдено: {', '.join(header)}")
    if errors:
        return {}, errors
    metas: dict[str, ProductMeta] = {}
    for no, rec in enumerate(reader, start=2):
        def cell(key: str) -> str:
            i = col.get(key)
            return rec[i].strip() if i is not None and i < len(rec) else ""
        sku = cell("sku")
        if not sku:
            errors.append(f"строка {no}: пустой артикул")
            continue
        if sku in metas:
            errors.append(f"строка {no}: артикул «{sku}» повторяется")
            continue
        if not cell("name"):
            errors.append(f"строка {no}: у «{sku}» пустое название")
            continue
        try:
            price = _parse_price(cell("price"))
        except ValueError as exc:
            errors.append(f"строка {no}: цена «{cell('price')}» не число или не подходит — {exc}")
            continue
        try:                                                # те же ограничения длины и очистка, что у формы и правки товара
            name, brand, category = _clean_texts(cell("name"), cell("brand"), cell("category"))
        except ValueError as exc:
            errors.append(f"строка {no}: у «{sku}» {exc}")
            continue
        metas[sku] = ProductMeta(sku, name, brand, category, price)
    return metas, errors


# --------------------------------------------------------------------------- проверка фото


@dataclass
class PhotoCheck:
    path: Path
    sha256: str = ""
    width: int = 0
    height: int = 0
    sharpness: float = 0.0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def measure_sharpness(rgb: np.ndarray) -> float:
    """Дисперсия лапласиана на уменьшенной копии (не зависит от разрешения фото)."""
    import cv2
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    scale = 512 / max(h, w)
    if scale < 1:
        gray = cv2.resize(gray, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def check_photo(path: Path, load: Callable[[Path], np.ndarray] | None = None) -> PhotoCheck:
    if load is None:
        from app.pipeline.imageio import load_rgb as load
    chk = PhotoCheck(path)
    try:
        chk.sha256 = file_sha256(path)
        rgb = load(path)
    except Exception as exc:    # noqa: BLE001 — любое исключение декодера = файл непригоден
        chk.errors.append(f"{path.name}: не читается ({exc.__class__.__name__})")
        return chk
    chk.height, chk.width = rgb.shape[:2]
    short = min(chk.height, chk.width)
    if short < MIN_SIDE_ERROR:
        chk.errors.append(f"{path.name}: слишком мелкое ({chk.width}x{chk.height})")
        return chk
    if short < MIN_SIDE_WARN:
        chk.warnings.append(f"{path.name}: низкое разрешение ({chk.width}x{chk.height}); лучше от {MIN_SIDE_WARN} px по короткой стороне")
    chk.sharpness = measure_sharpness(rgb)
    if chk.sharpness < MIN_SHARPNESS_WARN:
        chk.warnings.append(f"{path.name}: размытое или однотонное (резкость {chk.sharpness:.1f}) — эталон будет плохим")
    return chk


# --------------------------------------------------------------------------- манифест


@dataclass
class Manifest:
    photos: dict[str, dict] = field(default_factory=dict)       # sha256 -> {sku, file, product_id}
    products: dict[str, str] = field(default_factory=dict)      # sku -> product_id

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        if not path.is_file():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(photos=data.get("photos", {}), products=data.get("products", {}))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        from app.services.catalog_upload import _atomic_write  # noqa: PLC0415 — уникальный временный файл, fsync, уборка при ошибке
        _atomic_write(path, json.dumps({"version": 1, "photos": self.photos, "products": self.products}, ensure_ascii=False,
                                       indent=1).encode("utf-8"))   # атомарно: обрыв посреди записи не оставит битый манифест


# --------------------------------------------------------------------------- план


@dataclass
class SkuPlan:
    sku: str
    meta: ProductMeta
    action: str                                  # create | add_photos | unchanged | skip
    new_photos: list[PhotoCheck] = field(default_factory=list)
    known_photos: int = 0                        # уже импортированы (по манифесту)
    meta_changed: bool = False
    product_id: str | None = None                # для существующих


@dataclass
class ImportPlan:
    skus: list[SkuPlan]
    warnings: list[str]
    errors: list[str]

    def count(self, action: str) -> int:
        return sum(1 for s in self.skus if s.action == action)


def _meta_differs(meta: ProductMeta, ex: ExistingProduct) -> bool:
    return (meta.name, meta.brand, meta.category, meta.expected_price) != (ex.name, ex.brand, ex.category, ex.expected_price)


def build_plan(folders: list[SkuFolder], metas: dict[str, ProductMeta], manifest: Manifest, checks: dict[Path, PhotoCheck],
               existing: dict[str, ExistingProduct] | None = None, *, require_csv: bool = False) -> ImportPlan:
    existing = existing or {}
    warnings: list[str] = []
    errors: list[str] = []
    seen_sha: dict[str, str] = {}                                # sha -> sku, для дублей между товарами
    plans: list[SkuPlan] = []
    folder_skus = {f.sku for f in folders}
    for sku in sorted(set(metas) - folder_skus):
        warnings.append(f"«{sku}» есть в products.csv, но нет папки с фото — товар не загружается")
    if metas:
        for f in folders:
            if f.sku not in metas:
                (errors if require_csv else warnings).append(f"папка «{f.sku}» не описана в products.csv — "
                                                              + ("загрузка остановлена (--require-csv)" if require_csv else
                                                                 "название = артикул, бренд/категория по умолчанию"))
    elif require_csv:
        errors.append("products.csv обязателен (--require-csv), но не найден или пуст")

    for f in folders:
        meta = metas.get(f.sku) or ProductMeta(sku=f.sku, name=f.sku)
        good: list[PhotoCheck] = []
        known = 0
        local_seen: set[str] = set()
        for p in f.photos:
            chk = checks[p]
            warnings += chk.warnings
            if not chk.ok:
                warnings += [f"{e} — пропущено" for e in chk.errors]      # битое фото — предупреждение, а не ошибка всей загрузки
                continue
            if chk.sha256 in local_seen:
                warnings.append(f"{f.sku}/{p.name}: точная копия другого фото этого товара — пропущено")
                continue
            local_seen.add(chk.sha256)
            if chk.sha256 in seen_sha and seen_sha[chk.sha256] != f.sku:
                errors.append(f"{f.sku}/{p.name}: ТО ЖЕ фото уже лежит в товаре «{seen_sha[chk.sha256]}» — один снимок не может быть двумя товарами")
                continue
            seen_sha.setdefault(chk.sha256, f.sku)
            if chk.sha256 in manifest.photos:
                known += 1
                continue
            good.append(chk)
        ex = existing.get(f.sku)
        total_after = known + len(good)
        if total_after and total_after < MIN_PHOTOS_WARN:
            warnings.append(f"{f.sku}: всего {total_after} фото (рекомендуется от {MIN_PHOTOS_WARN}, лучше 5–8 с разных сторон)")
        if ex is None and not good:
            plans.append(SkuPlan(f.sku, meta, "skip", known_photos=known))
            warnings.append(f"{f.sku}: нет пригодных фото — товар не создаётся")
        elif ex is None:
            plans.append(SkuPlan(f.sku, meta, "create", good, known))
        else:
            plans.append(SkuPlan(f.sku, meta, "add_photos" if good else "unchanged", good, known,
                                 meta_changed=_meta_differs(meta, ex) if f.sku in metas else False, product_id=ex.id))
    return ImportPlan(plans, warnings, errors)


# --------------------------------------------------------------------------- выполнение


@dataclass
class ImportResult:
    created: int = 0
    extended: int = 0
    unchanged: int = 0
    skipped: int = 0
    photos_added: int = 0
    metadata_updated: int = 0
    failed: list[str] = field(default_factory=list)
    interrupted: bool = False
    embeddings: dict[str, np.ndarray] = field(default_factory=dict)     # sku -> векторы ЭТОГО запуска (для отчёта о похожих)


def _batches(items: list, n: int) -> Iterable[list]:
    for i in range(0, len(items), n):
        yield items[i:i + n]


def run_import(plan: ImportPlan, repo: CatalogRepo, embedder, index, *, index_path: str, manifest: Manifest, manifest_path: Path,
               load: Callable[[Path], np.ndarray] | None = None, update_metadata: bool = False, batch_size: int = 16,
               checkpoint_every: int = 25, out: Callable[[str], None] = print,
               remember: Callable[[str, str, list[PhotoCheck], np.ndarray], None] | None = None) -> ImportResult:
    """remember(товар, артикул, фото, векторы) — записи о фото и кэш векторов в базе (product_images), до индекса."""
    if load is None:
        from app.pipeline.imageio import load_rgb as load
    res = ImportResult()
    since_checkpoint = 0

    def checkpoint() -> None:
        index.save(index_path)                                  # сначала индекс, потом манифест: обрыв между ними даёт лишь
        manifest.save(manifest_path)                            # повторное добавление пары векторов, а не «потерянные» фото

    todo = plan.skus
    try:
        for n, sp in enumerate(todo, start=1):
            tag = f"[{n}/{len(todo)}] {sp.sku}"
            if sp.action == "skip":
                res.skipped += 1
                out(f"{tag}: пропущен")
                continue
            try:
                if sp.action == "unchanged":
                    if sp.meta_changed and update_metadata and sp.product_id:
                        repo.update_product(sp.product_id, sp.meta)
                        res.metadata_updated += 1
                        out(f"{tag}: данные обновлены")
                    else:
                        out(f"{tag}: без изменений" + (" (данные в CSV отличаются — нужен --update-metadata)" if sp.meta_changed else ""))
                    res.unchanged += 1
                    continue
                vectors = []
                for chunk in _batches(sp.new_photos, batch_size):
                    vectors.append(np.atleast_2d(embedder.embed([load(c.path) for c in chunk])))
                emb = np.vstack(vectors)
                # Эмбеддинги уже посчитаны — только теперь трогаем БД: упавшее вычисление не оставляет «пустой» товар.
                if sp.action == "create":
                    product_id = repo.create_product(sp.meta)
                else:
                    product_id = sp.product_id or manifest.products[sp.sku]
                    if sp.meta_changed and update_metadata:
                        repo.update_product(product_id, sp.meta)
                        res.metadata_updated += 1
                if remember is not None:
                    remember(product_id, sp.sku, sp.new_photos, emb)
                index.add(product_id, emb, [f"import:{c.sha256}" for c in sp.new_photos])   # источник — фото по SHA-256
                res.created += sp.action == "create"          # счётчики — только после успешного добавления в индекс
                res.extended += sp.action != "create"
                manifest.products[sp.sku] = product_id
                for c in sp.new_photos:
                    manifest.photos[c.sha256] = {"sku": sp.sku, "file": c.path.name, "product_id": product_id}
                res.photos_added += len(sp.new_photos)
                res.embeddings[sp.sku] = emb
                out(f"{tag}: {'создан' if sp.action == 'create' else 'добавлены фото'}, +{len(sp.new_photos)} фото")
                since_checkpoint += 1
                if since_checkpoint >= checkpoint_every:
                    checkpoint()
                    since_checkpoint = 0
            except Exception as exc:    # noqa: BLE001 — один сбойный товар не должен останавливать остальные
                res.failed.append(f"{sp.sku}: {exc.__class__.__name__}: {exc}")
                out(f"{tag}: ОШИБКА {exc.__class__.__name__}: {exc}")
    except KeyboardInterrupt:
        res.interrupted = True
        out("\nПрервано: сохраняю то, что уже загружено...")
    finally:
        checkpoint()
    return res


# --------------------------------------------------------------------------- отчёт о каталоге


@dataclass
class CatalogReport:
    confusable: list[tuple[str, str, float]]               # (SKU A, SKU B, максимальное сходство фото)
    loo_accuracy: float | None                              # доля фото, чьё ближайшее ДРУГОЕ фото — того же товара
    worst: list[tuple[str, float]]                          # SKU с худшей отделимостью
    skipped_reason: str | None = None


def catalog_report(embeddings: dict[str, np.ndarray], confusable_threshold: float = CONFUSABLE_THRESHOLD,
                   top: int = 10) -> CatalogReport:
    """По эмбеддингам, загруженным в этот запуск: какие товары система будет путать (похожи фото) и насколько каталог
    «самоотделим» (leave-one-out 1-NN). Это ПРЕДСКАЗАНИЕ по эталонам — не замена оценке на фото полок."""
    skus = [s for s, e in embeddings.items() if len(np.atleast_2d(e))]
    total = sum(len(np.atleast_2d(embeddings[s])) for s in skus)
    if len(skus) < 2:
        return CatalogReport([], None, [], "для сравнения нужно минимум 2 товара")
    if total > MAX_PHOTOS_FOR_SELF_CHECK:
        return CatalogReport([], None, [], f"слишком много фото для самопроверки ({total} > {MAX_PHOTOS_FOR_SELF_CHECK})")
    mats = [np.atleast_2d(embeddings[s]).astype(np.float32) for s in skus]
    owner = np.concatenate([np.full(len(m), i) for i, m in enumerate(mats)])
    allv = np.vstack(mats)
    sims = allv @ allv.T
    np.fill_diagonal(sims, -np.inf)
    n_sku = len(skus)
    # похожие пары: максимум по фото A x фото B
    pair_max = np.full((n_sku, n_sku), -np.inf)
    for i in range(n_sku):
        for j in range(i + 1, n_sku):
            pair_max[i, j] = sims[np.ix_(owner == i, owner == j)].max()
    pairs = [(skus[i], skus[j], float(pair_max[i, j])) for i in range(n_sku) for j in range(i + 1, n_sku)
             if pair_max[i, j] >= confusable_threshold]
    pairs.sort(key=lambda t: t[2], reverse=True)
    # leave-one-out: только фото тех товаров, у которых есть второе фото
    counts = np.bincount(owner, minlength=n_sku)
    eligible = np.where(counts[owner] >= 2)[0]
    nearest = sims.argmax(axis=1)
    hits = owner[nearest] == owner
    loo = float(hits[eligible].mean()) if len(eligible) else None
    per_sku = []
    for i in range(n_sku):
        mine = np.where((owner == i))[0]
        if counts[i] >= 2:
            per_sku.append((skus[i], float(hits[mine].mean())))
    worst = sorted([p for p in per_sku if p[1] < 1.0], key=lambda t: t[1])[:top]
    return CatalogReport(pairs[:top], loo, worst)


def format_catalog_report(rep: CatalogReport, confusable_threshold: float = CONFUSABLE_THRESHOLD) -> str:
    if rep.skipped_reason:
        return f"Самопроверка каталога пропущена: {rep.skipped_reason}."
    lines = []
    if rep.loo_accuracy is not None:
        lines.append(f"Самопроверка каталога: ближайшее другое фото принадлежит тому же товару в {rep.loo_accuracy:.0%} случаев")
    if rep.worst:
        lines.append("Хуже всего отделяются: " + ", ".join(f"{s} ({a:.0%})" for s, a in rep.worst))
    if rep.confusable:
        lines.append(f"Похожие товары (сходство фото ≥ {confusable_threshold:.2f}) — система будет сомневаться между ними и слать в ревью:")
        lines += [f"  {a}  ~  {b}   ({sc:.3f})" for a, b, sc in rep.confusable]
        lines.append("Совет: добавьте для этих товаров фото с ракурсами, где видно отличие (вкус, объём, цвет крышки), и включите "
                     "правило запаса margin_threshold (docs/pilot_plan.md).")
    elif rep.loo_accuracy is not None:
        lines.append("Явно похожих товаров среди загруженных не найдено.")
    return "\n".join(lines)
