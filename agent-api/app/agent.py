import re
from time import perf_counter
from typing import Any

from .config import settings
from .graph_client import GraphClient
from .llm import LLMClient
from .models import AnswerRequest, AnswerResponse, Citation
from .reranker import RerankerClient
from .seed import _japanese_number_to_int
from .opensearch_client import OpenSearchClient

REFERENCE_CUES = ("前条", "次条", "同条", "前項", "同項", "同号", "ただし", "定義", "準用", "除く")
# 項・号内の参照。条ノードから兄弟の項・号を辿れば解決できる参照語。
SIBLING_REFERENCE_CUES = ("前項", "同項", "次項", "前二項", "前三項", "各項", "前各項", "前号", "同号", "次号", "各号", "前各号")
ARTICLE_REFERENCE_PATTERN = re.compile(r"第[一二三四五六七八九十百千〇零\d]+条")
# 質問・選択肢中の「第N条(のM)」抽出用。seed.py のパターンと異なり「〜法第N条」も対象にする。
QUESTION_ARTICLE_PATTERN = re.compile(
    r"第([0-9一二三四五六七八九十百千〇零]+)条((?:の[0-9一二三四五六七八九十百千〇零]+)*)"
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
        trace: dict[str, Any] = {
            "rounds": [],
            "inputType": self._input_type(request),
            "limits": {
                "maxQueries": settings.agent_max_queries,
                "maxRetryRounds": settings.agent_max_retry_rounds,
                "maxTotalToolCalls": settings.agent_max_total_tool_calls,
                "maxGraphHop": settings.agent_max_graph_hop,
                "maxGraphPaths": settings.agent_max_graph_paths,
                "maxWallTimeSec": settings.agent_max_wall_time_sec,
                "candidateTopK": candidate_top_k,
                "rerankTopK": rerank_top_k,
                "citationTopK": request.topK,
                "maxLlmCalls": settings.agent_max_llm_calls,
            },
        }
        evidence: dict[str, dict[str, Any]] = {}
        graph_paths: list[dict[str, Any]] = []
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
            paths, new_count = self._expand_graph(request, evidence, rerank_top_k, candidate_top_k)
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
                retry_rounds = retry_round
                new_count = _merge_search_results(evidence, results, query, "follow_up_search")
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
        rerank_candidates = _fusion_ranked_evidence(evidence, rerank_candidate_top_k)
        fusion_ranked = rerank_candidates[:rerank_top_k]
        trace["candidatePoolContentUnitIds"] = list(evidence)
        trace["fusionTopContentUnitIds"] = [item["document"]["contentUnitId"] for item in fusion_ranked]
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
            final_ranked = _pin_ranked_evidence(rerank_result.items, rerank_top_k)
            trace["reranker"] = {
                "used": rerank_result.used,
                "provider": rerank_result.provider,
                "model": rerank_result.model,
                "latencyMs": rerank_result.latency_ms,
                "error": rerank_result.error,
                "fallback": None if rerank_result.used else "fusion_ranking",
                "candidateCount": len(rerank_candidates),
                "scores": rerank_result.scores,
            }
            if rerank_result.used:
                _append_route(route, "evidence_reranker")
        trace["rerankerTopContentUnitIds"] = [
            item["document"]["contentUnitId"] for item in final_ranked
        ]
        answer_candidates = _citations_from_items(final_ranked)
        _append_route(route, "answer_composer")
        answer_text, predicted_answer, judgements, assessment_citation_ids = self._compose_answer(
            request,
            route,
            answer_candidates,
            trace,
            deadline,
            None,
        )
        citations = _select_final_citations(answer_candidates, assessment_citation_ids, request.topK)
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

    def _search_evidence(
        self,
        query: str,
        law_top_k: int,
        user_clearance_level: int,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """法令とガイドラインを別候補プールで検索し、片方の母集団の大きさで取りこぼさない。"""
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
        merged: dict[str, dict[str, Any]] = {}
        for results in result_groups.values():
            for result in results:
                content_unit_id = str(result["document"]["contentUnitId"])
                existing = merged.get(content_unit_id)
                if existing is None or float(result.get("score", 0.0)) > float(existing.get("score", 0.0)):
                    merged[content_unit_id] = result
        ranked = sorted(merged.values(), key=lambda result: float(result.get("score", 0.0)), reverse=True)
        return ranked, {source: len(results) for source, results in result_groups.items()}

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
        try:
            result = self.llm_client.plan_search(
                request,
                settings.agent_max_queries,
                timeout_sec=max(1, min(settings.planner_timeout_sec, remaining)),
            )
        except Exception as exc:
            queries = _rule_based_decomposition(request, settings.agent_max_queries)
            trace["planner"] = {
                "used": False,
                "error": str(exc),
                "attemptCount": 1,
                "fallback": "rule_based_decomposition",
                "queries": queries,
            }
            return queries, _query_has_reference_cues(request)

        queries = list(dict.fromkeys([fallback[0], *result.queries]))[: settings.agent_max_queries]
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
    ) -> int | None:
        """質問・選択肢に明示された「法令名+第N条」を検索スコアに依らず候補プールへ直接投入する。

        埋め込み・BM25は条番号の完全一致を保証しないため、明示参照は検索とは別経路で解決する。
        対象法令は本文中に法令名が現れるものに限定し、無ければ既存候補の上位法令に絞る。
        """
        text = _search_query(request)
        all_suffixes = _extract_article_suffixes(text)
        if not all_suffixes:
            return None
        try:
            titles = self.os_client.law_titles()
            references = _law_article_references(text, titles)
            if not references:
                matched_laws = _matched_law_ids(text, titles)
                references = {law_id: all_suffixes for law_id in matched_laws}
            if not references:
                ranked = sorted(evidence.values(), key=lambda item: item["score"], reverse=True)
                matched_laws = list(
                    dict.fromkeys(item["document"].get("documentId") for item in ranked if item["document"].get("documentId"))
                )[:2]
                references = {law_id: all_suffixes for law_id in matched_laws}
            if not references:
                return None
            article_ids = [
                f"{law_id}-article-{suffix}"
                for law_id, suffixes in references.items()
                for suffix in suffixes
            ]
            documents = self.os_client.get_by_article_ids(
                article_ids,
                request.userClearanceLevel,
                max_chunks=max(100, len(article_ids) * 30),
            )
        except Exception as exc:
            trace["rounds"].append({"round": 0, "tool": "article_direct_lookup", "error": str(exc)})
            return None
        selected_documents = _select_direct_documents(request, documents, DIRECT_CHUNKS_PER_ARTICLE)
        new_count = _merge_direct_documents_into_evidence(
            evidence, selected_documents, "article_reference", direct_reference=True
        )
        trace["rounds"].append(
            {
                "round": 0,
                "tool": "article_direct_lookup",
                "references": references,
                "resultCount": len(documents),
                "selectedCount": len(selected_documents),
                "newContentUnitCount": new_count,
            }
        )
        return new_count

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
        ranked_evidence = sorted(evidence.values(), key=lambda item: item["score"], reverse=True)[:rerank_top_k]
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
        except Exception as exc:
            trace["rounds"].append({"round": 0, "tool": "guidance_explains_lookup", "error": str(exc)})
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
        except Exception as exc:
            trace["rounds"].append({"round": 0, "tool": "guidance_explains_lookup", "error": str(exc)})
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
            if any(cue in text for cue in REFERENCE_CUES[:6]):
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
        except Exception as exc:
            trace["evaluator"] = {
                "provider": self.llm_client.provider,
                "used": False,
                "error": str(exc),
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
    ) -> tuple[list[dict[str, Any]], int]:
        start_ids = []
        article_ids = []
        ranked_evidence = sorted(evidence.values(), key=lambda item: item["score"], reverse=True)[:rerank_top_k]
        for item in ranked_evidence:
            document = item["document"]
            start_ids.append(document["contentUnitId"])
            if document.get("parentContentUnitId"):
                start_ids.append(document["parentContentUnitId"])
            article_id = document.get("articleContentUnitId")
            if article_id:
                start_ids.append(article_id)
                article_ids.append(article_id)
        paths = self.graph_client.paths_from_many(
            list(dict.fromkeys(start_ids)),
            edge_type="REFERENCES",
            max_depth=settings.agent_max_graph_hop,
            limit=settings.agent_max_graph_paths,
            user_clearance_level=request.userClearanceLevel,
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
        query_text = _search_query(request)
        documents.sort(
            key=lambda document: _text_coverage(
                query_text,
                f"{document.get('heading') or ''} {document.get('text') or ''}",
            ),
            reverse=True,
        )
        new_count = 0
        for rank, document in enumerate(documents[:candidate_top_k], start=1):
            content_unit_id = document["contentUnitId"]
            graph_score = 0.35 / (settings.agent_rrf_k + rank)
            if content_unit_id not in evidence:
                evidence[content_unit_id] = {
                    "document": document,
                    "score": graph_score,
                    "sources": ["graph_expansion"],
                    "queries": [],
                    "introducedBy": "graph_expansion",
                }
                new_count += 1
            else:
                item = evidence[content_unit_id]
                item["score"] += graph_score
                item["sources"].append("graph_expansion")
        return paths, new_count

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
    ) -> tuple[str, str | None, dict[str, str] | None, list[str]]:
        if citations:
            remaining = int(deadline - perf_counter())
            if remaining <= 1:
                trace["llm"] = {"provider": self.llm_client.provider, "used": False, "error": "time_budget_exhausted"}
            else:
                try:
                    llm_result = self.llm_client.generate_answer(
                        request,
                        route,
                        citations,
                        timeout_sec=max(1, min(settings.llm_timeout_sec, remaining)),
                        evidence_by_choice=evidence_by_choice,
                    )
                except Exception as exc:
                    trace["llm"] = {
                        "provider": self.llm_client.provider,
                        "used": False,
                        "error": str(exc),
                        "attemptCount": 1,
                        "fallback": "no_choice_judgement",
                    }
                else:
                    if llm_result:
                        trace["llm"] = {
                            "provider": llm_result.provider,
                            "model": llm_result.model,
                            "used": True,
                            "latencyMs": llm_result.latencyMs,
                            "inputTokens": llm_result.inputTokens,
                            "outputTokens": llm_result.outputTokens,
                            "estimatedCost": llm_result.estimatedCost,
                            "validationError": llm_result.validationError,
                            "stopReason": llm_result.stopReason,
                            "contentBlockTypes": llm_result.contentBlockTypes,
                            "outputChars": llm_result.outputChars,
                            "retryCount": llm_result.retryCount,
                            "attemptCount": 1 + llm_result.retryCount,
                            "questionPolarity": llm_result.questionPolarity,
                            "choiceAssessments": llm_result.choiceAssessments,
                        }
                        answer_text = llm_result.text or "十分な引用根拠を取得できなかったため、断定回答は行いません。"
                        assessment_ids = _assessment_citation_ids(
                            llm_result.choiceAssessments,
                            llm_result.predictedAnswer,
                        )
                        return answer_text, llm_result.predictedAnswer, llm_result.choiceJudgements, assessment_ids

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


def _merge_direct_documents_into_evidence(
    evidence: dict[str, dict[str, Any]],
    selected_documents: list[dict[str, Any]],
    source_tag: str,
    *,
    direct_reference: bool = False,
) -> int:
    """条番号で直接取得したselected_documentsを、article_ranksでmustInclude判定しつつ
    evidence辞書へマージする。新規追加件数を返す。"""
    new_count = 0
    article_ranks: dict[str, int] = {}
    for document in selected_documents:
        content_unit_id = document["contentUnitId"]
        article_id = str(document.get("articleContentUnitId") or content_unit_id).split("-paragraph-", 1)[0]
        article_ranks[article_id] = article_ranks.get(article_id, 0) + 1
        must_include = article_ranks[article_id] == 1
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


def _matched_law_ids(text: str, titles: dict[str, str]) -> list[str]:
    """本文中に法令名が現れる法令を返す。親法名が施行令等の名前の一部として現れただけの場合は除外する。"""
    matched = {doc_id: title for doc_id, title in titles.items() if title and title in text}
    result = []
    for doc_id, title in matched.items():
        occurrences = text.count(title)
        covered = sum(
            text.count(other_title)
            for other_id, other_title in matched.items()
            if other_id != doc_id and title in other_title and len(other_title) > len(title)
        )
        if occurrences > covered:
            result.append(doc_id)
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


def _select_direct_documents(
    request: AnswerRequest,
    documents: list[dict[str, Any]],
    per_article: int,
) -> list[dict[str, Any]]:
    """直接解決した条ごとに、質問・選択肢へ最も近いチャンクだけを残す。"""
    query_parts = [request.question, *(request.choices or {}).values()]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for document in documents:
        content_unit_id = str(document.get("contentUnitId") or "")
        article_id = str(document.get("articleContentUnitId") or content_unit_id).split("-paragraph-", 1)[0]
        grouped.setdefault(article_id, []).append(document)

    selected = []
    for article_id in sorted(grouped):
        ranked = sorted(
            grouped[article_id],
            key=lambda document: max(
                _text_coverage(
                    part,
                    f"{document.get('heading') or ''} {document.get('text') or ''}",
                )
                for part in query_parts
            ),
            reverse=True,
        )
        selected.extend(ranked[:per_article])
    return selected


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
                "introducedBy": source,
            }
            new_count += 1
        else:
            evidence[content_unit_id]["score"] += rank_score
            evidence[content_unit_id]["sources"].append(source)
            evidence[content_unit_id]["queries"].append(query)
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
    """RRF上位へ、明示条番号から直接解決した各条の代表チャンクを必ず含める。"""
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
        if len(ranked) >= rerank_top_k:
            break
    return ranked


def _pin_ranked_evidence(
    ordered: list[dict[str, Any]],
    top_k: int,
) -> list[dict[str, Any]]:
    """専用リランカー適用後も、明示条番号から得た必須根拠を上位枠に保持する。"""
    pinned = [item for item in ordered if item.get("mustInclude")]
    ranked = []
    seen = set()
    for item in [*pinned, *ordered]:
        content_unit_id = item["document"]["contentUnitId"]
        if content_unit_id in seen:
            continue
        ranked.append(item)
        seen.add(content_unit_id)
        if len(ranked) >= top_k:
            break
    return ranked


def _citations_from_items(items: list[dict[str, Any]]) -> list[Citation]:
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
        for item in items
    ]


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
) -> list[Citation]:
    by_id = {citation.contentUnitId: citation for citation in candidates if citation.contentUnitId}
    selected = [by_id[content_unit_id] for content_unit_id in assessment_citation_ids if content_unit_id in by_id]
    selected_ids = {citation.contentUnitId for citation in selected}
    selected.extend(citation for citation in candidates if citation.contentUnitId not in selected_ids)
    return selected[:citation_top_k]


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
