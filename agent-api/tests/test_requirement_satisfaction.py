"""EvidenceRequirementの役割別充足判定テスト。"""

from app.evidence_requirements import EvidenceRequirement
from app.requirement_satisfaction import assess_candidate


def _candidate(article_id: str, text: str, **overrides) -> dict:
    return {
        "articleId": article_id,
        "heading": "",
        "text": text,
        "chunks": [{"contentUnitId": article_id, "text": text}],
        **overrides,
    }


def _requirement(role_family: str, **overrides) -> EvidenceRequirement:
    values = {
        "requirement_id": "req-1",
        "issue_id": "issue-1",
        "role_family": role_family,
        "key_terms": ("公開買付け",),
        "query_hint": "公開買付けの要件",
    }
    values.update(overrides)
    return EvidenceRequirement(**values)


def test_unrelated_candidate_does_not_satisfy_requirement() -> None:
    result = assess_candidate(
        _requirement("qualification"),
        _candidate("law-a-article-1", "株式会社の計算書類について定める。"),
    )
    assert result.satisfied is False
    assert "missing_key_term" in result.reasons


def test_role_and_key_term_must_both_match_for_lexical_candidate() -> None:
    result = assess_candidate(
        _requirement("qualification"),
        _candidate("law-a-article-2", "公開買付けを行うための要件は次のとおりとする。"),
    )
    assert result.satisfied is True


def test_exact_structural_article_is_accepted_without_lexical_similarity() -> None:
    result = assess_candidate(
        _requirement(
            "qualification",
            article_id="law-a-article-27_3",
            key_terms=(),
        ),
        _candidate("law-a-article-27_3", "当該規定の本文。"),
    )
    assert result.satisfied is True
    assert result.structurally_required is True


def test_wrong_article_does_not_satisfy_direct_requirement() -> None:
    result = assess_candidate(
        _requirement("normative_rule", article_id="law-a-article-2"),
        _candidate("law-a-article-3", "公開買付けについて定める。"),
    )
    assert result.satisfied is False
    assert "article_id_mismatch" in result.reasons


def test_direct_graph_or_explains_target_is_structurally_satisfying() -> None:
    result = assess_candidate(
        _requirement("normative_rule"),
        _candidate(
            "law-a-article-9",
            "質問の自然言語とは異なる法令用語で書かれた条文。",
            directMatch=True,
        ),
    )
    assert result.satisfied is True
    assert result.structurally_required is True
    assert result.reasons == ("direct_article_target",)


def test_unresolved_reference_cues_are_reported() -> None:
    result = assess_candidate(
        _requirement("linkage"),
        _candidate("law-a-article-4", "公開買付けについて前項の規定を準用する。"),
    )
    assert result.satisfied is True
    assert "前項" in result.unresolved_reference_cues
