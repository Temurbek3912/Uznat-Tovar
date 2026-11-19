"""
Тестовый каталог магазина из открытых баз (Open Food / Beauty / Products Facts): товары, которые продаются в стране, — с названием,
брендом, видом товара (категорией), штрихкодом и фото. Папка получается в формате scripts/import_catalog.py.

    python -m scripts.fetch_open_catalog --country uzbekistan --barcode-prefix 478 --out data/test-catalog-uz
    python -m scripts.import_catalog --dir data/test-catalog-uz
    python -m scripts.fetch_open_catalog --link-barcodes --out data/test-catalog-uz      # штрихкоды -> товары в базе
    python -m scripts.fetch_open_catalog --more russia,kazakhstan,turkey --total 200 --out data/test-catalog-more  # ещё, разных видов
    python -m scripts.fetch_open_catalog --rich pepsi,coca-cola,fanta --alcohol --min-photos 6 --total 20 --out D:/uznat_tovar/catalog-sets/rich
                                                                    # пробный набор «много фото»: до 15 фото на товар
    python -m scripts.fetch_open_catalog --popular 480 --countries uzbekistan,russia,kazakhstan --out D:/uznat_tovar/catalog-sets/popular-500
                                                                    # каталог ~500 товаров: лицевая сторона первой, без снимков состава

Только для проверки: фото в открытых базах сняты покупателями, по 1–5 на товар, 400 px. Цен в открытых базах почти нет — цена пустая.
Артикул каждого товара — «UZ-<штрихкод>» (по приставке тестовые товары легко найти и убрать). Повторный запуск докачивает недостающее.
"""
from __future__ import annotations

import argparse
import csv
import html
import re
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

from app.services.open_product_details import ALCOHOL_CATEGORY, TOBACCO_CATEGORY
from app.services.open_name_tidy import name_doubts, tidy_brand, tidy_name

USER_AGENT = "UznatTovar/1.0 (store product lookup)"
SEARCH = "https://search.openfoodfacts.org/search"          # поиск по всей Open Food Facts (обычный /api/v2/search бывает недоступен)
OTHER_BASES = ("https://world.openbeautyfacts.org", "https://world.openproductsfacts.org")
IMAGE_HOSTS = {
    "off": "https://images.openfoodfacts.org", "obf": "https://images.openbeautyfacts.org",
    "opf": "https://images.openproductsfacts.org",
}
FIELDS = "code,product_name,product_name_ru,product_name_uz,product_name_en,brands,quantity,categories_tags,images"
MAX_PHOTOS = 5
RICH_MAX_PHOTOS = 15            # --rich: сколько фото брать у товара, где их много (цель владельца — 10–15 на товар)
ALCOHOL_TAGS = ("en:alcoholic-beverages", "en:beers", "en:wines", "en:spirits", "en:vodkas", "en:ciders", "en:liqueurs")


def is_alcohol_tags(tags) -> bool:
    """Метки открытой базы говорят «алкоголь» (безалкогольное пиво там помечено en:non-alcoholic-beverages — не считаем)."""
    tags = [str(t) for t in tags or []]
    return any(t in ALCOHOL_TAGS for t in tags) and "en:non-alcoholic-beverages" not in tags and "en:alcohol-free-beers" not in tags
LANG_ORDER = ("ru", "uz", "en", "")


def _get_json(url: str, timeout: float = 60.0, tries: int = 3) -> dict:
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception:  # noqa: BLE001
            if attempt == tries - 1:
                raise
            time.sleep(3 * (attempt + 1))
    return {}


def _image_dir(code: str) -> str:
    return "/".join([code[0:3], code[3:6], code[6:9], code[9:]]) if len(code) > 8 else code


def photo_ids(images: dict, limit: int = MAX_PHOTOS) -> list[str]:
    """Номера фото товара: сначала выбранные «лицевая сторона», затем остальные загруженные (по порядку), без повторов."""
    images = images or {}
    first = [str(v.get("imgid")) for k, v in images.items() if k.startswith("front") and isinstance(v, dict) and v.get("imgid")]
    rest = sorted((k for k in images if k.isdigit()), key=int)
    out: list[str] = []
    for i in first + rest:
        if i not in out and i in images:
            out.append(i)
    return out[:limit]


