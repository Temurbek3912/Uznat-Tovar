"""
Замер ИИ-помощника: выбирает ли он ту команду (замер 2026-11-23, docs/benchmark/assistant_report_2026-11-23.md).

    .venv/Scripts/python.exe -X utf8 -m ml.benchmark_assistant --url http://127.0.0.1:8095/v1
    .venv/Scripts/python.exe -X utf8 -m ml.benchmark_assistant --url … --only "фанта" --out D:/опыт/a.json

Нужна запущенная модель (llama-server или LM Studio) по адресу --url. Разговор — настоящий (app/services/assistant.chat:
та же подсказка, схема ответа, проверки программы), база — временная SQLite с маленьким каталогом (ml/assistant_phrases.CATALOG),
рабочая база не трогается. Фразы и ожидания — ml/assistant_phrases.PHRASES.

Итог по каждой фразе:
  верно     — сделано ожидаемое (нужные данные посмотрены, нужная карточка или смена языка/фона);
  не та     — предложено или сделано ДРУГОЕ изменение (другой товар, поле, действие), или изменение там, где его быть не должно, —
              самое опасное: владелец может нажать «Сделать» не глядя;
  не сделал — ничего не предложено / не тот инструмент для ответа (владелец переспросит — не опасно, но неудобно).
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import types
from collections import Counter
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Sequence

from ml.assistant_phrases import ARCHIVED, CATALOG, PHRASES


def make_catalog(path: Path, rows=None):
    """Временная SQLite: rows — [(артикул, название, бренд, категория, цена, штрихкод, в архиве)]; по умолчанию — маленький
    каталог ml/assistant_phrases."""
    from sqlalchemy import create_engine  # noqa: PLC0415
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

    from app.db import migrate  # noqa: PLC0415
    from app.db.models import PRODUCT_ARCHIVED, Product, ProductBarcodeORM  # noqa: PLC0415
    if rows is None:
        rows = [(*r, False) for r in CATALOG] + [(*r, True) for r in ARCHIVED]
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    migrate.upgrade_to_head(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with session() as db:
        for n, (sku, name, brand, category, price, code, archived) in enumerate(rows):
            p = Product(sku=sku, name=name, brand=brand, category=category, expected_price=price,
                        created_at=datetime(2026, 11, 1) + timedelta(minutes=n))
            if archived:
                p.status = PRODUCT_ARCHIVED
            db.add(p)
            db.flush()
            if code:
                db.add(ProductBarcodeORM(barcode=code, product_id=p.id))
        db.commit()
    return engine, session


def outcome(reply) -> list[str]:
    """Что сделал помощник — строками в том же виде, что ожидания (ml/assistant_phrases)."""
    out = []
    card = reply.proposal
    if card:
        kind = card.get("kind")
        if kind == "update":
            out += [f"upd:{card.get('sku')}:{field}" for field in (card.get("changes") or {})] or [f"upd:{card.get('sku')}:?"]
        elif kind == "status":
            out.append(f"status:{card.get('sku')}:{card.get('status')}")
        elif kind == "add":
            out.append("add")
        elif kind == "bulk":
            out.append(f"bulk:{card.get('change')}:{card.get('where')}")
        else:
            out.append(f"card:{kind}")
    out += [f"{u.get('kind')}:{u.get('value')}" for u in reply.ui or []]
    return out


def judge(expect: str, done: list[str], reads: list[str]) -> str:
    options = expect.split("|")
    changes = [o for o in options if not o.startswith("read:") and o != "none"]
    if done:
        return "верно" if any(d in changes for d in done) and all(d in changes for d in done) else "не та"
    if changes:
        return "не сделал"
    if "none" in options:
        return "верно"
    wanted = [o.split(":", 1)[1] for o in options if o.startswith("read:")]
    return "верно" if any(w in reads for w in wanted) else "не сделал"


def run(url: str, model: str, phrases, log=print, pause: float = 0.0) -> list[dict]:
    from app.core.security import CurrentUser, Role  # noqa: PLC0415
    from app.services import assistant as A  # noqa: PLC0415
    owner = CurrentUser(id="u1", username="owner", role=Role.ADMIN)
    settings = types.SimpleNamespace(naming_enabled=True, assistant_enabled=True, naming_vision_url=url,
                                     naming_vision_model=model)
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        engine, session = make_catalog(Path(tmp) / "catalog.sqlite")
        try:
            for n, (said, lang, expect) in enumerate(phrases, 1):
                started = time.monotonic()
                try:
                    with session() as db:
                        reply = A.chat(db, owner, [{"role": "user", "content": said}], lang, settings)
                        db.rollback()
                    done, reads = outcome(reply), [t["name"] for t in reply.tools]
                    text, error = reply.text, reply.proposal_error
                except Exception as exc:  # noqa: BLE001 — модель не ответила: «не сделал»
                    done, reads, text, error = [], [], "", f"{type(exc).__name__}: {exc}"
                verdict = judge(expect, done, reads)
                rows.append({"said": said, "lang": lang, "expect": expect, "done": done, "reads": reads, "verdict": verdict,
                             "text": text[:300], "error": error[:200], "seconds": round(time.monotonic() - started, 1)})
                log(f"{n:3d} {verdict:9s} {said[:50]:50s} ждали {expect:32s} сделал {done or reads} {error[:60]}")
                if pause:
                    time.sleep(pause)
        finally:
            engine.dispose()
    return rows


def summary(rows: list[dict]) -> str:
    c = Counter(r["verdict"] for r in rows)
    by_lang = {}
    for r in rows:
        by_lang.setdefault(r["lang"], Counter())[r["verdict"]] += 1
    n = len(rows)
    pct = lambda k, cnt=c, total=n: f"{100 * cnt[k] / max(1, total):.0f} %"     # noqa: E731
    lines = [f"Фраз: {n}. Верно {c['верно']} ({pct('верно')}), не та команда {c['не та']} ({pct('не та')}), "
             f"не сделал {c['не сделал']} ({pct('не сделал')}). Среднее время ответа "
             f"{sum(r['seconds'] for r in rows) / max(1, n):.1f} с."]
    for lang, cnt in by_lang.items():
        total = sum(cnt.values())
        lines.append(f"  {lang}: верно {cnt['верно']} из {total}, не та {cnt['не та']}, не сделал {cnt['не сделал']}")
    return "\n".join(lines)


# --- разговоры (ml/assistant_dialogs.py, замер 2026-11-24) -----------------------------------------------------------------------

BIG_CATALOG = Path("D:/uznat_tovar/assistant-2026-11-24/catalog_copy.json")    # копия товаров рабочей базы (вне репозитория)
_PAGE_KEYS = ("assistant.proposal_default", "assistant.ui_default", "assistant.proposal_not_made", "assistant.empty_reply",
              "assistant.list_reply", "error.generic")
_I18N = {"ru": "i18n.ru.js", "uz": "i18n.uz.js", "uz-Cyrl": "i18n.uz-cyrl.js", "en": "i18n.en.js"}


def page_texts(lang: str) -> dict:
    """Тексты страницы, которые идут в историю вместо пустого ответа (frontend/assistant.js, SAY_KEYS)."""
    src = (Path(__file__).resolve().parents[1] / "frontend" / _I18N.get(lang, _I18N["ru"])).read_text(encoding="utf-8")
    out = {}
    for key in _PAGE_KEYS:
        m = re.search(rf'"{re.escape(key)}":\s*"((?:[^"\\]|\\.)*)"', src)
        out[key] = json.loads(f'"{m.group(1)}"') if m else ""
    return out


def big_rows(path: Path) -> list[tuple]:
    from ml.assistant_dialogs import big_price  # noqa: PLC0415
    items = json.loads(Path(path).read_text(encoding="utf-8"))
    return [(p["sku"], p["name"], p["brand"], p["category"], big_price(p["sku"]), (p.get("barcodes") or [None])[0],
             p.get("status") != "active") for p in items]


def _num(value) -> str:
    try:
        f = float(str(value).replace(" ", "").replace(",", "."))
    except ValueError:
        return str(value)
    return str(int(f)) if f == int(f) else str(f)


def outcome_values(reply) -> list[str]:
    """Как outcome, но со значением: «upd:NUT:price=47000», «bulk:price_percent:category=Сладости»."""
    out = []
    card = reply.proposal
    if card:
        kind = card.get("kind")
        if kind == "update":
            out += [f"upd:{card.get('sku')}:{f}={_num(v)}" for f, v in (card.get("changes") or {}).items()]
        elif kind == "status":
            out.append(f"status:{card.get('sku')}:{card.get('status')}")
        elif kind == "add":
            out.append("add")
        elif kind == "bulk" and card.get("change") == "price_list":     # список цен — по строке на товар
            out += [f"upd:{r.get('sku')}:price={_num(r.get('after'))}" for r in card.get("rows") or []] or ["card:list"]
        elif kind == "bulk":
            # категории, которые нашла программа («shirinliklar» → Сладости), а не слово владельца
            found = ";".join(card.get("categories") or []) if card.get("where") == "category" else ""
            out.append(f"bulk:{card.get('change')}:{card.get('where')}={found or card.get('query')}")
        else:
            out.append(f"card:{kind}")
    out += [f"{u.get('kind')}:{u.get('value')}" for u in reply.ui or []]
    return out


def _matches(want: str, did: str) -> bool:
    if "=" in want:
        key, value = want.split("=", 1)
        dkey, _, dvalue = did.partition("=")
        return key == dkey and (_num(value) == _num(dvalue) or value.lower() == dvalue.lower())
    return did.partition("=")[0] == want


def judge_turn(expect: str, done: list[str], reads: list[str]) -> str:
    """верно / частично (из двух действий сделано одно) / не та / не сделал."""
    alts = expect.split("|")
    change_alts = [a.split("&") for a in alts if not a.startswith("read:") and a != "none"]
    if done:
        for parts in change_alts:
            if all(any(_matches(p, d) for p in parts) for d in done):
                return "верно" if all(any(_matches(p, d) for d in done) for p in parts) else "частично"
        return "не та"
    if "none" in alts:
        return "верно"
    wanted = [a.split(":", 1)[1] for a in alts if a.startswith("read:")]
    if wanted and any(w in reads for w in wanted):
        return "верно"
    return "не сделал"


def _digits(text: str) -> str:
    return re.sub(r"(?<=\d)[\s  .,](?=\d{3}\b)", "", (text or "").lower())


def says_problems(text: str, says, not_says, prices: dict) -> list[str]:
    def fill(s: str) -> str:
        return re.sub(r"\{price:([^}]+)\}", lambda m: _num(prices.get(m.group(1))), s)
    plain = _digits(text)
    out = []
    for item in says:
        if not any(fill(v).lower() in plain for v in item.split("|")):
            out.append(f"нет «{item}»")
    for item in not_says:
        item = fill(item)
        if (re.search(item[3:], plain) if item.startswith("re:") else item.lower() in plain):
            out.append(f"лишнее «{item}»")
    return out


_CYR = re.compile(r"[а-яёқғҳў]{2,}", re.I)
_LAT = re.compile(r"(?<![\wʻ'’])[a-z][a-zʻ'’]{2,}", re.I)
_BAD_CURRENCY = {
    "ru": re.compile(r"\d\s*(?:тенге|сом\b|сома|сомов|руб)", re.I),
    "uz": re.compile(r"\d\s*(?:сум|сом|сўм|тенге|sum\b|som\b|so'm|tenge)", re.I),
    "uz-Cyrl": re.compile(r"\d\s*(?:soʻm|so'm|som\b|sum\b|сум\b|сума|сом\b|тенге)", re.I),
}
_LOOP = re.compile(r"(.{12,}?)\1{2,}", re.S)


def text_problems(text: str, lang: str, known: set[str]) -> list[str]:
    """Алфавит, валюта, зацикливание. known — слова каталога и вопроса (в ответе законны в любом алфавите)."""
    out = []
    if lang == "uz":
        bad = [w for w in _CYR.findall(text) if w.lower() not in known]
        if bad:
            out.append("кириллица: " + " ".join(bad[:4]))
    if lang == "uz-Cyrl":
        bad = [w for w in _LAT.findall(text) if w.lower().strip("ʻ'’") not in known]
        if bad:
            out.append("латиница: " + " ".join(bad[:4]))
    cur = _BAD_CURRENCY.get(lang)
    if cur and cur.search(text):
        out.append("валюта: " + cur.search(text).group(0))
    if _LOOP.search(text):
        out.append("повтор")
    return out


_DB_COLUMN = {"price": "expected_price", "name": "name", "brand": "brand", "category": "category", "in_stock": "in_stock",
              "status": "status"}


def _db_values(db, sku: str, fields) -> dict:
    from app.db.models import Product, ProductBarcodeORM  # noqa: PLC0415
    p = db.query(Product).filter(Product.sku == sku).one()
    out = {}
    for field in fields:
        if field == "barcode":
            out[field] = sorted(c for (c,) in db.query(ProductBarcodeORM.barcode).filter(ProductBarcodeORM.product_id == p.id))
        else:
            out[field] = getattr(p, _DB_COLUMN[field])
    return out


def _db_same(want, got, field: str) -> bool:
    if field == "barcode":
        return str(want) in got
    if field == "price":
        return got is not None and abs(float(got) - float(want)) < 0.01
    return (got or "") == want if isinstance(want, str) else got == want


def _press(session, owner, action: dict) -> tuple[dict | None, str]:
    """Кнопка «Сделать» (или «Отменить») — тот же вызов, что у страницы (POST /assistant/act)."""
    from app.api import routes_assistant  # noqa: PLC0415
    try:
        with session() as db:
            out = routes_assistant.assistant_act(routes_assistant.ActIn(action=action), db, owner, owner)
        return out, ""
    except Exception as exc:  # noqa: BLE001 — ошибка кнопки — тоже итог замера
        return None, f"{type(exc).__name__}: {getattr(exc, 'message', '') or exc}"[:200]


def apply_problems(session, owner, card: dict, want: dict, undo: bool) -> list[str]:
    """Нажать «Сделать», проверить базу; undo — нажать «Отменить» и проверить, что вернулось прежнее."""
    with session() as db:
        before = {sku: _db_values(db, sku, fields) for sku, fields in want.items()}
    out, err = _press(session, owner, card)
    if err:
        return [f"«Сделать»: {err}"]
    problems = []
    with session() as db:
        for sku, fields in want.items():
            got = _db_values(db, sku, fields)
            problems += [f"{sku}.{f}: в базе {got[f]!r}, ждали {v!r}" for f, v in fields.items() if not _db_same(v, got[f], f)]
    if undo and not problems:
        if not (out or {}).get("undo"):
            return ["нет «Отменить»"]
        _out, err = _press(session, owner, out["undo"])
        if err:
            return [f"«Отменить»: {err}"]
        with session() as db:
            for sku, fields in want.items():
                got = _db_values(db, sku, fields)
                problems += [f"после «Отменить» {sku}.{f}: {got[f]!r}, было {before[sku][f]!r}" for f in fields
                             if got[f] != before[sku][f]]
    return problems


def card_problems(card: dict | None, want: dict) -> list[str]:
    if not card:
        return ["нет карточки"]
    out = []
    for field, value in want.items():
        got = card.get(field)
        if field == "price":
            if got is None or abs(float(got) - float(value)) > 0.01:
                out.append(f"цена в карточке {got!r}, ждали {value}")
        elif str(value).lower() not in str(got or "").lower():
            out.append(f"{field} в карточке {got!r}, ждали «{value}»")
    return out


def run_dialogs(url: str, model: str, dialogs, big: Path | None = None, log=print, pause: float = 0.0) -> list[dict]:
    from app.core.security import CurrentUser, Role  # noqa: PLC0415
    from app.services import assistant as A  # noqa: PLC0415
    owner = CurrentUser(id="u1", username="owner", role=Role.ADMIN)
    settings = types.SimpleNamespace(naming_enabled=True, assistant_enabled=True, naming_vision_url=url,
                                     naming_vision_model=model)
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        catalogs = {"small": None}
        if any(d["catalog"] == "big" for d in dialogs):
            catalogs["big"] = big_rows(big or BIG_CATALOG)
        made = {name: make_catalog(Path(tmp) / f"{name}.sqlite", rows_) for name, rows_ in catalogs.items()}
        try:
            for number, dialog in enumerate(dialogs):
                if dialog.get("apply"):         # «Сделать» меняет базу — у разговора своя временная база
                    made[f"apply{number}"] = make_catalog(Path(tmp) / f"apply{number}.sqlite", catalogs[dialog["catalog"]])
                    engine, session = made[f"apply{number}"]
                else:
                    engine, session = made[dialog["catalog"]]
                cat = catalogs[dialog["catalog"]] or [(*r, False) for r in CATALOG + ARCHIVED]
                prices = {r[0]: r[4] for r in cat}
                known = {w.lower() for r in cat for w in re.findall(r"[\wʻ'’]+", f"{r[0]} {r[1]} {r[2]} {r[3]}")}
                history: list[dict] = []
                for n, turn in enumerate(dialog["turns"], 1):
                    lang, said = turn["lang"], turn["say"]
                    history.append({"role": "user", "content": said})
                    started = time.monotonic()
                    try:
                        with session() as db:
                            reply = A.chat(db, owner, list(history), lang, settings)
                            db.rollback()
                        done, reads = outcome_values(reply), [t["name"] for t in reply.tools]
                        text, error = reply.text, reply.proposal_error
                        page = page_texts(lang)
                        card = reply.proposal
                        key = ("assistant.list_reply" if card and card.get("kind") == "bulk" and not card.get("rows") else
                               "assistant.proposal_default" if card else "assistant.ui_default" if reply.ui else
                               "assistant.proposal_not_made" if error else "assistant.empty_reply")
                        shown = text or page[key]
                    except Exception as exc:  # noqa: BLE001 — модель не ответила: «не сделал»
                        done, reads, text, error = [], [], "", f"{type(exc).__name__}: {exc}"
                        shown = page_texts(lang)["error.generic"]
                    history.append({"role": "assistant", "content": shown})
                    verdict = judge_turn(turn["expect"], done, reads)
                    problems = []
                    if verdict == "верно" and text:
                        own = known | {w.lower() for w in re.findall(r"[\wʻ'’]+", said)}
                        wrong = says_problems(text, turn.get("says") or (), turn.get("not_says") or (), prices)
                        problems = wrong + text_problems(text, lang, own)
                        if wrong:
                            verdict = "ответ неверный"
                        elif problems:
                            verdict = "язык"
                    elif verdict == "верно" and turn.get("says") and not done:
                        verdict, problems = "ответ неверный", ["пустой ответ"]
                    if verdict == "верно" and turn.get("card"):
                        problems = card_problems(reply.proposal, turn["card"])
                        verdict = "карточка неверная" if problems else verdict
                    if verdict == "верно" and turn.get("db") and dialog.get("apply") and reply.proposal:
                        problems = apply_problems(session, owner, reply.proposal, turn["db"], bool(turn.get("undo")))
                        verdict = "не применилось" if problems else verdict
                    rows.append({"dialog": dialog["id"], "turn": n, "catalog": dialog["catalog"], "said": said, "lang": lang,
                                 "expect": turn["expect"], "done": done, "reads": reads, "verdict": verdict,
                                 "problems": problems, "text": text[:400], "error": error[:200],
                                 "seconds": round(time.monotonic() - started, 1)})
                    log(f"{dialog['id'][:22]:22s} {n} {verdict:14s} {said[:38]:38s} сделал {done or reads} "
                        f"{'; '.join(problems)[:70]} {error[:50]} | {text[:80]!r}")
                    if pause:
                        time.sleep(pause)
        finally:
            for engine, _session in made.values():
                engine.dispose()
    return rows


def dialog_summary(rows: list[dict]) -> str:
    c = Counter(r["verdict"] for r in rows)
    n = len(rows)
    parts = ", ".join(f"{k} {c[k]}" for k in ("верно", "не та", "частично", "не сделал", "ответ неверный", "язык",
                                               "карточка неверная", "не применилось") if c[k])
    lines = [f"Реплик: {n} (разговоров {len({r['dialog'] for r in rows})}). {parts}. Верно {100 * c['верно'] / max(1, n):.0f} %. "
             f"Среднее время {sum(r['seconds'] for r in rows) / max(1, n):.1f} с."]
    groups: dict[str, Counter] = {}
    for r in rows:
        groups.setdefault(r["dialog"].split("_")[0], Counter())[r["verdict"]] += 1
        groups.setdefault("lang:" + r["lang"], Counter())[r["verdict"]] += 1
    for g, cnt in groups.items():
        lines.append(f"  {g}: верно {cnt['верно']} из {sum(cnt.values())}; "
                     + ", ".join(f"{k} {v}" for k, v in cnt.items() if k != "верно"))
    return "\n".join(lines)

# --- вопросы «сколько» (ml/assistant_counts.py, этап 1 плана 2026-11-28) ----------------------------------------------------------

SHOP_COPY = Path("D:/uznat_tovar/assistant-counts-2026-11-28/shop_copy.db")    # копия data/lite/shop.db (вне репозитория)


def _key(text) -> str:
    import unicodedata  # noqa: PLC0415
    flat = unicodedata.normalize("NFKD", str(text or "")).lower()
    return re.sub(r"[^a-z0-9а-яё]+", "", "".join(ch for ch in flat if not unicodedata.combining(ch)))


def counts_catalog(copy: Path, tmp: Path):
    """Копия базы магазина во временной папке: схема — до последней, цены замера (ml/assistant_dialogs.big_price)."""
    import shutil  # noqa: PLC0415

    from sqlalchemy import create_engine  # noqa: PLC0415
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

    from app.db import migrate  # noqa: PLC0415
    from app.db.models import Product  # noqa: PLC0415
    from ml.assistant_dialogs import big_price  # noqa: PLC0415
    path = tmp / "shop.sqlite"
    shutil.copyfile(copy, path)
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    migrate.upgrade_to_head(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with session() as db:
        for p in db.query(Product).all():
            p.expected_price = big_price(p.sku)
        db.commit()
    return engine, session


def count_truth(db, spec: tuple) -> dict:
    """Верный ответ прямо по строкам базы: {"numbers": [допустимые числа]} или {"names": [...]}."""
    from app.db.models import PRODUCT_ACTIVE, PRODUCT_ARCHIVED, Product, ProductBarcodeORM  # noqa: PLC0415
    from app.services.product_images import PhotoRegistry  # noqa: PLC0415
    active = db.query(Product).filter(Product.status == PRODUCT_ACTIVE).all()
    kind = spec[0]
    if kind == "archived":
        return {"numbers": [db.query(Product).filter(Product.status == PRODUCT_ARCHIVED).count()]}
    if kind == "cats":
        cats = {p.category or "Без категории" for p in active}
        return {"numbers": sorted({len(cats), len(cats - {"Без категории"})})}
    if kind == "price":
        p = next(p for p in active if p.sku == spec[1])
        return {"numbers": [int(p.expected_price)]}
    if kind == "top":
        rows = [p for p in active if p.category == spec[1] and p.expected_price is not None]
        top = max(p.expected_price for p in rows)
        return {"names": [p.name for p in rows if p.expected_price == top]}
    rows = active
    rest = list(spec[2:]) if kind in ("cat", "brand") else list(spec[1:])
    if kind == "cat":
        rows = [p for p in rows if p.category in spec[1].split(";")]
    if kind == "brand":
        k = _key(spec[1])
        rows = [p for p in rows if _key(p.brand) == k or k in _key(p.name)]
    rest += [None] * (3 - len(rest))
    without, lo, hi = rest[:3]
    if without == "price":
        rows = [p for p in rows if p.expected_price is None]
    elif without == "barcode":
        coded = {pid for (pid,) in db.query(ProductBarcodeORM.product_id).all()}
        rows = [p for p in rows if p.id not in coded]
    elif without == "photo":
        photos = {pid: s.total for pid, s in PhotoRegistry(db).summaries([p.id for p in rows]).items()}
        rows = [p for p in rows if not photos.get(p.id)]
    if lo is not None or hi is not None:
        rows = [p for p in rows if p.expected_price is not None and (lo is None or p.expected_price >= lo)
                and (hi is None or p.expected_price <= hi)]
    return {"numbers": [len(rows)]}


_NONE = re.compile(r"(?i)(?<!\w)(?:нет|yoʻq|yo'q|йўқ|mavjud emas|мавжуд эмас|бар эмас|no|none)(?!\w)")


def count_verdict(text: str, truth: dict) -> str:
    if "names" in truth:
        return "верно" if any(_key(n) in _key(text) for n in truth["names"]) else "неверно"
    found = {int(n) for n in re.findall(r"(?<![\d.,])\d+(?!\d|[.,]\d)", _digits(text))}
    if 0 in truth["numbers"] and not found and _NONE.search(text or ""):
        return "верно"                      # «Нет товаров без фото» при нуле — тоже верное число
    return "верно" if any(n in found for n in truth["numbers"]) else "неверно"


def run_counts(url: str, model: str, questions, copy: Path, log=print, pause: float = 0.0) -> list[dict]:
    from app.core.security import CurrentUser, Role  # noqa: PLC0415
    from app.services import assistant as A  # noqa: PLC0415
    owner = CurrentUser(id="u1", username="owner", role=Role.ADMIN)
    settings = types.SimpleNamespace(naming_enabled=True, assistant_enabled=True, naming_vision_url=url,
                                     naming_vision_model=model)
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        engine, session = counts_catalog(copy, Path(tmp))
        try:
            for n, (said, lang, spec) in enumerate(questions, 1):
                started = time.monotonic()
                with session() as db:
                    truth = count_truth(db, spec)
                    try:
                        reply = A.chat(db, owner, [{"role": "user", "content": said}], lang, settings)
                        text, reads, error = reply.text, [t["name"] for t in reply.tools], ""
                    except Exception as exc:  # noqa: BLE001 — модель не ответила: «неверно»
                        text, reads, error = "", [], f"{type(exc).__name__}: {exc}"
                    db.rollback()
                verdict = count_verdict(text, truth)
                rows.append({"said": said, "lang": lang, "spec": list(spec), "truth": truth, "verdict": verdict,
                             "reads": reads, "text": text[:400], "error": error[:200],
                             "seconds": round(time.monotonic() - started, 1)})
                log(f"{n:3d} {verdict:8s} {said[:42]:42s} ждали {truth} | {reads} | {text[:90]!r} {error[:50]}")
                if pause:
                    time.sleep(pause)
        finally:
            engine.dispose()
    return rows


def counts_summary(rows: list[dict]) -> str:
    n = len(rows)
    ok = sum(r["verdict"] == "верно" for r in rows)
    lines = [f"Вопросов: {n}. Верно {ok} ({100 * ok / max(1, n):.0f} %). "
             f"Среднее время {sum(r['seconds'] for r in rows) / max(1, n):.1f} с."]
    for lang in dict.fromkeys(r["lang"] for r in rows):
        part = [r for r in rows if r["lang"] == lang]
        lines.append(f"  {lang}: верно {sum(r['verdict'] == 'верно' for r in part)} из {len(part)}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="адрес модели, например http://127.0.0.1:8095/v1")
    ap.add_argument("--model", default="qwen3-vl-4b")
    ap.add_argument("--only", default="", help="только фразы с этим текстом")
    ap.add_argument("--out", default="", help="записать подробности в JSON")
    ap.add_argument("--pause", type=float, default=0.0, help="пауза между фразами, с (видеокарта остывает)")
    ap.add_argument("--set", choices=("phrases", "dialogs", "counts", "owner"), default="phrases",
                    help="phrases — 126 фраз (ml/assistant_phrases); dialogs — разговоры (ml/assistant_dialogs); "
                         "counts — вопросы «сколько» (ml/assistant_counts); owner — всё, что спрашивает владелец, "
                         "на 4 языках, с «Сделать» на временной базе (ml/assistant_owner_questions)")
    ap.add_argument("--copy", default=str(SHOP_COPY), help="копия базы магазина для --set counts (SQLite)")
    ap.add_argument("--catalog", default=str(BIG_CATALOG), help="копия товаров для длинного каталога (JSON)")
    a = ap.parse_args(argv)
    if a.set == "counts":
        from ml.assistant_counts import QUESTIONS  # noqa: PLC0415
        rows = run_counts(a.url, a.model, [q for q in QUESTIONS if a.only.lower() in q[0].lower()], Path(a.copy),
                          pause=a.pause)
        print(counts_summary(rows))
    elif a.set in ("dialogs", "owner"):
        if a.set == "owner":
            from ml.assistant_owner_questions import DIALOGS  # noqa: PLC0415
        else:
            from ml.assistant_dialogs import DIALOGS  # noqa: PLC0415
        dialogs = [d for d in DIALOGS if a.only.lower() in d["id"].lower()]
        rows = run_dialogs(a.url, a.model, dialogs, Path(a.catalog), pause=a.pause)
        print(dialog_summary(rows))
    else:
        phrases = [p for p in PHRASES if a.only.lower() in p[0].lower()]
        rows = run(a.url, a.model, phrases, pause=a.pause)
        print(summary(rows))
    if a.out:
        Path(a.out).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
