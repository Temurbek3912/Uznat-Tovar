"""
«Добавить товар» без терминала: приём эталонных фото из браузера. Модуль чистый — без FastAPI, SQLAlchemy и моделей (проверяется
офлайн, tests/test_catalog_upload.py); API (app/api/routes_catalog.py) и воркер (app/worker.py) только вызывают его функции.

Что здесь решено и почему.
  * Человек вводит только НАЗВАНИЕ (+ фото). Артикул (SKU) строится из названия сам: «Кола 0,5 л» -> «KOLA-0.5-L»; занят — «-2», «-3».
  * Фото из браузера не доверяем: открываем сами, применяем EXIF-поворот (load_rgb), сжимаем до 1600 px и сохраняем заново JPEG —
    так в хранилище не попадают чужие форматы, огромные файлы и метаданные (GPS телефона).
  * Файлы лежат в data/catalog-uploads/<артикул>/NNN-<хеш>.jpg. Воркер получает не путь, а ССЫЛКУ «upload:<папка>/<файл>» и
    разрешает её ТОЛЬКО внутри этой папки (защита от «../» и символьных ссылок). Одинаковое фото дважды не сохраняется (хеш в имени).
  * Все ошибки для человека — на русском, простыми словами, в исключениях CatalogAddError (+ PhotoError, InputError): API превращает
    их в ответ 4xx, воркер — в понятную причину провала БЕЗ бессмысленных повторов.
Схему БД не меняем: число фото товара и миниатюра берутся из этой папки.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from app.core.app_errors import AppError, code_of, message_of, params_of
from app.pipeline.index_store import replace_with_retry

UPLOAD_SUBDIR = "catalog-uploads"          # внутри media_root (по умолчанию data/)
IDENTIFY_SUBDIR = "identify-tmp"           # временные кадры для «Узнать товар»
UPLOAD_REF_PREFIX = "upload:"
IDENTIFY_REF_PREFIX = "identify:"

MAX_PHOTOS_PER_REQUEST = 8                 # за один раз
MAX_PHOTOS_PER_PRODUCT = 20                # всего у одного товара
MAX_SIDE = 1600                            # длинная сторона после сжатия
THUMB_SIDE = 240
MIN_SIDE = 64                              # короче — фото бесполезно (как в catalog_import)
SMALL_SIDE = 224                           # DINOv2 приводит вход к 224 px: меньшее размывается
MAX_PIXELS = 60_000_000                    # защита от «бомб»: фото больше не раскрываем
JPEG_QUALITY = 92
MAX_NAME_LEN = 120
MAX_TEXT_LEN = 60                          # бренд, категория
MAX_SKU_LEN = 40
MAX_PRICE = 1_000_000_000.0
DEFAULT_BRAND, DEFAULT_CATEGORY = "Не указан", "Без категории"      # те же, что в catalog_import

_PHOTO_NAME = re.compile(r"^(\d{3})-([0-9a-f]{12})\.jpg$")
_SAFE_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}


# --------------------------------------------------------------------------- ошибки (текст — для человека)


class CatalogAddError(AppError):
    """Понятная человеку причина, почему товар/фото не добавлены. Повтор задачи не поможет.

    Единая система ошибок (app/core/app_errors.py): code/params — для перевода сообщения на язык приложения (ключ server.<code>
    в словарях страниц), текст по-русски — сообщение исключения. Вид — invalid (HTTP 422)."""

    kind = "invalid"


class InputError(CatalogAddError):
    """Неверно заполнена форма (название, цена…)."""


class PhotoError(CatalogAddError):
    """С фото что-то не так."""


class NotReadyError(CatalogAddError):
    """Система распознавания (модели, индекс) сейчас не готова. Вид unavailable (HTTP 503): повтор позже может помочь."""

    kind = "unavailable"
    default_code = "ml_not_ready"


_COMPONENT_NAME_RE = re.compile(r"^([a-z][a-z0-9_]{0,31}):")


def not_ready_error(problems: list[str]) -> NotReadyError:
    """Отказ «распознавание не готово» для страницы: только ИМЕНА компонентов (embedder, matcher…). Сами причины
    («OSError: … /root/.cache/…», путь к весам, текст исключения) клиенту не уходят — их пишет в журнал воркер."""
    names = []
    for problem in problems:
        match = _COMPONENT_NAME_RE.match(str(problem))
        if match and match.group(1) not in names:
            names.append(match.group(1))
    which = f" (не готовы: {', '.join(names)})" if names else ""
    return NotReadyError(f"Система распознавания сейчас не готова{which}. Сообщите администратору (docker compose logs worker).",
                         params={"components": ", ".join(names)} if names else None)


# --------------------------------------------------------------------------- текст: название, цена


# Поле формы по-русски -> код поля для перевода сообщения (server.field_<код> в словарях страниц).
_FIELD_CODES = {"название товара": "name", "бренд": "brand", "категорию": "category"}


def _field_param(label: str) -> dict:
    code = _FIELD_CODES.get(label)
    return {"key": f"server.field_{code}", "params": {}} if code else label   # неизвестное поле — как есть


def clean_text(raw: object, *, label: str, max_len: int, required: bool = False, default: str = "") -> str:
    """Убирает лишние пробелы и управляющие символы; пустое -> default (или ошибка, если поле обязательное)."""
    text = "" if raw is None else str(raw)
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        if required:
            raise InputError(f"Напишите {label}.", code="field_required", params={"field": _field_param(label)})
        return default
    if len(text) > max_len:
        raise InputError(f"Слишком длинное {label}: не больше {max_len} символов.", code="field_too_long",
                         params={"field": _field_param(label), "max": max_len})
    return text


def clean_name(raw: object) -> str:
    return clean_text(raw, label="название товара", max_len=MAX_NAME_LEN, required=True)


def _price_from_text(text: str) -> float:
    """Текст без пробелов -> число. Одиночный «,» или «.» — десятичный знак; повторяющийся — разделитель тысяч;
    если есть оба — десятичный тот, что стоит правее («1.200,50», «1,200.50»)."""
    if "," in text and "." in text:
        decimal = "," if text.rfind(",") > text.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        text = text.replace(thousands, "").replace(decimal, ".")
    elif text.count(",") > 1 or text.count(".") > 1:
        if not re.fullmatch(r"\d{1,3}(?:([.,])\d{3})(?:\1\d{3})+", text):       # 1.200.500 — тысячи; «12..3» — мусор
            raise ValueError(text)
        text = text.replace(",", "").replace(".", "")
    else:
        text = text.replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d+)?", text):
        raise ValueError(text)
    return float(text)


def parse_price(raw: object) -> float | None:
    """«9900», «9 900», «99,90», «99.90», «9900 сум» -> число; пусто -> None (цена не задана). Ноль и минус — ошибка."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise InputError("Цена — это число, например 9900.", code="price_not_number")
    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        text = str(raw).replace("\u00a0", " ").replace("\u202f", " ").strip()
        text = re.sub(r"\s*(сум|сўм|so'm|som|uzs)\.?$", "", text, flags=re.I).strip()
        if not text:
            return None
        try:
            value = _price_from_text(text.replace(" ", ""))
        except ValueError:
            raise InputError("Цена — это число, например 9900 или 99,90. Если цену не знаете — оставьте поле пустым.",
                             code="price_not_number") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise InputError("Цена — это число, например 9900.", code="price_not_number")
    if value <= 0:
        raise InputError("Цена должна быть больше нуля. Если цену не знаете — оставьте поле пустым.", code="price_not_positive")
    if value > MAX_PRICE:
        raise InputError("Слишком большая цена — проверьте, нет ли лишних нулей.", code="price_too_large")
    return round(value, 2)