def _text(value) -> str:
    return re.sub(r"\s+", " ", html.unescape(html.unescape(str(value or "")))).strip(" ;,")


def good_name(name: str) -> bool:
    """Название, а не мусор: хотя бы 3 буквы (не «3913», не «1»)."""
    return len(re.findall(r"[^\W\d_]", name)) >= 3


def product_name(p: dict) -> str:
    for lang in LANG_ORDER:
        name = tidy_name(_text(p.get(f"product_name_{lang}" if lang else "product_name")))   # без состава, калорий, цены
        if good_name(name):
            qty = _text(p.get("quantity"))
            return f"{name} {qty}".strip() if qty and qty.lower() not in name.lower() else name
    return ""


def brand_of(p: dict) -> str:
    brands = p.get("brands") or ""
    if isinstance(brands, list):
        brands = ", ".join(brands)
    brand = tidy_brand(_text(str(brands).split(",")[0]))
    return brand if good_name(brand) else ""


def category_of(p: dict, first_photo: Path | None = None) -> str:
    """Категория из открытой базы; её нет — вид по первому фото малой моделью (app/pipeline/kind_classifier.py, порог из настроек).
    Алкоголь по меткам открытой базы — отдельная категория 21+ (иначе пиво попало бы в «Напитки»)."""
    from app.services.product_kinds import kind_for_categories  # noqa: PLC0415
    if is_alcohol_tags(p.get("categories_tags") or []):
        return ALCOHOL_CATEGORY
    kind = kind_for_categories(list(p.get("categories_tags") or []))
    if not kind and first_photo is not None:
        kind = _photo_kind(first_photo)
    return RU_KINDS.get(kind or "", "")


_classifier = None


def _photo_kind(path: Path) -> str:
    global _classifier
    try:
        from app.core.config import get_settings  # noqa: PLC0415
        from app.pipeline.kind_classifier import get_classifier  # noqa: PLC0415
        settings = get_settings()
        if _classifier is None:
            _classifier = get_classifier(settings) or False
        if not _classifier:
            return ""
        score = _classifier.classify_jpeg(path.read_bytes())
        return score.kind if score.kind and score.score >= settings.naming_kind_min_score else ""
    except Exception:  # noqa: BLE001 — вид — только удобство
        return ""


RU_KINDS = {
    "drink": "Напитки", "dairy": "Молочные продукты", "sweets": "Сладости", "snack": "Снеки", "tea_coffee": "Чай и кофе",
    "grocery": "Бакалея", "sauce": "Соусы и специи", "canned": "Консервы", "bakery": "Хлеб и выпечка",
    "meat_fish": "Мясо и рыба", "fruit_veg": "Овощи и фрукты", "frozen": "Заморозка", "baby": "Детские товары",
    "medicine": "Лекарства", "cosmetics": "Косметика и гигиена", "cleaning": "Бытовая химия", "home": "Товары для дома",
    "pet": "Товары для животных", "tobacco": TOBACCO_CATEGORY, "clothing": "Одежда", "footwear": "Обувь", "bags": "Сумки",
    "accessories": "Аксессуары", "electronics": "Электроника", "stationery": "Канцтовары", "toy": "Игрушки",
}


def search_off(query: str, sort_by: str = "", max_pages: int = 0) -> list[dict]:
    """Поиск по Open Food Facts; sort_by — например "-unique_scans_n" (сначала самые сканируемые = самые продаваемые);
    max_pages — не больше стольких страниц по 100 (0 — все)."""
    out, page = [], 1
    while True:
        params = {"q": query, "page_size": 100, "page": page, "fields": FIELDS}
        if sort_by:
            params["sort_by"] = sort_by
        data = _get_json(f"{SEARCH}?{urllib.parse.urlencode(params)}")
        hits = data.get("hits") or []
        out += [dict(h, _src="off") for h in hits]
        if page >= int(data.get("page_count") or 1) or not hits or (max_pages and page >= max_pages):
            return out
        page += 1
        time.sleep(1)


