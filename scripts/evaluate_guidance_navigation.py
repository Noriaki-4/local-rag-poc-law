"""投入済みガイドの検索、EXPLAINS、Article全文取得を固定fixtureで検査する。

法的意味をプログラムで判断する評価ではない。データセット設計で分離された
ガイド本文と法令本文の間を、明示EXPLAINSだけでナビゲーションできるかを検査する。
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent-api"))

from app.graph_client import GraphClient  # noqa: E402
from app.legal_relation_classifier import article_texts_from_sources  # noqa: E402
from app.opensearch_client import (  # noqa: E402
    OpenSearchClient,
    RequirementSearchSpec,
)

DEFAULT_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs/samples/eval/guidance_navigation_fixture.jsonl"
)


def load_fixtures(path: Path) -> list[dict[str, Any]]:
    fixtures = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw_line.strip():
            continue
        row = json.loads(raw_line)
        required = {
            "fixtureId",
            "query",
            "expectedGuideDocumentId",
            "expectedExplainedArticleIds",
            "annotationBasis",
        }
        missing = sorted(required - set(row))
        if missing:
            raise ValueError(f"fixture line {line_number}: missing {missing}")
        fixtures.append(row)
    fixture_ids = [str(row["fixtureId"]) for row in fixtures]
    if len(fixture_ids) != len(set(fixture_ids)):
        raise ValueError("fixtureId must be unique")
    return fixtures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE_PATH)
    parser.add_argument("--top-k", type=int, default=10)
    args = parser.parse_args()
    if args.top_k < 1:
        parser.error("--top-k must be at least 1")
    fixtures = load_fixtures(args.fixture)

    opensearch = OpenSearchClient()
    specs = [
        RequirementSearchSpec(
            requirement_id=str(row["fixtureId"]),
            query=str(row["query"]),
            top_k=args.top_k,
            doc_type="guideline",
        )
        for row in fixtures
    ]
    search_results = opensearch.search_requirement_specs(
        specs,
        user_clearance_level=3,
        timeout_sec=60,
    )

    graph = GraphClient()
    rows = []
    try:
        for fixture in fixtures:
            fixture_id = str(fixture["fixtureId"])
            document_id = str(fixture["expectedGuideDocumentId"])
            hits = search_results.get(fixture_id, [])
            retrieved_document_ids = list(
                dict.fromkeys(str(hit.get("documentId") or "") for hit in hits)
            )
            paths = graph.paths_from_many(
                [document_id],
                edge_types=["EXPLAINS"],
                max_depth=1,
                limit=100,
                user_clearance_level=3,
                timeout_sec=30,
            )
            actual_article_ids = sorted(
                {
                    str((path.get("nodes") or [])[-1].get("graphNodeId") or "")
                    for path in paths
                    if path.get("nodes")
                }
                - {""}
            )
            expected_article_ids = sorted(
                str(value)
                for value in fixture["expectedExplainedArticleIds"]
            )
            sources = opensearch.get_complete_articles_by_ids(
                expected_article_ids,
                user_clearance_level=3,
            )
            fetched_articles = article_texts_from_sources(
                expected_article_ids,
                sources,
            )
            search_hit = document_id in retrieved_document_ids
            graph_exact = actual_article_ids == expected_article_ids
            complete_text = set(fetched_articles) == set(expected_article_ids)
            rows.append(
                {
                    "fixtureId": fixture_id,
                    "searchHit": search_hit,
                    "retrievedGuideDocumentIds": retrieved_document_ids,
                    "graphExact": graph_exact,
                    "expectedExplainedArticleIds": expected_article_ids,
                    "actualExplainedArticleIds": actual_article_ids,
                    "completeArticleText": complete_text,
                    "passed": search_hit and graph_exact and complete_text,
                    "annotationBasis": fixture["annotationBasis"],
                }
            )
    finally:
        graph.close()

    passed = sum(1 for row in rows if row["passed"])
    output = {
        "fixturePath": str(args.fixture),
        "fixtureCount": len(rows),
        "passedCount": passed,
        "allPassed": passed == len(rows),
        "checks": rows,
        "neo4jUpdated": False,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["allPassed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
