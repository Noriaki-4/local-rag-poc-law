import re
from typing import Any

from neo4j import GraphDatabase

from .config import settings
from .legal_ontology import EDGE_REGISTRY

EDGE_TYPE_PATTERN = re.compile(r"^[A-Z_]+$")


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

    def seed_nodes(self, nodes: list[dict[str, Any]]) -> None:
        with self.driver.session() as session:
            for node in nodes:
                labels = ["GraphNode", node.get("nodeType", "Unknown")]
                label_expr = ":".join(_safe_label(label) for label in labels)
                session.run(
                    f"MERGE (n:{label_expr} {{graphNodeId: $graphNodeId}}) SET n += $props",
                    graphNodeId=node["graphNodeId"],
                    props=node,
                ).consume()

    def seed_edges(self, edges: list[dict[str, Any]]) -> None:
        with self.driver.session() as session:
            for edge in edges:
                edge_type = edge["edgeType"]
                if not EDGE_TYPE_PATTERN.match(edge_type):
                    raise ValueError(f"Invalid edgeType: {edge_type}")
                session.run(
                    f"""
                    MATCH (from {{graphNodeId: $fromGraphNodeId}})
                    MATCH (to {{graphNodeId: $toGraphNodeId}})
                    MERGE (from)-[r:{edge_type} {{graphEdgeId: $graphEdgeId}}]->(to)
                    SET r += $props
                    """,
                    fromGraphNodeId=edge["fromGraphNodeId"],
                    toGraphNodeId=edge["toGraphNodeId"],
                    graphEdgeId=edge["graphEdgeId"],
                    props=edge,
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
        WHERE assertion.fromArticleId IN $fromArticleIds
        RETURN properties(assertion) AS assertion
        LIMIT $limit
        """
        with self.driver.session() as session:
            parameters = {
                "fromArticleIds": list(dict.fromkeys(from_article_ids)),
                "limit": max(1, min(limit, 100)),
            }
            if timeout_sec is None:
                records = session.run(query, **parameters)
                return [dict(record["assertion"]) for record in records]
            with session.begin_transaction(timeout=float(timeout_sec)) as transaction:
                return [
                    dict(record["assertion"])
                    for record in transaction.run(query, **parameters)
                ]

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
