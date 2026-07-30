"""LLM主導法令調査の判断契約と証拠境界のテスト。"""

import json

import pytest
from pydantic import ValidationError

from app.config import settings
from app.llm import LLMClient, build_answer_prompt
from app.llm_directed_research import (
    RESEARCH_STATUS_CONTINUE,
    RESEARCH_STATUS_INSUFFICIENT,
    RESEARCH_STATUS_READY,
    TOOL_EXPAND_GRAPH,
    TOOL_FETCH_ARTICLES,
    TOOL_SEARCH_CORPUS,
    EvidenceCatalog,
    ResearchAction,
    ResearchAuthorityNode,
    ResearchClaimStructure,
    ResearchCheckpoint,
    ResearchEvidenceSelection,
    ResearchIssueStructure,
    ResearchLogicalStructure,
    ResearchTurn,
    ResearchUnresolvedItem,
    build_research_checkpoint_prompt,
    build_research_turn_prompt,
    parse_research_checkpoint,
    parse_research_turn,
    research_checkpoint_json_schema,
    research_turn_json_schema,
    sanitize_research_checkpoint,
    validate_research_checkpoint,
    validate_research_turn,
)
from app.models import AnswerRequest, Citation


def _law_result(
    content_unit_id: str = "law-a-article-12-paragraph-1",
) -> dict:
    return {
        "document": {
            "contentUnitId": content_unit_id,
            "articleContentUnitId": "law-a-article-12",
            "documentId": "law-a",
            "docType": "law",
            "title": "テスト法",
            "heading": "第十二条",
            "text": "許可を受けなければならない。",
        },
        "score": 0.9,
    }


class TestResearchAction:
    def test_search_requires_query(self) -> None:
        with pytest.raises(ValidationError):
            ResearchAction(
                tool=TOOL_SEARCH_CORPUS,
                query=None,
                articleIds=[],
                docTypes=["law"],
                edgeTypes=[],
            )

    def test_article_tools_require_article_ids(self) -> None:
        for tool in (TOOL_FETCH_ARTICLES, TOOL_EXPAND_GRAPH):
            with pytest.raises(ValidationError):
                ResearchAction(
                    tool=tool,
                    query=None,
                    articleIds=[],
                    docTypes=[],
                    edgeTypes=[],
                )


class TestEvidenceCatalog:
    def test_normalizes_search_results_and_deduplicates(self) -> None:
        catalog = EvidenceCatalog()

        assert catalog.add_results([_law_result()]) == 1
        assert catalog.add_results([_law_result()]) == 0
        assert catalog.content_unit_ids == (
            "law-a-article-12-paragraph-1",
        )
        assert catalog.known_article_ids == ("law-a-article-12",)

    def test_graph_paths_add_only_article_ids(self) -> None:
        catalog = EvidenceCatalog()
        added = catalog.add_graph_paths(
            [
                {
                    "nodes": [
                        {"graphNodeId": "law-a"},
                        {
                            "graphNodeId": "law-a-article-12",
                            "contentUnitId": "law-a-article-12",
                        },
                        {"graphNodeId": "guidance-a"},
                    ]
                }
            ]
        )

        assert added == 1
        assert catalog.known_article_ids == ("law-a-article-12",)

    def test_prompt_items_obey_item_and_character_budget(self) -> None:
        catalog = EvidenceCatalog()
        catalog.add_results(
            [
                _law_result("law-a-article-12-paragraph-1"),
                _law_result("law-a-article-12-paragraph-2"),
            ]
        )

        items = catalog.prompt_items(max_items=1, max_chars=200)

        assert len(items) == 1
        assert items[0]["contentUnitId"] == "law-a-article-12-paragraph-1"
        assert sum(len(str(value or "")) for value in items[0].values()) <= 200

    def test_prompt_items_put_previously_selected_evidence_first(self) -> None:
        catalog = EvidenceCatalog()
        catalog.add_results(
            [
                _law_result("law-a-article-12-paragraph-1"),
                _law_result("law-a-article-12-paragraph-2"),
            ]
        )

        items = catalog.prompt_items(
            max_items=1,
            max_chars=500,
            preferred_content_ids=("law-a-article-12-paragraph-2",),
        )

        assert [item["contentUnitId"] for item in items] == [
            "law-a-article-12-paragraph-2"
        ]

    def test_prompt_items_round_robin_across_documents(self) -> None:
        catalog = EvidenceCatalog()
        catalog.add_results(
            [
                _law_result("law-a-article-12-paragraph-1"),
                _law_result("law-a-article-12-paragraph-2"),
                {
                    "document": {
                        "contentUnitId": "law-b-article-3-paragraph-1",
                        "articleContentUnitId": "law-b-article-3",
                        "documentId": "law-b",
                        "docType": "law",
                        "title": "テスト施行令",
                        "heading": "第三条",
                        "text": "政令で定める基準。",
                    }
                },
            ]
        )

        items = catalog.prompt_items(max_items=2, max_chars=1000)
        inventory = catalog.prompt_inventory(max_items=2)

        assert [item["documentId"] for item in items] == ["law-a", "law-b"]
        assert [item["documentId"] for item in inventory] == ["law-a", "law-b"]

    def test_diversifies_content_ids_by_article_without_dropping_any(self) -> None:
        catalog = EvidenceCatalog()
        for article, paragraphs in (("12", 3), ("13", 2)):
            for paragraph in range(1, paragraphs + 1):
                catalog.add_results(
                    [
                        {
                            "document": {
                                **_law_result()["document"],
                                "contentUnitId": (
                                    f"law-a-article-{article}-paragraph-{paragraph}"
                                ),
                                "articleContentUnitId": f"law-a-article-{article}",
                            }
                        }
                    ]
                )

        diversified = catalog.diversify_content_ids(
            catalog.content_unit_ids
        )

        assert diversified == (
            "law-a-article-12-paragraph-1",
            "law-a-article-13-paragraph-1",
            "law-a-article-12-paragraph-2",
            "law-a-article-13-paragraph-2",
            "law-a-article-12-paragraph-3",
        )


