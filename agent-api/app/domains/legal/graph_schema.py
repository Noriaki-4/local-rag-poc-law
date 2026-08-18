"""法令Graphの物理schemaと、LLMへ公開する探索語彙の正本。"""

from __future__ import annotations

from enum import StrEnum


class ProposedPredicate(StrEnum):
    """RelationAssertionが提示できる未確認の意味関係。"""

    IMPLEMENTS = "IMPLEMENTS"
    INCORPORATES = "INCORPORATES"
    USES_DEFINITION = "USES_DEFINITION"
    EXCEPTION_TO = "EXCEPTION_TO"
    OVERRIDES = "OVERRIDES"


class ClassificationRunPhase(StrEnum):
    """非同期分類Runの機械的な実行状態。"""

    BUILDING = "building"
    PUBLISHED = "published"
    FAILED = "failed"


class GraphDirection(StrEnum):
    """検索起点がRelationAssertionのどちら側かを表す。"""

    FROM_SUBJECT = "from_subject"
    TO_SUBJECT = "to_subject"


class GraphSearchMode(StrEnum):
    """LLMが選択できる固定Graph検索。自由Cypherは受け付けない。"""

    SEMANTIC_ASSERTION = "semantic_assertion"
    EXPLICIT_REFERENCE = "explicit_reference"
    EXPLAINS = "explains"


PHYSICAL_NODE_LABELS = frozenset(
    {
        "Document",
        "Article",
        "Paragraph",
        "Item",
        "RelationAssertion",
        "ClassificationRun",
    }
)

PHYSICAL_RELATION_TYPES = frozenset(
    {
        "HAS_CONTENT_UNIT",
        "REFERENCES",
        "EXPLAINS",
        "SUBJECT",
        "OBJECT",
        "CLASSIFIED_IN",
    }
)


# Neo4jのschema作成も定義箇所を分散させない。各文は再実行可能である。
NEO4J_SCHEMA_STATEMENTS = (
    "CREATE CONSTRAINT graph_node_id_unique IF NOT EXISTS "
    "FOR (n:GraphNode) REQUIRE n.graphNodeId IS UNIQUE",
    "CREATE CONSTRAINT relation_assertion_id_unique IF NOT EXISTS "
    "FOR (n:RelationAssertion) REQUIRE n.assertionId IS UNIQUE",
    "CREATE CONSTRAINT relation_assertion_dedupe_key_unique IF NOT EXISTS "
    "FOR (n:RelationAssertion) REQUIRE n.assertionDedupeKey IS UNIQUE",
    "CREATE CONSTRAINT classification_run_id_unique IF NOT EXISTS "
    "FOR (n:ClassificationRun) REQUIRE n.classificationRunId IS UNIQUE",
    "CREATE INDEX graph_node_document_id IF NOT EXISTS "
    "FOR (n:GraphNode) ON (n.documentId)",
    "CREATE INDEX document_authority_type IF NOT EXISTS "
    "FOR (n:Document) ON (n.authorityType)",
    "CREATE INDEX relation_assertion_predicate IF NOT EXISTS "
    "FOR (n:RelationAssertion) ON (n.proposedPredicate)",
    "CREATE INDEX relation_assertion_run_id IF NOT EXISTS "
    "FOR (n:RelationAssertion) ON (n.classificationRunId)",
    "CREATE INDEX classification_run_snapshot_id IF NOT EXISTS "
    "FOR (n:ClassificationRun) ON (n.sourceSnapshotId)",
)


__all__ = [
    "ClassificationRunPhase",
    "GraphDirection",
    "GraphSearchMode",
    "NEO4J_SCHEMA_STATEMENTS",
    "PHYSICAL_NODE_LABELS",
    "PHYSICAL_RELATION_TYPES",
    "ProposedPredicate",
]
