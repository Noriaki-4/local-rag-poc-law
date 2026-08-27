"""新FrameworkのArticle検索とGraphナビゲーション接続。"""

from __future__ import annotations

from typing import Any

import pytest

from app.adapters.tools.legal_search import (
    LegalFetchArticlesTool,
    LegalGraphNeighborsTool,
    LegalSearchTool,
    _graph_navigation_evidence,
)
from app.agent_framework.contracts import SolverDecision
from app.agent_framework.profiles import AgentLimits
from app.agent_framework.state import CaseState, Evidence, FinalAnswer, ToolRequest
from app.agent_framework.validation import ContractViolation, apply_solver_decision
from app.domains.legal.tools import legal_tool_registry


class FakeArticleSearch:
    def __init__(self) -> None:
        self.specs: list[Any] = []
        self.timeout_sec: float | None = None

    def search_requirement_specs(
        self,
        specs,
        *,
        user_clearance_level: int,
        timeout_sec: float,
    ) -> dict[str, list[dict[str, Any]]]:
        assert user_clearance_level == 2
        self.specs = specs
        self.timeout_sec = timeout_sec
        output: dict[str, list[dict[str, Any]]] = {}
        for spec in specs:
            if spec.doc_type == "law":
                output[spec.requirement_id] = [
                    {
                        "articleId": "law-a-article-27_2",
                        "chunks": [
                            {
                                "contentUnitId": "law-a-article-27_2-paragraph-1",
                                "articleContentUnitId": "law-a-article-27_2",
                                "documentId": "law-a",
                                "docType": "law",
                                "heading": "第二十七条の二 第1項",
                                "text": "公開買付けの要件を定める。",
                            },
                            {
                                "contentUnitId": "law-a-article-27_2-paragraph-2",
                                "articleContentUnitId": "law-a-article-27_2",
                                "documentId": "law-a",
                                "docType": "law",
                                "heading": "第二十七条の二 第2項",
                                "text": "同じArticleの別chunk。",
                            },
                            *[
                                {
                                    "contentUnitId": (
                                        "law-a-article-27_2-paragraph-1-item-"
                                        f"{item_no}"
                                    ),
                                    "articleContentUnitId": "law-a-article-27_2",
                                    "documentId": "law-a",
                                    "docType": "law",
                                    "heading": f"第二十七条の二 第1項第{item_no}号",
                                    "text": (
                                        "同じArticleの一致箇所。"
                                        + (
                                            "少数所有者と全所有者同意を"
                                            "内閣府令で定める。"
                                            if item_no == 13
                                            else ""
                                        )
                                    ),
                                }
                                for item_no in range(3, 14)
                            ],
                        ],
                    }
                ]
            else:
                output[spec.requirement_id] = [
                    {
                        "source": {
                            "contentUnitId": "guide-1",
                            "documentId": "guide",
                            "docType": "guideline",
                            "heading": "Q&A",
                            "text": "公開買付けの解説。",
                        }
                    }
                ]
        return output

    def get_by_article_ids(
        self,
        article_ids,
        user_clearance_level: int,
        *,
        max_chunks: int,
    ) -> list[dict[str, Any]]:
        assert article_ids == ["law-a-article-27_2"]
        assert user_clearance_level == 2
        assert max_chunks > 0
        return [
            {
                "contentUnitId": "law-a-article-27_2-paragraph-1",
                "articleContentUnitId": "law-a-article-27_2",
                "documentId": "law-a",
                "docType": "law",
                "heading": "第二十七条の二 第1項",
                "text": "公開買付けの要件を定める。",
            }
        ]


class FakeGraph:
    def __init__(self) -> None:
        self.formal_args: dict[str, Any] = {}
        self.assertion_args: dict[str, Any] = {}

    def article_relations_touching(self, article_ids, **kwargs):
        self.formal_args = {"article_ids": article_ids, **kwargs}
        return [
            {
                "graphEdgeId": "edge-1",
                "edgeType": "REFERENCES",
                "fromArticleId": "law-order-article-7",
                "fromDocumentId": "law-order",
                "fromTitle": "金融商品取引法施行令",
                "fromHeading": "第七条",
                "toArticleId": "law-act-article-27_2",
                "toDocumentId": "law-act",
                "toTitle": "金融商品取引法",
                "toHeading": "第二十七条の二",
            }
        ]

    def relation_assertions_touching(self, article_ids, **kwargs):
        self.assertion_args = {"article_ids": article_ids, **kwargs}
        return [
            {
                "assertionId": "assertion-1",
                "proposedPredicate": "USES_DEFINITION",
                "subjectArticleId": "law-act-article-27_2",
                "objectArticleId": "law-ordinance-article-2_5",
                "subjectSupportingSpanId": "law-act-article-27_2::span-1",
                "objectSupportingSpanId": "law-ordinance-article-2_5::span-2",
                "subjectSupportingQuote": "届出者は対象株券等を記載する。",
                "objectSupportingQuote": "対象株券等とは、次に掲げるものをいう。",
                "relationExplanation": "SUBJECTが使う対象株券等の範囲をOBJECTが定義する。",
                "classificationRunId": "classification-run-1",
                "basisEdgeId": "edge-definition-1",
            }
        ]


