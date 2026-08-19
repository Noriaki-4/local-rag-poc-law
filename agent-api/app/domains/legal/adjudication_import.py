"""Lunaの承認済み成果物をGraph保存契約へ投影する。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .graph_schema import (
    AdjudicationStatus,
    ClassificationCheckpointOutcome,
    ClassificationRunPhase,
)
from .relation_classification import (
    ApprovedAdjudicationRecord,
    ClassificationCheckpointRecord,
    ClassificationRunRecord,
    RelationAdjudicationCandidatePacket,
    RelationAdjudicationManifest,
    RelationAssertionRecord,
    UnresolvedAdjudicationRecord,
    build_assertion_records,
    stable_hash,
    validate_worker_adjudication,
    worker_adjudication_to_decision,
)


@dataclass(frozen=True)
class AdjudicationImportBatch:
    checkpoints: tuple[ClassificationCheckpointRecord, ...]
    assertions_by_candidate: dict[str, tuple[RelationAssertionRecord, ...]]


def classification_run_from_adjudication_manifest(
    manifest: RelationAdjudicationManifest,
    packets: Iterable[RelationAdjudicationCandidatePacket],
    *,
    classification_run_id: str | None = None,
) -> ClassificationRunRecord:
    """manifestとpacketの同一性を検証し、building Runを作る。"""

    ordered = tuple(sorted(packets, key=lambda item: item.candidate_key))
    if not ordered:
        raise ValueError("classification run packet is empty")
    candidate_keys = [item.candidate_key for item in ordered]
    if len(candidate_keys) != len(set(candidate_keys)):
        raise ValueError("classification run candidate keys must be unique")
    if manifest.candidate_count != len(ordered):
        raise ValueError("manifest and packet candidate counts differ")
    manifest_keys = {
        key for shard in manifest.shards for key in shard.candidate_keys
    }
    if manifest_keys != set(candidate_keys):
        raise ValueError("manifest and packet candidate scopes differ")

    for packet in ordered:
        if packet.source_snapshot_id != manifest.source_snapshot_id:
            raise ValueError("packet snapshot differs from manifest")
        if packet.graph_schema_version != manifest.graph_schema_version:
            raise ValueError("packet graph schema differs from manifest")
        if packet.prompt_version != manifest.prompt_version:
            raise ValueError("packet prompt differs from manifest")
        if packet.model != manifest.execution_profile.worker_model:
            raise ValueError("packet Worker model differs from manifest")
        if packet.reviewer_model != manifest.execution_profile.reviewer_model:
            raise ValueError("packet Reviewer model differs from manifest")
    if manifest.execution_profile.reasoning_effort != "high":
        raise ValueError("full adjudication run requires high reasoning effort")

    providers = {packet.provider for packet in ordered}
    if len(providers) != 1:
        raise ValueError("classification run requires one provider")
    scope_hash = stable_hash(
        {
            "candidateKeys": candidate_keys,
            "sourceSnapshotId": manifest.source_snapshot_id,
            "graphSchemaVersion": manifest.graph_schema_version,
        }
    )
    return ClassificationRunRecord(
        classification_run_id=(
            classification_run_id or f"classification-run-{scope_hash[:32]}"
        ),
        phase=ClassificationRunPhase.BUILDING,
        source_snapshot_id=manifest.source_snapshot_id,
        graph_schema_version=manifest.graph_schema_version,
        provider=next(iter(providers)),
        model=manifest.execution_profile.worker_model,
        reviewer_model=manifest.execution_profile.reviewer_model,
        prompt_version=manifest.prompt_version,
        skill_version=manifest.execution_profile.skill_version,
        reasoning_effort=manifest.execution_profile.reasoning_effort,
        candidates_per_model_call=manifest.max_candidates_per_shard,
        input_count=len(ordered),
        processed_count=0,
        classified_candidate_count=0,
        assertion_count=0,
        reference_only_count=0,
        uncertain_count=0,
        failed_count=0,
        scope_hash=scope_hash,
    )


def build_adjudication_import_batch(
    packets: Iterable[RelationAdjudicationCandidatePacket],
    approved_records: Iterable[ApprovedAdjudicationRecord],
    unresolved_records: Iterable[UnresolvedAdjudicationRecord],
    *,
    classification_run_id: str,
    processed_at: datetime,
) -> AdjudicationImportBatch:
    """LLM判断を変更せず、checkpointとAssertionの保存形へ投影する。"""

    packet_by_key = _unique_by_key(packets, label="packet")
    approved_by_key = _unique_by_key(approved_records, label="approved record")
    unresolved_by_key = _unique_by_key(
        unresolved_records, label="unresolved record"
    )
    result_keys = set(approved_by_key) | set(unresolved_by_key)
    if set(approved_by_key).intersection(unresolved_by_key):
        raise ValueError("candidate cannot be both approved and unresolved")
    unknown = result_keys.difference(packet_by_key)
    if unknown:
        raise ValueError(f"import results are outside packet scope: {sorted(unknown)}")

    checkpoints: list[ClassificationCheckpointRecord] = []
    assertions_by_candidate: dict[str, tuple[RelationAssertionRecord, ...]] = {}
    for candidate_key in sorted(result_keys):
        packet = packet_by_key[candidate_key]
        candidate = packet.to_candidate()
        assertions: tuple[RelationAssertionRecord, ...] = ()
        if candidate_key in approved_by_key:
            approved = approved_by_key[candidate_key]
            if approved.original_candidate != packet:
                raise ValueError("approved record candidate differs from packet")
            worker = approved.worker_decision
            validate_worker_adjudication(candidate, worker)
            payload = {
                "recordType": "reviewer_approved_worker",
                "approved": approved.model_dump(by_alias=True, mode="json"),
            }
            if worker.adjudication_status is AdjudicationStatus.ACCEPTED:
                decision = worker_adjudication_to_decision(worker)
                assertions = build_assertion_records(
                    candidate,
                    decision,
                    classification_run_id=classification_run_id,
                    classified_at=processed_at,
                )
                outcome = ClassificationCheckpointOutcome(decision.outcome.value)
            else:
                # Reviewerが不確実性を承認しても、未完了候補の
                # Assertionは公開せず、Worker回答全体を監査用に保存する。
                outcome = ClassificationCheckpointOutcome.UNCERTAIN
        else:
            unresolved = unresolved_by_key[candidate_key]
            if unresolved.original_candidate != packet:
                raise ValueError("unresolved record candidate differs from packet")
            payload = {
                "recordType": "unresolved_after_revision",
                "unresolved": unresolved.model_dump(by_alias=True, mode="json"),
            }
            outcome = ClassificationCheckpointOutcome.UNCERTAIN

        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        checkpoint_key = stable_hash(
            {
                "classificationRunId": classification_run_id,
                "candidateKey": candidate_key,
            }
        )
        checkpoints.append(
            ClassificationCheckpointRecord(
                checkpoint_id=f"classification-checkpoint-{checkpoint_key}",
                classification_run_id=classification_run_id,
                candidate_key=candidate_key,
                outcome=outcome,
                decision_payload_hash=stable_hash(payload),
                decision_payload_json=payload_json,
                assertion_count=len(assertions),
                processed_at=processed_at,
                source_snapshot_id=packet.source_snapshot_id,
                graph_schema_version=packet.graph_schema_version,
            )
        )
        assertions_by_candidate[candidate_key] = assertions
    return AdjudicationImportBatch(
        checkpoints=tuple(checkpoints),
        assertions_by_candidate=assertions_by_candidate,
    )


def _unique_by_key(records: Iterable[object], *, label: str) -> dict[str, object]:
    indexed: dict[str, object] = {}
    for record in records:
        candidate_key = str(getattr(record, "candidate_key", ""))
        if not candidate_key:
            raise ValueError(f"{label} candidate key is missing")
        if candidate_key in indexed:
            raise ValueError(f"duplicate {label} candidate key: {candidate_key}")
        indexed[candidate_key] = record
    return indexed


__all__ = [
    "AdjudicationImportBatch",
    "build_adjudication_import_batch",
    "classification_run_from_adjudication_manifest",
]
