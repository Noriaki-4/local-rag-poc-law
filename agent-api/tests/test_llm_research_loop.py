"""LLM主導調査ループとtool gatewayの境界テスト。"""

from time import perf_counter

import requests

from app.config import settings
from app.llm import ResearchCheckpointResult, ResearchTurnResult
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
    ResearchTurn,
)
from app.llm_research_loop import (
    _checkpoint_from_stage_selection,
    _checkpoint_answer_content_ids,
    run_llm_directed_research,
    run_llm_directed_research_shadow,
)
from app.llm_research_tools import LegalResearchToolGateway
from app.models import AnswerRequest


def _result(turn: ResearchTurn) -> ResearchTurnResult:
    """旧テスト用判断を現行の仮説検証契約へ補完する。"""
    hypothesis_id = "H-test-core"
    hypotheses = list(turn.hypotheses)
    if not hypotheses:
        evidence_ids = [
            item.contentUnitId for item in turn.selectedEvidence
        ]
        hypotheses = [
            ResearchHypothesis(
                hypothesisId=hypothesis_id,
                statement="質問の中心的結論が取得本文で支持される",
                status=(
                    "supported"
                    if turn.status == RESEARCH_STATUS_READY and evidence_ids
                    else "unverified"
                ),
                evidenceIds=evidence_ids,
                missing=([] if evidence_ids else ["中心的根拠"]),
            )
        ]
    else:
        hypothesis_id = hypotheses[0].hypothesisId
    actions = [
        (
            action
            if action.hypothesisIds
            else action.model_copy(
                update={"hypothesisIds": [hypothesis_id]}
            )
        )
        for action in turn.actions
    ]
    turn = turn.model_copy(
        update={"hypotheses": hypotheses, "actions": actions}
    )
    return ResearchTurnResult(
        turn=turn,
        provider="test",
        model="test-model",
        latencyMs=1,
        inputTokens=1,
        outputTokens=1,
    )


def _law_source() -> dict:
    return {
        "contentUnitId": "law-a-article-12-paragraph-1",
        "articleContentUnitId": "law-a-article-12",
        "documentId": "law-a",
        "docType": "law",
        "title": "テスト法",
        "heading": "第十二条",
        "text": "許可を受けなければならない。",
    }


class FakeOpenSearch:
    def __init__(self) -> None:
        self.search_calls: list[tuple] = []
        self.fetch_calls: list[list[str]] = []
        self.fetch_max_chunks: list[int] = []
        self.document_search_calls: list[tuple] = []

    def law_titles(self) -> dict[str, str]:
        return {"law-a": "テスト法"}

    def search(self, *args) -> list[dict]:
        self.search_calls.append(args)
        return [{"document": _law_source(), "score": 0.9}]

    def get_by_article_ids(
        self,
        article_ids: list[str],
        user_clearance_level: int,
        max_chunks: int,
    ) -> list[dict]:
        self.fetch_calls.append(article_ids)
        self.fetch_max_chunks.append(max_chunks)
        return [_law_source()]

    def search_requirement_specs(
        self, specs, *, user_clearance_level: int, timeout_sec: float
    ) -> dict[str, list[dict]]:
        self.document_search_calls.append(
            (specs, user_clearance_level, timeout_sec)
        )
        return {
            spec.requirement_id: [
                {
                    "articleId": "law-a-article-12",
                    "chunks": [_law_source()],
                }
            ]
            for spec in specs
            if spec.doc_type == "law"
        }


class FakeGraph:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def paths_from_many(self, article_ids: list[str], **kwargs) -> list[dict]:
        self.calls.append({"articleIds": article_ids, **kwargs})
        return [
            {
                "nodes": [
                    {"graphNodeId": "law-a-article-12"},
                    {"graphNodeId": "law-b-article-3"},
                ],
                "edges": [
                    {
                        "edgeType": "IMPLEMENTS",
                        "relationSource": "subordinate_law_parent_reference",
                        "relationConfidence": 0.98,
                        "derivedFromEdgeId": "reference-1",
                        "delegationWordingDetected": True,
                    }
                ],
            }
        ]


class SearchThenReadyLLM:
    def __init__(self) -> None:
        self.calls = 0

    def decide_legal_research_turn(
        self, request, catalog, history, timeout_sec, **kwargs
    ):
        self.calls += 1
        if self.calls == 1:
            return _result(
                ResearchTurn(
                    status=RESEARCH_STATUS_CONTINUE,
                    actions=[
                        ResearchAction(
                            tool=TOOL_SEARCH_CORPUS,
                            query="許可 根拠",
                            docTypes=["law"],
                        )
                    ],
                )
            )
        return _result(
            ResearchTurn(
                status=RESEARCH_STATUS_READY,
                selectedEvidence=[
                    ResearchEvidenceSelection(
                        contentUnitId="law-a-article-12-paragraph-1"
                    )
                ],
            )
        )


