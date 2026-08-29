from __future__ import annotations

from app.agent_framework.context import build_solver_context
from app.agent_framework.contracts import SolverDecision
from app.agent_framework.cycle_audit import (
    build_cycle_audit_report,
    build_cycle_checkpoint,
    render_cycle_audit_markdown,
)
from app.agent_framework.diagnostics import AgentDiagnostics, load_diagnostic_records
from app.agent_framework.profiles import AgentLimits, ModelCallProfile
from app.agent_framework.state import (
    CaseState,
    Evidence,
    Hypothesis,
    ToolRequest,
    ToolResult,
    WorkItem,
)


def _baseline() -> CaseState:
    return CaseState(
        case_id="case-cycle-audit",
        question="下位規範を確認してください",
        research_cycle_count=1,
        work_items=(WorkItem(work_item_id="w1", question="条件を確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="下位規範が条件を定める",
                gaps=("具体的な条件",),
            ),
        ),
    )


def _graph_state(*, include_inverse: bool = False) -> CaseState:
    baseline = _baseline()
    requests = [
        ToolRequest(
            request_id="graph-1",
            work_item_id="w1",
            tool_name="legal_graph_neighbors",
            arguments={
                "article_ids": ["law-a-article-1"],
                "mode": "semantic_assertion",
                "predicate": "IMPLEMENTS",
                "direction": "to_subject",
                "max_relations": 20,
            },
            purpose="具体化規定を確認する",
            hypothesis_ids=("h1",),
        )
    ]
    results = [
        ToolResult(
            request_id="graph-1",
            status="succeeded",
            cycle_no=1,
            elapsed_ms=12,
        )
    ]
    if include_inverse:
        requests.append(
            ToolRequest(
                request_id="graph-2",
                work_item_id="w1",
                tool_name="legal_graph_neighbors",
                arguments={
                    "article_ids": ["law-a-article-1"],
                    "mode": "semantic_assertion",
                    "predicate": "IMPLEMENTS",
                    "direction": "from_subject",
                    "max_relations": 20,
                },
                purpose="逆方向を確認する",
                hypothesis_ids=("h1",),
            )
        )
        results.append(
            ToolResult(
                request_id="graph-2",
                status="succeeded",
                cycle_no=1,
                elapsed_ms=8,
            )
        )
    return baseline.model_copy(
        update={"tool_requests": tuple(requests), "tool_results": tuple(results)}
    )


def _next_cycle_decision() -> SolverDecision:
    return SolverDecision(
        next="continue",
        start_next_cycle=True,
        decision_reason="別の探索方針を試す",
    )


def test_cycle_checkpoint_flags_empty_graph_without_inverse_attempt() -> None:
    checkpoint = build_cycle_checkpoint(
        baseline=_baseline(),
        state_after=_graph_state(),
        decision=_next_cycle_decision(),
        purpose="cycle_close",
        start_sequence=1,
        decision_sequence=4,
        model_metrics={
            "callCount": 2,
            "latencyMs": 120,
            "inputTokens": 300,
            "outputTokens": 80,
        },
    )

    assert {item["code"] for item in checkpoint["findings"]} == {
        "GRAPH_EMPTY_INVERSE_UNTRIED",
        "CYCLE_NO_PROGRESS",
    }
    assert checkpoint["toolMetrics"] == {"callCount": 1, "elapsedMs": 12}


def test_cycle_checkpoint_does_not_call_inverse_direction_wrong_when_tried() -> None:
    checkpoint = build_cycle_checkpoint(
        baseline=_baseline(),
        state_after=_graph_state(include_inverse=True),
        decision=_next_cycle_decision(),
        purpose="cycle_close",
        start_sequence=1,
        decision_sequence=5,
        model_metrics={},
    )

    assert "GRAPH_EMPTY_INVERSE_UNTRIED" not in {
        item["code"] for item in checkpoint["findings"]
    }


