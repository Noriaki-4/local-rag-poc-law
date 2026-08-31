import json
from pathlib import Path
from time import sleep
from typing import Any

import pytest
import requests

from app.adapters.models.structured_json import (
    StructuredJSONModelAdapter,
    _observation_work_item_contexts,
    render_observation_integration_model_call,
)
from app.agent_framework.context import SolverContext, build_solver_context
from app.agent_framework.ports.model import SolverCheckpointTimeout
from app.agent_framework.profiles import AgentLimits
from app.agent_framework.state import (
    CaseState,
    Evidence,
    Hypothesis,
    ToolRequest,
    ToolResult,
    WorkItem,
)
from app.agent_framework.work_item_sessions import WorkItemSessionCoordinator
from app.domains.legal.profiles import legal_agent_profile
from app.llm import StructuredJSONResult


def test_work_item_sessions_keep_affinity_and_advance_turns() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures/framework/tob_announcement_observation_scope_expansion_v409.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    contexts = _observation_work_item_contexts(
        SolverContext.model_validate(fixture["solverContext"])
    )
    assert contexts

    coordinator = WorkItemSessionCoordinator()
    first = coordinator.assign(contexts)
    second = coordinator.assign(contexts)

    assert [item[0].work_item_id for item in first] == [
        context.work_tree[0].work_item_id for context in contexts
    ]
    assert len({item[0].session_id for item in first}) == len(first)
    assert [item[0].session_id for item in second] == [
        item[0].session_id for item in first
    ]
    assert [item[0].turn for item in first] == [1] * len(first)
    assert [item[0].turn for item in second] == [2] * len(second)


def test_work_item_session_is_visible_in_completed_prompt() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures/framework/tob_announcement_observation_scope_expansion_v409.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = _observation_work_item_contexts(
        SolverContext.model_validate(fixture["solverContext"])
    )[0]
    session, projected = WorkItemSessionCoordinator().assign((context,))[0]
    profile = legal_agent_profile().solver_observation_integration
    assert profile is not None

    rendered = render_observation_integration_model_call(
        projected,
        profile,
        work_item_session=session,
    )

    assert rendered.input_payload["work_item_session"] == session.as_input()
    assert rendered.input_payload["work_items"] == [
        {
            "work_item_id": session.work_item_id,
            "question": projected.work_tree[0].question,
        }
    ]
    assert "このWorkItem専属" in rendered.instructions


def _two_work_item_context() -> SolverContext:
    state = CaseState(
        case_id="work-item-session-test",
        question="二つの事項を確認する。",
        work_items=(
            WorkItem(work_item_id="wi-1", question="事項1を確認する。"),
            WorkItem(work_item_id="wi-2", question="事項2を確認する。"),
        ),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h-1",
                work_item_id="wi-1",
                statement="事項1は本文に定められている。",
            ),
            Hypothesis(
                hypothesis_id="h-2",
                work_item_id="wi-2",
                statement="事項2は本文に定められている。",
            ),
        ),
        evidence=(
            Evidence(
                evidence_id="e-1",
                source_ref="fixture:e-1",
                content="事項1と事項2を定める本文。",
                created_cycle=1,
                metadata={"articleId": "article-1", "citationEligible": True},
            ),
        ),
        retained_evidence_ids=("e-1",),
    )
    return build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=120,
        finalize_only=False,
    )


def _observation_result(hypothesis_id: str) -> StructuredJSONResult:
    return StructuredJSONResult(
        payload={
            "decision_reason": f"{hypothesis_id}を確認した。",
            "update_hypotheses": [
                {
                    "hypothesis_id": hypothesis_id,
                    "judgment": "supported",
                    "evidence_ids": ["e-1"],
                    "gaps": [],
                }
            ],
        },
        provider="fake",
        model="fake-model",
        latencyMs=1,
        inputTokens=10,
        outputTokens=5,
    )


def test_observation_skips_model_when_tool_added_no_grounding_text() -> None:
    graph_request = ToolRequest(
        request_id="graph-1",
        work_item_id="wi-1",
        tool_name="legal_graph_neighbors",
        arguments={"article_ids": ["article-1"]},
        purpose="隣接候補を探す",
        hypothesis_ids=("h-1",),
    )
    state = CaseState(
        case_id="work-item-no-grounding-test",
        question="確認する。",
        research_cycle_count=1,
        work_items=(WorkItem(work_item_id="wi-1", question="事項を確認する。"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h-1",
                work_item_id="wi-1",
                statement="事項が定められている。",
            ),
        ),
        tool_requests=(graph_request,),
        tool_results=(
            ToolResult(
                request_id="graph-1",
                status="succeeded",
                evidence_ids=(),
                cycle_no=1,
            ),
        ),
    )
    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=120,
        finalize_only=False,
    )

    class UnexpectedModelCall:
        provider = "openai"

        def generate_structured_json(self, **kwargs: Any) -> StructuredJSONResult:
            raise AssertionError("grounding本文がなければModelを呼ばない")

    profile = legal_agent_profile().solver_observation_integration
    assert profile is not None
    result = StructuredJSONModelAdapter(UnexpectedModelCall()).solve(
        context,
        profile,
    )

    assert result.input_tokens == 0
    assert result.output_tokens == 0
    assert result.decision.next_focus_work_item_ids == ("wi-1",)
    assert result.decision.update.update_hypotheses == ()


