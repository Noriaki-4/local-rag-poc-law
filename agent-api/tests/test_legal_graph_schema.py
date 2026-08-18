from app.domains.legal.graph_schema import (
    CHECKPOINT_OUTCOME_RUN_COUNT_FIELD,
    NEO4J_SCHEMA_STATEMENTS,
    PHYSICAL_RELATION_TYPES,
    ClassificationCheckpointOutcome,
    ClassificationRunPhase,
    GraphDirection,
    GraphSearchMode,
    ProposedPredicate,
    RelationClassificationOutcome,
)


def test_graph_vocabularies_match_the_new_contract() -> None:
    assert {item.value for item in ProposedPredicate} == {
        "IMPLEMENTS",
        "INCORPORATES",
        "USES_DEFINITION",
        "EXCEPTION_TO",
        "OVERRIDES",
    }
    assert {item.value for item in ClassificationRunPhase} == {
        "building",
        "published",
        "failed",
    }
    assert {item.value for item in ClassificationCheckpointOutcome} == {
        "classified",
        "reference_only",
        "uncertain",
        "failed",
    }
    assert {item.value for item in RelationClassificationOutcome} == {
        "classified",
        "reference_only",
        "uncertain",
    }
    assert set(CHECKPOINT_OUTCOME_RUN_COUNT_FIELD) == {
        item.value for item in ClassificationCheckpointOutcome
    }
    assert {item.value for item in GraphDirection} == {
        "from_subject",
        "to_subject",
    }
    assert {item.value for item in GraphSearchMode} == {
        "semantic_assertion",
        "explicit_reference",
        "explains",
    }


def test_only_deterministic_and_assertion_relations_are_physical() -> None:
    assert PHYSICAL_RELATION_TYPES == {
        "HAS_CONTENT_UNIT",
        "REFERENCES",
        "EXPLAINS",
        "SUBJECT",
        "OBJECT",
        "CLASSIFIED_IN",
    }
    assert not PHYSICAL_RELATION_TYPES.intersection(
        {item.value for item in ProposedPredicate}
    )
    assert "MENTIONS" not in PHYSICAL_RELATION_TYPES
    assert "APPLIED_BY" not in PHYSICAL_RELATION_TYPES


def test_schema_has_all_required_uniqueness_constraints_and_indexes() -> None:
    schema = "\n".join(NEO4J_SCHEMA_STATEMENTS)
    assert NEO4J_SCHEMA_STATEMENTS[0] == "DROP INDEX graph_node_id IF EXISTS"
    assert "n.graphNodeId IS UNIQUE" in schema
    assert "n.assertionId IS UNIQUE" in schema
    assert "n.assertionDedupeKey IS UNIQUE" in schema
    assert "n.classificationRunId IS UNIQUE" in schema
    assert "n.checkpointId IS UNIQUE" in schema
    assert "n.proposedPredicate" in schema
    assert "n.sourceSnapshotId" in schema
    assert "ClassificationCheckpoint" in schema
