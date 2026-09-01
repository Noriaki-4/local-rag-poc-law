from __future__ import annotations

from typing import Any

from app.adapters.models.structured_json import (
    StructuredJSONModelAdapter,
    render_hypothesis_revision_model_call,
)
from app.agent_framework.context import build_solver_context
from app.agent_framework.profiles import AgentLimits
from app.agent_framework.state import (
    CaseState,
    DeferredFrontierResolution,
    DependencyDecision,
    Evidence,
    FrontierReAdoption,
    GraphCandidateReview,
    GraphFrontierDecision,
    Hypothesis,
    SearchCandidateReview,
    SearchCandidateSelection,
    ToolRequest,
    ToolResult,
    UnreviewedGraphResolution,
    WorkItem,
)
from app.agent_framework.contracts import (
    HypothesisRevisionDecision,
    HypothesisRevisionUpdate,
    SolverDecision,
)
from app.agent_framework.validation import (
    apply_hypothesis_revision,
    apply_solver_decision,
)
from app.domains.legal.profiles import legal_agent_profile
from app.llm import StructuredJSONResult


class FakeRevisionClient:
    provider = "openai"

    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def generate_structured_json(self, **kwargs: Any) -> StructuredJSONResult:
        self.calls.append(kwargs)
        return StructuredJSONResult(
            payload=self.payload,
            provider=self.provider,
            model=kwargs["model"],
            latencyMs=1,
            inputTokens=1,
            outputTokens=1,
            validationError=None,
            retryCount=0,
            stopReason="stop",
        )


class FakeRevisionSequenceClient(FakeRevisionClient):
    def __init__(self, payloads: list[dict[str, Any]]):
        super().__init__({})
        self.payloads = list(payloads)

    def generate_structured_json(self, **kwargs: Any) -> StructuredJSONResult:
        self.payload = self.payloads.pop(0)
        return super().generate_structured_json(**kwargs)


def _context(*, resolved: bool = False):
    return build_solver_context(
        CaseState(
            case_id="revision-case",
            question="確認事項",
            research_cycle_count=1,
            work_items=(
                WorkItem(
                    work_item_id="wi-1",
                    question="既存の確認事項",
                    state="resolved" if resolved else "open",
                    resolution="既存命題を確認済み" if resolved else None,
                    basis_hypothesis_ids=("h-1",) if resolved else (),
                ),
            ),
            hypotheses=(
                Hypothesis(
                    hypothesis_id="h-1",
                    work_item_id="wi-1",
                    statement="既存の命題",
                    judgment="contradicted",
                    evidence_ids=("e-1",),
                ),
            ),
            evidence=(
                Evidence(
                    evidence_id="e-1",
                    source_ref="source-1",
                    content="Cycleで取得した本文",
                    created_cycle=1,
                ),
            ),
        ),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )


def test_revision_adds_child_and_reopens_resolved_work_item() -> None:
    client = FakeRevisionClient(
        {
            "decision_reason": "本文に別の未確認事項がある",
            "revise_hypotheses": [],
            "add_hypotheses": [
                {
                    "hypothesis_id": "h-2",
                    "work_item_id": "wi-1",
                    "statement": "本文から判明した別の命題",
                    "evidence_ids": ["e-1"],
                    "gaps": ["追加確認事項"],
                }
            ],
        }
    )
    context = _context(resolved=True)
    profile = legal_agent_profile().solver_hypothesis_revision
    assert profile is not None

    result = StructuredJSONModelAdapter(client).solve(context, profile)
    assert result.hypothesis_revision is not None
    updated = apply_hypothesis_revision(
        CaseState(
            case_id="revision-case",
            question="確認事項",
            research_cycle_count=1,
            work_items=(
                WorkItem(
                    work_item_id="wi-1",
                    question="既存の確認事項",
                    state="resolved",
                    resolution="既存命題を確認済み",
                    basis_hypothesis_ids=("h-1",),
                ),
            ),
            hypotheses=(
                Hypothesis(
                    hypothesis_id="h-1",
                    work_item_id="wi-1",
                    statement="既存の命題",
                    judgment="supported",
                    evidence_ids=("e-1",),
                ),
            ),
            evidence=(
                Evidence(
                    evidence_id="e-1",
                    source_ref="source-1",
                    content="Cycleで取得した本文",
                    created_cycle=1,
                ),
            ),
        ),
        result.hypothesis_revision,
        material_evidence_ids={"e-1"},
        eligible_work_item_ids={"wi-1"},
    )
    assert [item.hypothesis_id for item in updated.hypotheses] == ["h-1", "h-2"]
    assert updated.hypotheses[0].statement == "既存の命題"
    assert updated.work_items[0].state == "open"


