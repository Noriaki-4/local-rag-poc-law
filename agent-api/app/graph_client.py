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

    def paths_from(self, from_graph_node_id: str, edge_type: str | None = None, max_depth: int = 2) -> list[dict[str, Any]]:
        if edge_type and not EDGE_TYPE_PATTERN.match(edge_type):
            raise ValueError(f"Invalid edgeType: {edge_type}")
        rel_expr = f":{edge_type}" if edge_type else ""
        query = f"""
        MATCH path = (start {{graphNodeId: $fromGraphNodeId}})-[{rel_expr}*1..{max_depth}]->(target)
        RETURN
          [node IN nodes(path) | properties(node)] AS nodes,
          [rel IN relationships(path) | properties(rel)] AS edges
        LIMIT 20
        """
        with self.driver.session() as session:
            return [dict(record) for record in session.run(query, fromGraphNodeId=from_graph_node_id)]


def _safe_label(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", value)
    return cleaned or "Unknown"