class TestResearchTurnValidation:
    def test_ready_can_select_only_visible_evidence(self) -> None:
        catalog = EvidenceCatalog()
        catalog.add_results([_law_result()])
        turn = ResearchTurn(
            status=RESEARCH_STATUS_READY,
            actions=[],
            selectedEvidence=[
                ResearchEvidenceSelection(
                    contentUnitId="law-a-article-12-paragraph-1"
                )
            ],
        )

        validation = validate_research_turn(turn, catalog)

        assert validation.valid is True
        assert validation.selected_content_unit_ids == (
            "law-a-article-12-paragraph-1",
        )

    def test_unknown_evidence_and_article_ids_are_rejected(self) -> None:
        catalog = EvidenceCatalog()
        catalog.add_results([_law_result()])
        turn = ResearchTurn(
            status=RESEARCH_STATUS_CONTINUE,
            actions=[
                ResearchAction(
                    tool=TOOL_FETCH_ARTICLES,
                    query=None,
                    articleIds=["law-invented-article-99"],
                    docTypes=[],
                    edgeTypes=[],
                )
            ],
            selectedEvidence=[
                ResearchEvidenceSelection(contentUnitId="invented-content")
            ],
        )

        validation = validate_research_turn(turn, catalog)

        assert validation.valid is False
        assert validation.errors == (
            "unknown_evidence_id:invented-content",
            "unknown_article_id:actions[0]:law-invented-article-99",
        )

    def test_continue_requires_an_action(self) -> None:
        validation = validate_research_turn(
            ResearchTurn(status=RESEARCH_STATUS_CONTINUE),
            EvidenceCatalog(),
        )

        assert validation.valid is False
        assert validation.errors == ("continue_requires_action",)

    def test_known_article_can_be_fetched(self) -> None:
        catalog = EvidenceCatalog()
        catalog.add_results([_law_result()])
        turn = ResearchTurn(
            status=RESEARCH_STATUS_CONTINUE,
            actions=[
                ResearchAction(
                    tool=TOOL_FETCH_ARTICLES,
                    query=None,
                    articleIds=["law-a-article-12"],
                    docTypes=[],
                    edgeTypes=[],
                )
            ],
        )

        assert validate_research_turn(turn, catalog).valid is True

    def test_unknown_document_scope_is_rejected(self) -> None:
        catalog = EvidenceCatalog()
        catalog.add_documents({"law-a": "テスト法"})
        turn = ResearchTurn(
            status=RESEARCH_STATUS_CONTINUE,
            actions=[
                ResearchAction(
                    tool=TOOL_SEARCH_CORPUS,
                    query="許可",
                    documentIds=["law-invented"],
                    docTypes=["law"],
                )
            ],
        )

        validation = validate_research_turn(turn, catalog)

        assert validation.valid is False
        assert validation.errors == (
            "unknown_document_id:actions[0]:law-invented",
        )


def test_prompt_leaves_search_strategy_to_llm() -> None:
    catalog = EvidenceCatalog()
    catalog.add_documents({"law-a": "テスト法"})
    catalog.add_results([_law_result()])

    prompt = build_research_turn_prompt(
        question="許可に必要な根拠は何ですか",
        choices=None,
        catalog=catalog,
        tool_history=[],
        max_actions=4,
        max_evidence_items=40,
        evidence_chars=12000,
    )

    assert "調査方法、検索語、探索順序はあなたが判断してください" in prompt
    assert "selectedEvidenceには" in prompt
    assert "出力上限は4,096トークンです" in prompt
    assert "JSON全体を2,500トークン以内" in prompt
    assert "法的結論を支える確認済みの根拠ID" in prompt
    assert "IDを削る前に理由を短縮してください" in prompt
    assert "JSONを完全に閉じること" in prompt
    assert "readyを返す直前に" in prompt
    assert "自分が本文確認を必要と判断" in prompt
    assert "質問された全事項や考え得る全論点の網羅を要求するものではなく" in prompt
    assert "探索を無期限に続けない" in prompt
    assert '"documentId": "law-a"' in prompt
    assert "law-a-article-12-paragraph-1" in prompt