def test_revision_replaces_current_hypothesis_and_keeps_old_version_out_of_view() -> None:
    client = FakeRevisionClient(
        {
            "decision_reason": "本文により現在の見立てを修正する",
            "revise_hypotheses": [
                {
                    "hypothesis_id": "h-1",
                    "statement": "本文を踏まえた現在の命題",
                    "judgment": "supported",
                    "evidence_ids": ["e-1"],
                    "gaps": ["次Cycleで確認する条件"],
                }
            ],
            "add_hypotheses": [],
        }
    )
    state = CaseState(
        case_id="revision-current-version",
        question="確認事項",
        research_cycle_count=1,
        work_items=(
            WorkItem(
                work_item_id="wi-1",
                question="既存の確認事項",
                state="open",
            ),
        ),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h-1",
                work_item_id="wi-1",
                statement="更新前の命題",
                judgment="contradicted",
                evidence_ids=("e-1",),
            ),
        ),
        evidence=(
            Evidence(
                evidence_id="e-1",
                source_ref="source-1",
                content="Cycleで取得した本文",
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
    profile = legal_agent_profile().solver_hypothesis_revision
    assert profile is not None

    result = StructuredJSONModelAdapter(client).solve(context, profile)
    assert result.hypothesis_revision is not None
    updated = apply_hypothesis_revision(
        state,
        result.hypothesis_revision,
        material_evidence_ids={"e-1"},
        eligible_work_item_ids={"wi-1"},
    )

    assert len(updated.hypotheses) == 1
    assert updated.hypotheses[0].hypothesis_id == "h-1"
    assert updated.hypotheses[0].statement == "本文を踏まえた現在の命題"
    assert updated.hypotheses[0].judgment == "supported"
    assert updated.work_items[0].state == "open"
    assert len(updated.hypothesis_history) == 1
    assert updated.hypothesis_history[0].hypothesis.statement == "更新前の命題"
    assert updated.hypothesis_history[0].version == 1

    next_context = build_solver_context(
        updated,
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    assert [item.statement for item in next_context.hypotheses] == [
        "本文を踏まえた現在の命題"
    ]
    assert "hypothesis_history" not in next_context.model_dump()


def test_revision_invalidates_only_derived_state_from_the_old_version() -> None:
    state = CaseState(
        case_id="revision-derived-state",
        question="確認事項",
        research_cycle_count=2,
        work_items=(
            WorkItem(work_item_id="wi-1", question="更新する確認事項"),
            WorkItem(work_item_id="wi-2", question="無関係な確認事項"),
        ),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h-1",
                work_item_id="wi-1",
                statement="更新前の命題",
                judgment="contradicted",
                evidence_ids=("e-1",),
            ),
            Hypothesis(
                hypothesis_id="h-2",
                work_item_id="wi-2",
                statement="維持する命題",
                judgment="supported",
                evidence_ids=("e-2",),
            ),
        ),
        tool_requests=(
            ToolRequest(
                request_id="search-1",
                work_item_id="wi-1",
                tool_name="legal_search",
                arguments={"query": "更新前"},
                purpose="更新前の検索",
                hypothesis_ids=("h-1",),
            ),
            ToolRequest(
                request_id="search-2",
                work_item_id="wi-2",
                tool_name="legal_search",
                arguments={"query": "無関係"},
                purpose="無関係な検索",
                hypothesis_ids=("h-2",),
            ),
            ToolRequest(
                request_id="graph-1",
                work_item_id="wi-1",
                tool_name="legal_graph_neighbors",
                arguments={"article_ids": ["article-1"]},
                purpose="更新前のGraph探索",
                hypothesis_ids=("h-1",),
            ),
            ToolRequest(
                request_id="graph-2",
                work_item_id="wi-2",
                tool_name="legal_graph_neighbors",
                arguments={"article_ids": ["article-2"]},
                purpose="無関係なGraph探索",
                hypothesis_ids=("h-2",),
            ),
        ),
        tool_results=tuple(
            ToolResult(
                request_id=request_id,
                status="succeeded",
                cycle_no=1,
            )
            for request_id in ("search-1", "search-2", "graph-1", "graph-2")
        ),
        evidence=(
            Evidence(
                evidence_id="e-1",
                source_ref="source-1",
                content="現在版を修正する本文",
                created_cycle=2,
            ),
            Evidence(
                evidence_id="e-2",
                source_ref="source-2",
                content="無関係な本文",
                created_cycle=1,
            ),
        ),
        dependency_decisions=(
            DependencyDecision(
                dependency_kind="lower_norm",
                work_item_id="wi-1",
                status="not_required",
                reason="更新前の判断",
                basis_evidence_ids=("e-1",),
            ),
            DependencyDecision(
                dependency_kind="lower_norm",
                work_item_id="wi-2",
                status="not_required",
                reason="維持する判断",
                basis_evidence_ids=("e-2",),
            ),
        ),
        search_candidate_reviews=(
            SearchCandidateReview(
                search_request_ids=("search-1",),
                selections=(
                    SearchCandidateSelection(
                        article_id="article-1",
                        reason="更新前の選択",
                        matched_hypothesis_ids=("h-1",),
                    ),
                ),
                deferred_article_ids=(),
                reason="更新前の検索候補評価",
                reviewed_cycle=1,
            ),
            SearchCandidateReview(
                search_request_ids=("search-2",),
                selections=(
                    SearchCandidateSelection(
                        article_id="article-2",
                        reason="維持する選択",
                        matched_hypothesis_ids=("h-2",),
                    ),
                ),
                deferred_article_ids=(),
                reason="維持する検索候補評価",
                reviewed_cycle=1,
            ),
        ),
        graph_candidate_reviews=(
            GraphCandidateReview(
                graph_request_ids=("graph-1",),
                reviewed_link_ids=("link-1",),
                frontier_decisions=(
                    GraphFrontierDecision(
                        frontier_item_id="frontier-1",
                        article_id="article-1",
                        work_item_id="wi-1",
                        hypothesis_id="h-1",
                        action="defer",
                        reason="更新前の保留判断",
                    ),
                ),
                reason="更新前のGraph候補評価",
                reviewed_cycle=1,
            ),
            GraphCandidateReview(
                graph_request_ids=("graph-2",),
                reviewed_link_ids=("link-2",),
                frontier_decisions=(
                    GraphFrontierDecision(
                        frontier_item_id="frontier-2",
                        article_id="article-2",
                        work_item_id="wi-2",
                        hypothesis_id="h-2",
                        action="defer",
                        reason="維持する保留判断",
                    ),
                ),
                reason="維持するGraph候補評価",
                reviewed_cycle=1,
            ),
        ),
        frontier_re_adoptions=(
            FrontierReAdoption(
                article_id="article-1",
                work_item_id="wi-1",
                hypothesis_id="h-1",
                reason="更新前の再採用",
            ),
            FrontierReAdoption(
                article_id="article-2",
                work_item_id="wi-2",
                hypothesis_id="h-2",
                reason="維持する再採用",
            ),
        ),
        deferred_frontier_resolutions=(
            DeferredFrontierResolution(
                frontier_item_id="frontier-1",
                article_id="article-1",
                work_item_id="wi-1",
                hypothesis_id="h-1",
                action="carry_forward",
                reason="更新前の保留継続",
                decided_cycle=1,
            ),
            DeferredFrontierResolution(
                frontier_item_id="frontier-2",
                article_id="article-2",
                work_item_id="wi-2",
                hypothesis_id="h-2",
                action="carry_forward",
                reason="維持する保留継続",
                decided_cycle=1,
            ),
        ),
        unreviewed_graph_resolutions=(
            UnreviewedGraphResolution(
                action="review_next_cycle",
                reason="更新前の未評価候補判断",
                candidate_count=1,
                decided_cycle=1,
            ),
        ),
    )
    revision = HypothesisRevisionDecision(
        decision_reason="本文から現在版を修正する",
        revise_hypotheses=(
            HypothesisRevisionUpdate(
                hypothesis_id="h-1",
                statement="更新後の命題",
                judgment="supported",
                evidence_ids=("e-1",),
                gaps=("更新後に確認する事項",),
            ),
        ),
    )

    updated = apply_hypothesis_revision(
        state,
        revision,
        material_evidence_ids={"e-1"},
        eligible_work_item_ids={"wi-1", "wi-2"},
        eligible_hypothesis_ids={"h-1"},
    )

    assert updated.tool_requests == state.tool_requests
    assert updated.tool_results == state.tool_results
    assert updated.evidence == state.evidence
    assert updated.invalidated_tool_request_ids == ("search-1", "graph-1")
    assert [item.work_item_id for item in updated.dependency_decisions] == ["wi-2"]
    assert [
        item.search_request_ids for item in updated.search_candidate_reviews
    ] == [("search-2",)]
    assert [
        item.graph_request_ids for item in updated.graph_candidate_reviews
    ] == [("graph-2",)]
    assert [item.hypothesis_id for item in updated.frontier_re_adoptions] == [
        "h-2"
    ]
    assert [
        item.hypothesis_id for item in updated.deferred_frontier_resolutions
    ] == ["h-2"]
    assert updated.unreviewed_graph_resolutions == ()

    context = build_solver_context(
        updated,
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    assert [
        item.hypothesis_ids for item in context.completed_legal_searches
    ] == [("h-2",)]
    assert [
        item.hypothesis_ids for item in context.completed_graph_searches
    ] == [("h-2",)]

    repeated_scope = ToolRequest(
        request_id="search-current-version",
        work_item_id="wi-1",
        tool_name="legal_search",
        arguments={"query": "更新前"},
        purpose="更新後の現在版について同じ検索範囲を再評価する",
        hypothesis_ids=("h-1",),
    )
    reapplied = apply_solver_decision(
        updated,
        SolverDecision(
            next="continue",
            decision_reason="現在版の未確認事項を検索する",
            next_focus_work_item_ids=("wi-1",),
            tool_requests=(repeated_scope,),
        ),
        limits=AgentLimits(),
        known_tool_names={"legal_search"},
        material_evidence_ids={"e-1", "e-2"},
        finalize_only=False,
    )
    assert reapplied.tool_requests[-1] == repeated_scope

    same_cycle_result = ToolResult(
        request_id=repeated_scope.request_id,
        status="succeeded",
        cycle_no=state.research_cycle_count,
    )
    after_same_cycle_search = CaseState.model_validate(
        reapplied.model_copy(
            update={
                "tool_results": (*reapplied.tool_results, same_cycle_result),
            }
        ).model_dump()
    )
    same_cycle_context = build_solver_context(
        after_same_cycle_search,
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    assert {
        (item.work_item_id, item.hypothesis_ids)
        for item in same_cycle_context.completed_legal_searches
    } == {
        ("wi-1", ("h-1",)),
        ("wi-2", ("h-2",)),
    }


def test_revision_returns_no_hypothesis_for_search_strategy_only() -> None:
    client = FakeRevisionClient(
        {
            "decision_reason": "本文に独立した新しい命題はない",
            "revise_hypotheses": [],
            "add_hypotheses": [],
        }
    )
    context = _context()
    profile = legal_agent_profile().solver_hypothesis_revision
    assert profile is not None

    result = StructuredJSONModelAdapter(client).solve(context, profile)

    assert result.hypothesis_revision is not None
    assert result.hypothesis_revision.add_hypotheses == ()
    rendered = render_hypothesis_revision_model_call(context, profile)
    assert rendered.input_payload["acquired_evidence"][0]["evidence_id"] == "e-1"


def test_revision_repairs_transport_shape_once_without_changing_meaning() -> None:
    proposal = {
        "hypothesis_id": "h-2",
        "work_item_id": "wi-1",
        "statement": "本文から判明した別の命題",
        "evidence_ids": ["e-1"],
        "gaps": ["追加確認事項"],
    }
    client = FakeRevisionSequenceClient(
        [
            {
                "decision_reason": "本文に別の未確認事項がある",
                "revise_hypotheses": [],
                "add_hypotheses": [
                    {**proposal, "evidence_ids": ["e-1"] * 13}
                ],
            },
            {
                "decision_reason": "本文に別の未確認事項がある",
                "revise_hypotheses": [],
                "add_hypotheses": [proposal],
            },
        ]
    )
    context = _context()
    profile = legal_agent_profile().solver_hypothesis_revision
    assert profile is not None

    result = StructuredJSONModelAdapter(client).solve(context, profile)

    assert result.hypothesis_revision is not None
    assert result.hypothesis_revision.add_hypotheses[0].evidence_ids == ("e-1",)
    assert result.attempt_count == 2
    assert len(client.calls) == 2
    assert "transport_repair" in client.calls[1]["prompt"]


def test_revision_projection_only_includes_current_cycle_evidence() -> None:
    state = CaseState(
        case_id="revision-cycle-scope",
        question="確認事項",
        research_cycle_count=2,
        work_items=(
            WorkItem(work_item_id="wi-1", question="既存の確認事項"),
        ),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h-1",
                work_item_id="wi-1",
                statement="既存の命題",
                judgment="contradicted",
                evidence_ids=("e-new",),
            ),
        ),
        evidence=(
            Evidence(
                evidence_id="e-old",
                source_ref="source-old",
                content="前Cycleで取得した本文",
                created_cycle=1,
            ),
            Evidence(
                evidence_id="e-new",
                source_ref="source-new",
                content="現在Cycleで取得した本文",
                created_cycle=2,
            ),
        ),
    )
    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    profile = legal_agent_profile().solver_hypothesis_revision
    assert profile is not None

    rendered = render_hypothesis_revision_model_call(context, profile)

    assert [
        item["evidence_id"] for item in rendered.input_payload["acquired_evidence"]
    ] == ["e-new"]
    evidence_schema = rendered.output_schema["properties"]["add_hypotheses"][
        "items"
    ]["properties"]["evidence_ids"]["items"]
    assert evidence_schema["enum"] == ["e-new"]


def test_revision_does_not_change_the_common_solver_contract() -> None:
    assert "hypothesis_revision" not in SolverDecision.model_json_schema()[
        "properties"
    ]


def test_revision_cycle_history_must_be_positive_unique_and_completed() -> None:
    CaseState(
        case_id="valid-history",
        question="確認事項",
        research_cycle_count=2,
        hypothesis_revision_cycles=(1, 2),
    )
    for invalid_cycles in ((0,), (1, 1), (3,)):
        try:
            CaseState(
                case_id="invalid-history",
                question="確認事項",
                research_cycle_count=2,
                hypothesis_revision_cycles=invalid_cycles,
            )
        except ValueError:
            continue
        raise AssertionError(f"invalid history was accepted: {invalid_cycles}")
