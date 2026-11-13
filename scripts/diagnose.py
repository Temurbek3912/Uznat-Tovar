"""
Диагностика базы данных — ТОЛЬКО ЧТЕНИЕ. Ничего не создаёт, не изменяет и не удаляет (даже таблицы).

    docker compose exec api python -m scripts.diagnose

Отвечает на вопросы, на которые раньше нельзя было получить внятный ответ:
  • К КАКОЙ базе подключено приложение (адрес без пароля)?
  • Это ОШИБКА подключения, «таблиц нет» или «таблицы есть, но данных нет»? (три разных состояния)
  • Сколько магазинов, полок, товаров, позиций планограмм, пользователей, снимков, камер?
  • Совпадают ли идентификаторы: нет ли снимков и планограмм у полок, которые не заведены в магазинах,
    и нет ли снимков, у которых магазин не тот, к которому относится полка?
  • Какие пользователи есть (логин, роль, активен ли, сколько магазинов) — без хешей паролей.

Коды выхода: 0 — база прочитана (предупреждения возможны); 1 — база недоступна; 2 — подключение есть, но таблиц нет
(выполните: docker compose run --rm bootstrap).
"""
from __future__ import annotations

import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

MAX_EXAMPLES = 8


@dataclass
class Inputs:
    """Всё, что прочитано из БД, в виде простых структур (чтобы анализ был чистой функцией и тестировался офлайн)."""
    counts: dict[str, int] = field(default_factory=dict)                  # users, active_admins, stores, shelves, products, planogram_slots, snapshots, cameras
    snapshot_status: dict[str, int] = field(default_factory=dict)         # status -> число
    shelves: list[tuple[str, str]] = field(default_factory=list)          # (shelf_id, store_id) заведённых полок
    snapshot_pairs: list[tuple[str, str]] = field(default_factory=list)   # (store_id, shelf_id) различные пары из снимков
    planogram_shelf_ids: list[str] = field(default_factory=list)          # различные shelf_id из планограмм
    users: list[tuple[str, str, bool, int]] = field(default_factory=list)  # (логин, роль, активен, число магазинов)


@dataclass(frozen=True)
class Finding:
    level: str      # "ok" | "info" | "warn"
    text: str


def _clip(items: Iterable[str], limit: int = MAX_EXAMPLES) -> str:
    items = sorted(items)
    shown = ", ".join(items[:limit])
    return shown + (f" … и ещё {len(items) - limit}" if len(items) > limit else "")


def analyze(data: Inputs) -> list[Finding]:
    """Выводы по прочитанным данным. Чистая функция."""
    out: list[Finding] = []
    c = data.counts
    registered = {shelf for shelf, _ in data.shelves}
    shelf_store = dict(data.shelves)

    # --- пустая БД — это не ошибка
    if c.get("stores", 0) == 0:
        out.append(Finding("info", "Магазинов в базе нет. Это НЕ ошибка подключения: таблицы читаются, они просто пусты. "
                                   "Для «Узнать товар» магазины не нужны: достаточно каталога товаров."))
    if c.get("users", 0) == 0:
        out.append(Finding("warn", "Пользователей нет — войти нельзя. Создайте администратора: docker compose run --rm bootstrap"))
    elif c.get("active_admins", 0) == 0:
        out.append(Finding("warn", "Нет ни одного АКТИВНОГО администратора: управлять системой через интерфейс нельзя. "
                                   "Включить существующего: python -m scripts.user_admin activate --username <логин>"))

    # --- согласованность идентификаторов
    orphan_snapshots = {(st, sh) for st, sh in data.snapshot_pairs if sh not in registered}
    if orphan_snapshots:
        out.append(Finding("warn", f"Есть снимки у полок, не заведённых в магазинах ({len(orphan_snapshots)}): "
                                   f"{_clip(sh for _, sh in orphan_snapshots)}. Отчёты по ним сохраняются, но полки нет в списках; "
                                   f"это данные прежнего аудита полок: они сохранены, но в интерфейсе их больше нет."))
    mismatched = {(st, sh) for st, sh in data.snapshot_pairs if sh in shelf_store and shelf_store[sh] != st}
    if mismatched:
        out.append(Finding("warn", f"Снимки загружены с другим магазином, чем тот, к которому относится полка ({len(mismatched)}): "
                                   f"{_clip(sh for _, sh in mismatched)} — вероятно, при загрузке перепутаны store_id и shelf_id."))
    orphan_plan = set(data.planogram_shelf_ids) - registered
    if orphan_plan:
        out.append(Finding("warn", f"Планограммы есть у полок, которых нет в магазинах ({len(orphan_plan)}): {_clip(orphan_plan)}."))
    planned = set(data.planogram_shelf_ids) & registered
    without_plan = registered - planned
    if registered and without_plan:
        out.append(Finding("info", f"Полок без планограммы: {len(without_plan)} из {len(registered)} — для них отчёт бессмысленен "
                                   f"(данные прежнего аудита полок сохранены, но больше не используются)."))
    if c.get("products", 0) == 0 and c.get("planogram_slots", 0) > 0:
        out.append(Finding("warn", "Есть позиции планограмм, но каталог товаров пуст — ссылки планограмм указывают в пустоту."))

    snaps = c.get("snapshots", 0)
    stuck = data.snapshot_status.get("queued", 0) + data.snapshot_status.get("processing", 0)
    if stuck:
        out.append(Finding("info", f"Снимков в очереди/обработке: {stuck}. Если число не уменьшается — смотрите docker compose logs worker."))
    if data.snapshot_status.get("failed", 0):
        out.append(Finding("warn", f"Снимков с ошибкой обработки: {data.snapshot_status['failed']} из {snaps}."))

    if not any(f.level == "warn" for f in out):
        out.append(Finding("ok", "Несоответствий идентификаторов и явных проблем с данными не найдено."))
    return out


