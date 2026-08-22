"""新5 predicate分類を実データの固定回帰fixtureで評価する。"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent-api"))

from app.config import settings
from app.graph_client import GraphClient
from app.legal_relation_classification_job import (
    LegalRelationClassificationJob,
    candidates_from_graph_and_sources,
)
from app.llm import LLMClient
from app.opensearch_client import OpenSearchClient

DEFAULT_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs/samples/eval/legal_relation_5predicate_regression_fixture.jsonl"
)


def _load_fixtures(path: Path) -> list[dict]:
    fixtures = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ids = [str(item["fixtureId"]) for item in fixtures]
    if len(ids) != len(set(ids)):
        raise ValueError("fixtureId must be unique")
    return fixtures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE_PATH)
    parser.add_argument("--fixture-id", action="append", default=[])
    args = parser.parse_args()
    fixtures = _load_fixtures(args.fixture)
    if args.fixture_id:
        selected = set(args.fixture_id)
        fixtures = [item for item in fixtures if item["fixtureId"] in selected]
        missing = selected.difference(item["fixtureId"] for item in fixtures)
        if missing:
            parser.error(f"unknown --fixture-id: {sorted(missing)}")

    graph = GraphClient()
    opensearch = OpenSearchClient()
    try:
        source = graph.classification_source_state()
        basis_ids = {str(item["basisEdgeId"]) for item in fixtures}
        rows = [
            row
            for row in graph.reference_candidates_for_classification(
                source_snapshot_id=str(source["sourceSnapshotId"])
            )
            if str(row["basis"].get("graphEdgeId") or "") in basis_ids
        ]
        rows_by_basis = {str(row["basis"]["graphEdgeId"]): row for row in rows}
        missing_basis = basis_ids.difference(rows_by_basis)
        if missing_basis:
            raise RuntimeError(f"fixture basis is missing: {sorted(missing_basis)}")
        ordered_rows = [rows_by_basis[str(item["basisEdgeId"])] for item in fixtures]
        article_ids = list(
            dict.fromkeys(
                str(row[key]["graphNodeId"])
                for row in ordered_rows
                for key in ("referenceSourceArticle", "referenceTargetArticle")
            )
        )
        sources = opensearch.get_complete_articles_by_ids(
            article_ids, user_clearance_level=3
        )
        candidates = candidates_from_graph_and_sources(
            ordered_rows,
            sources,
            source_snapshot_id=str(source["sourceSnapshotId"]),
            graph_schema_version=int(source["graphSchemaVersion"]),
            provider=settings.relation_classifier_provider,
            model=settings.relation_classifier_model,
            reviewer_model=settings.relation_classifier_reviewer_model or None,
        )
        candidates_by_basis = {
            basis_edge_id: candidate
            for candidate in candidates
            for basis_edge_id in candidate.basis_edge_ids
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
        results = []
        correct = 0
        for fixture in fixtures:
            candidate = candidates_by_basis[str(fixture["basisEdgeId"])]
            actual_endpoints = (
                candidate.reference_source.article_id,
                candidate.reference_target.article_id,
            )
            expected_endpoints = (
                str(fixture["referenceSourceArticleId"]),
                str(fixture["referenceTargetArticleId"]),
            )
            if actual_endpoints != expected_endpoints:
                raise RuntimeError(f"fixture endpoint mismatch: {fixture['fixtureId']}")
            decision = classifier.classify_candidate(candidate)
            actual = {
                key: value
                for key, value in zip(
                    (
                        "IMPLEMENTS",
                        "INCORPORATES",
                        "USES_DEFINITION",
                        "EXCEPTION_TO",
                        "OVERRIDES",
                    ),
                    (
                        decision.predicate_findings.implements.value,
                        decision.predicate_findings.incorporates.value,
                        decision.predicate_findings.uses_definition.value,
                        decision.predicate_findings.exception_to.value,
                        decision.predicate_findings.overrides.value,
                    ),
                    strict=True,
                )
            }
            passed = actual == fixture["expectedFindings"]
            correct += int(passed)
            results.append(
                {
                    "fixtureId": fixture["fixtureId"],
                    "passed": passed,
                    "expected": fixture["expectedFindings"],
                    "actual": actual,
                }
            )
    finally:
        graph.close()
    print(
        json.dumps(
            {
                "fixtureCount": len(fixtures),
                "correctCount": correct,
                "results": results,
                "neo4jUpdated": False,
                "provider": settings.relation_classifier_provider,
                "model": settings.relation_classifier_model,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if correct == len(fixtures) else 1


if __name__ == "__main__":
    raise SystemExit(main())
