import importlib.util
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts/evaluate_legal_relation_20_adjudicated.py"
FIXTURE_PATH = (
    REPO_ROOT
    / "docs/requirements/samples/eval/legal_relation_20_adjudicated_fixture.jsonl"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "evaluate_legal_relation_20_adjudicated", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_adjudicated_fixture_separates_resolution_and_predicate_gold():
    module = _load_module()
    fixtures = module.load_fixtures(FIXTURE_PATH)

    assert len(fixtures) == 20
    assert Counter(item["expectedResolutionStatus"] for item in fixtures) == {
        "resolved": 17,
        "unresolved": 2,
        "not_reference": 1,
    }
    assert sum(item["expectedPredicates"] is not None for item in fixtures) == 14
    assert sum(
        item["expectedResolutionStatus"] == "resolved"
        and item["currentReferenceTargetArticleId"]
        == item["expectedReferenceTargetArticleId"]
        for item in fixtures
    ) == 14


def test_invalid_current_pairs_are_not_semantic_teacher_labels():
    module = _load_module()
    fixtures = module.load_fixtures(FIXTURE_PATH)

    invalid_current_pairs = [
        item
        for item in fixtures
        if item["expectedResolutionStatus"] != "resolved"
        or item["currentReferenceTargetArticleId"]
        != item["expectedReferenceTargetArticleId"]
    ]

    assert len(invalid_current_pairs) == 6
    assert all(item["expectedPredicates"] is None for item in invalid_current_pairs)


def test_structural_evaluation_can_run_without_rebuilding_graph():
    module = _load_module()
    resolved = {
        "fixtureId": "resolved",
        "referenceSourceArticleId": "law-a-article-2",
        "currentReferenceTargetArticleId": "law-a-article-1",
        "expectedResolutionStatus": "resolved",
        "expectedReferenceTargetArticleId": "law-a-suppl-0-article-1",
        "annotationBasis": "附則内参照",
    }
    corrected_row = {
        "referenceSourceArticle": {"graphNodeId": "law-a-article-2"},
        "referenceTargetArticle": {"graphNodeId": "law-a-suppl-0-article-1"},
    }
    stale_row = {
        "referenceSourceArticle": {"graphNodeId": "law-a-article-2"},
        "referenceTargetArticle": {"graphNodeId": "law-a-article-1"},
    }
    not_reference = {
        **resolved,
        "fixtureId": "not-reference",
        "expectedResolutionStatus": "not_reference",
        "expectedReferenceTargetArticleId": None,
    }

    assert module.structural_result(resolved, corrected_row)["passed"] is True
    assert module.structural_result(resolved, stale_row)["passed"] is False
    assert module.structural_result(not_reference, None)["passed"] is True
    assert module.structural_result(not_reference, stale_row)["passed"] is False
