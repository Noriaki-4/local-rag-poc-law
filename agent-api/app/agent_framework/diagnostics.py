"""明示的に有効化した実行だけ、Agent判断材料をローカルへ記録する。"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import Any, Literal

from .context import SolverContext
from .contracts import SolverDecision
from .profiles import ModelCallProfile
from .prompt_assets import PromptAssetTrace
from .state import CaseState

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
        self._sequence = 0
        self._applied_decision_sequences: list[int] = []
        self._lock = RLock()
        self._profile_name = profile_name
        self._profile_version = profile_version

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
            "maxOutputTokens": profile.max_output_tokens,
            "timeoutSec": profile.timeout_sec,
            "profileName": self._profile_name,
            "profileVersion": self._profile_version,
        }
        if self.mode == "snapshot":
            record.update(
                {
                    "caseState": state.model_dump(mode="json"),
                    "solverContext": context.model_dump(mode="json"),
                    "modelProfile": profile.model_dump(mode="json"),
                }
            )
        self._write(record)

    def record_solver_output(
        self,
        *,
        state: CaseState,
        purpose: str,
        contract_attempt: int,
        decision: SolverDecision,
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
        }
        if self.mode == "snapshot":
            record["solverDecision"] = decision.model_dump(mode="json")
        self._write(record)

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

    def record_run_complete(
        self,
        *,
        state: CaseState,
        failure_code: str | None,
    ) -> None:
        if self.mode == "off":
            return
        record: dict[str, Any] = {
            "event": "run_complete",
            "caseId": state.case_id,
            "stateStatus": _state_status(state),
            "failureCode": failure_code,
            "appliedDecisionSequences": list(self._applied_decision_sequences),
        }
        if self.mode == "snapshot":
            record["caseState"] = state.model_dump(mode="json")
        self._write(record)

    def record_transport_input(
        self,
        *,
        context: SolverContext,
        profile: ModelCallProfile,
        prompt: str,
        schema: dict[str, Any],
        repair_index: int,
        prompt_assets: Sequence[PromptAssetTrace] = (),
    ) -> None:
        if self.mode == "off":
            return
        schema_json = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        record: dict[str, Any] = {
            "event": "transport_input",
            "caseId": context.case_id,
            "transportAttempt": repair_index + 1,
            "model": profile.model,
            "promptChars": len(prompt),
            "schemaChars": len(schema_json),
            "promptHash": _text_sha256(prompt),
            "schemaHash": _json_sha256(schema),
            "systemPromptHash": _text_sha256(profile.system_prompt),
            "profileName": self._profile_name,
            "profileVersion": self._profile_version,
            "promptBuilder": "app.adapters.models.structured_json:_solver_prompt",
            "promptAssets": list(prompt_assets),
        }
        if self.mode == "snapshot":
            record.update({"prompt": prompt, "transportSchema": schema})
        self._write(record)

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
    ) -> None:
        if self.mode == "off":
            return
        record: dict[str, Any] = {
            "event": "transport_output",
            "caseId": context.case_id,
            "transportAttempt": repair_index + 1,
            "validationError": validation_error,
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "providerRetryCount": provider_retry_count,
            "hasPayload": payload is not None,
            "payloadHash": _json_sha256(payload) if payload is not None else None,
        }
        if self.mode == "snapshot":
            record["payload"] = payload
        self._write(record)

    def record_transport_timeout(
        self,
        *,
        context: SolverContext,
        repair_index: int,
        reason: str,
    ) -> None:
        if self.mode == "off":
            return
        self._write(
            {
                "event": "transport_timeout",
                "caseId": context.case_id,
                "transportAttempt": repair_index + 1,
                "reason": reason,
            }
        )

    def _write(self, record: dict[str, Any]) -> int | None:
        try:
            with self._lock:
                self._sequence += 1
                record = {"sequence": self._sequence, **record}
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
            }
            for item in state.hypotheses
        ],
        "evidenceCount": len(state.evidence),
        "toolRequestCount": len(state.tool_requests),
        "toolResultCount": len(state.tool_results),
        "graphReviewCount": len(state.graph_candidate_reviews),
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
        "materialEvidenceCount": len(context.material_evidence),
        "materialEvidenceChars": sum(
            len(item.content) for item in context.material_evidence
        ),
        "recentToolResultCount": len(context.recent_tool_results),
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
        "toolRequestCount": len(decision.tool_requests),
        "hasGraphCandidateReview": decision.graph_candidate_review is not None,
        "hasAnswer": decision.answer is not None,
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
