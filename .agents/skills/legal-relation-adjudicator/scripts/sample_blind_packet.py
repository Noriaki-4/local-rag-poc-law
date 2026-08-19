"""Sample a deterministic, label-free candidate pool from live legal indexes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_KINDS = (
    "application",
    "parent_law_reference",
    "definition",
    "exception",
    "article_reference",
)


def _basis_ids(path: Path) -> set[str]:
    return {
        str(record["basisEdgeId"])
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
        for record in [json.loads(line)]
    }


def _ordered_basis_ids(path: Path) -> list[str]:
    basis_ids: list[str] = []
    seen: set[str] = set()
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        record = json.loads(line)
        basis_id = str(record.get("basisEdgeId") or "")
        if not basis_id:
            raise ValueError(f"{path}:{line_number}: missing basisEdgeId")
        if basis_id in seen:
            raise ValueError(f"{path}:{line_number}: duplicate basisEdgeId")
        seen.add(basis_id)
        basis_ids.append(basis_id)
    if not basis_ids:
        raise ValueError(f"{path}: selection is empty")
    return basis_ids


def _stable_order(row: dict[str, Any]) -> str:
    basis_id = str(row["basis"].get("graphEdgeId") or "")
    return hashlib.sha256(basis_id.encode("utf-8")).hexdigest()


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
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--per-kind", type=int, default=12)
    parser.add_argument("--kind", action="append", dest="kinds")
    parser.add_argument("--exclude-jsonl", type=Path, action="append", default=[])
    parser.add_argument("--provider", default="codex_subscription")
    parser.add_argument("--worker-model", default="gpt-5.6-luna")
    parser.add_argument("--reviewer-model", default="gpt-5.6-luna")
    parser.add_argument(
        "--select-basis-jsonl",
        type=Path,
        help="regenerate exactly these basisEdgeIds in file order",
    )
    parser.add_argument(
        "--select-basis-id",
        action="append",
        default=[],
        help="regenerate this basisEdgeId; repeat to preserve an explicit order",
    )
    args = parser.parse_args()
    if args.per_kind < 1:
        parser.error("--per-kind must be positive")
    kinds = tuple(args.kinds or DEFAULT_KINDS)
    if len(kinds) != len(set(kinds)):
        parser.error("--kind values must be unique")

    repo_root = args.repo_root.resolve()
    sys.path.insert(0, str(repo_root / "agent-api"))
    from app.graph_client import GraphClient
    from app.legal_relation_classification_job import (
        candidates_from_graph_and_sources,
        group_reference_rows_by_article_pair,
    )
    from app.opensearch_client import OpenSearchClient

    excluded: set[str] = set()
    for path in args.exclude_jsonl:
        excluded.update(_basis_ids(path))

    graph = GraphClient()
    try:
        source_state = graph.classification_source_state()
        rows = graph.reference_candidates_for_classification(
            source_snapshot_id=str(source_state["sourceSnapshotId"])
        )
    finally:
        graph.close()

    selected_seed_rows: list[dict[str, Any]] = []
    if args.select_basis_jsonl is not None and args.select_basis_id:
        parser.error("use only one of --select-basis-jsonl and --select-basis-id")
    if args.select_basis_jsonl is not None or args.select_basis_id:
        rows_by_basis_id = {
            str(row["basis"].get("graphEdgeId") or ""): row for row in rows
        }
        selected_basis_ids = (
            _ordered_basis_ids(args.select_basis_jsonl)
            if args.select_basis_jsonl is not None
            else list(args.select_basis_id)
        )
        if len(selected_basis_ids) != len(set(selected_basis_ids)):
            parser.error("selected basisEdgeIds must be unique")
        missing = [
            basis_id for basis_id in selected_basis_ids if basis_id not in rows_by_basis_id
        ]
        if missing:
            parser.error(f"selected basisEdgeIds are absent from Graph: {missing}")
        selected_seed_rows = [
            rows_by_basis_id[basis_id] for basis_id in selected_basis_ids
        ]
    else:
        for kind in kinds:
            eligible = [
                row
                for row in rows
                if row["basis"].get("referenceKind") == kind
                and str(row["basis"].get("graphEdgeId") or "") not in excluded
                and row["referenceSourceArticle"].get("graphNodeId")
                != row["referenceTargetArticle"].get("graphNodeId")
            ]
            eligible.sort(key=_stable_order)
            if len(eligible) < args.per_kind:
                parser.error(f"not enough candidates for {kind}: {len(eligible)}")
            selected_seed_rows.extend(eligible[: args.per_kind])

    selected_pairs = {
        (
            str(row["referenceSourceArticle"].get("graphNodeId") or ""),
            str(row["referenceTargetArticle"].get("graphNodeId") or ""),
        )
        for row in selected_seed_rows
    }
    selected_rows = [
        row
        for row in rows
        if (
            str(row["referenceSourceArticle"].get("graphNodeId") or ""),
            str(row["referenceTargetArticle"].get("graphNodeId") or ""),
        )
        in selected_pairs
    ]

    article_ids = list(
        dict.fromkeys(
            str(row[key]["graphNodeId"])
            for row in selected_rows
            for key in ("referenceSourceArticle", "referenceTargetArticle")
        )
    )
    sources = OpenSearchClient().get_complete_articles_by_ids(
        article_ids, user_clearance_level=3
    )
    records: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for group in group_reference_rows_by_article_pair(selected_rows):
        basis_ids = sorted(str(row["basis"]["graphEdgeId"]) for row in group)
        try:
            (candidate,) = candidates_from_graph_and_sources(
                list(group),
                sources,
                source_snapshot_id=str(source_state["sourceSnapshotId"]),
                graph_schema_version=int(source_state["graphSchemaVersion"]),
                provider=args.provider,
                model=args.worker_model,
                reviewer_model=args.reviewer_model,
            )
        except ValueError as error:
            skipped.append({"basisEdgeIds": basis_ids, "error": str(error)})
            continue
        value = candidate.model_dump(by_alias=True, mode="json")
        records.append(
            {
                "candidateKey": candidate.candidate_key,
                "sourceSnapshotId": value["sourceSnapshotId"],
                "graphSchemaVersion": value["graphSchemaVersion"],
                "promptVersion": value["promptVersion"],
                "provider": value["provider"],
                "model": value["model"],
                "reviewerModel": value["reviewerModel"],
                "basisEdgeIds": value["basisEdgeIds"],
                "referenceOccurrences": value["referenceOccurrences"],
                "referenceSourceArticle": value["referenceSource"],
                "referenceTargetArticle": value["referenceTarget"],
            }
        )

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
                "requestedBasisEdgeCount": len(selected_seed_rows),
                "expandedBasisEdgeCount": len(selected_rows),
                "skipped": skipped,
                "output": str(args.output.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
