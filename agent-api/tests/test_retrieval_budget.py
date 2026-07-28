"""時間予算・呼び出し回数管理のテスト (計画書 §11.2, §11.3, §16.3)。"""

import pytest

from app.retrieval_budget import (
    COMPONENT_GRAPH,
    COMPONENT_RERANK,
    COMPONENT_SEARCH,
    PROFILE_CODE_DEFAULT,
    PROFILE_EVAL_OPERATION,
    BudgetTracker,
    RerankBudget,
    TimeProfile,
    current_profile,
    shadow_phase_budget_sec,
)


class TestTimeProfile:
    def test_code_default_profile(self) -> None:
        profile = TimeProfile(PROFILE_CODE_DEFAULT, 110, 60, 90)
        assert profile.full_answer_safe_reserve_sec == 90
        assert profile.minimum_reserve_exploration_budget_sec == 50
        assert profile.full_answer_safe_exploration_budget_sec == 20

    def test_eval_operation_profile(self) -> None:
        profile = TimeProfile(PROFILE_EVAL_OPERATION, 280, 60, 180)
        assert profile.full_answer_safe_reserve_sec == 180
        assert profile.minimum_reserve_exploration_budget_sec == 220
        assert profile.full_answer_safe_exploration_budget_sec == 100

    def test_reserve_shorter_than_llm_timeout_warns(self) -> None:
        warnings = TimeProfile(PROFILE_CODE_DEFAULT, 110, 60, 90).warnings()
        assert any("AGENT_ANSWER_RESERVE_SEC" in warning for warning in warnings)

    def test_component_allocation_overflow_warns(self) -> None:
        """呼び出し回数込みの割当時間合計が探索予算を超えたら警告する (§11.2)。"""
        warnings = TimeProfile(PROFILE_CODE_DEFAULT, 110, 60, 90).warnings()
        assert any("full-answer-safe探索予算" in warning for warning in warnings)

    def test_no_warning_when_reserve_covers_llm_timeout(self) -> None:
        warnings = TimeProfile("custom", 600, 200, 90).warnings()
        assert not any("AGENT_ANSWER_RESERVE_SEC" in warning for warning in warnings)

    def test_profile_name_is_detected_from_settings(self) -> None:
        assert current_profile().name in {
            PROFILE_CODE_DEFAULT,
            PROFILE_EVAL_OPERATION,
            "custom",
        }

    def test_trace_contains_budget_fields(self) -> None:
        trace = TimeProfile(PROFILE_EVAL_OPERATION, 280, 60, 180).as_trace()
        assert trace["profileName"] == PROFILE_EVAL_OPERATION
        assert trace["fullAnswerSafeExplorationBudgetMs"] == 100_000
        assert trace["componentMaxInvocations"][COMPONENT_SEARCH] >= 1


class TestBudgetTracker:
    def test_effective_timeout_is_clamped_by_remaining_budget(self) -> None:
        tracker = BudgetTracker(
            profile=TimeProfile("custom", 600, 200, 90),
            started=0.0,
            exploration_budget_sec=2.0,
        )
        # now を明示して残り1秒とみなす。設定timeout(15秒)より短くなる。
        assert tracker.effective_timeout(COMPONENT_SEARCH, now=1.0) == pytest.approx(1.0)

    def test_effective_timeout_is_zero_after_deadline(self) -> None:
        tracker = BudgetTracker(started=0.0, exploration_budget_sec=1.0)
        assert tracker.effective_timeout(COMPONENT_GRAPH, now=5.0) == 0.0

    def test_invocation_limits_are_enforced(self) -> None:
        tracker = BudgetTracker(exploration_budget_sec=60.0)
        assert tracker.can_invoke(COMPONENT_SEARCH, max_invocations=2) is True
        tracker.record(COMPONENT_SEARCH, items=4, elapsed_ms=100)
        tracker.record(COMPONENT_SEARCH, items=3, elapsed_ms=120)
        assert tracker.can_invoke(COMPONENT_SEARCH, max_invocations=2) is False

    def test_records_items_per_invocation_for_trace(self) -> None:
        tracker = BudgetTracker(exploration_budget_sec=60.0)
        tracker.record(COMPONENT_RERANK, items=16, elapsed_ms=800)
        trace = tracker.as_trace()
        assert trace["componentActualInvocations"][COMPONENT_RERANK] == 1
        assert trace["componentItemsPerInvocation"][COMPONENT_RERANK] == [16]
        assert trace["componentElapsedMs"][COMPONENT_RERANK] == 800

    def test_can_continue_is_false_without_remaining_budget(self) -> None:
        tracker = BudgetTracker(started=0.0, exploration_budget_sec=0.0)
        assert tracker.can_continue() is False


class TestShadowBudget:
    def test_shadow_budget_uses_half_of_safe_remainder(self) -> None:
        profile = TimeProfile(PROFILE_EVAL_OPERATION, 280, 60, 180)
        budget = shadow_phase_budget_sec(
            deadline=280.0,
            now=0.0,
            profile=profile,
            configured_budget_sec=999.0,
            remaining_fraction=0.5,
        )
        assert budget == pytest.approx(50.0)

    def test_configured_budget_is_the_upper_bound(self) -> None:
        profile = TimeProfile(PROFILE_EVAL_OPERATION, 280, 60, 180)
        budget = shadow_phase_budget_sec(
            deadline=280.0, now=0.0, profile=profile, configured_budget_sec=20.0
        )
        assert budget == pytest.approx(20.0)

    def test_shadow_budget_is_zero_when_reserve_consumes_remainder(self) -> None:
        profile = TimeProfile(PROFILE_CODE_DEFAULT, 110, 60, 90)
        assert shadow_phase_budget_sec(deadline=100.0, now=30.0, profile=profile) == 0.0


class TestRerankBudget:
    def test_per_call_and_total_limits_are_applied(self) -> None:
        budget = RerankBudget(max_pairs_per_call=16, max_pairs_total=32)
        assert budget.allowed_pairs(30, used_pairs=0) == 16
        assert budget.allowed_pairs(30, used_pairs=24) == 8
        assert budget.allowed_pairs(30, used_pairs=32) == 0
