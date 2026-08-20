"""Relation意味分類packetを最大5件の決定的shardへ分割する。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "agent-api"))

from app.domains.legal.adjudication_packets import (  # noqa: E402
    plan_adjudication_shards,
)
from app.domains.legal.relation_classification import (  # noqa: E402
    RelationAdjudicationCandidatePacket,
)


def _load_packet(path: Path) -> tuple[RelationAdjudicationCandidatePacket, ...]:
    records = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            records.append(
                RelationAdjudicationCandidatePacket.model_validate_json(line)
            )
        except ValueError as error:
            raise ValueError(f"{path}:{line_number}: {error}") from error
    return tuple(records)


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-candidates-per-shard", type=int, default=5)
    parser.add_argument("--max-active-sessions", type=int, default=3)
    parser.add_argument(
        "--skill-version",
        default="legal-relation-adjudicator-2026-08-21-pair-v8",
    )
    parser.add_argument("--reasoning-effort", default="high")
    args = parser.parse_args()

    records = _load_packet(args.packet)
    packet_bytes = args.packet.read_bytes()
    manifest, shard_bytes = plan_adjudication_shards(
        records,
        source_packet=args.packet,
        source_packet_bytes=packet_bytes,
        max_candidates_per_shard=args.max_candidates_per_shard,
        max_active_sessions=args.max_active_sessions,
        skill_version=args.skill_version,
        reasoning_effort=args.reasoning_effort,
    )
    output_dir = args.output_dir.resolve()
    planned = [output_dir / name for name in shard_bytes]
    planned.append(output_dir / "manifest.json")
    existing = [path for path in planned if path.exists()]
    if existing:
        parser.error(f"refusing to overwrite existing artifacts: {existing}")
    for filename, data in shard_bytes.items():
        _atomic_create(output_dir / filename, data)
    manifest_bytes = (
        json.dumps(
            manifest.model_dump(by_alias=True, mode="json"),
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    _atomic_create(output_dir / "manifest.json", manifest_bytes)
    print(
        json.dumps(
            {
                "candidateCount": manifest.candidate_count,
                "shardCount": manifest.shard_count,
                "maxCandidatesPerShard": manifest.max_candidates_per_shard,
                "maxActiveSessions": manifest.execution_profile.max_active_sessions,
                "reasoningEffort": manifest.execution_profile.reasoning_effort,
                "manifest": str(output_dir / "manifest.json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
