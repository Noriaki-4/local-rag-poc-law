#!/usr/bin/env python3
"""Articleペア単位の意味分類を、label-free packetとgoldに照合する。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "agent-api"))

from app.domains.legal.graph_schema import ProposedPredicate  # noqa: E402
from app.domains.legal.relation_classification import (  # noqa: E402
    ApprovedAdjudicationRecord,
    EvaluationGrounding,
    PredicateGroundingAllowance,
    PredicateRecallAllowance,
    RelationAdjudicationCandidatePacket,
    WorkerAdjudicationRecord,
    validate_worker_adjudication,
)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _index(records: list[Any], *, label: str) -> dict[str, Any]:
    result = {record.candidate_key: record for record in records}
    if len(result) != len(records):
        raise ValueError(f"duplicate candidateKey in {label}")
    return result


def _actual_worker(record: dict[str, Any]) -> WorkerAdjudicationRecord:
    if "workerDecision" in record:
        approved = ApprovedAdjudicationRecord.model_validate(record)
        return approved.worker_decision
    return WorkerAdjudicationRecord.model_validate(record)


def _atomic_create(path: Path, data: bytes) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _assertions_by_predicate(
    record: WorkerAdjudicationRecord,
) -> dict[ProposedPredicate, dict[str, Any]]:
    return {
        assertion.proposed_predicate: assertion.model_dump(
            by_alias=True, mode="json"
        )
        for assertion in record.assertions
    }


def _grounding(assertion: dict[str, Any]) -> EvaluationGrounding:
    return EvaluationGrounding(
        reference_occurrence_hash=assertion["referenceOccurrenceHash"],
        reference_source_supporting_span_id=assertion[
            "referenceSourceSupportingSpanId"
        ],
        reference_target_supporting_span_id=assertion[
            "referenceTargetSupportingSpanId"
        ],
    )


def _grounding_allowances(
    records: list[PredicateGroundingAllowance],
    *,
    packets: dict[str, RelationAdjudicationCandidatePacket],
    gold: dict[str, WorkerAdjudicationRecord],
) -> dict[tuple[str, ProposedPredicate], frozenset[EvaluationGrounding]]:
    indexed: dict[
        tuple[str, ProposedPredicate], frozenset[EvaluationGrounding]
    ] = {}
    for record in records:
        key = (record.candidate_key, record.predicate)
        if key in indexed:
            raise ValueError(
                "duplicate grounding allowance: "
                f"{record.candidate_key} {record.predicate.value}"
            )
        packet = packets.get(record.candidate_key)
        expected = gold.get(record.candidate_key)
        if packet is None or expected is None:
            raise ValueError(
                f"grounding allowance is outside gold scope: {record.candidate_key}"
            )
        expected_assertion = _assertions_by_predicate(expected).get(record.predicate)
        if expected_assertion is None:
            raise ValueError(
                "grounding allowance requires an established gold predicate: "
                f"{record.candidate_key} {record.predicate.value}"
            )

        occurrence_source_spans = {
            occurrence.occurrence_hash: set(occurrence.source_span_ids)
            for occurrence in packet.reference_occurrences
        }
        target_spans = {
            span.span_id for span in packet.reference_target_article.spans
        }
        allowed = frozenset(record.allowed_groundings)
        for grounding in allowed:
            source_spans = occurrence_source_spans.get(
                grounding.reference_occurrence_hash
            )
            if source_spans is None:
                raise ValueError("grounding allowance uses an unknown occurrence hash")
            if grounding.reference_source_supporting_span_id not in source_spans:
                raise ValueError(
                    "grounding allowance source span does not belong to occurrence"
                )
            if grounding.reference_target_supporting_span_id not in target_spans:
                raise ValueError("grounding allowance uses an unknown target span")
        if _grounding(expected_assertion) not in allowed:
            raise ValueError(
                "grounding allowance must include the canonical gold grounding"
            )
        indexed[key] = allowed
    return indexed


def _recall_allowances(
    records: list[PredicateRecallAllowance],
    *,
    packets: dict[str, RelationAdjudicationCandidatePacket],
    gold: dict[str, WorkerAdjudicationRecord],
) -> frozenset[tuple[str, ProposedPredicate]]:
    """人が妥当と確認したpredicateだけを、必須再現率から除外する。"""

    indexed: set[tuple[str, ProposedPredicate]] = set()
    for record in records:
        key = (record.candidate_key, record.predicate)
        if key in indexed:
            raise ValueError(
                "duplicate predicate recall allowance: "
                f"{record.candidate_key} {record.predicate.value}"
            )
        if record.candidate_key not in packets or record.candidate_key not in gold:
            raise ValueError(
                "predicate recall allowance is outside gold scope: "
                f"{record.candidate_key}"
            )
        expected = gold[record.candidate_key]
        expected_finding = expected.predicate_assessments.by_predicate()[
            record.predicate
        ].finding
        if expected_finding.value != "established":
            raise ValueError(
                "predicate recall allowance requires an established gold predicate: "
                f"{record.candidate_key} {record.predicate.value}"
            )
        indexed.add(key)
    return frozenset(indexed)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument(
        "--actual",
        type=Path,
        action="append",
        nargs="+",
        required=True,
    )
    parser.add_argument("--grounding-allowances", type=Path)
    parser.add_argument("--predicate-recall-allowances", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    try:
        packets = _index(
            [
                RelationAdjudicationCandidatePacket.model_validate(record)
                for record in _load_jsonl(args.packet)
            ],
            label="packet",
        )
        gold = _index(
            [
                WorkerAdjudicationRecord.model_validate(record)
                for record in _load_jsonl(args.gold)
            ],
            label="gold",
        )
        actual = _index(
            [
                _actual_worker(record)
                for paths in args.actual
                for path in paths
                for record in _load_jsonl(path)
            ],
            label="actual",
        )
        allowance_records = (
            [
                PredicateGroundingAllowance.model_validate(record)
                for record in _load_jsonl(args.grounding_allowances)
            ]
            if args.grounding_allowances is not None
            else []
        )
        allowances = _grounding_allowances(
            allowance_records,
            packets=packets,
            gold=gold,
        )
        recall_allowance_records = (
            [
                PredicateRecallAllowance.model_validate(record)
                for record in _load_jsonl(args.predicate_recall_allowances)
            ]
            if args.predicate_recall_allowances is not None
            else []
        )
        recall_allowances = _recall_allowances(
            recall_allowance_records,
            packets=packets,
            gold=gold,
        )
    except ValueError as error:
        parser.error(str(error))

    if set(packets) != set(gold):
        parser.error("packet and gold candidate sets must match")

    missing = sorted(set(gold).difference(actual))
    unexpected = sorted(set(actual).difference(gold))
    mismatches: list[dict[str, Any]] = []
    status_correct = 0
    predicate_correct = 0
    raw_predicate_correct = 0
    direction_correct = 0
    grounding_correct = 0
    exact_correct = 0
    raw_exact_correct = 0
    optional_predicate_omissions = 0
    predicate_total = len(gold) * len(ProposedPredicate)
    assertion_total = sum(len(record.assertions) for record in gold.values())
    required_assertion_total = assertion_total - len(recall_allowances)

    for candidate_key in sorted(set(gold).intersection(actual)):
        packet = packets[candidate_key]
        expected = gold[candidate_key]
        observed = actual[candidate_key]
        try:
            validate_worker_adjudication(packet.to_candidate(), observed)
        except ValueError as error:
            mismatches.append(
                {"candidateKey": candidate_key, "contractError": str(error)}
            )
            continue

        differences: dict[str, Any] = {}
        raw_difference = False
        status_match = observed.adjudication_status is expected.adjudication_status
        status_correct += int(status_match)
        if not status_match:
            raw_difference = True
            differences["status"] = {
                "expected": expected.adjudication_status.value,
                "actual": observed.adjudication_status.value,
            }

        expected_assessments = expected.predicate_assessments.by_predicate()
        actual_assessments = observed.predicate_assessments.by_predicate()
        predicate_differences: dict[str, Any] = {}
        for predicate in ProposedPredicate:
            expected_finding = expected_assessments[predicate].finding
            actual_finding = actual_assessments[predicate].finding
            raw_match = expected_finding is actual_finding
            raw_predicate_correct += int(raw_match)
            omission_allowed = (
                (candidate_key, predicate) in recall_allowances
                and expected_finding.value == "established"
                and actual_finding.value == "not_established"
            )
            predicate_correct += int(raw_match or omission_allowed)
            optional_predicate_omissions += int(omission_allowed)
            if not raw_match:
                raw_difference = True
            if not raw_match and not omission_allowed:
                predicate_differences[predicate.value] = {
                    "expected": expected_finding.value,
                    "actual": actual_finding.value,
                }
        if predicate_differences:
            differences["predicates"] = predicate_differences

        expected_assertions = _assertions_by_predicate(expected)
        actual_assertions = _assertions_by_predicate(observed)
        assertion_differences: dict[str, Any] = {}
        for predicate, expected_assertion in expected_assertions.items():
            actual_assertion = actual_assertions.get(predicate)
            if actual_assertion is None:
                raw_difference = True
                if (candidate_key, predicate) in recall_allowances:
                    continue
                assertion_differences[predicate.value] = {"actual": None}
                continue
            direction_match = (
                actual_assertion["subjectArticleId"]
                == expected_assertion["subjectArticleId"]
                and actual_assertion["objectArticleId"]
                == expected_assertion["objectArticleId"]
            )
            allowed_groundings = allowances.get(
                (candidate_key, predicate),
                frozenset({_grounding(expected_assertion)}),
            )
            grounding_match = _grounding(actual_assertion) in allowed_groundings
            direction_correct += int(direction_match)
            grounding_correct += int(grounding_match)
            if not direction_match or not grounding_match:
                raw_difference = True
                assertion_differences[predicate.value] = {
                    "expected": expected_assertion,
                    "actual": actual_assertion,
                    "allowedGroundings": [
                        grounding.model_dump(by_alias=True, mode="json")
                        for grounding in sorted(
                            allowed_groundings,
                            key=lambda item: (
                                item.reference_occurrence_hash,
                                item.reference_source_supporting_span_id,
                                item.reference_target_supporting_span_id,
                            ),
                        )
                    ],
                }
        if set(actual_assertions).difference(expected_assertions):
            raw_difference = True
            assertion_differences["unexpectedPredicates"] = sorted(
                predicate.value
                for predicate in set(actual_assertions).difference(expected_assertions)
            )
        if assertion_differences:
            differences["assertions"] = assertion_differences

        if not differences:
            exact_correct += 1
        else:
            mismatches.append({"candidateKey": candidate_key, **differences})
        if not raw_difference:
            raw_exact_correct += 1

    report = {
        "candidateCount": len(gold),
        "actualCount": len(actual),
        "missingCandidateKeys": missing,
        "unexpectedCandidateKeys": unexpected,
        "exactCorrectCount": exact_correct,
        "rawExactCorrectCount": raw_exact_correct,
        "statusCorrectCount": status_correct,
        "predicateFindingCorrectCount": predicate_correct,
        "rawPredicateFindingCorrectCount": raw_predicate_correct,
        "predicateFindingTotal": predicate_total,
        "directionCorrectCount": direction_correct,
        "groundingCorrectCount": grounding_correct,
        "expectedAssertionCount": assertion_total,
        "requiredAssertionCount": required_assertion_total,
        "groundingAllowanceCount": len(allowances),
        "groundingAlternativeCount": sum(
            len(values) - 1 for values in allowances.values()
        ),
        "predicateRecallAllowanceCount": len(recall_allowances),
        "optionalPredicateOmissionCount": optional_predicate_omissions,
        "mismatches": mismatches,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        _atomic_create(args.output.resolve(), rendered.encode("utf-8"))
    print(rendered, end="")
    return 0 if exact_correct == len(gold) and not missing and not unexpected else 1


if __name__ == "__main__":
    raise SystemExit(main())
