"""Worker / Reviewer / 1回差戻し成果物の決定的な対応付け。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar

from .graph_schema import ReviewStatus
from .relation_classification import (
    AdjudicationRevisionPacket,
    ApprovedAdjudicationRecord,
    RelationAdjudicationCandidatePacket,
    ReviewerRecord,
    UnresolvedAdjudicationRecord,
    WorkerAdjudicationRecord,
    validate_reviewer_record,
)

RecordT = TypeVar("RecordT")


def _index_by_candidate_key(
    records: Iterable[RecordT],
    *,
    label: str,
) -> dict[str, RecordT]:
    indexed: dict[str, RecordT] = {}
    for record in records:
        candidate_key = str(getattr(record, "candidate_key", ""))
        if not candidate_key:
            raise ValueError(f"{label} record identity is missing")
        if candidate_key in indexed:
            raise ValueError(f"duplicate {label} candidate key: {candidate_key}")
        indexed[candidate_key] = record
    return indexed


def prepare_adjudication_revisions(
    packets: Iterable[RelationAdjudicationCandidatePacket],
    workers: Iterable[WorkerAdjudicationRecord],
    reviews: Iterable[ReviewerRecord],
) -> tuple[
    tuple[WorkerAdjudicationRecord, ...],
    tuple[AdjudicationRevisionPacket, ...],
]:
    """初回Reviewを検証し、承認済みと差戻し対象だけへ機械的に分ける。"""

    packet_by_key = _index_by_candidate_key(packets, label="packet")
    worker_by_key = _index_by_candidate_key(workers, label="Worker")
    review_by_key = _index_by_candidate_key(reviews, label="Reviewer")
    expected = set(packet_by_key)
    if set(worker_by_key) != expected or set(review_by_key) != expected:
        raise ValueError("packet, Worker, and Reviewer candidate sets must match")

    approved: list[WorkerAdjudicationRecord] = []
    revisions: list[AdjudicationRevisionPacket] = []
    for candidate_key in sorted(expected):
        packet = packet_by_key[candidate_key]
        worker = worker_by_key[candidate_key]
        review = review_by_key[candidate_key]
        validate_reviewer_record(packet.to_candidate(), worker, review)
        if review.review_status is ReviewStatus.APPROVE:
            approved.append(worker)
            continue
        revisions.append(
            AdjudicationRevisionPacket(
                candidate_key=candidate_key,
                original_candidate=packet,
                previous_decision=worker,
                review_feedback=review,
            )
        )
    return tuple(approved), tuple(revisions)


def merge_once_revised_adjudications(
    packets: Iterable[RelationAdjudicationCandidatePacket],
    initial_workers: Iterable[WorkerAdjudicationRecord],
    initial_reviews: Iterable[ReviewerRecord],
    revised_workers: Iterable[WorkerAdjudicationRecord],
    final_reviews: Iterable[ReviewerRecord],
) -> tuple[
    tuple[ApprovedAdjudicationRecord, ...],
    tuple[UnresolvedAdjudicationRecord, ...],
]:
    """承認済みWorker回答だけを返し、再差戻しを未解消として分離する。"""

    packet_values = tuple(packets)
    initial_worker_values = tuple(initial_workers)
    initial_review_values = tuple(initial_reviews)
    _, revisions = prepare_adjudication_revisions(
        packet_values,
        initial_worker_values,
        initial_review_values,
    )
    packet_by_key = _index_by_candidate_key(packet_values, label="packet")
    initial_worker_by_key = _index_by_candidate_key(
        initial_worker_values, label="initial Worker"
    )
    initial_review_by_key = _index_by_candidate_key(
        initial_review_values, label="initial Reviewer"
    )
    revised_worker_by_key = _index_by_candidate_key(
        revised_workers, label="revised Worker"
    )
    final_review_by_key = _index_by_candidate_key(
        final_reviews, label="final Reviewer"
    )
    requested = {item.candidate_key for item in revisions}
    if set(revised_worker_by_key) != requested or set(final_review_by_key) != requested:
        raise ValueError(
            "revised Worker and final Reviewer sets must exactly match requests"
        )

    approved = [
        ApprovedAdjudicationRecord(
            candidate_key=candidate_key,
            original_candidate=packet_by_key[candidate_key],
            worker_decision=initial_worker_by_key[candidate_key],
            approval_review=initial_review_by_key[candidate_key],
            revision_round=0,
        )
        for candidate_key in sorted(set(packet_by_key).difference(requested))
    ]
    unresolved: list[UnresolvedAdjudicationRecord] = []
    for candidate_key in sorted(requested):
        packet = packet_by_key[candidate_key]
        revised_worker = revised_worker_by_key[candidate_key]
        final_review = final_review_by_key[candidate_key]
        validate_reviewer_record(
            packet.to_candidate(), revised_worker, final_review
        )
        if final_review.review_status is ReviewStatus.APPROVE:
            approved.append(
                ApprovedAdjudicationRecord(
                    candidate_key=candidate_key,
                    original_candidate=packet,
                    worker_decision=revised_worker,
                    approval_review=final_review,
                    revision_round=1,
                    initial_worker_decision=initial_worker_by_key[candidate_key],
                    initial_review=initial_review_by_key[candidate_key],
                )
            )
            continue
        unresolved.append(
            UnresolvedAdjudicationRecord(
                candidate_key=candidate_key,
                reason="request_change_after_single_revision",
                original_candidate=packet,
                initial_worker_decision=initial_worker_by_key[candidate_key],
                initial_review=initial_review_by_key[candidate_key],
                revised_worker_decision=revised_worker,
                final_review=final_review,
            )
        )
    return (
        tuple(sorted(approved, key=lambda item: item.candidate_key)),
        tuple(sorted(unresolved, key=lambda item: item.candidate_key)),
    )


__all__ = [
    "merge_once_revised_adjudications",
    "prepare_adjudication_revisions",
]