def test_parallel_work_item_timeout_preserves_completed_session_delta() -> None:
    class PartialTimeoutClient:
        provider = "openai"

        def generate_structured_json(self, **kwargs: Any) -> StructuredJSONResult:
            if "wi-2" in kwargs["prompt"]:
                raise requests.ReadTimeout("wi-2 timed out")
            return _observation_result("h-1")

    profile = legal_agent_profile().solver_observation_integration
    assert profile is not None

    with pytest.raises(SolverCheckpointTimeout) as caught:
        StructuredJSONModelAdapter(PartialTimeoutClient()).solve(
            _two_work_item_context(),
            profile,
        )

    partial = caught.value.partial_decision
    assert partial is not None
    assert [
        item.hypothesis_id for item in partial.update.update_hypotheses
    ] == ["h-1"]
    assert [item.work_item_id for item in partial.update.update_work_items] == [
        "wi-1"
    ]
    assert partial.tool_requests == ()
    assert caught.value.completed_stage == "work_item_sessions_partial"


def test_parallel_work_item_results_merge_in_input_order() -> None:
    class OutOfOrderClient:
        provider = "openai"

        def generate_structured_json(self, **kwargs: Any) -> StructuredJSONResult:
            if "wi-1" in kwargs["prompt"]:
                sleep(0.02)
                return _observation_result("h-1")
            return _observation_result("h-2")

    profile = legal_agent_profile().solver_observation_integration
    assert profile is not None
    result = StructuredJSONModelAdapter(OutOfOrderClient()).solve(
        _two_work_item_context(),
        profile,
    )

    assert [
        item.hypothesis_id for item in result.decision.update.update_hypotheses
    ] == ["h-1", "h-2"]


def test_omitted_evidence_is_projected_by_tool_request_work_item() -> None:
    state = CaseState(
        case_id="work-item-omitted-scope-test",
        question="二つの事項を確認する。",
        work_items=(
            WorkItem(work_item_id="wi-1", question="事項1を確認する。"),
            WorkItem(work_item_id="wi-2", question="事項2を確認する。"),
        ),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h-1",
                work_item_id="wi-1",
                statement="事項1を確認する。",
            ),
            Hypothesis(
                hypothesis_id="h-2",
                work_item_id="wi-2",
                statement="事項2を確認する。",
            ),
        ),
        evidence=(
            Evidence(
                evidence_id="search-e-1",
                source_ref="fixture:search-e-1",
                content="事項1の検索抜粋。",
                created_cycle=1,
                metadata={"articleId": "article-1", "citationEligible": False},
            ),
            Evidence(
                evidence_id="search-e-2",
                source_ref="fixture:search-e-2",
                content="事項2の検索抜粋。",
                created_cycle=1,
                metadata={"articleId": "article-2", "citationEligible": False},
            ),
        ),
        tool_requests=(
            ToolRequest(
                request_id="r-1",
                work_item_id="wi-1",
                tool_name="legal_search",
                arguments={"query": "事項1"},
                purpose="事項1を探す",
                hypothesis_ids=("h-1",),
            ),
            ToolRequest(
                request_id="r-2",
                work_item_id="wi-2",
                tool_name="legal_search",
                arguments={"query": "事項2"},
                purpose="事項2を探す",
                hypothesis_ids=("h-2",),
            ),
        ),
        tool_results=(
            ToolResult(
                request_id="r-1",
                status="succeeded",
                evidence_ids=("search-e-1",),
                cycle_no=1,
            ),
            ToolResult(
                request_id="r-2",
                status="succeeded",
                evidence_ids=("search-e-2",),
                cycle_no=1,
            ),
        ),
        integrated_tool_result_request_ids=("r-1", "r-2"),
    )
    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=120,
        finalize_only=False,
    )
    projected = _observation_work_item_contexts(context)

    assert [item.omitted_evidence_ids for item in projected] == [(), ()]
    assert context.omitted_evidence_ids == ()
