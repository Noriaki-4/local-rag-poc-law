import re
from collections.abc import Iterable
from time import perf_counter
from typing import Any

import requests

from .config import settings
from .evidence_selector import (
    AspectEvidence,
    AspectEvidenceMatrix,
    aspect_queries_by_article,
    select_issue_covered_context,
)
from .evidence_selector import (
    article_id as evidence_article_id,
)
from .evidence_selector import (
    content_id as evidence_content_id,
)
from .graph_client import GraphClient
from .layered_shadow import run_layered_retrieval
from .llm import LLMClient, citation_context_stats
from .llm_directed_research import (
    TOOL_SEARCH_CORPUS,
    EvidenceCatalog,
    ResearchAction,
)
from .llm_research_loop import (
    run_llm_directed_research,
    run_llm_directed_research_shadow,
)
from .llm_research_tools import LegalResearchToolGateway
from .models import AnswerRequest, AnswerResponse, Citation
from .opensearch_client import OpenSearchClient
from .reranker import RerankerClient
from .research_case_store import InMemoryCaseStore
from .seed import _japanese_number_to_int

REFERENCE_CUES = (
    "前条",
    "次条",
    "同条",
    "前項",
    "同項",
    "同号",
    "ただし",
    "定義",
    "準用",
    "除く",
    "政令で定める",
    "内閣府令で定める",
    "省令で定める",
)
# 項・号内の参照。条ノードから兄弟の項・号を辿れば解決できる参照語。
SIBLING_REFERENCE_CUES = ("前項", "同項", "次項", "前二項", "前三項", "各項", "前各項", "前号", "同号", "次号", "各号", "前各号")
ARTICLE_REFERENCE_PATTERN = re.compile(r"第[一二三四五六七八九十百千〇零\d]+条")
# 質問・選択肢中の「第N条(のM)」抽出用。seed.py のパターンと異なり「〜法第N条」も対象にする。
QUESTION_ARTICLE_PATTERN = re.compile(
    r"第([0-9一二三四五六七八九十百千〇零]+)条((?:の[0-9一二三四五六七八九十百千〇零]+)*)"
)
QUESTION_PROVISION_PATTERN = re.compile(
    r"第([0-9一二三四五六七八九十百千〇零]+)条"
    r"((?:の[0-9一二三四五六七八九十百千〇零]+)*)"
    r"(?:第([0-9一二三四五六七八九十百千〇零]+)項)?"
    r"(?:第([0-9一二三四五六七八九十百千〇零]+)号)?"
)
FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
HIERARCHY_EDGE_TYPE = "HAS_CONTENT_UNIT"
HIERARCHY_MAX_DEPTH = 2
DIRECT_CHUNKS_PER_ARTICLE = 3
# ガイドライン文書 -EXPLAINS-> 条文 を辿るエッジ種別。
GUIDANCE_EXPLAINS_EDGE_TYPE = "EXPLAINS"
# 1回のguidance_explainsで確実投入する条文の上限。大きい対応表(製品区分×節)が
# rerank枠(rerank_top_k)を埋め尽くしてガイドライン本文を押し出すのを防ぐ。
# 条文はEXPLAINSエッジの出現順(=対応表の記載順、先頭に主要条文が来る)で切る。
GUIDANCE_EXPLAINS_MAX_ARTICLES = 6
# 質問に明示された法令ごとに確保する候補数と、対象とする法令数の上限。
NAMED_LAW_COVERAGE_PER_LAW = 5
NAMED_LAW_COVERAGE_MAX_LAWS = 3
# 再ランク後の上位枠から少しだけ外れた別法令を救済する範囲と件数。
# 全候補文書を無条件に昇格させると、無関係な法令まで回答候補へ混ざる。
FINAL_LAW_DIVERSITY_MAX_SLACK = 4
FINAL_LAW_DIVERSITY_MAX_ADDITIONS = 3
# 親法参照・委任・準用の高信頼Graph経路から必須候補にする上限。
# Graph候補全体を固定すると無関係な参照でrerank枠を占有するため、少数に限定する。
GRAPH_RELATION_PIN_MAX = 4
# Graph由来の候補を再ランカー順位から救済する範囲。これより下位は、関係自体が
# 正しくても質問との関連性が低い可能性が高いため、必須扱いしない。
GRAPH_RELATION_RERANK_SLACK = 4
# 最終引用枠で確保する高信頼Graph根拠の上限。複数を固定するとLLMが選んだ
# 直接根拠を押し出すため、法令間の接続を示す最優先の1件だけに限定する。
GRAPH_CITATION_CLOSURE_MAX = 1
# 分解クエリごとの代表候補をLLMコンテキストへ残す上限。質問全文だけの再ランクで
# 一部論点の条文が押し出されるのを防ぎつつ、低順位候補による枠の占有を抑える。
ASPECT_COVERAGE_MAX_QUERIES = 4
ASPECT_COVERAGE_MAX_ITEMS = 12
ASPECT_COVERAGE_PER_QUERY = 3
ASPECT_COVERAGE_MAX_QUERY_RANK = 20
FOLLOW_UP_ASPECT_COVERAGE_MAX_ITEMS = 2
FINAL_RERANK_MAX_ADDITIONS = 2
# Reviewerが選んだ修正・追加調査を実行できる上限。意味上の終了判断は
# Reviewerが行い、プログラムはこの回数・時間境界だけを強制する。
GROUNDING_REMEDIATION_MAX_ROUNDS = 2

# 利用者やプランナーが使いやすい略称を、seed済みの正式名称へ対応付ける。
# 検索結果の固定には使わず、法令内の補助検索を追加するためだけに使う。
LAW_TITLE_ALIASES: dict[str, tuple[str, ...]] = {
    "金融商品取引法": ("金商法",),
    "金融商品取引法施行令": ("金商法施行令",),
    "企業内容等の開示に関する内閣府令": ("開示府令",),
    "発行者以外の者による株券等の公開買付けの開示に関する内閣府令": ("公開買付府令",),
    "金融商品取引法第二条に規定する定義に関する内閣府令": ("定義府令",),
    "医薬品、医療機器等の品質、有効性及び安全性の確保等に関する法律": ("薬機法",),
    "医薬品、医療機器等の品質、有効性及び安全性の確保等に関する法律施行規則": (
        "薬機法施行規則",
    ),
}