class MultiSeedGraph:
    def __init__(self) -> None:
        self.formal_calls: list[list[str]] = []
        self.assertion_calls: list[list[str]] = []

    def article_relations_touching(self, article_ids, **kwargs):
        del kwargs
        self.formal_calls.append(article_ids)
        if article_ids == ["law-act-article-27_2"]:
            return [
                {
                    "graphEdgeId": "edge-ordinance-2_5",
                    "edgeType": "REFERENCES",
                    "fromArticleId": "law-ordinance-article-2_5",
                    "fromDocumentId": "law-ordinance",
                    "fromHeading": "第二条の五",
                    "toArticleId": "law-act-article-27_2",
                    "toDocumentId": "law-act",
                    "toHeading": "第二十七条の二",
                }
            ]
        if article_ids == ["law-act-article-27_3"]:
            return [
                {
                    "graphEdgeId": "edge-ordinance-10",
                    "edgeType": "REFERENCES",
                    "fromArticleId": "law-ordinance-article-10",
                    "fromDocumentId": "law-ordinance",
                    "fromHeading": "第十条",
                    "toArticleId": "law-act-article-27_3",
                    "toDocumentId": "law-act",
                    "toHeading": "第二十七条の三",
                }
            ]
        return []

    def relation_assertions_touching(self, article_ids, **kwargs):
        del kwargs
        self.assertion_calls.append(article_ids)
        return []


class DuplicatePairGraph:
    def article_relations_touching(self, article_ids, **kwargs):
        del article_ids, kwargs
        return [
            {
                "graphEdgeId": "edge-reference-paragraph-1",
                "edgeType": "REFERENCES",
                "fromArticleId": "law-order-article-7",
                "fromDocumentId": "law-order",
                "toArticleId": "law-act-article-27_2",
                "toDocumentId": "law-act",
            },
            {
                "graphEdgeId": "edge-reference-paragraph-2",
                "edgeType": "REFERENCES",
                "fromArticleId": "law-order-article-7",
                "fromDocumentId": "law-order",
                "toArticleId": "law-act-article-27_2",
                "toDocumentId": "law-act",
            },
        ]

    def relation_assertions_touching(self, article_ids, **kwargs):
        del article_ids, kwargs
        return [
            {
                "assertionId": "assertion-implements",
                "suggestedType": "IMPLEMENTS",
                "status": "unverified",
                "fromArticleId": "law-act-article-27_2",
                "fromDocumentId": "law-act",
                "toArticleId": "law-order-article-7",
                "toDocumentId": "law-order",
            }
        ]


def _request(tool_name: str, arguments: dict[str, Any]) -> ToolRequest:
    return ToolRequest(
        request_id=f"request-{tool_name}",
        work_item_id="work-1",
        tool_name=tool_name,
        arguments=arguments,
        purpose="接続を確認する",
    )


def test_legal_search_uses_article_aggregation_and_document_scope() -> None:
    client = FakeArticleSearch()
    execution = LegalSearchTool(
        client,  # type: ignore[arg-type]
        user_clearance_level=2,
        top_k=8,
    ).execute(
        _request(
            "legal_search",
            {
                "query": "公開買付けの要件",
                "doc_types": ["law", "guideline"],
                "document_ids": ["law-a"],
            },
        ),
        cycle_no=1,
        timeout_sec=12.5,
    )

    assert [spec.doc_type for spec in client.specs] == ["law", "guideline"]
    assert [spec.top_k for spec in client.specs] == [8, 2]
    assert all(spec.document_ids == ("law-a",) for spec in client.specs)
    assert client.timeout_sec == 12.5
    assert execution.result.evidence_ids == (
        execution.evidence[0].evidence_id,
        "search-nav-guide-1",
    )
    assert execution.evidence[0].evidence_id.startswith("search-nav-")
    assert "公開買付けの要件を定める。" in execution.evidence[0].content
    assert "同じArticleの別chunk。" in execution.evidence[0].content
    assert "少数所有者と全所有者同意" in execution.evidence[0].content
    assert execution.evidence[0].metadata["matchedChunkCount"] == 13
    assert "law-a-article-27_2-paragraph-2" not in execution.evidence[0].content
    assert execution.evidence[0].metadata["articleId"] == "law-a-article-27_2"
    assert execution.evidence[0].metadata["citationEligible"] is False
    assert execution.evidence[0].metadata["evidenceRole"] == "search_navigation"
    assert execution.evidence[1].metadata["citationEligible"] is False
    assert execution.evidence[1].metadata["articleId"] is None


