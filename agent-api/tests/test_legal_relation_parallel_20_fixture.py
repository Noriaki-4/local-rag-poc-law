import json
from collections import Counter
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = (
    REPO_ROOT
    / "docs/samples/eval/legal_relation_parallel_20_adjudicated_fixture.jsonl"
)


def _load_fixture() -> list[dict[str, object]]:
    with FIXTURE_PATH.open(encoding="utf-8") as fixture_file:
        return [json.loads(line) for line in fixture_file if line.strip()]


def test_parallel_fixture_has_twenty_unique_structurally_resolved_pairs():
    fixtures = _load_fixture()

    assert len(fixtures) == 20
    assert len({item["fixtureId"] for item in fixtures}) == 20
    assert len({item["basisEdgeId"] for item in fixtures}) == 20
    assert {item["expectedResolutionStatus"] for item in fixtures} == {"resolved"}
    assert all(
        item["currentReferenceTargetArticleId"]
        == item["expectedReferenceTargetArticleId"]
        for item in fixtures
    )


def test_parallel_fixture_preserves_independent_five_predicate_labels():
    fixtures = _load_fixture()
    predicate_counts = Counter(
        predicate
        for item in fixtures
        for predicate in item["expectedPredicates"]
    )

    assert predicate_counts == {
        "IMPLEMENTS": 4,
        "USES_DEFINITION": 10,
        "EXCEPTION_TO": 1,
        "OVERRIDES": 1,
    }
    assert sum(not item["expectedPredicates"] for item in fixtures) == 6
    assert any(len(item["expectedPredicates"]) > 1 for item in fixtures)


def test_parallel_fixture_records_manual_audit_basis():
    fixtures = _load_fixture()

    assert all(item["annotationBasis"] for item in fixtures)
    assert {
        item["adjudicationSource"] for item in fixtures
    } == {"codex_manual_review_2026-08-19_parallel_worker_reviewer_v6"}