def search_other(base: str, country: str, src: str) -> list[dict]:
    out, page = [], 1
    while True:
        q = urllib.parse.urlencode({"countries_tags_en": country, "page_size": 100, "page": page, "fields": FIELDS})
        data = _get_json(f"{base}/api/v2/search?{q}")
        prods = data.get("products") or []
        out += [dict(p, _src=src) for p in prods]
        if len(out) >= int(data.get("count") or 0) or not prods:
            return out
        page += 1
        time.sleep(2)


def collect(country: str, prefix: str) -> list[dict]:
    found: dict[str, dict] = {}
    batches = [search_off(f'countries_tags:"en:{country}"')]
    if prefix:
        batches.append([p for p in search_off(f"code:{prefix}*") if len(str(p.get("code", ""))) == 13])
    for base, src in zip(OTHER_BASES, ("obf", "opf")):
        try:
            batches.append(search_other(base, country, src))
        except Exception as exc:  # noqa: BLE001
            print(f"  {base}: недоступна ({type(exc).__name__}) — пропускаю")
    for batch in batches:
        for p in batch:
            code = str(p.get("code") or "").strip()
            if code.isdigit() and len(code) in (8, 12, 13) and code not in found:
                found[code] = p
    return list(found.values())


def download(url: str, path: Path) -> bool:
    if path.is_file() and path.stat().st_size > 0:
        return True
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
    except Exception:  # noqa: BLE001
        return False
    if len(data) < 2000:
        return False
    path.write_bytes(data)
    return True


def _download_product(out: Path, p: dict, category: str | None = None, limit: int = MAX_PHOTOS) -> tuple[dict | None, int]:
    """Фото товара в папку out/UZ-<код>; строка для products.csv (или None — ни одного фото) и число фото."""
    code, name = str(p["code"]), product_name(p)
    ids = photo_ids(p.get("images") or {}, limit)
    if not name or not ids:
        return None, 0
    sku = f"UZ-{code}"
    folder = out / sku
    folder.mkdir(exist_ok=True)
    got = 0
    for i in ids:
        url = f"{IMAGE_HOSTS[p['_src']]}/images/products/{_image_dir(code)}/{i}.400.jpg"
        if download(url, folder / f"{i}.jpg"):
            got += 1
        time.sleep(0.15)
    if not got:
        folder.rmdir()
        return None, 0
    barcode = "0" + code if len(code) == 12 else code          # UPC-A хранится как EAN-13 с нулём (app/services/barcode.py)
    first = folder / f"{ids[0]}.jpg"
    if category is None:
        category = category_of(p, first if first.is_file() else None)
    return {"sku": sku, "name": name[:120].rstrip(), "brand": brand_of(p), "category": category, "price": "", "barcode": barcode}, got


def _write_lists(out: Path, rows: list[dict]) -> None:
    with (out / "products.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["sku", "name", "brand", "category", "price"], delimiter=";", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    with (out / "barcodes.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["sku", "barcode"])
        w.writerows([(r["sku"], r["barcode"]) for r in rows])
    write_names_to_check(out, rows)


def write_names_to_check(out: Path, rows: list[dict]) -> int:
    """names_to_check.csv — названия, которые стоит проверить глазами (опечатка, набор букв, описание вместо названия).
    Формат — как у scripts/fix_product_names.py: заполнить new_name/new_brand или action=archive и применить после одобрения."""
    path = out / "names_to_check.csv"
    doubtful = [(r, name_doubts(r["name"], r.get("brand") or "")) for r in rows]
    doubtful = [(r, why) for r, why in doubtful if why]
    if not doubtful:
        path.unlink(missing_ok=True)
        return 0
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sku", "action", "old_name", "new_name", "old_brand", "new_brand", "why"])
        w.writerows([(r["sku"], "rename", r["name"], "", r.get("brand") or "", "", "; ".join(why)) for r, why in doubtful])
    return len(doubtful)


