"""Тесты чистой логики F12 (app/core/review.py): decide_review — реально выполняются офлайн."""
from __future__ import annotations

import pytest

from app.core.review import ReviewAction, ReviewOutcome, ReviewValidationError, decide_review


class TestConfirmed:
    def test_happy_path_keeps_suggested_product(self):
        outcome = decide_review(
            current_status="uncertain", matched_product_id="p-cola",
            action="confirmed", corrected_product_id=None,
        )
        assert outcome == ReviewOutcome(
            new_status="matched", new_matched_product_id="p-cola",
            triggers_reindex=False, reindex_product_id=None,
        )

    def test_confirmed_never_triggers_reindex(self):
        # Подтверждение — не новый визуальный пример, эмбеддинг уже был достаточно похож,
        # чтобы дать кандидата (иначе detection был бы "unknown", а не "uncertain").
        outcome = decide_review(
            current_status="uncertain", matched_product_id="p-cola",
            action="confirmed", corrected_product_id=None,
        )
        assert outcome.triggers_reindex is False

    def test_stray_corrected_product_id_is_ignored_when_confirming(self):
        # Клиент прислал оба поля — доверяем action, а не заодно переключаем product_id.
        outcome = decide_review(
            current_status="uncertain", matched_product_id="p-cola",
            action="confirmed", corrected_product_id="p-fanta",
        )
        assert outcome.new_matched_product_id == "p-cola"

    def test_no_suggested_product_is_a_validation_error_not_a_silent_match(self):
        with pytest.raises(ReviewValidationError, match="Нечего подтверждать"):
            decide_review(current_status="uncertain", matched_product_id=None,
                           action="confirmed", corrected_product_id=None)


class TestCorrected:
    def test_happy_path_switches_product_and_triggers_reindex(self):
        outcome = decide_review(
            current_status="uncertain", matched_product_id="p-cola",
            action="corrected", corrected_product_id="p-fanta",
        )
        assert outcome == ReviewOutcome(
            new_status="matched", new_matched_product_id="p-fanta",
            triggers_reindex=True, reindex_product_id="p-fanta",
        )

    def test_works_even_when_detector_had_no_candidate_at_all(self):
        # matched_product_id теоретически может быть None и у "uncertain" (данные повреждены/
        # ручной импорт) — 'corrected' всё равно должен уметь назначить товар с нуля.
        outcome = decide_review(
            current_status="uncertain", matched_product_id=None,
            action="corrected", corrected_product_id="p-fanta",
        )
        assert outcome.new_matched_product_id == "p-fanta"

    @pytest.mark.parametrize("bad_value", [None, ""])
    def test_missing_corrected_product_id_is_rejected(self, bad_value):
        with pytest.raises(ReviewValidationError, match="corrected_product_id"):
            decide_review(current_status="uncertain", matched_product_id="p-cola",
                           action="corrected", corrected_product_id=bad_value)


class TestMarkedUnknown:
    def test_happy_path_clears_product_and_never_reindexes(self):
        outcome = decide_review(
            current_status="uncertain", matched_product_id="p-cola",
            action="marked_unknown", corrected_product_id=None,
        )
        assert outcome == ReviewOutcome(
            new_status="unknown", new_matched_product_id=None,
            triggers_reindex=False, reindex_product_id=None,
        )

    def test_stray_corrected_product_id_is_ignored(self):
        outcome = decide_review(
            current_status="uncertain", matched_product_id="p-cola",
            action="marked_unknown", corrected_product_id="p-fanta",
        )
        assert outcome.new_matched_product_id is None
        assert outcome.triggers_reindex is False


class TestStatusGuard:
    @pytest.mark.parametrize("status", ["matched", "unknown", "empty_slot", "reviewed", ""])
    def test_only_uncertain_can_be_reviewed(self, status):
        with pytest.raises(ReviewValidationError, match="Ревью доступно только"):
            decide_review(current_status=status, matched_product_id="p-cola",
                           action="confirmed", corrected_product_id=None)

    def test_already_matched_cannot_be_reviewed_again(self):
        # Прямая защита от повторного ревью: после первого review статус в БД становится
        # "matched" (см. review_service.py), второй вызов должен упасть, а не тихо перезаписать.
        with pytest.raises(ReviewValidationError):
            decide_review(current_status="matched", matched_product_id="p-cola",
                           action="corrected", corrected_product_id="p-fanta")


class TestActionValidation:
    @pytest.mark.parametrize("bad_action", ["Confirmed", "CONFIRMED", "confirm", "reject", "", "unknown_action"])
    def test_unknown_or_mis_cased_action_is_rejected(self, bad_action):
        with pytest.raises(ReviewValidationError, match="Неизвестное действие"):
            decide_review(current_status="uncertain", matched_product_id="p-cola",
                           action=bad_action, corrected_product_id=None)

    def test_error_message_lists_valid_actions(self):
        with pytest.raises(ReviewValidationError) as exc_info:
            decide_review(current_status="uncertain", matched_product_id="p-cola",
                           action="bogus", corrected_product_id=None)
        for action in ReviewAction:
            assert action.value in str(exc_info.value)


class TestOutcomeIsPlainDataNotUsedForSideEffects:
    def test_outcome_is_frozen(self):
        outcome = decide_review(current_status="uncertain", matched_product_id="p-cola",
                                 action="confirmed", corrected_product_id=None)
        with pytest.raises(Exception):
            outcome.new_status = "unknown"

    def test_only_corrected_ever_sets_reindex_product_id(self):
        for action, corrected in [("confirmed", None), ("marked_unknown", None)]:
            outcome = decide_review(current_status="uncertain", matched_product_id="p-cola",
                                     action=action, corrected_product_id=corrected)
            assert outcome.reindex_product_id is None
        outcome = decide_review(current_status="uncertain", matched_product_id="p-cola",
                                 action="corrected", corrected_product_id="p-fanta")
        assert outcome.reindex_product_id == "p-fanta"
