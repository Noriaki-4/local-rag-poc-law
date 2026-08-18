"""Graph schema v9のREFERENCESを5種のRelationAssertionへ非同期分類する。"""

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent-api"))

from app.config import settings  # noqa: E402
from app.graph_client import GraphClient  # noqa: E402
from app.legal_relation_classification_job import (  # noqa: E402
    LegalRelationClassificationJob,
)
from app.llm import LLMClient  # noqa: E402
from app.opensearch_client import OpenSearchClient  # noqa: E402


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--run-id",
        default=None,
        help="中断したbuilding Runを再開するときのclassificationRunId",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="LLM分類、checkpoint保存、監査を実行しbuilding Runへ保存する",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="全scopeの品質確認後に監査済みRunを公開する",
    )
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.publish and not args.apply:
        parser.error("--publish requires --apply")

    graph = GraphClient()
    try:
        if args.apply:
            graph.ensure_legal_graph_schema()
        job = LegalRelationClassificationJob(
            graph,
            OpenSearchClient(),
            LLMClient(
                provider=settings.relation_classifier_provider,
                ollama_num_ctx=(
                    settings.relation_classifier_context_tokens
                    if settings.relation_classifier_provider == "ollama"
                    else None
                ),
                ollama_think=(
                    False
                    if settings.relation_classifier_provider == "ollama"
                    else None
                ),
            ),
        )
        report = job.run(
            limit=args.limit,
            run_id=args.run_id,
            apply=args.apply,
            publish=args.publish,
        )
    finally:
        graph.close()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if not args.apply:
        print(
            "dry-run: LLMとNeo4j更新は実行していません。"
            "分類する場合は--applyを指定してください。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