class AgentService:
    def __init__(
        self,
        os_client: OpenSearchClient,
        graph_client: GraphClient,
        llm_client: LLMClient,
        reranker_client: RerankerClient | None = None,
    ) -> None:
        self.os_client = os_client
        self.graph_client = graph_client
        self.llm_client = llm_client
        self.reranker_client = reranker_client or RerankerClient()

    def answer(self, request: AnswerRequest) -> AnswerResponse:
        started = perf_counter()
        deadline = started + settings.agent_max_wall_time_sec
        candidate_top_k = max(request.candidateTopK or settings.agent_candidate_top_k, request.topK)
        rerank_top_k = max(
            request.topK,
            min(request.rerankTopK or settings.agent_rerank_top_k, candidate_top_k),
        )
        route: list[str] = []
        max_research_llm_calls = settings.llm_research_max_turns * 3
        max_answer_llm_calls = (
            2 * (1 + GROUNDING_REMEDIATION_MAX_ROUNDS)
            if settings.agent_llm_directed_retrieval and not request.choices
            else 2
        )
        max_grounding_review_llm_calls = (
            2 * (1 + GROUNDING_REMEDIATION_MAX_ROUNDS)
            if settings.agent_llm_directed_retrieval and not request.choices
            else 0
        )
        max_llm_calls = (
            max_research_llm_calls
            + max_answer_llm_calls
            + max_grounding_review_llm_calls
            if settings.agent_llm_directed_retrieval
            else settings.agent_max_llm_calls
        )
        max_total_tool_calls = (
            settings.llm_research_max_tool_calls
            if settings.agent_llm_directed_retrieval
            else settings.agent_max_total_tool_calls
        )
        trace: dict[str, Any] = {
            "rounds": [],
            "inputType": self._input_type(request),
            "limits": {
                "maxQueries": settings.agent_max_queries,
                "maxRetryRounds": settings.agent_max_retry_rounds,
                "maxTotalToolCalls": max_total_tool_calls,
                "maxGraphHop": settings.agent_max_graph_hop,
                "maxGraphPaths": settings.agent_max_graph_paths,
                "maxWallTimeSec": settings.agent_max_wall_time_sec,
                "candidateTopK": candidate_top_k,
                "rerankTopK": rerank_top_k,
                "citationTopK": request.topK,
                "maxLlmCalls": max_llm_calls,
                "maxResearchLlmCalls": (
                    max_research_llm_calls
                    if settings.agent_llm_directed_retrieval
                    else 0
                ),
                "maxAnswerLlmCalls": max_answer_llm_calls,
                "maxGroundingReviewLlmCalls": (
                    max_grounding_review_llm_calls
                ),
                "answerReserveSec": settings.agent_answer_reserve_sec,
                "issueCoverageSelection": settings.agent_issue_coverage_selection,
                "issueCoverageShadow": settings.agent_issue_coverage_shadow,
            },
        }
        if settings.agent_llm_directed_retrieval:
            return self._answer_with_llm_directed_research(
                request,
                started,
                deadline,
                route,
                trace,
            )
        evidence: dict[str, dict[str, Any]] = {}
        graph_paths: list[dict[str, Any]] = []
        inherited_aspects_by_content_id: dict[str, set[str]] = {}
        executed_queries: list[str] = []
        tool_calls = 0

        if request.pattern in {"pattern_4_deepsearch_partial", "pattern_4_deepsearch"}:
            _append_route(route, "instruction_policy")
            trace["instruction"] = "Built-in legal answer and citation policy applied."

        queries, planner_graph_required = self._plan_queries(request, trace, deadline)
        if request.pattern in {"pattern_3_controlled_agentic_rag", "pattern_4_deepsearch_partial", "pattern_4_deepsearch"}:
            _append_route(route, "query_decomposition")

        broad_query = _search_query(request)
        for query_index, query in enumerate(queries):
            if not _can_call_tool(tool_calls, deadline):
                break
            results, source_result_counts = self._search_evidence(query, candidate_top_k, request.userClearanceLevel)
            tool_calls += 1
            executed_queries.append(query)
            search_phase = "broad_search" if query_index == 0 and query == broad_query else "initial_search"
            new_count = _merge_search_results(evidence, results, query, search_phase)
            _append_route(route, "law_search_tool")
            trace["rounds"].append(
                {
                    "round": 0,
                    "tool": "law_search_tool",
                    "phase": search_phase,
                    "query": query,
                    "resultCount": len(results),
                    "newContentUnitCount": new_count,
                    "sourceResultCounts": source_result_counts,
                    "useBm25": settings.agent_use_bm25,
                    "useVector": settings.agent_use_vector,
                }
            )

        all_focused_queries = [
            query for query in dict.fromkeys(executed_queries) if query != broad_query
        ]
        focused_queries = all_focused_queries[:ASPECT_COVERAGE_MAX_QUERIES]
        trace["selectedAspectQueries"] = focused_queries
        trace["ignoredAspectQueries"] = [
            {"query": query, "reason": "max_aspects"}
            for query in all_focused_queries[ASPECT_COVERAGE_MAX_QUERIES:]
        ]
        initial_aspect_matrix, aspect_rerank_trace = self._rerank_aspect_queries(
            evidence,
            focused_queries,
            ASPECT_COVERAGE_MAX_QUERIES,
            ASPECT_COVERAGE_MAX_QUERY_RANK,
            deadline,
        )
        aspect_content_ids = _mark_aspect_representatives(
            evidence,
            focused_queries,
            ASPECT_COVERAGE_MAX_ITEMS,
            ASPECT_COVERAGE_MAX_QUERY_RANK,
            query_orders=initial_aspect_matrix.orders_by_query,
            per_query=ASPECT_COVERAGE_PER_QUERY,
        )
        trace["aspectCoverageContentUnitIds"] = aspect_content_ids
        trace["aspectReranker"] = aspect_rerank_trace

        if _can_call_tool(tool_calls, deadline):
            article_new = self._inject_article_references(request, evidence, trace)
            if article_new is not None:
                tool_calls += 1
                _append_route(route, "article_direct_lookup")

        if _can_call_tool(tool_calls, deadline):
            guidance_new = self._inject_guidance_explained_articles(
                request, evidence, trace, rerank_top_k, graph_paths
            )
            if guidance_new is not None:
                tool_calls += 1
                _append_route(route, "guidance_explains_lookup")

        evaluator_queries: list[str] = []
        evaluator_graph_required = False
        evaluator_stop = False
        if request.pattern in {"pattern_3_controlled_agentic_rag", "pattern_4_deepsearch_partial", "pattern_4_deepsearch"}:
            _append_route(route, "evidence_evaluator")
            evaluator_queries, evaluator_graph_required, evaluator_stop = self._evaluate_evidence(
                request,
                evidence,
                trace,
                deadline,
                rerank_top_k,
            )

        graph_required = evaluator_graph_required or (
            not evaluator_stop and self._should_expand_graph(request, evidence, planner_graph_required)
        )
        if graph_required and _can_call_tool(tool_calls, deadline):
            paths, new_count = self._expand_graph(
                request,
                evidence,
                rerank_top_k,
                candidate_top_k,
                initial_aspect_matrix,
                inherited_aspects_by_content_id,
            )
            tool_calls += 1
            graph_paths.extend(paths)
            _append_route(route, "graph_search_tool")
            trace["rounds"].append(
                {
                    "round": 0,
                    "tool": "graph_search_tool",
                    "resultCount": len(paths),
                    "newContentUnitCount": new_count,
                    "maxDepth": settings.agent_max_graph_hop,
                }
            )

        retry_rounds = 0
        stop_reason = "baseline_or_rule_based_complete"
        if request.pattern in {"pattern_3_controlled_agentic_rag", "pattern_4_deepsearch_partial", "pattern_4_deepsearch"}:
            follow_up_queries = [] if evaluator_stop else evaluator_queries
            if not follow_up_queries and not evaluator_stop:
                follow_up_queries = _follow_up_queries(request, evidence, queries)
            trace["evidenceEvaluation"] = {
                "candidateCount": len(evidence),
                "followUpQueries": follow_up_queries[: settings.agent_max_retry_rounds],
            }
            stop_reason = "evidence_sufficient" if not follow_up_queries else "retry_limit_reached"
            for retry_round, query in enumerate(
                follow_up_queries[: settings.agent_max_retry_rounds],
                start=1,
            ):
                if not _can_call_tool(tool_calls, deadline):
                    stop_reason = "tool_or_time_budget_exhausted"
                    break
                results, source_result_counts = self._search_evidence(query, candidate_top_k, request.userClearanceLevel)
                tool_calls += 1
                executed_queries.append(query)
                retry_rounds = retry_round
                new_count = _merge_search_results(evidence, results, query, "follow_up_search")
                direct_content_ids = self._inject_article_references(
                    request,
                    evidence,
                    trace,
                    text_override=query,
                    round_number=retry_round,
                    source="follow_up_article_reference",
                )
                for direct_rank, content_unit_id in enumerate(
                    direct_content_ids or [],
                    start=1,
                ):
                    item = evidence.get(content_unit_id)
                    if item is None:
                        continue
                    query_ranks = item.setdefault("queryRanks", {})
                    query_ranks[query] = min(
                        int(query_ranks.get(query, direct_rank)),
                        direct_rank,
                    )
                follow_up_matrix, follow_up_rerank_trace = self._rerank_aspect_queries(
                    evidence,
                    [query],
                    1,
                    ASPECT_COVERAGE_MAX_QUERY_RANK,
                    deadline,
                )
                follow_up_aspect_ids = _mark_aspect_representatives(
                    evidence,
                    [query],
                    FOLLOW_UP_ASPECT_COVERAGE_MAX_ITEMS,
                    ASPECT_COVERAGE_MAX_QUERY_RANK,
                    query_orders=follow_up_matrix.orders_by_query,
                    per_query=FOLLOW_UP_ASPECT_COVERAGE_MAX_ITEMS,
                    dedupe_articles=False,
                )
                aspect_content_ids.extend(
                    content_unit_id
                    for content_unit_id in follow_up_aspect_ids
                    if content_unit_id not in aspect_content_ids
                )
                trace["aspectCoverageContentUnitIds"] = aspect_content_ids
                trace["aspectReranker"].extend(follow_up_rerank_trace)
                _append_route(route, "follow_up_search")
                trace["rounds"].append(
                    {
                        "round": retry_round,
                        "tool": "law_search_tool",
                        "phase": "follow_up",
                        "query": query,
                        "resultCount": len(results),
                        "newContentUnitCount": new_count,
                        "sourceResultCounts": source_result_counts,
                        "useBm25": settings.agent_use_bm25,
                        "useVector": settings.agent_use_vector,
                    }
                )
                if new_count == 0:
                    stop_reason = "no_new_content_units"
                    break
                if self._should_expand_graph(request, evidence, planner_graph_required) and _can_call_tool(tool_calls, deadline):
                    paths, graph_new_count = self._expand_graph(
                        request,
                        evidence,
                        rerank_top_k,
                        candidate_top_k,
                        initial_aspect_matrix,
                        inherited_aspects_by_content_id,
                    )
                    tool_calls += 1
                    graph_paths = _dedupe_graph_paths([*graph_paths, *paths])
                    _append_route(route, "graph_search_tool")
                    trace["rounds"].append(
                        {
                            "round": retry_round,
                            "tool": "graph_search_tool",
                            "resultCount": len(paths),
                            "newContentUnitCount": graph_new_count,
                            "maxDepth": settings.agent_max_graph_hop,
                        }
                    )
                if len(evidence) >= max(request.topK * 2, request.topK + 2):
                    stop_reason = "evidence_sufficient"
                    break

        rerank_candidate_top_k = min(
            len(evidence),
            max(rerank_top_k, settings.rerank_candidate_top_k),
        )
        # 再ランカー入力は資料の多様性を確保する一方、再ランカーを使えない場合の
        # フォールバックは多様化前のスコア順を使う。両者を共用すると、入力用に
        # 拾った低順位の資料がそのまま最終候補へ昇格してしまう。
        rerank_candidates = _fusion_ranked_evidence(evidence, rerank_candidate_top_k)
        fusion_ranked = _score_ranked_evidence(evidence, rerank_top_k)
        trace["candidatePoolContentUnitIds"] = list(evidence)
        trace["fusionTopContentUnitIds"] = [item["document"]["contentUnitId"] for item in fusion_ranked]
        # 候補プール→再ランカー入力→再ランク後、のどこで根拠が落ちたかを追えるようにする。
        trace["rerankCandidateContentUnitIds"] = [
            item["document"]["contentUnitId"] for item in rerank_candidates
        ]
        _append_route(route, "evidence_merge")
        remaining = int(deadline - perf_counter())
        if remaining > 1:
            rerank_result = self.reranker_client.rerank(
                _search_query(request),
                rerank_candidates,
                timeout_sec=max(1, min(settings.rerank_timeout_sec, remaining)),
            )
        else:
            rerank_result = None
        if rerank_result is None:
            final_ranked = fusion_ranked
            trace["reranker"] = {
                "used": False,
                "provider": settings.rerank_provider,
                "fallback": "time_budget",
            }
        else:
            old_final_ranked = (
                _pin_ranked_evidence(rerank_result.items, rerank_top_k)
                if rerank_result.used
                else fusion_ranked
            )
            final_ranked = old_final_ranked
            trace["reranker"] = {
                "used": rerank_result.used,
                "provider": rerank_result.provider,
                "model": rerank_result.model,
                "latencyMs": rerank_result.latency_ms,
                "errorCode": (
                    "reranker_error" if rerank_result.error else None
                ),
                "fallback": None if rerank_result.used else "fusion_ranking",
                "candidateCount": len(rerank_candidates),
                "scores": rerank_result.scores,
            }
            if rerank_result.used:
                _append_route(route, "evidence_reranker")
            trace["rerankedOrderContentUnitIds"] = [
                item["document"]["contentUnitId"] for item in rerank_result.items
            ]
            if (
                rerank_result.used
                and (
                    settings.agent_issue_coverage_selection
                    or settings.agent_issue_coverage_shadow
                )
            ):
                final_ranked = self._apply_issue_coverage_selection(
                    old_final_ranked,
                    rerank_result.items,
                    initial_aspect_matrix,
                    inherited_aspects_by_content_id,
                    rerank_top_k,
                    deadline,
                    trace,
                )
        raw_reranker_top_ids = {
            item["document"]["contentUnitId"]
            for item in (
                rerank_result.items[:rerank_top_k]
                if rerank_result is not None and rerank_result.used
                else final_ranked
            )
        }
        trace["rerankerTopContentUnitIds"] = [
            item["document"]["contentUnitId"] for item in final_ranked
        ]
        final_ranked = self._apply_layered_legal_retrieval(
            request,
            final_ranked,
            deadline,
            trace,
            route,
        )
        self._apply_llm_directed_retrieval_shadow(
            request,
            deadline,
            trace,
        )
        answer_candidates = _citations_from_items(final_ranked)
        structural_citation_ids = _graph_closure_citations_for_request(
            request,
            final_ranked,
            raw_reranker_top_ids,
            GRAPH_CITATION_CLOSURE_MAX,
        )
        trace["structuralCitationIds"] = structural_citation_ids
        _append_route(route, "answer_composer")
        answer_text, predicted_answer, judgements, assessment_citation_ids = self._compose_answer(
            request,
            route,
            answer_candidates,
            trace,
            deadline,
            None,
        )
        citations = _select_final_citations(
            answer_candidates,
            assessment_citation_ids,
            request.topK,
            answer_text=answer_text,
            expand_answer_citations=not request.choices,
            structural_citation_ids=structural_citation_ids,
        )
        trace["rerankedContentUnitIds"] = [
            citation.contentUnitId for citation in citations if citation.contentUnitId
        ]
        assessments = trace.get("llm", {}).get("choiceAssessments") or {}
        trace["choiceEvidence"] = {
            label: assessment.get("citationIds", [])
            for label, assessment in assessments.items()
        } or _choice_evidence_matrix(request, citations)
        trace["toolCallCount"] = tool_calls
        trace["retryRounds"] = retry_rounds
        trace["stopReason"] = stop_reason
        trace["elapsedMs"] = int((perf_counter() - started) * 1000)
        trace["retrievedContentUnitIds"] = list(evidence)
        trace["graphExpandedContentUnitIds"] = [
            content_unit_id
            for content_unit_id, item in evidence.items()
            if item.get("introducedBy") == "graph_expansion"
        ]
        trace["retrievedGraphNodeIds"] = _graph_node_ids(graph_paths)
        trace["retrievedGraphEdgeIds"] = _graph_edge_ids(graph_paths)
        trace["llmCallCount"] = sum(
            int(trace.get(key, {}).get("attemptCount") or 0)
            for key in ["planner", "evaluator", "llm"]
        ) + int(
            (
                trace.get("layeredLegalRetrieval", {}).get("issuePlanner", {})
            ).get("attemptCount")
            or 0
        ) + int(
            trace.get("llmDirectedLegalRetrieval", {}).get("llmCallCount") or 0
        )

        return AnswerResponse(
            pattern=request.pattern,
            route=route,
            answer=answer_text,
            predictedAnswer=predicted_answer,
            choiceJudgements=judgements,
            citations=citations,
            graphPaths=graph_paths,
            trace=trace,
        )

    def _answer_with_llm_directed_research(
        self,
        request: AnswerRequest,
        started: float,
        deadline: float,
        route: list[str],
        trace: dict[str, Any],
    ) -> AnswerResponse:
        """検索・取得以外の法的判断をLLMへ集約する単独回答経路。"""
        _append_route(route, "llm_directed_legal_research")
        try:
            outcome = run_llm_directed_research(
                request=request,
                os_client=self.os_client,
                graph_client=self.graph_client,
                llm_client=self.llm_client,
                deadline=deadline,
            )
        except Exception:  # noqa: BLE001 - 旧方式へ黙って切り替えない
            trace["llmDirectedLegalRetrieval"] = {
                "enabled": True,
                "mode": "active",
                "connectedToAnswer": True,
                "status": "internal_error",
                "stopReason": "active_internal_error",
                "incomplete": True,
                "errorCode": "active_internal_error",
                "llmCallCount": 0,
                "toolCallCount": 0,
            }
            trace["elapsedMs"] = int((perf_counter() - started) * 1000)
            return AnswerResponse(
                pattern=request.pattern,
                route=route,
                answer="法令調査処理で内部エラーが発生したため、根拠付きで回答できません。",
                predictedAnswer=None,
                choiceJudgements=None,
                citations=[],
                graphPaths=[],
                trace=trace,
            )

        research_trace = outcome.trace
        trace["llmDirectedLegalRetrieval"] = research_trace
        trace["retrievedContentUnitIds"] = research_trace.get(
            "availableEvidenceContentUnitIds", []
        )
        # ProjectorはCheckpointでLLMが選んだID順を決定的に展開するだけで、
        # 法的関連性による採否や並べ替えを行わない。最終的に使う根拠は
        # Main LLMが回答と同じ構造化判断で選ぶ。
        projected_answer_evidence = list(outcome.selected_evidence)
        answer_candidates = _citations_from_items(
            [
                {
                    "document": item,
                    "evidenceLane": (
                        "law" if item.get("docType") == "law" else "guidance"
                    ),
                }
                for item in projected_answer_evidence
            ]
        )
        trace["answerCandidateContentUnitIds"] = [
            citation.contentUnitId
            for citation in answer_candidates
            if citation.contentUnitId
        ]
        if not answer_candidates:
            status = str(research_trace.get("status") or "")
            if status == "timeout":
                answer = (
                    "法令調査LLMがタイムアウトしたため、必要な根拠を確認できず回答を中止しました。"
                )
            elif status == "transport_error":
                answer = (
                    "法令調査LLMへの接続に失敗したため、必要な根拠を確認できず回答を中止しました。"
                )
            elif status == "provider_quota_error":
                answer = (
                    "法令調査LLMのクレジットまたは利用枠が不足しているため、"
                    "調査を完了できませんでした。検索結果不足ではありません。"
                )
            elif status == "not_started":
                answer = (
                    "回答生成時間を確保すると法令調査の時間が残らないため、根拠付きで回答できません。"
                )
            else:
                answer = (
                    "投入済みデータから回答に必要な法令本文を確認できなかったため、"
                    "根拠付きで回答できません。"
                )
            trace["toolCallCount"] = int(research_trace.get("toolCallCount") or 0)
            trace["llmCallCount"] = int(research_trace.get("llmCallCount") or 0)
            trace["stopReason"] = research_trace.get("stopReason")
            trace["elapsedMs"] = int((perf_counter() - started) * 1000)
            return AnswerResponse(
                pattern=request.pattern,
                route=route,
                answer=answer,
                predictedAnswer=None,
                choiceJudgements=None,
                citations=[],
                graphPaths=[],
                trace=trace,
            )

        checkpoint = research_trace.get("checkpoint") or {}
        logical_structure = checkpoint.get("logicalStructure") or {}
        answer_contract_issues = [
            {
                "issueId": str(issue.get("issueId") or ""),
                "question": str(issue.get("question") or ""),
            }
            for issue in logical_structure.get("issues") or []
            if isinstance(issue, dict) and issue.get("issueId")
        ]
        if not answer_contract_issues:
            answer_contract_issues = [
                {"issueId": "overall", "question": request.question}
            ]
        research_context = {
            "stopReason": research_trace.get("stopReason"),
            "incomplete": bool(research_trace.get("incomplete")),
            "answerContract": {
                "version": "issue-grounding-v1",
                "issues": answer_contract_issues,
                "availableCitationIds": [
                    citation.contentUnitId
                    for citation in answer_candidates
                    if citation.contentUnitId
                ],
                "maxSelectedCitations": request.topK,
            },
        }
        _append_route(route, "answer_composer")
        (
            answer_text,
            predicted_answer,
            judgements,
            reviewed_citation_ids,
        ) = self._compose_answer(
            request,
            route,
            answer_candidates,
            trace,
            deadline,
            None,
            research_context=research_context,
        )
        citations = _select_final_citations(
            answer_candidates,
            reviewed_citation_ids,
            request.topK,
            answer_text=answer_text,
            expand_answer_citations=False,
            # active経路の最終引用はMain LLMが構造化citationIdsで選ぶ。
            # 回答契約やReviewer検証に失敗した際、候補を自動補充しない。
            fill_remaining=False,
        )
        trace["rerankedContentUnitIds"] = [
            citation.contentUnitId for citation in citations if citation.contentUnitId
        ]
        trace["toolCallCount"] = int(research_trace.get("toolCallCount") or 0)
        trace["llmCallCount"] = (
            int(research_trace.get("llmCallCount") or 0)
            + int(trace.get("llm", {}).get("attemptCount") or 0)
            + int(trace.get("groundingReview", {}).get("attemptCount") or 0)
        )
        trace["stopReason"] = research_trace.get("stopReason")
        trace["elapsedMs"] = int((perf_counter() - started) * 1000)
        return AnswerResponse(
            pattern=request.pattern,
            route=route,
            answer=answer_text,
            predictedAnswer=predicted_answer,
            choiceJudgements=judgements,
            citations=citations,
            graphPaths=[],
            trace=trace,
        )

    def _layered_explicit_references(self, request: AnswerRequest) -> list[dict[str, Any]]:
        """質問に明示された法令名・条番号を、既存の決定的パーサーで条IDへ解決する。

        plannerには条番号を断定させず、この結果をP0のRequirementとして統合する(§7.2)。
        """
        try:
            titles = self.os_client.law_titles()
        except Exception:  # noqa: BLE001 - 明示参照が取れないだけで新方式を止めない
            return []
        references: list[dict[str, Any]] = []
        for document_id, provisions in _law_provision_references(request.question, titles).items():
            title = titles.get(document_id, "")
            for provision in provisions:
                references.append(
                    {
                        "articleContentUnitId": (
                            f"{document_id}-article-{provision['articleSuffix']}"
                        ),
                        "documentId": document_id,
                        "matchedText": f"{title} 第{provision['articleSuffix']}条",
                    }
                )
        return references

    def _apply_layered_legal_retrieval(
        self,
        request: AnswerRequest,
        final_ranked: list[dict[str, Any]],
        deadline: float,
        trace: dict[str, Any],
        route: list[str],
    ) -> list[dict[str, Any]]:
        """法令レイヤー別探索(vNext)をshadowまたはactiveで実行する。

        shadowでは現行コンテキストを変更せずtraceだけを残す。新方式の内部障害は
        現行回答へ影響してはならないため、例外はここで握りつぶす。active時の意味上の
        根拠不足・予算不足は通常回答へ隠さず、answerStatusとして回答制御へ渡す
        (docs/layered_legal_evidence_retrieval_plan.md §11.3, §19)。
        """
        active = settings.agent_layered_legal_retrieval
        if not active and not settings.agent_layered_legal_retrieval_shadow:
            return final_ranked
        old_context_stats = citation_context_stats(
            _citations_from_items(final_ranked),
            settings.llm_max_context_chars,
        )
        try:
            outcome = run_layered_retrieval(
                request=request,
                os_client=self.os_client,
                graph_client=self.graph_client,
                reranker_client=self.reranker_client,
                llm_client=self.llm_client,
                deadline=deadline,
                explicit_references=self._layered_explicit_references(request),
                shadow=not active,
                # 論理LLM呼び出し数の上限内でだけ構造化plannerを呼ぶ(§11.4)。
                allow_planner_call=(
                    settings.agent_use_llm_planner and settings.agent_max_llm_calls >= 4
                ),
            )
        except Exception:  # noqa: BLE001 - 新方式の障害で現行回答を止めない
            trace["layeredLegalRetrieval"] = {
                "enabled": True,
                "mode": "active" if active else "shadow",
                "errorCode": "layered_retrieval_error",
                "fallback": "legacy_retrieval",
            }
            return final_ranked

        trace["layeredLegalRetrieval"] = outcome.trace
        trace["layeredLegalRetrieval"]["oldContextContentUnitIds"] = [
            evidence_content_id(item) for item in final_ranked
        ]
        trace["layeredLegalRetrieval"]["newContextContentUnitIds"] = [
            evidence_content_id(item) for item in outcome.assembly.items
        ]
        trace["layeredLegalRetrieval"]["contextTruncation"] = {
            "oldContext": old_context_stats,
            "newContext": {
                **citation_context_stats(
                    _citations_from_items(outcome.assembly.items),
                    settings.llm_max_context_chars,
                ),
                "computedInShadow": not active,
            },
        }
        if not active:
            return final_ranked
        _append_route(route, "layered_legal_retrieval")
        if not outcome.usable_for_answer:
            if outcome.trace.get("internalFailure"):
                trace["layeredLegalRetrieval"]["fallback"] = "legacy_retrieval"
                return final_ranked
            # 意味上の根拠不足を旧経路の通常回答で隠さない。内部障害だけを例外経路で
            # legacyへfallbackし、ここでは空コンテキストとanswerStatusを回答制御へ渡す。
            return []
        return outcome.assembly.items

    def _apply_llm_directed_retrieval_shadow(
        self,
        request: AnswerRequest,
        deadline: float,
        trace: dict[str, Any],
    ) -> None:
        """LLM主導探索を回答非接続で実行し、失敗を現行経路から隔離する。"""
        if not settings.agent_llm_directed_retrieval_shadow:
            return
        try:
            outcome = run_llm_directed_research_shadow(
                request=request,
                os_client=self.os_client,
                graph_client=self.graph_client,
                llm_client=self.llm_client,
                deadline=deadline,
            )
            trace["llmDirectedLegalRetrieval"] = outcome.trace
        except Exception:  # noqa: BLE001 - shadow障害で現行回答を止めない
            trace["llmDirectedLegalRetrieval"] = {
                "enabled": True,
                "mode": "shadow",
                "connectedToAnswer": False,
                "status": "internal_error",
                "stopReason": "shadow_internal_error",
                "incomplete": True,
                "errorCode": "shadow_internal_error",
                "llmCallCount": 0,
                "toolCallCount": 0,
            }

    def _apply_issue_coverage_selection(
        self,
        old_final_ranked: list[dict[str, Any]],
        globally_ranked: list[dict[str, Any]],
        initial_matrix: AspectEvidenceMatrix,
        inherited_aspects_by_content_id: dict[str, set[str]],
        top_k: int,
        deadline: float,
        trace: dict[str, Any],
    ) -> list[dict[str, Any]]:
        old_ids = [evidence_content_id(item) for item in old_final_ranked]
        trace["oldContextContentUnitIds"] = old_ids
        trace["graphInheritedAspects"] = {
            content_unit_id: sorted(queries)
            for content_unit_id, queries in inherited_aspects_by_content_id.items()
        }

        now = perf_counter()
        available_for_aspects, aspect_phase_budget = (
            _aspect_phase_budget_seconds(
                deadline,
                now,
                settings.agent_answer_reserve_sec,
                settings.rerank_timeout_sec,
            )
        )
        phase_deadline = now + aspect_phase_budget
        trace["answerReserveMs"] = settings.agent_answer_reserve_sec * 1000
        trace["availableForAspectPhaseMs"] = int(available_for_aspects * 1000)
        trace["aspectPhaseBudgetMs"] = int(aspect_phase_budget * 1000)

        final_matrix, aspect_trace = self._rerank_final_aspects(
            globally_ranked,
            initial_matrix,
            inherited_aspects_by_content_id,
            phase_deadline,
        )
        elapsed_ms = int((perf_counter() - now) * 1000)
        trace["aspectPhaseElapsedMs"] = elapsed_ms
        trace["finalAspectReranker"] = aspect_trace
        trace["aspectEvidenceMatrix"] = _aspect_matrix_trace(final_matrix)
        trace["skippedAspectQueries"] = [
            {
                "query": aspect.query,
                "reason": aspect.skipped_reason or aspect.error or "reranker_unavailable",
            }
            for aspect in final_matrix.aspects
            if not aspect.used
        ]

        candidate_ids = {
            evidence_content_id(item)
            for item in globally_ranked
        }
        trace["bestAspectCandidateIdsBefore30"] = {
            aspect.query: (
                aspect.ordered_content_ids[0]
                if aspect.ordered_content_ids
                else None
            )
            for aspect in initial_matrix.aspects
        }
        trace["bestAspectCandidateMissingFrom30"] = [
            aspect.ordered_content_ids[0]
            for aspect in initial_matrix.aspects
            if aspect.ordered_content_ids
            and aspect.ordered_content_ids[0] not in candidate_ids
        ]
        trace["graphInheritedCandidateMissingFrom30"] = sorted(
            content_unit_id
            for content_unit_id in inherited_aspects_by_content_id
            if content_unit_id not in candidate_ids
        )

        if not final_matrix.aspects:
            trace["newContextContentUnitIds"] = old_ids
            trace["selectorFallbackReason"] = "no_aspects"
            trace["shadowSelection"] = {
                "complete": False,
                "skippedAspectCount": 0,
            }
            return old_final_ranked

        used_aspects = [aspect for aspect in final_matrix.aspects if aspect.used]
        if not used_aspects:
            trace["newContextContentUnitIds"] = old_ids
            trace["selectorFallbackReason"] = "all_aspects_unavailable"
            trace["shadowSelection"] = {
                "complete": False,
                "skippedAspectCount": len(final_matrix.aspects),
            }
            return old_final_ranked

        selection = select_issue_covered_context(
            globally_ranked,
            final_matrix,
            top_k=top_k,
            max_aspects=ASPECT_COVERAGE_MAX_QUERIES,
            protected_chunk_limit=top_k // 2,
            explicit_chunk_limit=max(1, top_k // 4),
            rounds=2,
        )
        new_ids = [evidence_content_id(item) for item in selection.items]
        trace["newContextContentUnitIds"] = new_ids
        trace["explicitReferenceCandidateCount"] = sum(
            1
            for item in globally_ranked
            if (
                item.get("introducedBy") == "article_reference"
                or "article_reference" in (item.get("sources") or [])
            )
        )
        trace["explicitProtectedChunkCount"] = len(
            selection.explicit_protected_ids
        )
        trace["aspectProtectedChunkCount"] = len(
            selection.aspect_protected_ids
        )
        trace["protectedChunkCount"] = len(selection.protected_ids)
        trace["globalRankChunkCount"] = len(selection.global_rank_ids)
        trace["coveredArticleCount"] = len({
            article
            for articles in selection.covered_articles_by_query.values()
            for article in articles
        })
        trace["oldAspectCoverage"] = _context_aspect_coverage(
            old_final_ranked,
            final_matrix,
        )
        trace["newAspectCoverage"] = _context_aspect_coverage(
            selection.items,
            final_matrix,
        )
        skipped_count = sum(1 for aspect in final_matrix.aspects if not aspect.used)
        complete = skipped_count == 0
        trace["shadowSelection"] = {
            "complete": complete,
            "skippedAspectCount": skipped_count,
        }
        trace["selectorFallbackReason"] = None

        if settings.agent_issue_coverage_selection:
            return selection.items
        return old_final_ranked

    def _rerank_final_aspects(
        self,
        globally_ranked: list[dict[str, Any]],
        initial_matrix: AspectEvidenceMatrix,
        inherited_aspects_by_content_id: dict[str, set[str]],
        phase_deadline: float,
    ) -> tuple[AspectEvidenceMatrix, list[dict[str, Any]]]:
        aspects: list[AspectEvidence] = []
        trace: list[dict[str, Any]] = []
        for initial_aspect in initial_matrix.aspects[:ASPECT_COVERAGE_MAX_QUERIES]:
            query = initial_aspect.query
            inherited_ids = {
                content_unit_id
                for content_unit_id, queries in inherited_aspects_by_content_id.items()
                if query in queries
            }
            eligible_ids = set(initial_aspect.searched_content_ids) | inherited_ids
            candidates = [
                item
                for item in globally_ranked
                if evidence_content_id(item) in eligible_ids
            ]
            remaining = phase_deadline - perf_counter()
            if remaining <= 1:
                aspect = AspectEvidence(
                    query=query,
                    searched_content_ids=[
                        evidence_content_id(item) for item in candidates
                    ],
                    ordered_content_ids=[],
                    inherited_content_ids=inherited_ids,
                    used=False,
                    skipped_reason="aspect_phase_budget_exhausted",
                )
            elif len(candidates) < 2:
                aspect = AspectEvidence(
                    query=query,
                    searched_content_ids=[
                        evidence_content_id(item) for item in candidates
                    ],
                    ordered_content_ids=[
                        evidence_content_id(item) for item in candidates
                    ],
                    inherited_content_ids=inherited_ids,
                    used=False,
                    error="insufficient_candidates",
                )
            else:
                result = self.reranker_client.rerank(
                    query,
                    candidates,
                    timeout_sec=max(
                        1,
                        min(settings.rerank_timeout_sec, int(remaining)),
                    ),
                )
                ordered_items = result.items if result.used else candidates
                aspect = AspectEvidence(
                    query=query,
                    searched_content_ids=[
                        evidence_content_id(item) for item in candidates
                    ],
                    ordered_content_ids=[
                        evidence_content_id(item) for item in ordered_items
                    ],
                    scores=dict(result.scores) if result.used else {},
                    inherited_content_ids=inherited_ids,
                    used=result.used,
                    error=result.error,
                )
            aspects.append(aspect)
            trace.append(
                {
                    "query": query,
                    "candidateCount": len(candidates),
                    "used": aspect.used,
                    "errorCode": (
                        "aspect_reranker_error" if aspect.error else None
                    ),
                    "skippedReason": aspect.skipped_reason,
                    "inheritedContentUnitIds": sorted(inherited_ids),
                    "topContentUnitIds": aspect.ordered_content_ids[:3],
                    "scores": aspect.scores,
                }
            )
        return AspectEvidenceMatrix(aspects), trace

    def _rerank_aspect_queries(
        self,
        evidence: dict[str, dict[str, Any]],
        focused_queries: list[str],
        limit: int,
        max_query_rank: int,
        deadline: float,
    ) -> tuple[AspectEvidenceMatrix, list[dict[str, Any]]]:
        """分解クエリごとの候補、順位、cross-encoderスコアを独立状態で返す。"""
        aspects: list[AspectEvidence] = []
        trace = []
        for query in focused_queries[:limit]:
            candidates = [
                item
                for item in evidence.values()
                if int((item.get("queryRanks") or {}).get(query, max_query_rank + 1))
                <= max_query_rank
            ]
            candidates.sort(
                key=lambda item: (
                    int((item.get("queryRanks") or {}).get(query, max_query_rank + 1)),
                    -float(item.get("score", 0.0)),
                )
            )
            remaining = int(deadline - perf_counter())
            if len(candidates) < 2 or remaining <= 1:
                ordered = candidates
                used = False
                error = None if candidates else "no_candidates"
                scores: dict[str, float] = {}
                skipped_reason = "time_budget" if remaining <= 1 else None
            else:
                result = self.reranker_client.rerank(
                    query,
                    candidates,
                    timeout_sec=max(1, min(settings.rerank_timeout_sec, remaining)),
                )
                ordered = result.items if result.used else candidates
                used = result.used
                error = result.error
                scores = dict(result.scores) if result.used else {}
                skipped_reason = None
            searched_ids = [
                str(item["document"].get("contentUnitId") or "")
                for item in candidates
                if item["document"].get("contentUnitId")
            ]
            ordered_ids = [
                str(item["document"].get("contentUnitId") or "")
                for item in ordered
                if item["document"].get("contentUnitId")
            ]
            aspects.append(
                AspectEvidence(
                    query=query,
                    searched_content_ids=searched_ids,
                    ordered_content_ids=ordered_ids,
                    scores=scores,
                    used=used,
                    error=error,
                    skipped_reason=skipped_reason,
                )
            )
            trace.append(
                {
                    "query": query,
                    "candidateCount": len(candidates),
                    "used": used,
                    "errorCode": (
                        "aspect_reranker_error" if error else None
                    ),
                    "skippedReason": skipped_reason,
                    "topContentUnitIds": ordered_ids[:3],
                    "scores": scores,
                }
            )
        return AspectEvidenceMatrix(aspects), trace

    def _search_evidence(
        self,
        query: str,
        law_top_k: int,
        user_clearance_level: int,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """資料種別と明示法令ごとに候補を確保し、大きな母集団での取りこぼしを防ぐ。"""
        result_groups = {
            "law": self.os_client.search(
                query,
                "law",
                law_top_k,
                user_clearance_level,
                use_bm25=settings.agent_use_bm25,
                use_vector=settings.agent_use_vector,
            ),
            "guideline": self.os_client.search(
                query,
                "guideline",
                min(settings.agent_guidance_candidate_top_k, law_top_k),
                user_clearance_level,
                use_bm25=settings.agent_use_bm25,
                use_vector=settings.agent_use_vector,
            ),
        }
        if settings.agent_use_bm25 and hasattr(self.os_client, "search_by_document_id"):
            try:
                # 明示された法令ごとに候補を確保する。候補プールを厚くするだけで、
                # 関連性の判断は再ランクに委ねる(法令名を書いただけの条文は固定しない)。
                named_laws = _matched_law_ids(query, self.os_client.law_titles())
                scoped: list[dict[str, Any]] = []
                for document_id in named_laws[:NAMED_LAW_COVERAGE_MAX_LAWS]:
                    scoped.extend(
                        self.os_client.search_by_document_id(
                            query,
                            document_id,
                            min(NAMED_LAW_COVERAGE_PER_LAW, law_top_k),
                            user_clearance_level,
                        )
                    )
                if scoped:
                    result_groups["named_law"] = scoped
            except Exception:
                # 通常のlaw検索結果は利用できるため、明示法令の候補確保だけをフォールバックする。
                pass
        # 法令内検索のスコアはその法令の中だけの相対値で、通常検索のスコアとは比較できない。
        # 候補としては加えるが、順位は通常検索の後ろに置く(関連性の判断は再ランクに委ねる)。
        scoped_results = result_groups.pop("named_law", [])
        merged: dict[str, dict[str, Any]] = {}
        for results in result_groups.values():
            for result in results:
                content_unit_id = str(result["document"]["contentUnitId"])
                existing = merged.get(content_unit_id)
                if existing is None or float(result.get("score", 0.0)) > float(existing.get("score", 0.0)):
                    merged[content_unit_id] = result
        ranked = sorted(merged.values(), key=lambda result: float(result.get("score", 0.0)), reverse=True)
        appended = []
        for result in scoped_results:
            content_unit_id = str(result["document"]["contentUnitId"])
            if content_unit_id not in merged:
                merged[content_unit_id] = result
                appended.append(result)
        source_counts = {source: len(results) for source, results in result_groups.items()}
        if scoped_results:
            source_counts["named_law"] = len(scoped_results)
        return ranked + appended, source_counts

    def _plan_queries(
        self,
        request: AnswerRequest,
        trace: dict[str, Any],
        deadline: float,
    ) -> tuple[list[str], bool]:
        fallback = [_search_query(request)]
        if request.pattern in {"pattern_1_baseline_rag", "pattern_2_rule_based_agentic_rag"}:
            trace["planner"] = {"used": False, "fallback": "single_query"}
            return fallback, False
        if not settings.agent_use_llm_planner:
            queries = _rule_based_decomposition(request, settings.agent_max_queries)
            trace["planner"] = {"used": False, "fallback": "rule_based_decomposition", "queries": queries}
            return queries, _query_has_reference_cues(request)

        remaining = int(deadline - perf_counter())
        if remaining <= 1:
            trace["planner"] = {"used": False, "fallback": "time_budget"}
            return fallback, False
        focused_query_limit = max(1, settings.agent_max_queries - 1)
        try:
            result = self.llm_client.plan_search(
                request,
                focused_query_limit,
                timeout_sec=max(1, min(settings.planner_timeout_sec, remaining)),
            )
        except Exception:
            queries = _rule_based_decomposition(request, settings.agent_max_queries)
            trace["planner"] = {
                "used": False,
                "errorCode": "planner_error",
                "attemptCount": 1,
                "fallback": "rule_based_decomposition",
                "queries": queries,
            }
            return queries, _query_has_reference_cues(request)

        queries = list(dict.fromkeys([fallback[0], *result.queries]))[
            : settings.agent_max_queries
        ]
        if not result.queries:
            queries = _rule_based_decomposition(request, settings.agent_max_queries)
        trace["planner"] = {
            "provider": result.provider,
            "model": result.model,
            "used": bool(result.queries),
            "latencyMs": result.latencyMs,
            "inputTokens": result.inputTokens,
            "outputTokens": result.outputTokens,
            "validationError": result.validationError,
            "stopReason": result.stopReason,
            "retryCount": result.retryCount,
            "attemptCount": 1 + result.retryCount,
            "fallback": None if result.queries else "rule_based_decomposition",
            "queries": queries,
            "graphRequired": result.graphRequired,
        }
        return queries, result.graphRequired

    def _inject_article_references(
        self,
        request: AnswerRequest,
        evidence: dict[str, dict[str, Any]],
        trace: dict[str, Any],
        *,
        text_override: str | None = None,
        round_number: int = 0,
        source: str = "article_reference",
    ) -> list[str] | None:
        """質問・選択肢に明示された「法令名+第N条」を検索スコアに依らず候補プールへ直接投入する。

        埋め込み・BM25は条番号の完全一致を保証しないため、明示参照は検索とは別経路で解決する。
        対象法令は本文中に法令名が現れるものに限定し、無ければ既存候補の上位法令に絞る。
        """
        text = text_override or _search_query(request)
        all_provisions = _extract_provision_references(text)
        if not all_provisions:
            return None
        try:
            titles = self.os_client.law_titles()
            provision_references = _law_provision_references(text, titles)
            references = {
                law_id: list(dict.fromkeys(provision["articleSuffix"] for provision in provisions))
                for law_id, provisions in provision_references.items()
            }
            if not references:
                matched_laws = _matched_law_ids(text, titles)
                provision_references = {law_id: all_provisions for law_id in matched_laws}
                references = {
                    law_id: list(dict.fromkeys(item["articleSuffix"] for item in all_provisions))
                    for law_id in matched_laws
                }
            if not references:
                ranked = sorted(evidence.values(), key=lambda item: item["score"], reverse=True)
                matched_laws = list(
                    dict.fromkeys(item["document"].get("documentId") for item in ranked if item["document"].get("documentId"))
                )[:2]
                provision_references = {law_id: all_provisions for law_id in matched_laws}
                references = {
                    law_id: list(dict.fromkeys(item["articleSuffix"] for item in all_provisions))
                    for law_id in matched_laws
                }
            if not references:
                return None
            exact_content_ids = list(
                dict.fromkeys(
                    f"{law_id}-article-{provision['contentSuffix']}"
                    for law_id, provisions in provision_references.items()
                    for provision in provisions
                    if provision["contentSuffix"] != provision["articleSuffix"]
                )
            )
            article_ids = list(
                dict.fromkeys(
                    f"{law_id}-article-{provision['articleSuffix']}"
                    for law_id, provisions in provision_references.items()
                    for provision in provisions
                    if provision["contentSuffix"] == provision["articleSuffix"]
                )
            )
            documents = []
            if exact_content_ids:
                documents.extend(
                    self.os_client.get_by_content_unit_ids(
                        exact_content_ids,
                        request.userClearanceLevel,
                    )
                )
            if article_ids:
                documents.extend(
                    self.os_client.get_by_article_ids(
                        article_ids,
                        request.userClearanceLevel,
                        max_chunks=max(100, len(article_ids) * 30),
                    )
                )
        except Exception:
            trace["rounds"].append(
                {
                    "round": round_number,
                    "tool": "article_direct_lookup",
                    "errorCode": "article_direct_lookup_error",
                }
            )
            return None
        documents = list({document["contentUnitId"]: document for document in documents}.values())
        selected_documents = _select_direct_documents(
            request,
            documents,
            DIRECT_CHUNKS_PER_ARTICLE,
            preferred_content_ids=set(exact_content_ids),
            query_text=text,
        )
        new_count = _merge_direct_documents_into_evidence(
            evidence,
            selected_documents,
            source,
            direct_reference=True,
            must_include_content_ids=set(exact_content_ids),
        )
        trace["rounds"].append(
            {
                "round": round_number,
                "tool": "article_direct_lookup",
                "references": references,
                "contentUnitReferences": exact_content_ids,
                "resultCount": len(documents),
                "selectedCount": len(selected_documents),
                "newContentUnitCount": new_count,
            }
        )
        return [
            str(document["contentUnitId"])
            for document in selected_documents
            if document.get("contentUnitId")
        ]

    def _inject_guidance_explained_articles(
        self,
        request: AnswerRequest,
        evidence: dict[str, dict[str, Any]],
        trace: dict[str, Any],
        rerank_top_k: int,
        graph_paths: list[dict[str, Any]],
    ) -> int | None:
        """上位候補にガイドライン解説チャンクが入っている場合、そのチャンクが
        解説対象とする法令条文をグラフ(EXPLAINS)経由で特定し、スコアに依らず
        候補プールへ確実に投入する。

        薄い委任規定(例: 「...体制を整備すること」)は、具体的な言葉で書かれた
        ガイドライン解説とのスコア競争でRRF/rerankerに負けて最終引用から漏れる
        ことがある(実測: 薬機法第18条の2第1項第2号)。ガイドラインと条文の対応は
        seed時に「ガイドライン文書 -EXPLAINS-> 条文」エッジとしてグラフへ載せてある。
        グラフを羅針盤として使い、上位ヒットしたガイドライン文書からEXPLAINSを辿って
        条文を特定する(条文本文の取得はOpenSearch=Vector RAG側の役割)。最終的に
        引用するかどうかはLLM(_compose_answer)の判断に委ねる。
        """
        ranked_evidence = sorted(
            evidence.values(),
            key=lambda item: (
                bool(item.get("aspectInclude")),
                float(item["score"]),
            ),
            reverse=True,
        )[:rerank_top_k]
        document_ids: list[str] = []
        for item in ranked_evidence:
            document = item["document"]
            if document.get("docType") != "guideline":
                continue
            document_id = document.get("documentId")
            if document_id and document_id not in document_ids:
                document_ids.append(document_id)
        if not document_ids:
            return None
        try:
            paths = self.graph_client.paths_from_many(
                document_ids,
                edge_type=GUIDANCE_EXPLAINS_EDGE_TYPE,
                max_depth=1,
                limit=settings.agent_max_graph_paths,
                user_clearance_level=request.userClearanceLevel,
            )
        except Exception:
            trace["rounds"].append(
                {
                    "round": 0,
                    "tool": "guidance_explains_lookup",
                    "errorCode": "graph_lookup_error",
                }
            )
            return None
        article_ids = _explains_target_article_ids(paths, GUIDANCE_EXPLAINS_MAX_ARTICLES)
        if not article_ids:
            return None
        try:
            documents = self.os_client.get_by_article_ids(
                article_ids,
                request.userClearanceLevel,
                max_chunks=max(30, len(article_ids) * 30),
            )
        except Exception:
            trace["rounds"].append(
                {
                    "round": 0,
                    "tool": "guidance_explains_lookup",
                    "errorCode": "article_lookup_error",
                }
            )
            return None
        selected_documents = _select_direct_documents(request, documents, DIRECT_CHUNKS_PER_ARTICLE)
        new_count = _merge_direct_documents_into_evidence(evidence, selected_documents, "guidance_explains")
        graph_paths.extend(paths)
        trace["rounds"].append(
            {
                "round": 0,
                "tool": "guidance_explains_lookup",
                "guidanceDocumentIds": document_ids,
                "articleIds": article_ids,
                "resultCount": len(documents),
                "selectedCount": len(selected_documents),
                "newContentUnitCount": new_count,
            }
        )
        return new_count

    def _should_expand_graph(
        self,
        request: AnswerRequest,
        evidence: dict[str, dict[str, Any]],
        planner_graph_required: bool,
    ) -> bool:
        if request.pattern == "pattern_1_baseline_rag":
            return False
        if planner_graph_required or _query_has_reference_cues(request):
            return True
        ranked_evidence = sorted(evidence.values(), key=lambda item: item["score"], reverse=True)[: request.topK]
        for item in ranked_evidence:
            text = str(item["document"].get("text") or "")
            if any(cue in text for cue in REFERENCE_CUES):
                return True
            if len(ARTICLE_REFERENCE_PATTERN.findall(text)) > 1:
                return True
        return False

    def _evaluate_evidence(
        self,
        request: AnswerRequest,
        evidence: dict[str, dict[str, Any]],
        trace: dict[str, Any],
        deadline: float,
        rerank_top_k: int,
    ) -> tuple[list[str], bool, bool]:
        fallback_queries = _follow_up_queries(request, evidence, [])
        used_llm_calls = int(bool(trace.get("planner", {}).get("used")))
        remaining = int(deadline - perf_counter())
        if used_llm_calls >= settings.agent_max_llm_calls - 1 or remaining <= 2:
            trace["evaluator"] = {
                "used": False,
                "fallback": "rule_based_evaluator",
                "reason": "llm_or_time_budget",
            }
            return fallback_queries, _query_has_reference_cues(request), False
        citations = _citations_from_evidence(
            evidence,
            request,
            rerank_top_k,
            min(rerank_top_k, 10),
        )
        try:
            result = self.llm_client.evaluate_evidence(
                request,
                citations,
                max_queries=min(settings.agent_max_retry_rounds, 2),
                timeout_sec=max(1, min(settings.evaluator_timeout_sec, remaining)),
            )
        except Exception:
            trace["evaluator"] = {
                "provider": self.llm_client.provider,
                "used": False,
                "errorCode": "evaluator_error",
                "attemptCount": 1,
                "fallback": "rule_based_evaluator",
            }
            return fallback_queries, _query_has_reference_cues(request), False
        valid = not result.validationError
        trace["evaluator"] = {
            "provider": result.provider,
            "model": result.model,
            "used": valid,
            "latencyMs": result.latencyMs,
            "inputTokens": result.inputTokens,
            "outputTokens": result.outputTokens,
            "validationError": result.validationError,
            "stopReason": result.stopReason,
            "retryCount": result.retryCount,
            "attemptCount": 1 + result.retryCount,
            "fallback": None if valid else "rule_based_evaluator",
            "choiceCoverage": result.choiceCoverage,
            "followUpQueries": result.followUpQueries,
            "graphRequired": result.graphRequired,
            "stop": result.stop,
        }
        if not valid:
            return fallback_queries, _query_has_reference_cues(request), False
        return result.followUpQueries, result.graphRequired, result.stop

    def _expand_graph(
        self,
        request: AnswerRequest,
        evidence: dict[str, dict[str, Any]],
        rerank_top_k: int,
        candidate_top_k: int,
        aspect_matrix: AspectEvidenceMatrix | None = None,
        inherited_aspects_by_content_id: dict[str, set[str]] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        aspect_matrix = aspect_matrix or AspectEvidenceMatrix()
        inherited_aspects_by_content_id = (
            inherited_aspects_by_content_id
            if inherited_aspects_by_content_id is not None
            else {}
        )
        aspect_queries_by_source_article = aspect_queries_by_article(
            aspect_matrix,
            evidence,
            per_query=ASPECT_COVERAGE_PER_QUERY,
        )
        start_ids = []
        article_ids = []
        ranked_evidence = sorted(
            evidence.values(),
            key=lambda item: (
                bool(item.get("aspectInclude")),
                float(item["score"]),
            ),
            reverse=True,
        )[:rerank_top_k]
        for item in ranked_evidence:
            document = item["document"]
            start_ids.append(document["contentUnitId"])
            if document.get("parentContentUnitId"):
                start_ids.append(document["parentContentUnitId"])
            article_id = document.get("articleContentUnitId")
            if article_id:
                start_ids.append(article_id)
                article_ids.append(article_id)
        unique_start_ids = list(dict.fromkeys(start_ids))
        graph_query_parts = [
            _search_query(request),
            *(aspect.query for aspect in aspect_matrix.aspects),
        ]
        query_text = " ".join(dict.fromkeys(graph_query_parts))
        reference_paths = self._fair_graph_paths(
            unique_start_ids,
            "REFERENCES",
            request.userClearanceLevel,
            query_text,
        )
        implementation_paths = self._fair_graph_paths(
            unique_start_ids,
            "IMPLEMENTS",
            request.userClearanceLevel,
            query_text,
        )
        incorporation_paths = self._fair_graph_paths(
            unique_start_ids,
            "APPLIED_BY",
            request.userClearanceLevel,
            query_text,
        )
        paths = _dedupe_graph_paths(
            [*reference_paths, *implementation_paths, *incorporation_paths]
        )
        trusted_target_relations: dict[str, set[str]] = {}
        trusted_target_source_ids: dict[str, set[str]] = {}
        trusted_target_source_ranks: dict[str, int] = {}
        source_ranks = {
            str(content_unit_id).split("-paragraph-", 1)[0]: rank
            for rank, content_unit_id in enumerate(article_ids)
        }
        for relation_type, relation_paths in (
            ("IMPLEMENTS", implementation_paths),
            ("REFERENCES", reference_paths),
            ("APPLIED_BY", incorporation_paths),
        ):
            for path in relation_paths:
                edges = path.get("edges") or []
                nodes = path.get("nodes") or []
                if not edges or not nodes:
                    continue
                edge = edges[-1]
                if relation_type == "REFERENCES" and (
                    edge.get("relationSource") != "subordinate_law_parent_reference"
                ):
                    continue
                if float(edge.get("relationConfidence", 1.0)) < 0.9:
                    continue
                target_id = nodes[-1].get("contentUnitId")
                if not target_id:
                    continue
                target_article_id = str(target_id).split("-paragraph-", 1)[0]
                source_id = nodes[0].get("contentUnitId")
                source_article_id = str(source_id).split("-paragraph-", 1)[0]
                trusted_target_relations.setdefault(target_article_id, set()).add(
                    relation_type
                )
                if source_article_id:
                    trusted_target_source_ids.setdefault(target_article_id, set()).add(
                        source_article_id
                    )
                    source_rank = source_ranks.get(source_article_id, len(source_ranks))
                    trusted_target_source_ranks[target_article_id] = min(
                        trusted_target_source_ranks.get(target_article_id, source_rank),
                        source_rank,
                    )
        # 前項・同項・同号などの参照は、条ノードから兄弟の項・号を辿って解決する。
        if article_ids and _needs_sibling_expansion(request, ranked_evidence):
            sibling_paths = self.graph_client.paths_from_many(
                list(dict.fromkeys(article_ids)),
                edge_type=HIERARCHY_EDGE_TYPE,
                max_depth=HIERARCHY_MAX_DEPTH,
                limit=settings.agent_max_graph_paths,
                user_clearance_level=request.userClearanceLevel,
            )
            paths = _dedupe_graph_paths([*paths, *sibling_paths])
        all_target_source_ids: dict[str, set[str]] = {}
        for path in paths:
            nodes = path.get("nodes") or []
            if len(nodes) < 2:
                continue
            source_id = str(nodes[0].get("contentUnitId") or "")
            target_id = str(nodes[-1].get("contentUnitId") or "")
            if not source_id or not target_id:
                continue
            source_article_id = source_id.split("-paragraph-", 1)[0]
            target_article_id = target_id.split("-paragraph-", 1)[0]
            all_target_source_ids.setdefault(target_article_id, set()).add(
                source_article_id
            )
        for content_unit_id, item in evidence.items():
            target_article_id = evidence_article_id(item)
            inherited_queries = {
                query
                for source_article_id in all_target_source_ids.get(
                    target_article_id,
                    set(),
                )
                for query in aspect_queries_by_source_article.get(
                    source_article_id,
                    set(),
                )
            }
            if inherited_queries:
                inherited_aspects_by_content_id.setdefault(
                    content_unit_id,
                    set(),
                ).update(inherited_queries)
        target_ids = []
        for path in paths:
            for node in path.get("nodes", []):
                content_unit_id = node.get("contentUnitId")
                if content_unit_id and content_unit_id not in evidence:
                    target_ids.append(content_unit_id)
        documents = self.os_client.get_by_content_unit_ids(
            list(dict.fromkeys(target_ids)),
            request.userClearanceLevel,
        )
        documents.sort(
            key=lambda document: _text_coverage(
                query_text,
                f"{document.get('heading') or ''} {document.get('text') or ''}",
            ),
            reverse=True,
        )
        trusted_content_relations = _select_trusted_graph_documents(
            [
                *(item["document"] for item in evidence.values()),
                *documents,
            ],
            trusted_target_relations,
            query_text,
            GRAPH_RELATION_PIN_MAX,
            target_source_ranks=trusted_target_source_ranks,
        )
        documents = _prioritize_trusted_graph_documents(
            documents,
            set(trusted_content_relations),
            query_text,
        )
        aspect_article_ids = set(aspect_queries_by_source_article)
        for content_unit_id, relation_types in trusted_content_relations.items():
            target_article_id = content_unit_id.split("-paragraph-", 1)[0]
            graph_source_ids = trusted_target_source_ids.get(target_article_id, set())
            graph_source_rank = trusted_target_source_ranks.get(
                target_article_id,
                len(source_ranks),
            )
            if content_unit_id in evidence:
                item = evidence[content_unit_id]
                item["mustInclude"] = True
                item["citationClosure"] = True
                item["graphRelationTypes"] = sorted(
                    set(item.get("graphRelationTypes") or []) | relation_types
                )
                item["graphSourceArticleIds"] = sorted(
                    set(item.get("graphSourceArticleIds") or []) | graph_source_ids
                )
                item["graphSourceRank"] = min(
                    int(item.get("graphSourceRank", graph_source_rank)),
                    graph_source_rank,
                )
                item["graphFromAspect"] = bool(
                    set(item.get("graphSourceArticleIds") or []) & aspect_article_ids
                )
        new_count = 0
        for rank, document in enumerate(documents[:candidate_top_k], start=1):
            content_unit_id = document["contentUnitId"]
            trusted_graph_target = content_unit_id in trusted_content_relations
            relation_types = trusted_content_relations.get(content_unit_id, set())
            target_article_id = content_unit_id.split("-paragraph-", 1)[0]
            graph_source_ids = trusted_target_source_ids.get(target_article_id, set())
            all_graph_source_ids = all_target_source_ids.get(target_article_id, set())
            inherited_queries = {
                query
                for source_article_id in all_graph_source_ids
                for query in aspect_queries_by_source_article.get(
                    source_article_id,
                    set(),
                )
            }
            if inherited_queries:
                inherited_aspects_by_content_id.setdefault(
                    content_unit_id,
                    set(),
                ).update(inherited_queries)
            graph_source_rank = trusted_target_source_ranks.get(
                target_article_id,
                len(source_ranks),
            )
            graph_score = 0.35 / (settings.agent_rrf_k + rank)
            if content_unit_id not in evidence:
                evidence[content_unit_id] = {
                    "document": document,
                    "score": graph_score,
                    "sources": ["graph_expansion"],
                    "queries": [],
                    "introducedBy": "graph_expansion",
                    "mustInclude": trusted_graph_target,
                    "citationClosure": trusted_graph_target,
                    "graphRelationTypes": sorted(relation_types),
                    "graphSourceArticleIds": sorted(graph_source_ids),
                    "graphSourceRank": graph_source_rank,
                    "graphFromAspect": bool(graph_source_ids & aspect_article_ids),
                }
                new_count += 1
            else:
                item = evidence[content_unit_id]
                item["score"] += graph_score
                item["sources"].append("graph_expansion")
                item["mustInclude"] = item.get("mustInclude", False) or trusted_graph_target
                if trusted_graph_target:
                    item["citationClosure"] = True
                    item["graphRelationTypes"] = sorted(
                        set(item.get("graphRelationTypes") or []) | relation_types
                    )
                    item["graphSourceArticleIds"] = sorted(
                        set(item.get("graphSourceArticleIds") or []) | graph_source_ids
                    )
                    item["graphSourceRank"] = min(
                        int(item.get("graphSourceRank", graph_source_rank)),
                        graph_source_rank,
                    )
                    item["graphFromAspect"] = bool(
                        set(item.get("graphSourceArticleIds") or [])
                        & aspect_article_ids
                    )
        return paths, new_count

    def _fair_graph_paths(
        self,
        start_ids: list[str],
        edge_type: str,
        user_clearance_level: int,
        query_text: str = "",
    ) -> list[dict[str, Any]]:
        """上位ノードごとに少数ずつGraphを展開し、一つの条文による上限独占を防ぐ。"""
        paths: list[dict[str, Any]] = []
        for start_id in start_ids[: settings.agent_max_graph_paths]:
            per_start = self.graph_client.paths_from_many(
                [start_id],
                edge_type=edge_type,
                max_depth=settings.agent_max_graph_hop,
                limit=min(100, max(20, settings.agent_max_graph_paths * 4)),
                user_clearance_level=user_clearance_level,
            )
            # 同じ委任元から同一の施行令だけが先に多数返る場合でも、府令など
            # 別資料の具体化規定が探索対象になるよう、到達資料を優先して分散する。
            paths = _dedupe_graph_paths(
                [
                    *paths,
                    *_diverse_target_document_paths(
                        per_start,
                        2,
                        query_text=query_text,
                    ),
                ]
            )
            if len(paths) >= settings.agent_max_graph_paths:
                break
        return paths[: settings.agent_max_graph_paths]

    def _execute_reviewer_research_queries(
        self,
        request: AnswerRequest,
        queries: list[str],
        existing_citations: list[Citation],
        deadline: float,
    ) -> tuple[list[Citation], dict[str, Any]]:
        """Reviewerが決めた検索語だけを実行し、未選別の本文候補を返す。"""
        catalog = EvidenceCatalog()
        document_catalog_error = False
        try:
            catalog.add_documents(self.os_client.law_titles())
        except Exception:  # noqa: BLE001 - 全文書検索は一覧なしでも実行可能
            document_catalog_error = True
        catalog.add_results(
            [citation.model_dump() for citation in existing_citations]
        )
        existing_ids = set(catalog.content_unit_ids)
        gateway = LegalResearchToolGateway(self.os_client, self.graph_client)
        research_case = InMemoryCaseStore().create_case(request.question)
        executions: list[dict[str, Any]] = []
        unique_queries = list(dict.fromkeys(queries))[:2]
        for query in unique_queries:
            remaining = deadline - perf_counter()
            if remaining <= 1:
                executions.append(
                    {
                        "tool": TOOL_SEARCH_CORPUS,
                        "query": query,
                        "error": "deadline_exhausted",
                    }
                )
                break
            action = ResearchAction(
                tool=TOOL_SEARCH_CORPUS,
                query=query,
                reason="grounding_reviewer_requested_follow_up",
            )
            task = research_case.register_action(
                action,
                phase="grounding_review",
            )
            research_case.start_task(task.task_ref)
            execution = gateway.execute(
                action,
                catalog,
                user_clearance_level=request.userClearanceLevel,
                timeout_sec=max(0.1, remaining),
            )
            research_case.complete_tool_task(
                task_ref=task.task_ref,
                action=action,
                execution=execution,
                catalog=catalog,
            )
            executions.append(execution.as_trace(action))

        new_ids = tuple(
            content_unit_id
            for content_unit_id in catalog.content_unit_ids
            if content_unit_id not in existing_ids
        )
        # 検索順位を法的関連性の判定には使わない。返却候補はMainが本文を
        # 読んでcitationIdsに選ぶための材料にすぎない。
        items = catalog.items_by_ids(new_ids)
        follow_up_citations = _citations_from_items(
            [
                {
                    "document": item,
                    "evidenceLane": (
                        "law" if item.get("docType") == "law" else "guidance"
                    ),
                }
                for item in items
            ]
        )
        return follow_up_citations, {
            "requestedQueries": unique_queries,
            "executions": executions,
            "newContentUnitIds": list(new_ids),
            "semanticSelection": "main_llm",
            "documentCatalogError": document_catalog_error,
            "caseStore": research_case.trace(),
        }

    def _input_type(self, request: AnswerRequest) -> str:
        return "multiple_choice_legal_qa" if request.choices else "legal_qa"

    def _compose_answer(
        self,
        request: AnswerRequest,
        route: list[str],
        citations: list[Citation],
        trace: dict[str, Any],
        deadline: float,
        evidence_by_choice: dict[str, list[str]] | None,
        research_context: dict[str, Any] | None = None,
    ) -> tuple[str, str | None, dict[str, str] | None, list[str]]:
        layered_control = _layered_answer_control(trace)
        if (
            layered_control
            and layered_control.get("answerStatus")
            == "insufficient_primary_evidence"
        ):
            omitted = "、".join(
                layered_control.get("omittedPrimaryIssueLabels") or ["主たる論点"]
            )
            trace["llm"] = {
                "provider": self.llm_client.provider,
                "used": False,
                "error": "insufficient_primary_evidence",
            }
            return (
                f"主たる論点（{omitted}）の根拠を回答コンテキストへ収められなかったため、"
                "この質問には根拠付きで回答できません。",
                None,
                None,
                [],
            )
        if citations:
            # Reviewerの追加調査で得た候補も同じ最終化処理へ渡す。候補の
            # 意味上の採否はMain LLMのcitationIdsに委ねる。
            working_citations = list(citations)
            main_attempts: list[dict[str, Any]] = []
            review_attempts: list[dict[str, Any]] = []
            current_research_context = (
                dict(research_context) if research_context is not None else None
            )

            def call_main(
                *, reviewer_result=None, previous_result=None
            ):
                remaining = int(deadline - perf_counter())
                if remaining <= 1:
                    return None, "answer_model_time_exhausted"
                answer_kwargs: dict[str, Any] = {
                    "timeout_sec": max(1, min(settings.llm_timeout_sec, remaining)),
                    "evidence_by_choice": evidence_by_choice,
                    "answer_scope": layered_control,
                }
                if reviewer_result is not None and previous_result is not None:
                    answer_kwargs["review_feedback"] = list(
                        reviewer_result.issues
                    )
                    answer_kwargs["review_verdict"] = reviewer_result.verdict
                    answer_kwargs["previous_answer"] = previous_result.text
                    answer_kwargs["previous_answer_status"] = (
                        previous_result.answerStatus
                    )
                    answer_kwargs["previous_citation_ids"] = list(
                        previous_result.answerCitationIds or []
                    )
                    answer_kwargs["previous_missing"] = list(
                        previous_result.missing or []
                    )
                    answer_kwargs["previous_issue_decisions"] = list(
                        previous_result.answerIssueDecisions or []
                    )
                    answer_kwargs["review_findings"] = list(
                        reviewer_result.issueFindings or []
                    )
                if current_research_context is not None:
                    answer_kwargs["research_context"] = current_research_context
                try:
                    result = self.llm_client.generate_answer(
                        request, route, working_citations, **answer_kwargs
                    )
                except requests.Timeout:
                    return None, "answer_model_timeout"
                except Exception:  # noqa: BLE001
                    return None, "answer_model_error"
                if result is None:
                    return None, "answer_model_unavailable"
                main_attempts.append(
                    {
                        "model": result.model,
                        "latencyMs": result.latencyMs,
                        "inputTokens": result.inputTokens,
                        "outputTokens": result.outputTokens,
                        "validationError": result.validationError,
                        "stopReason": result.stopReason,
                        "retryCount": result.retryCount,
                        "answerStatus": result.answerStatus,
                        "citationIds": result.answerCitationIds,
                        "missing": result.missing,
                        "issueDecisions": result.answerIssueDecisions,
                    }
                )
                if result.validationError:
                    return None, "answer_contract_invalid"
                return result, None

            def call_reviewer(result):
                review = getattr(self.llm_client, "review_answer_grounding", None)
                remaining = int(deadline - perf_counter())
                if not callable(review):
                    return None, "grounding_review_unavailable"
                if remaining <= 1:
                    return None, "grounding_review_time_exhausted"
                citations_by_id = {
                    citation.contentUnitId: citation
                    for citation in working_citations
                    if citation.contentUnitId
                }
                selected_citations = [
                    citations_by_id[content_unit_id]
                    for content_unit_id in (result.answerCitationIds or [])
                    if content_unit_id in citations_by_id
                ]
                try:
                    reviewed = review(
                        request,
                        result.text,
                        selected_citations,
                        timeout_sec=max(1, min(settings.llm_timeout_sec, remaining)),
                        research_context=current_research_context,
                        answer_status=result.answerStatus,
                        citation_ids=result.answerCitationIds,
                        missing=result.missing,
                        issue_decisions=result.answerIssueDecisions,
                        available_citations=working_citations,
                    )
                except requests.Timeout:
                    return None, "grounding_review_timeout"
                except Exception:  # noqa: BLE001
                    return None, "grounding_review_error"
                review_attempts.append(
                    {
                        "model": reviewed.model,
                        "verdict": reviewed.verdict,
                        "issues": reviewed.issues,
                        "findings": reviewed.issueFindings,
                        "researchQueries": reviewed.researchQueries,
                        "latencyMs": reviewed.latencyMs,
                        "inputTokens": reviewed.inputTokens,
                        "outputTokens": reviewed.outputTokens,
                        "validationErrorCode": reviewed.validationError,
                        "stopReason": reviewed.stopReason,
                        "retryCount": reviewed.retryCount,
                    }
                )
                if reviewed.validationError:
                    return None, "grounding_review_contract_invalid"
                return reviewed, None

            llm_result, main_error = call_main()
            if llm_result is None:
                trace["llm"] = {
                    "provider": self.llm_client.provider,
                    "used": bool(main_attempts),
                    "errorCode": main_error,
                    "attemptCount": sum(
                        1 + int(item.get("retryCount") or 0) for item in main_attempts
                    ),
                    "attempts": main_attempts,
                }
                if request.choices:
                    return (
                        "Main Agentの構造化回答を検証できなかったため、"
                        "選択肢判定を行いません。",
                        None,
                        None,
                        [],
                    )
                return (
                    "Main Agentの構造化最終判断を検証できなかったため、"
                    "断定回答を行いません。",
                    None,
                    None,
                    [],
                )
            else:
                final_result = llm_result
                if not request.choices and research_context is not None:
                    reviewed, review_error = call_reviewer(final_result)
                    remediation_rounds: list[dict[str, Any]] = []
                    grounding_research_rounds: list[dict[str, Any]] = []
                    for remediation_index in range(
                        GROUNDING_REMEDIATION_MAX_ROUNDS
                    ):
                        if reviewed is None or reviewed.verdict in {
                            "supported",
                            "insufficient",
                        }:
                            break
                        remediation_trace: dict[str, Any] = {
                            "roundIndex": remediation_index,
                            "reviewerVerdict": reviewed.verdict,
                            "issues": list(reviewed.issues),
                        }
                        if reviewed.verdict == "needs_research":
                            follow_up_citations, follow_up_trace = (
                                self._execute_reviewer_research_queries(
                                    request,
                                    reviewed.researchQueries,
                                    working_citations,
                                    deadline,
                                )
                            )
                            follow_up_trace["roundIndex"] = remediation_index
                            grounding_research_rounds.append(follow_up_trace)
                            trace["groundingResearch"] = {
                                **follow_up_trace,
                                "rounds": list(grounding_research_rounds),
                            }
                            remediation_trace["researchQueries"] = list(
                                reviewed.researchQueries
                            )
                            remediation_trace["newEvidenceCount"] = len(
                                follow_up_citations
                            )
                            if not follow_up_citations:
                                review_error = "grounding_research_no_evidence"
                                remediation_trace["result"] = "no_evidence"
                                remediation_rounds.append(remediation_trace)
                                break
                            working_citations[:] = _dedupe_citations(
                                [*follow_up_citations, *working_citations]
                            )
                            if current_research_context is not None:
                                current_answer_contract = dict(
                                    current_research_context.get("answerContract")
                                    or {}
                                )
                                current_answer_contract["availableCitationIds"] = [
                                    citation.contentUnitId
                                    for citation in working_citations
                                    if citation.contentUnitId
                                ]
                                current_research_context = {
                                    **current_research_context,
                                    "answerContract": current_answer_contract,
                                    "reviewerFollowUp": {
                                        "performed": True,
                                        "queries": list(
                                            reviewed.researchQueries
                                        ),
                                        "newEvidenceContentUnitIds": [
                                            citation.contentUnitId
                                            for citation in follow_up_citations
                                            if citation.contentUnitId
                                        ],
                                    },
                                }
                            # 呼び出し元はこのリストから最終citationIdsを展開する。
                            citations[:] = working_citations

                        revised, revision_error = call_main(
                            reviewer_result=reviewed,
                            previous_result=final_result,
                        )
                        if revised is None:
                            main_error = revision_error
                            reviewed = None
                            review_error = (
                                "main_after_research_failed"
                                if remediation_trace["reviewerVerdict"]
                                == "needs_research"
                                else "main_revision_failed"
                            )
                            remediation_trace["result"] = review_error
                            remediation_rounds.append(remediation_trace)
                            break
                        final_result = revised
                        remediation_trace["result"] = "main_reconsidered"
                        remediation_rounds.append(remediation_trace)
                        reviewed, review_error = call_reviewer(final_result)
                    if (
                        reviewed is not None
                        and reviewed.verdict not in {"supported", "insufficient"}
                        and not review_error
                    ):
                        review_error = "grounding_remediation_limit_reached"
                    trace["groundingReview"] = {
                        "provider": self.llm_client.provider,
                        "used": bool(review_attempts),
                        "verdict": reviewed.verdict if reviewed else "insufficient",
                        "issues": reviewed.issues if reviewed else [],
                        "findings": (
                            reviewed.issueFindings if reviewed else []
                        ),
                        "researchQueries": (
                            reviewed.researchQueries if reviewed else []
                        ),
                        "errorCode": review_error,
                        "attemptCount": sum(
                            1 + int(item.get("retryCount") or 0)
                            for item in review_attempts
                        ),
                        "remediationRoundLimit": (
                            GROUNDING_REMEDIATION_MAX_ROUNDS
                        ),
                        "remediationRounds": remediation_rounds,
                        "attempts": review_attempts,
                    }
                    if reviewed is None or reviewed.verdict != "supported":
                        issues = (
                            reviewed.issues
                            if reviewed is not None
                            else (review_attempts[-1].get("issues") if review_attempts else [])
                        )
                        issue_lines = "\n".join(f"- {issue}" for issue in issues)
                        answer = (
                            "【根拠不十分】Main AgentとReviewerの検証で未解決の問題が残ったため、"
                            "断定回答を行いません。"
                        )
                        if issue_lines:
                            answer += f"\n確認が必要な点:\n{issue_lines}"
                        answer += "\n個別事情に応じて専門家へ確認してください。"
                        trace["groundingReview"]["answerSuppressed"] = True
                        trace["llm"] = _answer_trace(final_result, main_attempts)
                        return answer, None, None, []

                trace["llm"] = _answer_trace(final_result, main_attempts)
                answer_text = final_result.text or (
                    "十分な引用根拠を取得できなかったため、断定回答は行いません。"
                )
                assessment_ids = _assessment_citation_ids(
                    final_result.choiceAssessments,
                    final_result.predictedAnswer,
                )
                if not request.choices:
                    assessment_ids = list(final_result.answerCitationIds or [])
                    if final_result.answerStatus in {"partial", "insufficient"}:
                        label = (
                            "【一部のみ回答】"
                            if final_result.answerStatus == "partial"
                            else "【根拠不十分】"
                        )
                        if not answer_text.startswith(label):
                            answer_text = f"{label}{answer_text}"
                if research_context and research_context.get("incomplete"):
                    stop_reason = str(research_context.get("stopReason") or "unknown")
                    answer_text = (
                        "【調査未完了】反復上限までに十分性を確認できて"
                        f"いません（停止理由: {stop_reason}）。\n{answer_text}"
                    )
                    trace["partialAnswer"] = {
                        "incomplete": True,
                        "stopReason": stop_reason,
                    }
                if (
                    layered_control
                    and layered_control.get("answerStatus") == "partial_primary_evidence"
                ):
                    omitted = "、".join(
                        layered_control.get("omittedPrimaryIssueLabels")
                        or ["一部の主論点"]
                    )
                    answer_text = (
                        f"一部の主論点（{omitted}）は根拠不足のため回答できません。"
                        f"{answer_text}"
                    )
                predicted = (
                    None
                    if layered_control
                    and layered_control.get("answerStatus")
                    == "partial_primary_evidence"
                    else final_result.predictedAnswer
                )
                return (
                    answer_text,
                    predicted,
                    final_result.choiceJudgements,
                    assessment_ids,
                )

        if request.choices:
            return (
                "LLM 未使用のため選択肢判定は行いません。 評価時は predictedAnswer を null として扱ってください。",
                None,
                None,
                [],
            )
        if citations:
            return (
                "検索された根拠候補に基づく回答です。法的判断は引用元を確認し、必要に応じて専門家確認を行ってください。",
                None,
                None,
                [],
            )
        return "十分な引用根拠を取得できなかったため、断定回答は行いません。", None, None, []


def _answer_trace(result: Any, attempts: list[dict[str, Any]]) -> dict[str, Any]:
    """Main LLMの初回判断とReviewer後の再判断を同じtrace契約へまとめる。"""
    return {
        "provider": result.provider,
        "model": result.model,
        "used": True,
        "latencyMs": sum(int(item.get("latencyMs") or 0) for item in attempts),
        "inputTokens": sum(int(item.get("inputTokens") or 0) for item in attempts),
        "outputTokens": sum(int(item.get("outputTokens") or 0) for item in attempts),
        "estimatedCost": result.estimatedCost,
        "validationError": result.validationError,
        "stopReason": result.stopReason,
        "contentBlockTypes": result.contentBlockTypes,
        "outputChars": result.outputChars,
        "retryCount": sum(int(item.get("retryCount") or 0) for item in attempts),
        "attemptCount": sum(
            1 + int(item.get("retryCount") or 0) for item in attempts
        ),
        "questionPolarity": result.questionPolarity,
        "choiceAssessments": result.choiceAssessments,
        "answerStatus": result.answerStatus,
        "citationIds": result.answerCitationIds,
        "missing": result.missing,
        "issueDecisions": result.answerIssueDecisions,
        "attempts": attempts,
    }


def _dedupe_citations(citations: list[Citation]) -> list[Citation]:
    """contentUnitIdの一致だけで重複を除き、LLMが受け取る順序を保つ。"""
    output: list[Citation] = []
    seen: set[str] = set()
    for citation in citations:
        key = citation.contentUnitId or ""
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        output.append(citation)
    return output


def _merge_direct_documents_into_evidence(
    evidence: dict[str, dict[str, Any]],
    selected_documents: list[dict[str, Any]],
    source_tag: str,
    *,
    direct_reference: bool = False,
    must_include_content_ids: set[str] | None = None,
) -> int:
    """条番号で直接取得したselected_documentsを、article_ranksでmustInclude判定しつつ
    evidence辞書へマージする。新規追加件数を返す。"""
    new_count = 0
    article_ranks: dict[str, int] = {}
    for document in selected_documents:
        content_unit_id = document["contentUnitId"]
        article_id = str(document.get("articleContentUnitId") or content_unit_id).split("-paragraph-", 1)[0]
        article_ranks[article_id] = article_ranks.get(article_id, 0) + 1
        must_include = (
            content_unit_id in (must_include_content_ids or set())
            or article_ranks[article_id] == 1
        )
        if content_unit_id in evidence:
            item = evidence[content_unit_id]
            item["sources"].append(source_tag)
            if direct_reference:
                item["directReference"] = True
            item["mustInclude"] = item.get("mustInclude", False) or must_include
        else:
            evidence[content_unit_id] = {
                "document": document,
                "score": 0.0,
                "sources": [source_tag],
                "queries": [],
                "introducedBy": source_tag,
                "mustInclude": must_include,
                **({"directReference": True} if direct_reference else {}),
            }
            new_count += 1
    return new_count


def _search_query(request: AnswerRequest) -> str:
    if not request.choices:
        return request.question
    choice_texts = [text for _, text in sorted(request.choices.items())]
    return " ".join([request.question, *choice_texts])


def _rule_based_decomposition(request: AnswerRequest, max_queries: int) -> list[str]:
    queries = [_search_query(request)]
    if request.choices:
        queries.extend(f"{request.question} {text}" for _, text in sorted(request.choices.items()))
    return list(dict.fromkeys(queries))[:max_queries]


def _query_has_reference_cues(request: AnswerRequest) -> bool:
    text = _search_query(request)
    return sum(1 for cue in REFERENCE_CUES if cue in text) >= 2


def _extract_article_suffixes(text: str, limit: int = 5) -> list[str]:
    """質問・選択肢中の「第N条(のM)」を contentUnitId 接尾辞('185_22'等)へ変換して列挙する。"""
    suffixes: list[str] = []
    for match in QUESTION_ARTICLE_PATTERN.finditer(text.translate(FULLWIDTH_DIGITS)):
        parts = [match.group(1), *match.group(2).removeprefix("の").split("の")]
        numbers = [_japanese_number_to_int(part) for part in parts if part]
        if not numbers or any(number is None for number in numbers):
            continue
        suffix = "_".join(str(number) for number in numbers)
        if suffix not in suffixes:
            suffixes.append(suffix)
        if len(suffixes) >= limit:
            break
    return suffixes


def _extract_provision_references(text: str, limit: int = 12) -> list[dict[str, str]]:
    """質問中の条・項・号を、条IDと最も細かいcontentUnitId接尾辞へ変換する。

    「第N条第K号」は第1項第K号の省略表記として扱う。
    """
    references: list[dict[str, str]] = []
    normalized = text.translate(FULLWIDTH_DIGITS)
    for match in QUESTION_PROVISION_PATTERN.finditer(normalized):
        article_parts = [match.group(1), *match.group(2).removeprefix("の").split("の")]
        article_numbers = [_japanese_number_to_int(part) for part in article_parts if part]
        paragraph = _japanese_number_to_int(match.group(3)) if match.group(3) else None
        item = _japanese_number_to_int(match.group(4)) if match.group(4) else None
        if not article_numbers or any(number is None for number in article_numbers):
            continue
        if (match.group(3) and paragraph is None) or (match.group(4) and item is None):
            continue
        article_suffix = "_".join(str(number) for number in article_numbers)
        content_suffix = article_suffix
        if paragraph is not None or item is not None:
            content_suffix += f"-paragraph-{paragraph or 1}"
        if item is not None:
            content_suffix += f"-item-{item}"
        reference = {"articleSuffix": article_suffix, "contentSuffix": content_suffix}
        if reference not in references:
            references.append(reference)
        if len(references) >= limit:
            break
    return references


def _matched_law_ids(text: str, titles: dict[str, str]) -> list[str]:
    """本文中に正式名称または既知の略称が現れる法令を、登場順で返す。

    「金融商品取引法施行令」に含まれる「金融商品取引法」や、
    「薬機法施行規則」に含まれる「薬機法」は、同じ位置では長い名称側へ帰属させる。
    """
    mentions: list[tuple[int, int, str]] = []
    for document_id, title in titles.items():
        if not title:
            continue
        for name in (title, *LAW_TITLE_ALIASES.get(title, ())):
            start = 0
            while (index := text.find(name, start)) >= 0:
                mentions.append((index, index + len(name), document_id))
                start = index + 1

    selected: list[tuple[int, int, str]] = []
    for mention in sorted(mentions, key=lambda item: (item[0], -(item[1] - item[0]))):
        start, end, _ = mention
        if any(start < selected_end and selected_start < end for selected_start, selected_end, _ in selected):
            continue
        selected.append(mention)

    result = []
    for _, _, document_id in sorted(selected):
        if document_id not in result:
            result.append(document_id)
    return result


def _law_article_references(text: str, titles: dict[str, str]) -> dict[str, list[str]]:
    """法令名と、その法令名から次の法令名までに現れる条番号を対応付ける。"""
    normalized = text.translate(FULLWIDTH_DIGITS)
    candidates = []
    for document_id, title in titles.items():
        if not title:
            continue
        start = 0
        while (index := normalized.find(title, start)) >= 0:
            candidates.append((index, index + len(title), document_id, title))
            start = index + 1

    # 「金融商品取引法施行令」の中にある「金融商品取引法」は長い法令名側へ帰属させる。
    mentions = []
    for candidate in sorted(candidates, key=lambda item: (item[0], -(item[1] - item[0]))):
        if any(candidate[0] < end and start < candidate[1] for start, end, _, _ in mentions):
            continue
        mentions.append(candidate)
    mentions.sort(key=lambda item: item[0])

    references: dict[str, list[str]] = {}
    for index, (_, end, document_id, _) in enumerate(mentions):
        segment_end = mentions[index + 1][0] if index + 1 < len(mentions) else len(normalized)
        suffixes = _extract_article_suffixes(normalized[end:segment_end])
        if not suffixes:
            continue
        bucket = references.setdefault(document_id, [])
        for suffix in suffixes:
            if suffix not in bucket:
                bucket.append(suffix)
    return references


def _law_provision_references(
    text: str,
    titles: dict[str, str],
) -> dict[str, list[dict[str, str]]]:
    """法令名から次の法令名までに現れる条・項・号を、その法令へ対応付ける。"""
    normalized = text.translate(FULLWIDTH_DIGITS)
    candidates = []
    for document_id, title in titles.items():
        if not title:
            continue
        for name in (title, *LAW_TITLE_ALIASES.get(title, ())):
            start = 0
            while (index := normalized.find(name, start)) >= 0:
                candidates.append((index, index + len(name), document_id))
                start = index + 1

    mentions = []
    for candidate in sorted(candidates, key=lambda item: (item[0], -(item[1] - item[0]))):
        if any(candidate[0] < end and start < candidate[1] for start, end, _ in mentions):
            continue
        mentions.append(candidate)
    mentions.sort(key=lambda item: item[0])

    references: dict[str, list[dict[str, str]]] = {}
    for index, (_, end, document_id) in enumerate(mentions):
        segment_end = mentions[index + 1][0] if index + 1 < len(mentions) else len(normalized)
        provisions = _extract_provision_references(normalized[end:segment_end])
        if not provisions:
            continue
        bucket = references.setdefault(document_id, [])
        for provision in provisions:
            if provision not in bucket:
                bucket.append(provision)
    return references


def _select_direct_documents(
    request: AnswerRequest,
    documents: list[dict[str, Any]],
    per_article: int,
    preferred_content_ids: set[str] | None = None,
    query_text: str | None = None,
) -> list[dict[str, Any]]:
    """直接解決した条ごとに、質問・選択肢へ最も近いチャンクだけを残す。"""
    query_parts = [query_text or request.question, *(request.choices or {}).values()]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for document in documents:
        content_unit_id = str(document.get("contentUnitId") or "")
        article_id = str(document.get("articleContentUnitId") or content_unit_id).split("-paragraph-", 1)[0]
        grouped.setdefault(article_id, []).append(document)

    selected = []
    for article_id in sorted(grouped):
        preferred_count = sum(
            document.get("contentUnitId") in (preferred_content_ids or set())
            for document in grouped[article_id]
        )
        ranked = sorted(
            grouped[article_id],
            key=lambda document: (
                document.get("contentUnitId") in (preferred_content_ids or set()),
                max(
                    _text_coverage(
                        part,
                        f"{document.get('heading') or ''} {document.get('text') or ''}",
                    )
                    for part in query_parts
                ),
            ),
            reverse=True,
        )
        # 選択肢等に項・号が複数明記されている場合は、通常の条内チャンク上限より
        # 明示参照を優先する。明示されていない兄弟チャンクだけを per_article で抑える。
        selected.extend(ranked[: max(per_article, preferred_count)])
    return selected


def _select_trusted_graph_documents(
    documents: Iterable[dict[str, Any]],
    target_relations: dict[str, set[str]],
    query_text: str,
    limit: int,
    target_source_ranks: dict[str, int] | None = None,
) -> dict[str, set[str]]:
    """高信頼Graph対象ごとに代表1チャンクを選び、関係種別ごとの枠を確保する。"""
    best_by_article: dict[str, tuple[float, str, set[str], str]] = {}
    seen_content_ids: set[str] = set()
    for document in documents:
        content_unit_id = str(document.get("contentUnitId") or "")
        if not content_unit_id or content_unit_id in seen_content_ids:
            continue
        seen_content_ids.add(content_unit_id)
        article_id = str(
            document.get("articleContentUnitId") or content_unit_id
        ).split("-paragraph-", 1)[0]
        relations = target_relations.get(article_id)
        if not relations:
            continue
        coverage = _text_coverage(
            query_text,
            f"{document.get('heading') or ''} {document.get('text') or ''}",
        )
        candidate = (
            coverage,
            content_unit_id,
            relations,
            str(document.get("documentId") or article_id),
        )
        current = best_by_article.get(article_id)
        if current is None or (candidate[0], candidate[1]) > (current[0], current[1]):
            best_by_article[article_id] = candidate

    representatives = [
        {
            "articleId": article_id,
            "coverage": candidate[0],
            "contentUnitId": candidate[1],
            "relations": candidate[2],
            "documentId": candidate[3],
            "sourceRank": (target_source_ranks or {}).get(article_id, 1_000_000),
        }
        for article_id, candidate in best_by_article.items()
    ]
    representatives.sort(
        key=lambda item: (
            item["sourceRank"],
            -item["coverage"],
            item["contentUnitId"],
        )
    )

    selected: list[dict[str, Any]] = []
    selected_articles: set[str] = set()
    selected_documents: set[str] = set()
    for relation_type in ("IMPLEMENTS", "REFERENCES", "APPLIED_BY"):
        representative = next(
            (
                item
                for item in representatives
                if item["articleId"] not in selected_articles
                and relation_type in item["relations"]
                and item["documentId"] not in selected_documents
            ),
            None,
        )
        if representative is None:
            representative = next(
                (
                    item
                    for item in representatives
                    if item["articleId"] not in selected_articles
                    and relation_type in item["relations"]
                ),
                None,
            )
        if representative is None:
            continue
        selected.append(representative)
        selected_articles.add(representative["articleId"])
        selected_documents.add(representative["documentId"])
        if len(selected) >= limit:
            break

    for representative in representatives:
        if len(selected) >= limit:
            break
        if (
            representative["articleId"] in selected_articles
            or representative["documentId"] in selected_documents
        ):
            continue
        selected.append(representative)
        selected_articles.add(representative["articleId"])
        selected_documents.add(representative["documentId"])

    for representative in representatives:
        if len(selected) >= limit:
            break
        if representative["articleId"] in selected_articles:
            continue
        selected.append(representative)
        selected_articles.add(representative["articleId"])
        selected_documents.add(representative["documentId"])
    return {
        item["contentUnitId"]: set(item["relations"])
        for item in selected
    }


def _prioritize_trusted_graph_documents(
    documents: Iterable[dict[str, Any]],
    trusted_content_ids: set[str],
    query_text: str,
) -> list[dict[str, Any]]:
    """本文マージ上限より前に、高信頼グラフ対象そのものを配置する。"""
    return sorted(
        documents,
        key=lambda document: (
            document.get("contentUnitId") in trusted_content_ids,
            _text_coverage(
                query_text,
                f"{document.get('heading') or ''} {document.get('text') or ''}",
            ),
        ),
        reverse=True,
    )


def _explains_target_article_ids(paths: list[dict[str, Any]], limit: int) -> list[str]:
    """EXPLAINSパス(ガイドライン文書 -> 条文)の終端ノードから条文IDを抽出する。
    出現順を保ちつつ重複を除き、上限limit件で切る。"""
    article_ids: list[str] = []
    for path in paths:
        nodes = path.get("nodes") or []
        if not nodes:
            continue
        target = nodes[-1]
        article_id = target.get("contentUnitId") or target.get("graphNodeId")
        if article_id and article_id not in article_ids:
            article_ids.append(article_id)
        if len(article_ids) >= limit:
            break
    return article_ids[:limit]


def _needs_sibling_expansion(request: AnswerRequest, ranked_evidence: list[dict[str, Any]]) -> bool:
    """前項・同項・同号など、同一条内の兄弟項・号への参照があるか判定する。"""
    query_text = _search_query(request)
    if any(cue in query_text for cue in SIBLING_REFERENCE_CUES):
        return True
    for item in ranked_evidence:
        text = str(item["document"].get("text") or "")
        if any(cue in text for cue in SIBLING_REFERENCE_CUES):
            return True
    return False


def _merge_search_results(
    evidence: dict[str, dict[str, Any]],
    results: list[dict[str, Any]],
    query: str,
    source: str,
) -> int:
    new_count = 0
    for rank, result in enumerate(results, start=1):
        document = result["document"]
        content_unit_id = document["contentUnitId"]
        source_weight = 1.25 if source == "broad_search" else 1.0
        rank_score = source_weight / (settings.agent_rrf_k + rank)
        if content_unit_id not in evidence:
            evidence[content_unit_id] = {
                "document": document,
                "score": rank_score,
                "sources": [source],
                "queries": [query],
                "queryRanks": {query: rank},
                "introducedBy": source,
            }
            new_count += 1
        else:
            item = evidence[content_unit_id]
            item["score"] += rank_score
            item["sources"].append(source)
            item["queries"].append(query)
            query_ranks = item.setdefault("queryRanks", {})
            query_ranks[query] = min(int(query_ranks.get(query, rank)), rank)
    return new_count


def _follow_up_queries(
    request: AnswerRequest,
    evidence: dict[str, dict[str, Any]],
    existing_queries: list[str],
) -> list[str]:
    documents = [item["document"] for item in evidence.values()]
    corpus = " ".join(str(document.get("text") or "") for document in documents)
    candidates = []
    if request.choices:
        scored_choices = [
            (_text_coverage(text, corpus), f"{request.question} {text}")
            for _, text in sorted(request.choices.items())
        ]
        candidates.extend(query for _, query in sorted(scored_choices, key=lambda item: item[0]))
    elif documents:
        for document in documents[:2]:
            heading = str(document.get("heading") or "")
            if heading:
                candidates.append(f"{request.question} {heading}")
    return [query for query in dict.fromkeys(candidates) if query not in existing_queries]


def _text_coverage(text: str, corpus: str) -> float:
    normalized_text = re.sub(r"\s+", "", text)
    normalized_corpus = re.sub(r"\s+", "", corpus)
    grams = {normalized_text[index : index + 2] for index in range(max(0, len(normalized_text) - 1))}
    if not grams:
        return 0.0
    corpus_grams = {normalized_corpus[index : index + 2] for index in range(max(0, len(normalized_corpus) - 1))}
    return len(grams & corpus_grams) / len(grams)


def _diverse_target_document_paths(
    paths: Iterable[dict[str, Any]],
    limit: int,
    *,
    query_text: str = "",
) -> list[dict[str, Any]]:
    """グラフ到達先の資料を分散し、同じ下位法令による枠の独占を防ぐ。"""
    if limit <= 0:
        return []
    candidates = list(paths)
    if query_text:
        candidates.sort(
            key=lambda path: _text_coverage(
                query_text,
                " ".join(
                    str(value or "")
                    for value in (
                        ((path.get("nodes") or [{}])[-1]).get("title"),
                        ((path.get("nodes") or [{}])[-1]).get("heading"),
                    )
                ),
            ),
            reverse=True,
        )
    selected: list[dict[str, Any]] = []
    seen_documents: set[str] = set()
    for path in candidates:
        nodes = path.get("nodes") or []
        target = nodes[-1] if nodes else {}
        document_id = str(target.get("documentId") or target.get("graphNodeId") or "")
        if document_id in seen_documents:
            continue
        selected.append(path)
        seen_documents.add(document_id)
        if len(selected) >= limit:
            return selected
    for path in candidates:
        if path in selected:
            continue
        selected.append(path)
        if len(selected) >= limit:
            break
    return selected


def _mark_aspect_representatives(
    evidence: dict[str, dict[str, Any]],
    focused_queries: Iterable[str],
    limit: int,
    max_query_rank: int,
    query_orders: dict[str, list[str]] | None = None,
    per_query: int = 1,
    dedupe_articles: bool = True,
) -> list[str]:
    """分解クエリごとの検索上位を少数だけ、再ランカー後の候補枠まで保持する。

    質問全文との独立評価だけでは、複数論点のうち語数が少ない条文が落ちやすい。
    一方で各クエリの候補を全て固定すると検索ノイズを残すため、検索上位かつ
    Articleが重複しない代表だけを対象にする。
    """
    if limit <= 0 or max_query_rank <= 0:
        return []
    selected: list[str] = []
    selected_articles: set[str] = {
        str(item["document"].get("articleContentUnitId") or content_unit_id).split(
            "-paragraph-", 1
        )[0]
        for content_unit_id, item in evidence.items()
        if dedupe_articles and item.get("aspectInclude")
    }
    candidates_by_query: list[tuple[str, list[dict[str, Any]]]] = []
    for query in dict.fromkeys(focused_queries):
        candidates_by_id = {
            str(item["document"].get("contentUnitId") or ""): item
            for item in evidence.values()
            if int((item.get("queryRanks") or {}).get(query, max_query_rank + 1))
            <= max_query_rank
        }
        ordered_ids = (query_orders or {}).get(query, [])
        candidates = [
            candidates_by_id[content_unit_id]
            for content_unit_id in ordered_ids
            if content_unit_id in candidates_by_id
        ]
        candidates.extend(
            sorted(
                (
                    item
                    for content_unit_id, item in candidates_by_id.items()
                    if content_unit_id not in ordered_ids
                ),
                key=lambda item: (
                    int((item.get("queryRanks") or {}).get(query, max_query_rank + 1)),
                    -float(item.get("score", 0.0)),
                    str(item["document"].get("contentUnitId") or ""),
                ),
            )
        )
        candidates_by_query.append((query, candidates))

    selected_per_query = {query: 0 for query, _ in candidates_by_query}
    while len(selected) < limit and any(
        selected_per_query[query] < per_query and candidates
        for query, candidates in candidates_by_query
    ):
        added = False
        for query, candidates in candidates_by_query:
            if selected_per_query[query] >= per_query:
                continue
            representative = None
            while candidates:
                candidate = candidates.pop(0)
                content_unit_id = str(candidate["document"].get("contentUnitId") or "")
                article_id = str(
                    candidate["document"].get("articleContentUnitId") or content_unit_id
                ).split("-paragraph-", 1)[0]
                if dedupe_articles and article_id in selected_articles:
                    continue
                representative = candidate
                break
            if representative is None:
                selected_per_query[query] = per_query
                continue
            content_unit_id = str(representative["document"].get("contentUnitId") or "")
            article_id = str(
                representative["document"].get("articleContentUnitId") or content_unit_id
            ).split("-paragraph-", 1)[0]
            representative["aspectInclude"] = True
            representative.setdefault("aspectQueries", []).append(query)
            selected.append(content_unit_id)
            if dedupe_articles:
                selected_articles.add(article_id)
            selected_per_query[query] += 1
            added = True
            if len(selected) >= limit:
                break
        if not added:
            break
    return selected


def _aspect_matrix_trace(matrix: AspectEvidenceMatrix) -> dict[str, dict[str, Any]]:
    return {
        aspect.query: {
            "searchedContentUnitIds": list(aspect.searched_content_ids),
            "orderedContentUnitIds": list(aspect.ordered_content_ids),
            "scores": dict(aspect.scores),
            "inheritedContentUnitIds": sorted(aspect.inherited_content_ids),
            "used": aspect.used,
            "errorCode": (
                "aspect_reranker_error" if aspect.error else None
            ),
            "skippedReason": aspect.skipped_reason,
        }
        for aspect in matrix.aspects
    }


def _aspect_phase_budget_seconds(
    deadline: float,
    now: float,
    answer_reserve_sec: int,
    rerank_timeout_sec: int,
) -> tuple[float, float]:
    available = max(0.0, deadline - now - answer_reserve_sec)
    return available, min(float(rerank_timeout_sec), available)


def _context_aspect_coverage(
    items: list[dict[str, Any]],
    matrix: AspectEvidenceMatrix,
) -> dict[str, list[str]]:
    context_articles = {
        evidence_article_id(item)
        for item in items
        if evidence_article_id(item)
    }
    return {
        aspect.query: list(dict.fromkeys(
            evidence_article_id_by_content_id
            for evidence_article_id_by_content_id in (
                _content_id_to_article_id(content_unit_id, items)
                for content_unit_id in aspect.ordered_content_ids
            )
            if evidence_article_id_by_content_id in context_articles
        ))
        for aspect in matrix.aspects
    }


def _content_id_to_article_id(
    content_unit_id: str,
    items: list[dict[str, Any]],
) -> str:
    for item in items:
        if evidence_content_id(item) == content_unit_id:
            return evidence_article_id(item)
    return str(content_unit_id).split("-paragraph-", 1)[0]


def _citations_from_evidence(
    evidence: dict[str, dict[str, Any]],
    request: AnswerRequest,
    rerank_top_k: int,
    citation_top_k: int,
) -> list[Citation]:
    ranked = _fusion_ranked_evidence(evidence, rerank_top_k)
    if not ranked:
        return []
    selected = []
    broad_ranked = [item for item in ranked if "broad_search" in item["sources"]]
    if broad_ranked:
        selected.append(broad_ranked[0])

    max_score = max(float(item["score"]) for item in ranked) or 1.0
    for _, choice_text in sorted((request.choices or {}).items()):
        choice_ranked = sorted(
            ranked,
            key=lambda item: (
                0.75 * _text_coverage(
                    choice_text,
                    f"{item['document'].get('heading') or ''} {item['document'].get('text') or ''}",
                )
                + 0.25 * float(item["score"]) / max_score
            ),
            reverse=True,
        )
        choice = next((item for item in choice_ranked if item not in selected), None)
        if choice is not None:
            selected.append(choice)
        if len(selected) >= citation_top_k:
            break

    selected_ids = {item["document"]["contentUnitId"] for item in selected}
    selected.extend(
        item for item in ranked if item["document"]["contentUnitId"] not in selected_ids
    )
    ranked = selected[:citation_top_k]
    return [
        Citation(
            documentId=item["document"].get("documentId"),
            contentUnitId=item["document"].get("contentUnitId"),
            title=item["document"].get("title"),
            heading=item["document"].get("heading"),
            sourceObjectUri=item["document"].get("sourceObjectUri"),
            sourcePage=item["document"].get("sourcePage"),
            text=item["document"].get("text"),
        )
        for item in ranked
    ]


def _fusion_ranked_evidence(
    evidence: dict[str, dict[str, Any]],
    rerank_top_k: int,
) -> list[dict[str, Any]]:
    """RRF上位へ、明示条番号から直接解決した各条の代表チャンクを必ず含める。

    あわせて、候補に現れた法令・資料ごとの最上位1件を、最大で枠の半分まで先に確保する。
    スコア順だけで切ると1つの法令が枠を占有し、横断に必要な他法令が
    再ランカーへ届かないため(関連性の判断は再ランカーに委ねる)。
    """
    pinned = sorted(
        (item for item in evidence.values() if item.get("mustInclude")),
        key=lambda item: item["score"],
        reverse=True,
    )
    aspect_representatives = sorted(
        (
            item
            for item in evidence.values()
            if item.get("aspectInclude") and not item.get("mustInclude")
        ),
        key=lambda item: item["score"],
        reverse=True,
    )
    scored = sorted(evidence.values(), key=lambda item: item["score"], reverse=True)
    reserved_content_ids = {
        item["document"]["contentUnitId"]
        for item in [*pinned, *aspect_representatives]
    }
    representative_budget = max(
        0,
        rerank_top_k // 2 - len(reserved_content_ids),
    )
    representatives = _document_representatives(scored, representative_budget)
    ranked = []
    seen = set()
    for item in [*pinned, *aspect_representatives, *representatives, *scored]:
        content_unit_id = item["document"]["contentUnitId"]
        if content_unit_id in seen:
            continue
        ranked.append(item)
        seen.add(content_unit_id)
        if len(ranked) >= rerank_top_k:
            break
    return ranked


def _score_ranked_evidence(
    evidence: dict[str, dict[str, Any]],
    top_k: int,
) -> list[dict[str, Any]]:
    """明示条番号の必須根拠を優先し、それ以外は融合スコア順で返す。

    再ランカーが使えない場合のフォールバック用。再ランカー入力のための
    文書多様化は適用せず、低順位資料の意図しない昇格を防ぐ。
    """
    pinned = sorted(
        (item for item in evidence.values() if item.get("mustInclude")),
        key=lambda item: item["score"],
        reverse=True,
    )
    scored = sorted(evidence.values(), key=lambda item: item["score"], reverse=True)
    ranked = []
    seen = set()
    for item in [*pinned, *scored]:
        content_unit_id = item["document"]["contentUnitId"]
        if content_unit_id in seen:
            continue
        ranked.append(item)
        seen.add(content_unit_id)
        if len(ranked) >= top_k:
            break
    return ranked


def _document_representatives(
    scored: list[dict[str, Any]],
    max_documents: int,
) -> list[dict[str, Any]]:
    """スコア順の候補から、文書ごとの最上位1件を最大 max_documents 件返す。"""
    if max_documents <= 0:
        return []
    representatives = []
    seen_documents = set()
    for item in scored:
        document_id = str(item["document"].get("documentId") or "")
        if not document_id or document_id in seen_documents:
            continue
        seen_documents.add(document_id)
        representatives.append(item)
        if len(representatives) >= max_documents:
            break
    return representatives


def _pin_ranked_evidence(
    ordered: list[dict[str, Any]],
    top_k: int,
) -> list[dict[str, Any]]:
    """専用リランカー適用後も、明示条番号から得た必須根拠を上位枠に保持する。

    上位枠の直後にある別法令は、上位を占有する法令の末尾候補と限定的に入れ替える。
    全法令を無条件に前方へ出すと、再ランカーが低く評価した無関係法令まで混ざるため、
    救済範囲と追加件数を小さく制限する。ガイドラインは入れ替え対象にしない。
    """
    ranked = list(ordered[:top_k])
    seen = {
        item["document"]["contentUnitId"]
        for item in ranked
    }
    pinned_outside = []
    for index, item in enumerate(ordered[top_k:], start=top_k):
        if item["document"]["contentUnitId"] in seen:
            continue
        must_include = bool(item.get("mustInclude"))
        aspect_include = bool(item.get("aspectInclude"))
        if not must_include and not aspect_include:
            continue
        if item.get("citationClosure") and not item.get("directReference"):
            if (
                not item.get("graphFromAspect")
                and index >= top_k + GRAPH_RELATION_RERANK_SLACK
            ):
                continue
        pinned_outside.append(item)
    pinned_outside.sort(
        key=lambda item: (
            bool(item.get("directReference")),
            bool(item.get("graphFromAspect")),
            bool(item.get("mustInclude")),
        ),
        reverse=True,
    )
    pinned_outside = pinned_outside[:FINAL_RERANK_MAX_ADDITIONS]
    replaceable_indices = [
        index
        for index in range(len(ranked) - 1, -1, -1)
        if not ranked[index].get("mustInclude")
    ]
    rescued_ids: set[str] = set()
    for pinned in pinned_outside:
        if not replaceable_indices:
            break
        replace_index = replaceable_indices.pop(0)
        removed_id = ranked[replace_index]["document"]["contentUnitId"]
        seen.discard(removed_id)
        ranked[replace_index] = pinned
        pinned_id = pinned["document"]["contentUnitId"]
        seen.add(pinned_id)
        rescued_ids.add(pinned_id)

    if len(ranked) < top_k or top_k < 2:
        return ranked

    law_counts: dict[str, int] = {}
    selected_law_documents: set[str] = set()
    for item in ranked:
        if not _is_law_document(item["document"]):
            continue
        document_id = str(item["document"].get("documentId") or "")
        if not document_id:
            continue
        law_counts[document_id] = law_counts.get(document_id, 0) + 1
        selected_law_documents.add(document_id)

    slack = min(FINAL_LAW_DIVERSITY_MAX_SLACK, max(1, top_k // 4))
    max_additions = min(FINAL_LAW_DIVERSITY_MAX_ADDITIONS, max(1, top_k // 4))
    additions = 0
    for candidate in ordered[: min(len(ordered), top_k + slack)]:
        if additions >= max_additions or not _is_law_document(candidate["document"]):
            continue
        candidate_document_id = str(candidate["document"].get("documentId") or "")
        candidate_content_id = candidate["document"].get("contentUnitId")
        if (
            not candidate_document_id
            or candidate_document_id in selected_law_documents
            or candidate_content_id in seen
        ):
            continue

        replace_index = next(
            (
                index
                for index in range(len(ranked) - 1, -1, -1)
                if _is_law_document(ranked[index]["document"])
                and not ranked[index].get("mustInclude")
                and not ranked[index].get("aspectInclude")
                and index >= top_k // 2
                and ranked[index]["document"].get("contentUnitId") not in rescued_ids
                and law_counts.get(str(ranked[index]["document"].get("documentId") or ""), 0) > 1
            ),
            None,
        )
        if replace_index is None:
            continue

        removed = ranked[replace_index]
        removed_id = removed["document"].get("contentUnitId")
        removed_document_id = str(removed["document"].get("documentId") or "")
        if removed_id:
            seen.discard(removed_id)
        law_counts[removed_document_id] -= 1

        ranked[replace_index] = candidate
        seen.add(candidate_content_id)
        selected_law_documents.add(candidate_document_id)
        law_counts[candidate_document_id] = 1
        additions += 1
    return ranked


def _is_law_document(document: dict[str, Any]) -> bool:
    """seed済み文書を法令か判定する。古いテスト用データのID形式にも対応する。"""
    doc_type = document.get("docType")
    if doc_type:
        return doc_type == "law"
    return str(document.get("documentId") or "").startswith("law-")


def _citations_from_items(items: list[dict[str, Any]]) -> list[Citation]:
    return [
        Citation(
            documentId=str(item["document"].get("documentId") or ""),
            contentUnitId=str(item["document"].get("contentUnitId") or ""),
            title=item["document"].get("title"),
            heading=item["document"].get("heading"),
            sourceObjectUri=item["document"].get("sourceObjectUri"),
            sourcePage=item["document"].get("sourcePage"),
            text=item["document"].get("text"),
            evidenceLane=_evidence_lane(item),
            evidenceRole=item.get("evidenceRole"),
        )
        for item in items
    ]


def _evidence_lane(item: dict[str, Any]) -> str:
    """法令レーンとガイドレーンを引用単位で区別する(§10)。"""
    lane = item.get("evidenceLane")
    if lane:
        return str(lane)
    return "law" if _is_law_document(item.get("document", {})) else "guidance"


def _layered_answer_control(trace: dict[str, Any]) -> dict[str, Any] | None:
    layered = trace.get("layeredLegalRetrieval") or {}
    if layered.get("mode") != "active":
        return None
    control = layered.get("answerControl") or {}
    status = control.get("answerStatus") or (
        layered.get("contextCoverage") or {}
    ).get("answerStatus")
    if not status:
        return None
    return {**control, "answerStatus": status}


def _graph_closure_citation_ids(
    ranked_items: Iterable[dict[str, Any]],
    limit: int,
) -> list[str]:
    """最終引用で確保する高信頼Graph根拠を、起点条文の関連順位で選ぶ。"""
    if limit <= 0:
        return []
    items = list(ranked_items)
    final_article_ranks = {
        str(
            item["document"].get("articleContentUnitId")
            or item["document"].get("contentUnitId")
            or ""
        ).split("-paragraph-", 1)[0]: rank
        for rank, item in enumerate(items)
    }
    relation_ranks = {"IMPLEMENTS": 0, "REFERENCES": 1, "APPLIED_BY": 2}
    candidates: list[tuple[tuple[int, int, int, str], str]] = []
    for target_rank, item in enumerate(items):
        if not item.get("citationClosure"):
            continue
        content_unit_id = str(item["document"].get("contentUnitId") or "")
        if not content_unit_id:
            continue
        source_final_rank = min(
            (
                final_article_ranks[source_id]
                for source_id in (item.get("graphSourceArticleIds") or [])
                if source_id in final_article_ranks
            ),
            default=len(items) + int(item.get("graphSourceRank", len(items))),
        )
        relation_rank = min(
            (
                relation_ranks[relation_type]
                for relation_type in (item.get("graphRelationTypes") or [])
                if relation_type in relation_ranks
            ),
            default=len(relation_ranks),
        )
        candidates.append(
            (
                (source_final_rank, relation_rank, target_rank, content_unit_id),
                content_unit_id,
            )
        )
    candidates.sort(key=lambda candidate: candidate[0])
    return list(dict.fromkeys(content_unit_id for _, content_unit_id in candidates))[:limit]


def _graph_closure_citations_for_request(
    request: AnswerRequest,
    ranked_items: Iterable[dict[str, Any]],
    raw_reranker_top_ids: set[str],
    limit: int,
) -> list[str]:
    """選択式だけ、再ランカー本来の上位にあるGraph根拠を引用枠へ確保する。

    自由入力は回答本文が挙げた引用を上限外でも回収できるため固定枠は不要。
    また、Graph pinで再ランカー下位から救済しただけの候補は、関係が正しくても
    質問との関連性が弱い可能性があるので最終引用へ強制しない。
    """
    if not request.choices:
        return []
    eligible = [
        item
        for item in ranked_items
        if item["document"].get("contentUnitId") in raw_reranker_top_ids
    ]
    return _graph_closure_citation_ids(eligible, limit)


def _assessment_citation_ids(
    assessments: dict[str, dict[str, Any]] | None,
    predicted_answer: str | None,
) -> list[str]:
    if not assessments:
        return []
    labels = [predicted_answer] if predicted_answer in assessments else []
    labels.extend(label for label in sorted(assessments) if label != predicted_answer)
    result = []
    for label in labels:
        for content_unit_id in assessments[label].get("citationIds", []):
            if content_unit_id and content_unit_id not in result:
                result.append(content_unit_id)
    return result


def _select_final_citations(
    candidates: list[Citation],
    assessment_citation_ids: list[str],
    citation_top_k: int,
    answer_text: str | None = None,
    expand_answer_citations: bool = True,
    structural_citation_ids: list[str] | None = None,
    fill_remaining: bool = True,
) -> list[Citation]:
    """回答の根拠として返す引用を選ぶ。

    高信頼Graph根拠 → 選択肢判定の根拠 → 回答本文が挙げたID →
    残りの上位候補、の順に採用する。
    本文が挙げたIDは、回答中の表記を利用者が引用一覧で確認できるよう、
    citation_top_k を超えても落とさない。

    ただし選択式は評価の採点対象で、引用件数が増えるとcitationHitが甘くなる。
    採点を歪めないよう、選択式では expand_answer_citations=False で上限を厳守する。
    """
    by_id = {citation.contentUnitId: citation for citation in candidates if citation.contentUnitId}
    prioritized_ids = list(dict.fromkeys(structural_citation_ids or []))
    prioritized_ids.extend(
        content_unit_id
        for content_unit_id in assessment_citation_ids
        if content_unit_id not in prioritized_ids
    )
    if expand_answer_citations:
        prioritized_ids.extend(
            content_unit_id
            for content_unit_id in _cited_content_unit_ids(answer_text, by_id.keys())
            if content_unit_id not in prioritized_ids
        )
    selected = [by_id[content_unit_id] for content_unit_id in prioritized_ids if content_unit_id in by_id]
    selected_ids = {citation.contentUnitId for citation in selected}
    remaining = [citation for citation in candidates if citation.contentUnitId not in selected_ids]
    if not fill_remaining:
        return selected[:citation_top_k]
    if not expand_answer_citations:
        return (selected + remaining)[:citation_top_k]
    return selected + remaining[: max(0, citation_top_k - len(selected))]


def _cited_content_unit_ids(
    answer_text: str | None,
    known_ids: Iterable[str],
) -> list[str]:
    """回答本文に書かれたcontentUnitIdを、本文中の登場順で返す。"""
    if not answer_text:
        return []
    found = []
    resolved_article_ids: set[str] = set()
    for content_unit_id in known_ids:
        if not content_unit_id:
            continue
        position = answer_text.find(content_unit_id)
        if position < 0 and content_unit_id.count("-paragraph-") == 1:
            # 条文は項単位で投入されるが、回答は条レベルのIDで引用することがある。
            # 「第2条」が「第27条」を巻き込まないよう、区切りまで含めて照合する。
            article_id = content_unit_id.split("-paragraph-", 1)[0]
            if article_id in resolved_article_ids:
                continue
            position = next(
                (
                    match.start()
                    for match in re.finditer(re.escape(article_id), answer_text)
                    if not answer_text[match.end() : match.end() + 1].isalnum()
                    and not answer_text.startswith("_", match.end())
                    and not answer_text.startswith("-", match.end())
                ),
                -1,
            )
            if position >= 0:
                # 同じ条は先頭（最上位ランク）の項だけを代表として引用する。
                resolved_article_ids.add(article_id)
        if position >= 0:
            found.append((position, content_unit_id))
    return [content_unit_id for _, content_unit_id in sorted(found)]


def _choice_evidence_matrix(
    request: AnswerRequest,
    citations: list[Citation],
) -> dict[str, list[str]]:
    if not request.choices:
        return {}
    matrix = {}
    for label, choice_text in sorted(request.choices.items()):
        ranked = sorted(
            citations,
            key=lambda citation: _text_coverage(
                choice_text,
                f"{citation.heading or ''} {citation.text or ''}",
            ),
            reverse=True,
        )
        matrix[label.upper()] = [
            citation.contentUnitId
            for citation in ranked[:2]
            if citation.contentUnitId
        ]
    return matrix


def _dedupe_graph_paths(paths: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped = {}
    for path in paths:
        edge_ids = tuple(edge.get("graphEdgeId") for edge in path.get("edges", []))
        node_ids = tuple(node.get("graphNodeId") for node in path.get("nodes", []))
        deduped[(node_ids, edge_ids)] = path
    return list(deduped.values())


def _graph_node_ids(paths: list[dict[str, Any]]) -> list[str]:
    return list(
        dict.fromkeys(
            node["graphNodeId"]
            for path in paths
            for node in path.get("nodes", [])
            if node.get("graphNodeId")
        )
    )


def _graph_edge_ids(paths: list[dict[str, Any]]) -> list[str]:
    return list(
        dict.fromkeys(
            edge["graphEdgeId"]
            for path in paths
            for edge in path.get("edges", [])
            if edge.get("graphEdgeId")
        )
    )


def _append_route(route: list[str], step: str) -> None:
    if step not in route:
        route.append(step)


def _can_call_tool(tool_calls: int, deadline: float) -> bool:
    return tool_calls < settings.agent_max_total_tool_calls and perf_counter() < deadline
