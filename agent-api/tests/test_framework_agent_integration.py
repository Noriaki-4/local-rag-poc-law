"""新Frameworkから法令Tool・Model・API応答までの薄い縦切り。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app import main
from app.adapters.models import StructuredJSONModelAdapter
from app.adapters.models.structured_json import _solver_prompt
from app.agent_framework.context import (
    ContextCapacityExceeded,
    SolverContractFeedback,
    build_solver_context,
)
from app.agent_framework.contracts import SolverDecision
from app.agent_framework.profiles import AgentLimits, ModelCallProfile
from app.agent_framework.state import CaseState
from app.domains.legal import profiles as legal_profiles
from app.framework_agent import LegalFrameworkAgentService
from app.llm import StructuredJSONResult
from app.models import AnswerRequest, AnswerResponse


class FakeStructuredLLM:
    provider = "fake"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.payloads: list[dict[str, Any]] = [
            {
                "next": "continue",
                "update": {
                    "add_work_items": [
                        {
                            "work_item_id": "w1",
                            "question": "根拠条文を確認する",
                        }
                    ],
                    "add_hypotheses": [
                        {
                            "hypothesis_id": "h1",
                            "work_item_id": "w1",
                            "statement": "要件が条文に定められている",
                        }
                    ],
                },
                "next_focus_work_item_ids": ["w1"],
                "tool_requests": [
                    {
                        "request_id": "r1",
                        "work_item_id": "w1",
                        "tool_name": "legal_search",
                        "arguments": ('{"query":"検証法 要件","doc_types":["law"]}'),
                        "purpose": "根拠条文を検索する",
                        "hypothesis_ids": ["h1"],
                    }
                ],
            },
            {
                "next": "continue",
                "update": {
                    "update_hypotheses": [
                        {
                            "hypothesis_id": "h1",
                            "judgment": "unresolved",
                            "gaps": ["Article全体の確認が必要"],
                        }
                    ]
                },
                "next_focus_work_item_ids": ["w1"],
                "tool_requests": [
                    {
                        "request_id": "r2",
                        "work_item_id": "w1",
                        "tool_name": "fetch_articles",
                        "arguments": {"article_ids": ["law-test-article-2"]},
                        "purpose": "検索で発見したArticle本文を取得する",
                        "hypothesis_ids": ["h1"],
                    }
                ],
            },
            {
                "next": "finalize",
                "update": {
                    "update_work_items": [
                        {
                            "work_item_id": "w1",
                            "state": "resolved",
                            "resolution": "本文を確認した",
                        }
                    ],
                    "update_hypotheses": [
                        {
                            "hypothesis_id": "h1",
                            "judgment": "supported",
                            "evidence_ids": ["law-test-article-2"],
                        }
                    ],
                },
                "answer": {
                    "text": "検証法第2条が要件を定めています。",
                    "citation_ids": ["law-test-article-2"],
                },
            },
        ]

    def generate_structured_json(self, **kwargs: Any) -> StructuredJSONResult:
        self.calls.append(kwargs)
        decision = self.payloads.pop(0)
        return StructuredJSONResult(
            payload={
                "next": decision["next"],
                "update_json": json.dumps(
                    decision.get("update", {}),
                    ensure_ascii=False,
                ),
                "next_focus_work_item_ids": decision.get(
                    "next_focus_work_item_ids", []
                ),
                "retain_evidence_ids": decision.get("retain_evidence_ids", []),
                "tool_requests_json": json.dumps(
                    decision.get("tool_requests", []),
                    ensure_ascii=False,
                ),
                "dependency_decisions": decision.get("dependency_decisions", []),
                "graph_candidate_review": decision.get("graph_candidate_review"),
                "answer": decision.get("answer"),
            },
            provider="fake",
            model=kwargs["model"],
            latencyMs=1,
            inputTokens=10,
            outputTokens=20,
        )


class FakeOpenSearch:
    def search(self, *args: Any) -> list[dict[str, Any]]:
        return [
            {
                "document": {
                    "contentUnitId": "law-test-article-2",
                    "articleContentUnitId": "law-test-article-2",
                    "documentId": "law-test",
                    "docType": "law",
                    "title": "検証法",
                    "heading": "第二条",
                    "text": "第二条 要件を定める。",
                }
            }
        ]

    def get_by_article_ids(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "contentUnitId": "law-test-article-2",
                "articleContentUnitId": "law-test-article-2",
                "documentId": "law-test",
                "docType": "law",
                "title": "検証法",
                "heading": "第二条",
                "text": "第二条 要件を定める。",
            }
        ]


class FakeGraph:
    pass


def test_new_framework_uses_legal_tool_and_skips_reviewer_by_default(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        legal_profiles.settings,
        "agent_framework_reviewer_enabled",
        False,
    )
    llm = FakeStructuredLLM()
    service = LegalFrameworkAgentService(
        FakeOpenSearch(),
        FakeGraph(),
        llm,
    )

    response = service.answer(
        AnswerRequest(
            question="検証法の要件は何ですか",
            pattern="pattern_4_deepsearch",
        )
    )

    framework_trace = response.trace["agentFramework"]
    assert response.pattern == "agent_framework_v1"
    assert response.answer == "検証法第2条が要件を定めています。"
    assert response.citations[0].contentUnitId == "law-test-article-2"
    assert framework_trace["reviewerEnabled"] is False
    assert "answerStatus" not in framework_trace
    assert framework_trace["researchCycleCount"] == 1
    assert len(framework_trace["modelCalls"]) == 3
    assert [item["purpose"] for item in framework_trace["modelCalls"]] == [
        "research",
        "integration",
        "integration",
    ]
    assert framework_trace["toolCalls"][0]["arguments"] == {
        "query": "検証法 要件",
        "doc_types": ["law"],
    }
    assert framework_trace["toolCalls"][0]["purpose"] == "根拠条文を検索する"
    assert [item["tool_name"] for item in framework_trace["toolCalls"]] == [
        "legal_search",
        "fetch_articles",
        "legal_graph_neighbors",
    ]
    assert framework_trace["toolCalls"][2]["arguments"] == {
        "article_ids": ["law-test-article-2"],
        "edge_types": ["REFERENCES", "IMPLEMENTS", "APPLIED_BY"],
        "max_relations": 50,
    }
    assert len(llm.calls) == 3
    assert "decision_json" not in llm.calls[0]["schema"]["properties"]
    assert "next" in llm.calls[0]["schema"]["properties"]
    assert "dependency_decisions" in llm.calls[0]["schema"]["properties"]
    assert "dependency_decisions_json" not in llm.calls[0]["schema"]["properties"]
    initial_dependency_schema = llm.calls[0]["schema"]["properties"][
        "dependency_decisions"
    ]
    assert initial_dependency_schema["type"] == "array"
    assert initial_dependency_schema["minItems"] == 0
    assert initial_dependency_schema["maxItems"] == 0
    assert set(initial_dependency_schema["items"]["required"]) == {
        "dependency_kind",
        "work_item_id",
        "status",
        "reason",
        "source_evidence_ids",
        "action",
        "action_request_id",
        "target_article_ids",
        "evidence_ids",
    }
    assert llm.calls[1]["schema"]["properties"]["dependency_decisions"][
        "maxItems"
    ] == 0
    assert framework_trace["dependencyDecisions"] == []


def test_framework_reviewer_setting_defaults_to_false() -> None:
    from app.config import settings

    assert settings.agent_framework_reviewer_enabled is False


def test_all_solver_stages_include_shared_legal_research_rules() -> None:
    profile = legal_profiles.legal_agent_profile()
    assert profile.version == "38"
    prompts = (
        profile.solver_research.system_prompt,
        profile.solver_integration.system_prompt,
    )

    for prompt in prompts:
        assert prompt.count("全サイクル共通の規則") == 1
        assert "各サイクルで元の質問へ戻り" in prompt
        assert "別法令との関係や条文番号を推測して作業へ加えません" in prompt
        assert "委任先本文を未確認のまま当該観点をsupportedまたはresolvedにしません" in prompt
        assert "required_graph_review_request_idsが空でなければ" in prompt
        assert "material_included=falseのEvidenceは本文未提示" in prompt
        assert "graph_candidate_catalogには、Neo4jから取得済みのGraph候補" in prompt
        assert "articlesはGraphの両端ArticleをArticle IDごと" in prompt
        assert "linksはseed_article_idからcandidate_article_id" in prompt
        assert "content_statusはプログラムが管理する本文取得状態" in prompt
        assert "表示順や末尾にあることを理由に候補を無視せず" in prompt
        assert "本文中の条番号、法令番号、documentIdを組み合わせ" in prompt
        assert "各fetch_articles.arguments.article_idsをfetchable_article_ids" in prompt
        assert "その判断はfinalizeと矛盾する" in prompt
        assert "fetch_articlesは1回に最大4個のArticle ID" in prompt
        assert "4個は上限であり目標ではない" in prompt
        assert "WorkItemや取得目的が異なっても4個以内なら1つ" in prompt
        assert "Graph探索の上限は1ホップ" in prompt
        assert "Graph候補Articleには伴いません" in prompt
        assert "Graphを再展開しません" in prompt
        assert "[一致箇所N]" in prompt
        assert "IMPLEMENTSはfromが親規定、toが具体化規定" in prompt
        assert "outgoingは起点Articleがfrom側" in prompt
        assert "llm_classified_implementsが別処理のLLMによる具体化関係判定" in prompt
        assert "いずれのstatusも正式関係への昇格を意味せず" in prompt
        assert "parent_law_referenceが下位法令本文から親法律・親政令への明示参照" in prompt
        assert "生成元・監査用の来歴はCaseStateに保持" in prompt
        assert "回答に影響し得ると判断した隣接Article" in prompt
        assert "GraphをToolRequestへ指定しません" in prompt
        assert "search_navigationの検索結果は、次のlegal_searchまたはfetch_articles" in prompt
        assert "その本文抜粋をHypothesisのjudgment" in prompt
        assert "1つのHypothesisは、取得本文で独立に検証できる1つの命題" in prompt
        assert "特定条文の内容を説明する場合" in prompt
    assert "初回判断では" in profile.solver_research.system_prompt
    assert "WorkItem数を減らすために" in profile.solver_research.system_prompt
    assert "直前の全ToolResultとmaterial_evidenceを読み" in (
        profile.solver_integration.system_prompt
    )
    assert "起点Articleのfetch_articlesにだけ1ホップGraph取得" in (
        profile.solver_integration.system_prompt
    )
    assert "そのArticle IDがfetchable_article_idsにない場合" in (
        profile.solver_integration.system_prompt
    )
    assert "同じDecisionの既知ArticleはWorkItemごとに分けず" in (
        profile.solver_integration.system_prompt
    )
    assert "終了整合監査" in profile.solver_integration.system_prompt
    assert "各法的主張に直接対応するgrounding Evidence" in (
        profile.solver_integration.system_prompt
    )
    assert "目的条項・総則条項" in profile.solver_integration.system_prompt
    assert "委任元の具体的な文言" in profile.solver_integration.system_prompt
    assert profile.automatic_tools[0].trigger_tool_name == "fetch_articles"
    assert profile.automatic_tools[0].tool_name == "legal_graph_neighbors"
    assert (
        profile.automatic_tools[0].one_hop_candidate_metadata_key
        == "neighborArticleId"
    )
    assert profile.automatic_tools[0].solver_may_request is False
    assert len(profile.tool_list_argument_limits) == 1
    assert profile.tool_list_argument_limits[0].tool_name == "fetch_articles"
    assert profile.tool_list_argument_limits[0].argument_name == "article_ids"
    assert profile.tool_list_argument_limits[0].max_items == 4
    assert profile.graph_review_fetch_tool_name == "fetch_articles"
    assert profile.solver_graph_review is not None
    assert "Graph候補選別モード" in profile.solver_graph_review.system_prompt
    assert "selected_article_ids" in profile.solver_graph_review.system_prompt
    assert "relevant_article_ids" in profile.solver_graph_review.system_prompt
    assert "法的関連性はあなたが判断" in profile.solver_graph_review.system_prompt


def test_contract_repair_prompt_handles_unknown_article_ids() -> None:
    context = build_solver_context(
        CaseState(case_id="case-1", question="質問"),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
        contract_feedback=SolverContractFeedback(
            violation="tool request references unknown Article IDs: ['unknown']",
            previous_decision=SolverDecision(
                next="finalize",
                answer={"text": "回答"},
            ),
        ),
    )

    prompt = _solver_prompt(context, "system")

    assert "openは未完了で追加作業が必要" in prompt
    assert "droppedは前提否定・重複・質問との無関係" in prompt
    assert "unresolvedは根拠不足・両義的・未確認" in prompt
    assert "succeededはTool実行が完了した状態" in prompt
    assert "得た内容が質問を立証したという意味ではない" in prompt
    assert "finalize_only" in prompt
    assert "material_included" in prompt
    assert "not_requiredは当該依存確認が回答に不要" in prompt
    assert "fetchable_article_idsに完全一致するIDだけ" in prompt
    assert "未知IDの条番号を修正した別IDや新しいIDを追加しません" in prompt
    assert "本文中の条番号、法令番号、documentIdをArticle IDへ変換しません" in prompt
    assert "修復後のfetch_articlesの全IDをfetchable_article_ids" in prompt
    assert "fetch_articlesを残さず" in prompt
    assert "fetch_articles.article_ids exceeds the profile limit" in prompt
    assert "Articleを4個以下に意味選択" in prompt
    assert "dependency target Article repeats its source article" in prompt
    assert "action=assess_source、target_article_ids=[]" in prompt


def test_solver_prompt_fails_instead_of_dropping_context() -> None:
    context = build_solver_context(
        CaseState(case_id="case-1", question="質問"),
        AgentLimits(
            max_material_evidence_chars=1000,
            max_solver_input_chars=2000,
        ),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )

    with pytest.raises(ContextCapacityExceeded, match="context_capacity_exceeded"):
        _solver_prompt(context, "system")


def test_explicit_framework_endpoint_does_not_use_legacy_service(monkeypatch) -> None:
    expected = AnswerResponse(
        pattern="agent_framework_v1",
        route=["agent_framework"],
        answer="新経路",
        citations=[],
        graphPaths=[],
        trace={"agentFramework": {"reviewerEnabled": False}},
    )
    monkeypatch.setattr(
        main.framework_agent_service,
        "answer",
        lambda request: expected,
    )
    monkeypatch.setattr(
        main.agent_service,
        "answer",
        lambda request: (_ for _ in ()).throw(AssertionError("legacy called")),
    )

    response = main.framework_answer(AnswerRequest(question="質問"))

    assert response["pattern"] == "agent_framework_v1"


def test_answer_feature_flag_selects_new_framework(monkeypatch) -> None:
    expected = AnswerResponse(
        pattern="agent_framework_v1",
        route=["agent_framework"],
        answer="新経路",
        citations=[],
        graphPaths=[],
        trace={},
    )
    monkeypatch.setattr(main.settings, "agent_framework_active", True)
    monkeypatch.setattr(
        main.framework_agent_service,
        "answer",
        lambda request: expected,
    )

    response = main.answer(AnswerRequest(question="質問"))

    assert response["pattern"] == "agent_framework_v1"


def test_model_adapter_repairs_transport_json_once() -> None:
    class RepairClient:
        def __init__(self) -> None:
            self.calls = 0

        def generate_structured_json(self, **kwargs: Any) -> StructuredJSONResult:
            self.calls += 1
            payload = (
                {"next": "finalize", "answer": None}
                if self.calls == 1
                else {
                    "next": "finalize",
                    "answer": {"text": "修復済み"},
                }
            )
            return StructuredJSONResult(
                payload=payload,
                provider="fake",
                model="fake-model",
                latencyMs=1,
                inputTokens=1,
                outputTokens=1,
            )

    client = RepairClient()
    limits = AgentLimits()
    context = build_solver_context(
        CaseState(case_id="case-1", question="質問"),
        limits,
        remaining_wall_time_sec=60,
        finalize_only=False,
    )

    result = StructuredJSONModelAdapter(client).solve(
        context,
        ModelCallProfile(
            model="fake-model",
            system_prompt="prompt",
        ),
    )

    assert result.decision.answer is not None
    assert result.decision.answer.text == "修復済み"
    assert result.attempt_count == 2
    assert client.calls == 2
