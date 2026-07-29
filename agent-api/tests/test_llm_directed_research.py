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
    ResearchEvidenceSelection,
    ResearchTurn,
    build_research_turn_prompt,
    parse_research_turn,
    research_turn_json_schema,
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
        research_context={"status": RESEARCH_STATUS_READY},
    )

    assert "実際に質問された各事項へ一つずつ答えてください" in prompt
    assert "根拠を確認できない事項は推測せず未確認" in prompt
