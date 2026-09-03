from __future__ import annotations

import pytest

from app.adapters.models.structured_json import (
    _project_available_tools,
    _tool_requests_transport_schema,
    render_observation_integration_model_call,
)
from app.agent_framework.context import build_solver_context
from app.agent_framework.contracts import SolverDecision
from app.agent_framework.loop import _all_open_hypothesis_exploration_exhausted
from app.agent_framework.profiles import AgentLimits, ModelCallProfile
from app.agent_framework.state import (
    CaseState,
    Evidence,
    Hypothesis,
    ToolRequest,
    ToolResult,
    WorkItem,
)
from app.agent_framework.tool_contracts import ToolDefinition
from app.agent_framework.validation import ActionRejected, apply_solver_decision


def _search_request(request_id: str, hypothesis_id: str = "h-1") -> ToolRequest:
    return ToolRequest(
        request_id=request_id,
        work_item_id="wi-1",
        tool_name="legal_search",
        arguments={"query": "制度 要件", "doc_types": ["law"]},
        purpose="要件を探す",
        hypothesis_ids=(hypothesis_id,),
    )


def _graph_request(request_id: str) -> ToolRequest:
    return ToolRequest(
        request_id=request_id,
        work_item_id="wi-1",
        tool_name="legal_graph_neighbors",
        arguments={
            "article_ids": ["law-a-article-1"],
            "mode": "semantic_assertion",
            "predicate": "IMPLEMENTS",
            "direction": "from_subject",
            "max_relations": 20,
        },
        purpose="具体化規定を探す",
        hypothesis_ids=("h-1",),
    )


def _state(*, cycle_no: int = 1) -> CaseState:
    search = _search_request("search-1")
    return CaseState(
        case_id="exploration-case",
        question="制度の要件は何か",
        research_cycle_count=cycle_no,
        work_items=(WorkItem(work_item_id="wi-1", question="要件を確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h-1",
                work_item_id="wi-1",
                statement="制度には要件がある",
            ),
        ),
        evidence=(
            Evidence(
                evidence_id="e-article",
                source_ref="law-a-article-1",
                content="条文",
                metadata={"articleId": "law-a-article-1"},
                created_cycle=1,
            ),
        ),
        tool_requests=(search,),
        tool_results=(
            ToolResult(request_id=search.request_id, status="succeeded", cycle_no=1),
        ),
    )


def _tool_definition(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"{name}を実行する。",
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
        },
        result_description="候補を返す。",
    )


def test_context_reports_one_open_search_as_one_exploration_set() -> None:
    context = build_solver_context(
        _state(),
        AgentLimits(max_exploration_sets_per_hypothesis=1),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )

    status = context.hypothesis_exploration_sets[0]
    assert status.legal_search_used_in_cycle is True
    assert status.graph_used_in_cycle is False
    assert status.used_sets_total == 1
    assert status.remaining_new_sets_total == 0


def test_graph_can_complete_the_current_set_after_open_search() -> None:
    updated = apply_solver_decision(
        _state(),
        SolverDecision(next="continue", tool_requests=(_graph_request("graph-1"),)),
        limits=AgentLimits(max_exploration_sets_per_hypothesis=1),
        known_tool_names={"legal_search", "legal_graph_neighbors"},
        material_evidence_ids=(),
        finalize_only=False,
    )
    assert updated.tool_requests[-1].tool_name == "legal_graph_neighbors"