def test_finalization_prompt_requires_a_terminal_decision() -> None:
    catalog = EvidenceCatalog()
    catalog.add_documents({"law-a": "テスト法"})
    catalog.add_results([_law_result()])

    prompt = build_research_turn_prompt(
        question="許可に必要な根拠は何ですか",
        choices=None,
        catalog=catalog,
        tool_history=[],
        max_actions=0,
        max_evidence_items=40,
        evidence_chars=12000,
        remaining_turns=1,
        remaining_tool_calls=0,
        finalize_only=True,
    )
    schema = research_turn_json_schema(
        max_actions=0,
        max_selected_evidence=16,
        finalize_only=True,
    )

    assert "これは最終判断ターンです" in prompt
    assert "考え得る全ての例外" in prompt
    assert "selectedEvidenceは最大16件" in prompt
    assert schema["properties"]["status"]["enum"] == [
        RESEARCH_STATUS_READY,
        RESEARCH_STATUS_INSUFFICIENT,
    ]
    assert schema["properties"]["actions"]["maxItems"] == 0


def test_finalization_prompt_compacts_tool_history() -> None:
    catalog = EvidenceCatalog()
    catalog.add_documents({"law-a": "テスト法"})
    catalog.add_results([_law_result()])
    history = [
        {
            "turnIndex": 0,
            "decision": {
                "status": RESEARCH_STATUS_CONTINUE,
                "reason": "下位法令を確認する",
                "missingEvidence": ["具体的要件"],
                "selectedEvidence": [],
                "actions": [
                    {
                        "tool": TOOL_SEARCH_CORPUS,
                        "query": "最終ターンには不要な長い検索語",
                    }
                ],
            },
        },
        {
            "turnIndex": 1,
            "tool": TOOL_FETCH_ARTICLES,
            "articleIds": ["law-a-article-12"],
            "documentIds": [],
            "resultCount": 2,
            "newEvidenceCount": 1,
            "newArticleCount": 1,
            "autoGraphArticleIds": ["law-b-article-3"],
            "query": "履歴から落とす検索語",
            "reason": "履歴から落とす操作理由",
        },
    ]

    prompt = build_research_turn_prompt(
        question="許可に必要な根拠は何ですか",
        choices=None,
        catalog=catalog,
        tool_history=history,
        max_actions=0,
        max_evidence_items=32,
        evidence_chars=12000,
        finalize_only=True,
    )

    assert "最終ターンには不要な長い検索語" not in prompt
    assert "履歴から落とす検索語" not in prompt
    assert "履歴から落とす操作理由" not in prompt
    assert "下位法令を確認する" in prompt
    assert "law-b-article-3" in prompt


def test_checkpoint_rejects_unknown_evidence_and_next_article_ids() -> None:
    catalog = EvidenceCatalog()
    catalog.add_results([_law_result()])
    checkpoint = ResearchCheckpoint(
        status=RESEARCH_STATUS_CONTINUE,
        conclusion="許可規定を確認した。",
        evidenceIds=["unknown-content-unit"],
        nextQuestions=["省令の具体的要件"],
        nextArticleIds=["law-unknown-article-1"],
    )

    validation = validate_research_checkpoint(checkpoint, catalog)

    assert validation.valid is False
    assert validation.errors == (
        "unknown_evidence_id:unknown-content-unit",
        "unknown_next_article_id:law-unknown-article-1",
    )


def test_checkpoint_prompt_carries_state_and_reloads_exact_text() -> None:
    catalog = EvidenceCatalog()
    catalog.add_results([_law_result()])
    checkpoint = ResearchCheckpoint(
        status=RESEARCH_STATUS_CONTINUE,
        conclusion="許可規定を確認した。",
        evidenceIds=["law-a-article-12-paragraph-1"],
        nextQuestions=["省令の具体的要件"],
        nextArticleIds=["law-a-article-12"],
    )

    prompt = build_research_checkpoint_prompt(
        question="許可の要件は何ですか",
        choices=None,
        catalog=catalog,
        checkpoint=checkpoint,
        cycle_index=1,
        cycle_count=3,
        cycle_new_content_ids=(),
        tool_history=[{"query": "生の検索履歴は引き継がない"}],
        max_selected_evidence=16,
    )
    schema = research_checkpoint_json_schema(max_selected_evidence=16)

    assert "許可規定を確認した" in prompt
    assert "law-a-article-12-paragraph-1" in prompt
    assert "許可を受けなければならない" in prompt
    assert "生の検索履歴は引き継がない" not in prompt
    assert "status=readyを確定する直前に" in prompt
    assert "自分が本文確認を必要と判断" in prompt
    assert "質問された全事項の完全調査を要求するものではない" in prompt
    assert "無期限に継続せず" in prompt
    assert schema["required"] == [
        "status",
        "conclusion",
        "evidenceIds",
        "openEvidenceIds",
        "nextQuestions",
        "nextArticleIds",
        "logicalStructure",
    ]
    assert "findings" not in schema["properties"]
    assert schema["properties"]["evidenceIds"]["maxItems"] == 10
    assert schema["properties"]["nextArticleIds"]["maxItems"] == 10
    authority_node_schema = (
        schema["properties"]["logicalStructure"]["properties"]["issues"]
        ["items"]["properties"]["authorityNodes"]["items"]
    )
    assert authority_node_schema["properties"]["evidenceIds"]["maxItems"] == 20
    assert "issues" in schema["properties"]["logicalStructure"]["properties"]