# --------------------------------------------------------------------------- артикул из названия

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "ў": "u", "қ": "q", "ғ": "g", "ҳ": "h",                                   # узбекская кириллица
}


def make_slug(name: str) -> str:
    """Название -> безопасный артикул: латиница, цифры и . _ + -, верхний регистр, без пробелов."""
    text = name.lower()
    text = re.sub(r"(?<=\d),(?=\d)", ".", text)                               # 0,5 -> 0.5
    text = "".join(_TRANSLIT.get(ch, ch) for ch in text)
    text = re.sub(r"[^a-z0-9.+_-]+", "-", text)
    text = re.sub(r"-{2,}", "-", re.sub(r"\.{2,}", ".", text)).strip("-.")
    text = text[:MAX_SKU_LEN].rstrip("-.")
    return text.upper() or "TOVAR"


def make_sku(name: str, taken: Iterable[str]) -> str:
    """Свободный артикул: KOLA-0.5-L, а если занят (без учёта регистра) — KOLA-0.5-L-2, -3…"""
    base = make_slug(name)
    busy = {t.lower() for t in taken}
    if base.lower() not in busy:
        return base
    for n in range(2, 10_000):
        suffix = f"-{n}"
        candidate = base[: MAX_SKU_LEN - len(suffix)].rstrip("-.") + suffix
        if candidate.lower() not in busy:
            return candidate
    raise InputError("Не удалось подобрать код для товара — измените название.", code="sku_generation_failed")


