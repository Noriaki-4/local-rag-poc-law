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
FINDINGS = {"established", "not_established", "uncertain"}


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


def _validate_semantic_decision(
    packet: dict[str, Any],
    decision: dict[str, Any],
) -> None:
    """Validate IDs and output algebra without changing a meaning judgment."""

    basis_id = str(packet["basisEdgeId"])
    if decision.get("candidateKey") != packet.get("candidateKey"):
        raise ValueError(f"semantic candidateKey mismatch: {basis_id}")
    if decision.get("basisEdgeId") != basis_id:
        raise ValueError(f"semantic basisEdgeId mismatch: {basis_id}")
    status = decision.get("adjudicationStatus")
    if status not in {"accepted", "needs_resolution"}:
        raise ValueError(
            f"gold semantic decision must be accepted or needs_resolution: {basis_id}"
        )

    assessments = decision.get("predicateAssessments")
    if not isinstance(assessments, dict) or set(assessments) != set(
        PREDICATE_ORDER
    ):
        raise ValueError(f"semantic decision must assess all five predicates: {basis_id}")
    established: set[str] = set()
    for predicate in PREDICATE_ORDER:
        assessment = assessments[predicate]
        if not isinstance(assessment, dict) or set(assessment) != {
            "firstCondition",
            "secondCondition",
            "finding",
        }:
            raise ValueError(f"invalid predicate assessment shape: {basis_id} {predicate}")
        first = assessment["firstCondition"]
        second = assessment["secondCondition"]
        finding = assessment["finding"]
        if {first, second, finding} - FINDINGS:
            raise ValueError(f"unknown predicate finding: {basis_id} {predicate}")
        expected = (
            "established"
            if first == second == "established"
            else "not_established"
            if "not_established" in {first, second}
            else "uncertain"
        )
        if finding != expected:
            raise ValueError(
                f"predicate finding violates condition algebra: {basis_id} {predicate}"
            )
        if status == "accepted" and finding == "uncertain":
            raise ValueError(
                f"accepted gold decision cannot remain uncertain: {basis_id} {predicate}"
            )
        if status == "needs_resolution" and finding != "uncertain":
            raise ValueError(
                f"needs_resolution must not classify predicates: {basis_id} {predicate}"
            )
        if finding == "established":
            established.add(predicate)

    source = packet["referenceSourceArticle"]
    target = packet["referenceTargetArticle"]
    source_id = str(source["articleId"])
    target_id = str(target["articleId"])
    endpoints = {source_id, target_id}
    target_span_ids = {str(span["spanId"]) for span in target["spans"]}
    occurrences = {
        str(item["occurrenceHash"]): item for item in packet["referenceOccurrences"]
    }
    assertions = decision.get("assertions")
    if not isinstance(assertions, list):
        raise ValueError(f"semantic assertions must be a list: {basis_id}")
    asserted = [str(item.get("proposedPredicate")) for item in assertions]
    if len(asserted) != len(set(asserted)) or set(asserted) != established:
        raise ValueError(f"established predicates and assertions must match: {basis_id}")
    if status == "needs_resolution" and (
        assertions or not str(decision.get("note") or "").strip()
    ):
        raise ValueError(
            f"needs_resolution requires no assertions and a note: {basis_id}"
        )
    for assertion in assertions:
        predicate = str(assertion["proposedPredicate"])
        occurrence_hash = str(assertion["referenceOccurrenceHash"])
        occurrence = occurrences.get(occurrence_hash)
        if occurrence is None:
            raise ValueError(f"unknown assertion occurrence: {basis_id} {predicate}")
        subject_id = str(assertion["subjectArticleId"])
        object_id = str(assertion["objectArticleId"])
        if {subject_id, object_id} != endpoints:
            raise ValueError(
                f"assertion must use both known Article endpoints: {basis_id} {predicate}"
            )
        if (
            assertion["referenceSourceSupportingSpanId"]
            not in occurrence["sourceSpanIds"]
        ):
            raise ValueError(
                f"unknown physical source grounding span: {basis_id} {predicate}"
            )
        if assertion["referenceTargetSupportingSpanId"] not in target_span_ids:
            raise ValueError(
                f"unknown physical target grounding span: {basis_id} {predicate}"
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts-dir", type=Path, required=True)
    parser.add_argument("--legal-fixture-output", type=Path, required=True)
    parser.add_argument("--audit-output", type=Path, required=True)
    parser.add_argument("--dataset-manifest-output", type=Path, required=True)
    parser.add_argument("--guidance-fixture", type=Path, required=True)
    parser.add_argument("--manual-structure-adjudications", type=Path, required=True)
    parser.add_argument("--manual-adjudications", type=Path, required=True)
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
    manual_adjudications = _index(
        _load_jsonl(args.manual_adjudications), label="manual adjudications"
    )
    manual_structure_adjudications = _index(
        _load_jsonl(args.manual_structure_adjudications),
        label="manual structure adjudications",
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
    if not set(manual_adjudications).issubset(packet_ids):
        parser.error("manual adjudication contains an unknown basisEdgeId")
    if not set(manual_structure_adjudications).issubset(packet_ids):
        parser.error("manual structure adjudication contains an unknown basisEdgeId")

    fixtures: list[dict[str, Any]] = []
    audit_records: list[dict[str, Any]] = []
    for packet in packets:
        basis_id = str(packet["basisEdgeId"])
        original_structural = structure[basis_id]
        manual_structural = manual_structure_adjudications.get(basis_id)
        structural = manual_structural or original_structural
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
        manual = manual_adjudications.get(basis_id)
        if structural_status != "valid_pair":
            semantic_status = "not_applicable"
            predicates: list[str] | None = None
            annotation = str(structural["note"])
            semantic = None
            dispute = None
            manual = None
            adjudication_source = "gpt_5_6_sol_full_manual_gold_2026_08_19"
        elif manual is not None:
            semantic = manual["semanticDecision"]
            try:
                _validate_semantic_decision(packet, semantic)
            except ValueError as exc:
                parser.error(str(exc))
            if semantic["adjudicationStatus"] == "needs_resolution":
                semantic_status = "needs_resolution"
                predicates = None
            else:
                semantic_status = "codex_verified"
                established = {
                    str(assertion["proposedPredicate"])
                    for assertion in semantic.get("assertions", [])
                }
                predicates = [
                    value for value in PREDICATE_ORDER if value in established
                ]
            dispute = manual.get("semanticDispute", dispute)
            annotation = str(manual["annotationBasis"])
            adjudication_source = str(
                manual.get(
                    "adjudicationSource",
                    "gpt_5_6_sol_full_manual_gold_2026_08_19",
                )
            )
        elif dispute is not None:
            parser.error(f"unresolved semantic decision has no manual adjudication: {basis_id}")
        elif semantic is not None and semantic.get("adjudicationStatus") == "accepted":
            semantic_status = "codex_verified"
            try:
                _validate_semantic_decision(packet, semantic)
            except ValueError as exc:
                parser.error(str(exc))
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
            annotation += "Codex GPT-5.6 SolがArticleペアと引用文脈を確認して確定した。"
            adjudication_source = "gpt_5_6_sol_full_manual_gold_2026_08_19"
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
            "adjudicationSource": adjudication_source,
        }
        fixtures.append(fixture)
        audit_records.append(
            {
                "fixtureId": fixture["fixtureId"],
                "basisEdgeId": basis_id,
                "structuralDecision": structural,
                "originalStructuralDecision": (
                    original_structural if manual_structural is not None else None
                ),
                "manualStructureAdjudication": manual_structural,
                "semanticDecision": semantic,
                "semanticDispute": dispute,
                "manualFinalAdjudication": manual,
                "initialReview": initial_reviews.get(basis_id),
                "finalDifferentialReview": final_reviews.get(basis_id),
                "finalGoldAudit": {
                    "ownerModel": "gpt-5.6-sol",
                    "status": "verified",
                    "structureReviewed": True,
                    "meaningReviewed": structural_status == "valid_pair",
                },
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
            "answerKeyModel": "gpt-5.6-sol",
            "answerKeyAuditScope": "94 legal relation cases and 6 guidance cases",
            "answerKeyVerifiedCaseCount": len(fixtures) + len(guidance),
            "semanticVerifiedPairCount": sum(
                fixture["expectedSemanticStatus"] == "codex_verified"
                for fixture in fixtures
            ),
            "priorLunaArtifactsRetainedForAudit": True,
            "manualStructureCorrectionCount": len(
                manual_structure_adjudications
            ),
            "manualSemanticCorrectionCount": len(manual_adjudications),
            "finalAudit": (
                "Codex full 100-case manual audit plus Article-version and "
                "parent-law list-scope corrections"
            ),
            "meaningJudgmentByProgram": False,
        },
    }
    if len(fixtures) != 94 or len(guidance) != 6 or dataset_manifest["caseCount"] != 100:
        parser.error("dataset must contain 94 legal and 6 guidance cases")
    if any(
        fixture["expectedResolutionStatus"] == "resolved"
        and fixture["expectedPredicates"] is None
        and fixture["expectedSemanticStatus"] != "needs_resolution"
        for fixture in fixtures
    ):
        parser.error(
            "resolved pairs without predicates must explicitly be needs_resolution"
        )

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