def test_fetch_articles_returns_citation_eligible_text_with_source_id() -> None:
    execution = LegalFetchArticlesTool(
        FakeArticleSearch(),  # type: ignore[arg-type]
        user_clearance_level=2,
    ).execute(
        _request(
            "fetch_articles",
            {"article_ids": ["law-a-article-27_2"]},
        ),
        cycle_no=2,
        timeout_sec=12.5,
    )

    assert execution.result.evidence_ids == ("law-a-article-27_2-paragraph-1",)
    assert execution.evidence[0].metadata["citationEligible"] is True
    assert execution.evidence[0].metadata["evidenceRole"] == "retrieved_text"


def test_fetch_articles_accepts_five_and_rejects_more_article_ids() -> None:
    tool = LegalFetchArticlesTool(
        FakeArticleSearch(),  # type: ignore[arg-type]
        user_clearance_level=2,
    )

    article_ids_schema = tool.definition.input_schema["properties"]["article_ids"]
    assert article_ids_schema["maxItems"] == 5

    with pytest.raises(ValueError, match="tool arguments violate schema"):
        tool.execute(
            _request(
                "fetch_articles",
                {
                    "article_ids": [
                        f"law-a-article-{index}" for index in range(1, 7)
                    ]
                },
            ),
            cycle_no=2,
            timeout_sec=12.5,
        )


def test_explicit_reference_schema_exposes_lookup_intent_not_physical_direction() -> None:
    explicit_reference = next(
        variant
        for variant in LegalGraphNeighborsTool.definition.input_schema["anyOf"]
        if variant["properties"]["mode"].get("const") == "explicit_reference"
    )
    properties = explicit_reference["properties"]
    description = properties["reference_lookup"]["description"]

    assert "follow_reference_in_text" in description
    assert "find_articles_referencing_this" in description
    assert "direction" not in properties


@pytest.mark.parametrize(
    ("reference_lookup", "expected_direction"),
    (
        ("follow_reference_in_text", "outgoing"),
        ("find_articles_referencing_this", "incoming"),
    ),
)
def test_explicit_reference_lookup_maps_mechanically_to_graph_direction(
    reference_lookup: str,
    expected_direction: str,
) -> None:
    graph = FakeGraph()

    LegalGraphNeighborsTool(
        graph,  # type: ignore[arg-type]
        user_clearance_level=2,
    ).execute(
        _request(
            "legal_graph_neighbors",
            {
                "article_ids": ["law-act-article-27_2"],
                "mode": "explicit_reference",
                "reference_lookup": reference_lookup,
            },
        ),
        cycle_no=2,
        timeout_sec=10,
    )

    assert graph.formal_args["direction"] == expected_direction


def test_graph_tool_returns_selected_semantic_relation_as_navigation_only() -> None:
    graph = FakeGraph()
    execution = LegalGraphNeighborsTool(
        graph,  # type: ignore[arg-type]
        user_clearance_level=2,
    ).execute(
        _request(
            "legal_graph_neighbors",
            {
                "article_ids": ["law-act-article-27_2"],
                "mode": "semantic_assertion",
                "predicate": "USES_DEFINITION",
                "direction": "from_subject",
            },
        ),
        cycle_no=2,
        timeout_sec=10,
    )

    assert graph.formal_args == {}
    assert graph.assertion_args["article_ids"] == ["law-act-article-27_2"]
    assert graph.assertion_args["proposed_predicate"] == "USES_DEFINITION"
    assert graph.assertion_args["direction"] == "from_subject"
    assert len(execution.evidence) == 1
    assert all(
        item.metadata["citationEligible"] is False for item in execution.evidence
    )
    assert "law-ordinance-article-2_5" in execution.evidence[0].content
    assert '"direction":"from_subject"' in execution.evidence[0].content
    assert '"edgeType":"USES_DEFINITION"' in execution.evidence[0].content
    assert "SUBJECTが使う対象株券等の範囲をOBJECTが定義する" in (
        execution.evidence[0].content
    )
    assert "対象株券等とは、次に掲げるものをいう" in execution.evidence[0].content