def normalize_name(name: str) -> str:
    """Для поиска повторов: без регистра, ё=е, лишние пробелы и знаки не мешают."""
    text = name.casefold().replace("ё", "е")
    text = re.sub(r"(?<=\d),(?=\d)", ".", text)
    return re.sub(r"[^0-9a-zа-яўқғҳ.]+", " ", text).strip()


def find_duplicate(name: str, rows: Iterable[tuple[str, str]]) -> tuple[str, str] | None:
    """(id, название) уже существующего товара с таким же названием — или None."""
    wanted = normalize_name(name)
    if not wanted:
        return None
    for product_id, existing in rows:
        if normalize_name(existing) == wanted:
            return product_id, existing
    return None


# --------------------------------------------------------------------------- фото


@dataclass(frozen=True)
class NormalizedPhoto:
    jpeg: bytes
    width: int
    height: int
    sha12: str
    small: bool          # короткая сторона меньше 224 px
    blurry: bool         # размытое или однотонное

    def warning(self) -> str | None:
        if self.blurry:
            return "Фото получилось размытым или однотонным — лучше переснять."
        if self.small:
            return "Фото маленькое — лучше снять крупнее и ближе."
        return None


def _is_heic(data: bytes) -> bool:
    return len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in _HEIC_BRANDS


def fitted_size(width: int, height: int, max_side: int = MAX_SIDE) -> tuple[int, int]:
    """Размер после сжатия: длинная сторона не больше max_side, пропорции сохраняются."""
    longest = max(width, height)
    if longest <= max_side:
        return width, height
    scale = max_side / longest
    return max(1, round(width * scale)), max(1, round(height * scale))


