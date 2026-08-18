import re
from datetime import datetime
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

    def classification_source_state(self) -> dict[str, Any]:
        """決定的seedのArticleが共有するsnapshotとschemaを返す。"""

        query = """
        MATCH (article:Article)
        WITH collect(DISTINCT article.sourceSnapshotId) AS snapshots,
             collect(DISTINCT article.graphSchemaVersion) AS schemaVersions,
             count(article) AS articleCount
        OPTIONAL MATCH ()-[reference:REFERENCES]->()
        RETURN snapshots, schemaVersions, articleCount,
               count(reference) AS referenceCount,
               collect(DISTINCT reference.sourceSnapshotId) AS referenceSnapshots,
               collect(DISTINCT reference.graphSchemaVersion) AS referenceSchemaVersions
        """
        with self.driver.session() as session:
            record = session.run(query).single()
        if record is None:
            raise RuntimeError("classification source graph is unavailable")
        snapshots = [str(value) for value in record["snapshots"] if value]
        schema_versions = [int(value) for value in record["schemaVersions"] if value is not None]
        if len(snapshots) != 1 or len(schema_versions) != 1:
            raise RuntimeError(
                "classification source graph must contain exactly one snapshot and schema version"
            )
        reference_snapshots = [
            str(value) for value in record["referenceSnapshots"] if value
        ]
        reference_schema_versions = [
            int(value)
            for value in record["referenceSchemaVersions"]
            if value is not None
        ]
        if reference_snapshots and reference_snapshots != snapshots:
            raise RuntimeError("REFERENCES snapshot does not match Article snapshot")
        if reference_schema_versions and reference_schema_versions != schema_versions:
            raise RuntimeError("REFERENCES schema does not match Article schema")
        return {
            "sourceSnapshotId": snapshots[0],
            "graphSchemaVersion": schema_versions[0],
            "articleCount": int(record["articleCount"]),
            "referenceCount": int(record["referenceCount"]),
        }

    def reference_candidates_for_classification(
        self,
        *,
        source_snapshot_id: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """REFERENCESの物理方向と両端Articleを、意味判断せず候補化する。"""

        query = """
        MATCH (sourceUnit:GraphNode)-[basis:REFERENCES]->(targetUnit:GraphNode)
        WHERE basis.sourceSnapshotId = $sourceSnapshotId
        OPTIONAL MATCH (sourceAncestor:Article)-[:HAS_CONTENT_UNIT*1..2]->(sourceUnit)
        WITH sourceUnit, targetUnit, basis,
             CASE WHEN sourceUnit.nodeType = 'Article'
                  THEN sourceUnit ELSE sourceAncestor END AS sourceArticle
        OPTIONAL MATCH (targetAncestor:Article)-[:HAS_CONTENT_UNIT*1..2]->(targetUnit)
        WITH sourceUnit, targetUnit, basis, sourceArticle,
             CASE WHEN targetUnit.nodeType = 'Article'
                  THEN targetUnit ELSE targetAncestor END AS targetArticle
        WHERE sourceArticle IS NOT NULL
          AND targetArticle IS NOT NULL
          AND sourceArticle.graphNodeId <> targetArticle.graphNodeId
        RETURN DISTINCT
          properties(basis) AS basis,
          properties(sourceArticle) AS referenceSourceArticle,
          properties(targetArticle) AS referenceTargetArticle
        ORDER BY basis.graphEdgeId
        """
        parameters: dict[str, Any] = {"sourceSnapshotId": source_snapshot_id}
        if limit is not None:
            query += "\nLIMIT $limit"
            parameters["limit"] = max(1, limit)
        with self.driver.session() as session:
            return [dict(record) for record in session.run(query, **parameters)]

    def create_or_resume_classification_run(
        self, record: dict[str, Any]
    ) -> dict[str, Any]:
        """Runを一度だけ作り、既存なら上書きせず返す。"""

        run_id = str(record["classificationRunId"])
        with self.driver.session() as session:
            existing = session.run(
                "MATCH (run:ClassificationRun {classificationRunId: $runId}) "
                "RETURN properties(run) AS run",
                runId=run_id,
            ).single()
            if existing is not None:
                return dict(existing["run"])
            properties = {
                **record,
                "graphNodeId": run_id,
                "nodeType": "ClassificationRun",
            }
            created = session.run(
                """
                CREATE (run:GraphNode:ClassificationRun {
                  graphNodeId: $runId,
                  classificationRunId: $runId
                })
                SET run += $properties
                RETURN properties(run) AS run
                """,
                runId=run_id,
                properties=properties,
            ).single()
            return dict(created["run"])

    def classification_checkpoints(
        self, classification_run_id: str
    ) -> list[dict[str, Any]]:
        query = """
        MATCH (checkpoint:ClassificationCheckpoint {
          classificationRunId: $classificationRunId
        })
        RETURN properties(checkpoint) AS checkpoint
        ORDER BY checkpoint.candidateKey
        """
        with self.driver.session() as session:
            return [
                dict(record["checkpoint"])
                for record in session.run(
                    query, classificationRunId=classification_run_id
                )
            ]

    def save_classification_checkpoint(
        self,
        *,
        checkpoint: dict[str, Any],
        assertions: list[dict[str, Any]],
    ) -> bool:
        """1候補の結果とAssertion群を同じtransactionで冪等保存する。"""

        run_id = str(checkpoint["classificationRunId"])
        checkpoint_id = str(checkpoint["checkpointId"])

        def persist(transaction: Any) -> bool:
            run_record = transaction.run(
                "MATCH (run:ClassificationRun {classificationRunId: $runId}) "
                "RETURN properties(run) AS run",
                runId=run_id,
            ).single()
            if run_record is None:
                raise RuntimeError("classification run does not exist")
            run = dict(run_record["run"])
            if str(run.get("phase")) != "building":
                raise RuntimeError("classification run is not building")
            existing = transaction.run(
                "MATCH (checkpoint:ClassificationCheckpoint {checkpointId: $checkpointId}) "
                "RETURN properties(checkpoint) AS checkpoint",
                checkpointId=checkpoint_id,
            ).single()
            replacing_failed = False
            if existing is not None:
                saved = dict(existing["checkpoint"])
                replacing_failed = str(saved.get("outcome")) == "failed"
                if not replacing_failed:
                    comparable = (
                        "classificationRunId",
                        "candidateKey",
                        "outcome",
                        "decisionPayloadHash",
                        "decisionPayloadJson",
                        "assertionCount",
                    )
                    if any(saved.get(key) != checkpoint.get(key) for key in comparable):
                        raise RuntimeError(
                            "classification checkpoint payload conflicts with persisted result"
                        )
                    return False

            for assertion in assertions:
                basis = transaction.run(
                    """
                    MATCH ()-[reference:REFERENCES {graphEdgeId: $basisEdgeId}]->()
                    WHERE reference.sourceSnapshotId = $sourceSnapshotId
                    RETURN count(reference) AS count
                    """,
                    basisEdgeId=assertion["basisEdgeId"],
                    sourceSnapshotId=assertion["sourceSnapshotId"],
                ).single()
                if basis is None or int(basis["count"]) != 1:
                    raise RuntimeError("assertion basis REFERENCES edge is unavailable")

            checkpoint_properties = {
                **checkpoint,
                "graphNodeId": checkpoint_id,
                "nodeType": "ClassificationCheckpoint",
            }
            if replacing_failed:
                transaction.run(
                    """
                    MATCH (checkpoint:ClassificationCheckpoint {
                      checkpointId: $checkpointId
                    })
                    SET checkpoint = $properties
                    """,
                    checkpointId=checkpoint_id,
                    properties=checkpoint_properties,
                ).consume()
            else:
                transaction.run(
                    """
                    CREATE (checkpoint:GraphNode:ClassificationCheckpoint {
                      graphNodeId: $checkpointId,
                      checkpointId: $checkpointId
                    })
                    SET checkpoint += $properties
                    """,
                    checkpointId=checkpoint_id,
                    properties=checkpoint_properties,
                ).consume()

            for assertion in assertions:
                assertion_id = str(assertion["assertionId"])
                assertion_properties = {
                    **assertion,
                    "graphNodeId": assertion_id,
                    "nodeType": "RelationAssertion",
                }
                result = transaction.run(
                    """
                    MATCH (run:ClassificationRun {classificationRunId: $runId})
                    MATCH (subject:Article {graphNodeId: $subjectArticleId})
                    MATCH (object:Article {graphNodeId: $objectArticleId})
                    CREATE (assertion:GraphNode:RelationAssertion {
                      graphNodeId: $assertionId,
                      assertionId: $assertionId,
                      assertionDedupeKey: $assertionDedupeKey
                    })
                    SET assertion += $properties
                    CREATE (assertion)-[:SUBJECT {
                      graphEdgeId: $subjectEdgeId,
                      edgeType: 'SUBJECT',
                      sourceSnapshotId: $sourceSnapshotId,
                      graphSchemaVersion: $graphSchemaVersion
                    }]->(subject)
                    CREATE (assertion)-[:OBJECT {
                      graphEdgeId: $objectEdgeId,
                      edgeType: 'OBJECT',
                      sourceSnapshotId: $sourceSnapshotId,
                      graphSchemaVersion: $graphSchemaVersion
                    }]->(object)
                    CREATE (assertion)-[:CLASSIFIED_IN {
                      graphEdgeId: $runEdgeId,
                      edgeType: 'CLASSIFIED_IN',
                      sourceSnapshotId: $sourceSnapshotId,
                      graphSchemaVersion: $graphSchemaVersion
                    }]->(run)
                    RETURN assertion.assertionId AS assertionId
                    """,
                    runId=run_id,
                    subjectArticleId=assertion["subjectArticleId"],
                    objectArticleId=assertion["objectArticleId"],
                    assertionId=assertion_id,
                    assertionDedupeKey=assertion["assertionDedupeKey"],
                    properties=assertion_properties,
                    subjectEdgeId=f"edge-{assertion_id}-subject",
                    objectEdgeId=f"edge-{assertion_id}-object",
                    runEdgeId=f"edge-{assertion_id}-classified-in-{run_id}",
                    sourceSnapshotId=assertion["sourceSnapshotId"],
                    graphSchemaVersion=assertion["graphSchemaVersion"],
                ).single()
                if result is None:
                    raise RuntimeError("assertion endpoints are unavailable")

            # package初期化時のGraphClient循環importを避け、実行時に契約正本を読む。
            from .domains.legal.graph_schema import (
                CHECKPOINT_OUTCOME_RUN_COUNT_FIELD,
            )

            outcome_counter = CHECKPOINT_OUTCOME_RUN_COUNT_FIELD[
                str(checkpoint["outcome"])
            ]
            # counter名は固定mapからだけ選び、LLM出力をCypherへ埋め込まない。
            if replacing_failed:
                if str(checkpoint["outcome"]) != "failed":
                    transaction.run(
                        f"""
                        MATCH (run:ClassificationRun {{classificationRunId: $runId}})
                        SET run.failedCount = run.failedCount - 1,
                            run.{outcome_counter} = run.{outcome_counter} + 1,
                            run.assertionCount = run.assertionCount + $assertionCount
                        """,
                        runId=run_id,
                        assertionCount=len(assertions),
                    ).consume()
            else:
                transaction.run(
                    f"""
                    MATCH (run:ClassificationRun {{classificationRunId: $runId}})
                    SET run.processedCount = run.processedCount + 1,
                        run.{outcome_counter} = run.{outcome_counter} + 1,
                        run.assertionCount = run.assertionCount + $assertionCount
                    """,
                    runId=run_id,
                    assertionCount=len(assertions),
                ).consume()
            return True

        with self.driver.session() as session:
            return bool(session.execute_write(persist))

    def classification_run_materialization(
        self, classification_run_id: str
    ) -> dict[str, Any]:
        """publish監査用にRun、checkpoint、Assertion端点を取得する。"""

        with self.driver.session() as session:
            run_record = session.run(
                "MATCH (run:ClassificationRun {classificationRunId: $runId}) "
                "RETURN properties(run) AS run",
                runId=classification_run_id,
            ).single()
            checkpoints = [
                dict(record["checkpoint"])
                for record in session.run(
                    """
                    MATCH (checkpoint:ClassificationCheckpoint {
                      classificationRunId: $runId
                    })
                    RETURN properties(checkpoint) AS checkpoint
                    ORDER BY checkpoint.candidateKey
                    """,
                    runId=classification_run_id,
                )
            ]
            assertions = [
                {
                    "assertion": dict(record["assertion"]),
                    "subjects": list(record["subjects"]),
                    "objects": list(record["objects"]),
                    "runs": list(record["runs"]),
                    "basisCount": int(record["basisCount"]),
                }
                for record in session.run(
                    """
                    MATCH (assertion:RelationAssertion {
                      classificationRunId: $runId
                    })
                    OPTIONAL MATCH (assertion)-[:SUBJECT]->(subject:Article)
                    OPTIONAL MATCH (assertion)-[:OBJECT]->(object:Article)
                    OPTIONAL MATCH (assertion)-[:CLASSIFIED_IN]->(run:ClassificationRun)
                    OPTIONAL MATCH ()-[basis:REFERENCES]->()
                    WHERE basis.graphEdgeId = assertion.basisEdgeId
                    RETURN properties(assertion) AS assertion,
                           collect(DISTINCT subject.graphNodeId) AS subjects,
                           collect(DISTINCT object.graphNodeId) AS objects,
                           collect(DISTINCT run.classificationRunId) AS runs,
                           count(DISTINCT basis) AS basisCount
                    ORDER BY assertion.assertionId
                    """,
                    runId=classification_run_id,
                )
            ]
        return {
            "run": dict(run_record["run"]) if run_record is not None else None,
            "checkpoints": checkpoints,
            "assertions": assertions,
        }

    def publish_classification_run(
        self, classification_run_id: str, *, published_at: datetime
    ) -> dict[str, Any]:
        query = """
        MATCH (run:ClassificationRun {classificationRunId: $runId})
        WHERE run.phase = 'building' AND run.processedCount = run.inputCount
        SET run.phase = 'published', run.publishedAt = $publishedAt
        RETURN properties(run) AS run
        """
        with self.driver.session() as session:
            record = session.run(
                query,
                runId=classification_run_id,
                publishedAt=published_at,
            ).single()
        if record is None:
            raise RuntimeError("only a complete building classification run can publish")
        return dict(record["run"])

    def fail_classification_run(
        self, classification_run_id: str, *, error_code: str
    ) -> None:
        with self.driver.session() as session:
            session.run(
                """
                MATCH (run:ClassificationRun {classificationRunId: $runId})
                WHERE run.phase = 'building'
                SET run.phase = 'failed', run.errorCode = $errorCode
                """,
                runId=classification_run_id,
                errorCode=error_code,
            ).consume()

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
