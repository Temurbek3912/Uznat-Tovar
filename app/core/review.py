"""
Логика решения при ручной проверке (F12): что должно измениться у detection в
ответ на действие человека. Чистая функция — не знает про SQLAlchemy/БД/Celery,
поэтому реально выполняется и тестируется в sandbox (tests/test_review.py).

DB-слой (app/services/review_service.py) вызывает decide_review(), применяет
результат к ORM-объекту и, если нужно, ставит в очередь Celery-задачу
переиндексации (app/worker.py: reindex_correction_task) — сама функция здесь
ничего не пишет и ничего не запускает.

Модель ревью намеренно узкая, ровно под формулировку ТЗ («очередь неуверенных
распознаваний»): ревью доступно только для detection со статусом
DetectionStatus.UNCERTAIN — детектор нашёл похожий, но недостаточно уверенный
товар (см. ProductMatcher.match: unknown_threshold <= score < match_threshold).
Разбор статуса UNKNOWN (детектор вообще не нашёл кандидата — возможно, товар
есть, но каталог его ещё не знает) — сознательно другая задача (обнаружение
пробела в каталоге, а не проверка кандидата) и в этой сессии не реализована,
см. docs/HANDOFF.md, раздел «F12».
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

_UNCERTAIN = "uncertain"  # DetectionStatus.UNCERTAIN.value — как строка, чтобы не тянуть pydantic-схему
_MATCHED = "matched"
_UNKNOWN = "unknown"


class ReviewAction(str, Enum):
    CONFIRMED = "confirmed"            # предложенный алгоритмом product_id — верный
    CORRECTED = "corrected"             # алгоритм ошибся; реальный товар — corrected_product_id
    MARKED_UNKNOWN = "marked_unknown"   # это вообще не товар из каталога — ложное "uncertain"


class ReviewValidationError(ValueError):
    """Действие не подходит текущему состоянию detection — 422 на уровне HTTP, не 500."""


@dataclass(frozen=True)
class ReviewOutcome:
    new_status: str
    new_matched_product_id: str | None
    # True только для CORRECTED: сигнал ReviewService поставить в очередь Celery-задачу
    # переиндексации (добавить этот crop как ещё одно эталонное фото reindex_product_id).
    triggers_reindex: bool
    reindex_product_id: str | None


def decide_review(
    *,
    current_status: str,
    matched_product_id: str | None,
    action: str,
    corrected_product_id: str | None,
) -> ReviewOutcome:
    """Решает, что должно произойти с detection — ничего не меняет сама.

    Бросает ReviewValidationError на любое несоответствие (неверный статус,
    неизвестное действие, отсутствующий обязательный параметр) — вызывающая
    сторона превращает это в HTTP 422/400, не 500.
    """
    if current_status != _UNCERTAIN:
        raise ReviewValidationError(
            f"Ревью доступно только для статуса '{_UNCERTAIN}' (у detection статус {current_status!r})"
        )

    try:
        parsed_action = ReviewAction(action)
    except ValueError:
        valid = ", ".join(a.value for a in ReviewAction)
        raise ReviewValidationError(f"Неизвестное действие {action!r}. Допустимые: {valid}") from None

    if parsed_action is ReviewAction.CONFIRMED:
        if matched_product_id is None:
            # Не должно происходить в норме (у uncertain всегда есть кандидат — см. matcher.py),
            # но защищаемся от рассинхронизации данных явной ошибкой, а не тихим "matched" в никуда.
            raise ReviewValidationError("Нечего подтверждать — у этого detection нет предложенного товара")
        return ReviewOutcome(
            new_status=_MATCHED, new_matched_product_id=matched_product_id,
            triggers_reindex=False, reindex_product_id=None,
        )

    if parsed_action is ReviewAction.CORRECTED:
        if not corrected_product_id:
            raise ReviewValidationError("Для действия 'corrected' нужно указать corrected_product_id")
        return ReviewOutcome(
            new_status=_MATCHED, new_matched_product_id=corrected_product_id,
            triggers_reindex=True, reindex_product_id=corrected_product_id,
        )

    # ReviewAction.MARKED_UNKNOWN
    return ReviewOutcome(
        new_status=_UNKNOWN, new_matched_product_id=None,
        triggers_reindex=False, reindex_product_id=None,
    )
