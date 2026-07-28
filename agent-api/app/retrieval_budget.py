"""探索フェーズの時間予算と呼び出し回数の管理。

計画書 §11.2(グローバル安全予算)、§11.3(shadow専用予算)、§13(traceのtimeBudget)に対応する。

`AGENT_ANSWER_RESERVE_SEC`は探索スケジューラが最低限残す時間、`LLM_TIMEOUT_SEC`は回答LLMが
使い得る上限であり、意味が違う。回答がtimeout上限まで使っても壊れないよう、性能判定には
`fullAnswerSafeExplorationBudget`を使う。
"""

from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from .config import settings

COMPONENT_PLANNER = "planner"
COMPONENT_EMBEDDING = "embedding"
COMPONENT_SEARCH = "opensearch"
COMPONENT_GRAPH = "neo4j"
COMPONENT_RERANK = "rerank"
COMPONENT_EVALUATOR = "evaluator"
COMPONENT_REPLAN = "replan"

PROFILE_CODE_DEFAULT = "code_default_110s"
PROFILE_EVAL_OPERATION = "eval_operation_280s"
PROFILE_CUSTOM = "custom"


@dataclass(frozen=True)
class TimeProfile:
    """採用している時間profileと、そこから決まる探索予算(§11.2)。"""

    name: str
    wall_time_sec: int
    minimum_answer_reserve_sec: int
    llm_timeout_sec: int

    @property
    def full_answer_safe_reserve_sec(self) -> int:
        return max(self.minimum_answer_reserve_sec, self.llm_timeout_sec)

    @property
    def minimum_reserve_exploration_budget_sec(self) -> int:
        return max(0, self.wall_time_sec - self.minimum_answer_reserve_sec)

    @property
    def full_answer_safe_exploration_budget_sec(self) -> int:
        return max(0, self.wall_time_sec - self.full_answer_safe_reserve_sec)

    def warnings(self) -> tuple[str, ...]:
        """起動時・health・traceへ出す設定警告。値は自動で書き換えない(§11.2)。"""
        messages: list[str] = []
        if self.minimum_answer_reserve_sec < self.llm_timeout_sec:
            messages.append(
                "AGENT_ANSWER_RESERVE_SEC < LLM_TIMEOUT_SEC: "
                f"{self.minimum_answer_reserve_sec}s < {self.llm_timeout_sec}s. "
                "回答LLMがtimeout上限まで使うとwall timeを超える可能性がある。"
            )
        if self.full_answer_safe_exploration_budget_sec <= 0:
            messages.append(
                "full-answer-safe探索予算が0秒。wall timeまたは回答予約時間の設定を見直す。"
            )
        allocated = _configured_component_allocation_sec()
        if allocated > self.full_answer_safe_exploration_budget_sec:
            messages.append(
                "componentの設定timeout×最大呼び出し回数の合計"
                f"({allocated}s)がfull-answer-safe探索予算"
                f"({self.full_answer_safe_exploration_budget_sec}s)を超えている。"
                "Phase 0の実測後に割当値または採用profileを再設定する。"
            )
        return tuple(messages)

    def as_trace(self) -> dict[str, Any]:
        return {
            "profileName": self.name,
            "wallTimeMs": self.wall_time_sec * 1000,
            "minimumAnswerReserveMs": self.minimum_answer_reserve_sec * 1000,
            "llmTimeoutMs": self.llm_timeout_sec * 1000,
            "fullAnswerSafeReserveMs": self.full_answer_safe_reserve_sec * 1000,
            "fullAnswerSafeExplorationBudgetMs": (
                self.full_answer_safe_exploration_budget_sec * 1000
            ),
            "componentConfiguredTimeoutMs": {
                component: seconds * 1000
                for component, seconds in _configured_component_timeouts().items()
            },
            "componentMaxInvocations": dict(_component_max_invocations()),
            "warnings": list(self.warnings()),
        }


def current_profile() -> TimeProfile:
    wall = settings.agent_max_wall_time_sec
    reserve = settings.agent_answer_reserve_sec
    llm_timeout = settings.llm_timeout_sec
    name = settings.agent_time_profile_name or _profile_name(wall, reserve, llm_timeout)
    return TimeProfile(
        name=name,
        wall_time_sec=wall,
        minimum_answer_reserve_sec=reserve,
        llm_timeout_sec=llm_timeout,
    )


def _profile_name(wall: int, reserve: int, llm_timeout: int) -> str:
    if (wall, reserve, llm_timeout) == (110, 60, 90):
        return PROFILE_CODE_DEFAULT
    if (wall, reserve, llm_timeout) == (280, 60, 180):
        return PROFILE_EVAL_OPERATION
    return PROFILE_CUSTOM


def _configured_component_timeouts() -> dict[str, int]:
    return {
        COMPONENT_PLANNER: settings.planner_timeout_sec,
        COMPONENT_EMBEDDING: settings.embedding_timeout_sec,
        COMPONENT_SEARCH: 15,
        COMPONENT_GRAPH: 15,
        COMPONENT_RERANK: settings.rerank_timeout_sec,
        COMPONENT_EVALUATOR: settings.evaluator_timeout_sec,
        COMPONENT_REPLAN: settings.evaluator_timeout_sec,
    }


