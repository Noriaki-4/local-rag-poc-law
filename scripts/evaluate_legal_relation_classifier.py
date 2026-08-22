"""RelationAssertion分類を固定fixtureで評価する。

実際のNeo4j候補とOpenSearchのArticle本文を入力に使うが、分類結果は
Neo4jへ書き込まない。登録済みprovenanceも除いて毎回LLMを呼び、現在の
prompt・model・Article復元処理の組合せを測る。
"""

import argparse
import json
import sys
import traceback
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent-api"))

from app.config import settings  # noqa: E402
from app.graph_client import GraphClient  # noqa: E402
from app.legal_relation_classifier import (  # noqa: E402
    LegalRelationClassificationService,
)
from app.llm import LLMClient  # noqa: E402
from app.opensearch_client import OpenSearchClient  # noqa: E402

DEFAULT_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs/samples/eval/legal_relation_classifier_fixture.jsonl"
)
CLASSIFICATION_FIELDS = {
    "classificationVerdict",
    "classificationReason",
    "classificationDelegationFinding",
    "classificationImplementationFinding",
    "fromSupportingSpanId",
    "toSupportingSpanId",
    "fromSupportingQuote",
    "toSupportingQuote",
    "fromArticleHash",
    "toArticleHash",
    "classifierProvider",
    "classifierModel",
    "classifierReviewerModel",
    "classifierPromptVersion",
    "classifierPromptHash",
    "primaryClassifierPromptHash",
    "classifiedAt",
}