def test_current_set_is_exhausted_only_after_both_exploration_tools() -> None:
    search_only = build_solver_context(
        _state(),
        AgentLimits(max_exploration_sets_per_hypothesis=1),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    assert _all_open_hypothesis_exploration_exhausted(search_only) is False

    graph = _graph_request("graph-1")
    state = _state().model_copy(
        update={
            "tool_requests": (*_state().tool_requests, graph),
            "tool_results": (
                *_state().tool_results,
                ToolResult(
                    request_id=graph.request_id,
                    status="succeeded",
                    cycle_no=1,
                ),
            ),
        }
    )
    completed_set = build_solver_context(
        state,
        AgentLimits(max_exploration_sets_per_hypothesis=1),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    assert _all_open_hypothesis_exploration_exhausted(completed_set) is True


def test_used_exploration_tool_is_removed_from_current_cycle_contract() -> None:
    context = build_solver_context(
        _state(),
        AgentLimits(max_exploration_sets_per_hypothesis=2),
        remaining_wall_time_sec=60,
        finalize_only=False,
        available_tools=(
            _tool_definition("legal_search"),
            _tool_definition("legal_graph_neighbors"),
        ),
    )

    projected = _project_available_tools(context, None)

    assert [item.name for item in projected.available_tools] == [
        "legal_graph_neighbors"
    ]
    schema = _tool_requests_transport_schema(projected)
    assert schema["items"]["properties"]["tool_name"]["enum"] == [
        "legal_graph_neighbors"
    ]
    assert schema["items"]["properties"]["hypothesis_ids"]["items"][
        "enum"
    ] == ["h-1"]

    rendered = render_observation_integration_model_call(
        context,
        ModelCallProfile(
            model="test-model",
            system_prompt="取得本文を評価する。",
            context_projection="observation_integration",
        ),
    )
    assert [item["name"] for item in rendered.input_payload["available_tools"]] == [
        "legal_graph_neighbors"
    ]
    anthropic_rendered = render_observation_integration_model_call(
        context,
        ModelCallProfile(
            model="test-model",
            system_prompt="取得本文を評価する。",
            context_projection="observation_integration",
        ),
        provider="anthropic",
    )
    assert anthropic_rendered.input_payload["available_tools"] == (
        rendered.input_payload["available_tools"]
    )


def test_observation_integration_hides_all_tools_when_cycle_close_is_required() -> None:
    context = build_solver_context(
        _state(),
        AgentLimits(max_exploration_sets_per_hypothesis=2),
        remaining_wall_time_sec=60,
        finalize_only=False,
        available_tools=(
            _tool_definition("legal_search"),
            _tool_definition("fetch_articles"),
            _tool_definition("legal_graph_neighbors"),
        ),
    ).model_copy(update={"cycle_close_required": True})

    rendered = render_observation_integration_model_call(
        context,
        ModelCallProfile(
            model="test-model",
            system_prompt="取得本文を評価する。",
            context_projection="observation_integration",
        ),
    )

    assert rendered.input_payload["available_tools"] == []
    assert rendered.output_schema["properties"]["tool_requests"]["maxItems"] == 0


def test_exploration_schema_limits_tool_to_hypotheses_with_cycle_capacity() -> None:
    state = _state().model_copy(
        update={
            "hypotheses": (
                *_state().hypotheses,
                Hypothesis(
                    hypothesis_id="h-2",
                    work_item_id="wi-1",
                    statement="別の規律も確認する",
                ),
            )
        }
    )
    context = build_solver_context(
        state,
        AgentLimits(max_exploration_sets_per_hypothesis=1),
        remaining_wall_time_sec=60,
        finalize_only=False,
        available_tools=(_tool_definition("legal_search"),),
    )

    projected = _project_available_tools(context, None)
    schema = _tool_requests_transport_schema(projected)

    assert [item.name for item in projected.available_tools] == ["legal_search"]
    assert schema["items"]["properties"]["hypothesis_ids"]["items"][
        "enum"
    ] == ["h-2"]


def test_future_set_does_not_reenable_tools_used_in_current_cycle() -> None:
    graph = _graph_request("graph-1")
    initial = _state()
    state = initial.model_copy(
        update={
            "tool_requests": (*initial.tool_requests, graph),
            "tool_results": (
                *initial.tool_results,
                ToolResult(
                    request_id=graph.request_id,
                    status="succeeded",
                    cycle_no=1,
                ),
            ),
        }
    )
    context = build_solver_context(
        state,
        AgentLimits(max_exploration_sets_per_hypothesis=2),
        remaining_wall_time_sec=60,
        finalize_only=False,
        available_tools=(
            _tool_definition("legal_search"),
            _tool_definition("legal_graph_neighbors"),
        ),
    )

    projected = _project_available_tools(context, None)

    assert context.hypothesis_exploration_sets[0].remaining_new_sets_total == 1
    assert projected.available_tools == ()
    assert _tool_requests_transport_schema(projected)["maxItems"] == 0


def test_future_set_becomes_available_after_starting_next_cycle() -> None:
    context = build_solver_context(
        _state(cycle_no=2),
        AgentLimits(max_exploration_sets_per_hypothesis=2),
        remaining_wall_time_sec=60,
        finalize_only=False,
        available_tools=(
            _tool_definition("legal_search"),
            _tool_definition("legal_graph_neighbors"),
        ),
    )

    projected = _project_available_tools(context, None)

    assert context.hypothesis_exploration_sets[0].remaining_new_sets_total == 1
    assert [item.name for item in projected.available_tools] == [
        "legal_search",
        "legal_graph_neighbors",
    ]


def test_another_active_hypothesis_with_capacity_keeps_research_open() -> None:
    graph = _graph_request("graph-1")
    state = _state().model_copy(
        update={
            "hypotheses": (
                *_state().hypotheses,
                Hypothesis(
                    hypothesis_id="h-2",
                    work_item_id="wi-1",
                    statement="別の規律も確認する",
                ),
            ),
            "tool_requests": (*_state().tool_requests, graph),
            "tool_results": (
                *_state().tool_results,
                ToolResult(
                    request_id=graph.request_id,
                    status="succeeded",
                    cycle_no=1,
                ),
            ),
        }
    )
    context = build_solver_context(
        state,
        AgentLimits(max_exploration_sets_per_hypothesis=1),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )

    assert _all_open_hypothesis_exploration_exhausted(context) is False


def test_same_exploration_tool_cannot_run_twice_in_one_cycle() -> None:
    with pytest.raises(ActionRejected, match="successful legal_search scope"):
        apply_solver_decision(
            _state(),
            SolverDecision(next="continue", tool_requests=(_search_request("search-2"),)),
            limits=AgentLimits(max_exploration_sets_per_hypothesis=1),
            known_tool_names={"legal_search"},
            material_evidence_ids=(),
            finalize_only=False,
        )


def test_new_cycle_respects_total_set_limit_and_configured_second_set() -> None:
    state = _state(cycle_no=2)
    next_search = _search_request("search-2").model_copy(
        update={"arguments": {"query": "制度 例外", "doc_types": ["law"]}}
    )
    decision = SolverDecision(next="continue", tool_requests=(next_search,))

    with pytest.raises(ActionRejected, match="exploration-set limit"):
        apply_solver_decision(
            state,
            decision,
            limits=AgentLimits(max_exploration_sets_per_hypothesis=1),
            known_tool_names={"legal_search"},
            material_evidence_ids=(),
            finalize_only=False,
        )

    updated = apply_solver_decision(
        state,
        decision,
        limits=AgentLimits(max_exploration_sets_per_hypothesis=2),
        known_tool_names={"legal_search"},
        material_evidence_ids=(),
        finalize_only=False,
    )
    assert updated.tool_requests[-1].request_id == "search-2"


def test_completed_scope_is_duplicate_even_with_another_hypothesis_id() -> None:
    state = _state().model_copy(
        update={
            "hypotheses": (
                *_state().hypotheses,
                Hypothesis(
                    hypothesis_id="h-2",
                    work_item_id="wi-1",
                    statement="別の命題",
                ),
            )
        }
    )
    duplicate = _search_request("search-2", "h-2")

    with pytest.raises(ActionRejected, match="successful legal_search scope"):
        apply_solver_decision(
            state,
            SolverDecision(next="continue", tool_requests=(duplicate,)),
            limits=AgentLimits(),
            known_tool_names={"legal_search"},
            material_evidence_ids=(),
            finalize_only=False,
        )
