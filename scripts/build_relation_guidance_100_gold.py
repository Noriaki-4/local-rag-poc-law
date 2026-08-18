#!/usr/bin/env python3
"""Build the checked 100-case evaluation fixture from adjudication artifacts."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any


PREDICATE_ORDER = (
    "IMPLEMENTS",
    "INCORPORATES",
    "USES_DEFINITION",
    "EXCEPTION_TO",
    "OVERRIDES",
)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _load_glob(directory: Path, pattern: str) -> list[dict[str, Any]]:
    return [record for path in sorted(directory.glob(pattern)) for record in _load_jsonl(path)]


def _index(records: list[dict[str, Any]], *, label: str) -> dict[str, dict[str, Any]]:
    result = {str(record["basisEdgeId"]): record for record in records}
    if len(result) != len(records):
        raise ValueError(f"duplicate basisEdgeId in {label}")
    return result


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


def _jsonl(records: list[dict[str, Any]]) -> str:
    return "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        for record in records
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts-dir", type=Path, required=True)
    parser.add_argument("--legal-fixture-output", type=Path, required=True)
    parser.add_argument("--audit-output", type=Path, required=True)
    parser.add_argument("--dataset-manifest-output", type=Path, required=True)
    parser.add_argument("--guidance-fixture", type=Path, required=True)
    args = parser.parse_args()

    artifacts = args.artifacts_dir.resolve()
    packets = _load_jsonl(artifacts / "legal-blind-packet.jsonl")
    structure = _index(
        _load_glob(artifacts, "shard-??-structure-results.jsonl"),
        label="structure results",
    )
    approved = _index(
        _load_glob(artifacts, "shard-??-approved.jsonl"), label="approved semantics"
    )
    semantic_unresolved = _index(
        _load_glob(artifacts, "shard-??-unresolved.jsonl"),
        label="unresolved semantics",
    )
    initial_reviews = _index(
        _load_glob(artifacts, "shard-??-review-initial.jsonl"),
        label="initial reviews",
    )
    final_reviews = _index(
        _load_glob(artifacts, "shard-??-review-final.jsonl"),
        label="final reviews",
    )
    fixture_ids = {
        str(record["basisEdgeId"]): str(record["fixtureId"])
        for record in _load_jsonl(artifacts / "dataset-manifest.jsonl")
        if record["caseType"] == "legal_relation"
    }
    packet_ids = {str(packet["basisEdgeId"]) for packet in packets}
    if set(structure) != packet_ids or set(fixture_ids) != packet_ids:
        parser.error("legal packet, manifest, and structural decisions must match")

    fixtures: list[dict[str, Any]] = []
    audit_records: list[dict[str, Any]] = []
    for packet in packets:
        basis_id = str(packet["basisEdgeId"])
        structural = structure[basis_id]
        structural_status = str(structural["structuralStatus"])
        if structural_status == "valid_pair":
            resolution_status = "resolved"
            expected_target = structural["selectedTargetArticleId"]
        elif structural_status == "wrong_target":
            resolution_status = "resolved"
            expected_target = structural["selectedTargetArticleId"]
        else:
            resolution_status = structural_status
            expected_target = None

        semantic = approved.get(basis_id)
        dispute = semantic_unresolved.get(basis_id)
        if structural_status != "valid_pair":
            semantic_status = "not_applicable"
            predicates: list[str] | None = None
            annotation = str(structural["note"])
            semantic = None
            dispute = None
        elif dispute is not None:
            semantic_status = "unresolved_after_single_revision"
            predicates = None
            annotation = (
                "WorkerとReviewerが1回の差し戻し後も一致しないため、"
                "意味ラベルを教師値に採用しない。"
            )
            semantic = None
        elif semantic is not None and semantic.get("adjudicationStatus") == "accepted":
            semantic_status = "reviewer_approved"
            established = {
                str(assertion["proposedPredicate"])
                for assertion in semantic.get("assertions", [])
            }
            predicates = [value for value in PREDICATE_ORDER if value in established]
            annotation = (
                f"全5述語を同時評価し、{', '.join(predicates)}が成立。"
                if predicates
                else "全5述語を同時評価し、成立predicateなし。"
            )
            annotation += "Worker判定をReviewerが承認し、Codexが横断監査した。"
        else:
            parser.error(f"valid pair lacks approved or unresolved semantics: {basis_id}")

        fixture = {
            "fixtureId": fixture_ids[basis_id],
            "basisEdgeId": basis_id,
            "referenceKind": packet["referenceKind"],
            "referenceSourceArticleId": packet["referenceSourceArticle"]["articleId"],
            "currentReferenceTargetArticleId": packet["referenceTargetArticle"]["articleId"],
            "expectedResolutionStatus": resolution_status,
            "expectedReferenceTargetArticleId": expected_target,
            "expectedSemanticStatus": semantic_status,
            "expectedPredicates": predicates,
            "annotationBasis": annotation,
            "adjudicationSource": "codex_manual_audit_luna_worker_reviewer_2026_08_19",
        }
        fixtures.append(fixture)
        audit_records.append(
            {
                "fixtureId": fixture["fixtureId"],
                "basisEdgeId": basis_id,
                "structuralDecision": structural,
                "semanticDecision": semantic,
                "semanticDispute": dispute,
                "initialReview": initial_reviews.get(basis_id),
                "finalDifferentialReview": final_reviews.get(basis_id),
            }
        )

    guidance = _load_jsonl(args.guidance_fixture)
    resolution_counts = Counter(
        str(fixture["expectedResolutionStatus"]) for fixture in fixtures
    )
    semantic_counts = Counter(
        str(fixture["expectedSemanticStatus"]) for fixture in fixtures
    )
    predicate_counts = Counter(
        predicate
        for fixture in fixtures
        for predicate in (fixture["expectedPredicates"] or [])
    )
    source_documents = {
        str(fixture["referenceSourceArticleId"]).split("-article-", 1)[0].split(
            "-suppl-", 1
        )[0]
        for fixture in fixtures
    }
    dataset_manifest = {
        "datasetId": "legal-relation-guidance-100-v1",
        "schemaVersion": 1,
        "createdAt": "2026-08-19",
        "caseCount": len(fixtures) + len(guidance),
        "shardCount": 5,
        "casesPerShard": 20,
        "executionConcurrency": 3,
        "lanes": [
            {
                "caseType": "legal_relation",
                "fixture": str(args.legal_fixture_output),
                "caseCount": len(fixtures),
            },
            {
                "caseType": "guidance_navigation",
                "fixture": str(args.guidance_fixture),
                "caseCount": len(guidance),
            },
        ],
        "coverage": {
            "sourceLawFamilyCount": len(source_documents),
            "supplementaryProvisionCaseCount": sum(
                "-suppl-" in str(fixture["referenceSourceArticleId"])
                for fixture in fixtures
            ),
            "referenceKindCounts": Counter(
                str(fixture["referenceKind"]) for fixture in fixtures
            ),
            "resolutionStatusCounts": resolution_counts,
            "semanticStatusCounts": semantic_counts,
            "establishedPredicateCounts": predicate_counts,
        },
        "adjudication": {
            "workerModel": "gpt-5.6-luna",
            "reviewerModel": "gpt-5.6-luna",
            "maximumRevisionCount": 1,
            "finalAudit": "Codex cross-shard manual audit",
            "meaningJudgmentByProgram": False,
        },
    }
    if len(fixtures) != 94 or len(guidance) != 6 or dataset_manifest["caseCount"] != 100:
        parser.error("dataset must contain 94 legal and 6 guidance cases")

    _atomic_write(args.legal_fixture_output, _jsonl(fixtures))
    _atomic_write(args.audit_output, _jsonl(audit_records))
    _atomic_write(
        args.dataset_manifest_output,
        json.dumps(dataset_manifest, ensure_ascii=False, indent=2) + "\n",
    )
    print(json.dumps(dataset_manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
