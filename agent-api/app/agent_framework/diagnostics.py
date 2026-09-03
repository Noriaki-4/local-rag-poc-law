"""明示的に有効化した実行だけ、Agent判断材料をローカルへ記録する。"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from threading import RLock
from time import monotonic
from typing import Any, Literal

from app.config import settings

from .context import SolverActionFeedback, SolverContext
from .contracts import SolverDecision
from .cycle_audit import build_cycle_checkpoint, cycle_number
from .model_call_artifacts import RenderedModelCall, write_model_call_artifacts
from .ports.model import ReviewerView
from .profiles import ModelCallProfile, ReviewerProfile
from .state import CaseState, ReviewResult, ToolRequest, ToolResult

DiagnosticsMode = Literal["off", "status", "snapshot"]
logger = logging.getLogger(__name__)
_CASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")


class AgentDiagnostics:
    """通常実行へ影響させず、指定modeの診断recordをJSONLへ追記する。"""

    def __init__(
        self,
        *,
        mode: str,
        output_dir: Path,
        case_id: str,
        profile_name: str | None = None,
        profile_version: str | None = None,
    ) -> None:
        self.mode: DiagnosticsMode = (
            mode if mode in {"off", "status", "snapshot"} else "off"
        )
        self._path = output_dir / "agent-framework-diagnostics" / f"{case_id}.jsonl"
        self._artifact_root = output_dir / "agent-model-calls" / case_id
        self._sequence = 0
        self._applied_decision_sequences: list[int] = []
        self._lock = RLock()
        self._profile_name = profile_name
        self._profile_version = profile_version
        self._cycle_baselines: dict[int, CaseState] = {}
        self._cycle_start_sequences: dict[int, int] = {}
        self._cycle_model_metrics: dict[int, dict[str, int]] = {}
        self._recorded_cycle_checkpoints: set[int] = set()
        self._started_at = monotonic()

    @property
    def output_path(self) -> Path | None:
        return None if self.mode == "off" else self._path

    @property
    def applied_decision_sequences(self) -> tuple[int, ...]:
        return tuple(self._applied_decision_sequences)

    def record_solver_input(
        self,
        *,
        state: CaseState,
        context: SolverContext,
        profile: ModelCallProfile,
        purpose: str,
        contract_attempt: int,
    ) -> None:
        if self.mode == "off":
            return
        record: dict[str, Any] = {
            "event": "solver_input",
            "caseId": state.case_id,
            "purpose": purpose,
            "contractAttempt": contract_attempt + 1,
            "stateStatus": _state_status(state),
            "contextStatus": _context_status(context),
            "model": profile.model,
            "requestedReasoningEffort": profile.reasoning_effort,
            "maxOutputTokens": profile.max_output_tokens,
            "timeoutSec": profile.timeout_sec,
            "profileName": self._profile_name,
            "profileVersion": self._profile_version,
            "cycleNo": cycle_number(state),
            "questionHash": _text_sha256(state.question),
            "scope": _context_scope(context),
        }
        if self.mode == "snapshot":
            record.update(
                {
                    "caseState": state.model_dump(mode="json"),
                    "solverContext": context.model_dump(mode="json"),
                    "modelProfile": profile.model_dump(mode="json"),
                }
            )
        sequence = self._write(record)
        cycle_no = cycle_number(state)
        if cycle_no not in self._cycle_baselines:
            self._cycle_baselines[cycle_no] = state
            if sequence is not None:
                self._cycle_start_sequences[cycle_no] = sequence

    def record_solver_output(
        self,
        *,
        state: CaseState,
        purpose: str,
        contract_attempt: int,
        decision: SolverDecision,
        latency_ms: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        if self.mode == "off":
            return
        record: dict[str, Any] = {
            "event": "solver_output",
            "caseId": state.case_id,
            "purpose": purpose,
            "contractAttempt": contract_attempt + 1,
            "decisionStatus": _decision_status(decision),
            "solverDecisionHash": _json_sha256(decision.model_dump(mode="json")),
            "latencyMs": latency_ms,
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "cycleNo": cycle_number(state),
            "scope": _decision_scope(decision),
        }
        if self.mode == "snapshot":
            record["solverDecision"] = decision.model_dump(mode="json")
        self._write(record)
        metrics = self._cycle_model_metrics.setdefault(
            cycle_number(state),
            {"callCount": 0, "latencyMs": 0, "inputTokens": 0, "outputTokens": 0},
        )
        metrics["callCount"] += 1
        metrics["latencyMs"] += latency_ms or 0
        metrics["inputTokens"] += input_tokens or 0
        metrics["outputTokens"] += output_tokens or 0

    def record_contract_violation(
        self,
        *,
        state: CaseState,
        purpose: str,
        contract_attempt: int,
        decision: SolverDecision,
        violation: str,
    ) -> None:
        if self.mode == "off":
            return
        record: dict[str, Any] = {
            "event": "contract_violation",
            "caseId": state.case_id,
            "purpose": purpose,
            "contractAttempt": contract_attempt + 1,
            "violation": violation,
            "decisionStatus": _decision_status(decision),
            "solverDecisionHash": _json_sha256(decision.model_dump(mode="json")),
        }
        if self.mode == "snapshot":
            record["solverDecision"] = decision.model_dump(mode="json")
        self._write(record)

    def record_action_rejected(
        self,
        *,
        state: CaseState,
        purpose: str,
        decision_attempt: int,
        decision: SolverDecision,
        feedback: SolverActionFeedback,
    ) -> None:
        """実行前に棄却した決定的な重複行動を契約違反と分けて記録する。"""

        if self.mode == "off":
            return
        record: dict[str, Any] = {
            "event": "action_rejected",
            "caseId": state.case_id,
            "purpose": purpose,
            "decisionAttempt": decision_attempt + 1,
            "actionCode": feedback.code,
            "message": feedback.message,
            "rejectedRequestIds": [
                item.request_id for item in feedback.rejected_tool_requests
            ],
            "rejectedToolNames": [
                item.tool_name for item in feedback.rejected_tool_requests
            ],
            "decisionStatus": _decision_status(decision),
            "solverDecisionHash": _json_sha256(decision.model_dump(mode="json")),
        }
        if self.mode == "snapshot":
            record["solverDecision"] = decision.model_dump(mode="json")
            record["actionFeedback"] = feedback.model_dump(mode="json")
        self._write(record)

    def record_decision_applied(
        self,
        *,
        state_before: CaseState,
        state_after: CaseState,
        context: SolverContext,
        purpose: str,
        contract_attempt: int,
        decision: SolverDecision,
    ) -> None:
        """構造検証を通過し、CaseStateへ適用する判断だけを監査正本として残す。"""

        if self.mode == "off":
            return
        record: dict[str, Any] = {
            "event": "decision_applied",
            "caseId": state_before.case_id,
            "purpose": purpose,
            "contractAttempt": contract_attempt + 1,
            "decisionReason": decision.decision_reason,
            "decisionStatus": _decision_status(decision),
            "solverDecisionHash": _json_sha256(decision.model_dump(mode="json")),
            "stateBeforeStatus": _state_status(state_before),
            "stateAfterStatus": _state_status(state_after),
            "stateBeforeHash": _json_sha256(state_before.model_dump(mode="json")),
            "stateAfterHash": _json_sha256(state_after.model_dump(mode="json")),
            "cycleNo": cycle_number(state_before),
            "scope": _decision_scope(decision),
        }
        if self.mode == "snapshot":
            record.update(
                {
                    "caseStateBefore": state_before.model_dump(mode="json"),
                    "solverContext": context.model_dump(mode="json"),
                    "solverDecision": decision.model_dump(mode="json"),
                    "caseStateAfter": state_after.model_dump(mode="json"),
                }
            )
        sequence = self._write(record)
        if sequence is not None:
            self._applied_decision_sequences.append(sequence)
            self._record_cycle_checkpoint_if_needed(
                state_before=state_before,
                state_after=state_after,
                purpose=purpose,
                decision=decision,
                decision_sequence=sequence,
            )

    def _record_cycle_checkpoint_if_needed(
        self,
        *,
        state_before: CaseState,
        state_after: CaseState,
        purpose: str,
        decision: SolverDecision,
        decision_sequence: int,
    ) -> None:
        if not (
            purpose == "cycle_close"
            or decision.start_next_cycle
            or decision.next == "finalize"
        ):
            return
        cycle_no = cycle_number(state_before)
        if cycle_no in self._recorded_cycle_checkpoints:
            return
        baseline = self._cycle_baselines.get(cycle_no, state_before)
        snapshot = build_cycle_checkpoint(
            baseline=baseline,
            state_after=state_after,
            decision=decision,
            purpose=purpose,
            start_sequence=self._cycle_start_sequences.get(cycle_no),
            decision_sequence=decision_sequence,
            model_metrics=self._cycle_model_metrics.get(
                cycle_no,
                {"callCount": 0, "latencyMs": 0, "inputTokens": 0, "outputTokens": 0},
            ),
        )
        record: dict[str, Any] = {
            "event": "cycle_checkpoint",
            "caseId": state_before.case_id,
            "cycleNo": cycle_no,
            "startSequence": snapshot["startSequence"],
            "decisionSequence": decision_sequence,
            "purpose": purpose,
            "transition": snapshot["transition"],
            "decisionReason": decision.decision_reason,
            "findingCount": len(snapshot["findings"]),
            "findingCodes": [item["code"] for item in snapshot["findings"]],
            "findings": snapshot["findings"],
            "modelMetrics": snapshot["modelMetrics"],
            "toolMetrics": snapshot["toolMetrics"],
            "cycleSnapshotHash": _json_sha256(snapshot),
        }
        if self.mode == "snapshot":
            record["cycleSnapshot"] = snapshot
        self._write(record)
        self._recorded_cycle_checkpoints.add(cycle_no)

    def record_run_complete(
        self,
        *,
        state: CaseState,
        failure_code: str | None,
        elapsed_ms: int | None = None,
    ) -> None:
        if self.mode == "off":
            return
        record: dict[str, Any] = {
            "event": "run_complete",
            "caseId": state.case_id,
            "stateStatus": _state_status(state),
            "failureCode": failure_code,
            "appliedDecisionSequences": list(self._applied_decision_sequences),
            "elapsedMs": elapsed_ms,
        }
        if self.mode == "snapshot":
            record["caseState"] = state.model_dump(mode="json")
        self._write(record)

    def record_tool_execution(
        self,
        *,
        request: ToolRequest,
        result: ToolResult,
    ) -> None:
        if self.mode == "off":
            return
        self._write(
            {
                "event": "tool_execution",
                "caseId": self._path.stem,
                "cycleNo": result.cycle_no,
                "requestId": request.request_id,
                "toolName": request.tool_name,
                "workItemId": request.work_item_id,
                "hypothesisIds": list(request.hypothesis_ids),
                "purpose": request.purpose,
                "arguments": request.arguments,
                "status": result.status,
                "errorCode": result.error_code,
                "elapsedMs": result.elapsed_ms,
                "evidenceIds": list(result.evidence_ids),
            }
        )

    def record_reviewer_input(
        self,
        *,
        view: ReviewerView,
        profile: ReviewerProfile,
        rendered: RenderedModelCall,
        provider: str | None = None,
    ) -> None:
        if self.mode == "off":
            return
        schema_json = json.dumps(
            rendered.output_schema,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        artifact_dir = self._artifact_root / (
            f"reviewer-{rendered.request_hash[:12]}"
        )
        record: dict[str, Any] = {
            "event": "reviewer_input",
            "caseId": view.case_id,
            "model": profile.model,
            "requestedReasoningEffort": profile.reasoning_effort,
            **_effective_reasoning_settings(provider, profile),
            "workItemCount": len(view.work_items),
            "hypothesisCount": len(view.hypotheses),
            "dependencyDecisionCount": len(view.dependency_decisions),
            "evidenceCount": len(view.evidence),
            "promptChars": len(rendered.request),
            "schemaChars": len(schema_json),
            "promptHash": rendered.request_hash,
            "schemaHash": rendered.output_schema_hash,
            "instructionsHash": rendered.instructions_hash,
            "inputHash": rendered.input_hash,
            "normalizedSchemaHash": rendered.normalized_schema_hash,
            "systemPromptHash": _text_sha256(profile.system_prompt),
            "profileName": self._profile_name,
            "profileVersion": self._profile_version,
            "promptBuilder": "app.adapters.models.structured_json:_review_prompt",
            "artifactPath": str(artifact_dir),
        }
        if self.mode == "snapshot":
            record.update(
                {
                    "completeRequestPath": str(
                        artifact_dir / "complete_request.json"
                    ),
                    "reviewerView": view.model_dump(mode="json"),
                    "instructions": rendered.instructions,
                    "inputPayload": rendered.input_payload,
                    "prompt": rendered.request,
                    "transportSchema": rendered.output_schema,
                    "normalizedSchema": rendered.normalized_schema,
                    "modelProfile": profile.model_dump(mode="json"),
                }
            )
        self._write(record)
        if self.mode == "snapshot":
            self._write_model_call_artifacts(
                rendered,
                artifact_dir,
                provider=provider,
                profile=profile,
            )

    def record_reviewer_output(
        self,
        *,
        view: ReviewerView,
        payload: dict[str, Any] | None,
        review: ReviewResult | None,
        validation_error: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        provider_retry_count: int,
    ) -> None:
        if self.mode == "off":
            return
        record: dict[str, Any] = {
            "event": "reviewer_output",
            "caseId": view.case_id,
            "validationError": validation_error,
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "providerRetryCount": provider_retry_count,
            "hasPayload": payload is not None,
            "payloadHash": _json_sha256(payload) if payload is not None else None,
            "reviewResultHash": (
                _json_sha256(review.model_dump(mode="json"))
                if review is not None
                else None
            ),
            "verdict": review.verdict if review is not None else None,
            "findingCount": len(review.findings) if review is not None else 0,
        }
        if self.mode == "snapshot":
            record.update(
                {
                    "payload": payload,
                    "reviewResult": (
                        review.model_dump(mode="json")
                        if review is not None
                        else None
                    ),
                }
            )
        self._write(record)

    def record_reviewer_timeout(self, *, view: ReviewerView, reason: str) -> None:
        if self.mode == "off":
            return
        self._write(
            {
                "event": "reviewer_timeout",
                "caseId": view.case_id,
                "reason": reason,
            }
        )

    def record_reviewer_contract_violation(
        self,
        *,
        view: ReviewerView,
        review: ReviewResult,
        violation: str,
    ) -> None:
        if self.mode == "off":
            return
        record: dict[str, Any] = {
            "event": "reviewer_contract_violation",
            "caseId": view.case_id,
            "violation": violation,
            "reviewResultHash": _json_sha256(review.model_dump(mode="json")),
            "verdict": review.verdict,
            "findingCount": len(review.findings),
        }
        if self.mode == "snapshot":
            record["reviewResult"] = review.model_dump(mode="json")
        self._write(record)

    def record_reviewer_result_applied(
        self,
        *,
        state_before: CaseState,
        state_after: CaseState,
        view: ReviewerView,
        review: ReviewResult,
    ) -> None:
        if self.mode == "off":
            return
        record: dict[str, Any] = {
            "event": "reviewer_result_applied",
            "caseId": view.case_id,
            "reviewResultHash": _json_sha256(review.model_dump(mode="json")),
            "verdict": review.verdict,
            "findingIds": [item.finding_id for item in review.findings],
            "stateBeforeStatus": _state_status(state_before),
            "stateAfterStatus": _state_status(state_after),
        }
        if self.mode == "snapshot":
            record.update(
                {
                    "reviewerView": view.model_dump(mode="json"),
                    "reviewResult": review.model_dump(mode="json"),
                    "caseStateBefore": state_before.model_dump(mode="json"),
                    "caseStateAfter": state_after.model_dump(mode="json"),
                }
            )
        self._write(record)

    def record_transport_input(
        self,
        *,
        context: SolverContext,
        profile: ModelCallProfile,
        rendered: RenderedModelCall,
        repair_index: int,
        transport_stage: str = "solver",
        provider: str | None = None,
        work_item_session_id: str | None = None,
        work_item_session_turn: int | None = None,
    ) -> None:
        if self.mode == "off":
            return
        schema_json = json.dumps(
            rendered.output_schema,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        artifact_dir = self._artifact_root / (
            f"{transport_stage}-attempt-{repair_index + 1}-"
            f"{rendered.request_hash[:12]}"
        )
        record: dict[str, Any] = {
            "event": "transport_input",
            "caseId": context.case_id,
            "transportAttempt": repair_index + 1,
            "transportStage": transport_stage,
            "modelCallStage": rendered.stage,
            "model": profile.model,
            "requestedReasoningEffort": profile.reasoning_effort,
            **_effective_reasoning_settings(provider, profile),
            "promptChars": len(rendered.request),
            "schemaChars": len(schema_json),
            "promptHash": rendered.request_hash,
            "schemaHash": rendered.output_schema_hash,
            "instructionsHash": rendered.instructions_hash,
            "inputHash": rendered.input_hash,
            "normalizedSchemaHash": rendered.normalized_schema_hash,
            "systemPromptHash": _text_sha256(profile.system_prompt),
            "profileName": self._profile_name,
            "profileVersion": self._profile_version,
            "promptBuilder": (
                "app.adapters.models.structured_json:_search_reselection_prompt"
                if transport_stage == "search_reselection"
                else (
                    "app.adapters.models.structured_json:_search_review_prompt"
                    if transport_stage == "search_assessment"
                    else "app.adapters.models.structured_json:_solver_prompt"
                )
            ),
            "promptAssets": list(rendered.prompt_assets),
            "artifactPath": str(artifact_dir),
            "cycleNo": max(1, context.research_cycle_count),
            "scope": _context_scope(context),
        }
        if work_item_session_id is not None:
            record["workItemSessionId"] = work_item_session_id
        if work_item_session_turn is not None:
            record["workItemSessionTurn"] = work_item_session_turn
        if self.mode == "snapshot":
            record.update(
                {
                    "completeRequestPath": str(
                        artifact_dir / "complete_request.json"
                    ),
                    "instructions": rendered.instructions,
                    "inputPayload": rendered.input_payload,
                    "prompt": rendered.request,
                    "transportSchema": rendered.output_schema,
                    "normalizedSchema": rendered.normalized_schema,
                }
            )
        self._write(record)
        if self.mode == "snapshot":
            self._write_model_call_artifacts(
                rendered,
                artifact_dir,
                provider=provider,
                profile=profile,
            )

    def _write_model_call_artifacts(
        self,
        rendered: RenderedModelCall,
        output_dir: Path,
        *,
        provider: str | None,
        profile: ModelCallProfile,
    ) -> None:
        try:
            write_model_call_artifacts(
                rendered,
                output_dir,
                provider=provider or "unknown",
                profile_name=self._profile_name or "unknown",
                profile_version=self._profile_version or "unknown",
                model=profile.model,
                requested_reasoning_effort=profile.reasoning_effort,
                effective_reasoning_mode=_effective_reasoning_mode(
                    provider, profile.model, profile.reasoning_effort
                ),
                effective_reasoning_effort=_effective_reasoning_effort(
                    provider, profile
                ),
                thinking_budget_tokens=_effective_thinking_budget_tokens(
                    provider, profile
                ),
            )
        except OSError:
            logger.warning(
                "failed to write model call artifacts: %s",
                output_dir,
                exc_info=True,
            )

    def record_transport_output(
        self,
        *,
        context: SolverContext,
        repair_index: int,
        payload: dict[str, Any] | None,
        validation_error: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        provider_retry_count: int,
        latency_ms: int | None = None,
        transport_stage: str = "solver",
        work_item_session_id: str | None = None,
        work_item_session_turn: int | None = None,
    ) -> None:
        if self.mode == "off":
            return
        record: dict[str, Any] = {
            "event": "transport_output",
            "caseId": context.case_id,
            "transportAttempt": repair_index + 1,
            "transportStage": transport_stage,
            "validationError": validation_error,
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "latencyMs": latency_ms,
            "providerRetryCount": provider_retry_count,
            "hasPayload": payload is not None,
            "payloadHash": _json_sha256(payload) if payload is not None else None,
            "cycleNo": max(1, context.research_cycle_count),
            "scope": _context_scope(context),
        }
        if work_item_session_id is not None:
            record["workItemSessionId"] = work_item_session_id
        if work_item_session_turn is not None:
            record["workItemSessionTurn"] = work_item_session_turn
        if self.mode == "snapshot":
            record["payload"] = payload
        self._write(record)

    def record_transport_timeout(
        self,
        *,
        context: SolverContext,
        repair_index: int,
        reason: str,
        transport_stage: str = "solver",
        work_item_session_id: str | None = None,
        work_item_session_turn: int | None = None,
    ) -> None:
        if self.mode == "off":
            return
        record: dict[str, Any] = {
            "event": "transport_timeout",
            "caseId": context.case_id,
            "transportAttempt": repair_index + 1,
            "transportStage": transport_stage,
            "reason": reason,
            "cycleNo": max(1, context.research_cycle_count),
            "scope": _context_scope(context),
        }
        if work_item_session_id is not None:
            record["workItemSessionId"] = work_item_session_id
        if work_item_session_turn is not None:
            record["workItemSessionTurn"] = work_item_session_turn
        self._write(record)

    def _write(self, record: dict[str, Any]) -> int | None:
        try:
            with self._lock:
                self._sequence += 1
                record = {
                    "sequence": self._sequence,
                    "recordedAt": datetime.now(timezone.utc).isoformat(),
                    "runElapsedMs": max(
                        0,
                        int((monotonic() - self._started_at) * 1000),
                    ),
                    **record,
                }
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as output:
                    output.write(
                        json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                    )
                    output.write("\n")
                return self._sequence
        except OSError:
            # 診断出力の失敗で法令検索本体を失敗させない。
            logger.warning(
                "failed to write agent diagnostics: %s",
                self._path,
                exc_info=True,
            )
            return None


def load_diagnostic_records(
    output_dir: Path, case_id: str
) -> tuple[dict[str, Any], ...]:
    """検証済みcase IDの診断JSONLを読み、壊れたrecordを監査失敗として拒否する。"""

    if not _CASE_ID_PATTERN.fullmatch(case_id):
        raise ValueError("invalid diagnostic case ID")
    path = output_dir / "agent-framework-diagnostics" / f"{case_id}.jsonl"
    if not path.is_file():
        raise FileNotFoundError(path)
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid diagnostic JSONL at line {line_number}") from exc
        if not isinstance(record, dict):
            raise TypeError(f"diagnostic record {line_number} is not an object")
        records.append(record)
    return tuple(records)


def _state_status(state: CaseState) -> dict[str, Any]:
    replaced_ids = {
        item.replaces_hypothesis_id
        for item in state.hypotheses
        if item.replaces_hypothesis_id is not None
    }
    return {
        "runStatus": state.run_status,
        "stopReason": state.stop_reason,
        "researchCycleCount": state.research_cycle_count,
        "workItems": [
            {"workItemId": item.work_item_id, "state": item.state}
            for item in state.work_items
        ],
        "hypotheses": [
            {
                "hypothesisId": item.hypothesis_id,
                "workItemId": item.work_item_id,
                "judgment": item.judgment,
                "evidenceCount": len(item.evidence_ids),
                "replacesHypothesisId": item.replaces_hypothesis_id,
                "active": item.hypothesis_id not in replaced_ids,
            }
            for item in state.hypotheses
        ],
        "evidenceCount": len(state.evidence),
        "toolRequestCount": len(state.tool_requests),
        "toolResultCount": len(state.tool_results),
        "graphReviewCount": len(state.graph_candidate_reviews),
        "searchReviewCount": len(state.search_candidate_reviews),
        "reviewVerdict": state.review.verdict if state.review is not None else None,
        "reviewFindingCount": (
            len(state.review.findings) if state.review is not None else 0
        ),
        "reviewFindingResolutionCount": len(state.review_finding_resolutions),
    }


def _context_status(context: SolverContext) -> dict[str, Any]:
    return {
        "researchCycleCount": context.research_cycle_count,
        "remainingResearchCycles": context.remaining_research_cycles,
        "remainingWallTimeSec": context.remaining_wall_time_sec,
        "remainingFetchCapacity": context.remaining_fetch_capacity,
        "cycleCloseRequired": context.cycle_close_required,
        "canStartNextCycle": context.can_start_next_cycle,
        "finalizeOnly": context.finalize_only,
        "workItemCount": len(context.work_tree),
        "hypothesisCount": len(context.hypotheses),
        "hypothesisExplorationSets": [
            item.model_dump(mode="json")
            for item in context.hypothesis_exploration_sets
        ],
        "materialEvidenceCount": len(context.material_evidence),
        "materialEvidenceChars": sum(
            len(item.content) for item in context.material_evidence
        ),
        "recentToolResultCount": len(context.recent_tool_results),
        "searchCandidateCount": len(context.search_candidates),
        "requiredSearchReviewRequestCount": len(
            context.required_search_review_request_ids
        ),
        "graphReviewBatchCount": len(context.graph_review_batch.candidates),
        "graphReviewLedgerCount": len(context.graph_review_ledger),
        "requiredDependencyWorkItemCount": len(
            context.required_dependency_work_item_ids
        ),
        "contractViolation": (
            context.contract_feedback.violation
            if context.contract_feedback is not None
            else None
        ),
        "actionFeedbackCode": (
            context.action_feedback.code
            if context.action_feedback is not None
            else None
        ),
    }


def _decision_status(decision: SolverDecision) -> dict[str, Any]:
    return {
        "next": decision.next,
        "startNextCycle": decision.start_next_cycle,
        "addedWorkItemCount": len(decision.update.add_work_items),
        "updatedWorkItemCount": len(decision.update.update_work_items),
        "addedHypothesisCount": len(decision.update.add_hypotheses),
        "updatedHypothesisCount": len(decision.update.update_hypotheses),
        "dependencyDecisionCount": len(decision.dependency_decisions),
        "reviewFindingResolutionCount": len(
            decision.review_finding_resolutions
        ),
        "toolRequestCount": len(decision.tool_requests),
        "hasGraphCandidateReview": decision.graph_candidate_review is not None,
        "hasSearchCandidateReview": decision.search_candidate_review is not None,
        "hasAnswer": decision.answer is not None,
    }


def _context_scope(context: SolverContext) -> dict[str, list[str]]:
    return {
        "workItemIds": list(
            dict.fromkeys(item.work_item_id for item in context.work_tree)
        ),
        "hypothesisIds": list(
            dict.fromkeys(item.hypothesis_id for item in context.hypotheses)
        ),
    }


def _decision_scope(decision: SolverDecision) -> dict[str, list[str]]:
    work_item_ids = [
        *(item.work_item_id for item in decision.update.add_work_items),
        *(item.work_item_id for item in decision.update.update_work_items),
        *(item.work_item_id for item in decision.dependency_decisions),
        *(item.work_item_id for item in decision.tool_requests),
    ]
    hypothesis_ids = [
        *(item.hypothesis_id for item in decision.update.add_hypotheses),
        *(item.hypothesis_id for item in decision.update.update_hypotheses),
        *(
            hypothesis_id
            for request in decision.tool_requests
            for hypothesis_id in request.hypothesis_ids
        ),
    ]
    return {
        "workItemIds": list(dict.fromkeys(work_item_ids)),
        "hypothesisIds": list(dict.fromkeys(hypothesis_ids)),
    }


def _effective_reasoning_mode(
    provider: str | None,
    model: str,
    reasoning_effort: str | None = None,
) -> str:
    normalized = model.lower().replace("_", "-")
    if provider == "anthropic" and reasoning_effort == "none":
        return "disabled"
    if provider == "openai":
        return "reasoning_effort"
    if provider == "anthropic" and "haiku-4-5" in normalized:
        return "manual_extended_thinking"
    if provider == "anthropic" and "sonnet-4-6" in normalized:
        return "adaptive_thinking_with_effort"
    return "provider_default"


def _effective_reasoning_effort(
    provider: str | None,
    profile: ModelCallProfile,
) -> str | None:
    mode = _effective_reasoning_mode(
        provider, profile.model, profile.reasoning_effort
    )
    if mode in {"reasoning_effort", "adaptive_thinking_with_effort"}:
        return profile.reasoning_effort
    return None


def _effective_thinking_budget_tokens(
    provider: str | None,
    profile: ModelCallProfile,
) -> int | None:
    if (
        _effective_reasoning_mode(
            provider, profile.model, profile.reasoning_effort
        )
        != "manual_extended_thinking"
    ):
        return None
    budget = min(
        settings.anthropic_thinking_budget_tokens,
        profile.max_output_tokens - 1024,
    )
    return budget if budget >= 1024 else None


def _effective_reasoning_settings(
    provider: str | None,
    profile: ModelCallProfile,
) -> dict[str, Any]:
    return {
        "effectiveReasoningMode": _effective_reasoning_mode(
            provider, profile.model, profile.reasoning_effort
        ),
        "effectiveReasoningEffort": _effective_reasoning_effort(provider, profile),
        "thinkingBudgetTokens": _effective_thinking_budget_tokens(provider, profile),
    }


def _text_sha256(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _json_sha256(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return _text_sha256(canonical)