def test_loop_searches_then_selects_only_retrieved_evidence(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent_max_wall_time_sec", 280)
    monkeypatch.setattr(settings, "llm_timeout_sec", 60)
    monkeypatch.setattr(settings, "agent_answer_reserve_sec", 30)
    monkeypatch.setattr(settings, "llm_research_shadow_budget_sec", 30)
    os_client = FakeOpenSearch()
    llm = SearchThenReadyLLM()

    outcome = run_llm_directed_research_shadow(
        request=AnswerRequest(question="許可の根拠は何ですか"),
        os_client=os_client,
        graph_client=FakeGraph(),
        llm_client=llm,
        deadline=perf_counter() + 280,
    )

    assert outcome.trace["status"] == RESEARCH_STATUS_READY
    assert outcome.trace["connectedToAnswer"] is False
    assert outcome.trace["llmCallCount"] == 2
    assert outcome.trace["toolCallCount"] == 1
    assert outcome.selected_content_unit_ids == (
        "law-a-article-12-paragraph-1",
    )
    assert len(os_client.search_calls) == 1
    assert outcome.selected_evidence[0]["text"] == "許可を受けなければならない。"


def test_iterative_loop_runs_three_complete_cycles_and_carries_checkpoint(
    monkeypatch,
) -> None:
    class IterativeLLM:
        supports_iterative_research = True

        def __init__(self) -> None:
            self.stage_calls: list[dict] = []
            self.integration_calls: list[dict] = []

        def decide_legal_research_turn(
            self,
            request,
            catalog,
            history,
            timeout_sec,
            *,
            phase=None,
            cycle_index=None,
            checkpoint=None,
            **kwargs,
        ):
            self.stage_calls.append(
                {
                    "phase": phase,
                    "cycleIndex": cycle_index,
                    "checkpointConclusion": checkpoint.conclusion,
                    "checkpointNextArticleIds": tuple(
                        checkpoint.nextArticleIds
                    ),
                    "checkpointIssueIds": tuple(
                        issue.issueId
                        for issue in checkpoint.logicalStructure.issues
                    ),
                    "historyLength": len(history),
                }
            )
            if phase == "explore" and cycle_index == 0:
                return _result(
                    ResearchTurn(
                        status=RESEARCH_STATUS_CONTINUE,
                        actions=[
                            ResearchAction(
                                tool=TOOL_SEARCH_CORPUS,
                                query="許可",
                                docTypes=["law"],
                            )
                        ],
                    )
                )
            return _result(
                ResearchTurn(
                    status=RESEARCH_STATUS_READY,
                    selectedEvidence=[
                        ResearchEvidenceSelection(
                            contentUnitId=(
                                "law-a-article-12-paragraph-1"
                            )
                        )
                    ],
                )
            )

        def integrate_legal_research_cycle(
            self,
            request,
            catalog,
            checkpoint,
            *,
            cycle_index,
            cycle_count,
            cycle_new_content_ids,
            tool_history,
            timeout_sec,
            **kwargs,
        ):
            self.integration_calls.append(
                {
                    "cycleIndex": cycle_index,
                    "previousConclusion": checkpoint.conclusion,
                    "newIds": tuple(cycle_new_content_ids),
                }
            )
            integrated = ResearchCheckpoint(
                status=RESEARCH_STATUS_READY,
                conclusion=f"cycle-{cycle_index + 1}",
                evidenceIds=["law-a-article-12-paragraph-1"],
                nextQuestions=[],
                nextArticleIds=["law-a-article-12"],
                logicalStructure=ResearchLogicalStructure(
                    issues=[
                        ResearchIssueStructure(
                            issueId="permit",
                            question="許可義務",
                            status="verified",
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
                                )
                            ],
                            claims=[
                                ResearchClaimStructure(
                                    claimId="permit-required",
                                    conclusion="許可が必要",
                                    status="verified",
                                    authorityNodeIds=["act-12"],
                                )
                            ],
                        )
                    ]
                ),
            )
            return ResearchCheckpointResult(
                checkpoint=integrated,
                provider="test",
                model="test",
                latencyMs=1,
                inputTokens=1,
                outputTokens=1,
            )

    monkeypatch.setattr(settings, "llm_timeout_sec", 10)
    monkeypatch.setattr(settings, "agent_answer_reserve_sec", 10)
    monkeypatch.setattr(settings, "llm_research_active_budget_sec", 120)
    monkeypatch.setattr(settings, "llm_research_max_turns", 3)
    monkeypatch.setattr(settings, "llm_research_max_tool_calls", 18)
    llm = IterativeLLM()

    outcome = run_llm_directed_research(
        request=AnswerRequest(question="許可の根拠は何ですか"),
        os_client=FakeOpenSearch(),
        graph_client=FakeGraph(),
        llm_client=llm,
        deadline=perf_counter() + 150,
    )

    assert (
        outcome.trace["algorithm"]
        == "iterative_cycles_v8_hypothesis_testing"
    )
    assert outcome.trace["cycleCount"] == 3
    assert outcome.trace["llmCallCount"] == 9
    assert [call["phase"] for call in llm.stage_calls] == [
        "explore",
        "deepen",
        "explore",
        "deepen",
        "explore",
        "deepen",
    ]
    assert [
        call["checkpointConclusion"]
        for call in llm.stage_calls
        if call["phase"] == "explore"
    ] == ["", "cycle-1", "cycle-2"]
    assert [
        call["historyLength"]
        for call in llm.stage_calls
        if call["phase"] == "explore"
    ] == [0, 1, 0]
    assert [
        call["checkpointNextArticleIds"]
        for call in llm.stage_calls
        if call["phase"] == "explore"
    ] == [
        (),
        ("law-a-article-12",),
        ("law-a-article-12",),
    ]
    assert [
        call["checkpointIssueIds"]
        for call in llm.stage_calls
        if call["phase"] == "explore"
    ] == [(), ("permit",), ("permit",)]
    assert llm.integration_calls[0]["newIds"] == ()
    assert outcome.trace["status"] == RESEARCH_STATUS_READY
    assert outcome.selected_content_unit_ids == (
        "law-a-article-12-paragraph-1",
    )


