import re
from time import perf_counter
from typing import Any

from .config import settings
from .graph_client import GraphClient
from .llm import LLMClient
from .models import AnswerRequest, AnswerResponse, Citation
from .opensearch_client import OpenSearchClient


REFERENCE_CUES = ("前条", "次条", "同条", "前項", "同項", "同号", "ただし", "定義", "準用", "除く")
# 項・号内の参照。条ノードから兄弟の項・号を辿れば解決できる参照語。
SIBLING_REFERENCE_CUES = ("前項", "同項", "次項", "前二項", "前三項", "各項", "前各項", "前号", "同号", "次号", "各号", "前各号")
ARTICLE_REFERENCE_PATTERN = re.compile(r"第[一二三四五六七八九十百千〇零\d]+条")
HIERARCHY_EDGE_TYPE = "HAS_CONTENT_UNIT"
HIERARCHY_MAX_DEPTH = 2


class AgentService:
    def __init__(
        self,
        os_client: OpenSearchClient,
        graph_client: GraphClient,
        llm_client: LLMClient,
    ) -> None:
        self.os_client = os_client
        self.graph_client = graph_client
        self.llm_client = llm_client

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
            results = self.os_client.search(
                query,
                "law",
                candidate_top_k,
                request.userClearanceLevel,
                use_bm25=settings.agent_use_bm25,
                use_vector=settings.agent_use_vector,
            )
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
                    "useBm25": settings.agent_use_bm25,
                    "useVector": settings.agent_use_vector,
                }
            )

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
                results = self.os_client.search(
                    query,
                    "law",
                    candidate_top_k,
                    request.userClearanceLevel,
                    use_bm25=settings.agent_use_bm25,
                    use_vector=settings.agent_use_vector,
                )
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

        citations = _citations_from_evidence(evidence, request, rerank_top_k, request.topK)
        evidence_by_choice = _choice_evidence_matrix(request, citations)
        trace["choiceEvidence"] = evidence_by_choice
        _append_route(route, "evidence_merge")
        _append_route(route, "answer_composer")
        answer_text, predicted_answer, judgements = self._compose_answer(
            request,
            route,
            citations,
            trace,
            deadline,
            evidence_by_choice,
        )
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
            int(bool(trace.get(key, {}).get("used")))
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
            "fallback": None if result.queries else "rule_based_decomposition",
            "queries": queries,
            "graphRequired": result.graphRequired,
        }
        return queries, result.graphRequired

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
        new_count = 0
        for document in documents[:candidate_top_k]:
            content_unit_id = document["contentUnitId"]
            if content_unit_id not in evidence:
                evidence[content_unit_id] = {
                    "document": document,
                    "score": 0.9 / (settings.agent_rrf_k + 1),
                    "sources": ["graph_expansion"],
                    "queries": [],
                    "introducedBy": "graph_expansion",
                }
                new_count += 1
            else:
                evidence[content_unit_id]["score"] += 0.35 / (settings.agent_rrf_k + 1)
                evidence[content_unit_id]["sources"].append("graph_expansion")
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
        evidence_by_choice: dict[str, list[str]],
    ) -> tuple[str, str | None, dict[str, str] | None]:
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
                        }
                        answer_text = llm_result.text or "十分な引用根拠を取得できなかったため、断定回答は行いません。"
                        return answer_text, llm_result.predictedAnswer, llm_result.choiceJudgements

        if request.choices:
            return (
                "LLM 未使用のため選択肢判定は行いません。 評価時は predictedAnswer を null として扱ってください。",
                None,
                None,
            )
        if citations:
            return (
                "検索された根拠候補に基づく回答です。法的判断は引用元を確認し、必要に応じて専門家確認を行ってください。",
                None,
                None,
            )
        return "十分な引用根拠を取得できなかったため、断定回答は行いません。", None, None


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
    ranked = sorted(evidence.values(), key=lambda item: item["score"], reverse=True)[:rerank_top_k]
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
