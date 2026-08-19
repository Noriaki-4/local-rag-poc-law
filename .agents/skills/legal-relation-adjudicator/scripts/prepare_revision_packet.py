"""Validate Reviewer feedback and emit only requested-change candidates."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

PREDICATES = {
    "IMPLEMENTS",
    "INCORPORATES",
    "USES_DEFINITION",
    "EXCEPTION_TO",
    "OVERRIDES",
}
PROBLEM_TYPES = {"condition", "finding", "direction", "grounding"}
FINDINGS = {"established", "not_established", "uncertain"}
REVIEW_CONCLUSIONS = {"confirmed", "change_required"}


def _load_index(path: Path, *, label: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise TypeError(f"{path}:{line_number}: record must be an object")
        candidate_key = value.get("candidateKey")
        if not isinstance(candidate_key, str) or not candidate_key:
            raise ValueError(f"{label}:{line_number}: missing candidateKey")
        if candidate_key in indexed:
            raise ValueError(
                f"{label}:{line_number}: duplicate candidateKey {candidate_key}"
            )
        indexed[candidate_key] = value
    return indexed


def _known_span_ids(packet: dict[str, Any]) -> set[str]:
    known: set[str] = set()
    for article_key in ("referenceSourceArticle", "referenceTargetArticle"):
        article = packet.get(article_key)
        if not isinstance(article, dict):
            continue
        spans = article.get("spans")
        if not isinstance(spans, list):
            continue
        known.update(
            str(span["spanId"])
            for span in spans
            if isinstance(span, dict) and span.get("spanId")
        )
    return known


def _finding_from_conditions(first: str, second: str) -> str:
    if first == second == "established":
        return "established"
    if "not_established" in (first, second):
        return "not_established"
    return "uncertain"


def _validate_worker(worker: dict[str, Any], packet: dict[str, Any]) -> list[str]:
    """Validate semantic shape without making or repairing a meaning judgment."""

    errors: list[str] = []
    if worker.get("candidateKey") != packet.get("candidateKey"):
        errors.append("worker_candidate_key_mismatch")

    assessments = worker.get("predicateAssessments")
    if not isinstance(assessments, dict) or set(assessments) != PREDICATES:
        return [*errors, "worker_predicate_assessment_coverage"]
    established: set[str] = set()
    uncertain = False
    for predicate in PREDICATES:
        item = assessments[predicate]
        if not isinstance(item, dict):
            errors.append(f"worker:{predicate}:invalid_assessment")
            continue
        first = item.get("firstCondition")
        second = item.get("secondCondition")
        finding = item.get("finding")
        if first not in FINDINGS or second not in FINDINGS or finding not in FINDINGS:
            errors.append(f"worker:{predicate}:invalid_finding")
            continue
        if finding != _finding_from_conditions(first, second):
            errors.append(f"worker:{predicate}:condition_algebra")
        if finding == "established":
            established.add(predicate)
        uncertain = uncertain or finding == "uncertain"

    status = worker.get("adjudicationStatus")
    if status == "needs_resolution":
        if established or any(
            assessments[predicate].get("finding") != "uncertain"
            for predicate in PREDICATES
            if isinstance(assessments[predicate], dict)
        ):
            errors.append("worker_needs_resolution_predicate_algebra")
    else:
        expected_status = "needs_review" if uncertain else "accepted"
        if status != expected_status:
            errors.append("worker_adjudication_status_mismatch")

    source = packet.get("referenceSourceArticle")
    target = packet.get("referenceTargetArticle")
    if not isinstance(source, dict) or not isinstance(target, dict):
        return [*errors, "worker_missing_article_endpoints"]
    article_ids = {source.get("articleId"), target.get("articleId")}
    occurrence_by_hash = {
        item.get("occurrenceHash"): item
        for item in packet.get("referenceOccurrences", [])
        if isinstance(item, dict) and item.get("occurrenceHash")
    }
    target_span_ids = {
        item.get("spanId")
        for item in target.get("spans", [])
        if isinstance(item, dict) and item.get("spanId")
    }
    assertions = worker.get("assertions")
    if not isinstance(assertions, list):
        return [*errors, "worker_assertions_not_list"]
    assertion_predicates = {
        str(item.get("proposedPredicate"))
        for item in assertions
        if isinstance(item, dict)
    }
    if assertion_predicates != established or len(assertions) != len(established):
        errors.append("worker_assertion_predicate_coverage")
    for index, assertion in enumerate(assertions):
        prefix = f"worker_assertion_{index}"
        if not isinstance(assertion, dict):
            errors.append(f"{prefix}:not_object")
            continue
        occurrence = occurrence_by_hash.get(assertion.get("referenceOccurrenceHash"))
        if occurrence is None:
            errors.append(f"{prefix}:unknown_occurrence_hash")
            continue
        if {
            assertion.get("subjectArticleId"),
            assertion.get("objectArticleId"),
        } != article_ids:
            errors.append(f"{prefix}:invalid_endpoints")
        if assertion.get("referenceSourceSupportingSpanId") not in set(
            occurrence.get("sourceSpanIds", [])
        ):
            errors.append(f"{prefix}:invalid_source_span")
        if assertion.get("referenceTargetSupportingSpanId") not in target_span_ids:
            errors.append(f"{prefix}:invalid_target_span")
    return errors


def _validate_review(
    review: dict[str, Any], packet: dict[str, Any], worker: dict[str, Any]
) -> list[str]:
    errors: list[str] = []
    if review.get("candidateKey") != packet.get("candidateKey"):
        errors.append("review_candidate_key_mismatch")
    if worker.get("candidateKey") != packet.get("candidateKey"):
        errors.append("worker_candidate_key_mismatch")
    errors.extend(_validate_worker(worker, packet))

    status = review.get("reviewStatus")
    issues = review.get("issues")
    if status not in {"approve", "request_change"}:
        errors.append("invalid_review_status")
    if not isinstance(issues, list):
        return [*errors, "issues_not_list"]
    if status == "approve" and issues:
        errors.append("approve_with_issues")
    if status == "request_change" and not issues:
        errors.append("request_change_without_issues")

    predicate_checks = review.get("predicateChecks")
    if not isinstance(predicate_checks, dict) or set(predicate_checks) != PREDICATES:
        return [*errors, "predicate_check_coverage"]
    worker_assessments = worker.get("predicateAssessments")
    if (
        not isinstance(worker_assessments, dict)
        or set(worker_assessments) != PREDICATES
    ):
        return [*errors, "worker_predicate_assessment_coverage"]
    change_required: set[str] = set()
    for predicate in PREDICATES:
        check = predicate_checks[predicate]
        if not isinstance(check, dict):
            errors.append(f"{predicate}:invalid_predicate_check")
            continue
        worker_finding = check.get("workerFinding")
        actual_worker_finding = worker_assessments[predicate].get("finding")
        if worker_finding not in FINDINGS or worker_finding != actual_worker_finding:
            errors.append(f"{predicate}:worker_finding_mismatch")
        conclusion = check.get("reviewConclusion")
        if conclusion not in REVIEW_CONCLUSIONS:
            errors.append(f"{predicate}:invalid_review_conclusion")
        elif conclusion == "change_required":
            change_required.add(predicate)
        if not isinstance(check.get("note"), str) or not check["note"].strip():
            errors.append(f"{predicate}:missing_review_note")

    known_span_ids = _known_span_ids(packet)
    issue_predicates: set[str] = set()
    for index, issue in enumerate(issues):
        prefix = f"issue_{index}"
        if not isinstance(issue, dict):
            errors.append(f"{prefix}:not_object")
            continue
        if issue.get("predicate") not in PREDICATES:
            errors.append(f"{prefix}:invalid_predicate")
        else:
            issue_predicates.add(str(issue["predicate"]))
        if issue.get("problemType") not in PROBLEM_TYPES:
            errors.append(f"{prefix}:invalid_problem_type")
        if not isinstance(issue.get("critique"), str) or not issue["critique"].strip():
            errors.append(f"{prefix}:missing_critique")
        if (
            not isinstance(issue.get("recommendedAction"), str)
            or not issue["recommendedAction"].strip()
        ):
            errors.append(f"{prefix}:missing_recommended_action")
        span_ids = issue.get("supportingSpanIds", [])
        if not isinstance(span_ids, list):
            errors.append(f"{prefix}:supporting_span_ids_not_list")
        elif any(span_id not in known_span_ids for span_id in span_ids):
            errors.append(f"{prefix}:unknown_supporting_span_id")
    if issue_predicates != change_required:
        errors.append("change_required_issue_coverage")
    expected_status = "request_change" if change_required else "approve"
    if status != expected_status:
        errors.append("review_status_predicate_check_mismatch")
    return errors


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    packets = _load_index(args.packet, label="packet")
    workers = _load_index(args.worker, label="worker")
    reviews = _load_index(args.review, label="review")
    if set(packets) != set(workers) or set(packets) != set(reviews):
        parser.error("packet, worker, and review candidateKey sets must match")

    revision_records: list[dict[str, Any]] = []
    validation_errors: dict[str, list[str]] = {}
    approved_count = 0
    for candidate_key, packet in packets.items():
        worker = workers[candidate_key]
        review = reviews[candidate_key]
        errors = _validate_review(review, packet, worker)
        if errors:
            validation_errors[candidate_key] = errors
            continue
        if review["reviewStatus"] == "approve":
            approved_count += 1
            continue
        revision_records.append(
            {
                "candidateKey": packet["candidateKey"],
                "originalCandidate": packet,
                "previousDecision": worker,
                "reviewFeedback": review,
            }
        )

    if validation_errors:
        print(
            json.dumps(
                {"validationErrors": validation_errors},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    _atomic_write(
        args.output,
        "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in revision_records
        ),
    )
    print(
        json.dumps(
            {
                "candidateCount": len(packets),
                "approvedCount": approved_count,
                "requestChangeCount": len(revision_records),
                "output": str(args.output.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