def test_checkpoint_accepts_expanded_pending_articles_and_node_evidence() -> None:
    checkpoint = ResearchCheckpoint(
        status=RESEARCH_STATUS_CONTINUE,
        nextArticleIds=[
            f"law-a-article-{index}"
            for index in range(10)
        ],
        logicalStructure=ResearchLogicalStructure(
            issues=[
                ResearchIssueStructure(
                    issueId="many-evidence",
                    status="partial",
                    authorityNodes=[
                        ResearchAuthorityNode(
                            nodeId="node",
                            verificationStatus="text_verified",
                            evidenceIds=[
                                f"law-a-article-1-paragraph-{index}"
                                for index in range(20)
                            ],
                        )
                    ],
                )
            ]
        ),
    )

    assert len(checkpoint.nextArticleIds) == 10
    assert (
        len(
            checkpoint.logicalStructure.issues[0]
            .authorityNodes[0]
            .evidenceIds
        )
        == 20
    )
    with pytest.raises(ValidationError):
        ResearchCheckpoint(
            status=RESEARCH_STATUS_CONTINUE,
            nextArticleIds=[
                f"law-a-article-{index}"
                for index in range(11)
            ],
        )
    with pytest.raises(ValidationError):
        ResearchAuthorityNode(
            nodeId="too-many",
            verificationStatus="text_verified",
            evidenceIds=[
                f"law-a-article-1-paragraph-{index}"
                for index in range(21)
            ],
        )


def test_checkpoint_keeps_hierarchical_legal_structure() -> None:
    catalog = EvidenceCatalog()
    catalog.add_results([_law_result()])
    catalog.add_graph_paths(
        [
            {
                "nodes": [
                    {
                        "graphNodeId": "law-a-article-12",
                        "title": "テスト法",
                        "heading": "第十二条",
                    },
                    {
                        "graphNodeId": "law-b-article-3",
                        "title": "テスト法施行令",
                        "heading": "第三条",
                    },
                ],
                "edges": [
                    {
                        "edgeType": "IMPLEMENTS",
                        "relationSource": "verified_reference",
                        "relationConfidence": 0.98,
                    }
                ],
            }
        ]
    )
    checkpoint = ResearchCheckpoint(
        status=RESEARCH_STATUS_CONTINUE,
        conclusion="許可の原則は確認した。",
        evidenceIds=["law-a-article-12-paragraph-1"],
        nextQuestions=["施行令の具体的要件"],
        nextArticleIds=["law-b-article-3"],
        logicalStructure=ResearchLogicalStructure(
            issues=[
                ResearchIssueStructure(
                    issueId="permit",
                    question="許可が必要か",
                    status="partial",
                    authorityNodes=[
                        ResearchAuthorityNode(
                            nodeId="act-12",
                            articleId="law-a-article-12",
                            title="テスト法第十二条",
                            legalRole="直接根拠",
                            verificationStatus="text_verified",
                            evidenceIds=[
                                "law-a-article-12-paragraph-1"
                            ],
                        ),
                        ResearchAuthorityNode(
                            nodeId="order-3",
                            articleId="law-b-article-3",
                            title="テスト法施行令第三条",
                            legalRole="要件の具体化",
                            verificationStatus="text_not_fetched",
                            parentNodeId="act-12",
                            relationFromParent="IMPLEMENTS",
                            purpose="許可要件を具体化する",
                        ),
                    ],
                    claims=[
                        ResearchClaimStructure(
                            claimId="permit-required",
                            question="許可義務",
                            conclusion="原則として許可が必要",
                            status="partial",
                            authorityNodeIds=["act-12", "order-3"],
                        )
                    ],
                )
            ],
            unresolved=[
                ResearchUnresolvedItem(
                    issueId="permit",
                    claimId="permit-required",
                    articleId="law-b-article-3",
                    action="fetch_article",
                    reason="具体的要件の本文を確認する",
                    affectsCoreConclusion=True,
                )
            ],
        ),
    )

    validation = validate_research_checkpoint(checkpoint, catalog)
    prompt = build_research_turn_prompt(
        question="許可の要件は何ですか",
        choices=None,
        catalog=catalog,
        tool_history=[],
        max_actions=4,
        max_evidence_items=20,
        evidence_chars=8000,
        checkpoint=checkpoint,
        phase="explore",
        cycle_index=1,
        cycle_count=3,
    )

    assert validation.valid is True
    assert '"parentNodeId": "act-12"' in prompt
    assert '"relationFromParent": "IMPLEMENTS"' in prompt
    assert '"fromArticleId": "law-a-article-12"' in prompt
    assert '"toArticleId": "law-b-article-3"' in prompt