def _done(out: Path, rows: list[dict], photos: int) -> None:
    print(f"Готово: {len(rows)} товаров, {photos} фото -> {out}")
    doubtful = write_names_to_check(out, rows)
    if doubtful:
        print(f"Проверить названия: {doubtful} — список {out / 'names_to_check.csv'} (исправить и применить "
              f"scripts/fix_product_names.py после одобрения)")


def pick_varied(candidates: list[tuple[str, dict]], total: int, per_kind: int) -> list[tuple[str, dict]]:
    """Разнообразный набор: по кругу по категориям (каждая не больше per_kind), всего не больше total; порядок внутри — как пришёл."""
    groups: dict[str, list[dict]] = {}
    for category, p in candidates:
        groups.setdefault(category, []).append(p)
    picked: list[tuple[str, dict]] = []
    rnd = 0
    while len(picked) < total and rnd < per_kind:
        added = False
        for category, items in groups.items():
            if rnd < len(items) and len(picked) < total:
                picked.append((category, items[rnd]))
                added = True
        if not added:
            break
        rnd += 1
    return picked


def fetch_more(out: Path, countries: list[str], total: int, per_kind: int, skip_codes: set[str]) -> None:
    """Ещё товары для тестового каталога: страны, где продаются, только с заполненным составом и проверенными фото,
    разных категорий. Уже загруженные штрихкоды (skip_codes) пропускаются."""
    from app.services.open_product_details import category_from_name  # noqa: PLC0415
    out.mkdir(parents=True, exist_ok=True)
    found: dict[str, dict] = {}
    for country in countries:
        q = f'countries_tags:"en:{country}" AND states_tags:"en:ingredients-completed" AND states_tags:"en:photos-validated"'
        batches = [search_off(q)]
        for base, src in zip(OTHER_BASES, ("obf", "opf")):
            try:
                batches.append(search_other(base, country, src))
            except Exception as exc:  # noqa: BLE001
                print(f"  {base}: недоступна ({type(exc).__name__}) — пропускаю")
        for batch in batches:
            for p in batch:
                code = str(p.get("code") or "").strip()
                norm = "0" + code if len(code) == 12 else code
                if code.isdigit() and len(code) in (8, 12, 13) and norm not in skip_codes and code not in found:
                    found[code] = p
        print(f"  {country}: всего кандидатов {len(found)}")
    candidates = []
    for p in found.values():
        name = product_name(p)
        if not name or not p.get("quantity") or not photo_ids(p.get("images") or {}):
            continue
        category = category_of(p, None) or category_from_name(name)
        if category:
            candidates.append((category, p))
    picked = pick_varied(candidates, total, per_kind)
    print(f"Подходящих {len(candidates)}, беру {len(picked)} разных категорий")
    rows, photos = [], 0
    for n, (category, p) in enumerate(picked, 1):
        row, got = _download_product(out, p, category)
        if row:
            rows.append(row)
            photos += got
        if n % 25 == 0:
            print(f"  {n}/{len(picked)}: товаров с фото {len(rows)}, фото {photos}")
    _write_lists(out, rows)
    _done(out, rows, photos)


def rich_queries(brands: list[str], countries: list[str], alcohol: bool) -> list[str]:
    """Запросы --rich: каждая марка в каждой стране; с alcohol — ещё пиво, водка, вино этих стран (категория 21+)."""
    out = [f'brands_tags:"{b}" AND countries_tags:"en:{c}"' for b in brands for c in countries]
    if alcohol:
        out += [f'categories_tags:"{t}" AND countries_tags:"en:{c}"' for c in countries for t in ("en:beers", "en:vodkas", "en:wines")]
    return out


def pick_rich(found: list[dict], min_photos: int, total: int) -> list[dict]:
    """Товары, у которых в открытой базе не меньше min_photos фото, — сначала те, где фото больше; не больше total."""
    ok = [p for p in found if product_name(p) and len(photo_ids(p.get("images") or {}, 10_000)) >= min_photos]
    ok.sort(key=lambda p: -len(photo_ids(p.get("images") or {}, 10_000)))
    return ok[:total]