def test_iterative_loop_executes_checkpoint_pending_task_before_next_llm(
    monkeypatch,
) -> None:
    class ArticleAwareOpenSearch(FakeOpenSearch):
        def law_titles(self) -> dict[str, str]:
            return {"law-a": "テスト法", "law-b": "テスト府令"}

        def get_by_article_ids(
            self,
            article_ids: list[str],
            user_clearance_level: int,
            max_chunks: int,
        ) -> list[dict]:
            self.fetch_calls.append(article_ids)
            self.fetch_max_chunks.append(max_chunks)
            article_id = article_ids[0]
            return [
                {
                    "contentUnitId": f"{article_id}-paragraph-1",
                    "articleContentUnitId": article_id,
                    "documentId": article_id.split("-article-")[0],
                    "docType": "law",
                    "title": "テスト府令" if article_id.startswith("law-b") else "テスト法",
                    "heading": "第三条" if article_id.startswith("law-b") else "第十二条",
                    "text": "具体的要件を定める。",
                }
            ]

    class PendingTaskLLM:
        supports_iterative_research = True

        def decide_legal_research_turn(
            self, request, catalog, history, timeout_sec, **kwargs
        ):
            cycle_index = kwargs["cycle_index"]
            phase = kwargs["phase"]
            if cycle_index == 0 and phase == "explore":
                return _result(
                    ResearchTurn(
                        status=RESEARCH_STATUS_CONTINUE,
                        actions=[
                            ResearchAction(
                                tool=TOOL_SEARCH_CORPUS,
                                query="許可",
                                docTypes=["law"],
                            )
                        ],
                    )
                )
            if cycle_index == 0 and phase == "deepen":
                return _result(
                    ResearchTurn(
                        status=RESEARCH_STATUS_CONTINUE,
                            actions=[
                                ResearchAction(
                                    tool=TOOL_FETCH_ARTICLES,
                                    articleIds=["law-a-article-12"],
                                ),
                                ResearchAction(
                                    tool=TOOL_EXPAND_GRAPH,
                                    articleIds=["law-a-article-12"],
                                    edgeTypes=["IMPLEMENTS"],
                                ),
                            ],
                    )
                )
            assert "law-b-article-3-paragraph-1" in catalog.content_unit_ids
            return _result(
                ResearchTurn(
                    status=RESEARCH_STATUS_READY,
                    selectedEvidence=[
                        ResearchEvidenceSelection(
                            contentUnitId="law-b-article-3-paragraph-1"
                        )
                    ],
                )
            )

        def integrate_legal_research_cycle(
            self,
            request,
            catalog,
            checkpoint,
            *,
            cycle_index,
            **kwargs,
        ):
            if cycle_index == 0:
                integrated = ResearchCheckpoint(
                    status=RESEARCH_STATUS_CONTINUE,
                    conclusion="配下法令の本文確認が必要",
                    evidenceIds=["law-a-article-12-paragraph-1"],
                    nextArticleIds=["law-b-article-3"],
                )
            else:
                integrated = ResearchCheckpoint(
                    status=RESEARCH_STATUS_READY,
                    conclusion="配下法令で具体的要件を確認した",
                    evidenceIds=["law-b-article-3-paragraph-1"],
                )
            return ResearchCheckpointResult(
                checkpoint=integrated,
                provider="test",
                model="test",
                latencyMs=1,
                inputTokens=1,
                outputTokens=1,
            )

    monkeypatch.setattr(settings, "llm_timeout_sec", 10)
    monkeypatch.setattr(settings, "agent_answer_reserve_sec", 10)
    monkeypatch.setattr(settings, "llm_research_active_budget_sec", 120)
    monkeypatch.setattr(settings, "llm_research_max_turns", 3)
    monkeypatch.setattr(settings, "llm_research_max_tool_calls", 18)
    os_client = ArticleAwareOpenSearch()

    outcome = run_llm_directed_research(
        request=AnswerRequest(question="許可の具体的要件は何ですか"),
        os_client=os_client,
        graph_client=FakeGraph(),
        llm_client=PendingTaskLLM(),
        deadline=perf_counter() + 150,
    )

    assert ["law-b-article-3"] in os_client.fetch_calls
    assert any(
        execution["phase"] == "resume_pending"
        and execution["articleIds"] == ["law-b-article-3"]
        for execution in outcome.trace["toolExecutions"]
    )
    assert outcome.trace["status"] == RESEARCH_STATUS_READY
    assert outcome.selected_content_unit_ids == (
        "law-b-article-3-paragraph-1",
    )


def test_integration_failure_keeps_last_stage_evidence_selection() -> None:
    catalog = EvidenceCatalog()
    catalog.add_results([_law_source()])
    checkpoint = ResearchCheckpoint(
        status=RESEARCH_STATUS_CONTINUE,
        conclusion="前回の状態",
    )
    stage_turn = ResearchTurn(
        status=RESEARCH_STATUS_READY,
        selectedEvidence=[
            ResearchEvidenceSelection(
                contentUnitId="law-a-article-12-paragraph-1",
                reason="直前に確認した根拠",
            )
        ],
    )

    recovered = _checkpoint_from_stage_selection(
        checkpoint,
        stage_turn,
        catalog,
    )

    assert recovered.status == RESEARCH_STATUS_CONTINUE
    assert recovered.conclusion == "前回の状態"
    assert recovered.evidenceIds == [
        "law-a-article-12-paragraph-1"
    ]


def test_answer_evidence_preserves_checkpoint_selection_only() -> None:
    catalog = EvidenceCatalog()
    catalog.add_results(
        [
            _law_source(),
            {
                "contentUnitId": "law-b-article-3-paragraph-1",
                "articleContentUnitId": "law-b-article-3",
                "documentId": "law-b",
                "docType": "law",
                "title": "テスト施行令",
                "heading": "第三条",
                "text": "具体的要件を定める。",
            },
            {
                "contentUnitId": "law-c-article-5",
                "articleContentUnitId": "law-c-article-5",
                "documentId": "law-c",
                "docType": "law",
                "title": "テスト府令",
                "heading": "第五条",
                "text": "手続を定める。",
            },
            {
                "contentUnitId": "law-b-article-3-paragraph-2",
                "articleContentUnitId": "law-b-article-3",
                "documentId": "law-b",
                "docType": "law",
                "title": "テスト施行令",
                "heading": "第三条第二項",
                "text": "例外を定める。",
            },
        ]
    )
    checkpoint = ResearchCheckpoint(
        status=RESEARCH_STATUS_READY,
        evidenceIds=[
            "law-c-article-5",
            "law-a-article-12-paragraph-1",
        ],
        openEvidenceIds=[
            "law-b-article-3-paragraph-1",
            "law-c-article-5",
            "law-b-article-3-paragraph-2",
        ],
        logicalStructure=ResearchLogicalStructure(
            issues=[
                ResearchIssueStructure(
                    issueId="permit",
                    status="verified",
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
                            verificationStatus="text_verified",
                            evidenceIds=[
                                "law-b-article-3-paragraph-1"
                            ],
                        ),
                        ResearchAuthorityNode(
                            nodeId="ordinance",
                            articleId="law-c-article-5",
                            verificationStatus="text_verified",
                            evidenceIds=["law-c-article-5"],
                        ),
                    ],
                    claims=[
                        ResearchClaimStructure(
                            claimId="claim",
                            status="verified",
                            authorityNodeIds=[
                                "act",
                                "order",
                                "ordinance",
                            ],
                        )
                    ],
                )
            ]
        ),
    )

    selected = _checkpoint_answer_content_ids(
        checkpoint,
        catalog,
        limit=16,
    )

    assert selected == (
        "law-c-article-5",
        "law-a-article-12-paragraph-1",
    )


