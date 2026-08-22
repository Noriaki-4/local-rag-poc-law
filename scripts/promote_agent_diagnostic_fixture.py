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
    parser.add_argument("--fixture-id", required=True)
    parser.add_argument("--question-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def _load_record(path: Path, sequence: int) -> dict[str, Any]:
    with path.open(encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            if record.get("sequence") == sequence:
                if record.get("event") != "solver_input":
                    raise ValueError("selected sequence is not a solver_input event")
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
    record = _load_record(args.input, args.sequence)
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
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(fixture, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
