import re
from typing import Any

from neo4j import GraphDatabase

from .config import settings

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
    ) -> list[dict[str, Any]]:
        return self.paths_from_many(
            [from_graph_node_id],
            edge_type=edge_type,
            max_depth=max_depth,
            user_clearance_level=user_clearance_level,
        )

    def paths_from_many(
        self,
        from_graph_node_ids: list[str],
        edge_type: str | None = None,
        max_depth: int = 2,
        limit: int = 20,
        user_clearance_level: int = 2,
    ) -> list[dict[str, Any]]:
        if not from_graph_node_ids:
            return []
        if edge_type and not EDGE_TYPE_PATTERN.match(edge_type):
            raise ValueError(f"Invalid edgeType: {edge_type}")
        if max_depth < 1 or max_depth > 3:
            raise ValueError("max_depth must be between 1 and 3")
        rel_expr = f":{edge_type}" if edge_type else ""
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
        with self.driver.session() as session:
            return [
                dict(record)
                for record in session.run(
                    query,
                    fromGraphNodeIds=list(dict.fromkeys(from_graph_node_ids)),
                    limit=max(1, min(limit, 100)),
                    userClearanceLevel=user_clearance_level,
                )
            ]


def _safe_label(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", value)
    return cleaned or "Unknown"