def test_intermediate_integration_timeout_continues_to_next_cycle(
    monkeypatch,
) -> None:
    class RecoveringIntegrationLLM:
        supports_iterative_research = True

        def __init__(self) -> None:
            self.preferred_by_cycle: list[tuple[int | None, tuple[str, ...]]] = []
            self.integration_ids_by_cycle: list[
                tuple[int, tuple[str, ...]]
            ] = []

        def decide_legal_research_turn(
            self, request, catalog, history, timeout_sec, **kwargs
        ):
            self.preferred_by_cycle.append(
                (
                    kwargs.get("cycle_index"),
                    tuple(kwargs.get("preferred_content_ids") or ()),
                )
            )
            if kwargs.get("cycle_index") == 0:
                return _result(
                    ResearchTurn(
                        status=RESEARCH_STATUS_CONTINUE,
                        actions=[
                            ResearchAction(
                                tool=TOOL_SEARCH_CORPUS,
                                query="許可",
                                docTypes=["law"],
                            )
                        ],
                    )
                )
            return _result(
                ResearchTurn(
                    status=RESEARCH_STATUS_READY,
                    selectedEvidence=[
                        ResearchEvidenceSelection(
                            contentUnitId=(
                                "law-a-article-12-paragraph-1"
                            )
                        )
                    ],
                )
            )

        def integrate_legal_research_cycle(
            self,
            request,
            catalog,
            checkpoint,
            *,
            cycle_index,
            **kwargs,
        ):
            self.integration_ids_by_cycle.append(
                (
                    cycle_index,
                    tuple(kwargs.get("cycle_new_content_ids") or ()),
                )
            )
            if cycle_index == 0:
                raise requests.ReadTimeout("temporary integration timeout")
            return ResearchCheckpointResult(
                checkpoint=ResearchCheckpoint(
                    status=RESEARCH_STATUS_READY,
                    conclusion="許可が必要",
                    evidenceIds=[
                        "law-a-article-12-paragraph-1"
                    ],
                ),
                provider="test",
                model="test",
                latencyMs=1,
                inputTokens=1,
                outputTokens=1,
            )

    monkeypatch.setattr(settings, "llm_timeout_sec", 10)
    monkeypatch.setattr(settings, "agent_answer_reserve_sec", 10)
    monkeypatch.setattr(settings, "llm_research_active_budget_sec", 120)
    monkeypatch.setattr(settings, "llm_research_max_turns", 3)

    llm = RecoveringIntegrationLLM()
    outcome = run_llm_directed_research(
        request=AnswerRequest(question="許可の根拠は何ですか"),
        os_client=FakeOpenSearch(),
        graph_client=FakeGraph(),
        llm_client=llm,
        deadline=perf_counter() + 150,
    )

    assert outcome.trace["status"] == RESEARCH_STATUS_READY
    assert outcome.trace["cycleCount"] == 3
    assert outcome.trace["recoverableTimeouts"][0]["cycleIndex"] == 0
    assert "timeout" not in outcome.trace
    assert outcome.selected_content_unit_ids == (
        "law-a-article-12-paragraph-1",
    )
    assert any(
        cycle_index == 1
        and "law-a-article-12-paragraph-1" in preferred
        for cycle_index, preferred in llm.preferred_by_cycle
    )
    assert any(
        cycle_index == 1
        and "law-a-article-12-paragraph-1" in content_ids
        for cycle_index, content_ids in llm.integration_ids_by_cycle
    )


