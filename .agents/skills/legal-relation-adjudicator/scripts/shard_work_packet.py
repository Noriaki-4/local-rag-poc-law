"""Deterministically split a legal-relation work packet into balanced shards."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any


def _load_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    candidate_keys: set[str] = set()
    basis_ids: set[str] = set()
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict):
            raise TypeError(f"{path}:{line_number}: record must be an object")
        candidate_key = record.get("candidateKey")
        record_basis_ids = record.get("basisEdgeIds")
        if not isinstance(candidate_key, str) or not candidate_key:
            raise ValueError(f"{path}:{line_number}: missing candidateKey")
        if (
            not isinstance(record_basis_ids, list)
            or not record_basis_ids
            or any(not isinstance(value, str) or not value for value in record_basis_ids)
        ):
            raise ValueError(f"{path}:{line_number}: missing basisEdgeIds")
        if record_basis_ids != sorted(set(record_basis_ids)):
            raise ValueError(
                f"{path}:{line_number}: basisEdgeIds must be sorted and unique"
            )
        if candidate_key in candidate_keys:
            raise ValueError(f"{path}:{line_number}: duplicate candidateKey")
        overlap = basis_ids.intersection(record_basis_ids)
        if overlap:
            raise ValueError(
                f"{path}:{line_number}: duplicate basisEdgeIds {sorted(overlap)}"
            )
        candidate_keys.add(candidate_key)
        basis_ids.update(record_basis_ids)
        records.append(record)
    if not records:
        raise ValueError(f"{path}: packet is empty")
    return records


def _canonical_line(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _scope_hash(candidate_keys: list[str]) -> str:
    data = json.dumps(
        sorted(candidate_keys),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _sha256(data)


def _manifest_value(
    records: list[dict[str, Any]],
    *,
    field: str,
    override: Any,
) -> Any:
    observed = {record.get(field) for record in records if record.get(field) is not None}
    if len(observed) > 1:
        raise ValueError(f"packet contains multiple {field} values")
    packet_value = next(iter(observed), None)
    if override is not None and packet_value is not None and override != packet_value:
        raise ValueError(f"--{field} does not match packet {field}")
    value = override if override is not None else packet_value
    if value is None or value == "":
        raise ValueError(f"{field} is required in the packet or command line")
    return value


def _atomic_write(path: Path, data: bytes) -> None:
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


def _balanced_shards(
    records: list[dict[str, Any]], shard_count: int
) -> list[list[tuple[int, dict[str, Any], int]]]:
    weighted = [
        (index, record, len(_canonical_line(record)))
        for index, record in enumerate(records)
    ]
    weighted.sort(key=lambda item: (-item[2], str(item[1]["candidateKey"])))
    shards: list[list[tuple[int, dict[str, Any], int]]] = [
        [] for _ in range(shard_count)
    ]
    sizes = [0] * shard_count
    for item in weighted:
        shard_index = min(
            range(shard_count),
            key=lambda index: (sizes[index], len(shards[index]), index),
        )
        shards[shard_index].append(item)
        sizes[shard_index] += item[2]
    for shard in shards:
        shard.sort(key=lambda item: item[0])
    return shards


def _fixed_size_shards(
    records: list[dict[str, Any]], max_candidates_per_shard: int
) -> list[list[tuple[int, dict[str, Any], int]]]:
    weighted = [
        (index, record, len(_canonical_line(record)))
        for index, record in enumerate(records)
    ]
    return [
        weighted[index : index + max_candidates_per_shard]
        for index in range(0, len(weighted), max_candidates_per_shard)
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    sharding = parser.add_mutually_exclusive_group(required=True)
    sharding.add_argument("--shard-count", type=int)
    sharding.add_argument("--max-candidates-per-shard", type=int)
    parser.add_argument("--max-active-sessions", type=int, default=3)
    parser.add_argument("--worker-model", default="gpt-5.6-luna")
    parser.add_argument("--reviewer-model", default="gpt-5.6-luna")
    parser.add_argument("--reasoning-effort", default="high")
    parser.add_argument(
        "--skill-version", default="legal-relation-adjudicator-2026-08-21-pair-v8"
    )
    parser.add_argument("--source-snapshot-id")
    parser.add_argument("--graph-schema-version", type=int)
    parser.add_argument("--prompt-version")
    args = parser.parse_args()

    records = _load_records(args.packet)
    source_snapshot_id = _manifest_value(
        records,
        field="sourceSnapshotId",
        override=args.source_snapshot_id,
    )
    graph_schema_version = _manifest_value(
        records,
        field="graphSchemaVersion",
        override=args.graph_schema_version,
    )
    prompt_version = _manifest_value(
        records,
        field="promptVersion",
        override=args.prompt_version,
    )
    if not 1 <= args.max_active_sessions <= 3:
        parser.error("--max-active-sessions must be between 1 and 3")
    if args.shard_count is not None:
        if args.shard_count < 1:
            parser.error("--shard-count must be positive")
        if args.shard_count > len(records):
            parser.error("--shard-count cannot exceed candidate count")
        shard_count = args.shard_count
        max_candidates_per_shard = None
        shards = _balanced_shards(records, shard_count)
        sharding_mode = "balanced_shard_count"
    else:
        if args.max_candidates_per_shard is None:
            parser.error("--max-candidates-per-shard is required")
        if not 1 <= args.max_candidates_per_shard <= 5:
            parser.error("--max-candidates-per-shard must be between 1 and 5")
        max_candidates_per_shard = args.max_candidates_per_shard
        shard_count = math.ceil(len(records) / max_candidates_per_shard)
        shards = _fixed_size_shards(records, max_candidates_per_shard)
        sharding_mode = "fixed_candidate_limit"

    output_dir = args.output_dir.resolve()
    planned_paths = [
        output_dir / f"shard-{index:04d}.jsonl" for index in range(shard_count)
    ]
    manifest_path = output_dir / "manifest.json"
    existing = [path for path in [*planned_paths, manifest_path] if path.exists()]
    if existing:
        parser.error(f"refusing to overwrite existing artifacts: {existing}")

    manifest_shards: list[dict[str, Any]] = []
    for index, shard in enumerate(shards):
        shard_id = f"shard-{index:04d}"
        path = output_dir / f"{shard_id}.jsonl"
        data = "".join(_canonical_line(record) for _, record, _ in shard).encode(
            "utf-8"
        )
        _atomic_write(path, data)
        manifest_shards.append(
            {
                "shardId": shard_id,
                "file": path.name,
                "candidateCount": len(shard),
                "inputCharacters": len(data.decode("utf-8")),
                "sha256": _sha256(data),
                "candidateKeys": [record["candidateKey"] for _, record, _ in shard],
                "basisEdgeIds": sorted(
                    basis_id
                    for _, record, _ in shard
                    for basis_id in record["basisEdgeIds"]
                ),
            }
        )

    packet_bytes = args.packet.read_bytes()
    manifest = {
        "schemaVersion": 2,
        "sourcePacket": str(args.packet.resolve()),
        "sourcePacketSha256": _sha256(packet_bytes),
        "sourceSnapshotId": source_snapshot_id,
        "graphSchemaVersion": graph_schema_version,
        "promptVersion": prompt_version,
        "candidateCount": len(records),
        "scopeHash": _scope_hash(
            [str(record["candidateKey"]) for record in records]
        ),
        "shardingMode": sharding_mode,
        "maxCandidatesPerShard": max_candidates_per_shard,
        "shardCount": shard_count,
        "executionProfile": {
            "skillVersion": args.skill_version,
            "workerModel": args.worker_model,
            "reviewerModel": args.reviewer_model,
            "reasoningEffort": args.reasoning_effort,
            "candidatesPerWorkerSession": max_candidates_per_shard,
            "candidatesPerReviewerSession": max_candidates_per_shard,
            "maxActiveSessions": args.max_active_sessions,
            "workerReviewerSeparateContexts": True,
            "maxRevisionRounds": 1,
        },
        "shards": manifest_shards,
    }
    _atomic_write(
        manifest_path,
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    print(
        json.dumps(
            {
                "candidateCount": len(records),
                "shardCount": shard_count,
                "maxCandidatesPerShard": max_candidates_per_shard,
                "maxActiveSessions": args.max_active_sessions,
                "manifest": str(manifest_path),
                "shards": [
                    {
                        "shardId": shard["shardId"],
                        "candidateCount": shard["candidateCount"],
                        "inputCharacters": shard["inputCharacters"],
                    }
                    for shard in manifest_shards
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
