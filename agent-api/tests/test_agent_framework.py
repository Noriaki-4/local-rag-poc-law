"""新しい汎用Agent FrameworkのPhase 1契約テスト。"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from threading import Barrier

import pytest

from app.adapters.persistence.simple_in_memory import InMemoryCaseStore
from app.agent_framework.context import SolverContext, build_solver_context
from app.agent_framework.contracts import (
    CaseUpdate,
    HypothesisUpdate,
    SolverDecision,
    WorkItemImpactDecision,
    WorkItemUpdate,
)
from app.agent_framework.loop import AgentLoop
from app.agent_framework.observability import RunTrace
from app.agent_framework.ports.model import (
    ReviewCallResult,
    ReviewContext,
    SolverCallResult,
    SolverCheckpointTimeout,
)
from app.agent_framework.ports.tool import (
    ToolDefinition,
    ToolExecution,
    ToolRegistry,
)
from app.agent_framework.profiles import (
    AgentLimits,
    AgentProfile,
    AutomaticToolProfile,
    ModelCallProfile,
    ReviewerProfile,
    ToolListArgumentLimit,
)
from app.agent_framework.state import (
    CaseState,
    DependencyDecision,
    Evidence,
    FinalAnswer,
    GraphCandidateReview,
    GraphFrontierDecision,
    Hypothesis,
    ReviewFinding,
    ReviewFindingResolution,
    ReviewResult,
    ToolRequest,
    ToolResult,
    WorkItem,
)
from app.agent_framework.validation import ContractViolation, apply_solver_decision

DecisionFactory = Callable[[SolverContext, ModelCallProfile], SolverDecision]


class FakeModel:
    def __init__(
        self,
        decisions: list[SolverDecision | DecisionFactory],
        reviews: list[ReviewResult] | None = None,
    ) -> None:
        self.decisions = decisions
        self.reviews = reviews or []
        self.solver_contexts: list[SolverContext] = []
        self.solver_profiles: list[ModelCallProfile] = []
        self.review_contexts: list[ReviewContext] = []

    def solve(
        self,
        context: SolverContext,
        profile: ModelCallProfile,
    ) -> SolverCallResult:
        self.solver_contexts.append(context)
        self.solver_profiles.append(profile)
        item = self.decisions.pop(0)
        decision = item(context, profile) if callable(item) else item
        return SolverCallResult(
            decision=decision,
            input_tokens=10,
            output_tokens=20,
        )

    def review(
        self,
        context: ReviewContext,
        profile: ReviewerProfile,
    ) -> ReviewCallResult:
        self.review_contexts.append(context)
        return ReviewCallResult(review=self.reviews.pop(0))


class FakeReadTool:
    def __init__(
        self,
        name: str = "search",
        *,
        read_only: bool = True,
        parallel_safe: bool = True,
        barrier: Barrier | None = None,
    ) -> None:
        self._definition = ToolDefinition(
            name=name,
            description="テスト用のread-only Tool。",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            result_description="テスト用Evidenceを返す。",
            read_only=read_only,
            parallel_safe=parallel_safe,
        )
        self.barrier = barrier
        self.calls: list[str] = []

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def execute(
        self,
        request: ToolRequest,
        *,
        cycle_no: int,
        timeout_sec: float,
    ) -> ToolExecution:
        self.calls.append(request.request_id)
        if self.barrier is not None:
            self.barrier.wait(timeout=2)
        evidence_id = f"e_{request.request_id}"
        evidence = Evidence(
            evidence_id=evidence_id,
            source_ref=f"fake://{request.request_id}",
            content=f"{request.request_id}の取得本文",
            created_cycle=cycle_no,
        )
        return ToolExecution(
            result=ToolResult(
                request_id=request.request_id,
                status="succeeded",
                evidence_ids=(evidence_id,),
                elapsed_ms=1,
                cycle_no=cycle_no,
            ),
            evidence=(evidence,),
        )


def _profile(
    *,
    reviewer_enabled: bool = False,
    max_revisions: int = 1,
    automatic_tools: tuple[AutomaticToolProfile, ...] = (),
) -> AgentProfile:
    return AgentProfile(
        name="test",
        provider="fake",
        solver_research=ModelCallProfile(
            model="research-model",
            system_prompt="research",
        ),
        solver_integration=ModelCallProfile(
            model="integration-model",
            system_prompt="integration",
        ),
        reviewer=ReviewerProfile(
            enabled=reviewer_enabled,
            max_revisions=max_revisions,
            model="review-model",
            system_prompt="review",
        ),
        automatic_tools=automatic_tools,
        limits=AgentLimits(
            max_wall_time_sec=120,
            next_solver_call_reserve_sec=30,
        ),
    )


def _run(
    model: FakeModel,
    *,
    tools: tuple[FakeReadTool, ...] = (),
    profile: AgentProfile | None = None,
) -> tuple[CaseState, RunTrace]:
    store = InMemoryCaseStore()
    store.create(CaseState(case_id="case-1", question="質問"))
    result = AgentLoop(
        store=store,
        model=model,
        tools=ToolRegistry(tools),
        profile=profile or _profile(),
    ).run("case-1")
    return result.state, result.trace


def _first_research(requests: tuple[ToolRequest, ...]) -> SolverDecision:
    return SolverDecision(
        next="continue",
        update=CaseUpdate(
            add_work_items=(WorkItem(work_item_id="w1", question="根拠を確認する"),),
            add_hypotheses=(
                Hypothesis(
                    hypothesis_id="h1",
                    work_item_id="w1",
                    statement="根拠が存在する",
                ),
            ),
        ),
        next_focus_work_item_ids=("w1",),
        tool_requests=requests,
    )


def _request(request_id: str) -> ToolRequest:
    return ToolRequest(
        request_id=request_id,
        work_item_id="w1",
        tool_name="search",
        purpose="仮説を確認する",
        hypothesis_ids=("h1",),
    )


def test_early_finalize_stops_without_tools_or_reviewer() -> None:
    model = FakeModel(
        [
            SolverDecision(
                next="finalize",
                answer=FinalAnswer(text="取得済み情報で回答"),
            )
        ]
    )

    state, trace = _run(model)

    assert state.run_status == "completed"
    assert state.research_cycle_count == 0
    assert len(model.solver_contexts) == 1
    assert model.review_contexts == []
    assert trace.reviewer_enabled is False


def test_one_cycle_passes_every_result_to_closing_solver_decision() -> None:
    model = FakeModel(
        [
            _first_research((_request("r1"),)),
            SolverDecision(
                next="finalize",
                update=CaseUpdate(
                    update_work_items=(
                        WorkItemUpdate(
                            work_item_id="w1",
                            state="resolved",
                            resolution="本文を確認した",
                            basis_hypothesis_ids=("h1",),
                        ),
                    ),
                    update_hypotheses=(
                        HypothesisUpdate(
                            hypothesis_id="h1",
                            judgment="supported",
                            evidence_ids=("e_r1",),
                        ),
                    ),
                ),
                answer=FinalAnswer(
                    text="根拠あり",
                    citation_ids=("e_r1",),
                ),
            ),
        ]
    )

    state, trace = _run(model, tools=(FakeReadTool(),))

    closing_context = model.solver_contexts[1]
    assert state.run_status == "completed"
    assert state.research_cycle_count == 1
    assert tuple(item.request_id for item in closing_context.recent_tool_results) == (
        "r1",
    )
    assert closing_context.material_evidence_ids == {"e_r1"}
    assert state.hypotheses[0].judgment == "supported"
    assert [item.model for item in trace.model_calls] == [
        "research-model",
        "integration-model",
    ]
    assert [item.purpose for item in trace.model_calls] == [
        "research",
        "integration",
    ]


def test_cycle_close_marks_its_integrated_tool_results_as_consumed() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_cycle_close_replays_integrated_result_v353.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    request_id = fixture["observed"]["requestId"]
    model = FakeModel(
        [
            SolverDecision(
                next="continue",
                start_next_cycle=True,
                next_focus_work_item_ids=("w1",),
                retain_evidence_ids=(f"e_{request_id}",),
            ),
            SolverDecision(
                next="finalize",
                update=CaseUpdate(
                    update_work_items=(
                        WorkItemUpdate(
                            work_item_id="w1",
                            state="resolved",
                            resolution="本文を確認した",
                            basis_hypothesis_ids=("h1",),
                        ),
                    ),
                    update_hypotheses=(
                        HypothesisUpdate(
                            hypothesis_id="h1",
                            judgment="supported",
                            evidence_ids=(f"e_{request_id}",),
                        ),
                    ),
                ),
                answer=FinalAnswer(
                    text="確認済み",
                    citation_ids=(f"e_{request_id}",),
                ),
            ),
        ]
    )
    profile = _profile().model_copy(
        update={
            "solver_cycle_close": ModelCallProfile(
                model="integration-model",
                system_prompt="cycle close",
            ),
            "limits": AgentLimits(
                max_wall_time_sec=120,
                next_solver_call_reserve_sec=30,
                max_fetched_resources_per_cycle=1,
            ),
        }
    )

    initial_state = CaseState(
        case_id="case-1",
        question="質問",
        research_cycle_count=1,
        work_items=(WorkItem(work_item_id="w1", question="根拠を確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="根拠が存在する",
            ),
        ),
        tool_requests=(
            ToolRequest(
                request_id=request_id,
                work_item_id="w1",
                tool_name="fetch_articles",
                arguments={"article_ids": ["article-1"]},
                purpose="本文を確認する",
                hypothesis_ids=("h1",),
            ),
        ),
        tool_results=(
            ToolResult(
                request_id=request_id,
                status="succeeded",
                evidence_ids=(f"e_{request_id}",),
                cycle_no=1,
            ),
        ),
        evidence=(
            Evidence(
                evidence_id=f"e_{request_id}",
                source_ref=f"fake://{request_id}",
                content="取得本文",
                created_cycle=1,
            ),
        ),
    )
    store = InMemoryCaseStore()
    store.create(initial_state)
    result = AgentLoop(
        store=store,
        model=model,
        tools=ToolRegistry(()),
        profile=profile,
    ).run("case-1")
    state = result.state

    assert model.solver_contexts[0].cycle_close_required is True
    assert tuple(
        item.request_id for item in model.solver_contexts[0].recent_tool_results
    ) == (request_id,)
    assert (
        bool(model.solver_contexts[1].recent_tool_results)
        is fixture["expected"]["wasPresentedAgainAfterCycleClose"]
    )
    assert request_id in state.integrated_tool_result_request_ids


def test_solver_can_explicitly_start_three_research_cycles() -> None:
    def after_first(context: SolverContext, _: ModelCallProfile) -> SolverDecision:
        return SolverDecision(
            next="continue",
            start_next_cycle=True,
            update=CaseUpdate(
                update_hypotheses=(
                    HypothesisUpdate(
                        hypothesis_id="h1",
                        judgment="unresolved",
                        evidence_ids=("e_r1",),
                        gaps=("追加確認",),
                    ),
                )
            ),
            next_focus_work_item_ids=("w1",),
            retain_evidence_ids=("e_r1",),
            tool_requests=(_request("r2"),),
        )

    def after_second(context: SolverContext, _: ModelCallProfile) -> SolverDecision:
        return SolverDecision(
            next="continue",
            start_next_cycle=True,
            update=CaseUpdate(
                update_hypotheses=(
                    HypothesisUpdate(
                        hypothesis_id="h1",
                        judgment="unresolved",
                        evidence_ids=("e_r1", "e_r2"),
                        gaps=("最終確認",),
                    ),
                )
            ),
            next_focus_work_item_ids=("w1",),
            retain_evidence_ids=("e_r1", "e_r2"),
            tool_requests=(_request("r3"),),
        )

    def close_third(context: SolverContext, _: ModelCallProfile) -> SolverDecision:
        return SolverDecision(
            next="finalize",
            update=CaseUpdate(
                update_work_items=(
                    WorkItemUpdate(
                        work_item_id="w1",
                        state="resolved",
                        resolution="3回の結果を評価した",
                        basis_hypothesis_ids=("h1",),
                    ),
                ),
                update_hypotheses=(
                    HypothesisUpdate(
                        hypothesis_id="h1",
                        judgment="supported",
                        evidence_ids=("e_r1", "e_r2", "e_r3"),
                    ),
                ),
            ),
            retain_evidence_ids=("e_r1", "e_r2", "e_r3"),
            answer=FinalAnswer(
                text="3件を統合した回答",
                citation_ids=("e_r1", "e_r2", "e_r3"),
            ),
        )

    model = FakeModel(
        [
            _first_research((_request("r1"),)),
            after_first,
            after_second,
            close_third,
        ]
    )

    state, trace = _run(model, tools=(FakeReadTool(),))

    assert state.run_status == "completed"
    assert state.research_cycle_count == 3
    assert len(model.solver_contexts) == 4
    assert model.solver_contexts[-1].finalize_only is False
    assert model.solver_contexts[-1].remaining_research_cycles == 1
    assert model.solver_contexts[-1].material_evidence_ids == {
        "e_r1",
        "e_r2",
        "e_r3",
    }
    assert [item.model for item in trace.model_calls] == [
        "research-model",
        "integration-model",
        "integration-model",
        "integration-model",
    ]


def test_solver_can_start_second_cycle_without_forcing_a_third() -> None:
    model = FakeModel(
        [
            _first_research((_request("r1"),)),
            SolverDecision(
                next="continue",
                start_next_cycle=True,
                next_focus_work_item_ids=("w1",),
                retain_evidence_ids=("e_r1",),
                tool_requests=(_request("r2"),),
            ),
            SolverDecision(
                next="finalize",
                update=CaseUpdate(
                    update_work_items=(
                        WorkItemUpdate(
                            work_item_id="w1",
                            state="resolved",
                            resolution="2回で十分と判断した",
                            basis_hypothesis_ids=("h1",),
                        ),
                    ),
                    update_hypotheses=(
                        HypothesisUpdate(
                            hypothesis_id="h1",
                            judgment="supported",
                            evidence_ids=("e_r1", "e_r2"),
                        ),
                    ),
                ),
                answer=FinalAnswer(
                    text="2件を評価した回答",
                    citation_ids=("e_r1", "e_r2"),
                ),
            ),
        ]
    )

    state, trace = _run(model, tools=(FakeReadTool(),))

    assert state.run_status == "completed"
    assert state.research_cycle_count == 2
    assert len(model.solver_contexts) == 3
    assert model.solver_contexts[-1].finalize_only is False
    assert [item.model for item in trace.model_calls] == [
        "research-model",
        "integration-model",
        "integration-model",
    ]


def test_cycle_boundary_hands_retained_evidence_to_next_cycle_planning() -> None:
    def close_first_cycle(
        context: SolverContext,
        _: ModelCallProfile,
    ) -> SolverDecision:
        assert context.research_cycle_count == 1
        return SolverDecision(
            next="continue",
            start_next_cycle=True,
            update=CaseUpdate(
                update_hypotheses=(
                    HypothesisUpdate(
                        hypothesis_id="h1",
                        judgment="unresolved",
                        evidence_ids=("e_r1",),
                        gaps=("追加本文が必要",),
                    ),
                ),
            ),
            next_focus_work_item_ids=("w1",),
            retain_evidence_ids=("e_r1",),
        )

    def plan_second_cycle(
        context: SolverContext,
        _: ModelCallProfile,
    ) -> SolverDecision:
        assert context.research_cycle_count == 2
        assert context.material_evidence_ids == {"e_r1"}
        assert context.hypotheses[0].evidence_ids == ("e_r1",)
        return SolverDecision(
            next="continue",
            next_focus_work_item_ids=("w1",),
            retain_evidence_ids=("e_r1",),
            tool_requests=(_request("r2"),),
        )

    model = FakeModel(
        [
            _first_research((_request("r1"),)),
            close_first_cycle,
            plan_second_cycle,
            SolverDecision(
                next="finalize",
                update=CaseUpdate(
                    update_work_items=(
                        WorkItemUpdate(
                            work_item_id="w1",
                            state="resolved",
                            resolution="2 Cycleの本文を確認した",
                            basis_hypothesis_ids=("h1",),
                        ),
                    ),
                    update_hypotheses=(
                        HypothesisUpdate(
                            hypothesis_id="h1",
                            judgment="supported",
                            evidence_ids=("e_r1", "e_r2"),
                        ),
                    ),
                ),
                answer=FinalAnswer(
                    text="2件を統合した回答",
                    citation_ids=("e_r1", "e_r2"),
                ),
            ),
        ]
    )

    state, _ = _run(model, tools=(FakeReadTool(),))

    assert state.run_status == "completed"
    assert state.research_cycle_count == 2


def test_contract_repair_budget_exhaustion_returns_to_reserved_finalization() -> None:
    class ManualClock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    clock = ManualClock()

    class BudgetModel(FakeModel):
        def solve(
            self,
            context: SolverContext,
            profile: ModelCallProfile,
        ) -> SolverCallResult:
            if len(self.solver_contexts) == 1:
                clock.now = 95.0
            return super().solve(context, profile)

    model = BudgetModel(
        [
            _first_research((_request("r1"),)),
            SolverDecision(
                next="finalize",
                answer=FinalAnswer(text="未完了なのに通常終了しようとした"),
            ),
            SolverDecision(
                next="finalize",
                answer=FinalAnswer(
                    text="時間上限内で確認できた範囲の回答",
                    limitations=("追加確認が必要",),
                    unresolved_work_item_ids=("w1",),
                    unresolved_hypothesis_ids=("h1",),
                ),
            ),
        ]
    )
    store = InMemoryCaseStore()
    store.create(CaseState(case_id="case-1", question="質問"))

    result = AgentLoop(
        store=store,
        model=model,
        tools=ToolRegistry((FakeReadTool(),)),
        profile=_profile(),
        clock=clock,
    ).run("case-1")

    assert result.state.run_status == "completed"
    assert result.state.final_answer is not None
    assert result.state.final_answer.unresolved_work_item_ids == ("w1",)
    assert model.solver_contexts[-1].finalize_only is True
    assert model.solver_contexts[-1].cycle_step_timeout is True
    assert result.trace.failure_code is None


def test_cycle_step_timeout_enters_finalization_above_time_reserve() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_cycle_step_timeout_requires_finalization_v348.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    class ManualClock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    clock = ManualClock()
    limits = AgentLimits(
        max_wall_time_sec=240,
        finalization_reserve_sec=fixture["observedBoundary"][
            "finalization_reserve_sec"
        ],
    )

    class TimeoutOnceModel(FakeModel):
        def solve(
            self,
            context: SolverContext,
            profile: ModelCallProfile,
        ) -> SolverCallResult:
            self.solver_contexts.append(context)
            self.solver_profiles.append(profile)
            if len(self.solver_contexts) == 2:
                clock.now = (
                    limits.max_wall_time_sec
                    - fixture["observedBoundary"]["remaining_wall_time_sec"]
                )
                raise TimeoutError("cycle close timed out")
            item = self.decisions.pop(0)
            decision = item(context, profile) if callable(item) else item
            return SolverCallResult(
                decision=decision,
                input_tokens=10,
                output_tokens=20,
            )

    model = TimeoutOnceModel(
        [
            _first_research((_request("r1"),)),
            SolverDecision(
                next="finalize",
                answer=FinalAnswer(
                    text="確認できた範囲の回答",
                    limitations=("残る確認事項",),
                    unresolved_work_item_ids=("w1",),
                    unresolved_hypothesis_ids=("h1",),
                ),
            ),
        ]
    )
    profile = _profile().model_copy(update={"limits": limits})
    store = InMemoryCaseStore()
    store.create(CaseState(case_id=fixture["source"]["caseId"], question="質問"))

    result = AgentLoop(
        store=store,
        model=model,
        tools=ToolRegistry((FakeReadTool(),)),
        profile=profile,
        clock=clock,
    ).run(fixture["source"]["caseId"])

    assert result.state.run_status == "completed"
    assert model.solver_contexts[-1].cycle_step_timeout is True
    assert model.solver_contexts[-1].finalize_only is fixture["expected"][
        "finalize_only"
    ]


def test_completion_window_does_not_start_an_underbudget_integration() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_finalization_window_timeout_v355.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    class ManualClock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    clock = ManualClock()
    boundary = fixture["observedBoundary"]
    limits = AgentLimits(
        max_wall_time_sec=240,
        finalization_reserve_sec=boundary["finalization_reserve_sec"],
        cycle_close_reserve_sec=boundary["cycle_close_reserve_sec"],
        min_next_cycle_budget_sec=boundary["min_next_cycle_budget_sec"],
    )

    class BoundaryModel(FakeModel):
        def solve(
            self,
            context: SolverContext,
            profile: ModelCallProfile,
        ) -> SolverCallResult:
            self.solver_contexts.append(context)
            self.solver_profiles.append(profile)
            item = self.decisions.pop(0)
            decision = item(context, profile) if callable(item) else item
            if len(self.solver_contexts) == 1:
                clock.now = (
                    limits.max_wall_time_sec
                    - boundary["remaining_wall_time_sec"]
                )
            return SolverCallResult(
                decision=decision,
                input_tokens=10,
                output_tokens=20,
            )

    model = BoundaryModel(
        [
            _first_research((_request("r1"),)),
            SolverDecision(
                next="finalize",
                answer=FinalAnswer(
                    text="確認できた範囲の回答",
                    limitations=("残る確認事項",),
                    unresolved_work_item_ids=("w1",),
                    unresolved_hypothesis_ids=("h1",),
                ),
            ),
        ]
    )
    profile = _profile().model_copy(update={"limits": limits})
    store = InMemoryCaseStore()
    store.create(CaseState(case_id=fixture["source"]["caseId"], question="質問"))

    result = AgentLoop(
        store=store,
        model=model,
        tools=ToolRegistry((FakeReadTool(),)),
        profile=profile,
        clock=clock,
    ).run(fixture["source"]["caseId"])

    assert result.state.run_status == "completed"
    assert len(model.solver_contexts) == 2
    assert model.solver_contexts[-1].finalize_only is True


def test_completed_observation_checkpoint_survives_later_model_timeout() -> None:
    class CheckpointModel(FakeModel):
        def solve(
            self,
            context: SolverContext,
            profile: ModelCallProfile,
        ) -> SolverCallResult:
            self.solver_contexts.append(context)
            self.solver_profiles.append(profile)
            if len(self.solver_contexts) == 2:
                raise SolverCheckpointTimeout(
                    "cycle close transition timed out",
                    partial_decision=SolverDecision(
                        next="continue",
                        decision_reason="取得本文の評価結果を保存する",
                        update=CaseUpdate(
                            update_hypotheses=(
                                HypothesisUpdate(
                                    hypothesis_id="h1",
                                    judgment="supported",
                                    evidence_ids=("e_r1",),
                                ),
                            ),
                        ),
                    ),
                    completed_stage="observation_integration",
                    input_tokens=11,
                    output_tokens=7,
                )
            item = self.decisions.pop(0)
            decision = item(context, profile) if callable(item) else item
            return SolverCallResult(
                decision=decision,
                input_tokens=10,
                output_tokens=20,
            )

    model = CheckpointModel(
        [
            _first_research((_request("r1"),)),
            SolverDecision(
                next="finalize",
                update=CaseUpdate(
                    update_work_items=(
                        WorkItemUpdate(
                            work_item_id="w1",
                            state="resolved",
                            resolution="取得本文で仮説を確認した",
                            basis_hypothesis_ids=("h1",),
                        ),
                    ),
                ),
                answer=FinalAnswer(
                    text="取得本文に基づく回答",
                    citation_ids=("e_r1",),
                ),
            ),
        ]
    )

    state, trace = _run(model, tools=(FakeReadTool(),))

    hypothesis = next(item for item in state.hypotheses if item.hypothesis_id == "h1")
    assert hypothesis.judgment == "supported"
    assert hypothesis.evidence_ids == ("e_r1",)
    assert state.final_answer is not None
    assert state.final_answer.citation_ids == ("e_r1",)
    assert model.solver_contexts[-1].cycle_step_timeout is True
    assert any("checkpoint_timeout" in item.purpose for item in trace.model_calls)


def test_parallel_safe_read_requests_run_in_parallel() -> None:
    tool = FakeReadTool(barrier=Barrier(2))
    model = FakeModel(
        [
            _first_research((_request("r1"), _request("r2"))),
            SolverDecision(
                next="finalize",
                update=CaseUpdate(
                    update_work_items=(
                        WorkItemUpdate(
                            work_item_id="w1",
                            state="resolved",
                            resolution="並列結果を確認した",
                            basis_hypothesis_ids=("h1",),
                        ),
                    ),
                    update_hypotheses=(
                        HypothesisUpdate(
                            hypothesis_id="h1",
                            judgment="supported",
                            evidence_ids=("e_r1", "e_r2"),
                        ),
                    ),
                ),
                answer=FinalAnswer(
                    text="並列結果を確認",
                    citation_ids=("e_r1", "e_r2"),
                ),
            ),
        ]
    )

    state, _ = _run(model, tools=(tool,))

    assert state.run_status == "completed"
    assert set(tool.calls) == {"r1", "r2"}


def test_trigger_request_runs_hidden_automatic_tool_with_copied_arguments() -> None:
    trigger = FakeReadTool(name="fetch")
    companion = FakeReadTool(name="graph")
    request = ToolRequest(
        request_id="fetch-1",
        work_item_id="w1",
        tool_name="fetch",
        arguments={"keys": ["item-a", "item-b"]},
        purpose="本文を取得する",
        hypothesis_ids=("h1",),
    )
    profile = _profile(
        automatic_tools=(
            AutomaticToolProfile(
                trigger_tool_name="fetch",
                tool_name="graph",
                copied_argument_names=("keys",),
                fixed_arguments={"max_relations": 50},
                deduplicate_list_argument="keys",
                purpose="1ホップを取得する",
            ),
        )
    )
    model = FakeModel(
        [
            _first_research((request,)),
            SolverDecision(
                next="finalize",
                update=CaseUpdate(
                    update_work_items=(
                        WorkItemUpdate(
                            work_item_id="w1",
                            state="resolved",
                            resolution="取得結果を確認した",
                        ),
                    )
                ),
                answer=FinalAnswer(text="確認済み"),
            ),
        ]
    )

    state, trace = _run(model, tools=(trigger, companion), profile=profile)

    automatic = next(item for item in state.tool_requests if item.tool_name == "graph")
    assert trigger.calls == ["fetch-1"]
    assert companion.calls == [automatic.request_id]
    assert automatic.arguments == {
        "keys": ["item-a", "item-b"],
        "max_relations": 50,
    }
    assert automatic.work_item_id == request.work_item_id
    assert automatic.hypothesis_ids == request.hypothesis_ids
    assert [item.tool_name for item in trace.tool_calls] == ["fetch", "graph"]
    assert tuple(item.request_id for item in model.solver_contexts[1].recent_tool_results) == (
        "fetch-1",
        automatic.request_id,
    )


def test_automatic_tool_does_not_repeat_already_processed_list_values() -> None:
    trigger = FakeReadTool(name="fetch")
    companion = FakeReadTool(name="graph")
    automatic_tools = (
        AutomaticToolProfile(
            trigger_tool_name="fetch",
            tool_name="graph",
            copied_argument_names=("keys",),
            deduplicate_list_argument="keys",
            purpose="1ホップを取得する",
        ),
    )

    def fetch_again(_: SolverContext, __: ModelCallProfile) -> SolverDecision:
        return SolverDecision(
            next="continue",
            next_focus_work_item_ids=("w1",),
            tool_requests=(
                ToolRequest(
                    request_id="fetch-2",
                    work_item_id="w1",
                    tool_name="fetch",
                    arguments={"keys": ["item-a", "item-c"]},
                    purpose="追加本文を取得する",
                    hypothesis_ids=("h1",),
                ),
            ),
        )

    model = FakeModel(
        [
            _first_research(
                (
                    ToolRequest(
                        request_id="fetch-1",
                        work_item_id="w1",
                        tool_name="fetch",
                        arguments={"keys": ["item-a", "item-b"]},
                        purpose="本文を取得する",
                        hypothesis_ids=("h1",),
                    ),
                )
            ),
            fetch_again,
            SolverDecision(
                next="finalize",
                update=CaseUpdate(
                    update_work_items=(
                        WorkItemUpdate(
                            work_item_id="w1",
                            state="resolved",
                            resolution="取得結果を確認した",
                        ),
                    )
                ),
                answer=FinalAnswer(text="確認済み"),
            ),
        ]
    )

    state, _ = _run(
        model,
        tools=(trigger, companion),
        profile=_profile(automatic_tools=automatic_tools),
    )

    graph_requests = [
        item for item in state.tool_requests if item.tool_name == "graph"
    ]
    assert [item.arguments["keys"] for item in graph_requests] == [
        ["item-a", "item-b"],
        ["item-c"],
    ]
    assert len(companion.calls) == 2


def test_one_hop_automatic_tool_does_not_expand_discovered_candidate() -> None:
    trigger = FakeReadTool(name="fetch")

    class CandidateGraphTool(FakeReadTool):
        def execute(
            self,
            request: ToolRequest,
            *,
            cycle_no: int,
            timeout_sec: float,
        ) -> ToolExecution:
            del timeout_sec
            self.calls.append(request.request_id)
            evidence = Evidence(
                evidence_id=f"e_{request.request_id}",
                source_ref=f"fake://{request.request_id}",
                content="1ホップ候補",
                created_cycle=cycle_no,
                metadata={"neighborArticleId": "item-neighbor"},
            )
            return ToolExecution(
                result=ToolResult(
                    request_id=request.request_id,
                    status="succeeded",
                    evidence_ids=(evidence.evidence_id,),
                    elapsed_ms=1,
                    cycle_no=cycle_no,
                ),
                evidence=(evidence,),
            )

    companion = CandidateGraphTool(name="graph")
    automatic_tools = (
        AutomaticToolProfile(
            trigger_tool_name="fetch",
            tool_name="graph",
            copied_argument_names=("keys",),
            deduplicate_list_argument="keys",
            one_hop_candidate_metadata_key="neighborArticleId",
            purpose="1ホップを取得する",
        ),
    )
    model = FakeModel(
        [
            _first_research(
                (
                    ToolRequest(
                        request_id="fetch-root",
                        work_item_id="w1",
                        tool_name="fetch",
                        arguments={"keys": ["item-root"]},
                        purpose="起点本文を取得する",
                        hypothesis_ids=("h1",),
                    ),
                )
            ),
            SolverDecision(
                next="continue",
                next_focus_work_item_ids=("w1",),
                tool_requests=(
                    ToolRequest(
                        request_id="fetch-neighbor",
                        work_item_id="w1",
                        tool_name="fetch",
                        arguments={"keys": ["item-neighbor"]},
                        purpose="1ホップ候補本文を取得する",
                        hypothesis_ids=("h1",),
                    ),
                ),
            ),
            SolverDecision(
                next="finalize",
                update=CaseUpdate(
                    update_work_items=(
                        WorkItemUpdate(
                            work_item_id="w1",
                            state="resolved",
                            resolution="1ホップ候補本文まで確認した",
                        ),
                    )
                ),
                answer=FinalAnswer(text="確認済み"),
            ),
        ]
    )

    state, trace = _run(
        model,
        tools=(trigger, companion),
        profile=_profile(automatic_tools=automatic_tools),
    )

    graph_requests = [
        item for item in state.tool_requests if item.tool_name == "graph"
    ]
    assert state.run_status == "completed"
    assert len(graph_requests) == 1
    assert graph_requests[0].arguments["keys"] == ["item-root"]
    assert len(companion.calls) == 1
    assert [item.tool_name for item in trace.tool_calls] == [
        "fetch",
        "graph",
        "fetch",
    ]


def test_search_root_is_not_lost_when_same_article_is_a_graph_candidate() -> None:
    automatic = AutomaticToolProfile(
        trigger_tool_name="fetch",
        tool_name="graph",
        copied_argument_names=("keys",),
        deduplicate_list_argument="keys",
        one_hop_candidate_metadata_key="neighborArticleId",
        independent_root_metadata_key="articleId",
        independent_root_evidence_role="search_navigation",
        purpose="1ホップを取得する",
    )
    loop = AgentLoop(
        store=InMemoryCaseStore(),
        model=FakeModel([]),
        tools=ToolRegistry((FakeReadTool(name="fetch"), FakeReadTool(name="graph"))),
        profile=_profile(automatic_tools=(automatic,)),
    )
    state = CaseState(
        case_id="case-1",
        question="質問",
        evidence=(
            Evidence(
                evidence_id="graph-candidate",
                source_ref="neo4j:test",
                content="Graph候補",
                created_cycle=1,
                metadata={"neighborArticleId": "item-neighbor"},
            ),
            Evidence(
                evidence_id="search-candidate",
                source_ref="opensearch:test",
                content="検索候補",
                created_cycle=1,
                metadata={
                    "articleId": "item-neighbor",
                    "evidenceRole": "search_navigation",
                },
            ),
        ),
    )
    request = ToolRequest(
        request_id="fetch-neighbor",
        work_item_id="w1",
        tool_name="fetch",
        arguments={"keys": ["item-neighbor"]},
        purpose="独立した検索起点本文を取得する",
    )

    requests = loop._with_automatic_tools(state, (request,))

    assert [item.tool_name for item in requests] == ["fetch", "graph"]
    assert requests[1].arguments == {"keys": ["item-neighbor"]}


def test_new_graph_candidates_use_dedicated_solver_profile() -> None:
    class DiscoveryTool(FakeReadTool):
        def execute(
            self,
            request: ToolRequest,
            *,
            cycle_no: int,
            timeout_sec: float,
        ) -> ToolExecution:
            execution = super().execute(
                request,
                cycle_no=cycle_no,
                timeout_sec=timeout_sec,
            )
            evidence = execution.evidence[0].model_copy(
                update={
                    "metadata": {
                        "articleId": "law-act-article-1",
                        "citationEligible": False,
                    }
                }
            )
            return ToolExecution(result=execution.result, evidence=(evidence,))

    search = DiscoveryTool(name="search")
    fetch = FakeReadTool(name="fetch_articles")

    class GraphNavigationTool(FakeReadTool):
        def execute(
            self,
            request: ToolRequest,
            *,
            cycle_no: int,
            timeout_sec: float,
        ) -> ToolExecution:
            del timeout_sec
            self.calls.append(request.request_id)
            evidence = Evidence(
                evidence_id=f"graph-{request.request_id}",
                source_ref="neo4j:article_pair:test",
                content=json.dumps(
                    {
                        "seedArticleId": "law-act-article-1",
                        "seedDocumentId": "law-act",
                        "seedTitle": "法律",
                        "seedHeading": "第一条",
                        "neighborArticleId": "law-order-article-2",
                        "neighborDocumentId": "law-order",
                        "neighborTitle": "政令",
                        "neighborHeading": "第二条",
                        "relations": [
                            {
                                "kind": "formal_relation",
                                "edgeType": "REFERENCES",
                                "direction": "incoming",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                created_cycle=cycle_no,
                metadata={
                    "docType": "graph_navigation",
                    "citationEligible": False,
                    "neighborArticleId": "law-order-article-2",
                },
            )
            return ToolExecution(
                result=ToolResult(
                    request_id=request.request_id,
                    status="succeeded",
                    evidence_ids=(evidence.evidence_id,),
                    elapsed_ms=1,
                    cycle_no=cycle_no,
                ),
                evidence=(evidence,),
            )

    graph = GraphNavigationTool(name="legal_graph_neighbors")

    def select_graph_candidate(
        context: SolverContext,
        _: ModelCallProfile,
    ) -> SolverDecision:
        frontiers = context.graph_review_batch.candidates
        return SolverDecision(
            next="continue",
            next_focus_work_item_ids=("w1",),
            graph_candidate_review=GraphCandidateReview(
                graph_request_ids=context.required_graph_review_request_ids,
                reviewed_link_ids=tuple(
                    dict.fromkeys(
                        link.link_id
                        for frontier in frontiers
                        for link in frontier.links
                    )
                ),
                frontier_decisions=tuple(
                    GraphFrontierDecision(
                        frontier_item_id=frontier.frontier_item_id,
                        article_id=frontier.article_id,
                        work_item_id=frontier.work_item_id,
                        hypothesis_id=frontier.hypothesis_id,
                        action="select",
                        reason="作業に対応する具体化規定を確認する",
                    )
                    for frontier in frontiers
                ),
                reason="具体化規定の候補本文を確認する",
            ),
        )

    profile = _profile(
        automatic_tools=(
            AutomaticToolProfile(
                trigger_tool_name="fetch_articles",
                tool_name="legal_graph_neighbors",
                copied_argument_names=("article_ids",),
                deduplicate_list_argument="article_ids",
                one_hop_candidate_metadata_key="neighborArticleId",
                purpose="1ホップ候補を取得する",
            ),
        )
    ).model_copy(
        update={
            "solver_graph_review": ModelCallProfile(
                model="graph-model",
                system_prompt="graph-selection",
            ),
            "graph_review_fetch_tool_name": "fetch_articles",
            "tool_list_argument_limits": (
                ToolListArgumentLimit(
                    tool_name="fetch_articles",
                    argument_name="article_ids",
                    max_items=4,
                ),
            ),
        }
    )
    model = FakeModel(
        [
            _first_research((_request("discover-root"),)),
            SolverDecision(
                next="continue",
                next_focus_work_item_ids=("w1",),
                tool_requests=(
                    ToolRequest(
                        request_id="fetch-root",
                        work_item_id="w1",
                        tool_name="fetch_articles",
                        arguments={"article_ids": ["law-act-article-1"]},
                        purpose="起点本文を取得する",
                        hypothesis_ids=("h1",),
                    ),
                ),
            ),
            select_graph_candidate,
            SolverDecision(
                next="finalize",
                update=CaseUpdate(
                    update_work_items=(
                        WorkItemUpdate(
                            work_item_id="w1",
                            state="resolved",
                            resolution="候補本文まで確認した",
                        ),
                    )
                ),
                answer=FinalAnswer(text="確認済み"),
            ),
        ]
    )

    state, trace = _run(model, tools=(search, fetch, graph), profile=profile)

    assert state.run_status == "completed", (
        state.stop_reason,
        trace.failure_code,
        [item.purpose for item in trace.model_calls],
        [context.required_graph_review_request_ids for context in model.solver_contexts],
    )
    assert state.graph_candidate_reviews[0].selected_article_ids == (
        "law-order-article-2",
    )
    assert [item.purpose for item in trace.model_calls] == [
        "research",
        "integration",
        "graph_selection",
        "integration",
    ]
    assert model.solver_profiles[2].system_prompt == "graph-selection"
    assert len(graph.calls) == 1
    assert state.research_cycle_count == 1


def test_hidden_automatic_tool_cannot_be_requested_by_solver() -> None:
    automatic_tools = (
        AutomaticToolProfile(
            trigger_tool_name="fetch",
            tool_name="graph",
            copied_argument_names=("keys",),
            purpose="1ホップを取得する",
        ),
    )
    model = FakeModel(
        [
            _first_research(
                (
                    ToolRequest(
                        request_id="graph-1",
                        work_item_id="w1",
                        tool_name="graph",
                        purpose="直接要求してしまった",
                    ),
                )
            ),
            _first_research(
                (
                    ToolRequest(
                        request_id="graph-2",
                        work_item_id="w1",
                        tool_name="graph",
                        purpose="再び直接要求してしまった",
                    ),
                )
            ),
            _first_research(
                (
                    ToolRequest(
                        request_id="graph-3",
                        work_item_id="w1",
                        tool_name="graph",
                        purpose="三度直接要求してしまった",
                    ),
                )
            ),
        ]
    )

    state, _ = _run(
        model,
        tools=(FakeReadTool(name="fetch"), FakeReadTool(name="graph")),
        profile=_profile(automatic_tools=automatic_tools),
    )

    assert state.run_status == "failed"
    assert state.stop_reason == "protocol_error"


def test_tool_list_argument_limit_rejects_without_selecting_items() -> None:
    article_ids = [f"law-a-article-{index}" for index in range(1, 6)]
    state = CaseState(
        case_id="case-1",
        question="質問",
        work_items=(WorkItem(work_item_id="w1", question="条文を確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="根拠条文がある",
            ),
        ),
    )
    decision = SolverDecision(
        next="continue",
        next_focus_work_item_ids=("w1",),
        tool_requests=(
            ToolRequest(
                request_id="fetch-too-many",
                work_item_id="w1",
                tool_name="fetch_articles",
                arguments={"article_ids": article_ids},
                purpose="候補本文を取得する",
                hypothesis_ids=("h1",),
            ),
        ),
    )

    with pytest.raises(ContractViolation, match="profile limit of 4 items"):
        apply_solver_decision(
            state,
            decision,
            limits=AgentLimits(),
            known_tool_names={"fetch_articles"},
            material_evidence_ids=(),
            fetchable_article_ids=article_ids,
            tool_list_argument_limits={
                ("fetch_articles", "article_ids"): 4,
            },
            finalize_only=False,
        )

    assert state.tool_requests == ()


def test_article_fetch_requests_must_be_consolidated_by_solver() -> None:
    state = CaseState(
        case_id="case-1",
        question="質問",
        work_items=(WorkItem(work_item_id="w1", question="条文を確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="根拠条文がある",
            ),
        ),
    )
    requests = tuple(
        ToolRequest(
            request_id=f"fetch-{index}",
            work_item_id="w1",
            tool_name="fetch_articles",
            arguments={"article_ids": [article_id]},
            purpose="本文を取得する",
            hypothesis_ids=("h1",),
        )
        for index, article_id in enumerate(("law-a-article-1", "law-a-article-2"))
    )

    with pytest.raises(ContractViolation, match="must be consolidated"):
        apply_solver_decision(
            state,
            SolverDecision(
                next="continue",
                next_focus_work_item_ids=("w1",),
                tool_requests=requests,
            ),
            limits=AgentLimits(),
            known_tool_names={"fetch_articles"},
            material_evidence_ids=(),
            fetchable_article_ids=("law-a-article-1", "law-a-article-2"),
            graph_review_fetch_tool_name="fetch_articles",
            finalize_only=False,
        )


def test_write_tool_is_rejected_without_execution() -> None:
    tool = FakeReadTool(read_only=False)
    model = FakeModel(
        [
            _first_research((_request("r1"),)),
            _first_research((_request("r1"),)),
            _first_research((_request("r1"),)),
        ]
    )

    state, _ = _run(model, tools=(tool,))

    assert state.run_status == "failed"
    assert state.stop_reason == "protocol_error"
    assert tool.calls == []


def test_solver_can_repair_structural_contract() -> None:
    invalid_request = ToolRequest(
        request_id="invalid-r1",
        work_item_id="missing-work",
        tool_name="search",
        purpose="存在しない作業を参照してしまった",
    )
    model = FakeModel(
        [
            SolverDecision(
                next="continue",
                tool_requests=(invalid_request,),
            ),
            _first_research((_request("r1"),)),
            SolverDecision(
                next="finalize",
                update=CaseUpdate(
                    update_work_items=(
                        WorkItemUpdate(
                            work_item_id="w1",
                            state="resolved",
                            resolution="本文を確認した",
                            basis_hypothesis_ids=("h1",),
                        ),
                    ),
                    update_hypotheses=(
                        HypothesisUpdate(
                            hypothesis_id="h1",
                            judgment="supported",
                            evidence_ids=("e_r1",),
                        ),
                    ),
                ),
                answer=FinalAnswer(text="回答", citation_ids=("e_r1",)),
            ),
        ]
    )

    state, trace = _run(model, tools=(FakeReadTool(),))

    assert state.run_status == "completed"
    assert model.solver_contexts[0].contract_feedback is None
    feedback = model.solver_contexts[1].contract_feedback
    assert feedback is not None
    assert "open WorkItem" in feedback.violation
    assert feedback.previous_decision.tool_requests == (invalid_request,)
    assert [item.purpose for item in trace.model_calls] == [
        "research",
        "research_contract_repair",
        "integration",
    ]


def test_repeated_successful_action_returns_normal_feedback() -> None:
    first_request = _request("r1").model_copy(
        update={"tool_name": "legal_search"}
    )
    duplicate = _request("r2").model_copy(
        update={"tool_name": "legal_search"}
    )
    resolved = SolverDecision(
        next="finalize",
        update=CaseUpdate(
            update_work_items=(
                WorkItemUpdate(
                    work_item_id="w1",
                    state="resolved",
                    resolution="取得本文で確認した",
                    basis_hypothesis_ids=("h1",),
                ),
            ),
            update_hypotheses=(
                HypothesisUpdate(
                    hypothesis_id="h1",
                    judgment="supported",
                    evidence_ids=("e_r1",),
                ),
            ),
        ),
        answer=FinalAnswer(text="回答", citation_ids=("e_r1",)),
    )
    model = FakeModel(
        [
            _first_research((first_request,)),
            SolverDecision(next="continue", tool_requests=(duplicate,)),
            resolved,
        ]
    )

    state, trace = _run(model, tools=(FakeReadTool(name="legal_search"),))

    assert state.run_status == "completed"
    feedback_context = model.solver_contexts[2]
    assert feedback_context.contract_feedback is None
    assert feedback_context.action_feedback is not None
    assert feedback_context.action_feedback.code == "already_completed"
    assert feedback_context.action_feedback.rejected_tool_requests == (duplicate,)
    assert [item.purpose for item in trace.model_calls] == [
        "research",
        "integration",
        "integration_action_feedback",
    ]


def test_solver_can_use_second_contract_repair_without_program_rewriting() -> None:
    invalid_request = ToolRequest(
        request_id="invalid-r1",
        work_item_id="missing-work",
        tool_name="search",
        purpose="存在しない作業を参照してしまった",
    )
    model = FakeModel(
        [
            SolverDecision(next="continue", tool_requests=(invalid_request,)),
            SolverDecision(next="continue", tool_requests=(invalid_request,)),
            _first_research((_request("r1"),)),
            SolverDecision(
                next="finalize",
                update=CaseUpdate(
                    update_work_items=(
                        WorkItemUpdate(
                            work_item_id="w1",
                            state="resolved",
                            resolution="本文を確認した",
                            basis_hypothesis_ids=("h1",),
                        ),
                    ),
                    update_hypotheses=(
                        HypothesisUpdate(
                            hypothesis_id="h1",
                            judgment="supported",
                            evidence_ids=("e_r1",),
                        ),
                    ),
                ),
                answer=FinalAnswer(text="回答", citation_ids=("e_r1",)),
            ),
        ]
    )

    state, trace = _run(model, tools=(FakeReadTool(),))

    assert state.run_status == "completed"
    assert len(model.solver_contexts) == 4
    assert model.solver_contexts[1].contract_feedback is not None
    assert model.solver_contexts[2].contract_feedback is not None
    assert [item.purpose for item in trace.model_calls] == [
        "research",
        "research_contract_repair",
        "research_contract_repair",
        "integration",
    ]


def test_dependency_repair_receives_only_work_items_bound_to_grounding_request() -> None:
    work_item_ids = tuple(f"w{index}" for index in range(1, 5))
    initial = SolverDecision(
        next="continue",
        update=CaseUpdate(
            add_work_items=tuple(
                WorkItem(work_item_id=work_item_id, question=f"確認{index}")
                for index, work_item_id in enumerate(work_item_ids, start=1)
            ),
            add_hypotheses=(
                Hypothesis(
                    hypothesis_id="h1",
                    work_item_id="w1",
                    statement="取得本文が回答を支える",
                ),
            ),
        ),
        next_focus_work_item_ids=work_item_ids,
        tool_requests=(
            ToolRequest(
                request_id="r1",
                work_item_id="w1",
                tool_name="search",
                purpose="根拠候補を検索する",
            ),
        ),
    )
    close_updates = CaseUpdate(
        update_work_items=tuple(
            WorkItemUpdate(
                work_item_id=work_item_id,
                state="resolved",
                resolution="確認済み",
                basis_hypothesis_ids=("h1",) if work_item_id == "w1" else (),
            )
            for work_item_id in work_item_ids
        ),
        update_hypotheses=(
            HypothesisUpdate(
                hypothesis_id="h1",
                judgment="supported",
                evidence_ids=("e_r1",),
            ),
        ),
    )
    invalid = SolverDecision(
        next="finalize",
        update=close_updates,
        answer=FinalAnswer(text="回答", citation_ids=("e_r1",)),
    )
    repaired = SolverDecision(
        next="finalize",
        update=close_updates,
        dependency_decisions=(
            DependencyDecision(
                dependency_kind="lower_law",
                work_item_id="w1",
                status="not_required",
                reason="この作業には下位規範確認が不要",
                basis_evidence_ids=("e_r1",),
            ),
        ),
        answer=FinalAnswer(text="回答", citation_ids=("e_r1",)),
    )
    profile = _profile().model_copy(
        update={"required_dependency_kind": "lower_law"}
    )
    model = FakeModel([initial, invalid, repaired])

    state, trace = _run(model, tools=(FakeReadTool(),), profile=profile)

    assert state.run_status == "completed"
    integration_context = model.solver_contexts[1]
    assert integration_context.required_dependency_kind == "lower_law"
    assert integration_context.required_dependency_work_item_ids == ("w1",)
    repair_context = model.solver_contexts[2]
    assert repair_context.contract_feedback is not None
    assert "missing=['w1']" in (
        repair_context.contract_feedback.violation
    )
    assert [item.purpose for item in trace.model_calls] == [
        "research",
        "integration",
        "integration_contract_repair",
    ]


def test_reviewer_can_request_one_solver_revision_and_then_accept() -> None:
    finding = ReviewFinding(
        finding_id="finding-1",
        kind="coverage_gap",
        description="説明が不足",
        work_item_id="w1",
    )
    model = FakeModel(
        [
            SolverDecision(
                next="finalize",
                update=CaseUpdate(
                    add_work_items=(
                        WorkItem(
                            work_item_id="w1",
                            question="説明を確認する",
                            state="resolved",
                            resolution="初稿で説明した",
                        ),
                    ),
                ),
                answer=FinalAnswer(text="初稿"),
            ),
            SolverDecision(
                next="finalize",
                review_finding_resolutions=(
                    ReviewFindingResolution(
                        finding_id="finding-1",
                        outcome="addressed",
                        reason="回答へ説明を追加した",
                    ),
                ),
                answer=FinalAnswer(text="修正版"),
            ),
        ],
        reviews=[
            ReviewResult(
                verdict="revise",
                findings=(finding,),
            ),
            ReviewResult(verdict="accept"),
        ],
    )

    state, trace = _run(model, profile=_profile(reviewer_enabled=True))

    assert state.run_status == "completed"
    assert state.final_answer == FinalAnswer(text="修正版")
    assert len(model.review_contexts) == 2
    assert model.review_contexts[0].work_items[0].work_item_id == "w1"
    assert model.solver_contexts[1].reviewer_findings[0].description == "説明が不足"
    assert state.review_finding_resolutions[0].finding_id == "finding-1"
    assert [item.model for item in trace.model_calls] == [
        "research-model",
        "review-model",
        "integration-model",
        "review-model",
    ]


def test_reviewer_view_projects_case_structure_and_all_grounding_evidence() -> None:
    store = InMemoryCaseStore()
    state = CaseState(
        case_id="case-review-view",
        question="質問",
        work_items=(
            WorkItem(
                work_item_id="w1",
                question="要件を確認する",
                state="resolved",
                resolution="要件を確認した",
                basis_hypothesis_ids=("h1",),
            ),
        ),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="要件がある",
                judgment="supported",
                evidence_ids=("e1",),
            ),
        ),
        evidence=(
            Evidence(
                evidence_id="e1",
                source_ref="fake://1",
                content="回答に引用した本文",
                created_cycle=1,
            ),
            Evidence(
                evidence_id="e2",
                source_ref="fake://2",
                content="取得済みだが未引用の本文",
                created_cycle=1,
            ),
            Evidence(
                evidence_id="nav",
                source_ref="fake://nav",
                content="検索候補",
                created_cycle=1,
                metadata={"citationEligible": False},
            ),
        ),
        final_answer=FinalAnswer(text="回答", citation_ids=("e1",)),
    )
    store.create(state)
    loop = AgentLoop(
        store=store,
        model=FakeModel([]),
        tools=ToolRegistry(()),
        profile=_profile(reviewer_enabled=True),
    )

    view = loop._build_reviewer_view(state)

    assert view.work_items == state.work_items
    assert view.hypotheses == state.hypotheses
    assert [item.evidence_id for item in view.evidence] == ["e1", "e2"]


def test_second_reviewer_rejection_is_explicit_failure() -> None:
    finding = ReviewFinding(
        finding_id="finding-1",
        kind="internal_contradiction",
        description="なお不整合",
    )
    model = FakeModel(
        [
            SolverDecision(next="finalize", answer=FinalAnswer(text="初稿")),
            SolverDecision(
                next="finalize",
                review_finding_resolutions=(
                    ReviewFindingResolution(
                        finding_id="finding-1",
                        outcome="addressed",
                        reason="回答内の不整合を修正した",
                    ),
                ),
                answer=FinalAnswer(text="修正版"),
            ),
        ],
        reviews=[
            ReviewResult(verdict="revise", findings=(finding,)),
            ReviewResult(verdict="revise", findings=(finding,)),
        ],
    )

    state, _ = _run(model, profile=_profile(reviewer_enabled=True))

    assert state.run_status == "failed"
    assert state.stop_reason == "review_failed"


def test_reviewer_cannot_reference_unknown_case_ids() -> None:
    model = FakeModel(
        [SolverDecision(next="finalize", answer=FinalAnswer(text="初稿"))],
        reviews=[
            ReviewResult(
                verdict="revise",
                findings=(
                    ReviewFinding(
                        finding_id="finding-1",
                        kind="coverage_gap",
                        description="未知の作業を参照した",
                        work_item_id="unknown-work",
                    ),
                ),
            ),
        ],
    )

    state, trace = _run(model, profile=_profile(reviewer_enabled=True))

    assert state.run_status == "failed"
    assert state.stop_reason == "protocol_error"
    assert trace.failure_code == (
        "contract_violation:Reviewer finding references unknown WorkItem: "
        "unknown-work"
    )


def test_solver_must_resolve_every_pending_reviewer_finding() -> None:
    with pytest.raises(
        ContractViolation,
        match="review finding resolutions do not match pending findings",
    ):
        apply_solver_decision(
            CaseState(case_id="case-1", question="質問"),
            SolverDecision(
                next="finalize",
                answer=FinalAnswer(text="未修正版"),
            ),
            limits=AgentLimits(),
            known_tool_names=(),
            material_evidence_ids=(),
            required_review_finding_ids=("finding-1",),
            finalize_only=False,
        )


def test_contradicted_basis_requires_solver_impact_decision() -> None:
    evidence = Evidence(
        evidence_id="e1",
        source_ref="fake://1",
        content="反証本文",
        created_cycle=1,
    )
    state = CaseState(
        case_id="case-1",
        question="質問",
        work_items=(
            WorkItem(work_item_id="root", question="親作業"),
            WorkItem(
                work_item_id="child",
                parent_work_item_id="root",
                question="仮説を前提にした子作業",
                basis_hypothesis_ids=("h1",),
            ),
        ),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="root",
                statement="前提",
                judgment="supported",
                evidence_ids=("e1",),
            ),
        ),
        evidence=(evidence,),
    )
    incomplete = SolverDecision(
        next="continue",
        update=CaseUpdate(
            update_hypotheses=(
                HypothesisUpdate(
                    hypothesis_id="h1",
                    judgment="contradicted",
                    evidence_ids=("e1",),
                ),
            )
        ),
        next_focus_work_item_ids=("child",),
        tool_requests=(
            ToolRequest(
                request_id="r-impact",
                work_item_id="child",
                tool_name="search",
                purpose="反証後の影響を確認する",
            ),
        ),
    )

    with pytest.raises(ContractViolation, match="impact decisions"):
        apply_solver_decision(
            state,
            incomplete,
            limits=AgentLimits(),
            known_tool_names=("search",),
            material_evidence_ids=("e1",),
            finalize_only=False,
        )

    explicit = incomplete.model_copy(
        update={
            "next": "continue",
            "update": incomplete.update.model_copy(
                update={
                    "impact_decisions": (
                        WorkItemImpactDecision(
                            work_item_id="child",
                            action="retain",
                            reason="前提を外して確認作業自体は継続する",
                        ),
                    )
                }
            ),
            "next_focus_work_item_ids": ("child",),
            "tool_requests": (
                ToolRequest(
                    request_id="r-next",
                    work_item_id="child",
                    tool_name="search",
                    purpose="反証後の前提なしで確認を続ける",
                ),
            ),
            "answer": None,
        }
    )
    updated = apply_solver_decision(
        state,
        explicit,
        limits=AgentLimits(),
        known_tool_names=("search",),
        material_evidence_ids=("e1",),
        finalize_only=False,
    )

    child = next(item for item in updated.work_items if item.work_item_id == "child")
    assert child.state == "open"
    assert child.basis_hypothesis_ids == ()


@pytest.mark.parametrize("action", ["replace", "drop"])
def test_solver_controls_replacement_or_drop_after_contradiction(action: str) -> None:
    evidence = Evidence(
        evidence_id="e1",
        source_ref="fake://1",
        content="反証本文",
        created_cycle=1,
    )
    state = CaseState(
        case_id="case-1",
        question="質問",
        work_items=(
            WorkItem(work_item_id="root", question="親作業"),
            WorkItem(
                work_item_id="child",
                parent_work_item_id="root",
                question="旧作業",
                basis_hypothesis_ids=("h1",),
            ),
        ),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="root",
                statement="旧前提",
                judgment="supported",
                evidence_ids=("e1",),
            ),
        ),
        evidence=(evidence,),
    )
    added_items: tuple[WorkItem, ...] = ()
    replacement_id = None
    if action == "replace":
        replacement_id = "replacement"
        added_items = (
            WorkItem(
                work_item_id=replacement_id,
                parent_work_item_id="root",
                question="前提を改めた新作業",
                replaces_work_item_id="child",
            ),
        )
    next_work_item_id = replacement_id or "root"
    decision = SolverDecision(
        next="continue",
        update=CaseUpdate(
            add_work_items=added_items,
            update_hypotheses=(
                HypothesisUpdate(
                    hypothesis_id="h1",
                    judgment="contradicted",
                    evidence_ids=("e1",),
                ),
            ),
            impact_decisions=(
                WorkItemImpactDecision(
                    work_item_id="child",
                    action=action,
                    reason="前提が反証されたためSolverが判断した",
                    replacement_work_item_id=replacement_id,
                ),
            ),
        ),
        next_focus_work_item_ids=(next_work_item_id,),
        tool_requests=(
            ToolRequest(
                request_id="r-next",
                work_item_id=next_work_item_id,
                tool_name="search",
                purpose="反証を反映して調査を続ける",
            ),
        ),
    )

    updated = apply_solver_decision(
        state,
        decision,
        limits=AgentLimits(),
        known_tool_names=("search",),
        material_evidence_ids=("e1",),
        finalize_only=False,
    )

    old = next(item for item in updated.work_items if item.work_item_id == "child")
    assert old.state == "dropped"
    if action == "replace":
        replacement = next(
            item for item in updated.work_items if item.work_item_id == "replacement"
        )
        assert replacement.replaces_work_item_id == "child"


def test_delta_keeps_untouched_work_and_revalidates_updates() -> None:
    state = CaseState(
        case_id="case-1",
        question="質問",
        work_items=(
            WorkItem(work_item_id="w1", question="更新対象"),
            WorkItem(work_item_id="w2", question="別系統"),
        ),
    )
    invalid = SolverDecision(
        next="continue",
        update=CaseUpdate(
            update_work_items=(
                WorkItemUpdate(
                    work_item_id="w1",
                    state="open",
                    resolution="openなのに結論あり",
                ),
            )
        ),
        next_focus_work_item_ids=("w2",),
        tool_requests=(
            ToolRequest(
                request_id="r-invalid-update",
                work_item_id="w2",
                tool_name="search",
                purpose="別系統を確認する",
            ),
        ),
    )

    with pytest.raises(ContractViolation, match="schema"):
        apply_solver_decision(
            state,
            invalid,
            limits=AgentLimits(),
            known_tool_names=("search",),
            material_evidence_ids=(),
            finalize_only=False,
        )

    valid = SolverDecision(
        next="continue",
        update=CaseUpdate(
            update_work_items=(
                WorkItemUpdate(
                    work_item_id="w1",
                    state="resolved",
                    resolution="更新対象を完了した",
                ),
            )
        ),
        next_focus_work_item_ids=("w2",),
        tool_requests=(
            ToolRequest(
                request_id="r-next",
                work_item_id="w2",
                tool_name="search",
                purpose="別系統の調査を続ける",
            ),
        ),
    )
    updated = apply_solver_decision(
        state,
        valid,
        limits=AgentLimits(),
        known_tool_names=("search",),
        material_evidence_ids=(),
        finalize_only=False,
    )
    assert {item.work_item_id for item in updated.work_items} == {"w1", "w2"}


def test_finalize_rejects_open_work_unless_limited_answer_accounts_for_it() -> None:
    state = CaseState(
        case_id="case-1",
        question="質問",
        work_items=(WorkItem(work_item_id="w1", question="未確認事項"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="確認できるか",
            ),
        ),
    )

    with pytest.raises(ContractViolation, match="every open WorkItem"):
        apply_solver_decision(
            state,
            SolverDecision(next="finalize", answer=FinalAnswer(text="回答")),
            limits=AgentLimits(),
            known_tool_names=(),
            material_evidence_ids=(),
            finalize_only=False,
        )

    updated = apply_solver_decision(
        state,
        SolverDecision(
            next="finalize",
            update=CaseUpdate(
                update_hypotheses=(
                    HypothesisUpdate(
                        hypothesis_id="h1",
                        judgment="unresolved",
                        gaps=("根拠本文を取得できなかった",),
                    ),
                ),
            ),
            answer=FinalAnswer(
                text="確認できた範囲で回答",
                limitations=("根拠本文は未確認",),
                unresolved_work_item_ids=("w1",),
                unresolved_hypothesis_ids=("h1",),
            ),
        ),
        limits=AgentLimits(),
        known_tool_names=(),
        material_evidence_ids=(),
        finalize_only=True,
    )

    assert updated.work_items[0].state == "open"
    assert updated.hypotheses[0].judgment == "unresolved"


def test_context_enumerates_descendants_of_contradicted_basis() -> None:
    state = CaseState(
        case_id="case-1",
        question="質問",
        work_items=(
            WorkItem(work_item_id="root", question="親"),
            WorkItem(
                work_item_id="affected",
                parent_work_item_id="root",
                question="影響元",
                basis_hypothesis_ids=("h1",),
            ),
            WorkItem(
                work_item_id="child",
                parent_work_item_id="affected",
                question="子",
            ),
            WorkItem(
                work_item_id="grandchild",
                parent_work_item_id="child",
                question="孫",
            ),
        ),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="root",
                statement="反証済み前提",
                judgment="contradicted",
                evidence_ids=("e1",),
            ),
        ),
        evidence=(
            Evidence(
                evidence_id="e1",
                source_ref="fake://1",
                content="反証本文",
                created_cycle=1,
            ),
        ),
    )

    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )

    assert {item.work_item_id for item in context.affected_work_items} == {
        "affected",
        "child",
        "grandchild",
    }


def test_context_separates_grounding_evidence_from_fetchable_article_ids() -> None:
    state = CaseState(
        case_id="case-1",
        question="質問",
        research_cycle_count=1,
        work_items=(WorkItem(work_item_id="w1", question="関連条文を確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="関連する下位法令がある",
            ),
        ),
        tool_requests=(
            ToolRequest(
                request_id="r1",
                work_item_id="w1",
                tool_name="legal_graph_neighbors",
                arguments={"article_ids": ["law-a-article-1"]},
                purpose="1ホップを確認する",
                hypothesis_ids=("h1",),
            ),
        ),
        evidence=(
            Evidence(
                evidence_id="law-a-article-1-paragraph-1",
                source_ref="opensearch:paragraph",
                content="条文本文",
                created_cycle=1,
                metadata={
                    "articleId": "law-a-article-1",
                    "citationEligible": True,
                },
            ),
            Evidence(
                evidence_id="graph-nav-1",
                source_ref="neo4j:relation",
                content=json.dumps(
                    {
                        "seedArticleId": "law-a-article-1",
                        "neighborArticleId": "order-a-article-2",
                        "relations": [
                            {
                                "kind": "formal_relation",
                                "edgeType": "REFERENCES",
                                "direction": "from_subject",
                                "status": "unverified",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                created_cycle=1,
                metadata={
                    "fromArticleId": "law-a-article-1",
                    "toArticleId": "order-a-article-2",
                    "docType": "graph_navigation",
                    "citationEligible": False,
                },
            ),
        ),
        tool_results=(
            ToolResult(
                request_id="r1",
                status="succeeded",
                evidence_ids=(
                    "law-a-article-1-paragraph-1",
                    "graph-nav-1",
                ),
                cycle_no=1,
            ),
        ),
    )

    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )

    assert context.grounding_evidence_ids == ("law-a-article-1-paragraph-1",)
    assert context.navigation_evidence_ids == ()
    assert context.fetchable_article_ids == ("order-a-article-2",)
    assert tuple(item.evidence_id for item in context.evidence_manifest) == (
        "law-a-article-1-paragraph-1",
    )
    assert context.omitted_evidence_ids == ()
    assert context.recent_tool_results[0].evidence_ids == (
        "law-a-article-1-paragraph-1",
    )
    assert context.recent_tool_results[0].evidence_count == 2
    assert context.recent_tool_results[0].graph_projection_updated is True


def test_context_keeps_all_graph_candidates_outside_material_limit() -> None:
    graph_payload = {
        "seedArticleId": "law-act-article-27_2",
        "seedDocumentId": "law-act",
        "seedTitle": "金融商品取引法",
        "seedHeading": "第二十七条の二",
        "neighborArticleId": "law-ordinance-article-2_5",
        "neighborDocumentId": "law-ordinance",
        "neighborTitle": "発行者以外の者による株券等の公開買付けの開示に関する内閣府令",
        "neighborHeading": "第二条の五",
        "relations": [
            {
                "kind": "relation_assertion",
                "edgeType": "USES_DEFINITION",
                "direction": "to_subject",
                "basisEdgeId": "edge-1",
                "classificationRunId": "classification-run-1",
                "subjectArticleId": "law-ordinance-article-2_5",
                "objectArticleId": "law-act-article-27_2",
                "subjectSupportingSpanId": "ordinance::span-1",
                "objectSupportingSpanId": "act::span-1",
                "subjectSupportingQuote": "定義語を使用する。",
                "objectSupportingQuote": "定義語とは対象をいう。",
                "relationExplanation": "OBJECTが定める定義語をSUBJECTが利用する。",
                "sourceId": "edge-1",
            }
        ],
    }
    state = CaseState(
        case_id="case-1",
        question="公開買付けの要件は何ですか",
        research_cycle_count=1,
        work_items=(
            WorkItem(work_item_id="w1", question="適用要件を確認する"),
            WorkItem(work_item_id="w2", question="手続を確認する"),
        ),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="下位法令に具体的要件がある",
            ),
            Hypothesis(
                hypothesis_id="h2",
                work_item_id="w2",
                statement="下位法令に具体的手続がある",
            ),
        ),
        tool_requests=(
            ToolRequest(
                request_id="text-request",
                work_item_id="w1",
                tool_name="fetch_articles",
                arguments={"article_ids": ["law-act-article-27_2"]},
                purpose="起点本文を取得する",
                hypothesis_ids=("h1",),
            ),
            ToolRequest(
                request_id="graph-request",
                work_item_id="w1",
                tool_name="legal_graph_neighbors",
                arguments={"article_ids": ["law-act-article-27_2"]},
                purpose="1ホップを取得する",
                hypothesis_ids=("h1", "h2"),
            ),
        ),
        evidence=(
            Evidence(
                evidence_id="law-act-article-27_2",
                source_ref="opensearch:law-act-article-27_2",
                content="a" * 1000,
                created_cycle=1,
                metadata={
                    "articleId": "law-act-article-27_2",
                    "citationEligible": True,
                },
            ),
            Evidence(
                evidence_id="graph-nav-1",
                source_ref="neo4j:article_pair:1",
                title="Graph navigation candidate",
                content=json.dumps(graph_payload, ensure_ascii=False),
                created_cycle=1,
                metadata={
                    "docType": "graph_navigation",
                    "citationEligible": False,
                    "seedArticleId": "law-act-article-27_2",
                    "neighborArticleId": "law-ordinance-article-2_5",
                },
            ),
        ),
        tool_results=(
            ToolResult(
                request_id="text-request",
                status="succeeded",
                evidence_ids=("law-act-article-27_2",),
                cycle_no=1,
            ),
            ToolResult(
                request_id="graph-request",
                status="succeeded",
                evidence_ids=("graph-nav-1",),
                cycle_no=1,
            ),
        ),
    )

    context = build_solver_context(
        state,
        AgentLimits(
            max_material_evidence_chars=1000,
            max_solver_input_chars=20000,
        ),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )

    assert tuple(item.evidence_id for item in context.material_evidence) == (
        "law-act-article-27_2",
    )
    assert context.omitted_evidence_ids == ()
    assert context.navigation_evidence_ids == ()
    assert context.required_graph_review_request_ids == ("graph-request",)
    assert {
        item.work_item_id for item in context.graph_review_batch.candidates
    } == {"w1", "w2"}
    assert "law-ordinance-article-2_5" in context.fetchable_article_ids
    frontiers = context.graph_review_batch.candidates
    assert len(frontiers) == 2
    assert {item.article_id for item in frontiers} == {
        "law-ordinance-article-2_5"
    }
    assert {item.heading for item in frontiers} == {"第二条の五"}
    assert {item.content_status for item in frontiers} == {"not_requested"}
    link = frontiers[0].links[0]
    assert link.seed_article_id == "law-act-article-27_2"
    assert link.candidate_article_id == "law-ordinance-article-2_5"
    assert link.work_item_ids == ("w1", "w2")
    assert link.hypothesis_ids == ("h1", "h2")
    assert link.relations == (
        {
            "kind": "relation_assertion",
            "edgeType": "USES_DEFINITION",
            "direction": "to_subject",
            "basisEdgeId": "edge-1",
            "classificationRunId": "classification-run-1",
            "subjectArticleId": "law-ordinance-article-2_5",
            "objectArticleId": "law-act-article-27_2",
            "subjectSupportingSpanId": "ordinance::span-1",
            "objectSupportingSpanId": "act::span-1",
            "subjectSupportingQuote": "定義語を使用する。",
            "objectSupportingQuote": "定義語とは対象をいう。",
            "relationExplanation": "OBJECTが定める定義語をSUBJECTが利用する。",
        },
    )
    assert tuple(item.evidence_id for item in context.evidence_manifest) == (
        "law-act-article-27_2",
    )
    graph_result = next(
        item
        for item in context.recent_tool_results
        if item.request_id == "graph-request"
    )
    assert graph_result.evidence_ids == ()
    assert graph_result.evidence_count == 1
    assert graph_result.graph_projection_updated is True

    frontiers = context.graph_review_batch.candidates
    frontier = frontiers[0]
    pending_review = GraphCandidateReview(
        graph_request_ids=("graph-request",),
        reviewed_link_ids=tuple(
            dict.fromkeys(
                link.link_id for item in frontiers for link in item.links
            )
        ),
        frontier_decisions=tuple(
            GraphFrontierDecision(
                frontier_item_id=item.frontier_item_id,
                article_id=item.article_id,
                work_item_id=item.work_item_id,
                hypothesis_id=item.hypothesis_id,
                action="defer" if item == frontier else "reject",
                reason=(
                    "本文未取得の関連候補を後続stepへ残す"
                    if item == frontier
                    else "この作業には関連しない"
                ),
            )
            for item in frontiers
        ),
        reason="未取得frontierが残る",
    )
    pending_context = build_solver_context(
        state.model_copy(update={"graph_candidate_reviews": (pending_review,)}),
        AgentLimits(
            max_material_evidence_chars=1000,
            max_solver_input_chars=20000,
        ),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    assert pending_context.required_graph_review_request_ids == ()
    assert any(
        item.frontier_item_id == frontier.frontier_item_id
        and item.review_status == "relevant_deferred"
        for item in pending_context.graph_review_ledger
    )

    revised_review = pending_review.model_copy(
        update={
            "frontier_decisions": (
                pending_review.frontier_decisions[0].model_copy(
                    update={
                        "action": "reject",
                        "reason": "追加本文により候補は不要と再評価した",
                    }
                ),
                *pending_review.frontier_decisions[1:],
            ),
            "reason": "未取得frontierは残らない",
        }
    )
    revised_context = build_solver_context(
        state.model_copy(
            update={"graph_candidate_reviews": (pending_review, revised_review)}
        ),
        AgentLimits(
            max_material_evidence_chars=1000,
            max_solver_input_chars=20000,
        ),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    assert revised_context.required_graph_review_request_ids == ()
    assert any(
        item.frontier_item_id == frontier.frontier_item_id
        and item.review_status == "rejected"
        for item in revised_context.graph_review_ledger
    )


def test_graph_review_persists_llm_selection_without_duplicate_fetch_request() -> None:
    state = CaseState(
        case_id="case-1",
        question="質問",
        work_items=(WorkItem(work_item_id="w1", question="下位法令を確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="下位法令に具体化規定がある",
            ),
        ),
    )
    selection = GraphCandidateReview(
        graph_request_ids=("graph-request",),
        reviewed_link_ids=("link-1",),
        frontier_decisions=(
            GraphFrontierDecision(
                frontier_item_id="frontier-1",
                article_id="law-ordinance-article-10",
                work_item_id="w1",
                hypothesis_id="h1",
                action="select",
                reason="手続の具体化規定を確認する",
            ),
        ),
        reason="手続の具体化規定である可能性があるため本文を確認する",
    )
    updated = apply_solver_decision(
        state,
        SolverDecision(
            next="continue",
            next_focus_work_item_ids=("w1",),
            graph_candidate_review=selection,
        ),
        limits=AgentLimits(),
        known_tool_names={"fetch_articles"},
        material_evidence_ids=(),
        fetchable_article_ids=("law-ordinance-article-10",),
        required_graph_review_request_ids=("graph-request",),
        graph_candidate_article_ids=("law-ordinance-article-10",),
        graph_review_frontiers={
            "frontier-1": ("law-ordinance-article-10", "w1", "h1")
        },
        graph_review_link_ids=("link-1",),
        graph_selectable_frontiers={
            "frontier-1": ("law-ordinance-article-10", "w1", "h1")
        },
        graph_review_fetch_tool_name="fetch_articles",
        tool_list_argument_limits={("fetch_articles", "article_ids"): 4},
        finalize_only=False,
    )

    assert updated.graph_candidate_reviews == (
        selection.model_copy(update={"reviewed_cycle": 1}),
    )

    with pytest.raises(ContractViolation, match="graph_candidate_review"):
        apply_solver_decision(
            state,
            SolverDecision(
                next="continue",
                next_focus_work_item_ids=("w1",),
                tool_requests=(
                    ToolRequest(
                        request_id="r-missing-review",
                        work_item_id="w1",
                        tool_name="fetch_articles",
                        arguments={"article_ids": ["law-ordinance-article-10"]},
                        purpose="候補本文を確認する",
                        hypothesis_ids=("h1",),
                    ),
                ),
            ),
            limits=AgentLimits(),
            known_tool_names={"fetch_articles"},
            material_evidence_ids=(),
            required_graph_review_request_ids=("graph-request",),
            graph_candidate_article_ids=("law-ordinance-article-10",),
            graph_review_fetch_tool_name="fetch_articles",
            finalize_only=False,
        )

    with pytest.raises(ContractViolation, match="returns selections only"):
        apply_solver_decision(
            state,
            SolverDecision(
                next="continue",
                next_focus_work_item_ids=("w1",),
                graph_candidate_review=selection,
                tool_requests=(
                    ToolRequest(
                        request_id="fetch-target",
                        work_item_id="w1",
                        tool_name="fetch_articles",
                        arguments={"article_ids": ["law-ordinance-article-10"]},
                        purpose="重複した本文取得要求",
                        hypothesis_ids=("h1",),
                    ),
                ),
            ),
            limits=AgentLimits(),
            known_tool_names={"fetch_articles"},
            material_evidence_ids=(),
            fetchable_article_ids=("law-ordinance-article-10",),
            required_graph_review_request_ids=("graph-request",),
            graph_candidate_article_ids=("law-ordinance-article-10",),
            graph_review_frontiers={
                "frontier-1": ("law-ordinance-article-10", "w1", "h1")
            },
            graph_review_link_ids=("link-1",),
            graph_selectable_frontiers={
                "frontier-1": ("law-ordinance-article-10", "w1", "h1")
            },
            graph_review_fetch_tool_name="fetch_articles",
            finalize_only=False,
        )


def test_graph_review_may_defer_relevant_fetchable_candidate_with_capacity() -> None:
    state = CaseState(
        case_id="case-1",
        question="質問",
        work_items=(WorkItem(work_item_id="w1", question="下位法令を確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="下位法令に具体化規定がある",
            ),
        ),
    )
    review = GraphCandidateReview(
        graph_request_ids=("graph-request",),
        reviewed_link_ids=("link-1",),
        frontier_decisions=(
            GraphFrontierDecision(
                frontier_item_id="frontier-1",
                article_id="law-ordinance-article-10",
                work_item_id="w1",
                hypothesis_id="h1",
                action="defer",
                reason="関連するが本文未確認なので保留する",
            ),
        ),
        reason="関連候補を後続へ保留する",
    )

    updated = apply_solver_decision(
        state,
        SolverDecision(
            next="continue",
            next_focus_work_item_ids=("w1",),
            graph_candidate_review=review,
        ),
        limits=AgentLimits(),
        known_tool_names={"fetch_articles"},
        material_evidence_ids=(),
        fetchable_article_ids=("law-ordinance-article-10",),
        required_graph_review_request_ids=("graph-request",),
        graph_candidate_article_ids=("law-ordinance-article-10",),
        graph_review_frontiers={
            "frontier-1": ("law-ordinance-article-10", "w1", "h1")
        },
        graph_review_link_ids=("link-1",),
        graph_selectable_frontiers={
            "frontier-1": ("law-ordinance-article-10", "w1", "h1")
        },
        graph_review_fetch_tool_name="fetch_articles",
        remaining_fetch_capacity=1,
        finalize_only=False,
    )

    assert updated.graph_candidate_reviews[-1].frontier_decisions[0].action == (
        "defer"
    )


def test_graph_review_may_select_fewer_relevant_articles_than_capacity() -> None:
    state = CaseState(
        case_id="case-graph-underfill",
        question="質問",
        work_items=(WorkItem(work_item_id="w1", question="下位法令を確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="下位法令に具体化規定がある",
            ),
        ),
    )
    review = GraphCandidateReview(
        graph_request_ids=("graph-request",),
        reviewed_link_ids=("link-1", "link-2"),
        frontier_decisions=(
            GraphFrontierDecision(
                frontier_item_id="frontier-1",
                article_id="article-1",
                work_item_id="w1",
                hypothesis_id="h1",
                action="select",
                reason="具体化規定なので本文を確認する",
            ),
            GraphFrontierDecision(
                frontier_item_id="frontier-2",
                article_id="article-2",
                work_item_id="w1",
                hypothesis_id="h1",
                action="defer",
                reason="関連する具体化規定だが保留する",
            ),
        ),
        reason="二つの関連候補を評価した",
    )

    updated = apply_solver_decision(
        state,
        SolverDecision(
            next="continue",
            next_focus_work_item_ids=("w1",),
            graph_candidate_review=review,
        ),
        limits=AgentLimits(max_selected_frontier_per_step=3),
        known_tool_names={"fetch_articles"},
        material_evidence_ids=(),
        fetchable_article_ids=("article-1", "article-2"),
        required_graph_review_request_ids=("graph-request",),
        graph_candidate_article_ids=("article-1", "article-2"),
        graph_review_frontiers={
            "frontier-1": ("article-1", "w1", "h1"),
            "frontier-2": ("article-2", "w1", "h1"),
        },
        graph_review_link_ids=("link-1", "link-2"),
        graph_selectable_frontiers={
            "frontier-1": ("article-1", "w1", "h1"),
            "frontier-2": ("article-2", "w1", "h1"),
        },
        graph_review_fetch_tool_name="fetch_articles",
        remaining_fetch_capacity=2,
        finalize_only=False,
    )

    assert updated.graph_candidate_reviews[-1].selected_article_ids == (
        "article-1",
    )


def test_cycle_cannot_restart_after_every_declared_task_is_resolved() -> None:
    state = CaseState(
        case_id="case-1",
        question="質問",
        work_items=(
            WorkItem(
                work_item_id="w1",
                question="根拠を確認する",
                state="resolved",
                resolution="確認済み",
                basis_hypothesis_ids=("h1",),
            ),
        ),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="根拠がある",
                judgment="supported",
                evidence_ids=("e1",),
            ),
        ),
        evidence=(
            Evidence(
                evidence_id="e1",
                source_ref="fake://e1",
                content="根拠本文",
                created_cycle=1,
            ),
        ),
    )

    with pytest.raises(
        ContractViolation,
        match="start_next_cycle requires an unresolved WorkItem",
    ):
        apply_solver_decision(
            state,
            SolverDecision(
                next="continue",
                start_next_cycle=True,
                decision_reason="全確認事項が解決済み",
            ),
            limits=AgentLimits(),
            known_tool_names=set(),
            material_evidence_ids=("e1",),
            finalize_only=False,
            cycle_close_required=True,
            can_start_next_cycle=True,
        )


def test_finalize_requires_solver_declared_basis_evidence_in_citations() -> None:
    state = CaseState(
        case_id="case-1",
        question="質問",
        work_items=(
            WorkItem(
                work_item_id="w1",
                question="根拠を確認する",
                basis_hypothesis_ids=("h1",),
            ),
        ),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="根拠がある",
                judgment="supported",
                evidence_ids=("e1",),
            ),
        ),
        evidence=(
            Evidence(
                evidence_id="e1",
                source_ref="fake://e1",
                content="根拠本文",
                created_cycle=1,
            ),
        ),
    )
    decision = SolverDecision(
        next="finalize",
        update=CaseUpdate(
            update_work_items=(
                WorkItemUpdate(
                    work_item_id="w1",
                    state="resolved",
                    resolution="根拠を確認した",
                    basis_hypothesis_ids=("h1",),
                ),
            ),
        ),
        answer=FinalAnswer(text="回答", citation_ids=()),
    )

    with pytest.raises(ContractViolation, match="resolved WorkItem basis"):
        apply_solver_decision(
            state,
            decision,
            limits=AgentLimits(),
            known_tool_names={"search"},
            material_evidence_ids=("e1",),
            finalize_only=False,
        )


def test_graph_review_must_decide_every_frontier_in_the_current_batch() -> None:
    state = CaseState(
        case_id="case-1",
        question="質問",
        work_items=(WorkItem(work_item_id="w1", question="手続を確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="手続の具体化規定がある",
            ),
        ),
    )
    first = "law-ordinance-article-10"
    deferred = "law-ordinance-article-12"
    review = GraphCandidateReview(
        graph_request_ids=("graph-request",),
        reviewed_link_ids=("link-1", "link-2"),
        frontier_decisions=(
            GraphFrontierDecision(
                frontier_item_id="frontier-1",
                article_id=first,
                work_item_id="w1",
                hypothesis_id="h1",
                action="select",
                reason="手続の具体化候補を確認する",
            ),
        ),
        reason="手続候補を確認する",
    )
    with pytest.raises(ContractViolation, match="decide every batch Frontier"):
        apply_solver_decision(
            state,
            SolverDecision(
                next="continue",
                next_focus_work_item_ids=("w1",),
                graph_candidate_review=review,
            ),
            limits=AgentLimits(),
            known_tool_names={"fetch_articles"},
            material_evidence_ids=(),
            fetchable_article_ids=(first, deferred),
            required_graph_review_request_ids=("graph-request",),
            graph_candidate_article_ids=(first, deferred),
            graph_review_frontiers={
                "frontier-1": (first, "w1", "h1"),
                "frontier-2": (deferred, "w1", "h1"),
            },
            graph_review_link_ids=("link-1", "link-2"),
            graph_selectable_frontiers={
                "frontier-1": (first, "w1", "h1"),
                "frontier-2": (deferred, "w1", "h1"),
            },
            graph_review_fetch_tool_name="fetch_articles",
            tool_list_argument_limits={("fetch_articles", "article_ids"): 4},
            finalize_only=False,
        )


def test_graph_review_selection_is_bound_to_frontier_identity() -> None:
    state = CaseState(
        case_id="case-1",
        question="質問",
        work_items=(WorkItem(work_item_id="w1", question="手続を確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="手続の具体化規定がある",
            ),
        ),
    )
    candidate = "law-ordinance-article-10"
    review = GraphCandidateReview(
        graph_request_ids=("graph-request",),
        reviewed_link_ids=("link-1",),
        frontier_decisions=(
            GraphFrontierDecision(
                frontier_item_id="frontier-1",
                article_id=candidate,
                work_item_id="w1",
                hypothesis_id="h1",
                action="select",
                reason="親規定と具体化規定が関係する",
            ),
        ),
        reason="未取得の具体化規定を確認する",
    )
    updated = apply_solver_decision(
        state,
        SolverDecision(
            next="continue",
            next_focus_work_item_ids=("w1",),
            graph_candidate_review=review,
        ),
        limits=AgentLimits(),
        known_tool_names={"fetch_articles"},
        material_evidence_ids=(),
        fetchable_article_ids=(candidate,),
        required_graph_review_request_ids=("graph-request",),
        graph_candidate_article_ids=(candidate,),
        graph_known_article_ids=(candidate,),
        graph_review_frontiers={"frontier-1": (candidate, "w1", "h1")},
        graph_review_link_ids=("link-1",),
        graph_selectable_frontiers={
            "frontier-1": (candidate, "w1", "h1")
        },
        graph_review_fetch_tool_name="fetch_articles",
        tool_list_argument_limits={("fetch_articles", "article_ids"): 1},
        finalize_only=False,
    )

    assert updated.graph_candidate_reviews == (
        review.model_copy(update={"reviewed_cycle": 1}),
    )


def test_graph_catalog_normalizes_articles_and_preserves_every_relation_path() -> None:
    candidate_id = "law-ordinance-article-2_5"
    graph_evidence = tuple(
        Evidence(
            evidence_id=f"graph-nav-{index}",
            source_ref=f"neo4j:edge:{index}",
            content=json.dumps(
                {
                    "seedArticleId": seed_id,
                    "seedDocumentId": seed_id.rsplit("-article-", 1)[0],
                    "seedTitle": seed_title,
                    "neighborArticleId": candidate_id,
                    "neighborDocumentId": "law-ordinance",
                    "neighborTitle": "公開買付府令",
                    "neighborHeading": "第二条の五",
                    "relations": [
                        {
                            "kind": kind,
                            "edgeType": edge_type,
                            "direction": direction,
                            "status": status,
                            "sourceId": f"audit-edge-{index}",
                            "relationSource": "audit-only",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            created_cycle=1,
            metadata={
                "docType": "graph_navigation",
                "citationEligible": False,
                "seedArticleId": seed_id,
                "neighborArticleId": candidate_id,
            },
        )
        for index, (seed_id, seed_title, kind, edge_type, direction, status) in enumerate(
            (
                (
                    "law-order-article-7",
                    "金融商品取引法施行令",
                    "formal_relation",
                    "REFERENCES",
                    "incoming",
                    "unverified",
                ),
                (
                    "law-act-article-27_2",
                    "金融商品取引法",
                    "relation_assertion",
                    "IMPLEMENTS",
                    "outgoing",
                    "llm_classified_implements",
                ),
            ),
            start=1,
        )
    )
    state = CaseState(
        case_id="case-1",
        question="公開買付けの要件は何ですか",
        research_cycle_count=1,
        work_items=(WorkItem(work_item_id="w1", question="適用要件を確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="下位法令に具体的要件がある",
            ),
        ),
        tool_requests=tuple(
            ToolRequest(
                request_id=f"graph-request-{index}",
                work_item_id="w1",
                tool_name="legal_graph_neighbors",
                arguments={"article_ids": [evidence.metadata["seedArticleId"]]},
                purpose="1ホップを取得する",
                hypothesis_ids=("h1",),
            )
            for index, evidence in enumerate(graph_evidence, start=1)
        ),
        evidence=graph_evidence,
        tool_results=tuple(
            ToolResult(
                request_id=f"graph-request-{index}",
                status="succeeded",
                evidence_ids=(evidence.evidence_id,),
                cycle_no=1,
            )
            for index, evidence in enumerate(graph_evidence, start=1)
        ),
    )

    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )

    assert len(context.graph_review_batch.candidates) == 1
    candidate = context.graph_review_batch.candidates[0]
    assert candidate.article_id == candidate_id
    assert len(candidate.links) == 2
    assert {item.seed_article_id for item in candidate.links} == {
        "law-order-article-7",
        "law-act-article-27_2",
    }
    assert {item.relations[0]["edgeType"] for item in candidate.links} == {
        "REFERENCES",
        "IMPLEMENTS",
    }
    serialized = context.model_dump(mode="json")
    assert serialized["evidence_manifest"] == []
    assert serialized["navigation_evidence_ids"] == []
    assert serialized["omitted_evidence_ids"] == []
    assert all(
        result["evidence_ids"] == []
        for result in serialized["recent_tool_results"]
    )
    assert "sourceId" not in json.dumps(serialized["graph_review_batch"])
    assert set(context.fetchable_article_ids) == {candidate_id}


def test_required_dependency_decision_must_cover_open_work_and_reference_action() -> None:
    source_evidence = Evidence(
        evidence_id="source-1",
        source_ref="fake:source-1",
        content="政令で定める。",
        created_cycle=1,
        metadata={
            "articleId": "law-a-article-1",
            "documentId": "law-a",
            "citationEligible": True,
        },
    )
    state = CaseState(
        case_id="case-1",
        question="質問",
        research_cycle_count=1,
        work_items=(WorkItem(work_item_id="w1", question="下位法令を確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="下位法令に詳細がある",
            ),
        ),
        evidence=(source_evidence,),
    )
    request = ToolRequest(
        request_id="graph-1",
        work_item_id="w1",
        tool_name="legal_graph_neighbors",
        arguments={"article_ids": ["law-a-article-1"]},
        purpose="下位法令候補を確認する",
        hypothesis_ids=("h1",),
    )

    with pytest.raises(ContractViolation, match="lower_law decisions"):
        apply_solver_decision(
            state,
            SolverDecision(next="continue", tool_requests=(request,)),
            limits=AgentLimits(),
            known_tool_names={"legal_graph_neighbors"},
            material_evidence_ids=(source_evidence.evidence_id,),
            fetchable_article_ids=("law-a-article-1",),
            required_dependency_kind="lower_law",
            require_dependency_decisions=True,
            finalize_only=False,
        )

    missing_text_state = apply_solver_decision(
        state,
        SolverDecision(
            next="continue",
            dependency_decisions=(
                DependencyDecision(
                    dependency_kind="lower_law",
                    work_item_id="w1",
                    status="needs_action",
                    reason="判断に使える委任元本文がなく、接続先を調べる",
                    action_request_id="graph-1",
                ),
            ),
            tool_requests=(request,),
        ),
        limits=AgentLimits(),
        known_tool_names={"legal_graph_neighbors"},
        material_evidence_ids=(source_evidence.evidence_id,),
        fetchable_article_ids=("law-a-article-1",),
        required_dependency_kind="lower_law",
        require_dependency_decisions=True,
        finalize_only=False,
    )
    assert missing_text_state.dependency_decisions[0].basis_evidence_ids == ()

    updated = apply_solver_decision(
        state,
        SolverDecision(
            next="continue",
            dependency_decisions=(
                DependencyDecision(
                    dependency_kind="lower_law",
                    work_item_id="w1",
                    status="needs_action",
                    reason="親Articleから接続先を調べる",
                    basis_evidence_ids=(source_evidence.evidence_id,),
                    action_request_id="graph-1",
                ),
            ),
            tool_requests=(request,),
        ),
        limits=AgentLimits(),
        known_tool_names={"legal_graph_neighbors"},
        material_evidence_ids=(source_evidence.evidence_id,),
        fetchable_article_ids=("law-a-article-1",),
        required_dependency_kind="lower_law",
        require_dependency_decisions=True,
        finalize_only=False,
    )

    assert updated.dependency_decisions[0].status == "needs_action"

    cycle_boundary = apply_solver_decision(
        state,
        SolverDecision(
            next="continue",
            start_next_cycle=True,
            dependency_decisions=(
                DependencyDecision(
                    dependency_kind="lower_law",
                    work_item_id="w1",
                    status="needs_action",
                    reason="次Cycleで下位法令候補を確認する",
                    basis_evidence_ids=(source_evidence.evidence_id,),
                    action_request_id=None,
                ),
            ),
        ),
        limits=AgentLimits(),
        known_tool_names={"legal_graph_neighbors"},
        material_evidence_ids=(source_evidence.evidence_id,),
        fetchable_article_ids=("law-a-article-1",),
        required_dependency_kind="lower_law",
        require_dependency_decisions=True,
        cycle_close_required=True,
        finalize_only=False,
    )

    assert cycle_boundary.dependency_decisions[0].action_request_id is None


def test_unresolved_hypothesis_can_bind_only_presented_grounding_evidence() -> None:
    state = CaseState(
        case_id="case-unresolved-evidence",
        question="質問",
        work_items=(WorkItem(work_item_id="w1", question="確認事項"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="本文で一部を確認する命題",
            ),
        ),
        evidence=(
            Evidence(
                evidence_id="e1",
                source_ref="fixture:e1",
                content="命題の一部を確認できる本文",
                created_cycle=1,
            ),
        ),
    )
    decision = SolverDecision(
        next="continue",
        update=CaseUpdate(
            update_hypotheses=(
                HypothesisUpdate(
                    hypothesis_id="h1",
                    judgment="unresolved",
                    evidence_ids=("e1",),
                    gaps=("残る未確認事項",),
                ),
            ),
        ),
        tool_requests=(
            ToolRequest(
                request_id="search-1",
                work_item_id="w1",
                tool_name="search",
                purpose="残る未確認事項を検索する",
                hypothesis_ids=("h1",),
            ),
        ),
    )

    with pytest.raises(
        ContractViolation,
        match="hypothesis update uses evidence not shown in full",
    ):
        apply_solver_decision(
            state,
            decision,
            limits=AgentLimits(),
            known_tool_names={"search"},
            material_evidence_ids=(),
            finalize_only=False,
        )

    updated = apply_solver_decision(
        state,
        decision,
        limits=AgentLimits(),
        known_tool_names={"search"},
        material_evidence_ids=("e1",),
        finalize_only=False,
    )
    assert updated.hypotheses[0].judgment == "unresolved"
    assert updated.hypotheses[0].evidence_ids == ("e1",)
    assert updated.hypotheses[0].gaps == ("残る未確認事項",)


def test_fetched_article_can_be_reused_as_a_graph_origin_but_not_refetched() -> None:
    source = Evidence(
        evidence_id="source-article-body",
        source_ref="fake:source",
        content="委任先を確認する。",
        created_cycle=1,
        metadata={
            "articleId": "law-a-article-1",
            "citationEligible": True,
        },
    )
    state = CaseState(
        case_id="case-1",
        question="質問",
        research_cycle_count=1,
        work_items=(WorkItem(work_item_id="w1", question="下位法令を確認する"),),
        evidence=(source,),
    )
    graph_request = ToolRequest(
        request_id="graph-1",
        work_item_id="w1",
        tool_name="legal_graph_neighbors",
        arguments={"article_ids": ["law-a-article-1"]},
        purpose="取得済みArticleを起点に1ホップ確認する",
    )

    updated = apply_solver_decision(
        state,
        SolverDecision(next="continue", tool_requests=(graph_request,)),
        limits=AgentLimits(),
        known_tool_names={"legal_graph_neighbors", "fetch_articles"},
        material_evidence_ids=(source.evidence_id,),
        fetchable_article_ids=(),
        graph_review_fetch_tool_name="fetch_articles",
        finalize_only=False,
    )

    assert updated.tool_requests[-1] == graph_request

    refetch_request = graph_request.model_copy(
        update={"request_id": "fetch-1", "tool_name": "fetch_articles"}
    )
    with pytest.raises(ContractViolation, match="unknown Article IDs"):
        apply_solver_decision(
            state,
            SolverDecision(next="continue", tool_requests=(refetch_request,)),
            limits=AgentLimits(),
            known_tool_names={"legal_graph_neighbors", "fetch_articles"},
            material_evidence_ids=(source.evidence_id,),
            fetchable_article_ids=(),
            graph_review_fetch_tool_name="fetch_articles",
            finalize_only=False,
        )

    unknown_graph_request = graph_request.model_copy(
        update={
            "request_id": "graph-unknown",
            "arguments": {"article_ids": ["law-a-article-unknown"]},
        }
    )
    with pytest.raises(ContractViolation, match="unknown Article IDs"):
        apply_solver_decision(
            state,
            SolverDecision(next="continue", tool_requests=(unknown_graph_request,)),
            limits=AgentLimits(),
            known_tool_names={"legal_graph_neighbors", "fetch_articles"},
            material_evidence_ids=(source.evidence_id,),
            fetchable_article_ids=(),
            graph_review_fetch_tool_name="fetch_articles",
            finalize_only=False,
        )


def test_completed_dependency_cannot_reference_an_action_request() -> None:
    source_evidence = Evidence(
        evidence_id="source-1",
        source_ref="fake:source-1",
        content="内閣府令で定める。",
        created_cycle=1,
        metadata={"articleId": "law-a-article-1", "documentId": "law-a"},
    )
    state = CaseState(
        case_id="case-1",
        question="質問",
        research_cycle_count=1,
        work_items=(WorkItem(work_item_id="w1", question="確認"),),
        evidence=(source_evidence,),
    )
    request = ToolRequest(
        request_id="fetch-1",
        work_item_id="w1",
        tool_name="fetch_articles",
        arguments={"article_ids": ["law-a-article-1"]},
        purpose="本文を取得する",
    )

    with pytest.raises(ContractViolation, match="cannot reference an action request"):
        apply_solver_decision(
            state,
            SolverDecision(
                next="continue",
                dependency_decisions=(
                    DependencyDecision(
                        dependency_kind="lower_law",
                        work_item_id="w1",
                        status="resolved",
                        reason="確認済みとする",
                        basis_evidence_ids=(source_evidence.evidence_id,),
                        action_request_id=request.request_id,
                    ),
                ),
                tool_requests=(request,),
            ),
            limits=AgentLimits(),
            known_tool_names={"fetch_articles"},
            material_evidence_ids=(source_evidence.evidence_id,),
            fetchable_article_ids=("law-a-article-1",),
            required_dependency_kind="lower_law",
            require_dependency_decisions=True,
            finalize_only=False,
        )


def test_resolved_dependency_requires_current_grounding_evidence() -> None:
    source_evidence = Evidence(
        evidence_id="law-act-article-1-paragraph-1",
        source_ref="fake:act-1",
        content="政令で定める。",
        created_cycle=1,
        metadata={
            "citationEligible": True,
            "documentId": "law-act",
            "articleId": "law-act-article-1",
        },
    )
    evidence = Evidence(
        evidence_id="law-order-article-2-paragraph-1",
        source_ref="fake:order-2",
        content="下位法令本文",
        created_cycle=1,
        metadata={
            "citationEligible": True,
            "documentId": "law-order",
            "articleId": "law-order-article-2",
        },
    )
    state = CaseState(
        case_id="case-1",
        question="質問",
        research_cycle_count=1,
        work_items=(WorkItem(work_item_id="w1", question="確認"),),
        evidence=(source_evidence, evidence),
    )
    decision = SolverDecision(
        next="finalize",
        update=CaseUpdate(
            update_work_items=(
                WorkItemUpdate(
                    work_item_id="w1",
                    state="resolved",
                    resolution="下位法令本文を確認した",
                ),
            ),
        ),
        dependency_decisions=(
            DependencyDecision(
                dependency_kind="lower_law",
                work_item_id="w1",
                status="resolved",
                reason="下位法令本文で確認した",
                basis_evidence_ids=(
                    source_evidence.evidence_id,
                    evidence.evidence_id,
                ),
            ),
        ),
        answer=FinalAnswer(
            text="回答",
            citation_ids=(source_evidence.evidence_id, evidence.evidence_id),
        ),
    )

    updated = apply_solver_decision(
        state,
        decision,
        limits=AgentLimits(),
        known_tool_names=(),
        material_evidence_ids=(source_evidence.evidence_id, evidence.evidence_id),
        required_dependency_kind="lower_law",
        require_dependency_decisions=True,
        finalize_only=False,
    )

    assert updated.dependency_decisions[0].status == "resolved"


def test_resolved_dependency_requires_source_and_target_articles() -> None:
    source_paragraph_1 = Evidence(
        evidence_id="law-act-article-1-paragraph-1",
        source_ref="fake:act-1-p1",
        content="政令で定める。",
        created_cycle=1,
        metadata={"articleId": "law-act-article-1"},
    )
    source_paragraph_2 = Evidence(
        evidence_id="law-act-article-1-paragraph-2",
        source_ref="fake:act-1-p2",
        content="同じArticleの別Paragraph。",
        created_cycle=1,
        metadata={"articleId": "law-act-article-1"},
    )
    state = CaseState(
        case_id="case-1",
        question="質問",
        research_cycle_count=1,
        work_items=(WorkItem(work_item_id="w1", question="確認"),),
        evidence=(source_paragraph_1, source_paragraph_2),
    )
    decision = SolverDecision(
        next="finalize",
        update=CaseUpdate(
            update_work_items=(
                WorkItemUpdate(
                    work_item_id="w1",
                    state="resolved",
                    resolution="下位規範まで確認した",
                ),
            ),
        ),
        dependency_decisions=(
            DependencyDecision(
                dependency_kind="lower_law",
                work_item_id="w1",
                status="resolved",
                reason="同じArticleの二つのParagraphで確認した",
                basis_evidence_ids=(
                    source_paragraph_1.evidence_id,
                    source_paragraph_2.evidence_id,
                ),
            ),
        ),
        answer=FinalAnswer(
            text="回答",
            citation_ids=(source_paragraph_1.evidence_id,),
        ),
    )

    with pytest.raises(
        ContractViolation,
        match=(
            "work item 'w1' requires full-text evidence from at least two "
            "distinct Articles"
        ),
    ):
        apply_solver_decision(
            state,
            decision,
            limits=AgentLimits(),
            known_tool_names=(),
            material_evidence_ids=(
                source_paragraph_1.evidence_id,
                source_paragraph_2.evidence_id,
            ),
            required_dependency_kind="lower_law",
            require_dependency_decisions=True,
            finalize_only=False,
        )


def test_resolved_dependency_leaves_document_meaning_to_the_solver() -> None:
    source_evidence = Evidence(
        evidence_id="law-act-article-1-paragraph-1",
        source_ref="fake:act-1",
        content="政令で定める。",
        created_cycle=1,
        metadata={
            "citationEligible": True,
            "documentId": "law-act",
            "articleId": "law-act-article-1",
        },
    )
    other_article_same_document = Evidence(
        evidence_id="law-act-article-2-paragraph-1",
        source_ref="fake:act-2",
        content="別Article本文",
        created_cycle=1,
        metadata={
            "citationEligible": True,
            "documentId": "law-act",
            "articleId": "law-act-article-2",
        },
    )
    state = CaseState(
        case_id="case-1",
        question="質問",
        research_cycle_count=1,
        work_items=(WorkItem(work_item_id="w1", question="確認"),),
        evidence=(source_evidence, other_article_same_document),
    )

    decision = SolverDecision(
        next="finalize",
        update=CaseUpdate(
            update_work_items=(
                WorkItemUpdate(
                    work_item_id="w1",
                    state="resolved",
                    resolution="確認済み",
                ),
            ),
        ),
        dependency_decisions=(
            DependencyDecision(
                dependency_kind="lower_law",
                work_item_id="w1",
                status="resolved",
                reason="別Articleで確認した",
                basis_evidence_ids=(
                    source_evidence.evidence_id,
                    other_article_same_document.evidence_id,
                ),
            ),
        ),
        answer=FinalAnswer(
            text="回答",
            citation_ids=(other_article_same_document.evidence_id,),
        ),
    )

    with pytest.raises(
        ContractViolation,
        match="citations omit Articles declared as a resolved dependency basis",
    ):
        apply_solver_decision(
            state,
            decision,
            limits=AgentLimits(),
            known_tool_names=(),
            material_evidence_ids=(
                source_evidence.evidence_id,
                other_article_same_document.evidence_id,
            ),
            required_dependency_kind="lower_law",
            require_dependency_decisions=True,
            finalize_only=False,
        )

    updated = apply_solver_decision(
        state,
        decision.model_copy(
            update={
                "answer": FinalAnswer(
                    text="回答",
                    citation_ids=(
                        source_evidence.evidence_id,
                        other_article_same_document.evidence_id,
                    ),
                )
            }
        ),
        limits=AgentLimits(),
        known_tool_names=(),
        material_evidence_ids=(
            source_evidence.evidence_id,
            other_article_same_document.evidence_id,
        ),
        required_dependency_kind="lower_law",
        require_dependency_decisions=True,
        finalize_only=False,
    )

    assert updated.dependency_decisions[0].status == "resolved"


def test_context_round_robins_parallel_tool_evidence_before_material_limit() -> None:
    evidence = tuple(
        Evidence(
            evidence_id=evidence_id,
            source_ref=f"fake:{evidence_id}",
            content=character * 600,
            created_cycle=1,
        )
        for evidence_id, character in (
            ("a1", "a"),
            ("a2", "b"),
            ("b1", "c"),
            ("b2", "d"),
        )
    )
    state = CaseState(
        case_id="case-1",
        question="質問",
        research_cycle_count=1,
        evidence=evidence,
        tool_results=(
            ToolResult(
                request_id="r1",
                status="succeeded",
                evidence_ids=("a1", "a2"),
                cycle_no=1,
            ),
            ToolResult(
                request_id="r2",
                status="succeeded",
                evidence_ids=("b1", "b2"),
                cycle_no=1,
            ),
        ),
    )

    context = build_solver_context(
        state,
        AgentLimits(max_material_evidence_chars=1000),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )

    assert tuple(item.evidence_id for item in context.material_evidence) == (
        "a1",
    )
    assert context.omitted_evidence_ids == ("a2", "b1", "b2")


def test_agent_limits_reserve_room_for_context_structure() -> None:
    limits = AgentLimits()

    assert limits.max_material_evidence_chars == 50000
    assert limits.max_solver_input_chars == 240000

    with pytest.raises(ValueError, match="leave room"):
        AgentLimits(
            max_material_evidence_chars=50000,
            max_solver_input_chars=50000,
        )


def test_structural_validation_rejects_unknown_focus_basis_and_parent_cycle() -> None:
    limits = AgentLimits()
    answer = FinalAnswer(text="回答")

    with pytest.raises(ContractViolation, match="focus"):
        apply_solver_decision(
            CaseState(case_id="case-1", question="質問"),
            SolverDecision(
                next="finalize",
                next_focus_work_item_ids=("unknown",),
                answer=answer,
            ),
            limits=limits,
            known_tool_names=(),
            material_evidence_ids=(),
            finalize_only=False,
        )

    with pytest.raises(ContractViolation, match="basis"):
        apply_solver_decision(
            CaseState(case_id="case-1", question="質問"),
            SolverDecision(
                next="continue",
                update=CaseUpdate(
                    add_work_items=(
                        WorkItem(
                            work_item_id="w1",
                            question="作業",
                            basis_hypothesis_ids=("unknown",),
                        ),
                    )
                ),
                next_focus_work_item_ids=("w1",),
                tool_requests=(
                    ToolRequest(
                        request_id="r-basis",
                        work_item_id="w1",
                        tool_name="search",
                        purpose="基礎仮説を確認する",
                    ),
                ),
            ),
            limits=limits,
            known_tool_names=("search",),
            material_evidence_ids=(),
            finalize_only=False,
        )

    cyclic = CaseState(
        case_id="case-1",
        question="質問",
        work_items=(
            WorkItem(work_item_id="w1", parent_work_item_id="w2", question="1"),
            WorkItem(work_item_id="w2", parent_work_item_id="w1", question="2"),
        ),
    )
    with pytest.raises(ContractViolation, match="cycle"):
        apply_solver_decision(
            cyclic,
            SolverDecision(
                next="continue",
                next_focus_work_item_ids=("w1",),
                tool_requests=(
                    ToolRequest(
                        request_id="r-cycle",
                        work_item_id="w1",
                        tool_name="search",
                        purpose="循環を検査する",
                    ),
                ),
            ),
            limits=limits,
            known_tool_names=("search",),
            material_evidence_ids=(),
            finalize_only=False,
        )


def test_builtin_load_evidence_reintroduces_known_full_text() -> None:
    state = CaseState(
        case_id="case-1",
        question="質問",
        research_cycle_count=1,
        work_items=(WorkItem(work_item_id="w1", question="再読する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="既存根拠を確認する",
            ),
        ),
        evidence=(
            Evidence(
                evidence_id="e1",
                source_ref="fake://1",
                content="以前のサイクルで取得済みの本文",
                created_cycle=1,
            ),
        ),
    )
    model = FakeModel(
        [
            SolverDecision(
                next="continue",
                next_focus_work_item_ids=("w1",),
                tool_requests=(
                    ToolRequest(
                        request_id="load-1",
                        work_item_id="w1",
                        tool_name="load_evidence",
                        arguments={"evidence_ids": ["e1"]},
                        purpose="既存本文を再確認する",
                        hypothesis_ids=("h1",),
                    ),
                ),
            ),
            SolverDecision(
                next="finalize",
                update=CaseUpdate(
                    update_work_items=(
                        WorkItemUpdate(
                            work_item_id="w1",
                            state="resolved",
                            resolution="本文を再確認した",
                            basis_hypothesis_ids=("h1",),
                        ),
                    ),
                    update_hypotheses=(
                        HypothesisUpdate(
                            hypothesis_id="h1",
                            judgment="supported",
                            evidence_ids=("e1",),
                        ),
                    ),
                ),
                answer=FinalAnswer(text="再確認済み", citation_ids=("e1",)),
            ),
        ]
    )
    store = InMemoryCaseStore()
    store.create(state)

    result = AgentLoop(
        store=store,
        model=model,
        tools=ToolRegistry(()),
        profile=_profile(),
    ).run("case-1")

    assert result.state.run_status == "completed"
    assert model.solver_contexts[0].material_evidence == ()
    assert model.solver_contexts[1].material_evidence_ids == {"e1"}


def test_simple_store_isolated_copies_and_profile_defaults() -> None:
    store = InMemoryCaseStore()
    original = CaseState(case_id="case-1", question="質問")
    store.create(original)

    loaded = store.load("case-1")

    assert loaded == original
    assert _profile().reviewer.enabled is False


def test_profile_accepts_legacy_finalize_key_as_integration_alias() -> None:
    payload = _profile().model_dump()
    payload["solver_finalize"] = payload.pop("solver_integration")

    profile = AgentProfile.model_validate(payload)

    assert profile.solver_integration.model == "integration-model"
