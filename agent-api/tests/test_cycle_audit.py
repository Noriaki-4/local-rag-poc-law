from __future__ import annotations

from app.agent_framework.context import build_solver_context
from app.agent_framework.contracts import SolverDecision
from app.agent_framework.cycle_audit import (
    build_cycle_audit_report,
    build_cycle_checkpoint,
    compare_cycle_audit_reports,
    render_cycle_audit_comparison_markdown,
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
    diagnostics.record_run_complete(
        state=state_after,
        failure_code=None,
        elapsed_ms=800,
    )

    records = load_diagnostic_records(tmp_path, baseline.case_id)
    checkpoint = next(item for item in records if item["event"] == "cycle_checkpoint")
    assert checkpoint["modelMetrics"]["latencyMs"] == 125
    assert checkpoint["cycleSnapshot"]["hypotheses"][0]["gaps"] == [
        "具体的な条件"
    ]
    report = build_cycle_audit_report(records)
    assert report["cycleCount"] == 1
    assert report["findingCount"] == 2
    assert report["runMetrics"] == {
        "elapsedMs": 800,
        "modelCallCount": 1,
        "modelLatencyMs": 125,
        "inputTokens": 300,
        "outputTokens": 75,
            "toolCallCount": 1,
            "toolElapsedMs": 12,
            "workItemSessionCount": 0,
            "workItemSessionTimeoutCount": 0,
        }
    assert report["purposeMetrics"][0]["purpose"] == "cycle_close"
    assert isinstance(report["cycles"][0]["elapsedMs"], int)
    markdown = render_cycle_audit_markdown(report)
    assert "## Cycle 1" in markdown
    assert "## Run performance" in markdown
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


def test_report_flags_repeated_integration_structure() -> None:
    scope = {"workItemIds": ["w1"], "hypothesisIds": ["h1"]}
    records = (
        {
            "sequence": 1,
            "event": "solver_input",
            "purpose": "observation_integration",
            "contractAttempt": 1,
            "cycleNo": 1,
            "scope": scope,
        },
        {
            "sequence": 2,
            "event": "transport_input",
            "instructionsHash": "instructions",
            "inputHash": "input",
            "schemaHash": "schema",
            "transportStage": "solver",
        },
        {
            "sequence": 3,
            "event": "solver_output",
            "purpose": "observation_integration",
            "contractAttempt": 1,
            "cycleNo": 1,
            "scope": scope,
            "latencyMs": 100,
            "inputTokens": 20,
            "outputTokens": 5,
        },
        {
            "sequence": 4,
            "event": "decision_applied",
            "purpose": "observation_integration",
            "scope": scope,
            "stateAfterStatus": {"toolResultCount": 1},
        },
        {
            "sequence": 5,
            "event": "solver_input",
            "purpose": "observation_integration",
            "contractAttempt": 1,
            "cycleNo": 1,
            "scope": scope,
        },
        {
            "sequence": 6,
            "event": "transport_input",
            "instructionsHash": "instructions",
            "inputHash": "input",
            "schemaHash": "schema",
            "transportStage": "solver",
        },
        {
            "sequence": 7,
            "event": "solver_output",
            "purpose": "observation_integration",
            "contractAttempt": 1,
            "cycleNo": 1,
            "scope": scope,
            "latencyMs": 120,
            "inputTokens": 25,
            "outputTokens": 6,
        },
        {
            "sequence": 8,
            "event": "decision_applied",
            "purpose": "observation_integration",
            "scope": scope,
            "stateAfterStatus": {"toolResultCount": 1},
        },
        {
            "sequence": 9,
            "event": "decision_applied",
            "purpose": "integration",
            "scope": scope,
            "stateBeforeStatus": {"toolResultCount": 1},
        },
        {
            "sequence": 10,
            "event": "tool_execution",
            "cycleNo": 1,
            "requestId": "tool-1",
            "toolName": "legal_search",
            "arguments": {"query": "条件"},
            "hypothesisIds": ["h1"],
            "elapsedMs": 10,
        },
        {
            "sequence": 11,
            "event": "tool_execution",
            "cycleNo": 1,
            "requestId": "tool-2",
            "toolName": "legal_search",
            "arguments": {"query": "条件"},
            "hypothesisIds": ["h1"],
            "elapsedMs": 12,
        },
        {
            "sequence": 12,
            "event": "run_complete",
            "caseId": "case-1",
            "elapsedMs": 500,
            "stateStatus": {"runStatus": "completed"},
        },
    )

    report = build_cycle_audit_report(records)

    assert report["runMetrics"]["modelCallCount"] == 2
    assert report["runMetrics"]["toolCallCount"] == 2
    assert report["runMetrics"]["toolElapsedMs"] == 22
    assert report["hypothesisActivity"][0]["callCount"] == 2
    assert {item["code"] for item in report["executionFindings"]} == {
        "REPEATED_OBSERVATION_INTEGRATION_SCOPE",
        "ADJACENT_INTEGRATION_WITHOUT_NEW_TOOL_RESULT",
        "REPEATED_MODEL_INPUT",
        "REPEATED_TOOL_SCOPE",
    }


def test_cycle_audit_allows_observation_iteration_after_scoped_tool_result() -> None:
    scope = {"workItemIds": ["w1"], "hypothesisIds": ["h1"]}
    records = (
        {
            "sequence": 1,
            "event": "solver_input",
            "purpose": "observation_integration",
            "contractAttempt": 0,
            "cycleNo": 1,
            "scope": scope,
        },
        {
            "sequence": 2,
            "event": "solver_output",
            "purpose": "observation_integration",
            "contractAttempt": 0,
            "cycleNo": 1,
            "scope": scope,
            "latencyMs": 10,
        },
        {
            "sequence": 3,
            "event": "tool_execution",
            "cycleNo": 1,
            "requestId": "tool-1",
            "toolName": "fetch_articles",
            "arguments": {"article_ids": ["article-1"]},
            "hypothesisIds": ["h1"],
            "elapsedMs": 1,
        },
        {
            "sequence": 4,
            "event": "solver_input",
            "purpose": "observation_integration",
            "contractAttempt": 0,
            "cycleNo": 1,
            "scope": scope,
        },
        {
            "sequence": 5,
            "event": "solver_output",
            "purpose": "observation_integration",
            "contractAttempt": 0,
            "cycleNo": 1,
            "scope": scope,
            "latencyMs": 10,
        },
        {
            "sequence": 6,
            "event": "run_complete",
            "caseId": "case-1",
            "elapsedMs": 25,
            "stateStatus": {"runStatus": "completed"},
        },
    )

    report = build_cycle_audit_report(records)

    assert "REPEATED_OBSERVATION_INTEGRATION_SCOPE" not in {
        item["code"] for item in report["executionFindings"]
    }


def test_cycle_audit_does_not_report_contract_repair_as_repeated_integration(
) -> None:
    scope = {"workItemIds": ["w1"], "hypothesisIds": ["h1"]}
    records = (
        {
            "sequence": 1,
            "event": "solver_output",
            "purpose": "observation_integration",
            "contractAttempt": 1,
            "cycleNo": 1,
            "scope": scope,
            "latencyMs": 10,
        },
        {
            "sequence": 2,
            "event": "contract_violation",
            "purpose": "observation_integration",
            "contractAttempt": 1,
        },
        {
            "sequence": 3,
            "event": "solver_output",
            "purpose": "observation_integration",
            "contractAttempt": 2,
            "cycleNo": 1,
            "scope": scope,
            "latencyMs": 10,
        },
    )

    report = build_cycle_audit_report(records)

    assert "REPEATED_OBSERVATION_INTEGRATION_SCOPE" not in {
        item["code"] for item in report["executionFindings"]
    }
    assert "CONTRACT_VIOLATION" in {
        item["code"] for item in report["executionFindings"]
    }


def test_cycle_audit_reports_tools_per_work_item_without_cross_scope_duplicates(
) -> None:
    records = (
        {
            "sequence": 1,
            "event": "tool_execution",
            "cycleNo": 1,
            "requestId": "fetch-w1",
            "workItemId": "w1",
            "toolName": "fetch_articles",
            "arguments": {"article_ids": ["article-1", "article-2"]},
            "hypothesisIds": ["h1"],
            "elapsedMs": 10,
        },
        {
            "sequence": 2,
            "event": "tool_execution",
            "cycleNo": 1,
            "requestId": "fetch-w2",
            "workItemId": "w2",
            "toolName": "fetch_articles",
            "arguments": {"article_ids": ["article-1", "article-2"]},
            "hypothesisIds": ["h1"],
            "elapsedMs": 12,
        },
        {
            "sequence": 3,
            "event": "tool_execution",
            "cycleNo": 1,
            "requestId": "graph-w2",
            "workItemId": "w2",
            "toolName": "legal_graph_neighbors",
            "arguments": {"article_ids": ["article-2"]},
            "hypothesisIds": ["h1"],
            "elapsedMs": 5,
        },
    )

    report = build_cycle_audit_report(records)

    assert "REPEATED_TOOL_SCOPE" not in {
        item["code"] for item in report["executionFindings"]
    }
    assert report["workItemToolActivity"] == [
        {
            "workItemId": "w1",
            "callCount": 1,
            "searchCallCount": 0,
            "graphCallCount": 0,
            "fetchCallCount": 1,
            "fetchedArticleIds": ["article-1", "article-2"],
            "elapsedMs": 10,
        },
        {
            "workItemId": "w2",
            "callCount": 2,
            "searchCallCount": 0,
            "graphCallCount": 1,
            "fetchCallCount": 1,
            "fetchedArticleIds": ["article-1", "article-2"],
            "elapsedMs": 17,
        },
    ]
    markdown = render_cycle_audit_markdown(report)
    assert "## Tools by WorkItem" in markdown
    assert "| `w1` | 1 | 0 | 0 | 1 | 2 | 10 |" in markdown


def test_cycle_audit_allows_followup_integration_with_additional_scope() -> None:
    records = (
        {
            "sequence": 1,
            "event": "decision_applied",
            "purpose": "observation_integration",
            "scope": {"workItemIds": ["w1"], "hypothesisIds": ["h1"]},
            "stateAfterStatus": {"toolResultCount": 1},
        },
        {
            "sequence": 2,
            "event": "decision_applied",
            "purpose": "integration",
            "scope": {
                "workItemIds": ["w1", "w2"],
                "hypothesisIds": ["h1", "h2"],
            },
            "stateBeforeStatus": {"toolResultCount": 1},
        },
    )

    report = build_cycle_audit_report(records)

    assert "ADJACENT_INTEGRATION_WITHOUT_NEW_TOOL_RESULT" not in {
        item["code"] for item in report["executionFindings"]
    }


def test_cycle_audit_allows_integration_for_a_different_hypothesis() -> None:
    records = (
        {
            "sequence": 1,
            "event": "decision_applied",
            "purpose": "observation_integration",
            "scope": {"workItemIds": ["w1"], "hypothesisIds": ["h1"]},
            "stateAfterStatus": {"toolResultCount": 1},
        },
        {
            "sequence": 2,
            "event": "decision_applied",
            "purpose": "integration",
            "scope": {"workItemIds": ["w2"], "hypothesisIds": ["h2"]},
            "stateBeforeStatus": {"toolResultCount": 1},
        },
    )

    report = build_cycle_audit_report(records)

    assert report["executionFindings"] == []


def test_cycle_audit_tracks_work_item_sessions_by_id_and_turn() -> None:
    records = (
        {
            "sequence": 1,
            "event": "transport_input",
            "transportStage": "observation_integration",
            "transportAttempt": 1,
            "cycleNo": 5,
            "scope": {"workItemIds": ["w1"], "hypothesisIds": ["h1"]},
            "workItemSessionId": "wi-session-1",
            "workItemSessionTurn": 1,
            "promptChars": 100,
            "schemaChars": 20,
            "completeRequestPath": "/tmp/turn-1/complete_request.json",
        },
        {
            "sequence": 2,
            "event": "transport_input",
            "transportStage": "observation_integration",
            "transportAttempt": 1,
            "cycleNo": 5,
            "scope": {"workItemIds": ["w1"], "hypothesisIds": ["h1"]},
            "workItemSessionId": "wi-session-1",
            "workItemSessionTurn": 2,
            "promptChars": 110,
            "schemaChars": 20,
            "completeRequestPath": "/tmp/turn-2/complete_request.json",
        },
        {
            "sequence": 3,
            "event": "transport_output",
            "transportStage": "observation_integration",
            "transportAttempt": 1,
            "cycleNo": 5,
            "scope": {"workItemIds": ["w1"], "hypothesisIds": ["h1"]},
            "workItemSessionId": "wi-session-1",
            "workItemSessionTurn": 2,
            "latencyMs": 20,
            "inputTokens": 12,
            "outputTokens": 4,
        },
        {
            "sequence": 4,
            "event": "transport_output",
            "transportStage": "observation_integration",
            "transportAttempt": 1,
            "cycleNo": 5,
            "scope": {"workItemIds": ["w1"], "hypothesisIds": ["h1"]},
            "workItemSessionId": "wi-session-1",
            "workItemSessionTurn": 1,
            "latencyMs": 10,
            "inputTokens": 8,
            "outputTokens": 2,
        },
        {
            "sequence": 5,
            "event": "run_complete",
            "caseId": "case-session-audit",
            "elapsedMs": 40,
            "stateStatus": {"runStatus": "completed"},
        },
    )

    report = build_cycle_audit_report(records)

    assert report["runMetrics"]["workItemSessionCount"] == 1
    assert report["runMetrics"]["workItemSessionTimeoutCount"] == 0
    session = report["workItemSessionActivity"][0]
    assert session["sessionId"] == "wi-session-1"
    assert session["workItemIds"] == ["w1"]
    assert session["cycleNos"] == [5]
    assert [item["turn"] for item in session["turns"]] == [1, 2]
    assert session["latencyMs"] == 30
    assert session["inputTokens"] == 20
    assert session["outputTokens"] == 6
    markdown = render_cycle_audit_markdown(report)
    assert "## WorkItem sessions" in markdown
    assert "wi-session-1" in markdown


def test_cycle_audit_comparison_reports_run_and_purpose_deltas() -> None:
    baseline = {
        "caseId": "before",
        "runMetrics": {
            "elapsedMs": 1000,
            "modelCallCount": 2,
            "modelLatencyMs": 800,
            "inputTokens": 100,
            "outputTokens": 20,
            "toolCallCount": 1,
            "toolElapsedMs": 10,
        },
        "purposeMetrics": [
            {
                "purpose": "integration",
                "callCount": 2,
                "latencyMs": 800,
                "inputTokens": 100,
                "outputTokens": 20,
            }
        ],
    }
    current = {
        "caseId": "after",
        "runMetrics": {
            "elapsedMs": 1200,
            "modelCallCount": 3,
            "modelLatencyMs": 900,
            "inputTokens": 130,
            "outputTokens": 25,
            "toolCallCount": 1,
            "toolElapsedMs": 8,
        },
        "purposeMetrics": [
            {
                "purpose": "integration",
                "callCount": 3,
                "latencyMs": 900,
                "inputTokens": 130,
                "outputTokens": 25,
            }
        ],
    }

    comparison = compare_cycle_audit_reports(baseline, current)

    assert comparison["metrics"]["elapsedMs"]["delta"] == 200
    assert comparison["purposes"][0]["callCount"]["delta"] == 1
    markdown = render_cycle_audit_comparison_markdown(comparison)
    assert "Agent Diagnostic Comparison" in markdown
    assert "| elapsedMs | 1000 | 1200 | 200 |" in markdown