def test_cycle_checkpoint_flags_fetched_evidence_not_integrated_or_mapped() -> None:
    baseline = _baseline()
    request = ToolRequest(
        request_id="fetch-1",
        work_item_id="w1",
        tool_name="fetch_articles",
        arguments={"article_ids": ["law-a-article-2"]},
        purpose="条件本文を確認する",
        hypothesis_ids=("h1",),
    )
    evidence = Evidence(
        evidence_id="e1",
        source_ref="law-a-article-2",
        content="本文",
        created_cycle=1,
        metadata={"articleId": "law-a-article-2", "evidenceRole": "grounding"},
    )
    state_after = baseline.model_copy(
        update={
            "tool_requests": (request,),
            "tool_results": (
                ToolResult(
                    request_id="fetch-1",
                    status="succeeded",
                    evidence_ids=("e1",),
                    cycle_no=1,
                ),
            ),
            "evidence": (evidence,),
        }
    )

    checkpoint = build_cycle_checkpoint(
        baseline=baseline,
        state_after=state_after,
        decision=_next_cycle_decision(),
        purpose="cycle_close",
        start_sequence=1,
        decision_sequence=3,
        model_metrics={},
    )

    assert {item["code"] for item in checkpoint["findings"]} == {
        "FETCH_RESULT_NOT_INTEGRATED",
        "FETCH_RESULT_UNMAPPED",
    }


def test_snapshot_diagnostics_emits_cycle_checkpoint_and_report(tmp_path) -> None:
    baseline = _baseline()
    state_after = _graph_state()
    context = build_solver_context(
        baseline,
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    diagnostics = AgentDiagnostics(
        mode="snapshot",
        output_dir=tmp_path,
        case_id=baseline.case_id,
    )
    profile = ModelCallProfile(
        model="test-model",
        max_output_tokens=256,
        timeout_sec=30,
        system_prompt="test",
    )
    decision = _next_cycle_decision()
    diagnostics.record_solver_input(
        state=baseline,
        context=context,
        profile=profile,
        purpose="research",
        contract_attempt=0,
    )
    diagnostics.record_solver_output(
        state=baseline,
        purpose="cycle_close",
        contract_attempt=0,
        decision=decision,
        latency_ms=125,
        input_tokens=300,
        output_tokens=75,
    )
    diagnostics.record_decision_applied(
        state_before=baseline,
        state_after=state_after,
        context=context,
        purpose="cycle_close",
        contract_attempt=0,
        decision=decision,
    )
    diagnostics.record_run_complete(state=state_after, failure_code=None)

    records = load_diagnostic_records(tmp_path, baseline.case_id)
    checkpoint = next(item for item in records if item["event"] == "cycle_checkpoint")
    assert checkpoint["modelMetrics"]["latencyMs"] == 125
    assert checkpoint["cycleSnapshot"]["hypotheses"][0]["gaps"] == [
        "具体的な条件"
    ]
    report = build_cycle_audit_report(records)
    assert report["cycleCount"] == 1
    assert report["findingCount"] == 2
    markdown = render_cycle_audit_markdown(report)
    assert "## Cycle 1" in markdown
    assert "GRAPH_EMPTY_INVERSE_UNTRIED" in markdown


def test_status_diagnostics_keeps_cycle_summary_without_snapshot(tmp_path) -> None:
    baseline = _baseline()
    state_after = _graph_state()
    context = build_solver_context(
        baseline,
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    diagnostics = AgentDiagnostics(
        mode="status",
        output_dir=tmp_path,
        case_id=baseline.case_id,
    )
    decision = _next_cycle_decision()
    diagnostics.record_solver_input(
        state=baseline,
        context=context,
        profile=ModelCallProfile(
            model="test-model",
            max_output_tokens=256,
            timeout_sec=30,
            system_prompt="test",
        ),
        purpose="research",
        contract_attempt=0,
    )
    diagnostics.record_decision_applied(
        state_before=baseline,
        state_after=state_after,
        context=context,
        purpose="cycle_close",
        contract_attempt=0,
        decision=decision,
    )

    records = load_diagnostic_records(tmp_path, baseline.case_id)
    checkpoint = next(item for item in records if item["event"] == "cycle_checkpoint")
    assert "cycleSnapshot" not in checkpoint
    assert checkpoint["findingCodes"] == [
        "GRAPH_EMPTY_INVERSE_UNTRIED",
        "CYCLE_NO_PROGRESS",
    ]
