from app.domains.legal.graph_schema import (
    NEO4J_SCHEMA_STATEMENTS,
    PHYSICAL_RELATION_TYPES,
    ClassificationRunPhase,
    GraphDirection,
    GraphSearchMode,
    ProposedPredicate,
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
    assert "n.graphNodeId IS UNIQUE" in schema
    assert "n.assertionId IS UNIQUE" in schema
    assert "n.assertionDedupeKey IS UNIQUE" in schema
    assert "n.classificationRunId IS UNIQUE" in schema
    assert "n.proposedPredicate" in schema
    assert "n.sourceSnapshotId" in schema
