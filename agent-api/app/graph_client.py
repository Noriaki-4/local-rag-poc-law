import re
from typing import Any

from neo4j import GraphDatabase

from .config import settings
from .legal_ontology import (
    EDGE_REGISTRY,
    RELATION_STATUS_LLM_IMPLEMENTS,
    RELATION_STATUS_LLM_UNCERTAIN,
    RELATION_STATUS_UNVERIFIED,
    expandable_edge_types,
)

EDGE_TYPE_PATTERN = re.compile(r"^[A-Z_]+$")
SEED_BATCH_SIZE = 500


class GraphClient:
    def __init__(self) -> None:
        self.driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))

    def health(self) -> bool:
        try:
            with self.driver.session() as session:
                session.run("RETURN 1").consume()
            return True
        except Exception:
            return False

    def close(self) -> None:
        self.driver.close()

    def clear(self) -> None:
        with self.driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n").consume()

    def ensure_legal_graph_schema(self) -> None:
        """新法令GraphのConstraintとindexを冪等に作成する。"""

        from .domains.legal.graph_schema import NEO4J_SCHEMA_STATEMENTS

        with self.driver.session() as session:
            for statement in NEO4J_SCHEMA_STATEMENTS:
                session.run(statement).consume()

    def seed_nodes(self, nodes: list[dict[str, Any]]) -> None:
        nodes_by_type: dict[str, list[dict[str, Any]]] = {}
        for node in nodes:
            node_type = str(node.get("nodeType") or "Unknown")
            _safe_label(node_type)
            nodes_by_type.setdefault(node_type, []).append(node)

        with self.driver.session() as session:
            session.run(
                "CREATE INDEX graph_node_id IF NOT EXISTS "
                "FOR (n:GraphNode) ON (n.graphNodeId)"
            ).consume()
            for node_type, typed_nodes in nodes_by_type.items():
                label_expr = ":".join(
                    (_safe_label("GraphNode"), _safe_label(node_type))
                )
                for batch in _chunks(typed_nodes, SEED_BATCH_SIZE):
                    session.run(
                        f"""
                        UNWIND $rows AS row
                        MERGE (n:{label_expr} {{graphNodeId: row.graphNodeId}})
                        SET n += row.props
                        """,
                        rows=[
                            {
                                "graphNodeId": node["graphNodeId"],
                                "props": node,
                            }
                            for node in batch
                        ],
                    ).consume()

    def seed_edges(self, edges: list[dict[str, Any]]) -> None:
        edges_by_type: dict[str, list[dict[str, Any]]] = {}
        for edge in edges:
            edge_type = str(edge["edgeType"])
            if not EDGE_TYPE_PATTERN.fullmatch(edge_type):
                raise ValueError(f"Invalid edgeType: {edge_type}")
            edges_by_type.setdefault(edge_type, []).append(edge)

        with self.driver.session() as session:
            for edge_type, typed_edges in edges_by_type.items():
                for batch in _chunks(typed_edges, SEED_BATCH_SIZE):
                    session.run(
                        f"""
                        UNWIND $rows AS row
                        MATCH (from:GraphNode {{graphNodeId: row.fromGraphNodeId}})
                        MATCH (to:GraphNode {{graphNodeId: row.toGraphNodeId}})
                        MERGE (from)-[r:{edge_type} {{graphEdgeId: row.graphEdgeId}}]->(to)
                        SET r += row.props
                        """,
                        rows=[
                            {
                                "fromGraphNodeId": edge["fromGraphNodeId"],
                                "toGraphNodeId": edge["toGraphNodeId"],
                                "graphEdgeId": edge["graphEdgeId"],
                                "props": edge,
                            }
                            for edge in batch
                        ],
                    ).consume()

    def paths_from(
        self,
        from_graph_node_id: str,
        edge_type: str | None = None,
        max_depth: int = 2,
        user_clearance_level: int = 2,
        timeout_sec: float | None = None,
    ) -> list[dict[str, Any]]:
        return self.paths_from_many(
            [from_graph_node_id],
            edge_type=edge_type,
            max_depth=max_depth,
            user_clearance_level=user_clearance_level,
            timeout_sec=timeout_sec,
        )

    def paths_from_many(
        self,
        from_graph_node_ids: list[str],
        edge_type: str | None = None,
        max_depth: int = 2,
        limit: int = 20,
        user_clearance_level: int = 2,
        edge_types: list[str] | tuple[str, ...] | None = None,
        timeout_sec: float | None = None,
    ) -> list[dict[str, Any]]:
        """複数の起点から、許可されたedge種別をまとめて1回で辿る。

        `edge_types`はedge registryのallowlistを通し、Cypherへ文字列を直接埋め込まない。
        `timeout_sec`はNeo4j driverのtransaction timeoutへ変換し、request全体の残時間を
        超えてGraph探索が走らないようにする(計画書 §8.5, §11.2)。
        """
        if not from_graph_node_ids:
            return []
        selected = _allowed_edge_types(edge_types, edge_type)
        if max_depth < 1 or max_depth > 3:
            raise ValueError("max_depth must be between 1 and 3")
        if timeout_sec is not None and timeout_sec <= 0:
            return []
        rel_expr = f":{'|'.join(selected)}" if selected else ""
        query = f"""
        MATCH (start)
        WHERE start.graphNodeId IN $fromGraphNodeIds
        MATCH path = (start)-[{rel_expr}*1..{max_depth}]->(target)
        WHERE all(node IN nodes(path) WHERE coalesce(node.clearanceLevel, 3) <= $userClearanceLevel)
        RETURN
          [node IN nodes(path) | properties(node)] AS nodes,
          [rel IN relationships(path) | properties(rel)] AS edges
        LIMIT $limit
        """
        parameters = {
            "fromGraphNodeIds": list(dict.fromkeys(from_graph_node_ids)),
            "limit": max(1, min(limit, 100)),
            "userClearanceLevel": user_clearance_level,
        }
        with self.driver.session() as session:
            if timeout_sec is None:
                return [dict(record) for record in session.run(query, **parameters)]
            # Neo4j driverのtransaction timeoutへ変換する。単なるCypher parameterとして
            # 渡してもサーバ側では打ち切られない(§8.5)。
            with session.begin_transaction(timeout=float(timeout_sec)) as transaction:
                return [dict(record) for record in transaction.run(query, **parameters)]

    def relation_assertions_from(
        self,
        from_article_ids: list[str],
        limit: int = 20,
        user_clearance_level: int = 3,
        timeout_sec: float | None = None,
    ) -> list[dict[str, Any]]:
        """ガイド由来の未確認関係(RelationAssertion)を候補拡張のためだけに取得する。

        確定関係ではないため、根拠充足・mustInclude・法令関係図の確定線には使わない(§6.1)。
        """
        if not from_article_ids:
            return []
        if timeout_sec is not None and timeout_sec <= 0:
            return []
        query = """
        MATCH (assertion:RelationAssertion)
        MATCH (from:Article {graphNodeId: assertion.fromArticleId})
        MATCH (to:Article {graphNodeId: assertion.toArticleId})
        WHERE assertion.fromArticleId IN $fromArticleIds
        AND assertion.status IN $visibleStatuses
        AND coalesce(assertion.clearanceLevel, 3) <= $userClearanceLevel
        AND coalesce(from.clearanceLevel, 3) <= $userClearanceLevel
        AND coalesce(to.clearanceLevel, 3) <= $userClearanceLevel
        RETURN properties(assertion) AS assertion
        LIMIT $limit
        """
        with self.driver.session() as session:
            parameters = {
                "fromArticleIds": list(dict.fromkeys(from_article_ids)),
                "limit": max(1, min(limit, 100)),
                "userClearanceLevel": user_clearance_level,
                "visibleStatuses": [
                    RELATION_STATUS_UNVERIFIED,
                    RELATION_STATUS_LLM_UNCERTAIN,
                    RELATION_STATUS_LLM_IMPLEMENTS,
                ],
            }
            if timeout_sec is None:
                records = session.run(query, **parameters)
                return [dict(record["assertion"]) for record in records]
            with session.begin_transaction(timeout=float(timeout_sec)) as transaction:
                return [
                    dict(record["assertion"])
                    for record in transaction.run(query, **parameters)
                ]

    def article_relations_touching(
        self,
        article_ids: list[str],
        *,
        edge_types: list[str] | None = None,
        user_clearance_level: int = 3,
        limit: int = 50,
        timeout_sec: float | None = None,
    ) -> list[dict[str, Any]]:
        """指定Articleの入出力に接続する正式Graph関係を候補として取得する。

        下位法令から親法令へのREFERENCESのように、起点Articleから見て
        入力側にある関係も検索ナビゲーションに必要なため双方向を返す。
        関係の質問への関連性は呼び出し先LLMが本文で判断する。
        """
        if not article_ids:
            return []
        if timeout_sec is not None and timeout_sec <= 0:
            return []
        allowed_types = [
            edge_type
            for edge_type in dict.fromkeys(edge_types or expandable_edge_types())
            if edge_type in expandable_edge_types()
        ]
        if edge_types and len(allowed_types) != len(set(edge_types)):
            raise ValueError("unregistered or unimplemented graph edge type")
        query = """
        MATCH (from:GraphNode)-[relation]->(to:GraphNode)
        WHERE (
          any(articleId IN $articleIds WHERE
            from.graphNodeId = articleId
            OR from.graphNodeId STARTS WITH articleId + '-'
          )
          OR any(articleId IN $articleIds WHERE
            to.graphNodeId = articleId
            OR to.graphNodeId STARTS WITH articleId + '-'
          )
        )
        AND type(relation) IN $edgeTypes
        AND from.nodeType IN ['Article', 'Paragraph', 'Item']
        AND to.nodeType IN ['Article', 'Paragraph', 'Item']
        AND coalesce(from.clearanceLevel, 3) <= $userClearanceLevel
        AND coalesce(to.clearanceLevel, 3) <= $userClearanceLevel
        OPTIONAL MATCH (fromDocument:GraphNode {graphNodeId: from.documentId})
        OPTIONAL MATCH (toDocument:GraphNode {graphNodeId: to.documentId})
        RETURN relation {
          .*,
          edgeType: type(relation),
          fromContentUnitId: from.graphNodeId,
          fromNodeType: from.nodeType,
          fromDocumentId: from.documentId,
          fromTitle: coalesce(from.title, fromDocument.title),
          fromHeading: from.heading,
          toContentUnitId: to.graphNodeId,
          toNodeType: to.nodeType,
          toDocumentId: to.documentId,
          toTitle: coalesce(to.title, toDocument.title),
          toHeading: to.heading
        } AS relation
        ORDER BY relation.edgeType, relation.fromContentUnitId, relation.toContentUnitId
        LIMIT $limit
        """
        parameters = {
            "articleIds": list(dict.fromkeys(article_ids)),
            "edgeTypes": allowed_types,
            "userClearanceLevel": user_clearance_level,
            "limit": max(1, min(limit, 500)),
        }
        with self.driver.session() as session:
            if timeout_sec is None:
                return [
                    _with_relation_article_ids(dict(record["relation"]))
                    for record in session.run(query, **parameters)
                ]
            with session.begin_transaction(timeout=float(timeout_sec)) as transaction:
                return [
                    _with_relation_article_ids(dict(record["relation"]))
                    for record in transaction.run(query, **parameters)
                ]

    def relation_assertions_touching(
        self,
        article_ids: list[str],
        *,
        suggested_types: list[str] | None = None,
        user_clearance_level: int = 3,
        limit: int = 50,
        timeout_sec: float | None = None,
    ) -> list[dict[str, Any]]:
        """指定Articleを一方の端点に持つ未確認関係候補を取得する。

        候補は方向を確定関係として利用せず、LLMが親子双方の本文を取得するための
        ナビゲーション情報としてだけ返す。
        """
        if not article_ids:
            return []
        if timeout_sec is not None and timeout_sec <= 0:
            return []
        allowed_types = [
            edge_type
            for edge_type in dict.fromkeys(suggested_types or [])
            if edge_type in expandable_edge_types()
        ]
        if suggested_types and len(allowed_types) != len(set(suggested_types)):
            raise ValueError("unregistered or unimplemented suggested relation type")
        query = """
        MATCH (assertion:RelationAssertion)
        MATCH (from:Article {graphNodeId: assertion.fromArticleId})
        MATCH (to:Article {graphNodeId: assertion.toArticleId})
        WHERE (
          assertion.fromArticleId IN $articleIds
          OR assertion.toArticleId IN $articleIds
        )
        AND assertion.status IN $visibleStatuses
        AND coalesce(assertion.clearanceLevel, 3) <= $userClearanceLevel
        AND coalesce(from.clearanceLevel, 3) <= $userClearanceLevel
        AND coalesce(to.clearanceLevel, 3) <= $userClearanceLevel
        AND (
          size($suggestedTypes) = 0
          OR assertion.suggestedType IN $suggestedTypes
        )
        OPTIONAL MATCH (fromDocument:GraphNode {graphNodeId: from.documentId})
        OPTIONAL MATCH (toDocument:GraphNode {graphNodeId: to.documentId})
        RETURN assertion {
          .*,
          fromDocumentId: from.documentId,
          fromTitle: coalesce(from.title, fromDocument.title),
          fromHeading: from.heading,
          toDocumentId: to.documentId,
          toTitle: coalesce(to.title, toDocument.title),
          toHeading: to.heading
        } AS assertion
        ORDER BY assertion.assertionId
        LIMIT $limit
        """
        parameters = {
            "articleIds": list(dict.fromkeys(article_ids)),
            "suggestedTypes": allowed_types,
            "visibleStatuses": [
                RELATION_STATUS_UNVERIFIED,
                RELATION_STATUS_LLM_UNCERTAIN,
                RELATION_STATUS_LLM_IMPLEMENTS,
            ],
            "userClearanceLevel": user_clearance_level,
            "limit": max(1, min(limit, 500)),
        }
        with self.driver.session() as session:
            if timeout_sec is None:
                return [
                    dict(record["assertion"])
                    for record in session.run(query, **parameters)
                ]
            with session.begin_transaction(timeout=float(timeout_sec)) as transaction:
                return [
                    dict(record["assertion"])
                    for record in transaction.run(query, **parameters)
                ]

    def relation_assertions_for_classification(
        self,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """オフライン分類対象と既存の分類provenanceを取得する。"""
        query = """
        MATCH (assertion:RelationAssertion)
        MATCH (from:Article {graphNodeId: assertion.fromArticleId})
        MATCH (to:Article {graphNodeId: assertion.toArticleId})
        RETURN properties(assertion) AS assertion
        ORDER BY assertion.assertionId
        """
        parameters: dict[str, Any] = {}
        if limit is not None:
            query += "\nLIMIT $limit"
            parameters["limit"] = max(1, limit)
        with self.driver.session() as session:
            return [
                dict(record["assertion"])
                for record in session.run(query, **parameters)
            ]

    def update_relation_classifications(
        self, records: list[dict[str, Any]]
    ) -> None:
        """分類派生データだけを更新し、正式Article間エッジは作らない。"""
        if not records:
            return
        query = """
        UNWIND $records AS item
        MATCH (assertion:RelationAssertion {assertionId: item.assertionId})
        SET assertion += item
        """
        with self.driver.session() as session:
            session.run(query, records=records).consume()

    def edge_inventory(self) -> dict[str, int]:
        """seed済みエッジ種別と件数。ドキュメント・コードとの一致検査に使う(§6.3-13)。"""
        query = "MATCH ()-[r]->() RETURN type(r) AS edgeType, count(r) AS count"
        with self.driver.session() as session:
            return {
                str(record["edgeType"]): int(record["count"])
                for record in session.run(query)
            }

    def node_inventory(self) -> dict[str, int]:
        query = "MATCH (n) RETURN coalesce(n.nodeType, 'Unknown') AS nodeType, count(n) AS count"
        with self.driver.session() as session:
            return {
                str(record["nodeType"]): int(record["count"])
                for record in session.run(query)
            }

    def authority_type_inventory(self) -> dict[str, int]:
        query = """
        MATCH (n)
        WHERE n.nodeType IN ['Document', 'Article']
        RETURN coalesce(n.authorityType, 'missing') AS authorityType, count(n) AS count
        """
        with self.driver.session() as session:
            return {
                str(record["authorityType"]): int(record["count"])
                for record in session.run(query)
            }


def _with_relation_article_ids(relation: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(relation)
    normalized["fromArticleId"] = _article_id_from_content_unit(
        normalized.get("fromContentUnitId")
    )
    normalized["toArticleId"] = _article_id_from_content_unit(
        normalized.get("toContentUnitId")
    )
    return normalized


def _article_id_from_content_unit(value: Any) -> str:
    content_unit_id = str(value or "")
    return content_unit_id.split("-paragraph-", 1)[0].split("-item-", 1)[0]


def _allowed_edge_types(
    edge_types: list[str] | tuple[str, ...] | None,
    edge_type: str | None,
) -> tuple[str, ...]:
    """edge registryに登録された実装済みedge種別だけをCypherへ渡す。"""
    requested = list(edge_types or ([edge_type] if edge_type else []))
    selected: list[str] = []
    for value in requested:
        name = str(value)
        if not EDGE_TYPE_PATTERN.match(name):
            raise ValueError(f"Invalid edgeType: {name}")
        spec = EDGE_REGISTRY.get(name)
        if spec is None or not spec.implemented:
            raise ValueError(f"Unregistered or unimplemented edgeType: {name}")
        if name not in selected:
            selected.append(name)
    return tuple(selected)


def _safe_label(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", value)
    return cleaned or "Unknown"


def _chunks(
    items: list[dict[str, Any]], size: int
) -> list[list[dict[str, Any]]]:
    return [items[index : index + size] for index in range(0, len(items), size)]