def test_next_cycle_prompt_does_not_reinject_unselected_evidence_or_graph() -> None:
    catalog = EvidenceCatalog()
    selected = _law_result()
    unrelated = {
        "document": {
            "contentUnitId": "law-c-article-8-paragraph-1",
            "articleContentUnitId": "law-c-article-8",
            "documentId": "law-c",
            "docType": "law",
            "title": "無関係法",
            "heading": "第八条",
            "text": "前サイクルで取得したが採用しなかった本文。",
        }
    }
    catalog.add_results([selected, unrelated])
    catalog.add_graph_paths(
        [
            {
                "nodes": [
                    {"graphNodeId": "law-a-article-12"},
                    {"graphNodeId": "law-b-article-3"},
                ],
                "edges": [{"edgeType": "IMPLEMENTS"}],
            },
            {
                "nodes": [
                    {"graphNodeId": "law-c-article-8"},
                    {"graphNodeId": "law-d-article-9"},
                ],
                "edges": [{"edgeType": "REFERENCES"}],
            },
        ]
    )
    checkpoint = ResearchCheckpoint(
        status=RESEARCH_STATUS_CONTINUE,
        conclusion="許可規定と施行令の関係を確認中。",
        evidenceIds=["law-a-article-12-paragraph-1"],
        nextArticleIds=["law-b-article-3"],
        logicalStructure=ResearchLogicalStructure(
            issues=[
                ResearchIssueStructure(
                    issueId="permit",
                    question="許可要件",
                    status="partial",
                    authorityNodes=[
                        ResearchAuthorityNode(
                            nodeId="act",
                            articleId="law-a-article-12",
                            verificationStatus="text_verified",
                            evidenceIds=[
                                "law-a-article-12-paragraph-1"
                            ],
                        ),
                        ResearchAuthorityNode(
                            nodeId="order",
                            articleId="law-b-article-3",
                            verificationStatus="text_not_fetched",
                            parentNodeId="act",
                            relationFromParent="IMPLEMENTS",
                        ),
                    ],
                )
            ]
        ),
    )

    prompt = build_research_turn_prompt(
        question="許可要件は何ですか",
        choices=None,
        catalog=catalog,
        tool_history=[],
        max_actions=4,
        max_evidence_items=20,
        evidence_chars=8000,
        preferred_content_ids=checkpoint.evidenceIds,
        checkpoint=checkpoint,
        phase="explore",
        cycle_index=1,
        cycle_count=3,
    )

    assert "許可を受けなければならない" in prompt
    assert "前サイクルで取得したが採用しなかった本文" not in prompt
    assert '"fromArticleId": "law-a-article-12"' in prompt
    assert '"toArticleId": "law-b-article-3"' in prompt
    assert '"fromArticleId": "law-c-article-8"' not in prompt
    assert '"toArticleId": "law-d-article-9"' not in prompt


def test_next_cycle_prompt_includes_open_and_already_fetched_next_article() -> None:
    catalog = EvidenceCatalog()
    catalog.add_results(
        [
            _law_result(),
            {
                "document": {
                    "contentUnitId": "law-b-article-3-paragraph-1",
                    "articleContentUnitId": "law-b-article-3",
                    "documentId": "law-b",
                    "docType": "law",
                    "title": "テスト施行令",
                    "heading": "第三条",
                    "text": "取得済みだが判断中の本文。",
                }
            },
            {
                "document": {
                    "contentUnitId": "law-c-article-4-paragraph-1",
                    "articleContentUnitId": "law-c-article-4",
                    "documentId": "law-c",
                    "docType": "law",
                    "title": "テスト府令",
                    "heading": "第四条",
                    "text": "未取得扱いだがカタログには存在する本文。",
                }
            },
        ]
    )
    checkpoint = ResearchCheckpoint(
        status=RESEARCH_STATUS_CONTINUE,
        conclusion="確認を継続する。",
        evidenceIds=["law-a-article-12-paragraph-1"],
        openEvidenceIds=["law-b-article-3-paragraph-1"],
        nextArticleIds=["law-c-article-4"],
    )

    prompt = build_research_turn_prompt(
        question="要件は何ですか",
        choices=None,
        catalog=catalog,
        tool_history=[],
        max_actions=4,
        max_evidence_items=20,
        evidence_chars=8000,
        checkpoint=checkpoint,
        phase="explore",
        cycle_index=1,
        cycle_count=3,
    )

    assert "許可を受けなければならない" in prompt
    assert "取得済みだが判断中の本文" in prompt
    assert "未取得扱いだがカタログには存在する本文" in prompt


def test_checkpoint_rejects_evidence_that_is_also_open() -> None:
    catalog = EvidenceCatalog()
    catalog.add_results([_law_result()])
    checkpoint = ResearchCheckpoint(
        status=RESEARCH_STATUS_CONTINUE,
        conclusion="確認中",
        evidenceIds=["law-a-article-12-paragraph-1"],
        openEvidenceIds=["law-a-article-12-paragraph-1"],
    )

    validation = validate_research_checkpoint(checkpoint, catalog)

    assert validation.valid is False
    assert (
        "evidence_also_open:law-a-article-12-paragraph-1"
        in validation.errors
    )


