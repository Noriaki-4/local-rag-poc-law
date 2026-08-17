"""新Frameworkから法令Tool・Model・API応答までの薄い縦切り。"""

from __future__ import annotations

import json
from typing import Any

import pytest
import requests

from app import main
from app.adapters.models import StructuredJSONModelAdapter
from app.adapters.models.structured_json import _solver_prompt, _solver_transport_schema
from app.agent_framework.context import (
    ContextCapacityExceeded,
    GraphCandidateArticle,
    GraphCandidateCatalog,
    GraphCandidateLink,
    GraphReviewBatch,
    SolverContractFeedback,
    _graph_review_projection,
    build_solver_context,
)
from app.agent_framework.contracts import SolverDecision
from app.agent_framework.loop import AgentLoop
from app.agent_framework.profiles import AgentLimits, ModelCallProfile
from app.agent_framework.state import (
    CaseState,
    DeferredFrontierResolution,
    FinalAnswer,
    Hypothesis,
    ToolRequest,
    UnreviewedGraphResolution,
    WorkItem,
)
from app.agent_framework.validation import ContractViolation, apply_solver_decision
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
                "frontier_re_adoptions": decision.get("frontier_re_adoptions", []),
                "deferred_frontier_resolutions": decision.get(
                    "deferred_frontier_resolutions", []
                ),
                "unreviewed_graph_resolution": decision.get(
                    "unreviewed_graph_resolution"
                ),
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
    assert initial_dependency_schema["items"] == {"type": "string"}
    assert llm.calls[1]["schema"]["properties"]["dependency_decisions"][
        "maxItems"
    ] == 0
    assert framework_trace["dependencyDecisions"] == []


def test_framework_reviewer_setting_defaults_to_false() -> None:
    from app.config import settings

    assert settings.agent_framework_reviewer_enabled is False


def test_graph_review_paging_preserves_discovery_order_instead_of_hash_order() -> None:
    state = CaseState(
        case_id="case-1",
        question="質問",
        work_items=(WorkItem(work_item_id="w1", question="確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="関連Articleを確認する",
            ),
        ),
    )
    catalog = GraphCandidateCatalog(
        articles=(
            GraphCandidateArticle(
                article_id="article-b",
                document_id=None,
                title=None,
                heading=None,
                content_status="not_requested",
            ),
            GraphCandidateArticle(
                article_id="article-a",
                document_id=None,
                title=None,
                heading=None,
                content_status="not_requested",
            ),
        ),
        links=(
            GraphCandidateLink(
                link_id="link-b",
                seed_article_id="seed",
                candidate_article_id="article-b",
                work_item_ids=("w1",),
                hypothesis_ids=("h1",),
                relations=(),
                graph_request_ids=("graph-1",),
            ),
            GraphCandidateLink(
                link_id="link-a",
                seed_article_id="seed",
                candidate_article_id="article-a",
                work_item_ids=("w1",),
                hypothesis_ids=("h1",),
                relations=(),
                graph_request_ids=("graph-1",),
            ),
        ),
    )

    batch, _ = _graph_review_projection(state, catalog, max_candidates=2)

    assert [item.article_id for item in batch.candidates] == [
        "article-b",
        "article-a",
    ]


