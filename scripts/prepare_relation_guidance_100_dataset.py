#!/usr/bin/env python3
"""Prepare a reproducible 100-case legal-relation and guidance dataset.

The two lanes intentionally keep separate schemas: legal Article pairs require
semantic adjudication, while guidance cases test deterministic EXPLAINS
navigation.  A manifest binds both lanes into five 20-case evaluation shards.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


LEGAL_QUOTAS = {
    "parent_law_reference": 22,
    "application": 18,
    "definition": 18,
    "exception": 18,
    "article_reference": 18,
}
SUPPLEMENTARY_QUOTAS = {
    "parent_law_reference": 1,
    "application": 2,
    "definition": 5,
    "exception": 8,
    "article_reference": 6,
}
LEGAL_COUNT = sum(LEGAL_QUOTAS.values())
GUIDANCE_COUNT = 6
SHARD_COUNT = 5
CASES_PER_SHARD = 20


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _canonical_lines(records: list[dict[str, Any]]) -> str:
    return "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        for record in records
    )


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


def _document_id(article_id: str) -> str:
    return article_id.split("-article-", 1)[0]


def _stable_key(record: dict[str, Any]) -> str:
    basis_id = str(record["basisEdgeId"])
    return hashlib.sha256(basis_id.encode("utf-8")).hexdigest()


def _select_by_document(
    records: list[dict[str, Any]], *, count: int
) -> list[dict[str, Any]]:
    by_document: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        source_id = str(record["referenceSourceArticle"]["articleId"])
        document_id = _document_id(source_id).split("-suppl-", 1)[0]
        by_document[document_id].append(record)
    for values in by_document.values():
        values.sort(key=_stable_key)

    # Rare documents are visited first in every round.  This prevents large Acts
    # from crowding ordinances and supplementary provisions out of the fixture.
    document_ids = sorted(
        by_document,
        key=lambda document_id: (len(by_document[document_id]), document_id),
    )
    selected: list[dict[str, Any]] = []
    offset = 0
    while len(selected) < count:
        added = False
        for document_id in document_ids:
            values = by_document[document_id]
            if offset < len(values):
                selected.append(values[offset])
                added = True
                if len(selected) == count:
                    break
        if not added:
            raise ValueError(f"not enough candidates: need {count}")
        offset += 1
    return selected


def _select_diverse(
    records: list[dict[str, Any]], *, kind: str, count: int
) -> list[dict[str, Any]]:
    eligible = [record for record in records if record.get("referenceKind") == kind]
    supplementary_count = SUPPLEMENTARY_QUOTAS[kind]
    supplementary = [
        record
        for record in eligible
        if "-suppl-" in str(record["referenceSourceArticle"]["articleId"])
    ]
    main = [record for record in eligible if record not in supplementary]
    return [
        *_select_by_document(main, count=count - supplementary_count),
        *_select_by_document(supplementary, count=supplementary_count),
    ]


def _prepare_shards(
    legal_records: list[dict[str, Any]], guidance_records: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
    shards: list[list[dict[str, Any]]] = [[] for _ in range(SHARD_COUNT)]
    manifest_records: list[dict[str, Any]] = []
    for index, record in enumerate(legal_records):
        shard_index = index % SHARD_COUNT
        item = {
            "caseType": "legal_relation",
            "fixtureId": f"legal-relation-100-{index + 1:03d}",
            "shardId": f"shard-{shard_index + 1:02d}",
            "basisEdgeId": record["basisEdgeId"],
            "referenceKind": record["referenceKind"],
        }
        shards[shard_index].append(item)
        manifest_records.append(item)

    for index, record in enumerate(guidance_records):
        shard_index = min(range(SHARD_COUNT), key=lambda value: len(shards[value]))
        item = {
            "caseType": "guidance_navigation",
            "fixtureId": record["fixtureId"],
            "shardId": f"shard-{shard_index + 1:02d}",
            "expectedGuideDocumentId": record["expectedGuideDocumentId"],
        }
        shards[shard_index].append(item)
        manifest_records.append(item)

    if [len(shard) for shard in shards] != [CASES_PER_SHARD] * SHARD_COUNT:
        raise AssertionError("dataset shards are not exactly 20 cases each")
    return manifest_records, shards


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legal-pool", type=Path, required=True)
    parser.add_argument("--guidance-fixture", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--structure-packet",
        type=Path,
        help="optional 94-case blind structural packet to split without querying Graph",
    )
    parser.add_argument(
        "--structure-results-dir",
        type=Path,
        help="optional directory containing shard-NN-structure-results.jsonl",
    )
    args = parser.parse_args()

    pool = _load_jsonl(args.legal_pool)
    guidance = _load_jsonl(args.guidance_fixture)
    if len(guidance) != GUIDANCE_COUNT:
        parser.error(f"expected {GUIDANCE_COUNT} guidance cases, got {len(guidance)}")

    selected: list[dict[str, Any]] = []
    for kind, count in LEGAL_QUOTAS.items():
        selected.extend(_select_diverse(pool, kind=kind, count=count))
    if len(selected) != LEGAL_COUNT:
        raise AssertionError("legal selection count mismatch")
    basis_ids = [str(record["basisEdgeId"]) for record in selected]
    if len(basis_ids) != len(set(basis_ids)):
        raise AssertionError("legal selection contains duplicate basisEdgeId")

    manifest_records, shards = _prepare_shards(selected, guidance)
    output_dir = args.output_dir.resolve()
    _atomic_write(output_dir / "legal-blind-packet.jsonl", _canonical_lines(selected))
    _atomic_write(output_dir / "dataset-manifest.jsonl", _canonical_lines(manifest_records))
    for index, shard in enumerate(shards, start=1):
        legal_ids = {
            str(item["basisEdgeId"])
            for item in shard
            if item["caseType"] == "legal_relation"
        }
        legal_packet = [
            record for record in selected if str(record["basisEdgeId"]) in legal_ids
        ]
        _atomic_write(
            output_dir / f"shard-{index:02d}-legal-blind.jsonl",
            _canonical_lines(legal_packet),
        )
        _atomic_write(
            output_dir / f"shard-{index:02d}-manifest.jsonl",
            _canonical_lines(shard),
        )

    structure_by_basis: dict[str, dict[str, Any]] | None = None
    if args.structure_packet is not None:
        structure_records = _load_jsonl(args.structure_packet)
        structure_by_basis = {
            str(record["basisEdgeId"]): record for record in structure_records
        }
        if set(structure_by_basis) != set(basis_ids):
            parser.error("structure packet basisEdgeIds do not match legal selection")
        for index, shard in enumerate(shards, start=1):
            shard_basis_ids = [
                str(item["basisEdgeId"])
                for item in shard
                if item["caseType"] == "legal_relation"
            ]
            _atomic_write(
                output_dir / f"shard-{index:02d}-structure-audit.jsonl",
                _canonical_lines(
                    [structure_by_basis[basis_id] for basis_id in shard_basis_ids]
                ),
            )

    structural_status_counts: Counter[str] | None = None
    if args.structure_results_dir is not None:
        packet_by_basis = {
            str(record["basisEdgeId"]): record for record in selected
        }
        ordered_results: list[dict[str, Any]] = []
        for index, shard in enumerate(shards, start=1):
            results_path = (
                args.structure_results_dir
                / f"shard-{index:02d}-structure-results.jsonl"
            )
            results = _load_jsonl(results_path)
            expected_ids = [
                str(item["basisEdgeId"])
                for item in shard
                if item["caseType"] == "legal_relation"
            ]
            if [str(item.get("basisEdgeId")) for item in results] != expected_ids:
                parser.error(f"{results_path}: result order or coverage mismatch")
            semantic_packet: list[dict[str, Any]] = []
            for result in results:
                basis_id = str(result["basisEdgeId"])
                packet = packet_by_basis[basis_id]
                expected_structure_key = (
                    structure_by_basis[basis_id]["candidateKey"]
                    if structure_by_basis is not None
                    else None
                )
                if (
                    expected_structure_key is not None
                    and result.get("candidateKey") != expected_structure_key
                ):
                    parser.error(f"{results_path}: candidateKey mismatch for {basis_id}")
                status = result.get("structuralStatus")
                selected_target = result.get("selectedTargetArticleId")
                current_target = packet["referenceTargetArticle"]["articleId"]
                if status == "valid_pair" and selected_target != current_target:
                    parser.error(f"{results_path}: invalid valid_pair algebra")
                if status == "wrong_target" and (
                    not selected_target or selected_target == current_target
                ):
                    parser.error(f"{results_path}: invalid wrong_target algebra")
                if status in {"not_reference", "unresolved"} and selected_target is not None:
                    parser.error(f"{results_path}: invalid null-target algebra")
                if status not in {
                    "valid_pair",
                    "not_reference",
                    "wrong_target",
                    "unresolved",
                }:
                    parser.error(f"{results_path}: unknown structuralStatus {status}")
                if status == "valid_pair":
                    semantic_packet.append(packet)
            ordered_results.extend(results)
            _atomic_write(
                output_dir / f"shard-{index:02d}-semantic-blind.jsonl",
                _canonical_lines(semantic_packet),
            )
        _atomic_write(
            output_dir / "legal-structure-adjudication.jsonl",
            _canonical_lines(ordered_results),
        )
        structural_status_counts = Counter(
            str(result["structuralStatus"]) for result in ordered_results
        )

    source_documents = Counter(
        _document_id(str(record["referenceSourceArticle"]["articleId"]))
        for record in selected
    )
    summary = {
        "caseCount": len(manifest_records),
        "legalCaseCount": len(selected),
        "guidanceCaseCount": len(guidance),
        "shardCaseCounts": [len(shard) for shard in shards],
        "legalCountsByReferenceKind": Counter(
            str(record["referenceKind"]) for record in selected
        ),
        "legalCountsBySourceDocument": source_documents,
        "supplementaryProvisionCaseCount": sum(
            "-suppl-" in str(record["referenceSourceArticle"]["articleId"])
            for record in selected
        ),
    }
    if structural_status_counts is not None:
        summary["legalCountsByStructuralStatus"] = structural_status_counts
        summary["semanticAdjudicationCaseCount"] = structural_status_counts[
            "valid_pair"
        ]
    _atomic_write(
        output_dir / "selection-summary.json",
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
