"""
Обучение лёгкой проекции поверх векторов SigLIP 2 на фото каталога (app/pipeline/projection.py; замер 2026-11-22,
docs/benchmark/projection_report_2026-11-22.md).

    .venv/Scripts/python.exe -m ml.train_projection                       # проверка + файл models/projection/siglip2-base.npz
    .venv/Scripts/python.exe -m ml.train_projection --check-only          # только честная проверка, файл не пишется
    .venv/Scripts/python.exe -m ml.train_projection --out D:/опыт/p.npz   # файл в другое место
    .venv/Scripts/python.exe -m ml.train_projection --skip-check --out models/projection/siglip2-base-shop-ft.npz
        # для дообученной модели (ml/finetune_embedder.py): она видела все товары каталога, и проверка здесь была бы
        # нечестной (завышенной) — честная проверка проекции поверх неё — ml.finetune_check (векторы cv_*.npy)

Только ЧИТАЕТ базу и индекс: векторы фото — из кэша базы (image_embeddings) для модели рабочего индекса, без видеокарты и
без пересчёта фото. Пишет только файл проекции и отчёт рядом с ним. Индекс, база и config.yaml не меняются.

Честная проверка (до записи файла). Товары делятся на 5 частей: на трёх проекция учится, на четвёртой подбираются пороги
«найдено», на пятой — замер (по кругу, каждая часть по разу — замер). Снимки замера — фото товаров, которых проекция НЕ
видела; каталог — все товары. Как в ml/benchmark_similar.py: каждое фото по очереди — снимок, копии того же товара
(сходство > 0,93 по векторам БЕЗ проекции) убраны. Пороги подбираются так, чтобы доля уверенных ошибок и доля «узнанных»
чужих товаров были на 10 % ниже, чем у программы сейчас (match_threshold / margin_threshold из config.yaml).

Включить проекцию (с согласия владельца):
  1) models.embedder.projection_path: "models/projection/siglip2-base.npz" и пороги из отчёта (match_threshold, margin_threshold)
     в configs/config.yaml;
  2) python -m scripts.rebuild_index --apply --compute-missing — векторы с проекцией считаются из кэша базы, без пересчёта фото
     (копия прежнего индекса — рядом). Вернуть как было: projection_path "" и прежние пороги, затем та же пересборка.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Sequence

import numpy as np

from app.core.schemas import DetectionStatus
from app.pipeline.matcher import decide_match

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COPY_SIMILARITY = 0.93
DEPTH = 25
PARTS = 5
RECIPE = {"epochs": 200, "lr": 1e-3, "weight_decay": 1e-2, "temperature": 0.05, "batch": 4096, "seed": 0}
MATCH_GRID = tuple(round(x, 2) for x in np.arange(0.70, 0.951, 0.01))
MARGIN_GRID = (0.0, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08)
SAFETY = 0.9                                    # пороги подбираются с запасом: доли ошибок на 10 % ниже нынешних


# --------------------------------------------------------------------------- обучение


def train_matrix(vectors: np.ndarray, labels: Sequence[str], recipe: dict | None = None) -> np.ndarray:
    """Матрица A (d×d): фото одного товара ближе друг к другу (контрастная потеря по всем парам фото, температура recipe),
    A тянется к нулю (weight_decay) — проекция остаётся близкой к исходным векторам. Процессор, несколько секунд."""
    import torch  # noqa: PLC0415
    r = {**RECIPE, **(recipe or {})}
    torch.manual_seed(int(r["seed"]))
    rng = np.random.default_rng(int(r["seed"]))
    x_all = torch.tensor(np.asarray(vectors, dtype=np.float32))
    lab_all = np.unique(np.asarray(labels), return_inverse=True)[1]
    d = x_all.shape[1]
    a = torch.zeros(d, d, requires_grad=True)
    opt = torch.optim.Adam([a], lr=float(r["lr"]))
    products = np.unique(lab_all)
    for _ in range(int(r["epochs"])):
        if len(lab_all) <= int(r["batch"]):
            idx = np.arange(len(lab_all))
        else:                                   # большой каталог: случайные товары целиком, пока не наберётся batch фото
            take, total = [], 0
            for p in rng.permutation(products):
                rows = np.where(lab_all == p)[0]
                take.append(rows)
                total += len(rows)
                if total >= int(r["batch"]):
                    break
            idx = np.concatenate(take)
        x = x_all[idx]
        lab = torch.tensor(lab_all[idx])
        eye = torch.eye(len(idx))
        pos = (lab[:, None] == lab[None, :]).float() - eye
        has = pos.sum(1) > 0
        if not bool(has.any()):
            continue
        z = torch.nn.functional.normalize(x + x @ a, dim=-1)
        s = z @ z.T / float(r["temperature"]) - 1e4 * eye
        logp = s - torch.logsumexp(s, 1, keepdim=True)
        loss = (-(logp * pos).sum(1)[has] / pos.sum(1)[has]).mean() + float(r["weight_decay"]) * (a ** 2).sum()
        opt.zero_grad()
        loss.backward()
        opt.step()
    return a.detach().numpy().astype(np.float32)


def project(vectors: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    z = vectors + vectors @ matrix
    return (z / np.linalg.norm(z, axis=1, keepdims=True)).astype(np.float32)


# --------------------------------------------------------------------------- проверка


def product_parts(owners: Sequence[str], parts: int = PARTS, seed: int = 0) -> list[set[str]]:
    """Товары -> parts непересекающихся частей (все фото товара в одной части)."""
    products = sorted(set(owners))
    order = np.random.default_rng(seed).permutation(len(products))
    return [{products[j] for j in order[k::parts]} for k in range(parts)]


def ranked_lists(z: np.ndarray, raw: np.ndarray, owners: np.ndarray, queries: Sequence[int], stranger: bool = False,
                 copy: float = COPY_SIMILARITY, depth: int = DEPTH) -> dict[int, list[tuple[str, float]]]:
    """Снимок i -> лучший счёт на товар (по векторам z). Копии того же товара — по исходным векторам raw (проекция их
    сближает, и без этого замер незаметно включил бы копии). stranger — из каталога убраны все фото товара снимка."""
    out: dict[int, list[tuple[str, float]]] = {}
    for i in queries:
        same = owners == owners[i]
        allowed = np.ones(len(owners), bool)
        allowed[i] = False
        if stranger:
            allowed &= ~same
        else:
            allowed &= ~(same & ((raw @ raw[i]) > copy))
            if not (allowed & same).any():
                continue
        row = z @ z[i]
        best: dict[str, float] = {}
        for j in np.argsort(-row):
            if allowed[j] and owners[j] not in best:
                best[str(owners[j])] = float(row[j])
                if len(best) >= depth:
                    break
        out[i] = list(best.items())
    return out


def tally(ranked: dict, strangers: dict, owners: np.ndarray, front: np.ndarray, match: float, margin: float,
          unknown: float) -> Counter:
    c = Counter()
    for i, r in ranked.items():
        ok = bool(r) and r[0][0] == owners[i]
        c["n"] += 1
        c["top1"] += ok
        c["top5"] += owners[i] in [p for p, _ in r[:5]]
        c["front_n"] += bool(front[i])
        c["front_top1"] += bool(front[i]) and ok
        d = decide_match(r, match, unknown, margin)
        if d.status == DetectionStatus.MATCHED:
            c["confident"] += 1
            c["wrong"] += d.product_id != owners[i]
    c["strangers"] = len(strangers)
    c["stranger_accepted"] = sum(decide_match(r, match, unknown, margin).status == DetectionStatus.MATCHED
                                 for r in strangers.values())
    return c


def pick_thresholds(ranked: dict, strangers: dict, owners, front, unknown: float, max_wrong: float, max_stranger: float):
    """(match, margin) с наибольшим числом уверенных ответов при доле ошибок ≤ max_wrong и доле принятых чужих ≤ max_stranger."""
    best = None
    for match in MATCH_GRID:
        for margin in MARGIN_GRID:
            c = tally(ranked, strangers, owners, front, match, margin, unknown)
            if not c["confident"]:
                continue
            if c["wrong"] / c["confident"] <= max_wrong and c["stranger_accepted"] / max(1, c["strangers"]) <= max_stranger:
                if best is None or c["confident"] > best[0]:
                    best = (c["confident"], match, margin)
    return (best[1], best[2]) if best else None


@dataclass
class CheckResult:
    before: Counter
    after_now: Counter           # с проекцией, пороги программы сейчас
    after: Counter               # с проекцией, подобранные пороги
    thresholds: tuple[float, float] | None
    now: tuple[float, float]


def cross_check(vectors: np.ndarray, owners: Sequence[str], front: Sequence[bool], now: tuple[float, float], unknown: float,
                recipe: dict | None = None, parts: int = PARTS, log=print) -> CheckResult:
    """Честная проверка по частям товаров (см. описание модуля)."""
    owners = np.asarray(owners)
    front = np.asarray(front, bool)
    split = product_parts(owners, parts)
    cal_r, cal_s, test_r, test_s, base_cal_r, base_cal_s, base_r, base_s = ({} for _ in range(8))
    for k in range(parts):
        cal_p, test_p = split[k], split[(k + 1) % parts]
        train_idx = np.where(~np.isin(owners, list(cal_p | test_p)))[0]
        cal_q = np.where(np.isin(owners, list(cal_p)))[0]
        test_q = np.where(np.isin(owners, list(test_p)))[0]
        z = project(vectors, train_matrix(vectors[train_idx], owners[train_idx], recipe))
        cal_r.update(ranked_lists(z, vectors, owners, cal_q))
        cal_s.update(ranked_lists(z, vectors, owners, cal_q, stranger=True))
        test_r.update(ranked_lists(z, vectors, owners, test_q))
        test_s.update(ranked_lists(z, vectors, owners, test_q, stranger=True))
        log(f"  часть {k + 1} из {parts}: обучение на {len(set(owners[train_idx]))} товарах, замер на {len(test_p)}")
    base_r = ranked_lists(vectors, vectors, owners, range(len(owners)))
    base_s = ranked_lists(vectors, vectors, owners, range(len(owners)), stranger=True)
    base_cal_r = {i: base_r[i] for i in cal_r if i in base_r}
    base_cal_s = {i: base_s[i] for i in cal_s}
    c0 = tally(base_cal_r, base_cal_s, owners, front, now[0], now[1], unknown)
    max_wrong = c0["wrong"] / c0["confident"] if c0["confident"] else 0.0
    max_stranger = c0["stranger_accepted"] / max(1, c0["strangers"])
    thresholds = pick_thresholds(cal_r, cal_s, owners, front, unknown, SAFETY * max_wrong, SAFETY * max_stranger)
    before = tally({i: base_r[i] for i in test_r}, {i: base_s[i] for i in test_s}, owners, front, now[0], now[1], unknown)
    after_now = tally(test_r, test_s, owners, front, now[0], now[1], unknown)
    after = tally(test_r, test_s, owners, front, *(thresholds or now), unknown)
    return CheckResult(before, after_now, after, thresholds, now)


def _pct(a: float, b: float) -> str:
    return f"{100 * a / b:.1f} %".replace(".", ",") if b else "—"


def render(check: CheckResult, n_vectors: int, n_products: int, recipe: dict) -> str:
    def row(title: str, c: Counter) -> str:
        return (f"| {title} | {_pct(c['top1'], c['n'])} | {_pct(c['top5'], c['n'])} | {_pct(c['front_top1'], c['front_n'])} | "
                f"{_pct(c['confident'], c['n'])} ({c['confident']}) | {c['wrong']} | "
                f"{_pct(c['stranger_accepted'], c['strangers'])} |")
    m_now, g_now = check.now
    lines = [f"# Проекция поверх векторов модели — проверка ({date.today().isoformat()}, ml/train_projection.py)", "",
             f"Векторов: {n_vectors}, товаров: {n_products}. Снимков замера: {check.before['n']} (лицевых "
             f"{check.before['front_n']}). Замер — на товарах, которых проекция не видела (деление на {PARTS} частей). "
             "Фото каталога, не снимки магазина.", "",
             "| Способ | Верный первым | В пятёрке | Первым, лицевые | «Найдено» | Из них неверно | Чужой товар принят |",
             "|---|---|---|---|---|---|---|",
             row(f"Без проекции ({m_now:.2f} / {g_now:.2f})", check.before),
             row(f"С проекцией, те же пороги ({m_now:.2f} / {g_now:.2f})", check.after_now)]
    if check.thresholds:
        lines.append(row(f"С проекцией, пороги {check.thresholds[0]:.2f} / {check.thresholds[1]:.2f}", check.after))
        lines += ["", f"Рекомендуемые пороги с проекцией: match_threshold {check.thresholds[0]:.2f}, margin_threshold "
                      f"{check.thresholds[1]:.2f} (подобраны на отдельной части товаров: доля уверенных ошибок и "
                      "принятых чужих товаров — на 10 % ниже, чем сейчас)."]
    else:
        lines += ["", "Пороги с проекцией подобрать не удалось — оставьте прежние."]
    lines += ["", f"Обучение: {json.dumps(recipe, ensure_ascii=False)}."]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- данные


def load_catalog_vectors(settings, make_session=None):
    """Векторы активных эталонов активных товаров из кэша базы для модели рабочего индекса (без проекции)."""
    from app.core.config import resolve_project_path  # noqa: PLC0415
    from app.db.models import Product  # noqa: PLC0415
    from app.pipeline.embedder import model_version  # noqa: PLC0415
    from app.pipeline.matcher import open_catalog_index  # noqa: PLC0415
    from app.pipeline.projection import PROJ_MARK, Projection  # noqa: PLC0415
    from app.services import product_images as P  # noqa: PLC0415
    index = open_catalog_index(str(resolve_project_path(settings.matcher.index_path)), settings.embedder.embedding_dim)
    signature = getattr(index, "model", None)
    if signature is None:
        raise SystemExit("В индексе не записана модель векторов — сначала python -m scripts.rebuild_index --apply")
    if PROJ_MARK in str(signature.get("preprocess", "")):
        path = settings.embedder.projection_path
        if not path:
            raise SystemExit("Индекс построен с проекцией, а в настройке её нет — нужен паспорт модели без проекции")
        signature = Projection.load(resolve_project_path(path)).base_signature
    version = model_version(signature)
    if make_session is None:
        from app.db.session import SessionLocal  # noqa: PLC0415
        make_session = SessionLocal
    db = make_session()
    try:
        products = {pid for pid, status in db.query(Product.id, Product.status).all() if status != "archived"}
        registry = P.PhotoRegistry(db)
        plan = P.plan_rebuild(registry, products, version)
        names = {img.id: (img.original_name or "") for img in registry.images(products)}
    finally:
        db.close()
    items = plan.cached
    if not items:
        raise SystemExit(f"В кэше базы нет векторов модели {signature.get('name')} — python -m scripts.rebuild_index --apply")
    vectors = np.stack([np.asarray(i.vector, dtype=np.float32) for i in items])
    owners = [i.product_id for i in items]
    front = ["/front" in names.get(i.image_id, "") for i in items]
    return vectors, owners, front, signature, len(plan.missing)


def base_thresholds(settings) -> tuple[float, float]:
    """Пороги программы БЕЗ проекции — с ними сравнивается проекция. Проекция уже включена (в config.yaml её пороги) —
    пороги без проекции берутся из её файла (base_thresholds записываются при обучении)."""
    m = settings.matcher
    path = getattr(settings.embedder, "projection_path", "") or ""
    if path:
        from app.core.config import resolve_project_path  # noqa: PLC0415
        from app.pipeline.projection import Projection  # noqa: PLC0415
        try:
            saved = Projection.load(resolve_project_path(path)).info.get("base_thresholds")
        except (OSError, ValueError, KeyError):
            saved = None
        if saved:
            return float(saved[0]), float(saved[1])
    return float(m.match_threshold), float(m.margin_threshold)


def main(argv: Sequence[str] | None = None, settings=None, make_session=None, out=print) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(PROJECT_ROOT / "models" / "projection" / "siglip2-base.npz"))
    ap.add_argument("--check-only", action="store_true", help="только проверка, файл проекции не пишется")
    ap.add_argument("--threads", type=int, default=4, help="потоков процессора (ноутбук не перегружается)")
    ap.add_argument("--skip-check", action="store_true",
                    help="без проверки здесь: для дообученной модели, её проверка — ml.finetune_check")
    a = ap.parse_args(argv)
    import torch  # noqa: PLC0415
    torch.set_num_threads(max(1, a.threads))
    if settings is None:
        from app.core.config import get_settings  # noqa: PLC0415
        settings = get_settings()
    if settings.matcher is None or settings.embedder is None:
        out("В configs/config.yaml не заданы models.embedder / models.matcher")
        return 2
    vectors, owners, front, signature, missing = load_catalog_vectors(settings, make_session)
    n_products = len(set(owners))
    out(f"Модель: {signature.get('name')}; векторов из кэша: {len(owners)}, товаров: {n_products}"
        + (f"; без вектора в кэше: {missing} (не участвуют)" if missing else ""))
    if n_products < 2 * PARTS:
        out(f"Слишком мало товаров для проверки (нужно хотя бы {2 * PARTS}).")
        return 2
    m = settings.matcher
    now = base_thresholds(settings)
    if a.skip_check:
        from app.pipeline.projection import Projection  # noqa: PLC0415
        info = {"trained": date.today().isoformat(), "vectors": len(owners), "products": n_products, "recipe": RECIPE,
                "base_thresholds": None, "thresholds": None, "check": "ml.finetune_check"}
        path = Projection(train_matrix(vectors, owners), signature, info).save(a.out)
        Path(path).with_suffix(".md").write_text(
            f"# Проекция для {signature.get('name')} ({date.today().isoformat()})\n\nВекторов: {len(owners)}, товаров: "
            f"{n_products}. Проверка здесь не делалась (--skip-check): модель дообучена на этих товарах, честный замер "
            "проекции поверх неё — ml.finetune_check.\n", encoding="utf-8")
        out(f"Файл проекции: {path} (без проверки здесь: --skip-check)")
        return 0
    out("Честная проверка (обучение на части товаров, замер на других):")
    check = cross_check(vectors, owners, front, now, m.unknown_threshold, log=out)
    report = render(check, len(owners), n_products, RECIPE)
    out(report)
    gain = check.after["top1"] - check.before["top1"]
    if a.check_only:
        return 0
    if gain <= 0 or check.after["wrong"] > check.before["wrong"]:
        out("Проекция не лучше, чем без неё (верный первым или уверенные ошибки) — файл не записан.")
        return 1
    from app.pipeline.projection import Projection  # noqa: PLC0415
    matrix = train_matrix(vectors, owners)
    info = {"trained": date.today().isoformat(), "vectors": len(owners), "products": n_products, "recipe": RECIPE,
            "base_thresholds": list(now),
            "thresholds": list(check.thresholds) if check.thresholds else None,
            "check": {k: dict(getattr(check, k)) for k in ("before", "after_now", "after")}}
    path = Projection(matrix, signature, info).save(a.out)
    Path(path).with_suffix(".md").write_text(report, encoding="utf-8")
    out(f"Файл проекции: {path} (отчёт: {Path(path).with_suffix('.md')})")
    if check.thresholds:
        shown = Path(a.out).resolve()
        shown = shown.relative_to(PROJECT_ROOT) if shown.is_relative_to(PROJECT_ROOT) else shown
        out(f"Включить: в configs/config.yaml models.embedder.projection_path: \"{shown.as_posix()}\", "
            f"match_threshold {check.thresholds[0]:.2f}, margin_threshold {check.thresholds[1]:.2f}; затем "
            "python -m scripts.rebuild_index --apply --compute-missing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
