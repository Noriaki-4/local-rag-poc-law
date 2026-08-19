"""Merge only Reviewer-approved initial or once-revised Worker decisions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from prepare_revision_packet import _atomic_write, _load_index, _validate_review


def _jsonl(records: list[dict[str, Any]]) -> str:
    return "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        for record in records
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--initial-worker", type=Path, required=True)
    parser.add_argument("--initial-review", type=Path, required=True)
    parser.add_argument("--revised-worker", type=Path, required=True)
    parser.add_argument("--final-review", type=Path, required=True)
    parser.add_argument("--approved-output", type=Path, required=True)
    parser.add_argument("--unresolved-output", type=Path, required=True)
    args = parser.parse_args()

    packets = _load_index(args.packet, label="packet")
    initial_workers = _load_index(args.initial_worker, label="initial_worker")
    initial_reviews = _load_index(args.initial_review, label="initial_review")
    revised_workers = _load_index(args.revised_worker, label="revised_worker")
    final_reviews = _load_index(args.final_review, label="final_review")
    if set(packets) != set(initial_workers) or set(packets) != set(initial_reviews):
        parser.error("packet, initial worker, and initial review sets must match")

    requested: set[str] = set()
    validation_errors: dict[str, list[str]] = {}
    for candidate_key, packet in packets.items():
        errors = _validate_review(
            initial_reviews[candidate_key], packet, initial_workers[candidate_key]
        )
        if errors:
            validation_errors[f"initial:{candidate_key}"] = errors
        elif initial_reviews[candidate_key]["reviewStatus"] == "request_change":
            requested.add(candidate_key)
    if set(revised_workers) != requested or set(final_reviews) != requested:
        parser.error(
            "revised worker and final review sets must exactly match initial requests"
        )

    approved: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for candidate_key, packet in packets.items():
        initial_worker = initial_workers[candidate_key]
        initial_review = initial_reviews[candidate_key]
        if candidate_key not in requested:
            approved.append(
                {
                    "candidateKey": packet["candidateKey"],
                    "originalCandidate": packet,
                    "workerDecision": initial_worker,
                    "approvalReview": initial_review,
                    "revisionRound": 0,
                }
            )
            continue
        revised_worker = revised_workers[candidate_key]
        final_review = final_reviews[candidate_key]
        errors = _validate_review(final_review, packet, revised_worker)
        if errors:
            validation_errors[f"final:{candidate_key}"] = errors
            continue
        if final_review["reviewStatus"] == "approve":
            approved.append(
                {
                    "candidateKey": packet["candidateKey"],
                    "originalCandidate": packet,
                    "workerDecision": revised_worker,
                    "approvalReview": final_review,
                    "revisionRound": 1,
                    "initialWorkerDecision": initial_worker,
                    "initialReview": initial_review,
                }
            )
        else:
            unresolved.append(
                {
                    "candidateKey": packet["candidateKey"],
                    "reason": "request_change_after_single_revision",
                    "originalCandidate": packet,
                    "initialWorkerDecision": initial_worker,
                    "initialReview": initial_review,
                    "revisedWorkerDecision": revised_worker,
                    "finalReview": final_review,
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

    _atomic_write(args.approved_output, _jsonl(approved))
    _atomic_write(args.unresolved_output, _jsonl(unresolved))
    print(
        json.dumps(
            {
                "candidateCount": len(packets),
                "initiallyApprovedCount": len(packets) - len(requested),
                "revisionCount": len(requested),
                "approvedCount": len(approved),
                "unresolvedCount": len(unresolved),
                "approvedOutput": str(args.approved_output.resolve()),
                "unresolvedOutput": str(args.unresolved_output.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
