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
    ResearchHypothesis,
    ResearchIssueStructure,
    ResearchLogicalStructure,
    ResearchRelationDecision,
    ResearchTurn,
    ResearchUnresolvedItem,
    build_research_checkpoint_prompt,
    build_research_turn_prompt,
    hydrate_relation_decision_candidates,
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


def _core_hypothesis(
    *,
    status: str = "unverified",
    evidence_ids: list[str] | None = None,
) -> ResearchHypothesis:
    return ResearchHypothesis(
        hypothesisId="H-eligibility",
        statement="許可を受ける必要がある",
        status=status,
        evidenceIds=evidence_ids or [],
        missing=["義務の本則", "適用範囲"],
    )


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

    def test_relation_assertion_adds_both_articles_without_confirming_relation(self) -> None:
        catalog = EvidenceCatalog()
        added = catalog.add_relation_assertions(
            [
                {
                    "assertionId": "assertion-1",
                    "fromArticleId": "law-a-article-12",
                    "toArticleId": "law-b-article-3",
                    "suggestedType": "IMPLEMENTS",
                    "status": "unverified",
                }
            ]
        )
        assert added == 1
        assert set(catalog.known_article_ids) == {
            "law-a-article-12",
            "law-b-article-3",
        }
        assert catalog.prompt_graph_relations() == []
        assert catalog.prompt_relation_assertions()[0]["status"] == "unverified"

    def test_preclassified_relation_is_navigation_not_unverified_candidate(self) -> None:
        catalog = EvidenceCatalog()
        catalog.add_preclassified_relations(
            [
                {
                    "assertionId": "assertion-1",
                    "fromArticleId": "law-a-article-12",
                    "toArticleId": "law-b-article-3",
                    "status": "llm_classified_implements",
                    "classifierModel": "haiku-test",
                }
            ]
        )

        assert catalog.prompt_relation_assertions() == []
        relation = catalog.prompt_graph_relations()[0]
        assert relation["edgeType"] == "IMPLEMENTS"
        assert relation["relationSource"] == "offline_llm_classification"
        assert relation["assertionId"] == "assertion-1"

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

    def test_diversifies_preferred_content_across_documents_then_articles(self) -> None:
        catalog = EvidenceCatalog()
        content_ids = []
        for document_id in ("law-a", "law-b", "law-c"):
            for article in range(1, 4):
                content_id = f"{document_id}-article-{article}-paragraph-1"
                content_ids.append(content_id)
                catalog.add_results(
                    [
                        {
                            "document": {
                                "contentUnitId": content_id,
                                "articleContentUnitId": (
                                    f"{document_id}-article-{article}"
                                ),
                                "documentId": document_id,
                                "docType": "law",
                                "title": document_id,
                                "heading": f"第{article}条",
                                "text": "本文",
                            }
                        }
                    ]
                )

        diversified = catalog.diversify_content_ids_for_prompt(content_ids)

        assert [
            catalog.items_by_ids([content_id])[0]["documentId"]
            for content_id in diversified[:3]
        ] == ["law-a", "law-b", "law-c"]
        assert set(diversified) == set(content_ids)


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
            hypotheses=[_core_hypothesis()],
            actions=[
                ResearchAction(
                    tool=TOOL_FETCH_ARTICLES,
                    query=None,
                    articleIds=["law-invented-article-99"],
                    docTypes=[],
                    edgeTypes=[],
                    hypothesisIds=["H-eligibility"],
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
            hypotheses=[_core_hypothesis()],
            actions=[
                ResearchAction(
                    tool=TOOL_FETCH_ARTICLES,
                    query=None,
                    articleIds=["law-a-article-12"],
                    docTypes=[],
                    edgeTypes=[],
                    hypothesisIds=["H-eligibility"],
                )
            ],
        )

        assert validate_research_turn(turn, catalog).valid is True

    def test_unknown_document_scope_is_rejected(self) -> None:
        catalog = EvidenceCatalog()
        catalog.add_documents({"law-a": "テスト法"})
        turn = ResearchTurn(
            status=RESEARCH_STATUS_CONTINUE,
            hypotheses=[_core_hypothesis()],
            actions=[
                ResearchAction(
                    tool=TOOL_SEARCH_CORPUS,
                    query="許可",
                    documentIds=["law-invented"],
                    docTypes=["law"],
                    hypothesisIds=["H-eligibility"],
                )
            ],
        )

        validation = validate_research_turn(turn, catalog)

        assert validation.valid is False
        assert validation.errors == (
            "unknown_document_id:actions[0]:law-invented",
        )

    def test_action_requires_an_explicit_hypothesis(self) -> None:
        turn = ResearchTurn(
            status=RESEARCH_STATUS_CONTINUE,
            actions=[
                ResearchAction(
                    tool=TOOL_SEARCH_CORPUS,
                    query="許可",
                    docTypes=["law"],
                )
            ],
        )

        validation = validate_research_turn(turn, EvidenceCatalog())

        assert validation.valid is False
        assert validation.errors == ("actions_require_hypothesis",)


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
    assert "仮説検証の中心原則" in prompt
    assert "仮説を維持・修正・棄却・追加する" in prompt
    assert "質問の重要な特徴を説明できない" in prompt
    assert "プログラムが次の調査対象を推測すると期待しない" in prompt
    assert "IDの実在性" in prompt
    assert "禁止事項の検証" in prompt
    assert "selectedEvidenceには" in prompt
    assert "出力上限は4,096トークンです" in prompt
    assert "JSON全体を2,500トークン以内" in prompt
    assert "法的結論を支える確認済みの根拠ID" in prompt
    assert "IDを削る前に理由を短縮してください" in prompt
    assert "JSONを完全に閉じること" in prompt
    assert "readyを返す直前に" in prompt
    assert "自分が本文確認を必要と判断" in prompt
    assert "質問に明示されていない全論点の網羅を要求するものではない" in prompt
    assert "質問が明示した事項" in prompt
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


def test_research_turn_schema_limits_database_ids_to_catalog_values() -> None:
    schema = research_turn_json_schema(
        max_actions=4,
        max_selected_evidence=16,
        known_article_ids=["law-a-article-12", "law-b-article-3"],
        known_document_ids=["law-a", "law-b"],
        known_content_unit_ids=[
            "law-a-article-12-paragraph-1",
            "law-b-article-3-paragraph-1",
        ],
    )
    action = schema["properties"]["actions"]["items"]
    evidence = schema["properties"]["selectedEvidence"]["items"]

    assert action["properties"]["articleIds"]["items"]["enum"] == [
        "law-a-article-12",
        "law-b-article-3",
    ]
    assert action["properties"]["documentIds"]["items"]["enum"] == [
        "law-a",
        "law-b",
    ]
    assert evidence["properties"]["contentUnitId"]["enum"] == [
        "law-a-article-12-paragraph-1",
        "law-b-article-3-paragraph-1",
    ]


def test_checkpoint_schema_disallows_invented_article_and_evidence_ids() -> None:
    schema = research_checkpoint_json_schema(
        max_selected_evidence=10,
        known_article_ids=["law-a-article-12"],
        known_content_unit_ids=["law-a-article-12-paragraph-1"],
    )
    properties = schema["properties"]
    authority_node = (
        properties["logicalStructure"]["properties"]["issues"]["items"]
        ["properties"]["authorityNodes"]["items"]
    )
    unresolved = (
        properties["logicalStructure"]["properties"]["unresolved"]["items"]
    )

    assert properties["nextArticleIds"]["items"]["enum"] == [
        "law-a-article-12"
    ]
    assert authority_node["properties"]["articleId"]["enum"] == [
        "law-a-article-12",
        None,
    ]
    assert unresolved["properties"]["articleId"]["enum"] == [
        "law-a-article-12",
        None,
    ]
    assert properties["evidenceIds"]["items"]["enum"] == [
        "law-a-article-12-paragraph-1"
    ]


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


def test_relation_decision_requires_known_candidate_and_both_article_texts() -> None:
    catalog = EvidenceCatalog()
    catalog.add_results([_law_result()])
    catalog.add_relation_assertions(
        [
            {
                "assertionId": "assertion-1",
                "fromArticleId": "law-a-article-12",
                "toArticleId": "law-b-article-3",
                "suggestedType": "IMPLEMENTS",
            }
        ]
    )
    decision = ResearchRelationDecision(
        assertionId="assertion-1",
        verdict="confirmed",
        relationType="IMPLEMENTS",
        fromArticleId="law-a-article-12",
        toArticleId="law-b-article-3",
        evidenceIds=["law-a-article-12-paragraph-1"],
        reason="両条文が対応する",
    )
    checkpoint = ResearchCheckpoint(
        status=RESEARCH_STATUS_CONTINUE,
        logicalStructure=ResearchLogicalStructure(
            relationDecisions=[decision]
        ),
    )

    validation = validate_research_checkpoint(checkpoint, catalog)

    assert validation.valid is False
    assert validation.errors == (
        "relation_decision_requires_both_article_texts:assertion-1",
    )

    catalog.add_results(
        [
            {
                "document": {
                    "contentUnitId": "law-b-article-3-paragraph-1",
                    "articleContentUnitId": "law-b-article-3",
                    "documentId": "law-b",
                    "docType": "law",
                    "title": "テスト施行令",
                    "heading": "第三条",
                    "text": "具体的要件を定める。",
                }
            }
        ]
    )
    checkpoint.logicalStructure.relationDecisions[0].evidenceIds.append(
        "law-b-article-3-paragraph-1"
    )
    assert validate_research_checkpoint(checkpoint, catalog).valid is True


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
    assert "質問に明示されていない全論点の完全調査を要求するものではない" in prompt
    assert "質問が明示して求めた各事項を直接検証" in prompt
    assert "明示事項の根拠を落としてまで重複採用しない" in prompt
    assert "無期限に継続せず" in prompt
    assert "unresolved 6件" in prompt
    assert "relationDecisions 8件" in prompt
    assert "authorityNodesは合計20件" in prompt
    assert schema["required"] == [
        "status",
        "conclusion",
        "evidenceIds",
        "openEvidenceIds",
        "nextQuestions",
        "nextArticleIds",
        "logicalStructure",
    ]


def test_research_prompt_separates_unverified_relation_candidates() -> None:
    catalog = EvidenceCatalog()
    catalog.add_results([_law_result()])
    catalog.add_relation_assertions(
        [
            {
                "assertionId": "assertion-1",
                "fromArticleId": "law-a-article-12",
                "toArticleId": "law-b-article-3",
                "suggestedType": "IMPLEMENTS",
                "sourceText": "法第十二条に規定するもの",
            }
        ]
    )

    prompt = build_research_turn_prompt(
        question="具体化規定は何ですか",
        choices=None,
        catalog=catalog,
        tool_history=[],
        max_actions=2,
        max_evidence_items=10,
        evidence_chars=4000,
    )
    schema = research_checkpoint_json_schema(max_selected_evidence=16)

    assert "未確認のGraph関係候補" in prompt
    assert "assertion-1" in prompt
    assert "両端本文をfetch_articles" in prompt
    assert "findings" not in schema["properties"]
    assert schema["properties"]["evidenceIds"]["maxItems"] == 10
    assert schema["properties"]["nextArticleIds"]["maxItems"] == 10
    authority_node_schema = (
        schema["properties"]["logicalStructure"]["properties"]["issues"]
        ["items"]["properties"]["authorityNodes"]["items"]
    )
    assert authority_node_schema["properties"]["evidenceIds"]["maxItems"] == 20
    assert "issues" in schema["properties"]["logicalStructure"]["properties"]
    claim_schema = (
        schema["properties"]["logicalStructure"]["properties"]["issues"]
        ["items"]["properties"]["claims"]["items"]
    )
    assert claim_schema["properties"]["conclusion"]["maxLength"] == 300
    unresolved_schema = (
        schema["properties"]["logicalStructure"]["properties"]["unresolved"]
        ["items"]
    )
    assert unresolved_schema["properties"]["reason"]["maxLength"] == 180


def test_integration_prompt_assigns_relation_meaning_to_llm_with_both_texts() -> None:
    catalog = EvidenceCatalog()
    catalog.add_results([_law_result()])
    catalog.add_relation_assertions(
        [
            {
                "assertionId": "assertion-1",
                "fromArticleId": "law-a-article-12",
                "toArticleId": "law-b-article-3",
                "suggestedType": "IMPLEMENTS",
            }
        ]
    )

    prompt = build_research_checkpoint_prompt(
        question="具体化規定は何ですか",
        choices=None,
        catalog=catalog,
        checkpoint=ResearchCheckpoint(status=RESEARCH_STATUS_CONTINUE),
        cycle_index=0,
        cycle_count=3,
        cycle_new_content_ids=tuple(catalog.content_unit_ids),
        tool_history=[],
        max_selected_evidence=16,
    )

    assert "新しいrelationDecisionを作り" in prompt
    assert "両端Article本文の意味を比較して判断" in prompt
    assert "場合はuncertainとし" in prompt
    assert "場合はunverifiedとし" not in prompt
    assert "正式Graphエッジや他案件の法的事実へ昇格させない" in prompt

    relation_schema = research_checkpoint_json_schema(
        max_selected_evidence=16
    )["properties"]["logicalStructure"]["properties"]["relationDecisions"][
        "items"
    ]
    assert set(relation_schema["properties"]) == {
        "assertionId",
        "verdict",
        "evidenceIds",
        "reason",
    }


def test_final_cycle_checkpoint_schema_and_prompt_disallow_continue() -> None:
    catalog = EvidenceCatalog()
    prompt = build_research_checkpoint_prompt(
        question="許可の根拠は何ですか",
        choices=None,
        catalog=catalog,
        checkpoint=ResearchCheckpoint(status=RESEARCH_STATUS_CONTINUE),
        cycle_index=2,
        cycle_count=3,
        cycle_new_content_ids=(),
        tool_history=[],
        max_selected_evidence=16,
    )
    schema = research_checkpoint_json_schema(
        max_selected_evidence=16,
        final_cycle=True,
    )

    assert "status=continueは禁止" in prompt
    assert schema["properties"]["status"]["enum"] == [
        RESEARCH_STATUS_READY,
        RESEARCH_STATUS_INSUFFICIENT,
    ]


def test_prompt_evidence_marks_truncation_and_scope_validation_rejects_hidden_id() -> None:
    catalog = EvidenceCatalog()
    first = _law_result("law-a-article-12-paragraph-1")
    first["document"]["text"] = "長い本文" * 100
    catalog.add_results(
        [first, _law_result("law-a-article-12-paragraph-2")]
    )
    items = catalog.prompt_items(max_items=2, max_chars=150)

    assert items[0]["textTruncated"] is True
    assert items[0]["displayedTextChars"] < items[0]["originalTextChars"]
    turn = ResearchTurn(
        status=RESEARCH_STATUS_READY,
        selectedEvidence=[
            ResearchEvidenceSelection(
                contentUnitId="law-a-article-12-paragraph-2"
            )
        ],
    )
    validation = validate_research_turn(
        turn,
        catalog,
        allowed_content_unit_ids=["law-a-article-12-paragraph-1"],
    )

    assert validation.valid is False
    assert "unknown_evidence_id:law-a-article-12-paragraph-2" in validation.errors


def test_active_checkpoint_continue_requires_structured_follow_up() -> None:
    validation = validate_research_checkpoint(
        ResearchCheckpoint(status=RESEARCH_STATUS_CONTINUE),
        EvidenceCatalog(),
        require_structured_follow_up=True,
    )

    assert validation.valid is False
    assert "continue_requires_structured_follow_up" in validation.errors


def test_relation_decision_transport_hydrates_only_known_candidate_fields() -> None:
    catalog = EvidenceCatalog()
    catalog.add_relation_assertions(
        [
            {
                "assertionId": "assertion-1",
                "fromArticleId": "law-a-article-12",
                "toArticleId": "law-b-article-3",
                "suggestedType": "IMPLEMENTS",
            }
        ]
    )
    raw = json.dumps(
        {
            "logicalStructure": {
                "relationDecisions": [
                    {
                        "assertionId": "assertion-1",
                        "verdict": "confirmed",
                        "evidenceIds": ["evidence-a", "evidence-b"],
                        "reason": "両端本文が対応する",
                    },
                    {
                        "assertionId": "unknown",
                        "verdict": "confirmed",
                        "evidenceIds": [],
                        "reason": "未知候補",
                    },
                ]
            }
        },
        ensure_ascii=False,
    )

    hydrated = json.loads(hydrate_relation_decision_candidates(raw, catalog))
    decisions = hydrated["logicalStructure"]["relationDecisions"]

    assert len(decisions) == 1
    assert decisions[0]["relationType"] == "IMPLEMENTS"
    assert decisions[0]["fromArticleId"] == "law-a-article-12"
    assert decisions[0]["toArticleId"] == "law-b-article-3"


def test_research_prompt_does_not_reopen_case_decided_relation() -> None:
    catalog = EvidenceCatalog()
    catalog.add_results([_law_result()])
    catalog.add_relation_assertions(
        [
            {
                "assertionId": "assertion-decided",
                "fromArticleId": "law-a-article-12",
                "toArticleId": "law-b-article-3",
                "suggestedType": "IMPLEMENTS",
            }
        ]
    )

    prompt = build_research_turn_prompt(
        question="具体化規定は何ですか",
        choices=None,
        catalog=catalog,
        tool_history=[],
        max_actions=2,
        max_evidence_items=10,
        evidence_chars=4000,
        case_context={
            "relationDecisions": [
                {
                    "assertionId": "assertion-decided",
                    "verdict": "confirmed",
                }
            ]
        },
    )

    candidate_section = prompt.split(
        "未確認のGraph関係候補（必要なら両端本文を取得して統合段階で判断する）:\n",
        1,
    )[1].split("\n\n本文を確認できる証拠:", 1)[0]
    assert candidate_section == "[]"
    assert "assertion-decided" in prompt  # 案件状態の既存判断は引き続き見える


def test_relation_candidates_are_not_duplicated_from_case_view() -> None:
    catalog = EvidenceCatalog()
    catalog.add_results([_law_result()])
    candidate = {
        "assertionId": "assertion-once",
        "fromArticleId": "law-a-article-12",
        "toArticleId": "law-b-article-3",
        "suggestedType": "IMPLEMENTS",
    }
    catalog.add_relation_assertions([candidate])

    prompt = build_research_turn_prompt(
        question="具体化規定は何ですか",
        choices=None,
        catalog=catalog,
        tool_history=[],
        max_actions=2,
        max_evidence_items=10,
        evidence_chars=4000,
        case_context={"relationCandidates": [candidate]},
    )

    assert prompt.count("assertion-once") == 1


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


def test_checkpoint_cannot_drop_an_issue_from_the_previous_cycle() -> None:
    checkpoint = ResearchCheckpoint(
        status=RESEARCH_STATUS_CONTINUE,
        logicalStructure=ResearchLogicalStructure(
            issues=[
                ResearchIssueStructure(
                    issueId="requirements",
                    status="unresolved",
                )
            ]
        ),
    )

    validation = validate_research_checkpoint(
        checkpoint,
        EvidenceCatalog(),
        required_issue_ids=("requirements", "procedure"),
    )

    assert validation.valid is False
    assert "missing_previous_issue_id:procedure" in validation.errors


def test_ready_checkpoint_selects_evidence_for_every_retained_issue() -> None:
    first_id = "law-a-article-12-paragraph-1"
    second_id = "law-a-article-12-paragraph-2"
    catalog = EvidenceCatalog()
    catalog.add_results([_law_result(first_id), _law_result(second_id)])
    checkpoint = ResearchCheckpoint(
        status=RESEARCH_STATUS_READY,
        evidenceIds=[first_id],
        logicalStructure=ResearchLogicalStructure(
            issues=[
                ResearchIssueStructure(
                    issueId="requirements",
                    status="verified",
                    authorityNodes=[
                        ResearchAuthorityNode(
                            nodeId="requirements-law",
                            articleId="law-a-article-12",
                            verificationStatus="text_verified",
                            evidenceIds=[first_id],
                        )
                    ],
                ),
                ResearchIssueStructure(
                    issueId="procedure",
                    status="verified",
                    authorityNodes=[
                        ResearchAuthorityNode(
                            nodeId="procedure-law",
                            articleId="law-a-article-12",
                            verificationStatus="text_verified",
                            evidenceIds=[second_id],
                        )
                    ],
                ),
            ]
        ),
    )

    validation = validate_research_checkpoint(checkpoint, catalog)

    assert validation.valid is False
    assert (
        "ready_issue_requires_selected_evidence:procedure"
        in validation.errors
    )


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


def test_next_cycle_prompt_does_not_repeat_visible_evidence_in_inventory() -> None:
    catalog = EvidenceCatalog()
    catalog.add_results([_law_result()])
    checkpoint = ResearchCheckpoint(
        status=RESEARCH_STATUS_CONTINUE,
        conclusion="確認を継続する。",
        evidenceIds=["law-a-article-12-paragraph-1"],
    )

    prompt = build_research_turn_prompt(
        question="許可要件は何ですか",
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

    inventory_block = prompt.split(
        "候補一覧（本文が省略された候補はArticle IDをfetch_articlesして確認できる）:\n",
        1,
    )[1].split("\n\nGraph・索引時分類済み", 1)[0]
    assert inventory_block == "[]"
    assert "許可を受けなければならない" in prompt


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
    assert "本文が実際に定めているかで検証" in prompt
    assert "反証、適用範囲の不一致" in prompt
    assert "同じ仮説の周辺検索を続けず" in prompt


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
    assert "ready_has_unresolved_core_item" in validation.errors


def test_sanitizer_downgrades_ready_with_core_gap_without_losing_next_tasks() -> None:
    catalog = EvidenceCatalog()
    catalog.add_results([_law_result()])
    catalog.add_graph_paths(
        [
            {
                "nodes": [
                    {"graphNodeId": "law-a-article-12"},
                    {"graphNodeId": "law-b-article-3"},
                ],
                "edges": [{"edgeType": "IMPLEMENTS"}],
            }
        ]
    )
    checkpoint = ResearchCheckpoint(
        status=RESEARCH_STATUS_READY,
        conclusion="本則は確認したが配下法令は未確認",
        evidenceIds=["law-a-article-12-paragraph-1"],
        nextArticleIds=["law-b-article-3"],
        logicalStructure=ResearchLogicalStructure(
            issues=[
                ResearchIssueStructure(
                    issueId="permit",
                    status="partial",
                )
            ],
            unresolved=[
                ResearchUnresolvedItem(
                    issueId="permit",
                    articleId="law-b-article-3",
                    action="fetch_article",
                    reason="配下法令本文が未確認",
                    affectsCoreConclusion=True,
                )
            ],
        ),
    )

    sanitized, changes = sanitize_research_checkpoint(checkpoint, catalog)

    assert sanitized.status == RESEARCH_STATUS_CONTINUE
    assert sanitized.nextArticleIds == ["law-b-article-3"]
    assert changes["downgradedReadyStatus"] == [
        "unresolved_core_evidence"
    ]
    assert validate_research_checkpoint(sanitized, catalog).valid is True


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
    captured: dict[str, object] = {}

    def fake_transport(*args, **kwargs):
        captured["model"] = args[2]
        return raw, 12, 100, 50, "end_turn"

    monkeypatch.setattr(client, "_json_transport", fake_transport)
    monkeypatch.setattr(
        settings,
        "llm_research_stage_model",
        "stage-model-test",
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
    assert captured["model"] == "stage-model-test"
    assert result.model == "stage-model-test"
    assert result.as_trace()["decision"]["status"] == RESEARCH_STATUS_CONTINUE


def test_research_turn_requires_actions_to_reference_current_hypothesis() -> None:
    catalog = EvidenceCatalog()
    catalog.add_documents({"law-a": "テスト法"})
    hypothesis = _core_hypothesis()
    valid_turn = ResearchTurn(
        status=RESEARCH_STATUS_CONTINUE,
        hypotheses=[hypothesis],
        actions=[
            ResearchAction(
                tool=TOOL_SEARCH_CORPUS,
                query="許可の本則",
                documentIds=["law-a"],
                hypothesisIds=[hypothesis.hypothesisId],
            )
        ],
    )
    invalid_turn = valid_turn.model_copy(
        update={
            "actions": [
                valid_turn.actions[0].model_copy(
                    update={"hypothesisIds": ["H-unknown"]}
                )
            ]
        }
    )

    assert validate_research_turn(valid_turn, catalog).valid is True
    validation = validate_research_turn(invalid_turn, catalog)
    assert validation.valid is False
    assert (
        "unknown_action_hypothesis_id:actions[0]:H-unknown"
        in validation.errors
    )


def test_research_schemas_expose_hypothesis_and_verification_contract() -> None:
    turn_schema = research_turn_json_schema(
        max_actions=4,
        max_selected_evidence=16,
    )
    checkpoint_schema = research_checkpoint_json_schema(
        max_selected_evidence=16,
    )

    assert "hypotheses" in turn_schema["required"]
    assert turn_schema["properties"]["hypotheses"]["minItems"] == 1
    action = turn_schema["properties"]["actions"]["items"]
    assert "hypothesisIds" in action["required"]
    assert action["properties"]["hypothesisIds"]["minItems"] == 1
    logical = checkpoint_schema["properties"]["logicalStructure"]
    assert "hypotheses" in logical["required"]
    assert logical["properties"]["hypotheses"]["minItems"] == 1
    hypothesis = logical["properties"]["hypotheses"]["items"]
    assert hypothesis["required"] == [
        "hypothesisId",
        "statement",
        "status",
        "evidenceIds",
        "missing",
    ]


def test_ready_checkpoint_does_not_reclassify_unverified_hypothesis() -> None:
    catalog = EvidenceCatalog()
    catalog.add_results([_law_result()])
    checkpoint = ResearchCheckpoint(
        status=RESEARCH_STATUS_READY,
        evidenceIds=["law-a-article-12-paragraph-1"],
        logicalStructure=ResearchLogicalStructure(
            hypotheses=[_core_hypothesis(status="unverified")]
        ),
    )

    validation = validate_research_checkpoint(checkpoint, catalog)

    assert validation.valid is True
    assert "ready_has_unverified_hypothesis" not in validation.errors


def test_legacy_nested_hypothesis_is_not_semantically_reclassified() -> None:
    with pytest.raises(ValidationError):
        ResearchHypothesis.model_validate(
            {
                "hypothesisId": "H-old",
                "statement": "許可が必要である",
                "status": "unresolved",
                "verification": {
                    "result": "insufficient",
                    "evidenceIds": ["law-a-article-12-paragraph-1"],
                },
            }
        )


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
        captured["model"] = args[2]
        captured["maxTokens"] = args[3]
        captured["effort"] = kwargs.get("effort")
        captured["schema"] = args[1]
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
    monkeypatch.setattr(
        settings,
        "llm_research_integration_model",
        "integration-model-test",
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
    assert captured["maxTokens"] == 8192
    assert captured["model"] == "integration-model-test"
    assert result.model == "integration-model-test"
    assert captured["effort"] == "low"
    schema = captured["schema"]
    assert isinstance(schema, dict)
    assert "enum" not in schema["properties"]["nextArticleIds"]["items"]
    assert "enum" not in schema["properties"]["evidenceIds"]["items"]


def test_checkpoint_integration_retries_unknown_structure_id_without_coercion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = EvidenceCatalog()
    catalog.add_results([_law_result()])

    def payload(article_id: str) -> str:
        return json.dumps(
            {
                "status": RESEARCH_STATUS_READY,
                "conclusion": "許可が必要である。",
                "evidenceIds": ["law-a-article-12-paragraph-1"],
                "nextQuestions": [],
                "nextArticleIds": [],
                "logicalStructure": {
                    "issues": [
                        {
                            "issueId": "permit",
                            "question": "許可が必要か",
                            "status": "verified",
                            "authorityNodes": [
                                {
                                    "nodeId": "N-1",
                                    "articleId": article_id,
                                    "title": "テスト法第十二条",
                                    "legalRole": "本則",
                                    "verificationStatus": "text_verified",
                                    "evidenceIds": [
                                        "law-a-article-12-paragraph-1"
                                    ],
                                    "parentNodeId": None,
                                    "relationFromParent": "",
                                    "purpose": "許可義務の確認",
                                }
                            ],
                            "claims": [],
                        }
                    ],
                    "hypotheses": [],
                    "unresolved": [],
                    "relationDecisions": [],
                },
            },
            ensure_ascii=False,
        )

    responses = [
        payload("law-a-article-12-paragraph-1"),
        payload("law-a-article-12"),
    ]
    prompts: list[str] = []
    client = LLMClient()

    def fake_transport(prompt, *args, **kwargs):
        prompts.append(prompt)
        return responses.pop(0), 10, 100, 50, "end_turn"

    monkeypatch.setattr(client, "_json_transport", fake_transport)

    result = client.integrate_legal_research_cycle(
        AnswerRequest(question="許可の根拠は何ですか"),
        catalog,
        ResearchCheckpoint(status=RESEARCH_STATUS_CONTINUE),
        cycle_index=0,
        cycle_count=3,
        cycle_new_content_ids=("law-a-article-12-paragraph-1",),
        tool_history=[],
        timeout_sec=60,
    )

    assert result.validationError is None
    assert result.retryCount == 1
    assert result.checkpoint is not None
    node = result.checkpoint.logicalStructure.issues[0].authorityNodes[0]
    assert node.articleId == "law-a-article-12"
    assert "unknown_structure_article_id" in prompts[1]
    assert "プログラムは法的判断や次動作を推測して補正しません" in prompts[1]


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
            "answerContract": {
                "issues": [
                    {
                        "issueId": "permit",
                        "question": "許可の要件と例外は何か",
                    }
                ],
                "availableCitationIds": [
                    "law-a-article-12-paragraph-1"
                ],
            },
        },
    )

    assert "各issueIdのquestionと引用本文を読み" in prompt
    assert "根拠を確認できない論点は推測せず" in prompt
    assert "Main・Reviewer共有回答契約" in prompt
    assert "許可の要件と例外は何か" in prompt
    assert "反復調査の法的論理構造" not in prompt
