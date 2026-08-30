"""Research Cycleの経路を、保存済み状態から決定的に監査する。"""

from __future__ import annotations

import json
from typing import Any, Iterable

from .contracts import SolverDecision
from .state import CaseState, Evidence, Hypothesis, ToolRequest, ToolResult, WorkItem


def cycle_number(state: CaseState) -> int:
    """初回の未採番状態をCycle 1として表示する。"""

    return max(1, state.research_cycle_count)


def build_cycle_checkpoint(
    *,
    baseline: CaseState,
    state_after: CaseState,
    decision: SolverDecision,
    purpose: str,
    start_sequence: int | None,
    decision_sequence: int,
    model_metrics: dict[str, int],
) -> dict[str, Any]:
    """Cycle開始時と終了時の正本から、監査用の差分を作る。"""

    cycle_no = cycle_number(baseline)
    requests = {item.request_id: item for item in state_after.tool_requests}
    cycle_results = tuple(
        item for item in state_after.tool_results if item.cycle_no == cycle_no
    )
    new_evidence = _new_evidence(baseline, state_after)
    work_item_changes = _model_changes(
        baseline.work_items,
        state_after.work_items,
        "work_item_id",
    )
    hypothesis_changes = _model_changes(
        baseline.hypotheses,
        state_after.hypotheses,
        "hypothesis_id",
    )
    tool_executions = [
        _tool_execution(requests.get(result.request_id), result)
        for result in cycle_results
    ]
    findings = _structural_findings(
        baseline=baseline,
        state_after=state_after,
        decision=decision,
        cycle_results=cycle_results,
        requests=requests,
    )
    return {
        "caseId": state_after.case_id,
        "cycleNo": cycle_no,
        "startSequence": start_sequence,
        "decisionSequence": decision_sequence,
        "purpose": purpose,
        "transition": {
            "next": decision.next,
            "startNextCycle": decision.start_next_cycle,
            "hasAnswer": decision.answer is not None,
        },
        "decisionReason": decision.decision_reason,
        "workItems": [_work_item(item) for item in state_after.work_items],
        "workItemChanges": work_item_changes,
        "hypotheses": [_hypothesis(item) for item in state_after.hypotheses],
        "hypothesisChanges": hypothesis_changes,
        "newEvidence": [_evidence_summary(item) for item in new_evidence],
        "toolExecutions": tool_executions,
        "dependencyDecisions": [
            item.model_dump(mode="json")
            for item in state_after.dependency_decisions
            if _decision_belongs_to_cycle(item.model_dump(mode="json"), cycle_no)
        ],
        "unresolvedHypotheses": [
            _hypothesis(item)
            for item in state_after.hypotheses
            if item.requires_follow_up
        ],
        "findings": findings,
        "modelMetrics": dict(model_metrics),
        "toolMetrics": {
            "callCount": len(cycle_results),
            "elapsedMs": sum(item.elapsed_ms for item in cycle_results),
        },
    }


