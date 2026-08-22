from __future__ import annotations

from typing import Any

import pytest
from app import main
from app.agent_framework.context import build_solver_context
from app.agent_framework.contracts import SolverDecision
from app.agent_framework.diagnostics import AgentDiagnostics
from app.agent_framework.profiles import AgentLimits
from app.agent_framework.state import CaseState, FinalAnswer
from app.framework_audit import (
    AuditSnapshotInvalidError,
    FrameworkPostRunAuditService,
    PostRunAuditDisabledError,
)
from app.llm import StructuredJSONResult
from app.models import FrameworkAuditRequest, FrameworkAuditResponse


class FakeAuditLLM:
    def __init__(self, source_sequence: int) -> None:
        self.source_sequence = source_sequence
        self.calls: list[dict[str, Any]] = []

    def generate_structured_json(self, **kwargs: Any) -> StructuredJSONResult:
        self.calls.append(kwargs)
        return StructuredJSONResult(
            payload={
                "explanation": "取得済み根拠で作業を完了できると判断しています。",
                "recorded_facts": ["finalizeを選択した"],
                "inferences": ["未解決事項がないことが完了判断に寄与した"],
                "source_decision_sequences": [self.source_sequence],
                "limitations": ["内部思考そのものは記録されていない"],
            },
            provider="fake",
            model=kwargs["model"],
            latencyMs=1,
            inputTokens=100,
            outputTokens=50,
        )


def _write_completed_snapshot(tmp_path, *, mode: str = "snapshot") -> tuple[str, int]:
    case_id = "legal-audit-case"
    before = CaseState(case_id=case_id, question="要件は何ですか")
    context = build_solver_context(
        before,
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    decision = SolverDecision(
        next="finalize",
        decision_reason="必要な本文根拠が揃い、未解決事項がないため完了する",
        answer=FinalAnswer(text="回答"),
    )
    after = before.model_copy(
        update={
            "run_status": "completed",
            "final_answer": decision.answer,
        }
    )
    diagnostics = AgentDiagnostics(
        mode=mode,
        output_dir=tmp_path,
        case_id=case_id,
    )
    diagnostics.record_decision_applied(
        state_before=before,
        state_after=after,
        context=context,
        purpose="integration",
        contract_attempt=0,
        decision=decision,
    )
    diagnostics.record_run_complete(state=after, failure_code=None)
    return case_id, diagnostics.applied_decision_sequences[0]


def test_post_run_audit_explains_saved_applied_decision(monkeypatch, tmp_path) -> None:
    case_id, decision_sequence = _write_completed_snapshot(tmp_path)
    llm = FakeAuditLLM(decision_sequence)
    monkeypatch.setattr(
        "app.framework_audit.settings.agent_framework_post_run_audit",
        "on_demand",
    )
    monkeypatch.setattr("app.framework_audit.settings.eval_results_dir", tmp_path)
    monkeypatch.setattr(
        "app.framework_audit.settings.agent_framework_max_solver_input_chars",
        1_000_000,
    )

    response = FrameworkPostRunAuditService(llm).audit(
        FrameworkAuditRequest(
            caseId=case_id,
            inquiry="なぜ完了と判断したのですか",
        )
    )

    assert response.decisionSequence == decision_sequence
    assert response.recordedDecisionReason == (
        "必要な本文根拠が揃い、未解決事項がないため完了する"
    )
    assert response.sourceDecisionSequences == [decision_sequence]
    assert response.inputTokens == 100
    assert len(llm.calls) == 1
    assert "内部思考の復元ではありません" in llm.calls[0]["prompt"]
    assert llm.calls[0]["schema"]["properties"]["source_decision_sequences"]["items"][
        "enum"
    ] == [decision_sequence]


def test_post_run_audit_requires_snapshot_mode(monkeypatch, tmp_path) -> None:
    case_id, decision_sequence = _write_completed_snapshot(tmp_path, mode="status")
    monkeypatch.setattr(
        "app.framework_audit.settings.agent_framework_post_run_audit",
        "on_demand",
    )
    monkeypatch.setattr("app.framework_audit.settings.eval_results_dir", tmp_path)

    with pytest.raises(AuditSnapshotInvalidError, match="snapshot-mode"):
        FrameworkPostRunAuditService(FakeAuditLLM(decision_sequence)).audit(
            FrameworkAuditRequest(caseId=case_id)
        )


def test_post_run_audit_is_disabled_without_explicit_setting(
    monkeypatch,
    tmp_path,
) -> None:
    case_id, decision_sequence = _write_completed_snapshot(tmp_path)
    llm = FakeAuditLLM(decision_sequence)
    monkeypatch.setattr(
        "app.framework_audit.settings.agent_framework_post_run_audit",
        "off",
    )

    with pytest.raises(PostRunAuditDisabledError):
        FrameworkPostRunAuditService(llm).audit(FrameworkAuditRequest(caseId=case_id))

    assert llm.calls == []


def test_framework_audit_endpoint_uses_read_only_audit_service(monkeypatch) -> None:
    expected = FrameworkAuditResponse(
        caseId="legal-case",
        decisionSequence=3,
        recordedDecisionReason="根拠が揃った",
        explanation="完了判断を説明した",
        recordedFacts=["finalize"],
        inferences=[],
        sourceDecisionSequences=[3],
        limitations=[],
        model="model",
    )
    monkeypatch.setattr(
        main.framework_audit_service,
        "audit",
        lambda request: expected,
    )

    response = main.framework_audit(
        FrameworkAuditRequest(caseId="legal-case", inquiry="なぜですか")
    )

    assert response == expected.model_dump()