def test_current_cycle_graph_is_present_once_and_raw_history_is_compacted() -> None:
    catalog = EvidenceCatalog()
    relation = {
        "fromArticleId": "law-a-article-12",
        "edgeType": "IMPLEMENTS",
        "toArticleId": "law-b-article-3",
    }
    tool_history = [
        {
            "tool": TOOL_EXPAND_GRAPH,
            "articleIds": ["law-a-article-12"],
            "resultCount": 1,
            "graphRelations": [relation],
        }
    ]

    prompt = build_research_turn_prompt(
        question="許可要件は何ですか",
        choices=None,
        catalog=catalog,
        tool_history=tool_history,
        max_actions=4,
        max_evidence_items=20,
        evidence_chars=8000,
        checkpoint=ResearchCheckpoint(
            status=RESEARCH_STATUS_CONTINUE,
            conclusion="調査中",
        ),
        phase="deepen",
        cycle_index=0,
        cycle_count=3,
    )

    assert prompt.count('"fromArticleId": "law-a-article-12"') == 1
    assert '"graphRelations"' not in prompt
    assert '"graphRelationCount": 1' in prompt


def test_ready_checkpoint_rejects_unresolved_core_structure() -> None:
    catalog = EvidenceCatalog()
    catalog.add_results([_law_result()])
    checkpoint = ResearchCheckpoint(
        status=RESEARCH_STATUS_READY,
        conclusion="確認済み",
        evidenceIds=["law-a-article-12-paragraph-1"],
        logicalStructure=ResearchLogicalStructure(
            issues=[
                ResearchIssueStructure(
                    issueId="permit",
                    status="unresolved",
                )
            ],
            unresolved=[
                ResearchUnresolvedItem(
                    issueId="permit",
                    action="verify_text",
                    reason="中心条文が未確認",
                    affectsCoreConclusion=True,
                )
            ],
        ),
    )

    validation = validate_research_checkpoint(checkpoint, catalog)

    assert validation.valid is False
    assert "ready_has_unresolved_issue" in validation.errors
    assert "ready_has_unresolved_core_item" in validation.errors


def test_checkpoint_rejects_authority_hierarchy_cycle() -> None:
    catalog = EvidenceCatalog()
    catalog.add_results([_law_result()])
    checkpoint = ResearchCheckpoint(
        status=RESEARCH_STATUS_CONTINUE,
        logicalStructure=ResearchLogicalStructure(
            issues=[
                ResearchIssueStructure(
                    issueId="permit",
                    status="partial",
                    authorityNodes=[
                        ResearchAuthorityNode(
                            nodeId="a",
                            articleId="law-a-article-12",
                            verificationStatus="graph_verified",
                            parentNodeId="b",
                            relationFromParent="REFERENCES",
                        ),
                        ResearchAuthorityNode(
                            nodeId="b",
                            articleId="law-a-article-12",
                            verificationStatus="graph_verified",
                            parentNodeId="a",
                            relationFromParent="REFERENCES",
                        ),
                    ],
                    claims=[
                        ResearchClaimStructure(
                            claimId="cycle",
                            status="partial",
                            authorityNodeIds=["a", "b"],
                        )
                    ],
                )
            ]
        ),
    )

    validation = validate_research_checkpoint(checkpoint, catalog)

    assert validation.valid is False
    assert any(
        error.startswith("authority_hierarchy_cycle:")
        for error in validation.errors
    )


def test_checkpoint_allows_shared_authority_dag_across_claims() -> None:
    catalog = EvidenceCatalog()
    catalog.add_results([_law_result()])
    checkpoint = ResearchCheckpoint(
        status=RESEARCH_STATUS_CONTINUE,
        logicalStructure=ResearchLogicalStructure(
            issues=[
                ResearchIssueStructure(
                    issueId="permit",
                    status="verified",
                    authorityNodes=[
                        ResearchAuthorityNode(
                            nodeId="root",
                            articleId="law-a-article-12",
                            verificationStatus="text_verified",
                            evidenceIds=[
                                "law-a-article-12-paragraph-1"
                            ],
                        ),
                        ResearchAuthorityNode(
                            nodeId="detail",
                            articleId="law-a-article-12",
                            verificationStatus="graph_verified",
                            parentNodeId="root",
                            relationFromParent="IMPLEMENTS",
                        ),
                    ],
                    claims=[
                        ResearchClaimStructure(
                            claimId="principle",
                            status="verified",
                            authorityNodeIds=["root"],
                        ),
                        ResearchClaimStructure(
                            claimId="detail-claim",
                            status="verified",
                            authorityNodeIds=["root", "detail"],
                        ),
                    ],
                )
            ]
        ),
    )

    validation = validate_research_checkpoint(checkpoint, catalog)

    assert validation.valid is True


def test_checkpoint_rejects_unknown_shared_authority_reference() -> None:
    catalog = EvidenceCatalog()
    catalog.add_results([_law_result()])
    checkpoint = ResearchCheckpoint(
        status=RESEARCH_STATUS_CONTINUE,
        logicalStructure=ResearchLogicalStructure(
            issues=[
                ResearchIssueStructure(
                    issueId="permit",
                    status="partial",
                    claims=[
                        ResearchClaimStructure(
                            claimId="unknown",
                            status="partial",
                            authorityNodeIds=["missing-node"],
                        )
                    ],
                )
            ]
        ),
    )

    validation = validate_research_checkpoint(checkpoint, catalog)

    assert validation.valid is False
    assert (
        "unknown_claim_authority_node_id:permit:unknown:missing-node"
        in validation.errors
    )