def build_cycle_audit_report(
    records: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """診断JSONLのcycle_checkpointを人・機械共用の小さい報告へまとめる。"""

    record_list = tuple(records)
    checkpoints = [
        item for item in record_list if item.get("event") == "cycle_checkpoint"
    ]
    records_by_sequence = {
        item.get("sequence"): item
        for item in record_list
        if isinstance(item.get("sequence"), int)
    }
    cycles: list[dict[str, Any]] = []
    for record in checkpoints:
        snapshot = record.get("cycleSnapshot")
        if isinstance(snapshot, dict):
            cycle = dict(snapshot)
            cycle["elapsedMs"] = _cycle_elapsed_ms(record, records_by_sequence)
            cycles.append(cycle)
            continue
        cycles.append(
            {
                "cycleNo": record.get("cycleNo"),
                "startSequence": record.get("startSequence"),
                "decisionSequence": record.get("decisionSequence"),
                "purpose": record.get("purpose"),
                "transition": record.get("transition", {}),
                "decisionReason": record.get("decisionReason"),
                "findings": record.get("findings", []),
                "modelMetrics": record.get("modelMetrics", {}),
                "toolMetrics": record.get("toolMetrics", {}),
                "elapsedMs": _cycle_elapsed_ms(record, records_by_sequence),
            }
        )
    findings = [
        {"cycleNo": cycle.get("cycleNo"), **finding}
        for cycle in cycles
        for finding in cycle.get("findings", [])
    ]
    run_complete = next(
        (item for item in reversed(record_list) if item.get("event") == "run_complete"),
        None,
    )
    model_calls = _model_call_records(record_list)
    purpose_metrics = _purpose_metrics(model_calls)
    hypothesis_activity = _hypothesis_activity(model_calls)
    work_item_session_activity = _work_item_session_activity(record_list)
    execution_findings = [
        *_execution_findings(record_list, model_calls),
        *_work_item_session_findings(work_item_session_activity),
    ]
    tool_records = [
        item for item in record_list if item.get("event") == "tool_execution"
    ]
    tool_metrics = (
        {
            "callCount": len(tool_records),
            "elapsedMs": sum(
                int(item.get("elapsedMs") or 0) for item in tool_records
            ),
        }
        if tool_records
        else {
            "callCount": sum(
                int((cycle.get("toolMetrics") or {}).get("callCount", 0))
                for cycle in cycles
            ),
            "elapsedMs": sum(
                int((cycle.get("toolMetrics") or {}).get("elapsedMs", 0))
                for cycle in cycles
            ),
        }
    )
    run_metrics = {
        "elapsedMs": (
            run_complete.get("elapsedMs")
            if isinstance(run_complete, dict)
            else None
        ),
        "modelCallCount": len(model_calls),
        "modelLatencyMs": sum(
            int(item.get("latencyMs") or 0) for item in model_calls
        ),
        "inputTokens": sum(
            int(item.get("inputTokens") or 0) for item in model_calls
        ),
        "outputTokens": sum(
            int(item.get("outputTokens") or 0) for item in model_calls
        ),
        "toolCallCount": tool_metrics["callCount"],
        "toolElapsedMs": tool_metrics["elapsedMs"],
        "workItemSessionCount": len(work_item_session_activity),
        "workItemSessionTimeoutCount": sum(
            int(item["timeoutCount"]) for item in work_item_session_activity
        ),
    }
    return {
        "caseId": (
            run_complete.get("caseId")
            if isinstance(run_complete, dict)
            else (cycles[0].get("caseId") if cycles else None)
        ),
        "cycleCount": len(cycles),
        "findingCount": len(findings),
        "executionFindingCount": len(execution_findings),
        "finalRunStatus": (
            (run_complete.get("stateStatus") or {}).get("runStatus")
            if isinstance(run_complete, dict)
            else None
        ),
        "finalFailureCode": (
            run_complete.get("failureCode")
            if isinstance(run_complete, dict)
            else None
        ),
        "cycles": cycles,
        "findings": findings,
        "executionFindings": execution_findings,
        "runMetrics": run_metrics,
        "purposeMetrics": purpose_metrics,
        "hypothesisActivity": hypothesis_activity,
        "workItemSessionActivity": work_item_session_activity,
    }


def compare_cycle_audit_reports(
    baseline: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    """同じ評価条件の前後差を、意味評価せず実行指標だけで比較する。"""

    metric_names = (
        "elapsedMs",
        "modelCallCount",
        "modelLatencyMs",
        "inputTokens",
        "outputTokens",
        "toolCallCount",
        "toolElapsedMs",
        "workItemSessionCount",
        "workItemSessionTimeoutCount",
    )
    baseline_metrics = baseline.get("runMetrics", {})
    current_metrics = current.get("runMetrics", {})
    metrics = {
        name: _metric_comparison(
            baseline_metrics.get(name),
            current_metrics.get(name),
        )
        for name in metric_names
    }
    baseline_purposes = {
        item.get("purpose"): item for item in baseline.get("purposeMetrics", [])
    }
    current_purposes = {
        item.get("purpose"): item for item in current.get("purposeMetrics", [])
    }
    purposes = []
    for purpose in sorted(set(baseline_purposes) | set(current_purposes)):
        before = baseline_purposes.get(purpose, {})
        after = current_purposes.get(purpose, {})
        purposes.append(
            {
                "purpose": purpose,
                "callCount": _metric_comparison(
                    before.get("callCount", 0), after.get("callCount", 0)
                ),
                "latencyMs": _metric_comparison(
                    before.get("latencyMs", 0), after.get("latencyMs", 0)
                ),
                "inputTokens": _metric_comparison(
                    before.get("inputTokens", 0), after.get("inputTokens", 0)
                ),
                "outputTokens": _metric_comparison(
                    before.get("outputTokens", 0), after.get("outputTokens", 0)
                ),
            }
        )
    return {
        "baselineCaseId": baseline.get("caseId"),
        "currentCaseId": current.get("caseId"),
        "metrics": metrics,
        "purposes": purposes,
    }


def render_cycle_audit_markdown(report: dict[str, Any]) -> str:
    """Cycle監査報告を、本文を重複掲載しないMarkdownへ変換する。"""

    lines = [
        "# Agent Cycle Audit",
        "",
        f"- Case: `{_md(report.get('caseId'))}`",
        f"- Cycles: {report.get('cycleCount', 0)}",
        f"- Path findings: {report.get('findingCount', 0)}",
        f"- Execution findings: {report.get('executionFindingCount', 0)}",
        f"- Final run status: `{_md(report.get('finalRunStatus'))}`",
        "",
        (
            "> 最終回答の合否と探索経路の警告は別です。"
            "警告は法的誤りの確定ではなく、確認対象です。"
        ),
    ]
    run_metrics = report.get("runMetrics", {})
    lines.extend(
        [
            "",
            "## Run performance",
            "",
            (
                "| Wall ms | Model calls | Model latency ms | Input tokens | "
                "Output tokens | Tool calls | Tool ms |"
            ),
            "|---:|---:|---:|---:|---:|---:|---:|",
            (
                "| {elapsed} | {calls} | {latency} | {input_tokens} | "
                "{output_tokens} | {tool_calls} | {tool_ms} |"
            ).format(
                elapsed=_md(run_metrics.get("elapsedMs")),
                calls=run_metrics.get("modelCallCount", 0),
                latency=run_metrics.get("modelLatencyMs", 0),
                input_tokens=run_metrics.get("inputTokens", 0),
                output_tokens=run_metrics.get("outputTokens", 0),
                tool_calls=run_metrics.get("toolCallCount", 0),
                tool_ms=run_metrics.get("toolElapsedMs", 0),
            ),
        ]
    )
    purpose_metrics = report.get("purposeMetrics", [])
    if purpose_metrics:
        lines.extend(
            [
                "",
                "### Model calls by purpose",
                "",
                "| Purpose | Calls | Latency ms | Input tokens | Output tokens |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for item in purpose_metrics:
            lines.append(
                "| {purpose} | {calls} | {latency} | {input_tokens} | {output_tokens} |".format(
                    purpose=_md(item.get("purpose")),
                    calls=item.get("callCount", 0),
                    latency=item.get("latencyMs", 0),
                    input_tokens=item.get("inputTokens", 0),
                    output_tokens=item.get("outputTokens", 0),
                )
            )
    execution_findings = report.get("executionFindings", [])
    lines.extend(["", "## Execution findings", ""])
    if execution_findings:
        for finding in execution_findings:
            lines.append(
                f"- `{_md(finding.get('code'))}`: {_md(finding.get('message'))}"
            )
    else:
        lines.append("- なし")
    session_activity = report.get("workItemSessionActivity", [])
    if session_activity:
        lines.extend(
            [
                "",
                "## WorkItem sessions",
                "",
                (
                    "| Session | WorkItem | Cycles | Turns | Calls | Timeouts | "
                    "Latency ms | Input tokens | Output tokens |"
                ),
                "|---|---|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for item in session_activity:
            lines.append(
                (
                    "| `{session}` | {work_items} | {cycles} | {turns} | "
                    "{calls} | {timeouts} | {latency} | {input_tokens} | "
                    "{output_tokens} |"
                ).format(
                    session=_md(item.get("sessionId")),
                    work_items=_md(", ".join(item.get("workItemIds", []))),
                    cycles=_md(", ".join(str(v) for v in item.get("cycleNos", []))),
                    turns=len(item.get("turns", [])),
                    calls=item.get("callCount", 0),
                    timeouts=item.get("timeoutCount", 0),
                    latency=item.get("latencyMs", 0),
                    input_tokens=item.get("inputTokens", 0),
                    output_tokens=item.get("outputTokens", 0),
                )
            )
    hypothesis_activity = report.get("hypothesisActivity", [])
    if hypothesis_activity:
        lines.extend(
            [
                "",
                "## Hypothesis activity",
                "",
                "| Cycle | Hypothesis | Calls | Purposes |",
                "|---:|---|---:|---|",
            ]
        )
        for item in hypothesis_activity:
            purposes = ", ".join(
                f"{name}={count}"
                for name, count in item.get("purposes", {}).items()
            )
            lines.append(
                "| {cycle} | {hypothesis} | {calls} | {purposes} |".format(
                    cycle=item.get("cycleNo"),
                    hypothesis=_md(item.get("hypothesisId")),
                    calls=item.get("callCount", 0),
                    purposes=_md(purposes),
                )
            )
    for cycle in report.get("cycles", []):
        transition = cycle.get("transition", {})
        lines.extend(
            [
                "",
                f"## Cycle {cycle.get('cycleNo')}",
                "",
                f"- Transition: `{_md(transition.get('next'))}` / "
                f"start next: `{bool(transition.get('startNextCycle'))}`",
                f"- Reason: {_md(cycle.get('decisionReason'))}",
                f"- Wall time: `{_md(cycle.get('elapsedMs'))}` ms",
                f"- Model: {_metrics(cycle.get('modelMetrics', {}))}",
                f"- Tools: {_metrics(cycle.get('toolMetrics', {}))}",
            ]
        )
        findings = cycle.get("findings", [])
        lines.extend(["", "### Path findings", ""])
        if findings:
            for finding in findings:
                lines.append(
                    f"- `{_md(finding.get('code'))}`: {_md(finding.get('message'))}"
                )
        else:
            lines.append("- なし")
        hypotheses = cycle.get("hypotheses", [])
        if hypotheses:
            lines.extend(
                [
                    "",
                    "### Hypotheses at close",
                    "",
                    "| ID | WorkItem | Judgment | Gaps | Evidence |",
                    "|---|---|---|---:|---:|",
                ]
            )
            for item in hypotheses:
                lines.append(
                    "| {id} | {work} | {judgment} | {gaps} | {evidence} |".format(
                        id=_md(item.get("hypothesisId")),
                        work=_md(item.get("workItemId")),
                        judgment=_md(item.get("judgment")),
                        gaps=len(item.get("gaps", [])),
                        evidence=len(item.get("evidenceIds", [])),
                    )
                )
        tools = cycle.get("toolExecutions", [])
        if tools:
            lines.extend(
                [
                    "",
                    "### Tool executions",
                    "",
                    "| Request | Tool | Scope | Status | Results | ms |",
                    "|---|---|---|---|---:|---:|",
                ]
            )
            for item in tools:
                lines.append(
                    "| {request} | {tool} | {scope} | {status} | {results} | {ms} |".format(
                        request=_md(item.get("requestId")),
                        tool=_md(item.get("toolName")),
                        scope=_md(_tool_scope(item.get("arguments", {}))),
                        status=_md(item.get("status")),
                        results=len(item.get("evidenceIds", [])),
                        ms=item.get("elapsedMs", 0),
                    )
                )
    lines.append("")
    return "\n".join(lines)


def render_cycle_audit_comparison_markdown(comparison: dict[str, Any]) -> str:
    lines = [
        "# Agent Diagnostic Comparison",
        "",
        f"- Baseline: `{_md(comparison.get('baselineCaseId'))}`",
        f"- Current: `{_md(comparison.get('currentCaseId'))}`",
        "",
        "## Run metrics",
        "",
        "| Metric | Baseline | Current | Delta |",
        "|---|---:|---:|---:|",
    ]
    for name, value in comparison.get("metrics", {}).items():
        lines.append(
            "| {name} | {before} | {after} | {delta} |".format(
                name=_md(name),
                before=_md(value.get("baseline")),
                after=_md(value.get("current")),
                delta=_md(value.get("delta")),
            )
        )
    purposes = comparison.get("purposes", [])
    if purposes:
        lines.extend(
            [
                "",
                "## Model calls by purpose",
                "",
                "| Purpose | Calls before | Calls after | Calls delta | Latency delta ms |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for item in purposes:
            calls = item.get("callCount", {})
            latency = item.get("latencyMs", {})
            lines.append(
                "| {purpose} | {before} | {after} | {call_delta} | {latency_delta} |".format(
                    purpose=_md(item.get("purpose")),
                    before=calls.get("baseline", 0),
                    after=calls.get("current", 0),
                    call_delta=calls.get("delta", 0),
                    latency_delta=latency.get("delta", 0),
                )
            )
    lines.append("")
    return "\n".join(lines)


def _model_call_records(
    records: tuple[dict[str, Any], ...],
) -> list[dict[str, Any]]:
    pending: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    calls: list[dict[str, Any]] = []
    for record in records:
        event = record.get("event")
        key = (record.get("purpose"), record.get("contractAttempt"))
        if event == "solver_input":
            pending.setdefault(key, []).append(record)
            continue
        if event != "solver_output":
            continue
        inputs = pending.get(key, [])
        input_record = inputs.pop(0) if inputs else {}
        output_scope = record.get("scope") or {}
        input_scope = input_record.get("scope") or {}
        scope = {
            "workItemIds": list(
                output_scope.get("workItemIds")
                or input_scope.get("workItemIds")
                or []
            ),
            "hypothesisIds": list(
                output_scope.get("hypothesisIds")
                or input_scope.get("hypothesisIds")
                or []
            ),
        }
        calls.append(
            {
                "sequence": record.get("sequence"),
                "inputSequence": input_record.get("sequence"),
                "cycleNo": record.get("cycleNo") or input_record.get("cycleNo"),
                "purpose": record.get("purpose"),
                "latencyMs": record.get("latencyMs"),
                "inputTokens": record.get("inputTokens"),
                "outputTokens": record.get("outputTokens"),
                "scope": scope,
            }
        )
    return calls


def _purpose_metrics(model_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for call in model_calls:
        purpose = str(call.get("purpose") or "unknown")
        metrics = grouped.setdefault(
            purpose,
            {
                "purpose": purpose,
                "callCount": 0,
                "latencyMs": 0,
                "inputTokens": 0,
                "outputTokens": 0,
            },
        )
        metrics["callCount"] += 1
        metrics["latencyMs"] += int(call.get("latencyMs") or 0)
        metrics["inputTokens"] += int(call.get("inputTokens") or 0)
        metrics["outputTokens"] += int(call.get("outputTokens") or 0)
    return sorted(
        grouped.values(),
        key=lambda item: (-item["latencyMs"], item["purpose"]),
    )


def _hypothesis_activity(
    model_calls: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[int, str], dict[str, Any]] = {}
    for call in model_calls:
        cycle_no = int(call.get("cycleNo") or 1)
        for hypothesis_id in (call.get("scope") or {}).get("hypothesisIds", []):
            key = (cycle_no, hypothesis_id)
            activity = grouped.setdefault(
                key,
                {
                    "cycleNo": cycle_no,
                    "hypothesisId": hypothesis_id,
                    "callCount": 0,
                    "purposes": {},
                    "sequences": [],
                },
            )
            purpose = str(call.get("purpose") or "unknown")
            activity["callCount"] += 1
            activity["purposes"][purpose] = (
                activity["purposes"].get(purpose, 0) + 1
            )
            if isinstance(call.get("sequence"), int):
                activity["sequences"].append(call["sequence"])
    return [grouped[key] for key in sorted(grouped)]


def _work_item_session_activity(
    records: tuple[dict[str, Any], ...],
) -> list[dict[str, Any]]:
    """並列完了順に依存せず、論理session IDとturnで輸送記録を対応付ける。"""

    calls: dict[tuple[str, int, str, int], dict[str, Any]] = {}
    for record in records:
        session_id = record.get("workItemSessionId")
        turn = record.get("workItemSessionTurn")
        if not isinstance(session_id, str) or not isinstance(turn, int):
            continue
        stage = str(record.get("transportStage") or "unknown")
        attempt = int(record.get("transportAttempt") or 1)
        key = (session_id, turn, stage, attempt)
        call = calls.setdefault(
            key,
            {
                "sessionId": session_id,
                "turn": turn,
                "transportStage": stage,
                "transportAttempt": attempt,
                "cycleNo": record.get("cycleNo"),
                "workItemIds": [],
                "hypothesisIds": [],
                "inputSequence": None,
                "outputSequence": None,
                "timeoutSequence": None,
                "promptChars": 0,
                "schemaChars": 0,
                "latencyMs": 0,
                "inputTokens": 0,
                "outputTokens": 0,
                "validationError": None,
                "timedOut": False,
                "completeRequestPath": None,
            },
        )
        scope = record.get("scope") or {}
        call["workItemIds"] = list(
            dict.fromkeys([*call["workItemIds"], *(scope.get("workItemIds") or [])])
        )
        call["hypothesisIds"] = list(
            dict.fromkeys(
                [*call["hypothesisIds"], *(scope.get("hypothesisIds") or [])]
            )
        )
        if record.get("event") == "transport_input":
            call["inputSequence"] = record.get("sequence")
            call["promptChars"] = int(record.get("promptChars") or 0)
            call["schemaChars"] = int(record.get("schemaChars") or 0)
            call["completeRequestPath"] = record.get("completeRequestPath")
        elif record.get("event") == "transport_output":
            call["outputSequence"] = record.get("sequence")
            call["latencyMs"] = int(record.get("latencyMs") or 0)
            call["inputTokens"] = int(record.get("inputTokens") or 0)
            call["outputTokens"] = int(record.get("outputTokens") or 0)
            call["validationError"] = record.get("validationError")
        elif record.get("event") == "transport_timeout":
            call["timeoutSequence"] = record.get("sequence")
            call["timedOut"] = True

    sessions: dict[str, dict[str, Any]] = {}
    for call in sorted(
        calls.values(),
        key=lambda item: (
            int(item.get("inputSequence") or item.get("outputSequence") or 0),
            item["sessionId"],
            item["turn"],
        ),
    ):
        session = sessions.setdefault(
            call["sessionId"],
            {
                "sessionId": call["sessionId"],
                "workItemIds": [],
                "cycleNos": [],
                "callCount": 0,
                "timeoutCount": 0,
                "latencyMs": 0,
                "inputTokens": 0,
                "outputTokens": 0,
                "turns": [],
            },
        )
        session["workItemIds"] = list(
            dict.fromkeys([*session["workItemIds"], *call["workItemIds"]])
        )
        if isinstance(call.get("cycleNo"), int):
            session["cycleNos"] = list(
                dict.fromkeys([*session["cycleNos"], call["cycleNo"]])
            )
        session["callCount"] += 1
        session["timeoutCount"] += int(call["timedOut"])
        session["latencyMs"] += call["latencyMs"]
        session["inputTokens"] += call["inputTokens"]
        session["outputTokens"] += call["outputTokens"]
        session["turns"].append(call)
    return list(sessions.values())


def _work_item_session_findings(
    sessions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    sessions_by_work_item: dict[str, list[str]] = {}
    for session in sessions:
        work_item_ids = session.get("workItemIds", [])
        if len(work_item_ids) > 1:
            findings.append(
                _finding(
                    "WORK_ITEM_SESSION_SCOPE_CONFLICT",
                    "一つの専属sessionが複数のWorkItemを処理しています。",
                    sessionId=session.get("sessionId"),
                    workItemIds=work_item_ids,
                )
            )
        for work_item_id in work_item_ids:
            sessions_by_work_item.setdefault(work_item_id, []).append(
                session["sessionId"]
            )
        if session.get("timeoutCount", 0):
            findings.append(
                _finding(
                    "WORK_ITEM_SESSION_TIMEOUT",
                    "WorkItem専属sessionで時間切れが発生しています。",
                    sessionId=session.get("sessionId"),
                    workItemIds=work_item_ids,
                    timeoutCount=session.get("timeoutCount"),
                )
            )
    for work_item_id, session_ids in sorted(sessions_by_work_item.items()):
        unique_session_ids = list(dict.fromkeys(session_ids))
        if len(unique_session_ids) <= 1:
            continue
        findings.append(
            _finding(
                "WORK_ITEM_SESSION_ID_CHANGED",
                "同じWorkItemが複数の専属session IDで処理されています。",
                workItemId=work_item_id,
                sessionIds=unique_session_ids,
            )
        )
    return findings


def _execution_findings(
    records: tuple[dict[str, Any], ...],
    model_calls: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    observation_by_scope: dict[tuple[int, str], list[int]] = {}
    for call in model_calls:
        if call.get("purpose") != "observation_integration":
            continue
        cycle_no = int(call.get("cycleNo") or 1)
        for hypothesis_id in (call.get("scope") or {}).get("hypothesisIds", []):
            observation_by_scope.setdefault((cycle_no, hypothesis_id), []).append(
                int(call.get("sequence") or 0)
            )
    tool_executions = [
        item for item in records if item.get("event") == "tool_execution"
    ]
    for (cycle_no, hypothesis_id), sequences in sorted(observation_by_scope.items()):
        repeated_without_result: list[int] = []
        for previous_sequence, current_sequence in zip(sequences, sequences[1:]):
            has_new_scoped_result = any(
                isinstance(item.get("sequence"), int)
                and previous_sequence < item["sequence"] < current_sequence
                and item.get("cycleNo") == cycle_no
                and hypothesis_id in (item.get("hypothesisIds") or [])
                for item in tool_executions
            )
            if has_new_scoped_result:
                continue
            if not repeated_without_result:
                repeated_without_result.append(previous_sequence)
            repeated_without_result.append(current_sequence)
        if not repeated_without_result:
            continue
        findings.append(
            _finding(
                "REPEATED_OBSERVATION_INTEGRATION_SCOPE",
                (
                    "同じCycle・Hypothesisに対して、新しいTool結果を挟まず"
                    "Observation Integrationを再実行しています。"
                ),
                cycleNo=cycle_no,
                hypothesisIds=[hypothesis_id],
                callCount=len(repeated_without_result),
                sequences=repeated_without_result,
            )
        )

    applied = [item for item in records if item.get("event") == "decision_applied"]
    for previous, current in zip(applied, applied[1:]):
        if previous.get("purpose") != "observation_integration":
            continue
        if current.get("purpose") != "integration":
            continue
        previous_results = (previous.get("stateAfterStatus") or {}).get(
            "toolResultCount"
        )
        current_results = (current.get("stateBeforeStatus") or {}).get(
            "toolResultCount"
        )
        if (
            not isinstance(previous_results, int)
            or previous_results != current_results
        ):
            continue
        findings.append(
            _finding(
                "ADJACENT_INTEGRATION_WITHOUT_NEW_TOOL_RESULT",
                (
                    "Observation Integrationの直後に、新しいTool結果を挟まず"
                    "通常Integrationを実行しています。"
                ),
                previousSequence=previous.get("sequence"),
                currentSequence=current.get("sequence"),
                hypothesisIds=(previous.get("scope") or {}).get(
                    "hypothesisIds", []
                ),
            )
        )

    repeated_inputs: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for record in records:
        if record.get("event") != "transport_input":
            continue
        hashes = (
            record.get("instructionsHash"),
            record.get("inputHash"),
            record.get("schemaHash"),
        )
        if not all(isinstance(value, str) for value in hashes):
            continue
        key = (str(hashes[0]), str(hashes[1]), str(hashes[2]))
        repeated_inputs.setdefault(key, []).append(record)
    for duplicates in repeated_inputs.values():
        if len(duplicates) < 2:
            continue
        findings.append(
            _finding(
                "REPEATED_MODEL_INPUT",
                "同一の指示・入力・出力schemaでモデルを複数回呼び出しています。",
                sequences=[item.get("sequence") for item in duplicates],
                callCount=len(duplicates),
                transportStages=list(
                    dict.fromkeys(
                        str(item.get("transportStage")) for item in duplicates
                    )
                ),
            )
        )

    repeated_tools: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for record in records:
        if record.get("event") != "tool_execution":
            continue
        key = (
            record.get("cycleNo"),
            record.get("toolName"),
            json.dumps(
                record.get("arguments") or {},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            tuple(record.get("hypothesisIds") or []),
        )
        repeated_tools.setdefault(key, []).append(record)
    for duplicates in repeated_tools.values():
        if len(duplicates) < 2:
            continue
        findings.append(
            _finding(
                "REPEATED_TOOL_SCOPE",
                "同じCycleで同一のTool・引数・Hypothesisを複数回実行しています。",
                requestIds=[item.get("requestId") for item in duplicates],
                callCount=len(duplicates),
            )
        )
    return findings


def _cycle_elapsed_ms(
    checkpoint: dict[str, Any],
    records_by_sequence: dict[int, dict[str, Any]],
) -> int | None:
    start = records_by_sequence.get(checkpoint.get("startSequence"))
    start_elapsed = start.get("runElapsedMs") if isinstance(start, dict) else None
    end_elapsed = checkpoint.get("runElapsedMs")
    if not isinstance(start_elapsed, int) or not isinstance(end_elapsed, int):
        return None
    return max(0, end_elapsed - start_elapsed)


def _metric_comparison(before: Any, after: Any) -> dict[str, Any]:
    delta = after - before if isinstance(before, int) and isinstance(after, int) else None
    return {"baseline": before, "current": after, "delta": delta}


def _new_evidence(baseline: CaseState, state_after: CaseState) -> tuple[Evidence, ...]:
    known = {item.evidence_id for item in baseline.evidence}
    return tuple(item for item in state_after.evidence if item.evidence_id not in known)


def _model_changes(
    before: tuple[Any, ...],
    after: tuple[Any, ...],
    id_field: str,
) -> list[dict[str, Any]]:
    before_by_id = {getattr(item, id_field): item.model_dump(mode="json") for item in before}
    changes: list[dict[str, Any]] = []
    for item in after:
        item_id = getattr(item, id_field)
        current = item.model_dump(mode="json")
        previous = before_by_id.get(item_id)
        if previous != current:
            changes.append({"id": item_id, "before": previous, "after": current})
    return changes


def _work_item(item: WorkItem) -> dict[str, Any]:
    return {
        "workItemId": item.work_item_id,
        "parentWorkItemId": item.parent_work_item_id,
        "question": item.question,
        "state": item.state,
        "resolution": item.resolution,
    }


def _hypothesis(item: Hypothesis) -> dict[str, Any]:
    return {
        "hypothesisId": item.hypothesis_id,
        "workItemId": item.work_item_id,
        "statement": item.statement,
        "judgment": item.judgment,
        "gaps": list(item.gaps),
        "evidenceIds": list(item.evidence_ids),
    }


def _evidence_summary(item: Evidence) -> dict[str, Any]:
    return {
        "evidenceId": item.evidence_id,
        "articleId": item.metadata.get("articleId"),
        "evidenceRole": item.metadata.get("evidenceRole"),
        "title": item.title,
    }


def _tool_execution(
    request: ToolRequest | None,
    result: ToolResult,
) -> dict[str, Any]:
    return {
        "requestId": result.request_id,
        "toolName": request.tool_name if request is not None else None,
        "workItemId": request.work_item_id if request is not None else None,
        "hypothesisIds": list(request.hypothesis_ids) if request is not None else [],
        "purpose": request.purpose if request is not None else None,
        "arguments": request.arguments if request is not None else {},
        "status": result.status,
        "errorCode": result.error_code,
        "elapsedMs": result.elapsed_ms,
        "evidenceIds": list(result.evidence_ids),
    }


def _structural_findings(
    *,
    baseline: CaseState,
    state_after: CaseState,
    decision: SolverDecision,
    cycle_results: tuple[ToolResult, ...],
    requests: dict[str, ToolRequest],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    all_successful_graph_scopes = {
        _graph_scope(requests.get(result.request_id))
        for result in state_after.tool_results
        if result.status == "succeeded"
        and requests.get(result.request_id) is not None
        and requests[result.request_id].tool_name == "legal_graph_neighbors"
    }
    for result in cycle_results:
        request = requests.get(result.request_id)
        if request is None:
            continue
        if (
            request.tool_name == "legal_graph_neighbors"
            and request.arguments.get("mode") == "semantic_assertion"
            and result.status == "succeeded"
            and not result.evidence_ids
        ):
            inverse = _inverse_graph_scope(request)
            if inverse not in all_successful_graph_scopes:
                findings.append(
                    _finding(
                        "GRAPH_EMPTY_INVERSE_UNTRIED",
                        "意味関係Graph検索は0件で、同じ起点・関係の逆方向は未試行です。方向誤りの確定ではありません。",
                        requestId=request.request_id,
                        hypothesisIds=list(request.hypothesis_ids),
                        arguments=request.arguments,
                    )
                )
        if request.tool_name != "fetch_articles" or result.status != "succeeded":
            continue
        if result.request_id not in state_after.integrated_tool_result_request_ids:
            findings.append(
                _finding(
                    "FETCH_RESULT_NOT_INTEGRATED",
                    "取得済み本文のToolResultがCycle終了時点で統合済みとして記録されていません。",
                    requestId=request.request_id,
                    hypothesisIds=list(request.hypothesis_ids),
                )
            )
        if result.evidence_ids and request.hypothesis_ids:
            linked = {
                evidence_id
                for item in state_after.hypotheses
                if item.hypothesis_id in request.hypothesis_ids
                for evidence_id in item.evidence_ids
            }
            if not linked.intersection(result.evidence_ids):
                findings.append(
                    _finding(
                        "FETCH_RESULT_UNMAPPED",
                        "取得本文のEvidenceが、要求時に指定したHypothesisのいずれにも対応付けられていません。",
                        requestId=request.request_id,
                        hypothesisIds=list(request.hypothesis_ids),
                        evidenceIds=list(result.evidence_ids),
                    )
                )
    before_hypotheses = {item.hypothesis_id: item for item in baseline.hypotheses}
    for item in state_after.hypotheses:
        previous = before_hypotheses.get(item.hypothesis_id)
        if previous is None or not previous.gaps or item.gaps:
            continue
        if set(item.evidence_ids) <= set(previous.evidence_ids):
            findings.append(
                _finding(
                    "GAPS_CLEARED_WITHOUT_NEW_EVIDENCE",
                    "未確認事項が空になりましたが、このCycleでHypothesisへ新しいEvidenceは追加されていません。既存根拠の再評価なら妥当な場合があります。",
                    hypothesisIds=[item.hypothesis_id],
                )
            )
    if decision.start_next_cycle and not _has_cycle_progress(baseline, state_after):
        findings.append(
            _finding(
                "CYCLE_NO_PROGRESS",
                "新しいEvidenceまたはWorkItem・Hypothesisの変更がないまま次Cycleへ移行しています。",
            )
        )
    return findings


def _finding(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"severity": "warning", "code": code, "message": message, "details": details}


def _graph_scope(request: ToolRequest | None) -> tuple[Any, ...] | None:
    if request is None or request.tool_name != "legal_graph_neighbors":
        return None
    arguments = request.arguments
    return (
        tuple(arguments.get("article_ids", [])),
        arguments.get("mode"),
        arguments.get("predicate"),
        arguments.get("direction"),
    )


def _inverse_graph_scope(request: ToolRequest) -> tuple[Any, ...] | None:
    scope = _graph_scope(request)
    if scope is None:
        return None
    article_ids, mode, predicate, direction = scope
    inverse_direction = {
        "from_subject": "to_subject",
        "to_subject": "from_subject",
    }.get(direction)
    if inverse_direction is None:
        return None
    return article_ids, mode, predicate, inverse_direction


def _has_cycle_progress(baseline: CaseState, state_after: CaseState) -> bool:
    if {item.evidence_id for item in baseline.evidence} != {
        item.evidence_id for item in state_after.evidence
    }:
        return True
    return any(
        _model_changes(before, after, id_field)
        for before, after, id_field in (
            (baseline.work_items, state_after.work_items, "work_item_id"),
            (baseline.hypotheses, state_after.hypotheses, "hypothesis_id"),
        )
    )


def _decision_belongs_to_cycle(value: dict[str, Any], cycle_no: int) -> bool:
    decided_cycle = value.get("decided_cycle")
    return decided_cycle in {None, cycle_no}


def _tool_scope(arguments: dict[str, Any]) -> str:
    parts = []
    for key in ("mode", "predicate", "direction", "reference_lookup"):
        value = arguments.get(key)
        if value is not None:
            parts.append(f"{key}={value}")
    article_ids = arguments.get("article_ids")
    if isinstance(article_ids, list):
        parts.append(f"articles={len(article_ids)}")
    return ", ".join(parts) or "-"


def _metrics(value: dict[str, Any]) -> str:
    if not value:
        return "-"
    return ", ".join(f"{key}={item}" for key, item in value.items())


def _md(value: Any) -> str:
    if value is None:
        return "-"
    return str(value).replace("|", "\\|").replace("\n", " ")
