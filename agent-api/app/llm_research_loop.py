"""LLM判断と投入済み検索ツールを反復する法令調査ループ。"""

from dataclasses import dataclass
from time import perf_counter
from typing import Any

import requests

from .config import settings
from .graph_client import GraphClient
from .llm_directed_research import (
    RESEARCH_STATUS_INSUFFICIENT,
    RESEARCH_STATUS_READY,
    TOOL_FETCH_ARTICLES,
    EvidenceCatalog,
    validate_research_turn,
)
from .llm_research_tools import LegalResearchToolGateway
from .models import AnswerRequest
from .opensearch_client import OpenSearchClient


@dataclass(frozen=True)
class LLMResearchOutcome:
    selected_content_unit_ids: tuple[str, ...]
    selected_evidence: tuple[dict[str, Any], ...]
    trace: dict[str, Any]


def run_llm_directed_research(
    *,
    request: AnswerRequest,
    os_client: OpenSearchClient,
    graph_client: GraphClient,
    llm_client: Any,
    deadline: float,
) -> LLMResearchOutcome:
    """LLM主導探索を実行し、LLMが選んだ検証済み本文を回答用に返す。"""
    return _run_llm_directed_research(
        request=request,
        os_client=os_client,
        graph_client=graph_client,
        llm_client=llm_client,
        deadline=deadline,
        mode="active",
        budget_cap_sec=settings.llm_research_active_budget_sec,
        connected_to_answer=True,
    )


def run_llm_directed_research_shadow(
    *,
    request: AnswerRequest,
    os_client: OpenSearchClient,
    graph_client: GraphClient,
    llm_client: Any,
    deadline: float,
) -> LLMResearchOutcome:
    """現行回答を変更せず、LLM主導探索の判断・取得結果をtraceへ残す。"""
    return _run_llm_directed_research(
        request=request,
        os_client=os_client,
        graph_client=graph_client,
        llm_client=llm_client,
        deadline=deadline,
        mode="shadow",
        budget_cap_sec=settings.llm_research_shadow_budget_sec,
        connected_to_answer=False,
    )