def _component_max_invocations() -> dict[str, int]:
    return {
        COMPONENT_PLANNER: 1,
        COMPONENT_EMBEDDING: settings.layered_max_embedding_batch_calls_total,
        COMPONENT_SEARCH: settings.layered_max_search_batch_calls_total,
        COMPONENT_GRAPH: settings.layered_max_graph_batch_calls_total,
        COMPONENT_RERANK: settings.layered_max_rerank_calls_total,
        COMPONENT_EVALUATOR: 1,
        COMPONENT_REPLAN: settings.layered_max_replan_calls,
    }


def _configured_component_allocation_sec() -> int:
    timeouts = _configured_component_timeouts()
    invocations = _component_max_invocations()
    return sum(timeouts[component] * invocations.get(component, 0) for component in timeouts)


class BudgetTracker:
    """共有deadlineと呼び出し回数を追跡する。

    実効timeoutは `min(componentConfiguredTimeout, explorationDeadline - now)` とし、
    外部呼び出しが探索予算を超えて走らないようにする(§11.2)。
    """

    def __init__(
        self,
        *,
        profile: TimeProfile | None = None,
        started: float | None = None,
        exploration_budget_sec: float | None = None,
    ) -> None:
        self.profile = profile or current_profile()
        self.started = started if started is not None else perf_counter()
        budget = (
            exploration_budget_sec
            if exploration_budget_sec is not None
            else float(self.profile.full_answer_safe_exploration_budget_sec)
        )
        self.exploration_deadline = self.started + max(0.0, budget)
        self.invocations: dict[str, int] = {}
        self.items_per_invocation: dict[str, list[int]] = {}
        self.elapsed_ms: dict[str, int] = {}
        self.allocated_ms: dict[str, int] = {}

    def remaining_sec(self, now: float | None = None) -> float:
        return max(0.0, self.exploration_deadline - (now if now is not None else perf_counter()))

    def can_continue(self, minimum_sec: float = 0.5) -> bool:
        return self.remaining_sec() > minimum_sec

    def effective_timeout(self, component: str, now: float | None = None) -> float:
        configured = float(_configured_component_timeouts().get(component, 15))
        return max(0.0, min(configured, self.remaining_sec(now)))

    def can_invoke(self, component: str, *, max_invocations: int | None = None) -> bool:
        limit = (
            max_invocations
            if max_invocations is not None
            else _component_max_invocations().get(component, 0)
        )
        return self.invocations.get(component, 0) < limit and self.can_continue()

    def record(self, component: str, *, items: int = 0, elapsed_ms: int = 0) -> None:
        self.invocations[component] = self.invocations.get(component, 0) + 1
        self.items_per_invocation.setdefault(component, []).append(items)
        self.elapsed_ms[component] = self.elapsed_ms.get(component, 0) + elapsed_ms

    def as_trace(self) -> dict[str, Any]:
        trace = self.profile.as_trace()
        trace.update(
            {
                "componentActualInvocations": dict(self.invocations),
                "componentItemsPerInvocation": {
                    component: list(items) for component, items in self.items_per_invocation.items()
                },
                "componentElapsedMs": dict(self.elapsed_ms),
                "componentEffectiveTimeoutMs": {
                    component: int(self.effective_timeout(component) * 1000)
                    for component in _configured_component_timeouts()
                },
                "explorationRemainingMs": int(self.remaining_sec() * 1000),
            }
        )
        return trace


def shadow_phase_budget_sec(
    *,
    deadline: float,
    now: float,
    profile: TimeProfile | None = None,
    configured_budget_sec: float | None = None,
    remaining_fraction: float | None = None,
) -> float:
    """shadowが回答前の安全余白を使い切らないようにする(§11.3)。

    LAYERED_SHADOW_PHASE_BUDGET_SEC
      = min(configured shadow budget,
            max(0, deadline - now - fullAnswerSafeReserve) * LAYERED_SHADOW_REMAINING_FRACTION)
    """
    resolved_profile = profile or current_profile()
    configured = (
        configured_budget_sec
        if configured_budget_sec is not None
        else float(settings.layered_shadow_phase_budget_sec)
    )
    fraction = (
        remaining_fraction
        if remaining_fraction is not None
        else settings.layered_shadow_remaining_fraction
    )
    remaining = max(0.0, deadline - now - resolved_profile.full_answer_safe_reserve_sec)
    return max(0.0, min(configured, remaining * fraction))


@dataclass(frozen=True)
class RerankBudget:
    """Cross-Encoderのper-call / per-round / request全体の3段階上限(§11.2)。"""

    max_pairs_per_call: int = field(default_factory=lambda: settings.layered_max_rerank_pairs_per_call)
    max_pairs_total: int = field(default_factory=lambda: settings.layered_max_rerank_pairs_total)
    max_calls_per_round: int = field(default_factory=lambda: settings.layered_max_rerank_calls_per_round)
    max_calls_total: int = field(default_factory=lambda: settings.layered_max_rerank_calls_total)

    def allowed_pairs(self, requested: int, *, used_pairs: int) -> int:
        """per-call上限とrequest全体上限の両方を満たす件数だけ返す。"""
        return max(0, min(requested, self.max_pairs_per_call, self.max_pairs_total - used_pairs))