def fetch_rich(out: Path, brands: list[str], countries: list[str], min_photos: int, total: int, alcohol: bool,
               skip_codes: set[str]) -> None:
    """Пробный набор «много фото на товар»: известные в Узбекистане марки (и алкоголь 21+), у которых в открытой базе
    не меньше min_photos фото; у каждого — до RICH_MAX_PHOTOS фото. Честно: таких товаров мало (docs/CATALOG_SOURCES.md)."""
    out.mkdir(parents=True, exist_ok=True)
    found: dict[str, dict] = {}
    for q in rich_queries(brands, countries, alcohol):
        try:
            batch = search_off(q)
        except Exception as exc:  # noqa: BLE001
            print(f"  запрос не прошёл ({type(exc).__name__}): {q}")
            continue
        for p in batch:
            code = str(p.get("code") or "").strip()
            norm = "0" + code if len(code) == 12 else code
            if code.isdigit() and len(code) in (8, 12, 13) and norm not in skip_codes and code not in found:
                found[code] = p
    picked = pick_rich(list(found.values()), min_photos, total)
    print(f"Кандидатов {len(found)}, с {min_photos}+ фото беру {len(picked)}")
    rows, photos = [], 0
    for p in picked:
        row, got = _download_product(out, p, None, RICH_MAX_PHOTOS)
        if row:
            if not row["category"]:
                from app.services.open_product_details import category_from_name  # noqa: PLC0415
                row["category"] = category_from_name(row["name"]) or "Без категории"
            rows.append(row)
            photos += got
            print(f"  {row['sku']}: {got} фото — {row['name']} [{row['category']}]")
    _write_lists(out, rows)
    _done(out, rows, photos)


TEXT_PHOTO_KINDS = ("ingredients", "nutrition", "packaging")      # снимки состава, таблицы, упаковки — в поиск по виду не берём
POPULAR_MAX_PHOTOS = 10


def shop_photos(images: dict, limit: int = POPULAR_MAX_PHOTOS) -> list[tuple[str, str]]:
    """Фото товара для каталога «как видит покупатель»: (имя файла, имя на сервере без «.400.jpg»).
    Первое — выбранная в базе ЛИЦЕВАЯ сторона, обрезанная по товару (front_<язык>.<ревизия>); затем загруженные снимки, кроме
    отмеченных в базе как состав, таблица калорий или упаковка. Замер 2026-11-18 (docs/benchmark/models_report_2026-11-18.md):
    из 23 снимков, на которых ошиблись все модели, 15 — состав, таблица или штрихкод; такие фото каталога только мешают.
    Без выбранной лицевой стороны — пусто (товар не берётся)."""
    images = images or {}
    fronts = sorted(k for k, v in images.items() if k.startswith("front_") and isinstance(v, dict) and v.get("imgid"))
    if not fronts:
        return []
    key = next((k for k in ("front_ru", "front_uz", "front_en") if k in fronts), fronts[0])
    rev = images[key].get("rev")
    out: list[tuple[str, str]] = [(f"{key}.jpg", f"{key}.{rev}" if rev else str(images[key]["imgid"]))]
    text_ids = {str(v.get("imgid")) for k, v in images.items()
                if isinstance(v, dict) and k.split("_")[0] in TEXT_PHOTO_KINDS and v.get("imgid")}
    for i in sorted((k for k in images if k.isdigit()), key=int):
        if i not in text_ids and len(out) < limit:
            out.append((f"{i}.jpg", i))
    return out


def popular_ok(p: dict) -> bool:
    """Товар годится для каталога: есть название и лицевая сторона, не алкоголь и не табак (решение владельца 2026-11-17)."""
    if not product_name(p) or not shop_photos(p.get("images") or {}):
        return False
    tags = [str(t) for t in p.get("categories_tags") or []]
    return not is_alcohol_tags(tags) and not any("tobacco" in t or "cigar" in t for t in tags)


def pick_popular(found: list[dict], total: int) -> list[dict]:
    """Порядок как пришёл (страны по очереди, внутри — самые сканируемые), только подходящие, не больше total."""
    return [p for p in found if popular_ok(p)][:total]


