"""Live indexからlabel-freeなRelation意味分類packetをexportする。"""

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
    canonical_packet_jsonl,
    exclude_completed_packet_records,
    packet_records_from_candidates,
)
from app.graph_client import GraphClient  # noqa: E402
from app.legal_relation_classification_job import (  # noqa: E402
    candidates_from_graph_and_sources,
    group_reference_rows_by_article_pair,
)
from app.opensearch_client import OpenSearchClient  # noqa: E402


def _completed_candidate_keys(paths: list[Path]) -> set[str]:
    keys: set[str] = set()
    for path in paths:
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            record = json.loads(line)
            candidate_key = str(record.get("candidateKey") or "")
            if not candidate_key:
                raise ValueError(f"{path}:{line_number}: candidateKey is missing")
            if candidate_key in keys:
                raise ValueError(f"{path}:{line_number}: duplicate candidateKey")
            keys.add(candidate_key)
    return keys


def _atomic_create(path: Path, data: bytes) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing packet: {path}")
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
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--completed-jsonl", type=Path, action="append", default=[])
    parser.add_argument("--provider", default="codex_subscription")
    parser.add_argument("--worker-model", default="gpt-5.6-luna")
    parser.add_argument("--reviewer-model", default="gpt-5.6-luna")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")

    graph = GraphClient()
    try:
        source_state = graph.classification_source_state()
        rows = graph.reference_candidates_for_classification(
            source_snapshot_id=str(source_state["sourceSnapshotId"]),
        )
    finally:
        graph.close()
    eligible_groups = group_reference_rows_by_article_pair(rows)
    selected_groups = eligible_groups[: args.limit] if args.limit else eligible_groups
    selected_rows = [row for group in selected_groups for row in group]
    article_ids = list(
        dict.fromkeys(
            str(row[key].get("graphNodeId") or "")
            for row in selected_rows
            for key in ("referenceSourceArticle", "referenceTargetArticle")
        )
    )
    sources = OpenSearchClient().get_complete_articles_by_ids(
        article_ids,
        user_clearance_level=3,
    )
    candidates = candidates_from_graph_and_sources(
        selected_rows,
        sources,
        source_snapshot_id=str(source_state["sourceSnapshotId"]),
        graph_schema_version=int(source_state["graphSchemaVersion"]),
        provider=args.provider,
        model=args.worker_model,
        reviewer_model=args.reviewer_model,
    )
    all_records = packet_records_from_candidates(selected_rows, candidates)
    completed = _completed_candidate_keys(args.completed_jsonl)
    remaining = exclude_completed_packet_records(all_records, completed)
    _atomic_create(args.output, canonical_packet_jsonl(remaining))
    print(
        json.dumps(
            {
                "sourceSnapshotId": source_state["sourceSnapshotId"],
                "graphSchemaVersion": source_state["graphSchemaVersion"],
                "candidateCount": len(all_records),
                "eligibleCandidateCount": len(eligible_groups),
                "basisEdgeCount": len(selected_rows),
                "completedCandidateCount": len(completed),
                "exportedCandidateCount": len(remaining),
                "expectedLabelsIncluded": False,
                "output": str(args.output.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
