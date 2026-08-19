"""Score a blind adjudication artifact against hidden predicate and direction gold."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

PREDICATES = (
    "IMPLEMENTS",
    "INCORPORATES",
    "USES_DEFINITION",
    "EXCEPTION_TO",
    "OVERRIDES",
)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _eligible_gold(path: Path) -> dict[str, dict[str, Any]]:
    return {
        str(row["basisEdgeId"]): row
        for row in _load_jsonl(path)
        if row.get("expectedResolutionStatus") == "resolved"
        and row.get("expectedReferenceTargetArticleId")
        == row.get("currentReferenceTargetArticleId")
        and row.get("expectedPredicates") is not None
    }


def _semantic_directions(
    path: Path, eligible_basis_ids: set[str]
) -> dict[str, set[tuple[str, str, str]]]:
    directions: dict[str, set[tuple[str, str, str]]] = {}
    for row in _load_jsonl(path):
        basis_id = str(row.get("basisEdgeId") or "")
        if basis_id not in eligible_basis_ids:
            continue
        decision = row.get("semanticDecision")
        if not isinstance(decision, dict):
            continue
        assertions = decision.get("assertions")
        if not isinstance(assertions, list):
            raise ValueError(f"{basis_id}: semanticDecision.assertions must be a list")
        directions[basis_id] = {
            (
                str(assertion["proposedPredicate"]),
                str(assertion["subjectArticleId"]),
                str(assertion["objectArticleId"]),
            )
            for assertion in assertions
        }
    if set(directions) != eligible_basis_ids:
        missing = sorted(eligible_basis_ids - set(directions))
        extra = sorted(set(directions) - eligible_basis_ids)
        raise ValueError(
            f"gold audit semantic basis set mismatch: missing={missing}, extra={extra}"
        )
    return directions


def _finding_from_conditions(first: str, second: str) -> str:
    if first == second == "established":
        return "established"
    if "not_established" in (first, second):
        return "not_established"
    return "uncertain"


def _structural_errors(
    output: dict[str, Any], packet: dict[str, Any]
) -> list[str]:
    errors: list[str] = []
    if output.get("candidateKey") != packet.get("candidateKey"):
        errors.append("candidate_key_mismatch")
    if output.get("basisEdgeId") != packet.get("basisEdgeId"):
        errors.append("basis_edge_id_mismatch")
    assessments = output.get("predicateAssessments")
    if not isinstance(assessments, dict) or set(assessments) != set(PREDICATES):
        return [*errors, "predicate_assessment_coverage"]
    established: set[str] = set()
    uncertain = False
    for predicate in PREDICATES:
        item = assessments[predicate]
        if not isinstance(item, dict):
            errors.append(f"{predicate}:invalid_assessment")
            continue
        first = item.get("firstCondition")
        second = item.get("secondCondition")
        finding = item.get("finding")
        allowed = {"established", "not_established", "uncertain"}
        if first not in allowed or second not in allowed or finding not in allowed:
            errors.append(f"{predicate}:invalid_finding")
            continue
        if finding != _finding_from_conditions(first, second):
            errors.append(f"{predicate}:condition_algebra")
        if finding == "established":
            established.add(predicate)
        uncertain = uncertain or finding == "uncertain"
    expected_status = "needs_review" if uncertain else "accepted"
    if output.get("adjudicationStatus") != expected_status:
        errors.append("adjudication_status_mismatch")

    source = packet["referenceSourceArticle"]
    target = packet["referenceTargetArticle"]
    article_ids = {source["articleId"], target["articleId"]}
    occurrence_by_hash = {
        item["occurrenceHash"]: item for item in packet["referenceOccurrences"]
    }
    target_span_ids = {item["spanId"] for item in target["spans"]}
    assertions = output.get("assertions")
    if not isinstance(assertions, list):
        return [*errors, "assertions_not_list"]
    assertion_predicates = {
        str(item.get("proposedPredicate")) for item in assertions
    }
    if assertion_predicates != established or len(assertions) != len(established):
        errors.append("assertion_predicate_coverage")
    for assertion in assertions:
        occurrence = occurrence_by_hash.get(assertion.get("referenceOccurrenceHash"))
        if occurrence is None:
            errors.append("unknown_occurrence_hash")
            continue
        if {
            assertion.get("subjectArticleId"),
            assertion.get("objectArticleId"),
        } != article_ids:
            errors.append("invalid_assertion_endpoints")
        if assertion.get("referenceSourceSupportingSpanId") not in set(
            occurrence["sourceSpanIds"]
        ):
            errors.append("invalid_source_span")
        if assertion.get("referenceTargetSupportingSpanId") not in target_span_ids:
            errors.append("invalid_target_span")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--gold-audit",
        type=Path,
        help="Optional adjudication audit used to score semantic SUBJECT/OBJECT direction",
    )
    args = parser.parse_args()
    gold = _eligible_gold(args.gold)
    gold_directions = (
        _semantic_directions(args.gold_audit, set(gold))
        if args.gold_audit is not None
        else None
    )
    packets = {
        str(row["basisEdgeId"]): row for row in _load_jsonl(args.packet)
    }
    outputs = {
        str(row["basisEdgeId"]): row for row in _load_jsonl(args.output)
    }
    if set(gold) != set(packets) or set(gold) != set(outputs):
        parser.error("gold, packet, and output basis edge sets must match")

    results = []
    exact_count = 0
    predicate_exact_count = 0
    direction_exact_count = 0
    structural_error_count = 0
    counts = {predicate: Counter() for predicate in PREDICATES}
    for basis_id, expected_row in gold.items():
        output = outputs[basis_id]
        expected = set(expected_row["expectedPredicates"])
        actual = {
            predicate
            for predicate, assessment in output["predicateAssessments"].items()
            if assessment.get("finding") == "established"
        }
        errors = _structural_errors(output, packets[basis_id])
        predicate_exact = actual == expected and not errors
        predicate_exact_count += int(predicate_exact)
        actual_directions = {
            (
                str(assertion.get("proposedPredicate")),
                str(assertion.get("subjectArticleId")),
                str(assertion.get("objectArticleId")),
            )
            for assertion in output.get("assertions", [])
            if isinstance(assertion, dict)
        }
        expected_directions = (
            gold_directions[basis_id] if gold_directions is not None else None
        )
        direction_exact = (
            actual_directions == expected_directions
            if expected_directions is not None
            else None
        )
        if direction_exact is True:
            direction_exact_count += 1
        exact = predicate_exact and direction_exact is not False
        exact_count += int(exact)
        structural_error_count += int(bool(errors))
        for predicate in PREDICATES:
            if predicate in expected and predicate in actual:
                counts[predicate]["tp"] += 1
            elif predicate in actual:
                counts[predicate]["fp"] += 1
            elif predicate in expected:
                counts[predicate]["fn"] += 1
            else:
                counts[predicate]["tn"] += 1
        results.append(
            {
                "basisEdgeId": basis_id,
                "exact": exact,
                "predicateExact": predicate_exact,
                "directionExact": direction_exact,
                "expectedPredicates": sorted(expected),
                "actualPredicates": sorted(actual),
                "missing": sorted(expected - actual),
                "extra": sorted(actual - expected),
                "structuralErrors": errors,
                "expectedSemanticDirections": (
                    [
                        {
                            "proposedPredicate": predicate,
                            "subjectArticleId": subject,
                            "objectArticleId": object_,
                        }
                        for predicate, subject, object_ in sorted(expected_directions)
                    ]
                    if expected_directions is not None
                    else None
                ),
                "actualSemanticDirections": [
                    {
                        "proposedPredicate": predicate,
                        "subjectArticleId": subject,
                        "objectArticleId": object_,
                    }
                    for predicate, subject, object_ in sorted(actual_directions)
                ],
            }
        )
    print(
        json.dumps(
            {
                "candidateCount": len(gold),
                "exactCount": exact_count,
                "predicateExactCount": predicate_exact_count,
                "directionChecked": gold_directions is not None,
                "directionExactCount": (
                    direction_exact_count if gold_directions is not None else None
                ),
                "structuralErrorCount": structural_error_count,
                "predicateCounts": {
                    predicate: dict(counts[predicate]) for predicate in PREDICATES
                },
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if exact_count == len(gold) else 1


if __name__ == "__main__":
    raise SystemExit(main())
