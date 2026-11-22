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
from datetime import datetime
from pathlib import Path
from typing import Sequence

from ml.assistant_phrases import ARCHIVED, CATALOG, PHRASES


def make_catalog(path: Path):
    from sqlalchemy import create_engine  # noqa: PLC0415
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

    from app.db import migrate  # noqa: PLC0415
    from app.db.models import PRODUCT_ARCHIVED, Product, ProductBarcodeORM  # noqa: PLC0415
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    migrate.upgrade_to_head(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with session() as db:
        for n, (sku, name, brand, category, price, code) in enumerate(CATALOG + ARCHIVED):
            p = Product(sku=sku, name=name, brand=brand, category=category, expected_price=price,
                        created_at=datetime(2026, 11, 1 + n))
            if (sku, name, brand, category, price, code) in ARCHIVED:
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
    a = ap.parse_args(argv)
    phrases = [p for p in PHRASES if a.only.lower() in p[0].lower()]
    rows = run(a.url, a.model, phrases, pause=a.pause)
    print(summary(rows))
    if a.out:
        Path(a.out).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