def test_graph_tool_keeps_an_independent_relation_window_for_each_seed() -> None:
    graph = MultiSeedGraph()
    execution = LegalGraphNeighborsTool(
        graph,  # type: ignore[arg-type]
        user_clearance_level=2,
    ).execute(
        _request(
            "legal_graph_neighbors",
            {
                "article_ids": [
                    "law-act-article-27_2",
                    "law-act-article-27_3",
                ],
                "mode": "explicit_reference",
                "reference_lookup": "find_articles_referencing_this",
            },
        ),
        cycle_no=2,
        timeout_sec=10,
    )

    assert graph.formal_calls == [
        ["law-act-article-27_2"],
        ["law-act-article-27_3"],
    ]
    assert graph.assertion_calls == []
    contents = [item.content for item in execution.evidence]
    assert any("law-ordinance-article-2_5" in content for content in contents)
    assert any("law-ordinance-article-10" in content for content in contents)


def test_graph_tool_collapses_duplicate_relations_into_one_article_pair() -> None:
    execution = LegalGraphNeighborsTool(
        DuplicatePairGraph(),  # type: ignore[arg-type]
        user_clearance_level=2,
    ).execute(
        _request(
            "legal_graph_neighbors",
            {
                "article_ids": ["law-act-article-27_2"],
                "mode": "explicit_reference",
                "reference_lookup": "find_articles_referencing_this",
            },
        ),
        cycle_no=2,
        timeout_sec=10,
    )

    assert len(execution.evidence) == 1
    content = execution.evidence[0].content
    assert '"neighborArticleId":"law-order-article-7"' in content
    assert content.count('"kind":"formal_relation"') == 2
    assert '"kind":"relation_assertion"' not in content
    assert execution.evidence[0].metadata["edgeTypes"] == ("REFERENCES",)


def test_graph_navigation_id_distinguishes_relation_content_for_same_pair() -> None:
    formal = {
        "graphEdgeId": "edge-reference",
        "edgeType": "REFERENCES",
        "fromArticleId": "law-order-article-7",
        "toArticleId": "law-act-article-27_2",
    }
    assertion = {
        "assertionId": "assertion-implements",
        "proposedPredicate": "IMPLEMENTS",
        "subjectArticleId": "law-act-article-27_2",
        "objectArticleId": "law-order-article-7",
    }

    formal_evidence = _graph_navigation_evidence(
        [formal],
        [],
        1,
        seed_article_ids=("law-act-article-27_2",),
        max_items=10,
    )[0]
    assertion_evidence = _graph_navigation_evidence(
        [],
        [assertion],
        1,
        seed_article_ids=("law-act-article-27_2",),
        max_items=10,
    )[0]

    assert formal_evidence.evidence_id != assertion_evidence.evidence_id
    assert formal_evidence.evidence_id.split(":", 1)[0] == (
        assertion_evidence.evidence_id.split(":", 1)[0]
    )
    assert formal_evidence.content != assertion_evidence.content


@pytest.mark.parametrize(
    "arguments",
    (
        {
            "article_ids": ["law-act-article-27_2"],
            "mode": "semantic_assertion",
            "direction": "from_subject",
        },
        {
            "article_ids": ["law-act-article-27_2"],
            "mode": "explicit_reference",
            "predicate": "IMPLEMENTS",
            "reference_lookup": "find_articles_referencing_this",
        },
        {
            "article_ids": ["law-act-article-27_2"],
            "mode": "semantic_assertion",
            "predicate": "IMPLEMENTS",
            "direction": "incoming",
        },
    ),
)
def test_graph_tool_rejects_mixed_or_incomplete_selector(
    arguments: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="tool arguments violate schema"):
        LegalGraphNeighborsTool(
            FakeGraph(),  # type: ignore[arg-type]
            user_clearance_level=2,
        ).execute(
            _request("legal_graph_neighbors", arguments),
            cycle_no=2,
            timeout_sec=10,
        )


def test_graph_navigation_evidence_cannot_be_cited() -> None:
    navigation = Evidence(
        evidence_id="graph-nav-1",
        source_ref="neo4j:edge-1",
        content='{"fromArticleId":"law-a","toArticleId":"law-b"}',
        created_cycle=1,
        metadata={"citationEligible": False},
    )
    state = CaseState(
        case_id="case-1",
        question="質問",
        evidence=(navigation,),
    )

    with pytest.raises(ContractViolation, match="navigation-only"):
        apply_solver_decision(
            state,
            SolverDecision(
                next="finalize",
                answer=FinalAnswer(
                    text="回答",
                    citation_ids=(navigation.evidence_id,),
                ),
            ),
            limits=AgentLimits(),
            known_tool_names={"legal_search"},
            material_evidence_ids={navigation.evidence_id},
            finalize_only=False,
        )


def test_legal_registry_exposes_graph_navigation_tool() -> None:
    registry = legal_tool_registry(
        FakeArticleSearch(),  # type: ignore[arg-type]
        FakeGraph(),  # type: ignore[arg-type]
        user_clearance_level=2,
    )

    assert registry.names == {
        "legal_search",
        "fetch_articles",
        "legal_graph_neighbors",
    }
