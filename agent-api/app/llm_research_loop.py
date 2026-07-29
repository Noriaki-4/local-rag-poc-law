"""LLM判断と投入済み検索ツールを反復する法令調査ループ。"""

from dataclasses import dataclass
from time import perf_counter
from typing import Any

import requests

from .config import settings
from .graph_client import GraphClient
from .llm_directed_research import (
    RESEARCH_STATUS_CONTINUE,
    RESEARCH_STATUS_INSUFFICIENT,
    RESEARCH_STATUS_READY,
    TOOL_FETCH_ARTICLES,
    EvidenceCatalog,
    ResearchCheckpoint,
    sanitize_research_checkpoint,
    validate_research_checkpoint,
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
    if getattr(llm_client, "supports_iterative_research", False):
        return _run_iterative_research(
            request=request,
            os_client=os_client,
            graph_client=graph_client,
            llm_client=llm_client,
            deadline=deadline,
            mode=mode,
            budget_cap_sec=budget_cap_sec,
            connected_to_answer=connected_to_answer,
        )
    return _run_legacy_research(
        request=request,
        os_client=os_client,
        graph_client=graph_client,
        llm_client=llm_client,
        deadline=deadline,
        mode=mode,
        budget_cap_sec=budget_cap_sec,
        connected_to_answer=connected_to_answer,
    )


def _run_iterative_research(
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
    """探索・掘り下げ・統合を1組とし、結論と根拠階層を引き継いで反復する。"""
    started = perf_counter()
    answer_reserve = max(
        settings.agent_answer_reserve_sec,
        settings.llm_timeout_sec,
    )
    available = deadline - started - answer_reserve
    budget = min(float(budget_cap_sec), max(0.0, available))
    phase_deadline = started + budget
    cycle_count = settings.llm_research_max_turns
    trace: dict[str, Any] = {
        "enabled": True,
        "mode": mode,
        "algorithm": "iterative_cycles_v5_state_compression",
        "connectedToAnswer": connected_to_answer,
        "availableBeforeCapMs": max(0, int(available * 1000)),
        "budgetMs": int(budget * 1000),
        "answerReserveMs": answer_reserve * 1000,
        "maxTurns": cycle_count,
        "maxActionsPerTurn": settings.llm_research_max_actions_per_turn,
        "maxToolCalls": settings.llm_research_max_tool_calls,
        "globalSearchTopK": settings.llm_research_search_top_k,
        "documentSearchTopK": (
            settings.llm_research_document_search_top_k
        ),
        "cycles": [],
        "turns": [],
        "toolExecutions": [],
    }
    if budget <= 1:
        return _finish(
            trace,
            started,
            EvidenceCatalog(),
            status="not_started",
            stop_reason=f"insufficient_{mode}_time_budget",
            incomplete=True,
        )

    catalog = EvidenceCatalog()
    try:
        catalog.add_documents(os_client.law_titles())
        trace["documentCatalogCount"] = len(catalog.known_document_ids)
    except Exception as exc:  # noqa: BLE001
        trace["documentCatalogCount"] = 0
        trace["documentCatalogError"] = f"{type(exc).__name__}: {exc}"

    gateway = LegalResearchToolGateway(os_client, graph_client)
    checkpoint = ResearchCheckpoint(
        status=RESEARCH_STATUS_CONTINUE,
        conclusion="",
    )
    last_valid_checkpoint: ResearchCheckpoint | None = None
    tool_calls = 0
    llm_calls = 0
    stop_reason = "max_cycles_reached"
    failure_status: str | None = None
    recovery_content_ids: tuple[str, ...] = ()

    for cycle_index in range(cycle_count):
        cycle_started = perf_counter()
        remaining_total = phase_deadline - cycle_started
        if remaining_total <= 1:
            stop_reason = f"{mode}_time_budget_exhausted"
            failure_status = "timeout"
            break
        cycles_left = max(1, cycle_count - cycle_index)
        cycle_budget = remaining_total / cycles_left
        cycle_deadline = min(
            phase_deadline,
            cycle_started + cycle_budget,
        )
        # 探索・掘り下げがサイクル時間を使い切らないよう、統合呼び出しを
        # 明示的に予約する。未使用分は次サイクルの再計算時に繰り越される。
        integration_reserve = min(
            60.0,
            max(20.0, cycle_budget * 0.4),
        )
        cycle_trace: dict[str, Any] = {
            "cycleIndex": cycle_index,
            "budgetMs": int(cycle_budget * 1000),
            "integrationReserveMs": int(integration_reserve * 1000),
            "stages": [],
            "toolExecutions": [],
            "skippedPhases": [],
        }
        trace["cycles"].append(cycle_trace)
        cycle_history: list[dict[str, Any]] = []
        cycle_new_content_ids: list[str] = []
        cycle_direct_content_ids: list[str] = []
        latest_valid_stage_turn: Any | None = None

        for phase in ("explore", "deepen"):
            future_stage_reserve = (
                min(20.0, cycle_budget * 0.15)
                if phase == "explore"
                else 0.0
            )
            remaining = (
                cycle_deadline
                - perf_counter()
                - integration_reserve
                - future_stage_reserve
            )
            if remaining <= 1:
                cycle_trace["skippedPhases"].append(
                    {
                        "phase": phase,
                        "reason": "integration_time_reserved",
                    }
                )
                break
            checkpoint_ids = tuple(checkpoint.evidenceIds)
            preferred_ids = tuple(
                dict.fromkeys(
                    [
                        *catalog.diversify_content_ids(
                            cycle_new_content_ids
                        ),
                        *recovery_content_ids,
                        *checkpoint_ids,
                        *checkpoint.openEvidenceIds,
                    ]
                )
            )
            llm_calls += 1
            try:
                result = llm_client.decide_legal_research_turn(
                    request,
                    catalog,
                    cycle_history,
                    timeout_sec=max(
                        1,
                        min(
                            settings.llm_research_timeout_sec,
                            int(remaining),
                        ),
                    ),
                    remaining_turns=cycle_count - cycle_index,
                    remaining_tool_calls=max(
                        0,
                        settings.llm_research_max_tool_calls - tool_calls,
                    ),
                    finalize_only=False,
                    preferred_content_ids=preferred_ids,
                    phase=phase,
                    cycle_index=cycle_index,
                    cycle_count=cycle_count,
                    checkpoint=checkpoint,
                )
            except requests.Timeout as exc:
                cycle_trace["stageTimeout"] = {
                    "component": f"llm_research_{phase}",
                    "cycleIndex": cycle_index,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                cycle_trace["skippedPhases"].append(
                    {
                        "phase": phase,
                        "reason": "stage_llm_timeout",
                    }
                )
                break
            except requests.ConnectionError as exc:
                failure_status = "transport_error"
                stop_reason = "llm_connection_error"
                trace["transportError"] = {
                    "component": f"llm_research_{phase}",
                    "cycleIndex": cycle_index,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                break
            except Exception as exc:  # noqa: BLE001
                if _is_provider_quota_error(exc):
                    failure_status = "provider_quota_error"
                    stop_reason = "llm_provider_quota_error"
                    trace["providerError"] = {
                        "component": f"llm_research_{phase}",
                        "cycleIndex": cycle_index,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                else:
                    failure_status = "internal_error"
                    stop_reason = "llm_call_error"
                    trace["error"] = f"{type(exc).__name__}: {exc}"
                break

            stage_trace = {
                "phase": phase,
                **result.as_trace(),
            }
            cycle_trace["stages"].append(stage_trace)
            trace["turns"].append(
                {
                    "cycleIndex": cycle_index,
                    "phase": phase,
                    **result.as_trace(),
                }
            )
            if result.turn is None:
                stage_trace["validation"] = {
                    "valid": False,
                    "errors": [
                        result.validationError
                        or "structured_output_invalid"
                    ],
                }
                continue
            validation = validate_research_turn(result.turn, catalog)
            stage_trace["validation"] = validation.as_trace()
            if not validation.valid:
                cycle_history.append(
                    {
                        "phase": phase,
                        "validationErrors": list(validation.errors),
                    }
                )
                continue
            latest_valid_stage_turn = result.turn
            cycle_history.append(
                {
                    "phase": phase,
                    "decision": {
                        "status": result.turn.status,
                        "reason": result.turn.reason,
                        "missingEvidence": list(
                            result.turn.missingEvidence
                        ),
                        "selectedEvidence": [
                            item.model_dump()
                            for item in result.turn.selectedEvidence
                        ],
                    },
                }
            )
            for action in result.turn.actions:
                if tool_calls >= settings.llm_research_max_tool_calls:
                    stop_reason = "max_tool_calls_reached"
                    break
                remaining = (
                    cycle_deadline
                    - perf_counter()
                    - integration_reserve
                )
                if remaining <= 0.1:
                    cycle_trace["skippedPhases"].append(
                        {
                            "phase": phase,
                            "reason": "tool_skipped_for_integration_reserve",
                        }
                    )
                    break
                execution = gateway.execute(
                    action,
                    catalog,
                    user_clearance_level=request.userClearanceLevel,
                    timeout_sec=remaining,
                )
                execution_trace = execution.as_trace(action)
                execution_trace.update(
                    {
                        "cycleIndex": cycle_index,
                        "phase": phase,
                    }
                )
                trace["toolExecutions"].append(execution_trace)
                cycle_trace["toolExecutions"].append(execution_trace)
                cycle_history.append(execution_trace)
                # Articleの再取得は、カタログ上は新規0件でも「LLMが今回
                # 読み直すよう指定した本文」である。差分IDだけでなく今回
                # fetchが返した全IDを次の段階・統合で優先表示する。
                cycle_new_content_ids.extend(
                    execution.returned_content_unit_ids
                    or execution.new_content_unit_ids
                )
                if action.tool == TOOL_FETCH_ARTICLES:
                    cycle_direct_content_ids.extend(
                        execution.returned_content_unit_ids
                    )
                tool_calls += 1
            if stop_reason == "max_tool_calls_reached":
                break

        if failure_status:
            break

        remaining = cycle_deadline - perf_counter()
        if remaining <= 1:
            failure_status = "timeout"
            stop_reason = f"{mode}_cycle_time_budget_exhausted"
            break
        direct_integration_content_ids = tuple(
            dict.fromkeys(cycle_direct_content_ids)
        )
        integration_content_ids = (
            direct_integration_content_ids
            or recovery_content_ids
        )
        recovery_candidates = _recovery_content_ids(
            catalog,
            direct_content_ids=direct_integration_content_ids,
            cycle_content_ids=tuple(
                dict.fromkeys(cycle_new_content_ids)
            ),
        )
        cycle_trace["stateCompression"] = {
            "acquiredContentUnitCount": len(
                tuple(dict.fromkeys(cycle_new_content_ids))
            ),
            "directFetchedContentUnitIds": list(
                direct_integration_content_ids
            ),
            "integrationContentUnitIds": list(
                integration_content_ids
            ),
            "generalSearchContentUnitCount": len(
                set(cycle_new_content_ids)
                - set(integration_content_ids)
            ),
            "recoveryContentUnitIds": list(recovery_candidates),
            "carriedRecoveryContentUnitIds": list(recovery_content_ids),
        }
        llm_calls += 1
        try:
            integration = llm_client.integrate_legal_research_cycle(
                request,
                catalog,
                checkpoint,
                cycle_index=cycle_index,
                cycle_count=cycle_count,
                # 一般検索はArticle発見で役割を終える。統合LLMには直接取得本文と、
                # tool_historyから抽出する段階選択本文だけを優先して渡す。
                cycle_new_content_ids=integration_content_ids,
                tool_history=cycle_history,
                timeout_sec=max(
                    1,
                    min(
                        settings.llm_research_timeout_sec,
                        int(remaining),
                    ),
                ),
            )
        except requests.Timeout as exc:
            fallback = _checkpoint_from_stage_selection(
                checkpoint,
                latest_valid_stage_turn,
                catalog,
            )
            if fallback is not checkpoint:
                checkpoint = fallback
                last_valid_checkpoint = fallback
                cycle_trace["integrationFallback"] = (
                    "last_valid_stage_selection"
                )
            timeout_trace = {
                "component": "llm_research_integrate",
                "cycleIndex": cycle_index,
                "error": f"{type(exc).__name__}: {exc}",
            }
            cycle_trace["integrationTimeout"] = timeout_trace
            if (
                cycle_index + 1 < cycle_count
                and phase_deadline - perf_counter() > 1
            ):
                # 取得済み本文はCatalogに残っている。中間統合1回のtimeoutで
                # 残りサイクルを捨てず、直前の検証済み状態から再探索する。
                trace.setdefault("recoverableTimeouts", []).append(
                    timeout_trace
                )
                recovery_content_ids = recovery_candidates
                continue
            failure_status = "timeout"
            stop_reason = "llm_timeout"
            trace["timeout"] = timeout_trace
            break
        except requests.ConnectionError as exc:
            fallback = _checkpoint_from_stage_selection(
                checkpoint,
                latest_valid_stage_turn,
                catalog,
            )
            if fallback is not checkpoint:
                checkpoint = fallback
                last_valid_checkpoint = fallback
                cycle_trace["integrationFallback"] = (
                    "last_valid_stage_selection"
                )
            failure_status = "transport_error"
            stop_reason = "llm_connection_error"
            trace["transportError"] = {
                "component": "llm_research_integrate",
                "cycleIndex": cycle_index,
                "error": f"{type(exc).__name__}: {exc}",
            }
            break
        except Exception as exc:  # noqa: BLE001
            fallback = _checkpoint_from_stage_selection(
                checkpoint,
                latest_valid_stage_turn,
                catalog,
            )
            if fallback is not checkpoint:
                checkpoint = fallback
                last_valid_checkpoint = fallback
                cycle_trace["integrationFallback"] = (
                    "last_valid_stage_selection"
                )
            if _is_provider_quota_error(exc):
                failure_status = "provider_quota_error"
                stop_reason = "llm_provider_quota_error"
                trace["providerError"] = {
                    "component": "llm_research_integrate",
                    "cycleIndex": cycle_index,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            else:
                failure_status = "internal_error"
                stop_reason = "llm_call_error"
                trace["error"] = f"{type(exc).__name__}: {exc}"
            break

        integration_trace = {
            "phase": "integrate",
            **integration.as_trace(),
        }
        cycle_trace["stateCompression"]["promptContentUnitIds"] = list(
            integration.promptContentUnitIds
        )
        cycle_trace["stages"].append(integration_trace)
        if integration.checkpoint is None:
            integration_trace["validation"] = {
                "valid": False,
                "errors": [
                    integration.validationError
                    or "structured_output_invalid"
                ],
            }
            fallback = _checkpoint_from_stage_selection(
                checkpoint,
                latest_valid_stage_turn,
                catalog,
            )
            if fallback is not checkpoint:
                checkpoint = fallback
                last_valid_checkpoint = fallback
                cycle_trace["integrationFallback"] = (
                    "last_valid_stage_selection"
                )
            recovery_content_ids = recovery_candidates
            continue
        checkpoint_validation = validate_research_checkpoint(
            integration.checkpoint,
            catalog,
        )
        integration_trace["validation"] = (
            checkpoint_validation.as_trace()
        )
        if not checkpoint_validation.valid:
            sanitized_checkpoint, sanitization = (
                sanitize_research_checkpoint(
                    integration.checkpoint,
                    catalog,
                )
            )
            sanitized_validation = validate_research_checkpoint(
                sanitized_checkpoint,
                catalog,
            )
            if sanitization:
                integration_trace["sanitization"] = {
                    **sanitization,
                    "validation": sanitized_validation.as_trace(),
                }
            if sanitization and sanitized_validation.valid:
                checkpoint = sanitized_checkpoint
                last_valid_checkpoint = checkpoint
                recovery_content_ids = (
                    ()
                    if _checkpoint_has_research_state(checkpoint)
                    else recovery_candidates
                )
                _record_checkpoint_classification(
                    cycle_trace,
                    checkpoint,
                    integration_content_ids,
                )
                cycle_trace["integrationFallback"] = (
                    "sanitized_integration_checkpoint"
                )
                cycle_trace["checkpointStatus"] = checkpoint.status
                cycle_trace["checkpointConclusion"] = (
                    checkpoint.conclusion
                )
                continue
            fallback = _checkpoint_from_stage_selection(
                checkpoint,
                latest_valid_stage_turn,
                catalog,
            )
            if fallback is not checkpoint:
                checkpoint = fallback
                last_valid_checkpoint = fallback
                cycle_trace["integrationFallback"] = (
                    "last_valid_stage_selection"
                )
            recovery_content_ids = recovery_candidates
            continue
        checkpoint = integration.checkpoint
        last_valid_checkpoint = checkpoint
        recovery_content_ids = (
            ()
            if _checkpoint_has_research_state(checkpoint)
            else recovery_candidates
        )
        _record_checkpoint_classification(
            cycle_trace,
            checkpoint,
            integration_content_ids,
        )
        cycle_trace["checkpointStatus"] = checkpoint.status
        cycle_trace["checkpointConclusion"] = checkpoint.conclusion

    trace["cycleCount"] = len(trace["cycles"])
    trace["llmCallCount"] = llm_calls
    trace["toolCallCount"] = tool_calls
    final_checkpoint = last_valid_checkpoint or checkpoint
    selected_ids = tuple(
        content_unit_id
        for content_unit_id in dict.fromkeys(
            final_checkpoint.evidenceIds
        )
        if content_unit_id in set(catalog.content_unit_ids)
    )
    status = final_checkpoint.status
    if failure_status:
        status = "partial" if selected_ids else failure_status
    elif len(trace["cycles"]) < cycle_count:
        status = "partial" if selected_ids else "incomplete"
    if not failure_status and len(trace["cycles"]) == cycle_count:
        stop_reason = (
            "iterative_ready"
            if final_checkpoint.status == RESEARCH_STATUS_READY
            else (
                "iterative_insufficient"
                if final_checkpoint.status == RESEARCH_STATUS_INSUFFICIENT
                else "iterative_cycles_complete"
            )
        )
    trace["checkpoint"] = final_checkpoint.model_dump()
    return _finish(
        trace,
        started,
        catalog,
        status=status,
        stop_reason=stop_reason,
        incomplete=status != RESEARCH_STATUS_READY,
        selected_ids=selected_ids,
    )


def _recovery_content_ids(
    catalog: EvidenceCatalog,
    *,
    direct_content_ids: tuple[str, ...],
    cycle_content_ids: tuple[str, ...],
) -> tuple[str, ...]:
    """統合障害時だけ次サイクルへ渡す、1回限りの小さな回復候補。"""
    if direct_content_ids:
        return catalog.diversify_content_ids(direct_content_ids)[:20]
    return catalog.diversify_content_ids_by_document(
        cycle_content_ids
    )[:18]


def _record_checkpoint_classification(
    cycle_trace: dict[str, Any],
    checkpoint: ResearchCheckpoint,
    integration_content_ids: tuple[str, ...],
) -> None:
    compression = cycle_trace.setdefault("stateCompression", {})
    evidence_ids = tuple(dict.fromkeys(checkpoint.evidenceIds))
    open_ids = tuple(dict.fromkeys(checkpoint.openEvidenceIds))
    classified = set(evidence_ids) | set(open_ids)
    prompt_ids = set(compression.get("promptContentUnitIds") or [])
    compression["checkpointEvidenceIds"] = list(evidence_ids)
    compression["checkpointOpenEvidenceIds"] = list(open_ids)
    compression["discardedDirectContentUnitIds"] = [
        content_unit_id
        for content_unit_id in integration_content_ids
        if (
            content_unit_id in prompt_ids
            and content_unit_id not in classified
        )
    ]
    compression["unpresentedDirectContentUnitIds"] = [
        content_unit_id
        for content_unit_id in integration_content_ids
        if content_unit_id not in prompt_ids
    ]


def _checkpoint_has_research_state(
    checkpoint: ResearchCheckpoint,
) -> bool:
    """次サイクルが再開に使える、確認済みIDまたは根拠ノードがあるか。"""
    if (
        checkpoint.evidenceIds
        or checkpoint.openEvidenceIds
        or checkpoint.nextArticleIds
    ):
        return True
    return any(
        node.articleId or node.evidenceIds
        for issue in checkpoint.logicalStructure.issues
        for node in issue.authorityNodes
    )


def _checkpoint_from_stage_selection(
    checkpoint: ResearchCheckpoint,
    turn: Any | None,
    catalog: EvidenceCatalog,
) -> ResearchCheckpoint:
    """統合障害時も、LLMが直前に明示選択した原文だけは失わない。"""
    if turn is None:
        return checkpoint
    visible = set(catalog.content_unit_ids)
    selected_ids = list(
        catalog.diversify_content_ids(
            [
                item.contentUnitId
                for item in turn.selectedEvidence
                if item.contentUnitId in visible
            ]
        )
    )[:10]
    if not selected_ids:
        return checkpoint
    missing = list(turn.missingEvidence)
    return checkpoint.model_copy(
        update={
            # 段階判断は法的構造を統合していないため、readyへ昇格させない。
            "status": RESEARCH_STATUS_CONTINUE,
            "evidenceIds": selected_ids,
            "nextQuestions": (
                missing[:3] if missing else checkpoint.nextQuestions
            ),
            "nextArticleIds": checkpoint.nextArticleIds,
        }
    )


def _is_provider_quota_error(exc: Exception) -> bool:
    """LLM事業者のクレジット・利用枠不足を検索失敗と区別する。"""
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "credit balance is too low",
            "insufficient credit",
            "insufficient_credit",
            "purchase credits",
            "plans & billing",
        )
    )


def _run_legacy_research(
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
                    execution.returned_content_unit_ids
                    or execution.new_content_unit_ids
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
