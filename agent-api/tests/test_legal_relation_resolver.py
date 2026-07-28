"""法令間関係の判定と子Requirement生成のテスト (計画書 §6.1, §8.4, §16.1)。"""

import pytest

from app.evidence_requirements import (
    ORIGIN_ARTICLE_TEXT,
    ORIGIN_GRAPH,
    ORIGIN_PLANNER,
    EvidenceRequirement,
)
from app.legal_ontology import (
    AUTHORITY_CABINET_OFFICE_ORDINANCE,
    AUTHORITY_CABINET_ORDER,
    IMPLEMENTS_CONFIDENCE_EXPLICIT_DELEGATION,
    IMPLEMENTS_CONFIDENCE_FAMILY_RULE,
    REFERENCE_KIND_APPLICATION,
    REFERENCE_KIND_DELEGATION_PARENT,
    REFERENCE_KIND_EXCEPTION,
    REFERENCE_ONLY_CONFIDENCE,
)
from app.legal_relation_resolver import (
    assess_implements,
    child_requirements_from_article_text,
    child_requirements_from_graph,
    classify_reference_kind,
    has_delegation_wording,
)


@pytest.fixture(autouse=True)
def _local_law_registry(monkeypatch: pytest.MonkeyPatch):
    """テスト実行時は、コンテナ内パスではなくリポジトリのlaw_registry.jsonを読む。"""
    from pathlib import Path

    from app import law_family
    from app.config import settings

    monkeypatch.setattr(
        settings, "samples_dir", Path(__file__).resolve().parents[2] / "docs" / "requirements" / "samples"
    )
    law_family.clear_cache()
    yield
    law_family.clear_cache()


def _requirement(**overrides: object) -> EvidenceRequirement:
    values: dict[str, object] = {
        "requirement_id": "req-1",
        "issue_id": "issue-1",
        "role_family": "qualification",
        "role_subtypes": ("exception",),
        "origin": ORIGIN_PLANNER,
        "conclusion_group_ids": ("group-issue-1-normative_rule",),
        "article_id": "law-a-article-27_2",
    }
    values.update(overrides)
    return EvidenceRequirement(**values)  # type: ignore[arg-type]


class TestImplementsAssessment:
    def test_explicit_delegation_is_high_confidence(self) -> None:
        assessment = assess_implements(
            parent_text="前項に規定するもののほか、政令で定めるものを除く。",
            child_text="法第二十七条の二第一項に規定する政令で定めるものは、次に掲げるものとする。",
            child_authority_type=AUTHORITY_CABINET_ORDER,
            same_family=True,
        )
        assert assessment.confidence == IMPLEMENTS_CONFIDENCE_EXPLICIT_DELEGATION
        assert assessment.delegation_wording_detected is True
        assert assessment.is_implements is True

    def test_plain_reference_stays_references(self) -> None:
        """委任文言も具体化表現も無い単純参照はIMPLEMENTSにしない (§6.1)。"""
        assessment = assess_implements(
            parent_text="公開買付けを行う者は、公開買付開始公告を行わなければならない。",
            child_text="法第二十七条の三を参照。",
            child_authority_type=AUTHORITY_CABINET_OFFICE_ORDINANCE,
            same_family=True,
        )
        assert assessment.confidence == REFERENCE_ONLY_CONFIDENCE
        assert assessment.is_implements is False

    def test_specification_wording_without_delegation_is_family_rule(self) -> None:
        assessment = assess_implements(
            parent_text="公開買付開始公告を行わなければならない。",
            child_text="法第二十七条の三第一項の規定により公告すべき事項は、次のとおりとする。",
            child_authority_type=AUTHORITY_CABINET_OFFICE_ORDINANCE,
            same_family=True,
        )
        assert assessment.confidence == IMPLEMENTS_CONFIDENCE_FAMILY_RULE
        assert assessment.is_implements is True

    def test_other_family_is_never_implements(self) -> None:
        assessment = assess_implements(
            parent_text="政令で定めるところにより届け出なければならない。",
            child_text="法第五条の規定により…",
            child_authority_type=AUTHORITY_CABINET_ORDER,
            same_family=False,
        )
        assert assessment.is_implements is False

    def test_delegation_wording_is_layer_specific(self) -> None:
        text = "内閣府令で定める事項を公告しなければならない。"
        assert has_delegation_wording(text, AUTHORITY_CABINET_OFFICE_ORDINANCE) is True
        assert has_delegation_wording(text, AUTHORITY_CABINET_ORDER) is False


class TestReferenceKind:
    def test_delegation_parent(self) -> None:
        assert (
            classify_reference_kind("法第五条に規定する", is_parent_law_reference=True)
            == REFERENCE_KIND_DELEGATION_PARENT
        )

    def test_application(self) -> None:
        assert classify_reference_kind("第二十七条の規定を準用する。") == REFERENCE_KIND_APPLICATION

    def test_exception(self) -> None:
        assert classify_reference_kind("ただし、第三条の場合はこの限りでない。") == REFERENCE_KIND_EXCEPTION