def render(target: str, tables_ok: bool, missing: list[str], data: Inputs | None, findings: list[Finding]) -> str:
    lines = [f"База данных: {target}", ""]
    if not tables_ok:
        lines.append(f"Подключение есть, но таблиц не хватает: {', '.join(missing)}")
        lines.append("Выполните: docker compose run --rm bootstrap   (создаёт недостающие таблицы, существующие данные не трогает)")
        return "\n".join(lines)
    assert data is not None
    labels = [("users", "Пользователей"), ("stores", "Магазинов"), ("shelves", "Полок"), ("products", "Товаров (SKU)"),
              ("planogram_slots", "Позиций планограмм"), ("snapshots", "Снимков"), ("cameras", "Камер")]
    for key, label in labels:
        if key in data.counts:
            lines.append(f"  {label:<20} {data.counts[key]}")
    if data.snapshot_status:
        lines.append("  Снимки по статусам:   " + ", ".join(f"{k}: {v}" for k, v in sorted(data.snapshot_status.items())))
    if data.users:
        lines += ["", "Пользователи (пароли и хеши не выводятся):"]
        for name, role, active, stores in sorted(data.users):
            lines.append(f"  {name:<24} {role:<18} {'активен' if active else 'ОТКЛЮЧЁН':<9} магазинов: {stores}")
    lines += ["", "Выводы:"]
    mark = {"ok": "[OK]  ", "info": "[INFO]", "warn": "[WARN]"}
    lines += [f"  {mark[f.level]} {f.text}" for f in findings]
    return "\n".join(lines)


# ------------------------------------------------------------------ чтение БД


def collect(db, table_names: set[str]) -> Inputs:
    from sqlalchemy import func, select

    from app.db.models import (PlanogramSlotORM, Product, ShelfORM, ShelfSnapshot, Store, User)

    def count(model, *where) -> int:
        return int(db.execute(select(func.count()).select_from(model).where(*where)).scalar_one())

    data = Inputs()
    data.counts = {
        "users": count(User), "active_admins": count(User, User.role == "admin", User.is_active.is_(True)),
        "stores": count(Store), "shelves": count(ShelfORM), "products": count(Product),
        "planogram_slots": count(PlanogramSlotORM), "snapshots": count(ShelfSnapshot),
    }
    if "cameras" in table_names:
        from app.db.models import CameraORM
        data.counts["cameras"] = count(CameraORM)
    data.snapshot_status = {str(s): int(n) for s, n in db.execute(
        select(ShelfSnapshot.status, func.count()).group_by(ShelfSnapshot.status)).all()}
    data.shelves = [(str(sh), str(st)) for sh, st in db.execute(select(ShelfORM.shelf_id, ShelfORM.store_id)).all()]
    data.snapshot_pairs = [(str(st), str(sh)) for st, sh in db.execute(
        select(ShelfSnapshot.store_id, ShelfSnapshot.shelf_id).distinct()).all()]
    data.planogram_shelf_ids = [str(sh) for (sh,) in db.execute(select(PlanogramSlotORM.shelf_id).distinct()).all()]
    for user in db.execute(select(User)).scalars().all():
        data.users.append((user.username, user.role, bool(user.is_active), len(user.store_access)))
    return data


def main(out: Callable[[str], None] = print) -> int:
    from sqlalchemy import inspect, text

    from app.db.models import Base
    from app.db.session import SessionLocal, engine

    target = engine.url.render_as_string(hide_password=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — любая причина недоступности — это «БД недоступна», а не «данных нет»
        out(f"База данных: {target}\n")
        out(f"НЕ УДАЛОСЬ ПОДКЛЮЧИТЬСЯ ({type(exc).__name__}). Это ошибка подключения, а не отсутствие данных.")
        out("Проверьте: docker compose ps (db должен быть healthy); docker compose logs db; "
            "совпадают ли POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB в .env с теми, с которыми создан том pgdata.")
        return 1

    present = set(inspect(engine).get_table_names())
    missing = sorted(set(Base.metadata.tables) - present)
    core_missing = [t for t in missing if t in {"users", "stores", "shelves", "products", "planogram_slots", "shelf_snapshots"}]
    if core_missing:
        out(render(target, False, core_missing, None, []))
        return 2

    db = SessionLocal()
    try:
        data = collect(db, present)
    finally:
        db.close()
    findings = analyze(data)
    if missing:
        findings.insert(0, Finding("info", f"Новых таблиц ещё нет: {', '.join(missing)} — будут созданы при следующем "
                                           f"docker compose up -d (существующие данные не затрагиваются)."))
    out(render(target, True, [], data, findings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