class EvaluationGraph:
    """評価対象だけを返し、更新をメモリに捕捉するGraph代理。"""

    def __init__(self, assertions: list[dict[str, Any]]) -> None:
        self.assertions = assertions
        self.records: list[dict[str, Any]] = []

    def relation_assertions_for_classification(
        self, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        rows = []
        for assertion in self.assertions[:limit]:
            # 登録済みでもskipせず、現在の条件で再評価する。
            row = {
                key: value
                for key, value in assertion.items()
                if key not in CLASSIFICATION_FIELDS
            }
            row["status"] = "unverified"
            rows.append(row)
        return rows

    def update_relation_classifications(
        self, records: list[dict[str, Any]]
    ) -> None:
        self.records.extend(records)


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
            "assertionId",
            "fromArticleId",
            "toArticleId",
            "expectedVerdict",
            "annotationBasis",
        }
        missing = sorted(required - set(row))
        if missing:
            raise ValueError(f"fixture line {line_number}: missing {missing}")
        if row["expectedVerdict"] not in {"implements", "reference_only"}:
            raise ValueError(
                f"fixture line {line_number}: unsupported expectedVerdict"
            )
        fixtures.append(row)
    ids = [str(row["fixtureId"]) for row in fixtures]
    assertion_ids = [str(row["assertionId"]) for row in fixtures]
    if len(ids) != len(set(ids)) or len(assertion_ids) != len(set(assertion_ids)):
        raise ValueError("fixtureId and assertionId must be unique")
    return fixtures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE_PATH)
    parser.add_argument(
        "--fixture-id",
        action="append",
        default=[],
        help="評価するfixtureId。複数回指定可",
    )
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    fixtures = load_fixtures(args.fixture)
    if args.fixture_id:
        requested_ids = list(dict.fromkeys(args.fixture_id))
        by_fixture_id = {str(row["fixtureId"]): row for row in fixtures}
        missing_fixture_ids = [
            fixture_id
            for fixture_id in requested_ids
            if fixture_id not in by_fixture_id
        ]
        if missing_fixture_ids:
            parser.error(f"unknown --fixture-id: {missing_fixture_ids}")
        fixtures = [by_fixture_id[fixture_id] for fixture_id in requested_ids]
    if args.offset < 0:
        parser.error("--offset must be zero or greater")
    fixtures = fixtures[args.offset :]
    if args.limit is not None:
        if args.limit < 1:
            parser.error("--limit must be at least 1")
        fixtures = fixtures[: args.limit]

    graph = GraphClient()
    try:
        all_assertions = graph.relation_assertions_for_classification()
    finally:
        graph.close()
    by_id = {str(row["assertionId"]): row for row in all_assertions}
    missing_assertion_ids = [
        row["assertionId"] for row in fixtures if row["assertionId"] not in by_id
    ]
    endpoint_mismatches = []
    selected = []
    for fixture in fixtures:
        assertion = by_id.get(fixture["assertionId"])
        if assertion is None:
            continue
        actual_endpoints = (
            assertion.get("fromArticleId"),
            assertion.get("toArticleId"),
        )
        expected_endpoints = (
            fixture["fromArticleId"],
            fixture["toArticleId"],
        )
        if actual_endpoints != expected_endpoints:
            endpoint_mismatches.append(
                {
                    "fixtureId": fixture["fixtureId"],
                    "expected": expected_endpoints,
                    "actual": actual_endpoints,
                }
            )
            continue
        selected.append(assertion)
    if missing_assertion_ids or endpoint_mismatches:
        print(
            json.dumps(
                {
                    "fixtureCount": len(fixtures),
                    "missingAssertionIds": missing_assertion_ids,
                    "endpointMismatches": endpoint_mismatches,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    evaluation_graph = EvaluationGraph(selected)
    llm = LLMClient(
        provider=settings.relation_classifier_provider,
        ollama_num_ctx=(
            settings.relation_classifier_context_tokens
            if settings.relation_classifier_provider == "ollama"
            else None
        ),
        ollama_think=(
            False if settings.relation_classifier_provider == "ollama" else None
        ),
    )
    runtime_error = None
    try:
        report = LegalRelationClassificationService(
            evaluation_graph,
            OpenSearchClient(),
            llm,
        ).run(dry_run=False)
    except Exception as exc:  # noqa: BLE001 - 評価は部分結果も出力する
        runtime_error = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(limit=6),
        }
        report = {
            "assertionCount": len(fixtures),
            "classifiedCount": len(evaluation_graph.records),
            "partial": True,
        }
    records_by_id = {
        str(record["assertionId"]): record for record in evaluation_graph.records
    }
    expected_counts: Counter[str] = Counter()
    predicted_counts: Counter[str] = Counter()
    confusion: Counter[str] = Counter()
    tag_totals: Counter[str] = Counter()
    tag_correct: Counter[str] = Counter()
    failures = []
    correct = 0
    for fixture in fixtures:
        expected = str(fixture["expectedVerdict"])
        record = records_by_id.get(str(fixture["assertionId"]))
        predicted = (
            str(record["classificationVerdict"]) if record is not None else "missing"
        )
        expected_counts[expected] += 1
        predicted_counts[predicted] += 1
        confusion[f"{expected}->{predicted}"] += 1
        coverage_tags = [str(tag) for tag in fixture.get("coverageTags") or []]
        for tag in coverage_tags:
            tag_totals[tag] += 1
        if predicted == expected:
            correct += 1
            for tag in coverage_tags:
                tag_correct[tag] += 1
            continue
        failures.append(
            {
                "fixtureId": fixture["fixtureId"],
                "assertionId": fixture["assertionId"],
                "expected": expected,
                "predicted": predicted,
                "reviewerUsed": bool(
                    record and record.get("classifierReviewerModel")
                ),
                "reason": (
                    record.get("classificationReason") if record else "missing result"
                ),
                "annotationBasis": fixture["annotationBasis"],
            }
        )
    output = {
        "fixturePath": str(args.fixture),
        "fixtureCount": len(fixtures),
        "correctCount": correct,
        "accuracy": correct / len(fixtures) if fixtures else None,
        "expectedCounts": dict(expected_counts),
        "predictedCounts": dict(predicted_counts),
        "confusion": dict(confusion),
        "coverageByTag": {
            tag: {
                "correctCount": tag_correct[tag],
                "fixtureCount": total,
                "accuracy": tag_correct[tag] / total,
            }
            for tag, total in sorted(tag_totals.items())
        },
        "failures": failures,
        "classificationRun": report,
        "captureOnly": True,
        "neo4jUpdated": False,
        "runtimeError": runtime_error,
        "provider": settings.relation_classifier_provider,
        "model": settings.relation_classifier_model,
        "reviewerModel": settings.relation_classifier_reviewer_model,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 1 if runtime_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
