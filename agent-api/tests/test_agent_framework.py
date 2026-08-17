"""新しい汎用Agent FrameworkのPhase 1契約テスト。"""

from __future__ import annotations

import json
from collections.abc import Callable
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
    GraphWorkItemAssessment,
    Hypothesis,
    ReviewFinding,
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
    assert model.solver_contexts[-1].finalize_only is True
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
                        ),
                    )
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
        return SolverDecision(
            next="continue",
            next_focus_work_item_ids=("w1",),
            graph_candidate_review=GraphCandidateReview(
                graph_request_ids=context.required_graph_review_request_ids,
                selected_article_ids=("law-order-article-2",),
                work_item_assessments=(
                        GraphWorkItemAssessment(
                            work_item_id="w1",
                            relevant_article_ids=("law-order-article-2",),
                            selected_article_ids=("law-order-article-2",),
                            reason="作業に対応する具体化規定を確認する",
                    ),
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
    assert "open work item" in feedback.violation
    assert feedback.previous_decision.tool_requests == (invalid_request,)
    assert [item.purpose for item in trace.model_calls] == [
        "research",
        "research_contract_repair",
        "integration",
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


def test_dependency_repair_receives_four_required_work_items() -> None:
    work_item_ids = tuple(f"w{index}" for index in range(1, 5))
    initial = SolverDecision(
        next="continue",
        update=CaseUpdate(
            add_work_items=tuple(
                WorkItem(work_item_id=work_item_id, question=f"確認{index}")
                for index, work_item_id in enumerate(work_item_ids, start=1)
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
            )
            for work_item_id in work_item_ids
        )
    )
    invalid = SolverDecision(
        next="finalize",
        update=close_updates,
        answer=FinalAnswer(text="回答", citation_ids=("e_r1",)),
    )
    repaired = SolverDecision(
        next="finalize",
        update=close_updates,
        dependency_decisions=tuple(
            DependencyDecision(
                dependency_kind="lower_law",
                work_item_id=work_item_id,
                status="not_required",
                reason="この作業には下位規範確認が不要",
                source_evidence_ids=("e_r1",),
            )
            for work_item_id in work_item_ids
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
    assert integration_context.required_dependency_work_item_ids == work_item_ids
    repair_context = model.solver_contexts[2]
    assert repair_context.contract_feedback is not None
    assert "missing=['w1', 'w2', 'w3', 'w4']" in (
        repair_context.contract_feedback.violation
    )
    assert [item.purpose for item in trace.model_calls] == [
        "research",
        "integration",
        "integration_contract_repair",
    ]


def test_reviewer_can_request_one_solver_revision_and_then_accept() -> None:
    model = FakeModel(
        [
            SolverDecision(
                next="finalize",
                answer=FinalAnswer(text="初稿"),
            ),
            SolverDecision(
                next="finalize",
                answer=FinalAnswer(text="修正版"),
            ),
        ],
        reviews=[
            ReviewResult(
                verdict="revise",
                findings=(ReviewFinding(description="説明が不足"),),
            ),
            ReviewResult(verdict="accept"),
        ],
    )

    state, trace = _run(model, profile=_profile(reviewer_enabled=True))

    assert state.run_status == "completed"
    assert state.final_answer == FinalAnswer(text="修正版")
    assert len(model.review_contexts) == 2
    assert model.solver_contexts[1].reviewer_findings[0].description == "説明が不足"
    assert [item.model for item in trace.model_calls] == [
        "research-model",
        "review-model",
        "integration-model",
        "review-model",
    ]


def test_second_reviewer_rejection_is_explicit_failure() -> None:
    finding = ReviewFinding(description="なお不整合")
    model = FakeModel(
        [
            SolverDecision(next="finalize", answer=FinalAnswer(text="初稿")),
            SolverDecision(next="finalize", answer=FinalAnswer(text="修正版")),
        ],
        reviews=[
            ReviewResult(verdict="revise", findings=(finding,)),
            ReviewResult(verdict="revise", findings=(finding,)),
        ],
    )

    state, _ = _run(model, profile=_profile(reviewer_enabled=True))

    assert state.run_status == "failed"
    assert state.stop_reason == "review_failed"


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
        next="finalize",
        update=CaseUpdate(
            update_hypotheses=(
                HypothesisUpdate(
                    hypothesis_id="h1",
                    judgment="contradicted",
                    evidence_ids=("e1",),
                ),
            )
        ),
        answer=FinalAnswer(text="反証された", citation_ids=("e1",)),
    )

    with pytest.raises(ContractViolation, match="impact decisions"):
        apply_solver_decision(
            state,
            incomplete,
            limits=AgentLimits(),
            known_tool_names=(),
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
        next="finalize",
        update=CaseUpdate(
            update_work_items=(
                WorkItemUpdate(
                    work_item_id="w1",
                    state="open",
                    resolution="openなのに結論あり",
                ),
            )
        ),
        answer=FinalAnswer(text="回答"),
    )

    with pytest.raises(ContractViolation, match="schema"):
        apply_solver_decision(
            state,
            invalid,
            limits=AgentLimits(),
            known_tool_names=(),
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
                content="検索候補",
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
    assert context.fetchable_article_ids == (
        "law-a-article-1",
        "order-a-article-2",
    )
    assert tuple(item.evidence_id for item in context.evidence_manifest) == (
        "law-a-article-1-paragraph-1",
    )
    assert context.omitted_evidence_ids == ()
    assert context.recent_tool_results[0].evidence_ids == (
        "law-a-article-1-paragraph-1",
    )
    assert context.recent_tool_results[0].evidence_count == 2
    assert context.recent_tool_results[0].graph_catalog_projected is True


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
                "kind": "formal_relation",
                "edgeType": "REFERENCES",
                "direction": "incoming",
                "status": "unverified",
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
    assert context.required_graph_review_work_item_ids == ("w1", "w2")
    assert "law-ordinance-article-2_5" in context.fetchable_article_ids
    catalog = context.graph_candidate_catalog
    assert tuple(item.article_id for item in catalog.articles) == (
        "law-act-article-27_2",
        "law-ordinance-article-2_5",
    )
    assert catalog.articles[0].title == "金融商品取引法"
    assert catalog.articles[0].content_status == "succeeded"
    assert catalog.articles[1].heading == "第二条の五"
    assert catalog.articles[1].content_status == "not_requested"
    assert len(catalog.links) == 1
    link = catalog.links[0]
    assert link.seed_article_id == "law-act-article-27_2"
    assert link.candidate_article_id == "law-ordinance-article-2_5"
    assert link.work_item_ids == ("w1", "w2")
    assert link.hypothesis_ids == ("h1", "h2")
    assert link.relations == (
        {
            "kind": "formal_relation",
            "edgeType": "REFERENCES",
            "direction": "incoming",
            "status": "unverified",
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
    assert graph_result.graph_catalog_projected is True

    pending_review = GraphCandidateReview(
        graph_request_ids=("graph-request",),
        selected_article_ids=(),
        work_item_assessments=(
            GraphWorkItemAssessment(
                work_item_id="w1",
                relevant_article_ids=("law-ordinance-article-2_5",),
                selected_article_ids=(),
                reason="本文未取得の関連候補を後続stepへ残す",
            ),
            GraphWorkItemAssessment(
                work_item_id="w2",
                relevant_article_ids=(),
                selected_article_ids=(),
                reason="関連候補はない",
            ),
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
    assert pending_context.required_graph_review_request_ids == ("graph-request",)

    revised_review = pending_review.model_copy(
        update={
            "work_item_assessments": (
                GraphWorkItemAssessment(
                    work_item_id="w1",
                    relevant_article_ids=(),
                    selected_article_ids=(),
                    reason="追加本文により候補は不要と再評価した",
                ),
                pending_review.work_item_assessments[1],
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
        selected_article_ids=("law-ordinance-article-10",),
        work_item_assessments=(
            GraphWorkItemAssessment(
                work_item_id="w1",
                relevant_article_ids=("law-ordinance-article-10",),
                selected_article_ids=("law-ordinance-article-10",),
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
        required_graph_review_work_item_ids=("w1",),
        graph_candidate_article_ids=("law-ordinance-article-10",),
        graph_review_fetch_tool_name="fetch_articles",
        tool_list_argument_limits={("fetch_articles", "article_ids"): 4},
        finalize_only=False,
    )

    assert updated.graph_candidate_reviews == (selection,)

    with pytest.raises(ContractViolation, match="graph_candidate_review"):
        apply_solver_decision(
            state,
            SolverDecision(next="finalize", answer=FinalAnswer(text="回答")),
            limits=AgentLimits(),
            known_tool_names={"fetch_articles"},
            material_evidence_ids=(),
            required_graph_review_request_ids=("graph-request",),
            required_graph_review_work_item_ids=("w1",),
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
            required_graph_review_work_item_ids=("w1",),
            graph_candidate_article_ids=("law-ordinance-article-10",),
            graph_review_fetch_tool_name="fetch_articles",
            finalize_only=False,
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


def test_graph_review_cannot_defer_declared_relevant_article_with_unused_capacity() -> None:
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
        selected_article_ids=(first,),
        work_item_assessments=(
            GraphWorkItemAssessment(
                work_item_id="w1",
                relevant_article_ids=(first, deferred),
                selected_article_ids=(first,),
                reason="手続の具体化候補を確認する",
            ),
        ),
        reason="手続候補を確認する",
    )
    request = ToolRequest(
        request_id="fetch-target",
        work_item_id="w1",
        tool_name="fetch_articles",
        arguments={"article_ids": [first]},
        purpose="候補本文を確認する",
        hypothesis_ids=("h1",),
    )

    with pytest.raises(ContractViolation, match="fetch capacity unused"):
        apply_solver_decision(
            state,
            SolverDecision(
                next="continue",
                next_focus_work_item_ids=("w1",),
                graph_candidate_review=review,
                tool_requests=(request,),
            ),
            limits=AgentLimits(),
            known_tool_names={"fetch_articles"},
            material_evidence_ids=(),
            fetchable_article_ids=(first, deferred),
            required_graph_review_request_ids=("graph-request",),
            required_graph_review_work_item_ids=("w1",),
            graph_candidate_article_ids=(first, deferred),
            graph_review_fetch_tool_name="fetch_articles",
            tool_list_argument_limits={("fetch_articles", "article_ids"): 4},
            finalize_only=False,
        )


def test_graph_review_relevant_ids_may_include_already_fetched_article() -> None:
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
    fetched = "law-act-article-27_3"
    candidate = "law-ordinance-article-10"
    review = GraphCandidateReview(
        graph_request_ids=("graph-request",),
        selected_article_ids=(candidate,),
        work_item_assessments=(
            GraphWorkItemAssessment(
                work_item_id="w1",
                relevant_article_ids=(fetched, candidate),
                selected_article_ids=(candidate,),
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
        fetchable_article_ids=(fetched, candidate),
        required_graph_review_request_ids=("graph-request",),
        required_graph_review_work_item_ids=("w1",),
        graph_candidate_article_ids=(candidate,),
        graph_known_article_ids=(fetched, candidate),
        graph_review_fetch_tool_name="fetch_articles",
        tool_list_argument_limits={("fetch_articles", "article_ids"): 1},
        finalize_only=False,
    )

    assert updated.graph_candidate_reviews == (review,)


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

    catalog = context.graph_candidate_catalog
    assert len(catalog.articles) == 3
    assert sum(item.article_id == candidate_id for item in catalog.articles) == 1
    assert len(catalog.links) == 2
    assert {item.seed_article_id for item in catalog.links} == {
        "law-order-article-7",
        "law-act-article-27_2",
    }
    assert {item.relations[0]["edgeType"] for item in catalog.links} == {
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
    assert "sourceId" not in json.dumps(serialized["graph_candidate_catalog"])
    assert set(context.fetchable_article_ids) == {
        "law-order-article-7",
        "law-act-article-27_2",
        candidate_id,
    }


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

    with pytest.raises(ContractViolation, match="requires source evidence"):
        apply_solver_decision(
            state,
            SolverDecision(
                next="continue",
                dependency_decisions=(
                    DependencyDecision(
                        dependency_kind="lower_law",
                        work_item_id="w1",
                        status="needs_action",
                        reason="親Articleから接続先を調べる",
                        action="discover_target",
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
                    source_evidence_ids=(source_evidence.evidence_id,),
                    action="discover_target",
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


def test_dependency_target_fetch_cannot_repeat_declared_source_article() -> None:
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

    with pytest.raises(ContractViolation, match="repeats its source article"):
        apply_solver_decision(
            state,
            SolverDecision(
                next="continue",
                dependency_decisions=(
                    DependencyDecision(
                        dependency_kind="lower_law",
                        work_item_id="w1",
                        status="needs_action",
                        reason="委任先本文を取得する",
                        source_evidence_ids=(source_evidence.evidence_id,),
                        action="fetch_target",
                        action_request_id=request.request_id,
                        target_article_ids=("law-a-article-1",),
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
            dependency_target_fetch_tool_name="fetch_articles",
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
                source_evidence_ids=(source_evidence.evidence_id,),
                target_article_ids=("law-order-article-2",),
                evidence_ids=(evidence.evidence_id,),
            ),
        ),
        answer=FinalAnswer(text="回答", citation_ids=(evidence.evidence_id,)),
    )

    updated = apply_solver_decision(
        state,
        decision,
        limits=AgentLimits(),
        known_tool_names=(),
        material_evidence_ids=(source_evidence.evidence_id, evidence.evidence_id),
        required_dependency_kind="lower_law",
        require_dependency_decisions=True,
        dependency_resolution_requires_distinct_document=True,
        finalize_only=False,
    )

    assert updated.dependency_decisions[0].status == "resolved"


def test_resolved_dependency_rejects_evidence_from_source_document() -> None:
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

    with pytest.raises(ContractViolation, match="document distinct"):
        apply_solver_decision(
            state,
            SolverDecision(
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
                        source_evidence_ids=(source_evidence.evidence_id,),
                        target_article_ids=("law-act-article-2",),
                        evidence_ids=(other_article_same_document.evidence_id,),
                    ),
                ),
                answer=FinalAnswer(
                    text="回答",
                    citation_ids=(other_article_same_document.evidence_id,),
                ),
            ),
            limits=AgentLimits(),
            known_tool_names=(),
            material_evidence_ids=(
                source_evidence.evidence_id,
                other_article_same_document.evidence_id,
            ),
            required_dependency_kind="lower_law",
            require_dependency_decisions=True,
            dependency_resolution_requires_distinct_document=True,
            finalize_only=False,
        )


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
                next="finalize",
                update=CaseUpdate(
                    add_work_items=(
                        WorkItem(
                            work_item_id="w1",
                            question="作業",
                            basis_hypothesis_ids=("unknown",),
                        ),
                    )
                ),
                answer=answer,
            ),
            limits=limits,
            known_tool_names=(),
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
            SolverDecision(next="finalize", answer=answer),
            limits=limits,
            known_tool_names=(),
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
