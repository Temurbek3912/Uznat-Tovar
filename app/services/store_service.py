"""
Список магазинов с полками для клиентов (телефонное приложение, агент камер).

Чистая функция без SQLAlchemy: принимает уже выбранные строки как обычные объекты, поэтому реально
тестируется офлайн (tests/test_store_service.py). Чтение из БД и фильтр по скоупу пользователя — в
app/api/routes_stores.py.
"""
from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from typing import Iterable, Mapping

from app.core.app_errors import AppError
from app.core.security import is_safe_path_segment


def build_store_listing(
    stores: Iterable,
    shelves: Iterable,
    snapshot_shelf_pairs: Iterable[tuple[str, str]],
    slot_counts: Mapping[str, int],
) -> list[dict]:
    """
    stores: объекты с id/name/address/format; shelves: объекты со store_id/shelf_id/name (реестр ShelfORM);
    snapshot_shelf_pairs: (store_id, shelf_id) из ранее загруженных снимков; slot_counts: shelf_id -> число слотов.

    Полки из реестра имеют приоритет. Полки, по которым уже есть снимки, но которых нет в реестре
    (демо-данные, снимки до появления реестра), добавляются с registered=False и именем = коду, чтобы
    их можно было выбрать в приложении. Магазины и полки отсортированы по имени (без учёта регистра).
    """
    by_store: dict[str, dict[str, dict]] = {}

    for sh in shelves:
        by_store.setdefault(sh.store_id, {})[sh.shelf_id] = {
            "shelf_id": sh.shelf_id, "name": sh.name, "registered": True,
            "planogram_slots": int(slot_counts.get(sh.shelf_id, 0)),
        }
    for store_id, shelf_id in snapshot_shelf_pairs:
        by_store.setdefault(store_id, {}).setdefault(shelf_id, {
            "shelf_id": shelf_id, "name": shelf_id, "registered": False,
            "planogram_slots": int(slot_counts.get(shelf_id, 0)),
        })

    result = []
    for st in sorted(stores, key=lambda s: (s.name or "").lower()):
        shelf_list = sorted(by_store.get(st.id, {}).values(), key=lambda x: x["name"].lower())
        result.append({
            "id": st.id, "name": st.name, "address": st.address, "format": st.format, "shelves": shelf_list,
        })
    return result


# ---------------------------------------------------------------------------------------------------------
# Создание магазинов и полок (POST /api/v1/stores, POST /api/v1/stores/{id}/shelves). Тоже чистая логика без БД.
# ---------------------------------------------------------------------------------------------------------

STORE_FORMATS = ("hypermarket", "supermarket", "convenience")
MAX_SHELVES_PER_REQUEST = 500        # большой магазин — сотни полок; больше за один запрос — почти наверняка ошибка клиента
MAX_NAME_LENGTH = 200
MAX_CODE_LENGTH = 64                 # автогенерируемый код; явный может быть до 128 (см. is_safe_path_segment)


class StoreInputError(AppError, ValueError):
    """Некорректные данные магазина/полки. Текст ошибки — для пользователя (показывается в интерфейсе).
    Единая система ошибок: вид invalid (HTTP 422), код и параметры — для перевода сообщения."""

    kind = "invalid"
    default_code = "store_input_invalid"


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    # узбекская кириллица
    "ў": "o", "қ": "q", "ғ": "g", "ҳ": "h",
}


def slugify(text: str, max_len: int = MAX_CODE_LENGTH) -> str:
    """«Напитки, стеллаж 1» -> «napitki-stellazh-1». Латиница/цифры, слова через «-». Пустая строка, если букв нет."""
    out = []
    for ch in (text or "").lower():
        out.append(_TRANSLIT.get(ch, ch))
    slug = re.sub(r"[^a-z0-9]+", "-", "".join(out)).strip("-")
    return slug[:max_len].strip("-")


def validate_store_fields(name: str, address: str | None, fmt: str | None) -> tuple[str, str, str]:
    """-> (название, адрес, формат) в каноничном виде. Адрес необязателен (в БД колонка NOT NULL — пишем пустую строку)."""
    clean_name = (name or "").strip()
    if not clean_name:
        raise StoreInputError("Название магазина не может быть пустым", "store_name_required")
    if len(clean_name) > MAX_NAME_LENGTH:
        raise StoreInputError(f"Название магазина длиннее {MAX_NAME_LENGTH} символов", "store_name_too_long", {"max": MAX_NAME_LENGTH})
    clean_address = (address or "").strip()
    if len(clean_address) > MAX_NAME_LENGTH * 2:
        raise StoreInputError("Адрес слишком длинный", "store_address_too_long", {"max": MAX_NAME_LENGTH * 2})
    clean_format = (fmt or "supermarket").strip().lower()
    if clean_format not in STORE_FORMATS:
        raise StoreInputError(f"Формат магазина: {', '.join(STORE_FORMATS)}", "store_format_invalid", {"formats": ", ".join(STORE_FORMATS)})
    return clean_name, clean_address, clean_format