def _drop_folder(folder: Path) -> None:
    """Папка товара, которую только что создала эта же загрузка (только наши скачанные фото)."""
    for f in folder.iterdir():
        f.unlink()
    folder.rmdir()


def _download_shop(out: Path, p: dict) -> tuple[dict | None, int]:
    code, name = str(p["code"]), product_name(p)
    photos = shop_photos(p.get("images") or {})
    folder = out / f"UZ-{code}"
    folder.mkdir(exist_ok=True)
    got = 0
    for fname, remote in photos:
        url = f"{IMAGE_HOSTS[p['_src']]}/images/products/{_image_dir(code)}/{remote}.400.jpg"
        if download(url, folder / fname):
            got += 1
        time.sleep(0.15)
    first = folder / photos[0][0]
    if not got or not first.is_file():                 # без лицевой стороны товар не берём
        _drop_folder(folder)
        return None, 0
    from app.services.open_product_details import category_from_name  # noqa: PLC0415
    category = category_of(p, first) or category_from_name(name) or "Без категории"
    if category in (ALCOHOL_CATEGORY, TOBACCO_CATEGORY):
        _drop_folder(folder)
        return None, 0
    barcode = "0" + code if len(code) == 12 else code
    return {"sku": f"UZ-{code}", "name": name[:120].rstrip(), "brand": brand_of(p), "category": category, "price": "",
            "barcode": barcode}, got


def fetch_popular(out: Path, countries: list[str], total: int, skip_codes: set[str], pages: int = 10) -> None:
    """Каталог ~total товаров «как в магазине Узбекистана»: сначала все товары первой страны (Узбекистан), затем самые
    сканируемые товары следующих (Россия, Казахстан…). Только с лицевой стороной; без алкоголя и табака; до POPULAR_MAX_PHOTOS
    фото без снимков состава. Уже загруженные штрихкоды (skip_codes) пропускаются. Повторный запуск докачивает недостающее."""
    out.mkdir(parents=True, exist_ok=True)
    found: dict[str, dict] = {}
    for country in countries:
        try:
            batch = search_off(f'countries_tags:"en:{country}"', sort_by="-unique_scans_n", max_pages=pages)
        except Exception as exc:  # noqa: BLE001
            print(f"  {country}: запрос не прошёл ({type(exc).__name__})")
            continue
        for p in batch:
            code = str(p.get("code") or "").strip()
            norm = "0" + code if len(code) == 12 else code
            if code.isdigit() and len(code) in (8, 12, 13) and norm not in skip_codes and code not in found:
                found[code] = p
        print(f"  {country}: кандидатов всего {len(found)}", flush=True)
    picked = pick_popular(list(found.values()), int(total * 1.15) + 10)       # запас: часть отсеется при скачивании
    print(f"Подходящих (есть лицевая сторона, не 21+) беру {len(picked)}", flush=True)
    rows, photos = [], 0
    for n, p in enumerate(picked, 1):
        if len(rows) >= total:
            break
        row, got = _download_shop(out, p)
        if row:
            rows.append(row)
            photos += got
        if n % 25 == 0:
            print(f"  {n}/{len(picked)}: товаров {len(rows)}, фото {photos}", flush=True)
            _write_lists(out, rows)
    _write_lists(out, rows)
    _done(out, rows, photos)


