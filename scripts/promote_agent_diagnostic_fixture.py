"""診断JSONLのSolver入力を、外部LLM不要の回帰fixtureへ固定する。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--sequence", required=True, type=int)
    parser.add_argument("--output-sequence", type=int)
    parser.add_argument("--transport-input-sequence", type=int)
    parser.add_argument("--transport-output-sequence", type=int)
    parser.add_argument("--violation-sequence", type=int)
    parser.add_argument("--fixture-id", required=True)
    parser.add_argument("--question-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def _load_record(
    path: Path,
    sequence: int,
    *,
    expected_event: str | None = None,
) -> dict[str, Any]:
    with path.open(encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            if record.get("sequence") == sequence:
                if (
                    expected_event is not None
                    and record.get("event") != expected_event
                ):
                    raise ValueError(
                        f"sequence {sequence} is not a {expected_event} event"
                    )
                return record
    raise ValueError(f"sequence {sequence} was not found")


def _replace_case_id(value: Any, source_id: str, fixture_id: str) -> Any:
    if isinstance(value, dict):
        return {
            key: _replace_case_id(item, source_id, fixture_id)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_case_id(item, source_id, fixture_id) for item in value]
    if value == source_id:
        return fixture_id
    return value


def main() -> None:
    args = _parse_args()
    record = _load_record(
        args.input,
        args.sequence,
        expected_event="solver_input",
    )
    source_case_id = record["caseId"]
    fixture_case_id = f"fixture-{args.fixture_id}"
    case_state = _replace_case_id(
        record["caseState"], source_case_id, fixture_case_id
    )
    solver_context = _replace_case_id(
        record["solverContext"], source_case_id, fixture_case_id
    )
    evidence_role_counts: dict[str, int] = {}
    for evidence in solver_context["material_evidence"]:
        role = evidence["metadata"].get("evidenceRole", "unknown")
        evidence_role_counts[role] = evidence_role_counts.get(role, 0) + 1
    fixture = {
        "fixtureId": args.fixture_id,
        "questionId": args.question_id,
        "source": {
            "diagnosticsMode": "snapshot",
            "diagnosticCaseId": source_case_id,
            "sequence": record["sequence"],
            "purpose": record["purpose"],
            "model": record["model"],
            "profileName": record.get("profileName"),
            "profileVersion": record.get("profileVersion"),
        },
        "expectations": {
            "researchCycleCount": solver_context["research_cycle_count"],
            "remainingFetchCapacity": solver_context["remaining_fetch_capacity"],
            "workItemCount": len(solver_context["work_tree"]),
            "hypothesisCount": len(solver_context["hypotheses"]),
            "materialEvidenceCount": len(solver_context["material_evidence"]),
            "recentToolResultCount": len(solver_context["recent_tool_results"]),
            "fetchableArticleCount": len(
                solver_context["fetchable_article_ids"]
            ),
            "fetchedResourceCount": len(
                solver_context["fetched_resource_ids_this_cycle"]
            ),
            "evidenceRoleCounts": evidence_role_counts,
        },
        "caseState": case_state,
        "solverContext": solver_context,
    }
    if args.output_sequence is not None:
        output_record = _load_record(
            args.input,
            args.output_sequence,
            expected_event="solver_output",
        )
        if output_record.get("purpose") != record.get("purpose"):
            raise ValueError("solver input and output purposes do not match")
        fixture["source"]["outputSequence"] = args.output_sequence
        fixture["source"]["decisionHash"] = output_record.get(
            "solverDecisionHash"
        )
        fixture["observedSolverDecision"] = _replace_case_id(
            output_record["solverDecision"],
            source_case_id,
            fixture_case_id,
        )
    if args.transport_input_sequence is not None:
        transport_input = _load_record(
            args.input,
            args.transport_input_sequence,
            expected_event="transport_input",
        )
        if transport_input.get("caseId") != source_case_id:
            raise ValueError("solver and transport input case IDs do not match")
        fixture["source"].update(
            {
                "transportInputSequence": args.transport_input_sequence,
                "transportStage": transport_input.get("transportStage"),
                "transportAttempt": transport_input.get("transportAttempt"),
                "promptHash": transport_input.get("promptHash"),
                "schemaHash": transport_input.get("schemaHash"),
                "instructionsHash": transport_input.get("instructionsHash"),
                "inputHash": transport_input.get("inputHash"),
                "normalizedSchemaHash": transport_input.get(
                    "normalizedSchemaHash"
                ),
            }
        )
        fixture["observedTransportInput"] = {
            "instructions": transport_input["instructions"],
            "inputPayload": _replace_case_id(
                transport_input["inputPayload"],
                source_case_id,
                fixture_case_id,
            ),
            "transportSchema": transport_input["transportSchema"],
            "normalizedSchema": transport_input["normalizedSchema"],
        }
    if args.transport_output_sequence is not None:
        transport_output = _load_record(
            args.input,
            args.transport_output_sequence,
            expected_event="transport_output",
        )
        if transport_output.get("caseId") != source_case_id:
            raise ValueError("solver and transport output case IDs do not match")
        transport_stage = fixture["source"].get("transportStage")
        if (
            transport_stage is not None
            and transport_output.get("transportStage") != transport_stage
        ):
            raise ValueError("transport input and output stages do not match")
        fixture["source"].update(
            {
                "transportOutputSequence": args.transport_output_sequence,
                "payloadHash": transport_output.get("payloadHash"),
            }
        )
        fixture["observedTransportOutput"] = {
            "payload": _replace_case_id(
                transport_output.get("payload"),
                source_case_id,
                fixture_case_id,
            ),
            "validationError": transport_output.get("validationError"),
            "inputTokens": transport_output.get("inputTokens"),
            "outputTokens": transport_output.get("outputTokens"),
            "providerRetryCount": transport_output.get(
                "providerRetryCount"
            ),
        }
    if args.violation_sequence is not None:
        violation_record = _load_record(
            args.input,
            args.violation_sequence,
            expected_event="contract_violation",
        )
        if violation_record.get("purpose") != record.get("purpose"):
            raise ValueError("solver input and violation purposes do not match")
        decision_hash = fixture["source"].get("decisionHash")
        if (
            decision_hash is not None
            and violation_record.get("solverDecisionHash") != decision_hash
        ):
            raise ValueError("solver output and violation decision hashes do not match")
        fixture["source"]["violationSequence"] = args.violation_sequence
        fixture["expectedViolation"] = violation_record["violation"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(fixture, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
