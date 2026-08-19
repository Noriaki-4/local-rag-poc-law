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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--actual", type=Path, action="append", required=True)
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
                for path in args.actual
                for record in _load_jsonl(path)
            ],
            label="actual",
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
    direction_correct = 0
    grounding_correct = 0
    exact_correct = 0
    predicate_total = len(gold) * len(ProposedPredicate)
    assertion_total = sum(len(record.assertions) for record in gold.values())

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
        status_match = observed.adjudication_status is expected.adjudication_status
        status_correct += int(status_match)
        if not status_match:
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
            predicate_correct += int(expected_finding is actual_finding)
            if expected_finding is not actual_finding:
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
                assertion_differences[predicate.value] = {"actual": None}
                continue
            direction_match = (
                actual_assertion["subjectArticleId"]
                == expected_assertion["subjectArticleId"]
                and actual_assertion["objectArticleId"]
                == expected_assertion["objectArticleId"]
            )
            grounding_match = all(
                actual_assertion[field] == expected_assertion[field]
                for field in (
                    "referenceOccurrenceHash",
                    "referenceSourceSupportingSpanId",
                    "referenceTargetSupportingSpanId",
                )
            )
            direction_correct += int(direction_match)
            grounding_correct += int(grounding_match)
            if not direction_match or not grounding_match:
                assertion_differences[predicate.value] = {
                    "expected": expected_assertion,
                    "actual": actual_assertion,
                }
        if set(actual_assertions).difference(expected_assertions):
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

    report = {
        "candidateCount": len(gold),
        "actualCount": len(actual),
        "missingCandidateKeys": missing,
        "unexpectedCandidateKeys": unexpected,
        "exactCorrectCount": exact_correct,
        "statusCorrectCount": status_correct,
        "predicateFindingCorrectCount": predicate_correct,
        "predicateFindingTotal": predicate_total,
        "directionCorrectCount": direction_correct,
        "groundingCorrectCount": grounding_correct,
        "expectedAssertionCount": assertion_total,
        "mismatches": mismatches,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        _atomic_create(args.output.resolve(), rendered.encode("utf-8"))
    print(rendered, end="")
    return 0 if exact_correct == len(gold) and not missing and not unexpected else 1


if __name__ == "__main__":
    raise SystemExit(main())