def encode_jpeg(rgb, max_side: int = MAX_SIDE) -> bytes:
    """RGB-массив -> JPEG; длинную сторону ужимает до max_side."""
    from PIL import Image

    image = Image.fromarray(rgb)
    size = fitted_size(image.width, image.height, max_side)
    if size != image.size:
        image = image.resize(size, Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return buffer.getvalue()


def normalize_photo(data: bytes, *, check_quality: bool = True) -> NormalizedPhoto:
    """Байты от клиента -> проверенный JPEG. Непригодное фото — PhotoError с понятным текстом."""
    from PIL import Image

    from app.pipeline.imageio import load_rgb

    if not data:
        raise PhotoError("Файл пустой — выберите фото ещё раз.", code="photo_empty")
    if _is_heic(data):
        raise PhotoError(
            "Фото в формате HEIC (так снимает iPhone) не поддерживается. Снимите прямо на этой странице или "
            "в настройках камеры выберите «Наиболее совместимый» формат.", code="photo_heic"
        )
    try:
        with Image.open(io.BytesIO(data)) as probe:  # exif-ok: только размер до раскрытия (защита от огромных файлов)
            width, height = probe.size
    except Exception:  # noqa: BLE001 — любой отказ декодера = это не фото
        raise PhotoError("Не удалось открыть файл как фото. Подойдут JPG, PNG или WEBP.", code="photo_not_image") from None
    if width * height > MAX_PIXELS:
        raise PhotoError("Фото слишком большое. Снимите в обычном размере или уменьшите его.", code="photo_too_many_pixels")
    try:
        rgb = load_rgb(data)
    except Exception:  # noqa: BLE001
        raise PhotoError("Фото повреждено и не читается — выберите другое.", code="photo_corrupted") from None
    height, width = rgb.shape[:2]
    if min(height, width) < MIN_SIDE:
        raise PhotoError(f"Фото слишком маленькое ({width}×{height}). Снимите крупнее.", code="photo_too_small",
                         params={"width": width, "height": height})
    blurry = False
    if check_quality:
        from app.services.catalog_import import MIN_SHARPNESS_WARN, measure_sharpness

        blurry = measure_sharpness(rgb) < MIN_SHARPNESS_WARN
    jpeg = encode_jpeg(rgb)
    stored_width, stored_height = fitted_size(width, height)           # размер СОХРАНЁННОГО файла, а не исходного
    return NormalizedPhoto(
        jpeg=jpeg, width=stored_width, height=stored_height, sha12=hashlib.sha256(jpeg).hexdigest()[:12],
        small=min(height, width) < SMALL_SIDE, blurry=blurry,
    )


def make_thumbnail(jpeg: bytes, side: int = THUMB_SIDE) -> bytes:
    """Маленькая копия для списка товаров."""
    from app.pipeline.imageio import load_rgb

    return encode_jpeg(load_rgb(jpeg), max_side=side)


def cached_thumbnail(source: Path) -> bytes:
    """Миниатюра фото-файла с кэшем рядом (thumb-<3 знака имени>.jpg): нет кэша — пересчитать и попробовать сохранить.
    Кэш — удобство, не обязанность: не записался — просто пересчитаем в следующий раз."""
    cached = source.with_name(f"thumb-{source.name[:3]}.jpg")
    if cached.is_file():
        return cached.read_bytes()
    content = make_thumbnail(source.read_bytes())
    try:
        cached.write_bytes(content)
    except OSError:
        pass
    return content


# --------------------------------------------------------------------------- папки и ссылки


def uploads_root(media_root: Path) -> Path:
    return Path(media_root) / UPLOAD_SUBDIR


def identify_root(media_root: Path) -> Path:
    return Path(media_root) / IDENTIFY_SUBDIR


def folder_name(sku: str) -> str:
    """Папка товара. Нормальные артикулы остаются как есть; необычные (кириллица, пробелы, «..») — детерминированный хеш."""
    if _SAFE_PART.match(sku) and ".." not in sku and len(sku) <= 80:
        return sku
    return "x-" + hashlib.sha256(sku.encode("utf-8")).hexdigest()[:16]


def list_photo_files(root: Path, sku: str) -> list[Path]:
    """Файлы эталонных фото товара по порядку добавления (миниатюра и прочее служебное не считается)."""
    folder = Path(root) / folder_name(sku)
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and _PHOTO_NAME.match(p.name))


def upload_ref(folder: str, filename: str) -> str:
    return f"{UPLOAD_REF_PREFIX}{folder}/{filename}"


def is_upload_ref(ref: str) -> bool:
    return isinstance(ref, str) and ref.startswith(UPLOAD_REF_PREFIX)


def is_identify_ref(ref: str) -> bool:
    return isinstance(ref, str) and ref.startswith(IDENTIFY_REF_PREFIX)