def plan_shelves(
    items: Sequence[tuple[str | None, str]],
    taken_codes: Collection[str],
    store_name: str = "",
) -> list[tuple[str, str]]:
    """Полки запроса -> [(код, название)] с уникальными кодами.

    items: (явный код или None/«», название). Код полки глобально уникален (планограмма ключуется одним shelf_id),
    поэтому автогенерируемый код = «<магазин>-<полка>», при совпадении с занятым или с соседней полкой запроса
    добавляется «-2», «-3» … Явный код не «исправляется» молча: занятый или недопустимый — ошибка (кто его задал,
    тот на него и рассчитывает — например, в CSV планограммы).
    """
    if len(items) > MAX_SHELVES_PER_REQUEST:
        raise StoreInputError(f"За один раз можно добавить не больше {MAX_SHELVES_PER_REQUEST} полок", "shelves_per_request_limit",
                              {"max": MAX_SHELVES_PER_REQUEST})

    used = {c for c in taken_codes}
    explicit = [(code or "").strip() for code, _ in items]
    # Явные коды резервируем заранее: автокод одной полки не должен занять код, который явно задан у другой.
    seen_explicit: set[str] = set()
    for code in explicit:
        if not code:
            continue
        if not is_safe_path_segment(code):
            raise StoreInputError(
                f"Код полки «{code}» недопустим: латиница, цифры и . _ - (начинается с буквы или цифры, без пробелов)",
                "shelf_code_invalid", {"code": code})
        if code in used or code in seen_explicit:
            raise StoreInputError(f"Код полки «{code}» уже занят", "shelf_code_taken", {"code": code})
        seen_explicit.add(code)
    reserved = used | seen_explicit

    prefix = slugify(store_name, 24)
    result: list[tuple[str, str]] = []
    for (_, raw_name), code in zip(items, explicit):
        name = (raw_name or "").strip()
        if not name:
            raise StoreInputError("Название полки не может быть пустым", "shelf_name_required")
        if len(name) > MAX_NAME_LENGTH:
            raise StoreInputError(f"Название полки «{name[:30]}…» длиннее {MAX_NAME_LENGTH} символов", "shelf_name_too_long",
                                  {"max": MAX_NAME_LENGTH})
        if not code:
            base = "-".join(part for part in (prefix, slugify(name, 36)) if part) or "shelf"
            code, n = base, 2
            while code in reserved:
                code, n = f"{base}-{n}", n + 1
            reserved.add(code)
        result.append((code, name))
    return result


# ---------------------------------------------------------------------------------------------------------
# Защита от случайного удаления: объекты, на которые опираются снимки и отчёты, не удаляются
# ---------------------------------------------------------------------------------------------------------

class DeleteBlocker(str):
    """Причина запрета удаления: сама строка — прежний русский текст (как раньше), `code` и `params` — для перевода на странице
    (ключ server.<code>; единая система ошибок)."""

    code: str
    params: dict

    def __new__(cls, code: str, text: str, **params):
        obj = super().__new__(cls, text)
        obj.code = code
        obj.params = dict(params)
        return obj


def shelf_delete_blockers(snapshots: int, cameras: int, planogram_slots: int, with_planogram: bool) -> list[str]:
    """Причины, по которым полку удалять НЕЛЬЗЯ (пустой список — можно). Снимки и отчёты не удаляются никогда."""
    reasons = []
    if snapshots:
        reasons.append(DeleteBlocker("blocker_shelf_snapshots",
                                     f"по полке есть снимки ({snapshots}) — удалить её значило бы осиротить отчёты", n=snapshots))
    if cameras:
        reasons.append(DeleteBlocker("blocker_shelf_cameras",
                                     f"к полке привязаны камеры ({cameras}) — сначала удалите или перепривяжите их", n=cameras))
    if planogram_slots and not with_planogram:
        reasons.append(DeleteBlocker("blocker_shelf_planogram",
                                     f"у полки есть планограмма ({planogram_slots} позиций) — подтвердите её удаление вместе с полкой",
                                     n=planogram_slots))
    return reasons


def store_delete_blockers(shelves: int, cameras: int, snapshots: int, users: int) -> list[str]:
    """Магазин удаляется только пустым: без полок, камер, снимков и закреплённых за ним пользователей."""
    reasons = []
    if shelves:
        reasons.append(DeleteBlocker("blocker_store_shelves", f"в магазине есть полки ({shelves}) — удалите их сначала", n=shelves))
    if cameras:
        reasons.append(DeleteBlocker("blocker_store_cameras", f"в магазине есть камеры ({cameras})", n=cameras))
    if snapshots:
        reasons.append(DeleteBlocker("blocker_store_snapshots", f"по магазину есть снимки ({snapshots}) — они нужны отчётам", n=snapshots))
    if users:
        reasons.append(DeleteBlocker("blocker_store_users",
                                     f"за магазином закреплены пользователи ({users}) — снимите доступ в разделе «Пользователи»", n=users))
    return reasons