def test_checkpoint_sanitization_keeps_verified_parts_and_drops_unknown_ids() -> None:
    catalog = EvidenceCatalog()
    catalog.add_results([_law_result()])
    checkpoint = ResearchCheckpoint(
        status=RESEARCH_STATUS_READY,
        evidenceIds=[
            "law-a-article-12-paragraph-1",
            "fabricated-evidence",
        ],
        nextArticleIds=[
            "law-a-article-12",
            "law-x-article-99",
        ],
        logicalStructure=ResearchLogicalStructure(
            issues=[
                ResearchIssueStructure(
                    issueId="permit",
                    status="verified",
                    authorityNodes=[
                        ResearchAuthorityNode(
                            nodeId="known",
                            articleId="law-a-article-12",
                            verificationStatus="text_verified",
                            evidenceIds=[
                                "law-a-article-12-paragraph-1"
                            ],
                        ),
                        ResearchAuthorityNode(
                            nodeId="unknown",
                            articleId="law-x-article-99",
                            verificationStatus="graph_verified",
                        ),
                    ],
                    claims=[
                        ResearchClaimStructure(
                            claimId="claim",
                            status="verified",
                            authorityNodeIds=["known", "unknown"],
                        )
                    ],
                )
            ],
            unresolved=[
                ResearchUnresolvedItem(
                    issueId="permit",
                    claimId="claim",
                    articleId="law-x-article-99",
                    action="fetch_article",
                    reason="未確認ID",
                )
            ],
        ),
    )

    sanitized, changes = sanitize_research_checkpoint(
        checkpoint,
        catalog,
    )

    assert sanitized.status == RESEARCH_STATUS_CONTINUE
    assert sanitized.evidenceIds == [
        "law-a-article-12-paragraph-1"
    ]
    assert sanitized.nextArticleIds == ["law-a-article-12"]
    assert [
        node.nodeId
        for node in sanitized.logicalStructure.issues[0].authorityNodes
    ] == ["known"]
    assert (
        sanitized.logicalStructure.issues[0]
        .claims[0]
        .authorityNodeIds
        == ["known"]
    )
    assert sanitized.logicalStructure.unresolved == []
    assert changes["removedAuthorityNodeIds"] == ["unknown"]
    assert validate_research_checkpoint(sanitized, catalog).valid is True


def test_issue_allows_five_claims_within_global_limit() -> None:
    issue = ResearchIssueStructure(
        issueId="many-claims",
        status="partial",
        claims=[
            ResearchClaimStructure(
                claimId=f"claim-{index}",
                status="partial",
            )
            for index in range(5)
        ],
    )
    schema = research_checkpoint_json_schema(max_selected_evidence=10)

    assert len(issue.claims) == 5
    assert (
        schema["properties"]["logicalStructure"]["properties"]["issues"]
        ["items"]["properties"]["claims"]["maxItems"]
        == 8
    )


def test_checkpoint_prompt_puts_stage_selected_evidence_before_other_new_items() -> None:
    catalog = EvidenceCatalog()
    first = _law_result("law-a-article-12-paragraph-1")
    selected = _law_result("law-b-article-9-paragraph-1")
    selected["document"].update(
        {
            "articleContentUnitId": "law-b-article-9",
            "documentId": "law-b",
            "title": "選択法",
            "heading": "第九条",
            "text": "統合で優先して読む本文。",
        }
    )
    catalog.add_results([first, selected])

    prompt = build_research_checkpoint_prompt(
        question="根拠は何ですか",
        choices=None,
        catalog=catalog,
        checkpoint=ResearchCheckpoint(
            status=RESEARCH_STATUS_CONTINUE,
            conclusion="",
        ),
        cycle_index=0,
        cycle_count=3,
        cycle_new_content_ids=[
            "law-a-article-12-paragraph-1",
            "law-b-article-9-paragraph-1",
        ],
        tool_history=[
            {
                "decision": {
                    "selectedEvidence": [
                        {
                            "contentUnitId": "law-b-article-9-paragraph-1",
                            "reason": "直接根拠",
                        }
                    ]
                }
            }
        ],
        max_selected_evidence=16,
    )
    new_evidence = prompt.split(
        "今回の直接取得・段階選択原文", 1
    )[1]

    assert new_evidence.index("law-b-article-9-paragraph-1") < (
        new_evidence.index("law-a-article-12-paragraph-1")
    )


def test_checkpoint_parser_enforces_selected_evidence_limit() -> None:
    raw = json.dumps(
        {
            "status": RESEARCH_STATUS_READY,
            "conclusion": "確認済み",
            "evidenceIds": ["law-a-1", "law-a-2"],
            "nextQuestions": [],
            "nextArticleIds": [],
        }
    )

    checkpoint, error = parse_research_checkpoint(
        raw,
        max_selected_evidence=1,
    )

    assert checkpoint is None
    assert "max_selected_evidence=1" in str(error)