def _atomic_write(path: Path, data: bytes) -> None:
    """Запись целиком или никак. Имя временного файла уникально: раньше оно было одно на файл («.part»), и два одновременных
    запроса с одним и тем же фото (двойное нажатие «Сохранить») писали в один временный файл — один мог переименовать
    недописанный файл другого. Временный файл сбрасывается на диск до замены и удаляется при ошибке (например, диск полон)."""
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:12]}.part")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        # Windows: замена файла, который в этот момент заменяет другой запрос (или держит антивирус), даёт PermissionError —
        # повторяем (тот же механизм, что у индекса). Найдено первым прогоном тестов на Windows 2026-11-13.
        replace_with_retry(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


@dataclass
class SavedPhotos:
    refs: list[str]          # ссылки на ВСЕ фото из пачки (в том же порядке), включая уже лежавшие в папке
    new_files: int           # сколько файлов действительно записано
    already_had: int         # сколько фото из пачки уже было (такое же — по хешу)


def save_photos(root: Path, sku: str, photos: list[NormalizedPhoto]) -> SavedPhotos:
    """Записывает фото товара. Одинаковое фото (по хешу) второй раз не пишется — но ссылка на него возвращается: так повторная
    попытка после сбоя находит свои файлы и не плодит копии."""
    if not photos:
        raise PhotoError("Добавьте хотя бы одно фото товара.", code="photo_required")
    folder = Path(root) / folder_name(sku)
    existing = list_photo_files(root, sku)
    by_hash = {m.group(2): p.name for p in existing if (m := _PHOTO_NAME.match(p.name))}
    fresh = len({p.sha12 for p in photos} - set(by_hash))
    if len(existing) + fresh > MAX_PHOTOS_PER_PRODUCT:
        raise PhotoError(f"У одного товара может быть не больше {MAX_PHOTOS_PER_PRODUCT} фото — сейчас уже {len(existing)}.",
                         code="photos_per_product_limit", params={"max": MAX_PHOTOS_PER_PRODUCT, "existing": len(existing)})
    folder.mkdir(parents=True, exist_ok=True)
    next_no = max((int(m.group(1)) for p in existing if (m := _PHOTO_NAME.match(p.name))), default=0) + 1
    refs: list[str] = []
    new_files = already = 0
    for photo in photos:
        name = by_hash.get(photo.sha12)
        if name is None:
            name = f"{next_no:03d}-{photo.sha12}.jpg"
            _atomic_write(folder / name, photo.jpeg)
            by_hash[photo.sha12] = name
            next_no += 1
            new_files += 1
        else:
            already += 1
        refs.append(upload_ref(folder.name, name))
    return SavedPhotos(refs=refs, new_files=new_files, already_had=already)


def resolve_ref(root: Path, prefix: str, ref: str) -> Path:
    """«<prefix><папка>/<файл>» (или «<prefix><файл>» для одиночных) -> путь ТОЛЬКО внутри root. Иначе PhotoError."""
    if not isinstance(ref, str) or not ref.startswith(prefix):
        raise PhotoError("Неверная ссылка на фото.", code="photo_bad_ref")
    parts = ref[len(prefix):].split("/")
    if not parts or any(not _SAFE_PART.match(part) or ".." in part for part in parts):
        raise PhotoError("Неверная ссылка на фото.", code="photo_bad_ref")
    base = Path(root).resolve()
    target = base.joinpath(*parts).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise PhotoError("Неверная ссылка на фото.", code="photo_bad_ref") from None
    if not target.is_file():
        raise PhotoError("Файл с фото не найден на сервере — загрузите фото заново.", code="photo_file_missing")
    return target


def resolve_upload_ref(root: Path, ref: str) -> Path:
    if is_upload_ref(ref) and len(ref[len(UPLOAD_REF_PREFIX):].split("/")) != 2:
        raise PhotoError("Неверная ссылка на фото.", code="photo_bad_ref")
    return resolve_ref(root, UPLOAD_REF_PREFIX, ref)


# --------------------------------------------------------------------------- временные кадры «Узнать товар»


def save_identify_frame(root: Path, token: str, jpeg: bytes) -> str:
    """Кадр для распознавания -> файл во временной папке; возвращает ссылку «identify:<токен>.jpg»."""
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise PhotoError("Внутренняя ошибка: неверный идентификатор кадра.")
    Path(root).mkdir(parents=True, exist_ok=True)
    name = f"{token}.jpg"
    _atomic_write(Path(root) / name, jpeg)
    return f"{IDENTIFY_REF_PREFIX}{name}"


def sweep_old_files(root: Path, *, max_age_seconds: float, now: float) -> int:
    """Удаляет во временной папке файлы старше max_age_seconds (забытые кадры). Возвращает, сколько удалено."""
    folder = Path(root)
    if not folder.is_dir():
        return 0
    removed = 0
    for path in folder.iterdir():
        try:
            if path.is_file() and now - path.stat().st_mtime > max_age_seconds:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# --------------------------------------------------------------------------- журнал проиндексированных фото (для восстановления индекса)

MANIFEST_NAME = "uploads_manifest.json"


def manifest_path_for(index_path: str | Path) -> Path:
    """Журнал лежит рядом с индексом каталога (models/index/): их удаляют и сохраняют вместе."""
    return Path(index_path).parent / MANIFEST_NAME


def manifest_key(ref_or_key: str) -> str:
    """«upload:KOLA/001-abc.jpg» -> «KOLA/001-abc.jpg» (ключ журнала: папка/файл; хеш в имени однозначно задаёт фото)."""
    return ref_or_key[len(UPLOAD_REF_PREFIX):] if ref_or_key.startswith(UPLOAD_REF_PREFIX) else ref_or_key


class UploadsManifest:
    """Какие загруженные из браузера фото уже превращены в вектора индекса. Индекс не умеет удалять и не помнит, чьи это фото;
    без журнала после пересборки индекса фото из data/catalog-uploads пришлось бы искать вручную."""

    def __init__(self, path: Path, keys: set[str] | None = None):
        self.path, self.keys = Path(path), set(keys or ())

    @classmethod
    def load(cls, path: Path) -> "UploadsManifest":
        path = Path(path)
        if not path.exists():
            return cls(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            keys = data["photos"]
            if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
                raise ValueError("photos")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CatalogAddError(f"Журнал {path} повреждён. Удалите его (и файлы индекса — тогда фото проиндексируются заново) и повторите.") from exc
        return cls(path, set(keys))

    def has(self, key: str) -> bool:
        return manifest_key(key) in self.keys

    def add(self, keys: Iterable[str]) -> None:
        self.keys.update(manifest_key(k) for k in keys)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.path, json.dumps({"version": 1, "photos": sorted(self.keys)}, ensure_ascii=False, indent=1).encode("utf-8"))


def not_yet_indexed(index_path: str | Path, refs: Iterable[str]) -> list[str]:
    """Какие из ссылок ещё не превращены в векторы (по журналу рядом с индексом). Повтор задачи после удачного сохранения
    (Celery выдаёт задачу снова, если процесс погиб до подтверждения) и повторная загрузка того же фото не плодят дубли
    векторов. Журнал не читается — считаем, что ничего не проиндексировано: лишний вектор безопаснее пропущенного фото.
    Ссылки не на загруженные фото (не upload:…) журнал не ведёт — они возвращаются всегда."""
    refs = list(refs)
    try:
        manifest = UploadsManifest.load(manifest_path_for(index_path))
    except (CatalogAddError, OSError):
        return refs
    return [r for r in refs if not (is_upload_ref(r) and manifest.has(r))]


def record_indexed(index_path: str | Path, refs: Iterable[str]) -> bool:
    """Отмечает фото как проиндексированные. Журнал — удобство, а не обязательное условие добавления товара: любой сбой здесь
    (нет прав, журнал повреждён) НЕ должен ронять добавление — возвращаем False, а скрипт восстановления потом доиндексирует."""
    try:
        manifest = UploadsManifest.load(manifest_path_for(index_path))
        manifest.add(refs)
        manifest.save()
        return True
    except (CatalogAddError, OSError):
        return False


@dataclass
class ReindexItem:
    product_id: str
    sku: str
    name: str
    files: list[Path]


def plan_reindex(products: Iterable[tuple[str, str, str]], root: Path, manifest: UploadsManifest) -> list[ReindexItem]:
    """Какие фото из data/catalog-uploads ещё не в индексе: (id, артикул, название) товаров из БД -> что доиндексировать.
    Папки без товара в БД игнорируются (человек мог убрать товар из базы: векторы такого товара не нужны)."""
    items: list[ReindexItem] = []
    for product_id, sku, name in products:
        todo = [f for f in list_photo_files(root, sku) if not manifest.has(f"{folder_name(sku)}/{f.name}")]
        if todo:
            items.append(ReindexItem(product_id, sku, name, todo))
    return items


# --------------------------------------------------------------------------- состояние задачи для интерфейса

GENERIC_FAILURE = "Не получилось добавить товар. Попробуйте ещё раз; если не выходит снова — сообщите администратору."


def describe_task(status: str, result: object) -> dict:
    """Статус Celery -> простое состояние для человека: working / done / failed + сообщение.
    detail — техническая причина «для специалиста» (показывается свёрнуто)."""
    if status == "SUCCESS":
        return {"state": "done", "message": "Готово: товар добавлен.", "product_id": result if isinstance(result, str) else None, "detail": None}
    if status in ("FAILURE", "REVOKED"):
        # Наш текст (AppError) показывается как есть; текст любого другого исключения клиенту НЕ отдаётся (там бывают пути,
        # SQL, адреса): только общий текст и код, подробности — в журнале воркера.
        is_ours = isinstance(result, AppError) or type(result).__name__ in {"CatalogAddError", "PhotoError", "InputError", "NotReadyError"}
        text = message_of(result) if is_ours else ""
        message = text if text else GENERIC_FAILURE
        code = (code_of(result) or "catalog_error") if is_ours and text else "task_failed"
        return {"state": "failed", "message": message, "product_id": None, "detail": None, "code": code,
                "params": params_of(result) if is_ours and text else {}}
    return {"state": "working", "message": "Система изучает фото…", "product_id": None, "detail": None}