def test_active_loop_connects_llm_selection_to_answer_evidence(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_timeout_sec", 60)
    monkeypatch.setattr(settings, "agent_answer_reserve_sec", 30)
    monkeypatch.setattr(settings, "llm_research_active_budget_sec", 30)

    outcome = run_llm_directed_research(
        request=AnswerRequest(question="許可の根拠は何ですか"),
        os_client=FakeOpenSearch(),
        graph_client=FakeGraph(),
        llm_client=SearchThenReadyLLM(),
        deadline=perf_counter() + 280,
    )

    assert outcome.trace["mode"] == "active"
    assert outcome.trace["connectedToAnswer"] is True
    assert outcome.selected_content_unit_ids == (
        "law-a-article-12-paragraph-1",
    )


class InvalidThenCorrectedLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.histories: list[list[dict]] = []

    def decide_legal_research_turn(
        self, request, catalog, history, timeout_sec, **kwargs
    ):
        self.calls += 1
        self.histories.append(list(history))
        if kwargs.get("finalize_only"):
            return _result(
                ResearchTurn(
                    status=RESEARCH_STATUS_INSUFFICIENT,
                    selectedEvidence=[
                        ResearchEvidenceSelection(
                            contentUnitId="law-a-article-12-paragraph-1"
                        )
                    ],
                    missingEvidence=["追加の根拠"],
                )
            )
        if self.calls == 1:
            return _result(
                ResearchTurn(
                    status=RESEARCH_STATUS_CONTINUE,
                    actions=[
                        ResearchAction(
                            tool=TOOL_FETCH_ARTICLES,
                            articleIds=["invented-law-article-99"],
                        )
                    ],
                )
            )
        return _result(
            ResearchTurn(
                status=RESEARCH_STATUS_CONTINUE,
                actions=[
                    ResearchAction(
                        tool=TOOL_SEARCH_CORPUS,
                        query="根拠",
                        docTypes=["law"],
                    )
                ],
            )
        )


def test_invalid_article_id_is_not_executed_and_is_returned_for_correction(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "llm_timeout_sec", 10)
    monkeypatch.setattr(settings, "agent_answer_reserve_sec", 10)
    monkeypatch.setattr(settings, "llm_research_max_turns", 3)
    llm = InvalidThenCorrectedLLM()
    os_client = FakeOpenSearch()

    outcome = run_llm_directed_research_shadow(
        request=AnswerRequest(question="根拠は何ですか"),
        os_client=os_client,
        graph_client=FakeGraph(),
        llm_client=llm,
        deadline=perf_counter() + 100,
    )

    assert os_client.fetch_calls == []
    assert len(os_client.search_calls) == 1
    assert "unknown_article_id" in llm.histories[1][0]["validationErrors"][0]
    assert outcome.trace["turns"][0]["validation"]["valid"] is False


def test_no_shadow_budget_is_explicit_and_makes_no_llm_call(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_timeout_sec", 90)
    monkeypatch.setattr(settings, "agent_answer_reserve_sec", 60)
    llm = SearchThenReadyLLM()

    outcome = run_llm_directed_research_shadow(
        request=AnswerRequest(question="根拠は何ですか"),
        os_client=FakeOpenSearch(),
        graph_client=FakeGraph(),
        llm_client=llm,
        deadline=perf_counter() + 20,
    )

    assert outcome.trace["status"] == "not_started"
    assert outcome.trace["stopReason"] == "insufficient_shadow_time_budget"
    assert outcome.trace["incomplete"] is True
    assert llm.calls == 0


def test_llm_read_timeout_is_reported_as_timeout(monkeypatch) -> None:
    class TimeoutLLM:
        def decide_legal_research_turn(self, *args, **kwargs):
            raise requests.ReadTimeout("model did not respond")

    monkeypatch.setattr(settings, "llm_timeout_sec", 10)
    monkeypatch.setattr(settings, "agent_answer_reserve_sec", 10)

    outcome = run_llm_directed_research_shadow(
        request=AnswerRequest(question="根拠は何ですか"),
        os_client=FakeOpenSearch(),
        graph_client=FakeGraph(),
        llm_client=TimeoutLLM(),
        deadline=perf_counter() + 100,
    )

    assert outcome.trace["status"] == "timeout"
    assert outcome.trace["stopReason"] == "llm_timeout"
    assert outcome.trace["timeout"]["component"] == "llm_research_decision"
    assert outcome.trace["incomplete"] is True


def test_llm_connection_error_is_distinct_from_timeout(monkeypatch) -> None:
    class ConnectionFailureLLM:
        def decide_legal_research_turn(self, *args, **kwargs):
            raise requests.ConnectionError("remote closed connection")

    monkeypatch.setattr(settings, "llm_timeout_sec", 10)
    monkeypatch.setattr(settings, "agent_answer_reserve_sec", 10)

    outcome = run_llm_directed_research_shadow(
        request=AnswerRequest(question="根拠は何ですか"),
        os_client=FakeOpenSearch(),
        graph_client=FakeGraph(),
        llm_client=ConnectionFailureLLM(),
        deadline=perf_counter() + 100,
    )

    assert outcome.trace["status"] == "transport_error"
    assert outcome.trace["stopReason"] == "llm_connection_error"
    assert outcome.trace["transportError"]["component"] == (
        "llm_research_decision"
    )


def test_provider_credit_error_is_distinct_from_missing_evidence(
    monkeypatch,
) -> None:
    class CreditFailureLLM:
        supports_iterative_research = True

        def decide_legal_research_turn(self, *args, **kwargs):
            raise ValueError(
                "400: Your credit balance is too low. "
                "Please go to Plans & Billing to purchase credits."
            )

    monkeypatch.setattr(settings, "llm_timeout_sec", 10)
    monkeypatch.setattr(settings, "agent_answer_reserve_sec", 10)

    outcome = run_llm_directed_research_shadow(
        request=AnswerRequest(question="根拠は何ですか"),
        os_client=FakeOpenSearch(),
        graph_client=FakeGraph(),
        llm_client=CreditFailureLLM(),
        deadline=perf_counter() + 100,
    )

    assert outcome.trace["status"] == "provider_quota_error"
    assert outcome.trace["stopReason"] == "llm_provider_quota_error"
    assert outcome.trace["providerError"]["component"] == (
        "llm_research_explore"
    )


def test_gateway_fetch_and_graph_accept_only_cataloged_article() -> None:
    catalog = EvidenceCatalog()
    catalog.add_results([_law_source()])
    os_client = FakeOpenSearch()
    graph_client = FakeGraph()
    gateway = LegalResearchToolGateway(os_client, graph_client)

    fetch_action = ResearchAction(
        tool=TOOL_FETCH_ARTICLES,
        articleIds=["law-a-article-12"],
    )
    fetch = gateway.execute(
        fetch_action,
        catalog,
        user_clearance_level=2,
        timeout_sec=5,
    )
    graph = gateway.execute(
        ResearchAction(
            tool=TOOL_EXPAND_GRAPH,
            articleIds=["law-a-article-12"],
            edgeTypes=["IMPLEMENTS"],
        ),
        catalog,
        user_clearance_level=2,
        timeout_sec=5,
    )

    assert fetch.error is None
    assert graph.error is None
    assert fetch.auto_graph_path_count == 0
    assert fetch.new_article_ids == ()
    assert fetch.as_trace(fetch_action)["autoGraphArticleIds"] == []
    assert graph.new_article_ids == ("law-b-article-3",)
    assert graph.as_trace(
        ResearchAction(
            tool=TOOL_EXPAND_GRAPH,
            articleIds=["law-a-article-12"],
            edgeTypes=["IMPLEMENTS"],
        )
    )["graphRelations"] == [
        {
            "fromArticleId": "law-a-article-12",
            "fromDocumentId": "",
            "fromTitle": "",
            "fromHeading": "",
            "edgeType": "IMPLEMENTS",
            "toArticleId": "law-b-article-3",
            "toDocumentId": "",
            "toTitle": "",
            "toHeading": "",
            "relationSource": "subordinate_law_parent_reference",
            "relationConfidence": 0.98,
        }
    ]
    assert "law-b-article-3" in catalog.known_article_ids
    assert len(graph_client.calls) == 1
    assert all(call["max_depth"] == 1 for call in graph_client.calls)
    assert os_client.fetch_max_chunks == [
        settings.llm_research_max_chunks_per_article
    ]


def test_gateway_returns_relation_assertion_separately_from_trusted_paths() -> None:
    class GraphWithAssertion(FakeGraph):
        def relation_assertions_touching(
            self, article_ids: list[str], **kwargs
        ) -> list[dict]:
            return [
                {
                    "assertionId": "assertion-law-reference-1",
                    "fromArticleId": "law-a-article-12",
                    "toArticleId": "law-c-article-7",
                    "suggestedType": "IMPLEMENTS",
                    "status": "unverified",
                }
            ]

    catalog = EvidenceCatalog()
    catalog.add_results([_law_source()])
    result = LegalResearchToolGateway(
        FakeOpenSearch(), GraphWithAssertion()
    ).execute(
        ResearchAction(
            tool=TOOL_EXPAND_GRAPH,
            articleIds=["law-a-article-12"],
            edgeTypes=["IMPLEMENTS"],
        ),
        catalog,
        user_clearance_level=2,
        timeout_sec=5,
    )

    assert result.error is None
    assert result.result_count == 2
    assert len(result.graph_relations) == 1
    assert result.relation_assertions == (
        {
            "assertionId": "assertion-law-reference-1",
            "fromArticleId": "law-a-article-12",
            "toArticleId": "law-c-article-7",
            "suggestedType": "IMPLEMENTS",
            "assertionSource": None,
            "assertedByDocumentId": None,
            "sourceReferenceEdgeId": None,
            "sourceText": "",
            "delegationWordingDetected": False,
            "specificationWordingDetected": False,
            "status": "unverified",
        },
    )
    assert "law-c-article-7" in catalog.known_article_ids
    assert all(
        relation["toArticleId"] != "law-c-article-7"
        for relation in result.graph_relations
    )


def test_gateway_uses_offline_classification_as_navigation_only() -> None:
    class GraphWithClassifiedAssertion(FakeGraph):
        def relation_assertions_touching(
            self, article_ids: list[str], **kwargs
        ) -> list[dict]:
            return [
                {
                    "assertionId": "assertion-classified-1",
                    "fromArticleId": "law-a-article-12",
                    "toArticleId": "law-c-article-7",
                    "fromDocumentId": "law-a",
                    "toDocumentId": "law-c",
                    "suggestedType": "IMPLEMENTS",
                    "status": "llm_classified_implements",
                    "classifierModel": "haiku-test",
                }
            ]

    catalog = EvidenceCatalog()
    catalog.add_results([_law_source()])
    result = LegalResearchToolGateway(
        FakeOpenSearch(), GraphWithClassifiedAssertion()
    ).execute(
        ResearchAction(
            tool=TOOL_EXPAND_GRAPH,
            articleIds=["law-a-article-12"],
            edgeTypes=["IMPLEMENTS"],
        ),
        catalog,
        user_clearance_level=2,
        timeout_sec=5,
    )

    assert result.error is None
    assert result.relation_assertions == ()
    classified = next(
        relation
        for relation in result.graph_relations
        if relation.get("assertionId") == "assertion-classified-1"
    )
    assert classified["relationSource"] == "offline_llm_classification"
    assert "law-c-article-7" in catalog.known_article_ids


def test_gateway_fetch_does_not_select_graph_relation_automatically() -> None:
    catalog = EvidenceCatalog()
    catalog.add_results([_law_source()])
    graph_client = FakeGraph()
    gateway = LegalResearchToolGateway(FakeOpenSearch(), graph_client)

    result = gateway.execute(
        ResearchAction(
            tool=TOOL_FETCH_ARTICLES,
            articleIds=["law-a-article-12"],
        ),
        catalog,
        user_clearance_level=2,
        timeout_sec=5,
    )

    assert result.error is None
    assert graph_client.calls == []


def test_gateway_returns_existing_fetched_content_for_next_prompt() -> None:
    catalog = EvidenceCatalog()
    catalog.add_results([_law_source()])
    gateway = LegalResearchToolGateway(FakeOpenSearch(), FakeGraph())
    action = ResearchAction(
        tool=TOOL_FETCH_ARTICLES,
        articleIds=["law-a-article-12"],
    )

    result = gateway.execute(
        action,
        catalog,
        user_clearance_level=2,
        timeout_sec=5,
    )

    assert result.new_evidence_count == 0
    assert result.new_content_unit_ids == ()
    assert result.returned_content_unit_ids == (
        "law-a-article-12-paragraph-1",
    )
    assert result.as_trace(action)["returnedContentUnitIds"] == [
        "law-a-article-12-paragraph-1"
    ]


def test_gateway_allocates_enough_chunks_for_multiple_fetched_articles() -> None:
    catalog = EvidenceCatalog()
    catalog.add_results([_law_source()])
    catalog.add_graph_paths(
        [{"nodes": [{"graphNodeId": "law-b-article-3"}]}]
    )
    os_client = FakeOpenSearch()
    gateway = LegalResearchToolGateway(os_client, FakeGraph())

    result = gateway.execute(
        ResearchAction(
            tool=TOOL_FETCH_ARTICLES,
            articleIds=["law-a-article-12", "law-b-article-3"],
        ),
        catalog,
        user_clearance_level=2,
        timeout_sec=5,
    )

    assert result.error is None
    assert os_client.fetch_calls == [
        ["law-a-article-12"],
        ["law-b-article-3"],
    ]
    assert os_client.fetch_max_chunks == [
        settings.llm_research_max_chunks_per_article,
        settings.llm_research_max_chunks_per_article,
    ]


def test_gateway_rejects_untrusted_explicit_graph_relations() -> None:
    class UntrustedGraph(FakeGraph):
        def paths_from_many(self, article_ids: list[str], **kwargs) -> list[dict]:
            self.calls.append({"articleIds": article_ids, **kwargs})
            return [
                {
                    "nodes": [
                        {"graphNodeId": "law-a-article-12"},
                        {"graphNodeId": "law-b-article-3"},
                    ],
                    "edges": [
                        {
                            "edgeType": "IMPLEMENTS",
                            "relationSource": "subordinate_law_parent_reference",
                            "relationConfidence": 0.5,
                        }
                    ],
                }
            ]

    catalog = EvidenceCatalog()
    catalog.add_results([_law_source()])
    result = LegalResearchToolGateway(
        FakeOpenSearch(),
        UntrustedGraph(),
    ).execute(
        ResearchAction(
            tool=TOOL_EXPAND_GRAPH,
            articleIds=["law-a-article-12"],
            edgeTypes=["IMPLEMENTS"],
        ),
        catalog,
        user_clearance_level=2,
        timeout_sec=5,
    )

    assert result.error is None
    assert result.auto_graph_path_count == 0
    assert result.auto_graph_article_ids == ()
    assert "law-b-article-3" not in catalog.known_article_ids


def test_gateway_can_scope_search_to_a_cataloged_document() -> None:
    catalog = EvidenceCatalog()
    catalog.add_documents({"law-a": "テスト法"})
    os_client = FakeOpenSearch()
    gateway = LegalResearchToolGateway(os_client, FakeGraph())

    result = gateway.execute(
        ResearchAction(
            tool=TOOL_SEARCH_CORPUS,
            query="許可",
            documentIds=["law-a"],
            docTypes=["law"],
        ),
        catalog,
        user_clearance_level=2,
        timeout_sec=5,
    )

    assert result.error is None
    assert len(os_client.document_search_calls) == 1
    assert os_client.search_calls == []
    specs = os_client.document_search_calls[0][0]
    assert specs[0].top_k == settings.llm_research_document_search_top_k
    assert "law-a-article-12-paragraph-1" in catalog.content_unit_ids


def test_gateway_keeps_unscoped_search_at_the_smaller_global_limit() -> None:
    catalog = EvidenceCatalog()
    os_client = FakeOpenSearch()
    gateway = LegalResearchToolGateway(os_client, FakeGraph())

    result = gateway.execute(
        ResearchAction(
            tool=TOOL_SEARCH_CORPUS,
            query="許可",
            docTypes=["law"],
        ),
        catalog,
        user_clearance_level=2,
        timeout_sec=5,
    )

    assert result.error is None
    assert os_client.search_calls[0][2] == settings.llm_research_search_top_k


def test_gateway_keeps_one_representative_chunk_per_searched_article() -> None:
    class MultiChunkOpenSearch(FakeOpenSearch):
        def search_requirement_specs(
            self, specs, *, user_clearance_level: int, timeout_sec: float
        ) -> dict[str, list[dict]]:
            paragraph_1 = _law_source()
            paragraph_2 = {
                **_law_source(),
                "contentUnitId": "law-a-article-12-paragraph-2",
                "text": "厚生労働省令で定める。",
            }
            return {
                spec.requirement_id: [
                    {
                        "articleId": "law-a-article-12",
                        "chunks": [paragraph_1, paragraph_2],
                    }
                ]
                for spec in specs
            }

    catalog = EvidenceCatalog()
    catalog.add_documents({"law-a": "テスト法"})
    gateway = LegalResearchToolGateway(MultiChunkOpenSearch(), FakeGraph())

    result = gateway.execute(
        ResearchAction(
            tool=TOOL_SEARCH_CORPUS,
            query="許可",
            documentIds=["law-a"],
            docTypes=["law"],
        ),
        catalog,
        user_clearance_level=2,
        timeout_sec=5,
    )

    assert result.result_count == 1
    assert catalog.content_unit_ids == (
        "law-a-article-12-paragraph-1",
    )


class ContinueUntilFinalLLM:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def decide_legal_research_turn(
        self,
        request,
        catalog,
        history,
        timeout_sec,
        *,
        remaining_turns=None,
        remaining_tool_calls=None,
        finalize_only=False,
        preferred_content_ids=(),
    ):
        self.calls.append(
            {
                "remainingTurns": remaining_turns,
                "remainingToolCalls": remaining_tool_calls,
                "finalizeOnly": finalize_only,
            }
        )
        if finalize_only:
            return _result(
                ResearchTurn(
                    status=RESEARCH_STATUS_INSUFFICIENT,
                    selectedEvidence=[
                        ResearchEvidenceSelection(
                            contentUnitId="law-a-article-12-paragraph-1"
                        )
                    ],
                    missingEvidence=["省令の具体的要件"],
                )
            )
        return _result(
            ResearchTurn(
                status=RESEARCH_STATUS_CONTINUE,
                actions=[
                    ResearchAction(
                        tool=TOOL_SEARCH_CORPUS,
                        query="許可 根拠",
                        docTypes=["law"],
                    )
                ],
                selectedEvidence=(
                    [
                        ResearchEvidenceSelection(
                            contentUnitId="law-a-article-12-paragraph-1"
                        )
                    ]
                    if catalog.content_unit_ids
                    else []
                ),
            )
        )


def test_loop_reserves_the_last_turn_for_finalization(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_timeout_sec", 10)
    monkeypatch.setattr(settings, "agent_answer_reserve_sec", 10)
    monkeypatch.setattr(settings, "llm_research_active_budget_sec", 60)
    monkeypatch.setattr(settings, "llm_research_max_turns", 3)
    monkeypatch.setattr(settings, "llm_research_max_tool_calls", 8)
    llm = ContinueUntilFinalLLM()

    outcome = run_llm_directed_research(
        request=AnswerRequest(question="許可の根拠は何ですか"),
        os_client=FakeOpenSearch(),
        graph_client=FakeGraph(),
        llm_client=llm,
        deadline=perf_counter() + 100,
    )

    assert [call["finalizeOnly"] for call in llm.calls] == [
        False,
        False,
        True,
    ]
    assert llm.calls[-1]["remainingTurns"] == 1
    assert outcome.trace["status"] == RESEARCH_STATUS_INSUFFICIENT
    assert outcome.trace["stopReason"] == "llm_insufficient"
    assert outcome.selected_content_unit_ids == (
        "law-a-article-12-paragraph-1",
    )


def test_loop_prioritizes_evidence_fetched_after_the_previous_decision(
    monkeypatch,
) -> None:
    fetched_content_id = "law-a-article-12-paragraph-2"

    class FetchesNewParagraphOpenSearch(FakeOpenSearch):
        def get_by_article_ids(
            self,
            article_ids: list[str],
            user_clearance_level: int,
            max_chunks: int,
        ) -> list[dict]:
            self.fetch_calls.append(article_ids)
            self.fetch_max_chunks.append(max_chunks)
            return [
                {
                    **_law_source(),
                    "contentUnitId": fetched_content_id,
                    "text": "直前に取得した具体的要件。",
                }
            ]

    class SearchFetchFinalizeLLM:
        def __init__(self) -> None:
            self.calls = 0
            self.final_preferred_ids: tuple[str, ...] = ()

        def decide_legal_research_turn(
            self,
            request,
            catalog,
            history,
            timeout_sec,
            *,
            finalize_only=False,
            preferred_content_ids=(),
            **kwargs,
        ):
            self.calls += 1
            if self.calls == 1:
                return _result(
                    ResearchTurn(
                        status=RESEARCH_STATUS_CONTINUE,
                        actions=[
                            ResearchAction(
                                tool=TOOL_SEARCH_CORPUS,
                                query="許可",
                                docTypes=["law"],
                            )
                        ],
                    )
                )
            if self.calls == 2:
                return _result(
                    ResearchTurn(
                        status=RESEARCH_STATUS_CONTINUE,
                        actions=[
                            ResearchAction(
                                tool=TOOL_FETCH_ARTICLES,
                                articleIds=["law-a-article-12"],
                            )
                        ],
                    )
                )
            assert finalize_only is True
            self.final_preferred_ids = tuple(preferred_content_ids)
            return _result(
                ResearchTurn(
                    status=RESEARCH_STATUS_READY,
                    selectedEvidence=[
                        ResearchEvidenceSelection(
                            contentUnitId=fetched_content_id
                        )
                    ],
                )
            )

    monkeypatch.setattr(settings, "llm_timeout_sec", 10)
    monkeypatch.setattr(settings, "agent_answer_reserve_sec", 10)
    monkeypatch.setattr(settings, "llm_research_active_budget_sec", 60)
    monkeypatch.setattr(settings, "llm_research_max_turns", 3)
    llm = SearchFetchFinalizeLLM()

    outcome = run_llm_directed_research(
        request=AnswerRequest(question="許可の具体的要件は何ですか"),
        os_client=FetchesNewParagraphOpenSearch(),
        graph_client=FakeGraph(),
        llm_client=llm,
        deadline=perf_counter() + 100,
    )

    assert llm.final_preferred_ids[0] == fetched_content_id
    assert outcome.selected_content_unit_ids == (fetched_content_id,)


def test_loop_keeps_recent_direct_evidence_if_final_turn_times_out(
    monkeypatch,
) -> None:
    fetched_content_id = "law-a-article-12-paragraph-2"

    class FetchesNewParagraphOpenSearch(FakeOpenSearch):
        def get_by_article_ids(
            self,
            article_ids: list[str],
            user_clearance_level: int,
            max_chunks: int,
        ) -> list[dict]:
            return [
                {
                    **_law_source(),
                    "contentUnitId": fetched_content_id,
                    "text": "直前に取得した具体的要件。",
                }
            ]

    class FinalTimeoutLLM:
        def __init__(self) -> None:
            self.calls = 0

        def decide_legal_research_turn(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _result(
                    ResearchTurn(
                        status=RESEARCH_STATUS_CONTINUE,
                        actions=[
                            ResearchAction(
                                tool=TOOL_SEARCH_CORPUS,
                                query="許可",
                                docTypes=["law"],
                            )
                        ],
                    )
                )
            if self.calls == 2:
                return _result(
                    ResearchTurn(
                        status=RESEARCH_STATUS_CONTINUE,
                        actions=[
                            ResearchAction(
                                tool=TOOL_FETCH_ARTICLES,
                                articleIds=["law-a-article-12"],
                            )
                        ],
                    )
                )
            raise requests.ReadTimeout("final turn timed out")

    monkeypatch.setattr(settings, "llm_timeout_sec", 10)
    monkeypatch.setattr(settings, "agent_answer_reserve_sec", 10)
    monkeypatch.setattr(settings, "llm_research_active_budget_sec", 60)
    monkeypatch.setattr(settings, "llm_research_max_turns", 3)

    outcome = run_llm_directed_research(
        request=AnswerRequest(question="許可の具体的要件は何ですか"),
        os_client=FetchesNewParagraphOpenSearch(),
        graph_client=FakeGraph(),
        llm_client=FinalTimeoutLLM(),
        deadline=perf_counter() + 100,
    )

    assert outcome.trace["status"] == "partial"
    assert outcome.trace["stopReason"] == "llm_timeout"
    assert outcome.trace["recentDirectEvidenceAddedForPartialAnswer"] == [
        fetched_content_id
    ]
    assert outcome.selected_content_unit_ids == (fetched_content_id,)


def test_loop_recovers_last_valid_selection_if_finalization_is_invalid(
    monkeypatch,
) -> None:
    class InvalidFinalLLM(ContinueUntilFinalLLM):
        def decide_legal_research_turn(self, *args, finalize_only=False, **kwargs):
            if finalize_only:
                self.calls.append({"finalizeOnly": True})
                return _result(
                    ResearchTurn(
                        status=RESEARCH_STATUS_CONTINUE,
                        actions=[
                            ResearchAction(
                                tool=TOOL_SEARCH_CORPUS,
                                query="さらに調査",
                                docTypes=["law"],
                            )
                        ],
                    )
                )
            return super().decide_legal_research_turn(
                *args,
                finalize_only=finalize_only,
                **kwargs,
            )

    monkeypatch.setattr(settings, "llm_timeout_sec", 10)
    monkeypatch.setattr(settings, "agent_answer_reserve_sec", 10)
    monkeypatch.setattr(settings, "llm_research_active_budget_sec", 60)
    monkeypatch.setattr(settings, "llm_research_max_turns", 3)
    llm = InvalidFinalLLM()

    outcome = run_llm_directed_research(
        request=AnswerRequest(question="許可の根拠は何ですか"),
        os_client=FakeOpenSearch(),
        graph_client=FakeGraph(),
        llm_client=llm,
        deadline=perf_counter() + 100,
    )

    assert outcome.trace["status"] == "partial"
    assert outcome.trace["selectionRecoveredFromLastValidTurn"] is True
    assert outcome.selected_content_unit_ids == (
        "law-a-article-12-paragraph-1",
    )


def test_gateway_rejects_uncataloged_ids_even_without_loop_validation() -> None:
    catalog = EvidenceCatalog()
    os_client = FakeOpenSearch()
    graph_client = FakeGraph()
    gateway = LegalResearchToolGateway(os_client, graph_client)

    result = gateway.execute(
        ResearchAction(
            tool=TOOL_FETCH_ARTICLES,
            articleIds=["invented-law-article-99"],
        ),
        catalog,
        user_clearance_level=2,
        timeout_sec=5,
    )

    assert "unknown articleIds" in str(result.error)
    assert os_client.fetch_calls == []
    assert graph_client.calls == []