def test_all_solver_stages_include_shared_legal_research_rules() -> None:
    profile = legal_profiles.legal_agent_profile()
    assert profile.version == "49"
    prompts = (
        profile.solver_research.system_prompt,
        profile.solver_integration.system_prompt,
    )

    for prompt in prompts:
        assert prompt.count("全サイクル共通の規則") == 1
        assert "各サイクルで元の質問へ戻り" in prompt
        assert "別法令との関係や条文番号を推測して作業へ加えません" in prompt
        assert "委任先本文を未確認のまま当該観点をsupportedまたはresolvedにしません" in prompt
        assert "material_included=falseのEvidenceは本文未提示" in prompt
        assert "graph_review_batchは今回判断が必要な新規・再採用・新Link差分" in prompt
        assert "graph_review_ledgerは過去の全評価済みfrontier" in prompt
        assert "content_statusのnot_requested/pending/succeeded/failed/timeout" in prompt
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
        assert "GraphをToolRequestへ指定しません" in prompt
        assert "evidenceRole=search_navigationの検索結果は、次のlegal_searchまたはfetch_articles" in prompt
        assert "その本文抜粋をHypothesisのjudgment" in prompt
        assert "1つのHypothesisは、取得本文で独立に検証できる1つの命題" in prompt
        assert "特定条文の内容を説明する場合" in prompt
        assert "cycle_step_timeoutはCycle用の時間切れ" in prompt
        assert "deferred_frontier_resolutions" in prompt
        assert "Programは既知ID、全件性、actionと次動作の参照整合だけ" in prompt
        assert "remaining_unreviewed_count>0" in prompt
        assert "unreviewed_graph_resolution" in prompt
        assert "unresolved_work_item_ids" in prompt
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
    assert "各open WorkItemへ対応付けます" in (
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
    graph_prompt = profile.solver_graph_review.system_prompt
    assert "Graph Reviewモード" in graph_prompt
    assert "batchの全frontier_item_idへ判断を1件ずつ返します" in graph_prompt
    assert "select、関係するが今回の取得枠外ならdefer" in graph_prompt
    assert "reviewed_link_idsにはbatch内の全link_id" in graph_prompt
    assert "start_next_cycle=false" in graph_prompt
    assert "deferred_frontier_resolutions=[]" in graph_prompt
    assert "各reasonは判断を区別できる一文" in graph_prompt


def test_cycle_boundary_requires_a_structural_resolution_for_every_deferred_frontier(
) -> None:
    state = CaseState(case_id="case-1", question="質問", research_cycle_count=1)

    with pytest.raises(ContractViolation, match="every active deferred Frontier"):
        apply_solver_decision(
            state,
            SolverDecision(next="finalize", answer=FinalAnswer(text="回答")),
            limits=AgentLimits(),
            known_tool_names={"fetch_articles"},
            material_evidence_ids=(),
            graph_review_fetch_tool_name="fetch_articles",
            deferred_frontiers={"f1": ("article-1", "w1", "h1")},
            finalize_only=False,
        )


def test_cycle_boundary_accepts_solver_no_longer_needed_judgment() -> None:
    state = CaseState(case_id="case-1", question="質問", research_cycle_count=1)
    resolution = DeferredFrontierResolution(
        frontier_item_id="f1",
        article_id="article-1",
        work_item_id="w1",
        hypothesis_id="h1",
        action="no_longer_needed",
        reason="後続本文により質問への回答には不要と判断した",
    )

    updated = apply_solver_decision(
        state,
        SolverDecision(
            next="finalize",
            deferred_frontier_resolutions=(resolution,),
            answer=FinalAnswer(text="回答"),
        ),
        limits=AgentLimits(),
        known_tool_names={"fetch_articles"},
        material_evidence_ids=(),
        graph_review_fetch_tool_name="fetch_articles",
        deferred_frontiers={"f1": ("article-1", "w1", "h1")},
        finalize_only=False,
    )

    assert updated.deferred_frontier_resolutions[0].action == "no_longer_needed"
    assert updated.deferred_frontier_resolutions[0].decided_cycle == 1


def test_fetch_next_cycle_must_match_the_cycle_start_fetch() -> None:
    state = CaseState(
        case_id="case-1",
        question="質問",
        research_cycle_count=1,
        work_items=(WorkItem(work_item_id="w1", question="確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="Article本文が必要である",
            ),
        ),
    )
    resolution = DeferredFrontierResolution(
        frontier_item_id="f1",
        article_id="article-1",
        work_item_id="w1",
        hypothesis_id="h1",
        action="fetch_next_cycle",
        reason="次Cycleで本文を確認する",
    )
    decision = SolverDecision(
        next="continue",
        start_next_cycle=True,
        next_focus_work_item_ids=("w1",),
        deferred_frontier_resolutions=(resolution,),
        tool_requests=(
            ToolRequest(
                request_id="fetch-1",
                work_item_id="w1",
                tool_name="fetch_articles",
                arguments={"article_ids": ["article-1"]},
                purpose="保留したArticle本文を確認する",
                hypothesis_ids=("h1",),
            ),
        ),
    )

    updated = apply_solver_decision(
        state,
        decision,
        limits=AgentLimits(),
        known_tool_names={"fetch_articles"},
        material_evidence_ids=(),
        fetchable_article_ids=("article-1",),
        graph_review_fetch_tool_name="fetch_articles",
        deferred_frontiers={"f1": ("article-1", "w1", "h1")},
        finalize_only=False,
    )
    assert updated.tool_requests[-1].arguments["article_ids"] == ["article-1"]

    missing_fetch = decision.model_copy(update={"tool_requests": ()})
    projected = apply_solver_decision(
        state,
        missing_fetch,
        limits=AgentLimits(),
        known_tool_names={"fetch_articles"},
        material_evidence_ids=(),
        fetchable_article_ids=("article-1",),
        graph_review_fetch_tool_name="fetch_articles",
        deferred_frontiers={"f1": ("article-1", "w1", "h1")},
        finalize_only=False,
    )
    assert projected.deferred_frontier_resolutions[0].action == "fetch_next_cycle"


def test_carry_forward_keeps_a_deferred_frontier_without_forcing_a_fetch() -> None:
    state = CaseState(
        case_id="case-1",
        question="質問",
        research_cycle_count=1,
        work_items=(WorkItem(work_item_id="w1", question="確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="Article本文が必要である",
            ),
        ),
    )
    resolution = DeferredFrontierResolution(
        frontier_item_id="f1",
        article_id="article-1",
        work_item_id="w1",
        hypothesis_id="h1",
        action="carry_forward",
        reason="次Cycleの取得上限外なのでactive候補として保持する",
    )

    updated = apply_solver_decision(
        state,
        SolverDecision(
            next="continue",
            start_next_cycle=True,
            deferred_frontier_resolutions=(resolution,),
        ),
        limits=AgentLimits(),
        known_tool_names={"fetch_articles"},
        material_evidence_ids=(),
        graph_review_fetch_tool_name="fetch_articles",
        deferred_frontiers={"f1": ("article-1", "w1", "h1")},
        finalize_only=False,
    )

    assert updated.deferred_frontier_resolutions[0].action == "carry_forward"


def test_agent_loop_mechanically_projects_fetch_next_cycle_resolutions() -> None:
    profile = legal_profiles.legal_agent_profile()
    loop = object.__new__(AgentLoop)
    loop._profile = profile
    resolution = DeferredFrontierResolution(
        frontier_item_id="f1",
        article_id="article-1",
        work_item_id="w1",
        hypothesis_id="h1",
        action="fetch_next_cycle",
        reason="次Cycleで取得する",
    )

    request = loop._deferred_frontier_fetch_request(
        CaseState(case_id="case-1", question="質問", research_cycle_count=1),
        (resolution,),
    )

    assert request.tool_name == "fetch_articles"
    assert request.arguments == {"article_ids": ["article-1"]}
    assert request.work_item_id == "w1"
    assert request.hypothesis_ids == ("h1",)


def test_solver_can_start_a_cycle_to_review_preserved_graph_candidates() -> None:
    decision = SolverDecision(
        next="continue",
        start_next_cycle=True,
        unreviewed_graph_resolution=UnreviewedGraphResolution(
            action="review_next_cycle",
            reason="次Cycleで未評価候補を確認する",
        ),
    )

    updated = apply_solver_decision(
        CaseState(case_id="case-1", question="質問", research_cycle_count=1),
        decision,
        limits=AgentLimits(),
        known_tool_names=set(),
        material_evidence_ids=(),
        unreviewed_graph_candidate_count=5,
        finalize_only=False,
    )

    assert updated.final_answer is None
    assert updated.unreviewed_graph_resolutions[0].candidate_count == 5


def test_cycle_boundary_cannot_silently_ignore_unreviewed_graph_candidates() -> None:
    with pytest.raises(ContractViolation, match="unreviewed Graph candidate pool"):
        apply_solver_decision(
            CaseState(case_id="case-1", question="質問", research_cycle_count=1),
            SolverDecision(next="finalize", answer=FinalAnswer(text="回答")),
            limits=AgentLimits(),
            known_tool_names=set(),
            material_evidence_ids=(),
            unreviewed_graph_candidate_count=5,
            finalize_only=False,
        )


def test_cycle_boundary_schema_requires_unreviewed_graph_resolution() -> None:
    context = build_solver_context(
        CaseState(case_id="case-1", question="質問", research_cycle_count=1),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    ).model_copy(
        update={
            "graph_review_batch": GraphReviewBatch(remaining_unreviewed_count=5),
            "cycle_budget_reached": True,
            "cycle_close_required": True,
        }
    )

    schema = _solver_transport_schema(context)

    resolution_schema = schema["properties"]["unreviewed_graph_resolution"]
    assert resolution_schema["type"] == "object"
    assert "anyOf" not in resolution_schema


def test_open_finalize_repair_schema_forces_the_next_cycle_shape() -> None:
    context = build_solver_context(
        CaseState(
            case_id="case-1",
            question="質問",
            research_cycle_count=1,
            work_items=(WorkItem(work_item_id="w1", question="確認する"),),
        ),
        AgentLimits(),
        remaining_wall_time_sec=120,
        finalize_only=False,
        contract_feedback=SolverContractFeedback(
            violation="finalize must account for every open WorkItem",
            previous_decision=SolverDecision(
                next="finalize",
                answer=FinalAnswer(text="回答"),
            ),
        ),
    ).model_copy(
        update={
            "graph_review_batch": GraphReviewBatch(remaining_unreviewed_count=5),
            "cycle_budget_reached": True,
            "cycle_close_required": True,
        }
    )

    schema = _solver_transport_schema(context)
    properties = schema["properties"]

    assert properties["next"]["enum"] == ["continue"]
    assert properties["start_next_cycle"]["enum"] == [True]
    assert properties["tool_requests_json"]["enum"] == ["[]"]
    assert properties["answer"] == {"type": "null"}
    assert properties["unreviewed_graph_resolution"]["properties"]["action"][
        "enum"
    ] == ["review_next_cycle"]
    assert properties["next_focus_work_item_ids"]["items"]["enum"] == ["w1"]
    assert properties["update_json"]["enum"] == ["{}"]


def test_open_finalize_adapter_preserves_previous_case_update() -> None:
    previous_decision = SolverDecision(
        next="finalize",
        update={
            "update_hypotheses": [
                {
                    "hypothesis_id": "h1",
                    "judgment": "unresolved",
                    "gaps": ["追加確認が必要"],
                }
            ]
        },
        answer=FinalAnswer(text="限定回答"),
    )
    context = build_solver_context(
        CaseState(
            case_id="case-1",
            question="質問",
            research_cycle_count=1,
            work_items=(WorkItem(work_item_id="w1", question="確認する"),),
            hypotheses=(
                Hypothesis(
                    hypothesis_id="h1",
                    work_item_id="w1",
                    statement="確認する",
                ),
            ),
        ),
        AgentLimits(),
        remaining_wall_time_sec=120,
        finalize_only=False,
        contract_feedback=SolverContractFeedback(
            violation="finalize must account for every open WorkItem",
            previous_decision=previous_decision,
        ),
    ).model_copy(
        update={
            "graph_review_batch": GraphReviewBatch(remaining_unreviewed_count=1),
            "cycle_budget_reached": True,
            "cycle_close_required": True,
        }
    )

    class RepairLLM:
        provider = "fake"

        def generate_structured_json(self, **kwargs: Any) -> StructuredJSONResult:
            return StructuredJSONResult(
                payload={
                    "next": "continue",
                    "start_next_cycle": True,
                    "update_json": "{}",
                    "next_focus_work_item_ids": ["w1"],
                    "retain_evidence_ids": [],
                    "tool_requests_json": "[]",
                    "dependency_decisions": [],
                    "graph_candidate_review": None,
                    "frontier_re_adoptions": [],
                    "deferred_frontier_resolutions": [],
                    "unreviewed_graph_resolution": {
                        "action": "review_next_cycle",
                        "reason": "次Cycleで確認する",
                    },
                    "answer": None,
                },
                provider="fake",
                model=kwargs["model"],
                latencyMs=1,
                inputTokens=10,
                outputTokens=10,
            )

    result = StructuredJSONModelAdapter(RepairLLM()).solve(
        context,
        ModelCallProfile(model="fake", system_prompt="判断する"),
    )

    assert result.decision.update == previous_decision.update


def test_preflight_reports_independent_contract_violations_together() -> None:
    state = CaseState(
        case_id="case-1",
        question="質問",
        research_cycle_count=1,
        work_items=(WorkItem(work_item_id="w1", question="確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="確認する",
            ),
        ),
    )
    decision = SolverDecision(
        next="finalize",
        update={
            "update_hypotheses": [
                {
                    "hypothesis_id": "h1",
                    "judgment": "supported",
                    "evidence_ids": ["unknown-evidence"],
                }
            ]
        },
        answer=FinalAnswer(text="回答"),
    )

    with pytest.raises(ContractViolation) as exc_info:
        apply_solver_decision(
            state,
            decision,
            limits=AgentLimits(),
            known_tool_names=set(),
            material_evidence_ids=(),
            unreviewed_graph_candidate_count=5,
            finalize_only=False,
        )

    message = str(exc_info.value)
    assert "multiple contract violations" in message
    assert "unknown evidence IDs" in message
    assert "unreviewed Graph candidate pool" in message
    assert "every open WorkItem" in message


def test_preflight_reports_invalid_tool_and_focus_references_together() -> None:
    state = CaseState(case_id="case-1", question="質問")
    decision = SolverDecision(
        next="continue",
        update={
            "add_work_items": [
                {"work_item_id": "w1", "question": "確認する"}
            ],
            "add_hypotheses": [
                {
                    "hypothesis_id": "h1",
                    "work_item_id": "w1",
                    "statement": "確認する",
                }
            ],
        },
        next_focus_work_item_ids=("unknown-work",),
        tool_requests=(
            ToolRequest(
                request_id="r1",
                work_item_id="unknown-work",
                tool_name="legal_search",
                arguments={"query": "確認"},
                purpose="確認する",
                hypothesis_ids=("unknown-hypothesis",),
            ),
        ),
    )

    with pytest.raises(ContractViolation) as exc_info:
        apply_solver_decision(
            state,
            decision,
            limits=AgentLimits(),
            known_tool_names={"legal_search"},
            material_evidence_ids=(),
            finalize_only=False,
        )

    message = str(exc_info.value)
    assert "multiple contract violations" in message
    assert "focus must reference open WorkItem IDs" in message
    assert "tool requests must reference open WorkItem IDs" in message
    assert "tool requests reference unknown Hypothesis IDs" in message


def test_unreviewed_graph_pool_can_remain_unresolved_only_in_limited_answer() -> None:
    state = CaseState(
        case_id="case-1",
        question="質問",
        research_cycle_count=4,
        work_items=(WorkItem(work_item_id="w1", question="府令を確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="府令が要件を定める",
                gaps=("Graph候補は未評価",),
            ),
        ),
    )
    decision = SolverDecision(
        next="finalize",
        unreviewed_graph_resolution=UnreviewedGraphResolution(
            action="unresolved_at_limit",
            reason="Cycle上限のため候補を評価できない",
        ),
        answer=FinalAnswer(
            text="確認済み範囲の限定回答",
            limitations=("府令候補は未確認",),
            unresolved_work_item_ids=("w1",),
            unresolved_hypothesis_ids=("h1",),
        ),
    )

    finalized = apply_solver_decision(
        state,
        decision,
        limits=AgentLimits(max_research_cycles=4),
        known_tool_names=set(),
        material_evidence_ids=(),
        unreviewed_graph_candidate_count=5,
        finalize_only=True,
        can_start_next_cycle=False,
    )

    assert finalized.unreviewed_graph_resolutions[0].action == "unresolved_at_limit"
    assert finalized.work_items[0].state == "open"


def test_answer_limitations_require_structured_unresolved_scope() -> None:
    with pytest.raises(ContractViolation, match="limitations and unresolved_work_item_ids"):
        apply_solver_decision(
            CaseState(case_id="case-1", question="質問"),
            SolverDecision(
                next="finalize",
                answer=FinalAnswer(
                    text="限定回答",
                    limitations=("府令本文は未確認",),
                ),
            ),
            limits=AgentLimits(),
            known_tool_names=set(),
            material_evidence_ids=(),
            finalize_only=False,
        )


def test_normal_finalize_cannot_leave_structured_unresolved_scope() -> None:
    state = CaseState(
        case_id="case-1",
        question="質問",
        work_items=(WorkItem(work_item_id="w1", question="府令を確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="府令が要件を定める",
                gaps=("本文未確認",),
            ),
        ),
    )
    decision = SolverDecision(
        next="finalize",
        answer=FinalAnswer(
            text="限定回答",
            limitations=("府令本文は未確認",),
            unresolved_work_item_ids=("w1",),
            unresolved_hypothesis_ids=("h1",),
        ),
    )

    with pytest.raises(ContractViolation, match="another Cycle can start"):
        apply_solver_decision(
            state,
            decision,
            limits=AgentLimits(),
            known_tool_names=set(),
            material_evidence_ids=(),
            finalize_only=False,
            can_start_next_cycle=True,
        )

    finalized = apply_solver_decision(
        state,
        decision,
        limits=AgentLimits(),
        known_tool_names=set(),
        material_evidence_ids=(),
        finalize_only=True,
        can_start_next_cycle=False,
    )
    assert finalized.work_items[0].state == "open"
    assert finalized.final_answer.unresolved_work_item_ids == ("w1",)


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


def test_contract_repair_prompt_does_not_close_work_only_to_pass_finalize() -> None:
    context = build_solver_context(
        CaseState(
            case_id="case-1",
            question="質問",
            work_items=(WorkItem(work_item_id="w1", question="府令を確認する"),),
            hypotheses=(
                Hypothesis(
                    hypothesis_id="h1",
                    work_item_id="w1",
                    statement="府令が要件を定める",
                    gaps=("本文未確認",),
                ),
            ),
        ),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
        contract_feedback=SolverContractFeedback(
            violation="finalize must account for every open WorkItem",
            previous_decision=SolverDecision(
                next="finalize",
                answer=FinalAnswer(text="回答"),
            ),
        ),
    )

    prompt = _solver_prompt(context, "system")

    assert "契約を通す目的だけでopen WorkItemをresolvedまたはdroppedへ変更" in prompt
    assert "openを偽って閉じず" in prompt
    assert "limitationsを削除して見かけ上完了させてはいけません" in prompt


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


def test_finalize_only_context_never_claims_that_another_cycle_can_start() -> None:
    context = build_solver_context(
        CaseState(case_id="case-1", question="質問", research_cycle_count=4),
        AgentLimits(max_research_cycles=4),
        remaining_wall_time_sec=60,
        finalize_only=True,
    )

    assert context.remaining_research_cycles == 0
    assert context.can_start_next_cycle is False


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


def test_model_adapter_normalizes_provider_timeout() -> None:
    class TimeoutClient:
        def generate_structured_json(self, **kwargs: Any) -> StructuredJSONResult:
            del kwargs
            raise requests.ReadTimeout("provider did not respond")

    context = build_solver_context(
        CaseState(case_id="case-1", question="質問"),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )

    with pytest.raises(TimeoutError, match="model provider request timed out"):
        StructuredJSONModelAdapter(TimeoutClient()).solve(
            context,
            ModelCallProfile(
                model="fake-model",
                system_prompt="prompt",
            ),
        )
