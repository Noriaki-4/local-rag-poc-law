"""Research Cycleの経路を、保存済み状態から決定的に監査する。"""

from __future__ import annotations

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
    cycles: list[dict[str, Any]] = []
    for record in checkpoints:
        snapshot = record.get("cycleSnapshot")
        if isinstance(snapshot, dict):
            cycles.append(snapshot)
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
    return {
        "caseId": (
            run_complete.get("caseId")
            if isinstance(run_complete, dict)
            else (cycles[0].get("caseId") if cycles else None)
        ),
        "cycleCount": len(cycles),
        "findingCount": len(findings),
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
    }


def render_cycle_audit_markdown(report: dict[str, Any]) -> str:
    """Cycle監査報告を、本文を重複掲載しないMarkdownへ変換する。"""

    lines = [
        "# Agent Cycle Audit",
        "",
        f"- Case: `{_md(report.get('caseId'))}`",
        f"- Cycles: {report.get('cycleCount', 0)}",
        f"- Path findings: {report.get('findingCount', 0)}",
        f"- Final run status: `{_md(report.get('finalRunStatus'))}`",
        "",
        "> 最終回答の合否と探索経路の警告は別です。警告は法的誤りの確定ではなく、確認対象です。",
    ]
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