class TestChildRequirementsFromArticleText:
    def test_cabinet_order_delegation_creates_child(self) -> None:
        children = child_requirements_from_article_text(
            _requirement(),
            article_id="law-a-article-27_2",
            text="政令で定めるものとする。",
        )
        assert [child.authority_type for child in children] == [AUTHORITY_CABINET_ORDER]
        child = children[0]
        assert child.entered_by == "IMPLEMENTS"
        assert child.origin == ORIGIN_ARTICLE_TEXT
        assert child.parent_article_id == "law-a-article-27_2"
        assert child.conclusion_group_ids == ("group-issue-1-normative_rule",)
        assert child.depth == 1

    def test_cabinet_office_ordinance_delegation(self) -> None:
        children = child_requirements_from_article_text(
            _requirement(),
            article_id="law-a-article-27_3",
            text="内閣府令で定める事項を公告しなければならない。",
        )
        assert children[0].authority_type == AUTHORITY_CABINET_OFFICE_ORDINANCE

    def test_application_creates_linkage_requirement(self) -> None:
        children = child_requirements_from_article_text(
            _requirement(), article_id="law-a-article-1", text="第二十七条の規定を準用する。"
        )
        assert any(child.role_family == "linkage" for child in children)

    def test_exception_cue_creates_exception_requirement(self) -> None:
        children = child_requirements_from_article_text(
            _requirement(),
            article_id="law-a-article-27_2",
            text="政令で定めるものを除く。",
        )
        assert {child.authority_type for child in children} == {AUTHORITY_CABINET_ORDER, None}
        exception = next(child for child in children if child.authority_type is None)
        assert exception.role_subtypes == ("exception",)

    def test_no_cue_creates_no_child(self) -> None:
        children = child_requirements_from_article_text(
            _requirement(), article_id="law-a-article-1", text="有価証券の売買を行うことができる。"
        )
        assert children == ()

    def test_children_are_deduplicated_by_key(self) -> None:
        children = child_requirements_from_article_text(
            _requirement(),
            article_id="law-a-article-1",
            text="政令で定めるところにより、政令で定める場合とする。",
        )
        assert len(children) == 1


class TestChildRequirementsFromGraph:
    def _edge(self, **overrides: object) -> dict[str, object]:
        edge = {
            "edgeType": "IMPLEMENTS",
            "fromGraphNodeId": "law-a-article-27_2",
            "toGraphNodeId": "law-b-article-7",
            "relationSource": "subordinate_law_parent_reference",
            "relationConfidence": IMPLEMENTS_CONFIDENCE_EXPLICIT_DELEGATION,
            "derivedFromEdgeId": "edge-law-b-article-7-references-law-a-article-27_2",
            "delegationWordingDetected": True,
        }
        edge.update(overrides)
        return edge

    def test_trusted_edge_creates_child(self) -> None:
        children = child_requirements_from_graph(
            _requirement(),
            [self._edge()],
            authority_types_by_article={"law-b-article-7": AUTHORITY_CABINET_ORDER},
        )
        assert len(children) == 1
        assert children[0].article_id == "law-b-article-7"
        assert children[0].authority_type == AUTHORITY_CABINET_ORDER
        assert children[0].origin == ORIGIN_GRAPH

    def test_low_confidence_edge_is_skipped(self) -> None:
        children = child_requirements_from_graph(
            _requirement(), [self._edge(relationConfidence=0.7, delegationWordingDetected=False)]
        )
        assert children == ()

    def test_unverified_assertion_is_skipped(self) -> None:
        children = child_requirements_from_graph(
            _requirement(), [self._edge(status="unverified", relationSource="guidance_assertion")]
        )
        assert children == ()

    def test_mentions_edge_does_not_expand(self) -> None:
        children = child_requirements_from_graph(
            _requirement(),
            [
                self._edge(
                    edgeType="MENTIONS",
                    relationSource="guidance_mention_rule",
                    relationConfidence=0.9,
                )
            ],
        )
        assert children == ()

    def test_applied_by_becomes_linkage_requirement(self) -> None:
        children = child_requirements_from_graph(
            _requirement(),
            [
                self._edge(
                    edgeType="APPLIED_BY",
                    relationSource="incorporation_reference_rule",
                    relationConfidence=0.9,
                )
            ],
        )
        assert children[0].role_family == "linkage"
        assert children[0].role_subtypes == ("application",)

    def test_max_children_is_capped(self) -> None:
        edges = [self._edge(toGraphNodeId=f"law-b-article-{index}") for index in range(10)]
        children = child_requirements_from_graph(_requirement(), edges, max_children=6)
        assert len(children) == 6


class TestLawFamilyScoping:
    """委任先の探索を同一法令系統へ絞る (計画書 §6.3-7, §9.1)。"""

    def test_child_inherits_family_root_from_parent_article(self) -> None:
        children = child_requirements_from_article_text(
            _requirement(),
            article_id="law-335AC0000000145-article-18_2",
            text="厚生労働省令で定める。",
        )
        assert children[0].family_root == "law-335AC0000000145"

    def test_unknown_law_family_does_not_block_expansion(self) -> None:
        children = child_requirements_from_article_text(
            _requirement(),
            article_id="law-unknown-article-1",
            text="政令で定める。",
        )
        assert children[0].family_root is None
