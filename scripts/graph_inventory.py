"""稼働中のNeo4j・agent-apiに対するPhase 0のGraph棚卸し。

layered_legal_evidence_retrieval_plan.md Phase 0(§15)の次の項目を機械的に出す。

- 現在seedされるnode/edge種別を件数付きで出力する
- 現行エッジが5種であり、未実装エッジ(DEFINES/USES_TERM/EXCEPTION_TO)が
  Graphに存在しないことをコードとNeo4jの両方で確認する
- registryの実装済みエッジ種別とNeo4jの実データを突き合わせる
- Law/ArticleのauthorityType分布と、未設定・未判別の件数を出す
- 採用中の時間profileと設定警告を出す

使い方(docker compose upでスタックが起動している状態で実行する):

    uv run --with neo4j --with requests python scripts/graph_inventory.py

環境変数: NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD, AGENT_API_URL

このスクリプトは読み取りのみで、Graphを変更しない。終了コードは、registryとNeo4jの
エッジ種別が一致し、authorityType未設定が0件の場合だけ0。
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent-api"))

from app.graph_audit import compare_edge_inventory, missing_edge_types  # noqa: E402
from app.graph_client import GraphClient  # noqa: E402
from app.legal_ontology import (  # noqa: E402
    EDGE_REGISTRY,
    GRAPH_SCHEMA_VERSION,
    SEEDED_EDGE_TYPES,
)

API_URL = os.getenv("AGENT_API_URL", "http://localhost:8000").rstrip("/")


def main() -> int:
    graph = GraphClient()
    try:
        if not graph.health():
            print("Neo4j へ接続できません。docker compose up の状態を確認してください。")
            return 2
        edges = graph.edge_inventory()
        nodes = graph.node_inventory()
        authorities = graph.authority_type_inventory()
    finally:
        graph.close()

    unimplemented = tuple(
        name for name, spec in EDGE_REGISTRY.items() if not spec.implemented
    )
    report = {
        "graphSchemaVersion": GRAPH_SCHEMA_VERSION,
        "registryImplementedEdgeTypes": list(SEEDED_EDGE_TYPES),
        "registryUnimplementedEdgeTypes": list(unimplemented),
        "neo4jEdgeTypeCounts": edges,
        "neo4jNodeTypeCounts": nodes,
        "authorityTypeCounts": authorities,
        "unimplementedEdgeTypesFoundInGraph": [
            name for name in unimplemented if name in edges
        ],
        "edgeInventoryViolations": compare_edge_inventory(edges),
        # registryでは実装済みだが、このコーパスでは0件だった種別(違反ではない)。
        "edgeTypesWithoutInstances": missing_edge_types(edges),
        "health": _health(),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))

    ok = (
        not report["edgeInventoryViolations"]
        and not report["unimplementedEdgeTypesFoundInGraph"]
        and authorities.get("missing", 0) == 0
    )
    if not ok:
        print(
            "\n[graph-inventory] 不一致があります。"
            "エッジ種別の実装状況・再シードの要否・authorityTypeのregistry入力を確認してください。"
        )
    if report["edgeTypesWithoutInstances"]:
        print(
            "[graph-inventory] registry実装済みだがこのコーパスでは0件の種別: "
            f"{', '.join(report['edgeTypesWithoutInstances'])}。"
            "抽出条件を満たす資料が無いだけかを確認する(違反ではない)。"
        )
    if authorities.get("ordinance_unspecified"):
        print(
            "[graph-inventory] ordinance_unspecified が "
            f"{authorities['ordinance_unspecified']} 件あります。"
            "省令・内閣府令の判別は law_registry.json へ人手で明示してください(§5.2)。"
        )
    return 0 if ok else 1


def _health() -> dict:
    try:
        with urllib.request.urlopen(f"{API_URL}/health", timeout=10) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - 参考情報なので失敗しても続行する
        return {"error": str(exc)}
    return {
        "timeBudget": payload.get("timeBudget"),
        "layeredLegalRetrieval": payload.get("layeredLegalRetrieval"),
    }


if __name__ == "__main__":
    raise SystemExit(main())
