"""意味分類packetの決定的なexport・再開filter・shard計画。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from .relation_classification import (
    AdjudicationShardManifest,
    RelationAdjudicationCandidatePacket,
    RelationAdjudicationExecutionProfile,
    RelationAdjudicationManifest,
    RelationClassificationCandidate,
    stable_hash,
)


def packet_records_from_candidates(
    rows: Iterable[dict[str, Any]],
    candidates: Iterable[RelationClassificationCandidate],
) -> tuple[RelationAdjudicationCandidatePacket, ...]:
    """Graph edgeとArticleペア候補のOccurrence対応を意味判断なしで検証する。"""

    kinds_by_basis: dict[str, str] = {}
    for row in rows:
        basis = dict(row.get("basis") or {})
        basis_edge_id = str(basis.get("graphEdgeId") or "")
        reference_kind = str(basis.get("referenceKind") or "")
        if not basis_edge_id or not reference_kind:
            raise ValueError("Graph row requires basis edge ID and reference kind")
        if basis_edge_id in kinds_by_basis:
            raise ValueError(f"duplicate Graph basis edge: {basis_edge_id}")
        kinds_by_basis[basis_edge_id] = reference_kind

    candidate_basis_ids: set[str] = set()
    candidate_values: list[RelationClassificationCandidate] = []
    for candidate in candidates:
        overlap = candidate_basis_ids.intersection(candidate.basis_edge_ids)
        if overlap:
            raise ValueError(f"basis edge appears in multiple candidates: {sorted(overlap)}")
        candidate_basis_ids.update(candidate.basis_edge_ids)
        candidate_values.append(candidate)
        for occurrence in candidate.reference_occurrences:
            if kinds_by_basis.get(occurrence.basis_edge_id) != occurrence.reference_kind:
                raise ValueError("candidate occurrence reference kind does not match Graph")
    if set(kinds_by_basis) != candidate_basis_ids:
        raise ValueError("Graph rows and candidates must cover the same basis edges")

    records = [
        RelationAdjudicationCandidatePacket.from_candidate(candidate)
        for candidate in candidate_values
    ]
    return tuple(sorted(records, key=lambda item: item.candidate_key))


def exclude_completed_packet_records(
    records: Iterable[RelationAdjudicationCandidatePacket],
    completed_candidate_keys: set[str],
) -> tuple[RelationAdjudicationCandidatePacket, ...]:
    """保存済みcandidate keyだけを除外し、未知keyによる誤Run再開を拒否する。"""

    ordered = tuple(sorted(records, key=lambda item: item.candidate_key))
    known = {record.candidate_key for record in ordered}
    unknown = completed_candidate_keys.difference(known)
    if unknown:
        raise ValueError(f"completed candidate keys are outside packet scope: {sorted(unknown)}")
    return tuple(
        record
        for record in ordered
        if record.candidate_key not in completed_candidate_keys
    )


def canonical_packet_jsonl(
    records: Iterable[RelationAdjudicationCandidatePacket],
) -> bytes:
    return "".join(
        json.dumps(
            record.model_dump(by_alias=True, mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
        for record in records
    ).encode("utf-8")


def plan_adjudication_shards(
    records: Iterable[RelationAdjudicationCandidatePacket],
    *,
    source_packet: Path,
    source_packet_bytes: bytes,
    max_candidates_per_shard: int = 5,
    max_active_sessions: int = 3,
    skill_version: str = "legal-relation-adjudicator-2026-08-19-pair-v3",
    reasoning_effort: str = "high",
) -> tuple[RelationAdjudicationManifest, dict[str, bytes]]:
    """固定件数上限でshardを作り、Pydantic manifestとbytesを返す。"""

    if not 1 <= max_candidates_per_shard <= 5:
        raise ValueError("max candidates per shard must be between 1 and 5")
    if not 1 <= max_active_sessions <= 3:
        raise ValueError("max active sessions must be between 1 and 3")
    ordered = tuple(sorted(records, key=lambda item: item.candidate_key))
    if not ordered:
        raise ValueError("adjudication packet is empty")
    candidate_keys = [item.candidate_key for item in ordered]
    basis_edge_ids = [
        basis_edge_id for item in ordered for basis_edge_id in item.basis_edge_ids
    ]
    if len(candidate_keys) != len(set(candidate_keys)):
        raise ValueError("candidate keys must be unique")
    if len(basis_edge_ids) != len(set(basis_edge_ids)):
        raise ValueError("basis edge IDs must be unique")

    source_snapshot_ids = {item.source_snapshot_id for item in ordered}
    graph_schema_versions = {item.graph_schema_version for item in ordered}
    prompt_versions = {item.prompt_version for item in ordered}
    providers = {item.provider for item in ordered}
    worker_models = {item.model for item in ordered}
    reviewer_models = {item.reviewer_model for item in ordered}
    for values, label in (
        (source_snapshot_ids, "source snapshot"),
        (graph_schema_versions, "graph schema version"),
        (prompt_versions, "prompt version"),
        (providers, "provider"),
        (worker_models, "worker model"),
        (reviewer_models, "reviewer model"),
    ):
        if len(values) != 1:
            raise ValueError(f"packet must have exactly one {label}")

    shard_bytes: dict[str, bytes] = {}
    shard_manifests: list[AdjudicationShardManifest] = []
    for index, offset in enumerate(
        range(0, len(ordered), max_candidates_per_shard)
    ):
        shard = ordered[offset : offset + max_candidates_per_shard]
        shard_id = f"shard-{index:04d}"
        filename = f"{shard_id}.jsonl"
        data = canonical_packet_jsonl(shard)
        shard_bytes[filename] = data
        shard_manifests.append(
            AdjudicationShardManifest(
                shard_id=shard_id,
                file=filename,
                candidate_count=len(shard),
                input_characters=len(data.decode("utf-8")),
                sha256=hashlib.sha256(data).hexdigest(),
                candidate_keys=tuple(item.candidate_key for item in shard),
                basis_edge_ids=tuple(
                    sorted(
                        basis_edge_id
                        for item in shard
                        for basis_edge_id in item.basis_edge_ids
                    )
                ),
            )
        )

    source_snapshot_id = next(iter(source_snapshot_ids))
    graph_schema_version = next(iter(graph_schema_versions))
    prompt_version = next(iter(prompt_versions))
    worker_model = next(iter(worker_models))
    reviewer_model = next(iter(reviewer_models))
    manifest = RelationAdjudicationManifest(
        schema_version=2,
        source_packet=str(source_packet.resolve()),
        source_packet_sha256=hashlib.sha256(source_packet_bytes).hexdigest(),
        source_snapshot_id=source_snapshot_id,
        graph_schema_version=graph_schema_version,
        prompt_version=prompt_version,
        candidate_count=len(ordered),
        scope_hash=stable_hash(sorted(candidate_keys)),
        sharding_mode="fixed_candidate_limit",
        max_candidates_per_shard=max_candidates_per_shard,
        shard_count=len(shard_manifests),
        execution_profile=RelationAdjudicationExecutionProfile(
            skill_version=skill_version,
            worker_model=worker_model,
            reviewer_model=reviewer_model,
            reasoning_effort=reasoning_effort,
            candidates_per_worker_session=max_candidates_per_shard,
            candidates_per_reviewer_session=max_candidates_per_shard,
            max_active_sessions=max_active_sessions,
            worker_reviewer_separate_contexts=True,
            max_revision_rounds=1,
        ),
        shards=tuple(shard_manifests),
    )
    return manifest, shard_bytes


__all__ = [
    "canonical_packet_jsonl",
    "exclude_completed_packet_records",
    "packet_records_from_candidates",
    "plan_adjudication_shards",
]
