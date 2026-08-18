"""手動確認した20件で参照先解決と5 predicate分類を分離評価する。"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent-api"))

from app.config import settings
from app.domains.legal.graph_schema import ProposedPredicate
from app.graph_client import GraphClient
from app.legal_relation_classification_job import (
    LegalRelationClassificationJob,
    candidates_from_graph_and_sources,
)
from app.llm import LLMClient
from app.opensearch_client import OpenSearchClient

DEFAULT_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs/requirements/samples/eval/legal_relation_20_adjudicated_fixture.jsonl"
)
RESOLUTION_STATUSES = frozenset({"resolved", "unresolved", "not_reference"})
PREDICATES = tuple(item.value for item in ProposedPredicate)


def load_fixtures(path: Path) -> list[dict[str, Any]]:
    fixtures = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ids = [str(item["fixtureId"]) for item in fixtures]
    basis_ids = [str(item["basisEdgeId"]) for item in fixtures]
    if len(ids) != len(set(ids)):
        raise ValueError("fixtureId must be unique")
    if len(basis_ids) != len(set(basis_ids)):
        raise ValueError("basisEdgeId must be unique")
    for item in fixtures:
        fixture_id = str(item["fixtureId"])
        status = str(item["expectedResolutionStatus"])
        if status not in RESOLUTION_STATUSES:
            raise ValueError(f"invalid resolution status: {fixture_id}: {status}")
        target_id = item.get("expectedReferenceTargetArticleId")
        if (status == "resolved") != (isinstance(target_id, str) and bool(target_id)):
            raise ValueError(
                f"resolved requires a target and other statuses forbid it: {fixture_id}"
            )
        expected = item.get("expectedPredicates")
        if expected is not None:
            if status != "resolved":
                raise ValueError(
                    f"only a resolved pair may have predicate labels: {fixture_id}"
                )
            if not isinstance(expected, list):
                raise ValueError(f"expectedPredicates must be a list or null: {fixture_id}")
            if len(expected) != len(set(expected)) or not set(expected).issubset(
                PREDICATES
            ):
                raise ValueError(f"invalid expectedPredicates: {fixture_id}")
    return fixtures


def select_fixtures(
    fixtures: list[dict[str, Any]], selected_ids: list[str]
) -> list[dict[str, Any]]:
    if not selected_ids:
        return fixtures
    selected = set(selected_ids)
    result = [item for item in fixtures if item["fixtureId"] in selected]
    missing = selected.difference(str(item["fixtureId"]) for item in result)
    if missing:
        raise ValueError(f"unknown fixtureId: {sorted(missing)}")
    return result


def adjudication_sources(fixtures: list[dict[str, Any]]) -> list[str]:
    return sorted(
        {
            str(item["adjudicationSource"])
            for item in fixtures
            if str(item.get("adjudicationSource") or "").strip()
        }
    )


def structural_result(
    fixture: dict[str, Any], row: dict[str, Any] | None
) -> dict[str, Any]:
    actual_source_id = None
    actual_target_id = None
    if row is not None:
        actual_source_id = str(row["referenceSourceArticle"]["graphNodeId"])
        actual_target_id = str(row["referenceTargetArticle"]["graphNodeId"])

    expected_status = str(fixture["expectedResolutionStatus"])
    source_matches = actual_source_id == fixture["referenceSourceArticleId"]
    if expected_status == "resolved":
        passed = (
            source_matches
            and actual_target_id == fixture["expectedReferenceTargetArticleId"]
        )
    else:
        passed = row is None

    return {
        "fixtureId": fixture["fixtureId"],
        "passed": passed,
        "expectedResolutionStatus": expected_status,
        "expectedReferenceTargetArticleId": fixture[
            "expectedReferenceTargetArticleId"
        ],
        "actualReferenceSourceArticleId": actual_source_id,
        "actualReferenceTargetArticleId": actual_target_id,
        "currentGraphMatchesRecordedBaseline": (
            source_matches
            and actual_target_id == fixture["currentReferenceTargetArticleId"]
        ),
        "annotationBasis": fixture["annotationBasis"],
    }


def classify_eligible(
    *,
    fixtures: list[dict[str, Any]],
    rows_by_basis: dict[str, dict[str, Any]],
    structural_by_id: dict[str, dict[str, Any]],
    graph: GraphClient,
    source_state: dict[str, Any],
) -> list[dict[str, Any]]:
    eligible = [
        item
        for item in fixtures
        if structural_by_id[str(item["fixtureId"])]["passed"]
        and item["expectedPredicates"] is not None
    ]
    if not eligible:
        return []

    ordered_rows = [rows_by_basis[str(item["basisEdgeId"])] for item in eligible]
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
        provider=settings.relation_classifier_provider,
        model=settings.relation_classifier_model,
        reviewer_model=settings.relation_classifier_reviewer_model or None,
    )
    candidates_by_basis = {
        candidate.basis_edge_id: candidate for candidate in candidates
    }
    classifier = LegalRelationClassificationJob(
        graph,
        opensearch,
        LLMClient(
            provider=settings.relation_classifier_provider,
            ollama_num_ctx=(
                settings.relation_classifier_context_tokens
                if settings.relation_classifier_provider == "ollama"
                else None
            ),
            ollama_think=(
                False if settings.relation_classifier_provider == "ollama" else None
            ),
        ),
    )

    results: list[dict[str, Any]] = []
    for fixture in eligible:
        fixture_id = str(fixture["fixtureId"])
        expected = set(fixture["expectedPredicates"])
        candidate = candidates_by_basis.get(str(fixture["basisEdgeId"]))
        if candidate is None:
            results.append(
                {
                    "fixtureId": fixture_id,
                    "passed": False,
                    "expectedPredicates": sorted(expected),
                    "error": "complete Article source could not be constructed",
                }
            )
            continue
        try:
            decision = classifier.classify_candidate(candidate)
            findings = {
                predicate: finding.value
                for predicate, finding in zip(
                    PREDICATES,
                    (
                        decision.predicate_findings.implements,
                        decision.predicate_findings.incorporates,
                        decision.predicate_findings.uses_definition,
                        decision.predicate_findings.exception_to,
                        decision.predicate_findings.overrides,
                    ),
                    strict=True,
                )
            }
            expected_findings = {
                predicate: (
                    "established" if predicate in expected else "not_established"
                )
                for predicate in PREDICATES
            }
            results.append(
                {
                    "fixtureId": fixture_id,
                    "passed": findings == expected_findings,
                    "expectedPredicates": sorted(expected),
                    "actualPredicates": sorted(
                        predicate
                        for predicate, finding in findings.items()
                        if finding == "established"
                    ),
                    "findings": findings,
                    "outcome": decision.outcome.value,
                }
            )
        except Exception as exc:  # noqa: BLE001 - 残りのfixtureを継続する
            results.append(
                {
                    "fixtureId": fixture_id,
                    "passed": False,
                    "expectedPredicates": sorted(expected),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE_PATH)
    parser.add_argument("--fixture-id", action="append", default=[])
    parser.add_argument(
        "--classify",
        action="store_true",
        help="構造解決に合格し正解predicateがあるペアだけをLLMで分類する",
    )
    args = parser.parse_args()
    try:
        fixtures = select_fixtures(load_fixtures(args.fixture), args.fixture_id)
    except ValueError as exc:
        parser.error(str(exc))

    graph = GraphClient()
    try:
        source_state = graph.classification_source_state()
        basis_ids = {str(item["basisEdgeId"]) for item in fixtures}
        rows = [
            row
            for row in graph.reference_candidates_for_classification(
                source_snapshot_id=str(source_state["sourceSnapshotId"])
            )
            if str(row["basis"].get("graphEdgeId") or "") in basis_ids
        ]
        rows_by_basis = {
            str(row["basis"]["graphEdgeId"]): row
            for row in rows
            if row["basis"].get("graphEdgeId")
        }
        structural = [
            structural_result(item, rows_by_basis.get(str(item["basisEdgeId"])))
            for item in fixtures
        ]
        structural_by_id = {str(item["fixtureId"]): item for item in structural}
        classification = (
            classify_eligible(
                fixtures=fixtures,
                rows_by_basis=rows_by_basis,
                structural_by_id=structural_by_id,
                graph=graph,
                source_state=source_state,
            )
            if args.classify
            else []
        )
    finally:
        graph.close()

    status_counts = Counter(
        str(item["expectedResolutionStatus"]) for item in fixtures
    )
    teacher_sources = adjudication_sources(fixtures)
    structural_correct = sum(bool(item["passed"]) for item in structural)
    classification_correct = sum(bool(item["passed"]) for item in classification)
    report = {
        "fixtureCount": len(fixtures),
        "teacherData": {
            "adjudicationSource": (
                teacher_sources[0] if len(teacher_sources) == 1 else None
            ),
            "adjudicationSources": teacher_sources,
            "resolutionStatusCounts": dict(sorted(status_counts.items())),
            "predicateLabeledCount": sum(
                item["expectedPredicates"] is not None for item in fixtures
            ),
        },
        "structuralResolution": {
            "correctCount": structural_correct,
            "results": structural,
        },
        "semanticClassification": {
            "executed": args.classify,
            "eligibleCount": len(classification),
            "correctCount": classification_correct,
            "results": classification,
        },
        "neo4jUpdated": False,
        "provider": settings.relation_classifier_provider if args.classify else None,
        "model": settings.relation_classifier_model if args.classify else None,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    all_passed = structural_correct == len(fixtures)
    if args.classify:
        all_passed = all_passed and classification_correct == len(classification)
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
