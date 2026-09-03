"""保存済みAgent診断Snapshotを説明する、読み取り専用の事後監査。"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.agent_framework.diagnostics import load_diagnostic_records
from app.config import settings
from app.domains.legal import legal_agent_profile
from app.domains.legal.model_routing import legal_model_for
from app.llm import LLMClient
from app.models import FrameworkAuditRequest, FrameworkAuditResponse


class PostRunAuditDisabledError(RuntimeError):
    pass


class AuditSnapshotNotFoundError(FileNotFoundError):
    pass


class AuditSnapshotInvalidError(ValueError):
    pass


class AuditContextCapacityError(ValueError):
    pass


class AuditModelProtocolError(ValueError):
    pass


class _AuditModelOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    explanation: str = Field(min_length=1)
    recorded_facts: list[str] = Field(default_factory=list)
    inferences: list[str] = Field(default_factory=list)
    source_decision_sequences: list[int] = Field(min_length=1)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_sources(self) -> _AuditModelOutput:
        if len(self.source_decision_sequences) != len(
            set(self.source_decision_sequences)
        ):
            raise ValueError("source decision sequences must be unique")
        return self


class FrameworkPostRunAuditService:
    def __init__(self, llm_client: LLMClient) -> None:
        self._llm_client = llm_client

    def audit(self, request: FrameworkAuditRequest) -> FrameworkAuditResponse:
        if settings.agent_framework_post_run_audit != "on_demand":
            raise PostRunAuditDisabledError("post-run audit is disabled")

        try:
            records = load_diagnostic_records(
                settings.eval_results_dir,
                request.caseId,
            )
        except FileNotFoundError as exc:
            raise AuditSnapshotNotFoundError(request.caseId) from exc
        except (TypeError, ValueError) as exc:
            raise AuditSnapshotInvalidError(str(exc)) from exc

        applied = tuple(
            record for record in records if record.get("event") == "decision_applied"
        )
        complete = next(
            (
                record
                for record in reversed(records)
                if record.get("event") == "run_complete"
            ),
            None,
        )
        if not applied or complete is None:
            raise AuditSnapshotInvalidError(
                "snapshot has no applied decision or run completion record"
            )
        if "caseState" not in complete:
            raise AuditSnapshotInvalidError(
                "post-run audit requires a snapshot-mode diagnostic run"
            )

        target = self._target_decision(applied, request.decisionSequence)
        required_target_fields = {
            "solverContext",
            "solverDecision",
            "caseStateBefore",
            "caseStateAfter",
        }
        if not required_target_fields.issubset(target):
            raise AuditSnapshotInvalidError(
                "target decision lacks snapshot-mode decision material"
            )

        sequences = tuple(int(record["sequence"]) for record in applied)
        view = _audit_view(applied, target, complete)
        prompt = _audit_prompt(request.inquiry, view)
        if len(prompt) > settings.agent_framework_max_solver_input_chars:
            raise AuditContextCapacityError(
                "post-run audit prompt exceeds AGENT_FRAMEWORK_MAX_SOLVER_INPUT_CHARS"
            )

        profile = legal_agent_profile().solver_integration
        audit_model = legal_model_for("post_run_audit")
        schema = _audit_schema(sequences)
        result = self._llm_client.generate_structured_json(
            prompt=prompt,
            schema=schema,
            model=audit_model,
            max_tokens=min(
                profile.max_output_tokens,
                settings.agent_framework_post_run_audit_max_tokens,
            ),
            timeout_sec=max(1, round(profile.timeout_sec)),
        )
        if result.validationError or result.payload is None:
            raise AuditModelProtocolError(
                f"post-run audit transport invalid: {result.validationError or 'empty'}"
            )
        try:
            output = _AuditModelOutput.model_validate(result.payload)
        except ValidationError as exc:
            raise AuditModelProtocolError(
                "post-run audit result violates schema"
            ) from exc
        unknown_sequences = set(output.source_decision_sequences).difference(sequences)
        if unknown_sequences:
            raise AuditModelProtocolError(
                "post-run audit cited an unknown decision sequence"
            )

        target_sequence = int(target["sequence"])
        return FrameworkAuditResponse(
            caseId=request.caseId,
            decisionSequence=target_sequence,
            recordedDecisionReason=str(target.get("decisionReason") or ""),
            explanation=output.explanation,
            recordedFacts=output.recorded_facts,
            inferences=output.inferences,
            sourceDecisionSequences=output.source_decision_sequences,
            limitations=output.limitations,
            model=audit_model,
            inputTokens=result.inputTokens,
            outputTokens=result.outputTokens,
        )

    @staticmethod
    def _target_decision(
        applied: tuple[dict[str, Any], ...],
        requested_sequence: int | None,
    ) -> dict[str, Any]:
        if requested_sequence is None:
            return applied[-1]
        target = next(
            (
                record
                for record in applied
                if record.get("sequence") == requested_sequence
            ),
            None,
        )
        if target is None:
            raise AuditSnapshotInvalidError(
                f"applied decision sequence not found: {requested_sequence}"
            )
        return target


def _audit_view(
    applied: tuple[dict[str, Any], ...],
    target: dict[str, Any],
    complete: dict[str, Any],
) -> dict[str, Any]:
    final_state = complete["caseState"]
    return {
        "run": {
            "stateStatus": complete.get("stateStatus"),
            "failureCode": complete.get("failureCode"),
            "finalAnswer": final_state.get("final_answer"),
        },
        "decisionLedger": [
            {
                "sequence": record.get("sequence"),
                "purpose": record.get("purpose"),
                "decisionReason": record.get("decisionReason"),
                "decisionStatus": record.get("decisionStatus"),
            }
            for record in applied
        ],
        "targetDecision": {
            "sequence": target.get("sequence"),
            "purpose": target.get("purpose"),
            "recordedDecisionReason": target.get("decisionReason"),
            # SolverContextを当時LLMが見た正本とし、同じEvidenceを
            # CaseState before/afterから二重に渡さない。
            "stateBeforeStatus": target.get("stateBeforeStatus"),
            "solverContext": target.get("solverContext"),
            "solverDecision": target.get("solverDecision"),
            "stateAfterStatus": target.get("stateAfterStatus"),
        },
    }


def _audit_prompt(inquiry: str, view: dict[str, Any]) -> str:
    payload = json.dumps(view, ensure_ascii=False, separators=(",", ":"))
    return (
        "あなたはAgent実行後の読み取り専用監査役です。新しい法的判断や回答修正を行わず、"
        "保存された実行記録だけから利用者の問い合わせに答えてください。"
        "これは内部思考の復元ではありません。decisionReasonや状態・IDに明記された内容は"
        "recorded_factsへ、記録を組み合わせた事後的な説明はinferencesへ分けます。"
        "確認できないことはlimitationsへ書き、推測を事実として扱いません。"
        "source_decision_sequencesには実際に参照した既知sequenceだけを指定してください。\n"
        f"<inquiry>{inquiry}</inquiry>"
        f"<audit_view>{payload}</audit_view>"
    )


def _audit_schema(sequences: tuple[int, ...]) -> dict[str, Any]:
    string_array = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "explanation": {"type": "string"},
            "recorded_facts": string_array,
            "inferences": string_array,
            "source_decision_sequences": {
                "type": "array",
                "items": {"type": "integer", "enum": list(sequences)},
                "minItems": 1,
            },
            "limitations": string_array,
        },
        "required": [
            "explanation",
            "recorded_facts",
            "inferences",
            "source_decision_sequences",
            "limitations",
        ],
    }