def _run_llm_directed_research(
    *,
    request: AnswerRequest,
    os_client: OpenSearchClient,
    graph_client: GraphClient,
    llm_client: Any,
    deadline: float,
    mode: str,
    budget_cap_sec: int,
    connected_to_answer: bool,
) -> LLMResearchOutcome:
    started = perf_counter()
    # 回答生成の上限時間を確保し、調査が最終回答の時間を消費しないようにする。
    answer_reserve = max(
        settings.agent_answer_reserve_sec,
        settings.llm_timeout_sec,
    )
    available = deadline - started - answer_reserve
    budget = min(float(budget_cap_sec), max(0.0, available))
    phase_deadline = started + budget
    base_trace: dict[str, Any] = {
        "enabled": True,
        "mode": mode,
        "connectedToAnswer": connected_to_answer,
        "availableBeforeCapMs": max(0, int(available * 1000)),
        "budgetMs": int(budget * 1000),
        "answerReserveMs": answer_reserve * 1000,
        "maxTurns": settings.llm_research_max_turns,
        "maxActionsPerTurn": settings.llm_research_max_actions_per_turn,
        "maxToolCalls": settings.llm_research_max_tool_calls,
        "globalSearchTopK": settings.llm_research_search_top_k,
        "documentSearchTopK": settings.llm_research_document_search_top_k,
        "turns": [],
        "toolExecutions": [],
    }
    if budget <= 1:
        return _finish(
            base_trace,
            started,
            EvidenceCatalog(),
            status="not_started",
            stop_reason=f"insufficient_{mode}_time_budget",
            incomplete=True,
        )

    catalog = EvidenceCatalog()
    try:
        catalog.add_documents(os_client.law_titles())
        base_trace["documentCatalogCount"] = len(catalog.known_document_ids)
    except Exception as exc:  # noqa: BLE001 - 文書一覧なしでも通常検索は可能
        base_trace["documentCatalogCount"] = 0
        base_trace["documentCatalogError"] = f"{type(exc).__name__}: {exc}"
    gateway = LegalResearchToolGateway(os_client, graph_client)
    tool_history: list[dict[str, Any]] = []
    tool_calls = 0
    llm_calls = 0
    selected_ids: tuple[str, ...] = ()
    last_valid_selected_ids: tuple[str, ...] = ()
    recent_direct_content_ids: tuple[str, ...] = ()
    status = "incomplete"
    stop_reason = "max_turns_reached"

    for turn_index in range(settings.llm_research_max_turns):
        remaining = phase_deadline - perf_counter()
        if remaining <= 1:
            stop_reason = f"{mode}_time_budget_exhausted"
            break
        remaining_turns = settings.llm_research_max_turns - turn_index
        remaining_tool_calls = max(
            0,
            settings.llm_research_max_tool_calls - tool_calls,
        )
        # 最後のLLM呼び出しは必ず、追加探索ではなく証拠選択と回答可否の
        # 確定に使う。ツール予算を使い切った場合も同じ終端契約へ切り替える。
        finalize_only = remaining_turns == 1 or remaining_tool_calls == 0
        preferred_content_ids = tuple(
            dict.fromkeys(
                [
                    *recent_direct_content_ids,
                    *last_valid_selected_ids,
                ]
            )
        )
        llm_calls += 1
        try:
            result = llm_client.decide_legal_research_turn(
                request,
                catalog,
                tool_history,
                timeout_sec=max(
                    1,
                    min(settings.llm_research_timeout_sec, int(remaining)),
                ),
                remaining_turns=remaining_turns,
                remaining_tool_calls=remaining_tool_calls,
                finalize_only=finalize_only,
                preferred_content_ids=preferred_content_ids,
            )
        except requests.Timeout as exc:
            status = "timeout"
            stop_reason = "llm_timeout"
            base_trace["timeout"] = {
                "component": "llm_research_decision",
                "turnIndex": turn_index,
                "error": f"{type(exc).__name__}: {exc}",
            }
            break
        except requests.ConnectionError as exc:
            status = "transport_error"
            stop_reason = "llm_connection_error"
            base_trace["transportError"] = {
                "component": "llm_research_decision",
                "turnIndex": turn_index,
                "error": f"{type(exc).__name__}: {exc}",
            }
            break
        except Exception as exc:  # noqa: BLE001 - traceへ分類して呼び出し元で扱う
            status = "internal_error"
            stop_reason = "llm_call_error"
            base_trace["error"] = f"{type(exc).__name__}: {exc}"
            break
        turn_trace = {
            "turnIndex": turn_index,
            **result.as_trace(),
        }
        base_trace["turns"].append(turn_trace)
        if result.turn is None:
            status = "invalid"
            stop_reason = "llm_output_invalid"
            tool_history.append(
                {
                    "turnIndex": turn_index,
                    "validationErrors": [
                        result.validationError or "structured_output_invalid"
                    ],
                    "executed": False,
                }
            )
            continue

        validation = validate_research_turn(
            result.turn,
            catalog,
            finalize_only=finalize_only,
        )
        turn_trace["validation"] = validation.as_trace()
        if not validation.valid:
            # エラー内容を次ターンへ返して自己修正を1回以上許す。DB操作は実行しない。
            tool_history.append(
                {
                    "turnIndex": turn_index,
                    "validationErrors": list(validation.errors),
                    "executed": False,
                }
            )
            status = "invalid"
            stop_reason = "llm_decision_rejected"
            continue
        if validation.selected_content_unit_ids:
            # continue中にもLLMは確認済み根拠を選べる。後続の最終判断が
            # timeout・形式不正でも、最後に検証できた選択を失わない。
            last_valid_selected_ids = validation.selected_content_unit_ids

        tool_history.append(
            {
                "turnIndex": turn_index,
                "decision": {
                    "status": result.turn.status,
                    "reason": result.turn.reason,
                    "missingEvidence": list(result.turn.missingEvidence),
                    "selectedEvidence": [
                        item.model_dump() for item in result.turn.selectedEvidence
                    ],
                    "actions": [item.model_dump() for item in result.turn.actions],
                },
                "executed": False,
            }
        )
        if result.turn.status == RESEARCH_STATUS_READY:
            selected_ids = validation.selected_content_unit_ids
            status = RESEARCH_STATUS_READY
            stop_reason = "llm_ready"
            break
        if result.turn.status == RESEARCH_STATUS_INSUFFICIENT:
            selected_ids = validation.selected_content_unit_ids
            status = RESEARCH_STATUS_INSUFFICIENT
            stop_reason = "llm_insufficient"
            break

        next_recent_direct_content_ids: list[str] = []
        for action in result.turn.actions:
            if tool_calls >= settings.llm_research_max_tool_calls:
                stop_reason = "max_tool_calls_reached"
                break
            remaining = phase_deadline - perf_counter()
            if remaining <= 0.1:
                stop_reason = f"{mode}_time_budget_exhausted"
                break
            execution = gateway.execute(
                action,
                catalog,
                user_clearance_level=request.userClearanceLevel,
                timeout_sec=remaining,
            )
            execution_trace = execution.as_trace(action)
            execution_trace["turnIndex"] = turn_index
            base_trace["toolExecutions"].append(execution_trace)
            tool_history.append(execution_trace)
            if action.tool == TOOL_FETCH_ARTICLES:
                next_recent_direct_content_ids.extend(
                    execution.new_content_unit_ids
                )
            tool_calls += 1
        recent_direct_content_ids = tuple(
            dict.fromkeys(next_recent_direct_content_ids)
        )
        if stop_reason in {
            "max_tool_calls_reached",
            f"{mode}_time_budget_exhausted",
        }:
            break

    base_trace["llmCallCount"] = llm_calls
    base_trace["toolCallCount"] = tool_calls
    if not selected_ids and (
        last_valid_selected_ids or recent_direct_content_ids
    ):
        selected_ids = tuple(
            dict.fromkeys(
                [
                    *last_valid_selected_ids,
                    *recent_direct_content_ids,
                ]
            )
        )[: settings.llm_research_max_selected_evidence]
        if last_valid_selected_ids:
            base_trace["selectionRecoveredFromLastValidTurn"] = True
        if recent_direct_content_ids:
            base_trace["recentDirectEvidenceAddedForPartialAnswer"] = list(
                recent_direct_content_ids
            )
        if status not in {
            RESEARCH_STATUS_READY,
            RESEARCH_STATUS_INSUFFICIENT,
        }:
            status = "partial"
    return _finish(
        base_trace,
        started,
        catalog,
        status=status,
        stop_reason=stop_reason,
        incomplete=status != RESEARCH_STATUS_READY,
        selected_ids=selected_ids,
    )


def _finish(
    trace: dict[str, Any],
    started: float,
    catalog: EvidenceCatalog,
    *,
    status: str,
    stop_reason: str,
    incomplete: bool,
    selected_ids: tuple[str, ...] = (),
) -> LLMResearchOutcome:
    trace.update(
        {
            "status": status,
            "stopReason": stop_reason,
            "incomplete": incomplete,
            "elapsedMs": int((perf_counter() - started) * 1000),
            "availableEvidenceContentUnitIds": list(catalog.content_unit_ids),
            "knownArticleIds": list(catalog.known_article_ids),
            "selectedContentUnitIds": list(selected_ids),
            "llmCallCount": int(trace.get("llmCallCount") or 0),
            "toolCallCount": int(trace.get("toolCallCount") or 0),
        }
    )
    return LLMResearchOutcome(
        selected_content_unit_ids=selected_ids,
        selected_evidence=tuple(catalog.items_by_ids(selected_ids)),
        trace=trace,
    )