def fetch(out: Path, country: str, prefix: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    print(f"Ищу товары: страна {country}" + (f", штрихкоды {prefix}…" if prefix else ""))
    products = collect(country, prefix)
    print(f"Найдено товаров: {len(products)}")
    rows, photos = [], 0
    for n, p in enumerate(products, 1):
        row, got = _download_product(out, p)
        if row:
            rows.append(row)
            photos += got
        if n % 50 == 0:
            print(f"  {n}/{len(products)}: товаров с фото {len(rows)}, фото {photos}")
    _write_lists(out, rows)
    _done(out, rows, photos)


def link_barcodes(out: Path) -> None:
    """Штрихкоды из barcodes.csv -> таблица штрихкодов базы (товар уже загружен scripts/import_catalog.py). Занятые коды не трогаются."""
    from app.db.models import Product, ProductBarcodeORM  # noqa: PLC0415
    from app.db.session import SessionLocal  # noqa: PLC0415
    with (out / "barcodes.csv").open(encoding="utf-8") as f:
        pairs = [(r["sku"], r["barcode"]) for r in csv.DictReader(f, delimiter=";")]
    db = SessionLocal()
    added = skipped = 0
    try:
        ids = {p.sku: p.id for p in db.query(Product.sku, Product.id).filter(Product.sku.in_([s for s, _ in pairs])).all()}
        taken = {b for (b,) in db.query(ProductBarcodeORM.barcode).filter(ProductBarcodeORM.barcode.in_([c for _, c in pairs])).all()}
        for sku, code in pairs:
            if sku not in ids or code in taken:
                skipped += 1
                continue
            db.add(ProductBarcodeORM(barcode=code, product_id=ids[sku]))
            added += 1
        db.commit()
    finally:
        db.close()
    print(f"Штрихкоды: привязано {added}, пропущено {skipped}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data/test-catalog-uz")
    ap.add_argument("--country", default="uzbekistan")
    ap.add_argument("--barcode-prefix", default="478", help="штрихкоды производителей страны (Узбекистан — 478); пусто — не искать")
    ap.add_argument("--link-barcodes", action="store_true", help="только привязать штрихкоды уже загруженных товаров")
    ap.add_argument("--more", default="", help="ещё товары из этих стран через запятую (russia,kazakhstan,turkey): только с "
                                               "составом и проверенными фото, разных категорий; уже загруженные пропускаются")
    ap.add_argument("--total", type=int, default=200, help="--more: сколько товаров взять")
    ap.add_argument("--per-category", type=int, default=20, help="--more: не больше стольких товаров одной категории")
    ap.add_argument("--rich", default="", help="марки через запятую (pepsi,coca-cola,…): только товары с --min-photos+ фото, "
                                               "у каждого до 15 фото; страны — --countries")
    ap.add_argument("--countries", default="uzbekistan,russia,kazakhstan", help="--rich: страны продажи")
    ap.add_argument("--min-photos", type=int, default=6, help="--rich: не меньше стольких фото в открытой базе")
    ap.add_argument("--popular", type=int, default=0, help="каталог из N товаров: товары --countries по очереди, внутри — "
                    "самые сканируемые; только с лицевой стороной, без алкоголя и табака, без снимков состава")
    ap.add_argument("--alcohol", action="store_true", help="--rich: ещё пиво, водка, вино этих стран (категория «Алкоголь (21+)»)")
    args = ap.parse_args()
    if args.popular:
        from app.db.models import ProductBarcodeORM  # noqa: PLC0415
        from app.db.session import SessionLocal  # noqa: PLC0415
        db = SessionLocal()
        try:
            skip = {b for (b,) in db.query(ProductBarcodeORM.barcode).all()}
        finally:
            db.close()
        fetch_popular(Path(args.out), [c.strip() for c in args.countries.split(",") if c.strip()], args.popular, skip)
    elif args.rich:
        fetch_rich(Path(args.out), [b.strip() for b in args.rich.split(",") if b.strip()],
                   [c.strip() for c in args.countries.split(",") if c.strip()], args.min_photos, args.total, args.alcohol, set())
    elif args.link_barcodes:
        link_barcodes(Path(args.out))
    elif args.more:
        from app.db.models import ProductBarcodeORM  # noqa: PLC0415
        from app.db.session import SessionLocal  # noqa: PLC0415
        db = SessionLocal()
        try:
            skip = {b for (b,) in db.query(ProductBarcodeORM.barcode).all()}
        finally:
            db.close()
        fetch_more(Path(args.out), [c.strip() for c in args.more.split(",") if c.strip()], args.total, args.per_category, skip)
    else:
        fetch(Path(args.out), args.country, args.barcode_prefix)


if __name__ == "__main__":
    main()
