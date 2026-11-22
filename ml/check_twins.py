"""
Правило двойника и строгий режим — честная проверка на фото каталога (замер 2026-11-23,
docs/benchmark/twins_report_2026-11-23.md).

    .venv/Scripts/python.exe -m ml.check_twins                 # таблица: сейчас / двойник 0.75…0.85 / строгий режим
    .venv/Scripts/python.exe -m ml.check_twins --twins 0.8     # только эти значения

Только ЧИТАЕТ базу и индекс (векторы фото — из кэша базы, как ml/train_projection.py). Ничего не пишет.

Как считается. Снимки — фото товаров, которых проекция не видела (деление товаров на 5 частей, как в ml.train_projection);
копии того же фото убраны. «Двойник» (decide_match, twin_threshold) — наибольшее сходство эталонных фото двух товаров в
каталоге, как в программе: каталог — с проекцией, обученной на ВСЕХ товарах (так устроен рабочий индекс). Если проекция в
config.yaml выключена — всё без проекции.

Что смотреть: «из них неверно» (уверенные ошибки — то, что клиент увидит как «программа перепутала») и «чужой товар принят»
(товара нет в каталоге, а программа уверенно назвала другой). Цена — меньше ответов «найдено», больше «похоже, выберите».
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from typing import Sequence

import numpy as np

from app.core.schemas import DetectionStatus
from app.pipeline.matcher import decide_match
from ml.train_projection import (PARTS, base_thresholds, load_catalog_vectors, product_parts, project, ranked_lists,
                                 train_matrix)

TWINS = (0.75, 0.78, 0.80, 0.82, 0.85)
STRICT = (0.86, 0.88, 0.90)


def twin_table(z: np.ndarray, owners: Sequence[str]):
    """-> функция twin(a, b): наибольшее сходство фото товаров a и b по векторам z (как ProductMatcher.twin_similarity)."""
    owners = np.asarray(owners)
    products = sorted(set(owners))
    pos = {p: n for n, p in enumerate(products)}
    ow = np.array([pos[p] for p in owners])
    sims = z @ z.T
    table = np.full((len(products), len(products)), -1.0, np.float32)
    for a in range(len(products)):
        np.maximum.at(table[a], ow, sims[ow == a].max(0))
    return lambda a, b: float(table[pos[a], pos[b]]) if a != b else -1.0


def tally(ranked: dict, strangers: dict, owners, decide) -> Counter:
    c = Counter()
    for i, r in ranked.items():
        c["n"] += 1
        c["top1"] += bool(r) and r[0][0] == owners[i]
        d = decide(r)
        if d.status == DetectionStatus.MATCHED:
            c["confident"] += 1
            c["wrong"] += d.product_id != owners[i]
        elif d.status == DetectionStatus.UNCERTAIN:
            c["uncertain"] += 1
            c["uncertain_in5"] += owners[i] in [p for p, _ in r[:5]]
    c["strangers"] = len(strangers)
    c["stranger_accepted"] = sum(decide(r).status == DetectionStatus.MATCHED for r in strangers.values())
    return c


def _pct(a, b) -> str:
    return f"{100 * a / b:.1f} %".replace(".", ",") if b else "—"


def row(title: str, c: Counter) -> str:
    return (f"| {title} | {_pct(c['confident'], c['n'])} ({c['confident']}) | {c['wrong']} | "
            f"{c['uncertain']} (верный в пятёрке {c['uncertain_in5']}) | {_pct(c['stranger_accepted'], c['strangers'])} |")


def check(vectors, owners, matrix_full, match: float, margin: float, unknown: float, twins=TWINS, strict=STRICT,
          projected: bool = True, log=print) -> list[str]:
    owners = np.asarray(owners)
    ranked, strangers = {}, {}
    if projected:
        split = product_parts(owners, PARTS)
        for k in range(PARTS):
            test_p = split[(k + 1) % PARTS]
            train_idx = np.where(~np.isin(owners, list(split[k] | test_p)))[0]
            test_q = np.where(np.isin(owners, list(test_p)))[0]
            z = project(vectors, train_matrix(vectors[train_idx], owners[train_idx]))
            ranked.update(ranked_lists(z, vectors, owners, test_q))
            strangers.update(ranked_lists(z, vectors, owners, test_q, stranger=True))
            log(f"  часть {k + 1} из {PARTS}")
        twin = twin_table(project(vectors, matrix_full), owners)
    else:
        ranked = ranked_lists(vectors, vectors, owners, range(len(owners)))
        strangers = ranked_lists(vectors, vectors, owners, range(len(owners)), stranger=True)
        twin = twin_table(vectors, owners)

    def rule(m, t):
        return lambda r: decide_match(r, m, unknown, margin, t, twin)

    lines = [f"Снимков: {len(ranked)}; пороги {match:.2f} / запас {margin:.2f}.", "",
             "| Способ | «Найдено» | Из них неверно | «Похоже» | Чужой товар принят |", "|---|---|---|---|---|",
             row("Сейчас, без правила двойника", tally(ranked, strangers, owners, rule(match, 0.0)))]
    for t in twins:
        lines.append(row(f"Двойник {t:.2f}", tally(ranked, strangers, owners, rule(match, t))))
    t = 0.80 if 0.80 in twins or not twins else twins[0]
    for m in strict:
        lines.append(row(f"Строгий: найдено от {m:.2f}, двойник {t:.2f}", tally(ranked, strangers, owners, rule(max(m, match), t))))
    return lines


def main(argv: Sequence[str] | None = None, settings=None, make_session=None, out=print) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--twins", type=float, nargs="*", default=list(TWINS), help="значения порога двойника")
    ap.add_argument("--strict", type=float, nargs="*", default=list(STRICT), help="пороги «найдено» строгого режима")
    ap.add_argument("--threads", type=int, default=4)
    a = ap.parse_args(argv)
    import torch  # noqa: PLC0415
    torch.set_num_threads(max(1, a.threads))
    if settings is None:
        from app.core.config import get_settings  # noqa: PLC0415
        settings = get_settings()
    if settings.matcher is None or settings.embedder is None:
        out("В configs/config.yaml не заданы models.embedder / models.matcher")
        return 2
    vectors, owners, _front, signature, _missing = load_catalog_vectors(settings, make_session)
    if len(set(owners)) < 2 * PARTS:
        out(f"Слишком мало товаров для проверки (нужно хотя бы {2 * PARTS}).")
        return 2
    projected = bool(getattr(settings.embedder, "projection_path", ""))
    m = settings.matcher
    match, margin = (float(m.match_threshold), float(m.margin_threshold))
    if not projected:
        match, margin = base_thresholds(settings)
    out(f"Модель: {signature.get('name')}; векторов {len(owners)}, товаров {len(set(owners))}; "
        f"проекция {'включена' if projected else 'выключена'}")
    full = train_matrix(vectors, owners) if projected else None
    for line in check(vectors, owners, full, match, margin, float(m.unknown_threshold), tuple(a.twins), tuple(a.strict),
                      projected, log=out):
        out(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
