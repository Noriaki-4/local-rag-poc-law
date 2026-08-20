import json
from pathlib import Path

from app.legal_relation_classification_job import (
    RELATION_ADJUDICATION_PROMPT_VERSION,
    RELATION_CLASSIFICATION_PROMPT_VERSION,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPO_ROOT / ".agents/skills/legal-relation-adjudicator"
PAIR_GOLD = (
    REPO_ROOT
    / "docs/requirements/samples/eval/legal_relation_73_pair_overrides.jsonl"
)
EDGE_GOLD = (
    REPO_ROOT
    / "docs/requirements/samples/eval/legal_relation_94_adjudicated_fixture.jsonl"
)


def _load_jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _pair_record(subject_id: str, object_id: str) -> dict[str, object]:
    for record in _load_jsonl(PAIR_GOLD):
        assertions = record["assertions"]
        if any(
            assertion["subjectArticleId"] == subject_id
            and assertion["objectArticleId"] == object_id
            for assertion in assertions
        ):
            return record
    raise AssertionError(f"missing semantic pair {subject_id} -> {object_id}")


def _established_predicates(record: dict[str, object]) -> set[str]:
    return {
        assertion["proposedPredicate"] for assertion in record["assertions"]
    }


def test_read_as_application_contract_requires_independent_dual_check() -> None:
    classification = (
        SKILL_ROOT / "references/classification-contract.md"
    ).read_text(encoding="utf-8")
    review = (SKILL_ROOT / "references/review-contract.md").read_text(
        encoding="utf-8"
    )
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

    incorporates = classification.split("### INCORPORATES", maxsplit=1)[1].split(
        "### USES_DEFINITION", maxsplit=1
    )[0]
    assert "第X条の規定の適用については" in incorporates
    assert "Establish `INCORPORATES` and evaluate `OVERRIDES`" in incorporates
    assert "Do not infer `INCORPORATES` from every `OVERRIDES`" in incorporates
    assert "第百四十条" not in incorporates
    assert "confirm both `INCORPORATES` and `OVERRIDES`" in review
    assert "exact read-as occurrence establishes `OVERRIDES`" in skill
    assert RELATION_ADJUDICATION_PROMPT_VERSION == (
        "legal-relation-5predicate-v23-pair"
    )
    assert RELATION_CLASSIFICATION_PROMPT_VERSION == (
        "legal-relation-5predicate-v21-pair"
    )


def test_read_as_and_non_application_gold_preserve_predicate_boundary() -> None:
    read_as = _pair_record(
        "law-336M50000100001-suppl-381-article-2",
        "law-336M50000100001-article-140",
    )
    non_application = _pair_record(
        "law-403AC0000000090-article-23",
        "law-403AC0000000090-article-13",
    )

    assert _established_predicates(read_as) == {
        "INCORPORATES",
        "EXCEPTION_TO",
        "OVERRIDES",
    }
    assert _established_predicates(non_application) == {
        "EXCEPTION_TO",
        "OVERRIDES",
    }

    ordinary_incorporation = next(
        record
        for record in _load_jsonl(EDGE_GOLD)
        if record["fixtureId"] == "legal-relation-100-056"
    )
    assert ordinary_incorporation["expectedPredicates"] == ["INCORPORATES"]