def test_finalization_rejects_continue_even_with_an_action() -> None:
    catalog = EvidenceCatalog()
    catalog.add_results([_law_result()])
    turn = ResearchTurn(
        status=RESEARCH_STATUS_CONTINUE,
        actions=[
            ResearchAction(
                tool=TOOL_SEARCH_CORPUS,
                query="追加調査",
                docTypes=["law"],
            )
        ],
        selectedEvidence=[
            ResearchEvidenceSelection(
                contentUnitId="law-a-article-12-paragraph-1"
            )
        ],
    )

    validation = validate_research_turn(
        turn,
        catalog,
        finalize_only=True,
    )

    assert validation.valid is False
    assert "finalize_requires_terminal_status" in validation.errors
    assert "finalize_must_not_request_actions" in validation.errors


def test_parser_enforces_action_limit() -> None:
    action = {
        "tool": TOOL_SEARCH_CORPUS,
        "query": "許可",
        "articleIds": [],
        "docTypes": ["law"],
        "edgeTypes": [],
        "reason": "",
    }
    raw = json.dumps(
        {
            "status": RESEARCH_STATUS_CONTINUE,
            "actions": [action, action],
            "selectedEvidence": [],
            "missingEvidence": [],
            "reason": "",
        }
    )

    turn, error = parse_research_turn(
        raw,
        max_actions=1,
        max_selected_evidence=16,
    )

    assert turn is None
    assert "max_actions=1" in str(error)


def test_llm_client_returns_structured_research_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = json.dumps(
        {
            "status": RESEARCH_STATUS_CONTINUE,
            "actions": [
                {
                    "tool": TOOL_SEARCH_CORPUS,
                    "query": "製造販売業 許可",
                    "articleIds": [],
                    "docTypes": ["law"],
                    "edgeTypes": [],
                    "reason": "根拠条文を取得する",
                }
            ],
            "selectedEvidence": [],
            "missingEvidence": ["許可の根拠"],
            "reason": "まだ証拠がない",
        },
        ensure_ascii=False,
    )
    client = LLMClient()
    monkeypatch.setattr(
        client,
        "_json_transport",
        lambda *args, **kwargs: (raw, 12, 100, 50, "end_turn"),
    )
    monkeypatch.setattr(settings, "llm_research_max_actions_per_turn", 4)
    monkeypatch.setattr(settings, "llm_research_max_selected_evidence", 16)

    result = client.decide_legal_research_turn(
        AnswerRequest(question="医薬品の製造販売には何が必要ですか"),
        EvidenceCatalog(),
    )

    assert result.validationError is None
    assert result.turn is not None
    assert result.turn.actions[0].query == "製造販売業 許可"
    assert result.inputTokens == 100
    assert result.as_trace()["decision"]["status"] == RESEARCH_STATUS_CONTINUE


def test_llm_client_uses_separate_budget_for_checkpoint_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = json.dumps(
        {
            "status": RESEARCH_STATUS_CONTINUE,
            "conclusion": "許可規定を確認した。",
            "evidenceIds": [],
            "nextQuestions": ["具体的要件"],
            "nextArticleIds": [],
            "logicalStructure": {
                "issues": [],
                "unresolved": [],
            },
        },
        ensure_ascii=False,
    )
    captured: dict[str, object] = {}
    client = LLMClient()

    def fake_transport(*args, **kwargs):
        captured["maxTokens"] = args[3]
        captured["effort"] = kwargs.get("effort")
        return raw, 12, 100, 50, "end_turn"

    monkeypatch.setattr(client, "_json_transport", fake_transport)
    monkeypatch.setattr(
        settings,
        "llm_research_integration_max_tokens",
        8192,
    )
    monkeypatch.setattr(
        settings,
        "llm_research_integration_effort",
        "low",
    )

    result = client.integrate_legal_research_cycle(
        AnswerRequest(question="許可の根拠は何ですか"),
        EvidenceCatalog(),
        ResearchCheckpoint(status=RESEARCH_STATUS_CONTINUE),
        cycle_index=0,
        cycle_count=3,
        cycle_new_content_ids=(),
        tool_history=[],
        timeout_sec=30,
    )

    assert result.validationError is None
    assert result.checkpoint is not None
    assert captured == {"maxTokens": 8192, "effort": "low"}


def test_answer_prompt_requires_each_requested_matter_to_be_addressed() -> None:
    prompt = build_answer_prompt(
        AnswerRequest(question="要件と例外を説明してください"),
        ["llm_directed_research"],
        [
            Citation(
                documentId="law-a",
                contentUnitId="law-a-article-12-paragraph-1",
                text="許可を受けなければならない。",
            )
        ],
        research_context={
            "status": RESEARCH_STATUS_READY,
            "logicalStructure": {
                "issues": [
                    {
                        "issueId": "permit",
                        "claims": [
                            {
                                "claimId": "permit-required",
                                "conclusion": "許可が必要",
                            }
                        ],
                    }
                ]
            },
        },
    )

    assert "実際に質問された各事項へ一つずつ答えてください" in prompt
    assert "根拠を確認できない事項は推測せず未確認" in prompt
    assert "反復調査の法的論理構造" in prompt
    assert "permit-required" in prompt
