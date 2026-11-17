"""
Инструменты ИИ-помощника (документ 16, §4, §11). Этап A1 — чтение каталога (kind="read"). Этап A2 — «предложить изменение»
(kind="propose": только проверка и карточка для владельца, база НЕ меняется — app/services/assistant_actions.py) и настройки
страницы (kind="ui": язык и тема этого браузера, выполняются сразу).

Правила (документ 16, §2, §6):
- модель не получает «дамп базы» и не пишет SQL: только эти инструменты, каждый отдаёт короткий итог (ограничено число строк);
- каждый инструмент требует права пользователя (Permission) — список для модели собирается по правам ЭТОГО пользователя, и сервер
  ещё раз проверяет право перед вызовом (модель могла назвать чужой инструмент);
- аргументы проверяются здесь, а не моделью: неизвестные поля отбрасываются, числа ограничиваются, строки обрезаются;
- в ответах нет паролей, токенов, пользователей, журналов — только товары.
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func

from app.core import text_search
from app.core.security import CurrentUser, Permission, utcnow
from app.db.models import PRODUCT_ACTIVE, PRODUCT_ARCHIVED, Product, ProductBarcodeORM, ScanEventORM
from app.services import assistant_actions as actions
from app.services import scan_log
from app.services.barcode import search_keys
from app.services.catalog_upload import DEFAULT_BRAND, DEFAULT_CATEGORY
from app.services.product_images import PhotoRegistry
from app.services.product_search import apply_search

MAX_ROWS = 20                   # больше строк модель всё равно не прочитает внимательно, а память разговора маленькая
MAX_TEXT = 120                  # длинное описание и состав — обрезаются
MISSING_FIELDS = ("photo", "barcode", "price", "brand", "category")
MISSING_LABELS = {"photo": "без фото", "barcode": "без штрихкода", "price": "без цены", "brand": "без бренда",
                  "category": "без категории"}


class ToolError(ValueError):
    """Неверные аргументы инструмента: текст уходит модели, чтобы она исправилась или спросила пользователя."""


@dataclass(frozen=True)
class Tool:
    name: str
    permission: Permission
    description: str            # для модели — по-русски, коротко
    args: dict                  # {имя: подсказка} — допустимые аргументы; подсказка коротко (память модели маленькая) или ""
    run: Callable
    kind: str = "read"          # read — смотреть; propose — предложить изменение владельцу; ui — язык/тема страницы
    required: tuple = ()        # обязательные аргументы — модель не сможет их пропустить (своя JSON-схема у каждого инструмента)
    choices: dict = field(default_factory=dict)     # {аргумент: [допустимые значения]}
    answer_next: bool = False   # после успешного вызова модель только отвечает: итог уже полный (иначе она «перепроверяет»)
    wants_lang: bool = False    # инструмент получает язык владельца (args[LANG_ARG]) — от сервера, не от модели


LANG_ARG = "_lang"              # не в args инструмента: модель его не видит и подставить не может


def _cut(value, limit: int = MAX_TEXT):
    if not isinstance(value, str):
        return value
    value = value.strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _limit(args: dict, default: int = 10) -> int:
    raw = args.get("limit", default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, MAX_ROWS))


def _text(args: dict, name: str) -> str:
    value = args.get(name, "")
    return value.strip()[:100] if isinstance(value, str) else ""


def _active(db):
    return db.query(Product).filter(Product.status == PRODUCT_ACTIVE)


def _codes_of(db, ids: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if ids:
        rows = db.query(ProductBarcodeORM.barcode, ProductBarcodeORM.product_id).filter(ProductBarcodeORM.product_id.in_(ids)).all()
        for code, pid in rows:
            out.setdefault(pid, []).append(code)
    return out


def _photo_counts(db, ids: list[str]) -> dict[str, int]:
    if not ids:
        return {}
    return {pid: s.total for pid, s in PhotoRegistry(db).summaries(ids).items()}


def _row(p: Product, codes: dict, photos: dict) -> dict:
    return {"sku": p.sku, "name": _cut(p.name), "brand": _cut(p.brand), "category": _cut(p.category),
            "price": p.expected_price, "barcodes": sorted(codes.get(p.id, [])), "photos": photos.get(p.id, 0)}


def _no_brand(p: Product) -> bool:
    return not (p.brand or "").strip() or p.brand == DEFAULT_BRAND


def _no_category(p: Product) -> bool:
    return not (p.category or "").strip() or p.category == DEFAULT_CATEGORY


# --- инструменты -----------------------------------------------------------------------------------------------------------------

def catalog_overview(db, args: dict) -> dict:
    products = _active(db).all()
    ids = [p.id for p in products]
    photos, codes = _photo_counts(db, ids), _codes_of(db, ids)
    categories = Counter((p.category or DEFAULT_CATEGORY) for p in products)
    return {
        "active": len(products),
        "archived": db.query(Product).filter(Product.status == PRODUCT_ARCHIVED).count(),
        "without_photo": sum(1 for p in products if not photos.get(p.id)),
        "without_barcode": sum(1 for p in products if p.id not in codes),
        "without_price": sum(1 for p in products if p.expected_price is None),
        "without_brand": sum(1 for p in products if _no_brand(p)),
        "without_category": sum(1 for p in products if _no_category(p)),
        "categories": [{"name": _cut(name), "count": n} for name, n in categories.most_common(15)],
        "categories_total": len(categories),
    }


def find_products(db, args: dict) -> dict:
    query = _text(args, "query")
    if not query:
        raise ToolError("Нужен аргумент query: что искать (название, бренд, артикул или штрихкод).")
    q, exact = apply_search(db, _active(db), query)
    total, by_word = q.count(), ""
    if not total:
        # Поиск ищет фразу целиком, а модель пишет её по-своему: «Coca-Cola 0,33 l» (латинская l) при товаре «0,33 л» не
        # находилось ничего. Тогда — по самому длинному слову фразы, и модель видит, по какому слову нашлось.
        for word in sorted({w for w in re.split(r"[\s,.;:()«»\"']+", query) if len(w) >= 3}, key=lambda w: (-len(w), w)):
            if word == query:
                continue
            q, exact = apply_search(db, _active(db), word)
            total = q.count()
            if total:
                by_word = word
                break
    order = (exact.desc(), Product.sku) if exact is not None else (Product.sku,)
    products = q.order_by(*order).limit(_limit(args)).all()
    ids = [p.id for p in products]
    codes, photos = _codes_of(db, ids), _photo_counts(db, ids)
    out = {"query": query, "total": total, "items": [_row(p, codes, photos) for p in products]}
    if by_word:
        out["note"] = f"Точного совпадения нет; найдено по слову «{by_word}» — проверь, тот ли это товар."
    return out


def products_missing(db, args: dict) -> dict:
    field = _text(args, "field").lower()
    if field not in MISSING_FIELDS:
        raise ToolError("Аргумент field: одно из " + ", ".join(MISSING_FIELDS) + ".")
    products = _active(db).order_by(Product.sku).all()
    ids = [p.id for p in products]
    photos, codes = _photo_counts(db, ids), _codes_of(db, ids)
    test = {
        "photo": lambda p: not photos.get(p.id),
        "barcode": lambda p: p.id not in codes,
        "price": lambda p: p.expected_price is None,
        "brand": _no_brand,
        "category": _no_category,
    }[field]
    found = [p for p in products if test(p)]
    limit = _limit(args)
    # Готовая фраза с числом: живая qwen3-vl-4b (2026-11-17) на «Сколько товаров без штрихкода?» брала первый товар списка,
    # смотрела его карточку и предлагала ему выдуманный штрихкод — вместо ответа числом.
    summary = f"Товаров {MISSING_LABELS[field]}: {len(found)} из {len(products)}."
    if len(found) > limit:
        summary += f" Ниже первые {limit}; весь список — на странице «Товары»."
    return {"field": field, "total": len(found), "summary": summary,
            "items": [{"sku": p.sku, "name": _cut(p.name)} for p in found[:limit]]}


def similar_names(db, args: dict) -> dict:
    """Возможные дубли: одинаковое название (без учёта регистра, «ё/е», апострофов и лишних пробелов). Штрихкод дублем быть не
    может — один код ведёт только на один товар (это проверяет сама база)."""
    groups: dict[str, list[Product]] = {}
    for p in _active(db).order_by(Product.sku).all():
        key = " ".join(text_search.normalize(p.name).split())
        if key:
            groups.setdefault(key, []).append(p)
    dupes = [g for g in groups.values() if len(g) > 1]
    limit = _limit(args)
    return {"groups_total": len(dupes),
            "groups": [[{"sku": p.sku, "name": _cut(p.name), "brand": _cut(p.brand)} for p in g[:5]] for g in dupes[:limit]]}


def product_details(db, args: dict) -> dict:
    key = _text(args, "product")
    if not key:
        raise ToolError("Нужен аргумент product: артикул (sku) или штрихкод товара.")
    product = db.query(Product).filter(Product.sku == key).first()
    if product is None:
        codes = search_keys(key)
        if codes:
            row = db.query(ProductBarcodeORM.product_id).filter(ProductBarcodeORM.barcode.in_(codes)).first()
            product = db.get(Product, row[0]) if row else None
    if product is None:
        return {"found": False, "product": key}
    codes, photos = _codes_of(db, [product.id]), _photo_counts(db, [product.id])
    out = _row(product, codes, photos)
    out.update({"found": True, "status": product.status, "in_stock": product.in_stock,
                **{name: _cut(getattr(product, name, None)) for name in
                   ("manufacturer", "country", "net_content", "ingredients", "description")}})
    return out


FEW_PHOTOS = 2                  # 1–2 фото: по виду узнаётся хуже, нужно 2–3 с разных сторон
OFTEN = 3                       # «часто сканируют» — от стольких сканов за выбранные дни
SHOW_ROWS = 8                   # строк в каждом списке: итог для модели — до 2000 знаков


_KINDS: dict[str, dict[str, str]] = {}
_I18N_FILES = {"ru": "i18n.ru.js", "uz": "i18n.uz.js", "uz-Cyrl": "i18n.uz-cyrl.js", "en": "i18n.en.js"}
# «без названия (вид)» — сразу на языке владельца: живая qwen3-vl-4b (2026-11-17) в узбекском ответе оставляла
# «без названия (Молочный продукт)» по-русски — то, что похоже на название товара, она не переводит.
_NO_NAME = {"ru": "без названия", "uz": "nomsiz", "uz-Cyrl": "номсиз", "en": "no name"}


def _kind_label(code: str, lang: str = "ru") -> str:
    """Код вида (drink) -> подпись на языке владельца из словаря страниц (frontend/i18n.<язык>.js, kind.<код>).
    Нет подписи на этом языке — по-русски; нет и её — код как есть («dairy» модель печатала владельцу прямо так)."""
    lang = lang if lang in _I18N_FILES else "ru"
    if lang not in _KINDS:
        try:
            text = (Path(__file__).resolve().parents[2] / "frontend" / _I18N_FILES[lang]).read_text(encoding="utf-8")
            _KINDS[lang] = dict(re.findall(r'"kind\.(\w+)":\s*"([^"]+)"', text))
        except OSError:
            _KINDS[lang] = {}
    label = _KINDS[lang].get(code)
    return label if label else (code if lang == "ru" else _kind_label(code, "ru"))


def _days(args: dict) -> int:
    try:
        value = int(args.get("days", 30))
    except (TypeError, ValueError):
        value = 30
    return max(1, min(value, scan_log.KEEP_DAYS))


TIPS = ("no_photo", "more_photos", "check_photos", "add_barcode")     # советы по товару: коды для страницы (i18n) и для модели
_TIP_TEXT = {"no_photo": "нет фото: по виду не узнаётся — добавьте 2–3 фото с разных сторон",
             "more_photos": "добавьте ещё 2–3 фото с разных сторон",
             "check_photos": "проверьте фото товара (на всех ли он сам) и добавьте крупное фото этикетки",
             "add_barcode": "добавьте штрихкод — по нему узнаётся сразу"}


def _tips(unsure: int, photos: int, has_code: bool) -> list[str]:
    if not photos:
        return ["no_photo"]
    tips = []
    if photos <= FEW_PHOTOS:
        tips.append("more_photos")
    elif unsure:
        tips.append("check_photos")
    if unsure and not has_code:
        tips.append("add_barcode")
    return tips


def scan_report(db, days: int) -> dict:
    """Итог сканов покупателей за days дней (app/services/scan_log.py) — числами, без готовых фраз. Его показывает страница
    помощника сразу, без модели (GET /assistant/scans, решение владельца 2026-11-17), и из него же собирается scan_problems.
      counts   — сканов по итогу: matched, uncertain, not_in_catalog, unknown;
      weak     — товары, которые узнавались неуверенно или часто сканируются при 1–2 фото (tips — коды советов, TIPS);
      missing  — что сканировали, а в каталоге нет: name (как назвала программа) или kind (вид), times;
      started  — были ли сканы вообще (False — программа ещё не начала их считать)."""
    days = max(1, min(int(days), scan_log.KEEP_DAYS))
    since = utcnow() - timedelta(days=days)
    rows = db.query(ScanEventORM.status, ScanEventORM.product_id, ScanEventORM.guess_name, ScanEventORM.guess_kind)         .filter(ScanEventORM.at >= since).all()
    counts = Counter(r.status for r in rows)
    per: dict[str, list[int]] = {}
    for r in rows:
        if r.product_id:
            item = per.setdefault(r.product_id, [0, 0])
            item[0] += 1
            item[1] += r.status == "uncertain"
    products = {p.id: p for p in _active(db).filter(Product.id.in_(list(per))).all()} if per else {}
    photos, codes = _photo_counts(db, list(products)), _codes_of(db, list(products))
    weak = []
    for pid, (scans, unsure) in per.items():
        p = products.get(pid)
        if p is None or not (unsure or (scans >= OFTEN and photos.get(pid, 0) <= FEW_PHOTOS)):
            continue
        weak.append({"sku": p.sku, "name": _cut(p.name, 60), "scans": scans, "unsure": unsure, "photos": photos.get(pid, 0),
                     "tips": _tips(unsure, photos.get(pid, 0), pid in codes)})
    weak.sort(key=lambda w: (-w["unsure"], -w["scans"], w["sku"]))
    missing: dict[tuple, dict] = {}
    for r in rows:
        if r.status != "not_in_catalog":
            continue
        name = (r.guess_name or "").strip()
        if not name and not r.guess_kind:
            continue
        key = (" ".join(text_search.normalize(name).split()), "") if name else ("", r.guess_kind)
        item = missing.setdefault(key, {"name": _cut(name, 60) or None, "kind": None if name else r.guess_kind, "times": 0})
        item["times"] += 1
    return {"days": days, "scans": len(rows), "started": db.query(func.min(ScanEventORM.at)).scalar() is not None,
            "counts": {s: counts[s] for s in scan_log.STATUSES}, "weak_total": len(weak), "weak": weak[:SHOW_ROWS],
            "missing_total": len(missing),
            "missing": sorted(missing.values(), key=lambda m: -m["times"])[:SHOW_ROWS],
            "many_unknown": bool(rows) and counts["unknown"] * 4 >= len(rows)}


def scan_problems(db, args: dict) -> dict:
    """«Почему не узнал» (документ 16, §10.5): по сканам покупателей за N дней — товары, которые узнавались неуверенно или
    часто сканируются при 1–2 фото; что сканировали, а в каталоге этого нет; сколько не поняли совсем. Числа — scan_report."""
    lang = args.get(LANG_ARG) if args.get(LANG_ARG) in _NO_NAME else "ru"
    rep = scan_report(db, _days(args))
    days, counts = rep["days"], rep["counts"]
    # Счёт — сканов, а не товаров: с полями «unsure: 7» модель писала «7 товаров не распознаются». Готовая фраза summary
    # переписывается моделью без ошибок.
    # «Программа узнала» — с подлежащим: с «узнано …» модель писала «покупатели не узнают товары».
    out = {"days": days, "scans": rep["scans"], "summary": (
        f"За {days} дн. покупатели сделали {rep['scans']} сканов. Из этих сканов программа узнала товар уверенно — "
        f"{counts['matched']}, неуверенно — {counts['uncertain']}; товара не было в каталоге — {counts['not_in_catalog']}; "
        f"не поняла, что на фото, — {counts['unknown']}.")}
    if not rep["started"]:
        out["note"] = "Сканов покупателей ещё нет: программа начала их считать с этого обновления."
    out["weak_total"] = rep["weak_total"]
    out["weak"] = [{k: w[k] for k in ("sku", "name", "scans", "unsure", "photos")} | {"advice": "; ".join(_TIP_TEXT[t] for t in w["tips"])}
                   for w in rep["weak"]]
    out["missing"] = [{"name": m["name"] or f"{_NO_NAME[lang]} ({_kind_label(m['kind'], lang)})", "times": m["times"]}
                      for m in rep["missing"]]
    if out["missing"]:
        out["missing_advice"] = "этих товаров нет в каталоге (уже проверено, искать не нужно): если они продаются — добавьте их с фото"
    if rep["many_unknown"]:
        out["not_understood_advice"] = "много сканов не поняли совсем: попросите снимать ближе, при хорошем свете, штрихкодом к камере"
    return out


TOOLS: tuple[Tool, ...] = (
    Tool("catalog_overview", Permission.CATALOG_READ,
         "Сводка каталога: сколько товаров, в архиве, без фото, штрихкода, цены, бренда, категории; категории.",
         {}, catalog_overview),
    Tool("find_products", Permission.CATALOG_READ,
         "Найти товары по названию, бренду, артикулу или штрихкоду.",
         {"query": "", "limit": "до 20"}, find_products, required=("query",)),
    Tool("products_missing", Permission.CATALOG_READ,
         "Товары, у которых нет фото, штрихкода, цены, бренда или категории.",
         {"field": "|".join(MISSING_FIELDS), "limit": "до 20"}, products_missing,
         required=("field",), choices={"field": list(MISSING_FIELDS)}, answer_next=True),
    Tool("similar_names", Permission.CATALOG_READ,
         "Возможные дубли: товары с одинаковым названием.",
         {"limit": "до 20"}, similar_names),
    Tool("product_details", Permission.CATALOG_READ,
         "Всё о товаре по артикулу (sku) или штрихкоду.",
         {"product": "артикул или штрихкод"}, product_details, required=("product",)),
    Tool("scan_problems", Permission.CATALOG_READ,
         "Почему не узнаёт: сканы покупателей за days дней — что узнаётся плохо, кому нужно больше фото, чего нет в каталоге.",
         {"days": "1–90, обычно 30"}, scan_problems, answer_next=True, wants_lang=True),
)
TOOLS += (
    Tool("propose_update", Permission.CATALOG_WRITE,
         "Предложить изменить одно поле одного товара.",
         {"product": "артикул или штрихкод", "field": "|".join(actions.EDIT_FIELDS),
          "value": "новое значение; пусто — убрать; in_stock: да/нет"},
         lambda db, args, props: props.update(db, args), "propose",
         required=("product", "field", "value"), choices={"field": list(actions.EDIT_FIELDS)}),
    Tool("propose_archive", Permission.CATALOG_WRITE,
         "Предложить убрать товар в архив (вместо удаления) или вернуть из архива.",
         {"product": "артикул или штрихкод", "status": "archived|active"},
         lambda db, args, props: props.status(db, args), "propose",
         required=("product", "status"), choices={"status": ["archived", "active"]}),
    Tool("propose_add", Permission.CATALOG_WRITE,
         "Предложить новый товар; фото добавит владелец.",
         {"name": "", "price": "", "brand": "", "category": ""},
         lambda db, args, props: props.add(db, args), "propose", required=("name",)),
    Tool("propose_bulk", Permission.CATALOG_WRITE,
         "Одно изменение многим товарам (до 50).",
         {"where": "|".join(actions.BULK_WHERE), "query": "", "change": "|".join(actions.BULK_CHANGES), "value": ""},
         lambda db, args, props: props.bulk(db, args), "propose", required=("where", "query", "change", "value"),
         choices={"where": list(actions.BULK_WHERE), "change": list(actions.BULK_CHANGES)}),
    Tool("set_language", Permission.ASSISTANT_USE, "Сменить язык программы сразу.",
         {"lang": "|".join(actions.UI_LANGS)}, lambda args: actions.ui_action(args, "lang"), "ui",
         required=("lang",), choices={"lang": list(actions.UI_LANGS)}),
    Tool("set_theme", Permission.ASSISTANT_USE, "Сменить фон программы сразу.",
         {"theme": "light (светлый)|dark (тёмный)"}, lambda args: actions.ui_action(args, "theme"), "ui",
         required=("theme",), choices={"theme": list(actions.UI_THEMES)}),
)
_BY_NAME = {t.name: t for t in TOOLS}


def allowed_tools(user: CurrentUser) -> list[Tool]:
    return [t for t in TOOLS if user.has_permission(t.permission)]


def run_tool(db, user: CurrentUser, name: str, args, proposals: actions.Proposals | None = None,
             ui: list | None = None, lang: str = "ru") -> dict:
    """Вызов от имени пользователя: инструмент должен существовать И быть разрешён ему; лишние аргументы отбрасываются.
    Предложения складываются в proposals (база не меняется), действия страницы — в ui."""
    tool = _BY_NAME.get(name)
    if tool is None or not user.has_permission(tool.permission):
        raise ToolError(f"Инструмента «{_cut(str(name), 40)}» нет или он вам недоступен.")
    clean = {k: v for k, v in (args if isinstance(args, dict) else {}).items() if k in tool.args}
    if tool.wants_lang:
        clean[LANG_ARG] = lang
    try:
        if tool.kind == "propose":
            return tool.run(db, clean, proposals if proposals is not None else actions.Proposals())
        if tool.kind == "ui":
            act = tool.run(clean)
            if ui is not None:
                ui.append(act)
            return {"ok": True, "done": act}
        return tool.run(db, clean)
    except actions.ActionError as exc:
        raise ToolError(str(exc)) from exc


def get_tool(name: str) -> Tool | None:
    return _BY_NAME.get(name)
