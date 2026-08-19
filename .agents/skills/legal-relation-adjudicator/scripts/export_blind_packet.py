"""Export structurally valid relation candidates without expected labels."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


def _load_fixture(path: Path) -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [
        row
        for row in rows
        if row.get("expectedResolutionStatus") == "resolved"
        and row.get("expectedReferenceTargetArticleId")
        == row.get("currentReferenceTargetArticleId")
        and row.get("expectedPredicates") is not None
    ]


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
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provider", default="codex_subscription")
    parser.add_argument("--worker-model", default="gpt-5.6-luna")
    parser.add_argument("--reviewer-model", default="gpt-5.6-luna")
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    sys.path.insert(0, str(repo_root / "agent-api"))
    from app.graph_client import GraphClient
    from app.legal_relation_classification_job import (
        candidates_from_graph_and_sources,
    )
    from app.opensearch_client import OpenSearchClient

    fixtures = _load_fixture(args.fixture)
    basis_ids = {str(item["basisEdgeId"]) for item in fixtures}
    graph = GraphClient()
    try:
        source_state = graph.classification_source_state()
        all_rows = graph.reference_candidates_for_classification(
            source_snapshot_id=str(source_state["sourceSnapshotId"])
        )
    finally:
        graph.close()
    rows_by_basis = {str(row["basis"]["graphEdgeId"]): row for row in all_rows}
    missing = basis_ids.difference(rows_by_basis)
    if missing:
        parser.error(f"basis edges are missing: {sorted(missing)}")
    seed_rows = [rows_by_basis[str(item["basisEdgeId"])] for item in fixtures]
    selected_pairs = {
        (
            str(row["referenceSourceArticle"].get("graphNodeId") or ""),
            str(row["referenceTargetArticle"].get("graphNodeId") or ""),
        )
        for row in seed_rows
    }
    ordered_rows = [
        row
        for row in all_rows
        if (
            str(row["referenceSourceArticle"].get("graphNodeId") or ""),
            str(row["referenceTargetArticle"].get("graphNodeId") or ""),
        )
        in selected_pairs
    ]
    article_ids = list(
        dict.fromkeys(
            str(row[key]["graphNodeId"])
            for row in ordered_rows
            for key in ("referenceSourceArticle", "referenceTargetArticle")
        )
    )
    opensearch = OpenSearchClient()
    sources = opensearch.get_complete_articles_by_ids(
        article_ids, user_clearance_level=3
    )
    candidates = candidates_from_graph_and_sources(
        ordered_rows,
        sources,
        source_snapshot_id=str(source_state["sourceSnapshotId"]),
        graph_schema_version=int(source_state["graphSchemaVersion"]),
        provider=args.provider,
        model=args.worker_model,
        reviewer_model=args.reviewer_model,
    )
    records = []
    for candidate in candidates:
        record = candidate.model_dump(by_alias=True, mode="json")
        records.append(
            {
                "candidateKey": candidate.candidate_key,
                "sourceSnapshotId": record["sourceSnapshotId"],
                "graphSchemaVersion": record["graphSchemaVersion"],
                "promptVersion": record["promptVersion"],
                "provider": record["provider"],
                "model": record["model"],
                "reviewerModel": record["reviewerModel"],
                "basisEdgeIds": record["basisEdgeIds"],
                "referenceOccurrences": record["referenceOccurrences"],
                "referenceSourceArticle": record["referenceSource"],
                "referenceTargetArticle": record["referenceTarget"],
            }
        )
    forbidden_keys = {"expectedPredicates", "expectedFindings", "annotationBasis"}
    if any(forbidden_keys.intersection(record) for record in records):
        raise RuntimeError("blind packet contains expected labels")
    _atomic_write(
        args.output,
        "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in records
        ),
    )
    print(
        json.dumps(
            {
                "candidateCount": len(records),
                "output": str(args.output.resolve()),
                "expectedLabelsIncluded": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
