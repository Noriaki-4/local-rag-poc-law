"""RelationAssertionを法令本文でオフライン分類する。

seedの同期経路には接続しない。既定は分類結果を保存せず、`--apply`を指定した場合だけ
RelationAssertionノードの派生プロパティを更新する。正式なArticle間エッジは作らない。
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent-api"))

from app.graph_client import GraphClient  # noqa: E402
from app.legal_relation_classifier import (  # noqa: E402
    LegalRelationClassificationService,
)
from app.llm import LLMClient  # noqa: E402
from app.opensearch_client import OpenSearchClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="分類結果をNeo4jのRelationAssertionへ保存する",
    )
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")

    graph = GraphClient()
    try:
        service = LegalRelationClassificationService(
            graph,
            OpenSearchClient(),
            LLMClient(),
        )
        report = service.run(limit=args.limit, dry_run=not args.apply)
    finally:
        graph.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not args.apply:
        print(
            "dry-run: Neo4jは更新していません。"
            "保存する場合は--applyを指定してください。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
