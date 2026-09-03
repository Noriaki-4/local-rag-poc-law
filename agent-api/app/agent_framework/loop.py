"""意味判断をModelへ委ね、上限と参照整合だけを扱うAgentLoop。"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from time import monotonic

from pydantic import ValidationError

from .context import (
    GRAPH_REVIEW_FETCH_REQUEST_PREFIX,
    ContextCapacityExceeded,
    GraphReviewBatch,
    HypothesisExplorationSetStatus,
    SearchCandidateArticle,
    SolverActionFeedback,
    SolverContext,
    SolverContractFeedback,
    build_solver_context,
    pending_candidate_review_work_item_ids,
)
from .contracts import CaseUpdate, SolverDecision
from .diagnostics import AgentDiagnostics
from .observability import (
    AgentRunResult,
    ModelCallTrace,
    RunTrace,
    ToolCallTrace,
)
from .ports.model import (
    ModelPort,
    ModelProtocolError,
    ReviewerView,
    SolverCheckpointTimeout,
)
from .ports.tool import ToolDefinition, ToolExecution, ToolRegistry
from .profiles import AgentProfile, ModelCallProfile, ReviewerProfile
from .state import (
    active_hypotheses,
    CaseState,
    DeferredFrontierResolution,
    Evidence,
    GraphCandidateReview,
    ReviewFinding,
    ReviewResult,
    RunStatus,
    SearchCandidateReview,
    ToolRequest,
    ToolResult,
    fetched_article_ids_by_work_item,
    utc_now,
)
from .store import CaseStore
from .validation import (
    ActionRejected,
    ContractViolation,
    apply_hypothesis_revision,
    apply_solver_decision,
)

LOAD_EVIDENCE_TOOL = "load_evidence"
LOAD_EVIDENCE_DEFINITION = ToolDefinition(
    name=LOAD_EVIDENCE_TOOL,
    description=(
        "Caseでは取得済みだが現在のPromptから省略されたEvidence本文を再表示する。"
        "omitted_evidence_idsにある既知IDだけを指定する。"
        "新しい検索、Article本文取得、Graph探索は行わない。"
    ),
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "evidence_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "今回再表示するomitted_evidence_idsの完全一致。",
            }
        },
        "required": ["evidence_ids"],
    },
    result_description="指定した既知Evidenceの本文をmaterial_evidenceへ再投影する。",
    read_only=True,
    parallel_safe=True,
)
MAX_SOLVER_DECISION_ATTEMPTS = 3
logger = logging.getLogger(__name__)


def _follow_up_exploration_statuses(
    context: SolverContext,
) -> tuple[HypothesisExplorationSetStatus, ...]:
    """回答に未確認事項が残るactive Hypothesisの探索状態を返す。"""

    open_work_item_ids = {
        item.work_item_id for item in context.work_tree if item.state == "open"
    }
    follow_up_hypothesis_ids = tuple(
        item.hypothesis_id
        for item in context.hypotheses
        if item.work_item_id in open_work_item_ids and item.requires_follow_up
    )
    if not follow_up_hypothesis_ids:
        return ()
    status_by_hypothesis = {
        item.hypothesis_id: item for item in context.hypothesis_exploration_sets
    }
    if any(
        hypothesis_id not in status_by_hypothesis
        for hypothesis_id in follow_up_hypothesis_ids
    ):
        return ()
    return tuple(
        status_by_hypothesis[hypothesis_id]
        for hypothesis_id in follow_up_hypothesis_ids
    )


def _all_open_hypothesis_exploration_exhausted(
    context: SolverContext,
) -> bool:
    """新規セットも現在セットの未使用Toolも残っていないか返す。"""

    statuses = _follow_up_exploration_statuses(context)
    return bool(statuses) and all(
        item.remaining_new_sets_total == 0
        and item.legal_search_used_in_cycle == item.graph_used_in_cycle
        for item in statuses
    )


def _all_open_hypothesis_new_exploration_sets_exhausted(
    context: SolverContext,
) -> bool:
    """回答に未確認事項が残るactive Hypothesisの新規セットが尽きたか返す。"""

    statuses = _follow_up_exploration_statuses(context)
    return bool(statuses) and all(
        item.remaining_new_sets_total == 0 for item in statuses
    )


def _current_cycle_exploration_set_complete(
    context: SolverContext,
) -> bool:
    """未確認Hypothesisが現在Cycleの探索セットを使い終えたか返す。"""

    statuses = _follow_up_exploration_statuses(context)
    return bool(statuses) and all(
        item.legal_search_used_in_cycle and item.graph_used_in_cycle
        for item in statuses
    )


def _project_completed_exploration_set_to_cycle_close(
    context: SolverContext,
    *,
    finalize_only: bool,
    pending_grounding_observation: bool,
    pending_known_candidate_processing: bool,
    revision_ready: bool,
) -> SolverContext:
    """処理待ちを残さず使い終えた探索セットをCycle境界へ投影する。"""

    if (
        finalize_only
        or pending_grounding_observation
        or pending_known_candidate_processing
        or revision_ready
        or not _current_cycle_exploration_set_complete(context)
    ):
        return context
    return context.model_copy(
        update={
            "cycle_close_required": True,
            "required_dependency_kind": None,
            "required_dependency_work_item_ids": (),
        }
    )


def _has_pending_known_candidate_processing(context: SolverContext) -> bool:
    """現在stepで評価できる検索・Graph候補があるか返す。"""

    return bool(
        pending_candidate_review_work_item_ids(context)
        or context.graph_review_batch.candidates
    )


def _observation_decisions_by_work_item(
    decision: SolverDecision,
    context: SolverContext,
) -> dict[str, SolverDecision]:
    """WorkItem別に生成されたObservation差分を再び独立Decisionへ分ける。"""

    if (
        decision.next != "continue"
        or decision.start_next_cycle
        or decision.answer is not None
        or decision.update.set_non_work_item_requirements is not None
        or decision.update.add_work_items
        or decision.update.add_hypotheses
        or decision.update.impact_decisions
        or decision.review_finding_resolutions
        or decision.graph_candidate_review is not None
        or decision.search_candidate_review is not None
        or decision.frontier_re_adoptions
        or decision.deferred_frontier_resolutions
        or decision.unreviewed_graph_resolution is not None
    ):
        return {}

    hypothesis_work_item_ids = {
        item.hypothesis_id: item.work_item_id for item in context.hypotheses
    }
    work_item_ids = set(decision.next_focus_work_item_ids)
    work_item_ids.update(
        item.work_item_id for item in decision.update.update_work_items
    )
    work_item_ids.update(
        hypothesis_work_item_ids[item.hypothesis_id]
        for item in decision.update.update_hypotheses
        if item.hypothesis_id in hypothesis_work_item_ids
    )
    work_item_ids.update(
        item.work_item_id for item in decision.dependency_decisions
    )
    work_item_ids.update(item.work_item_id for item in decision.tool_requests)
    if not work_item_ids:
        return {}

    ordered_ids = tuple(
        item.work_item_id
        for item in context.work_tree
        if item.work_item_id in work_item_ids
    )
    if set(ordered_ids) != work_item_ids:
        return {}

    result: dict[str, SolverDecision] = {}
    for work_item_id in ordered_ids:
        work_item_updates = tuple(
            item
            for item in decision.update.update_work_items
            if item.work_item_id == work_item_id
        )
        hypothesis_updates = tuple(
            item
            for item in decision.update.update_hypotheses
            if hypothesis_work_item_ids.get(item.hypothesis_id) == work_item_id
        )
        dependencies = tuple(
            item
            for item in decision.dependency_decisions
            if item.work_item_id == work_item_id
        )
        tool_requests = tuple(
            item
            for item in decision.tool_requests
            if item.work_item_id == work_item_id
        )
        focus_ids = tuple(
            item
            for item in decision.next_focus_work_item_ids
            if item == work_item_id
        )
        result[work_item_id] = SolverDecision(
            next="continue",
            decision_reason=decision.decision_reason,
            update=CaseUpdate(
                update_work_items=work_item_updates,
                update_hypotheses=hypothesis_updates,
            ),
            next_focus_work_item_ids=focus_ids,
            dependency_decisions=dependencies,
            tool_requests=tool_requests,
        )
    return result


def _merge_observation_work_item_decisions(
    decisions: tuple[SolverDecision, ...],
) -> SolverDecision:
    """検証済みの独立したWorkItem差分を、意味を変えず連結する。"""

    if len(decisions) == 1:
        return decisions[0]
    return SolverDecision(
        next="continue",
        decision_reason=(
            f"{len(decisions)}件のWorkItemについて、個別に検証した差分を統合した。"
        ),
        update=CaseUpdate(
            update_work_items=tuple(
                item
                for decision in decisions
                for item in decision.update.update_work_items
            ),
            update_hypotheses=tuple(
                item
                for decision in decisions
                for item in decision.update.update_hypotheses
            ),
        ),
        next_focus_work_item_ids=tuple(
            dict.fromkeys(
                item
                for decision in decisions
                for item in decision.next_focus_work_item_ids
            )
        ),
        dependency_decisions=tuple(
            item
            for decision in decisions
            for item in decision.dependency_decisions
        ),
        tool_requests=tuple(
            item for decision in decisions for item in decision.tool_requests
        ),
    )


def _without_rejected_tool_requests(
    decision: SolverDecision,
    rejected_requests: tuple[ToolRequest, ...],
) -> SolverDecision:
    """Programが棄却した行動だけをObservation差分から取り除く。"""

    rejected_request_ids = {
        request.request_id for request in rejected_requests
    }
    if not rejected_request_ids:
        return decision
    return decision.model_copy(
        update={
            "tool_requests": tuple(
                request
                for request in decision.tool_requests
                if request.request_id not in rejected_request_ids
            ),
            "dependency_decisions": tuple(
                item.model_copy(update={"action_request_id": None})
                if item.action_request_id in rejected_request_ids
                else item
                for item in decision.dependency_decisions
            ),
        }
    )


class AgentLoop:
    def __init__(
        self,
        *,
        store: CaseStore,
        model: ModelPort,
        tools: ToolRegistry,
        profile: AgentProfile,
        diagnostics: AgentDiagnostics | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._store = store
        self._model = model
        self._tools = tools
        self._profile = profile
        self._diagnostics = diagnostics
        self._clock = clock

    def _apply_observation_work_item_decision(
        self,
        state: CaseState,
        decision: SolverDecision,
        context: SolverContext,
    ) -> CaseState:
        """Observation差分を単一WorkItem用の契約で検証して適用する。"""

        return apply_solver_decision(
            state,
            decision,
            limits=self._profile.limits,
            known_tool_names=self._read_only_tool_names,
            material_evidence_ids=context.material_evidence_ids,
            fetchable_article_ids=context.fetchable_article_ids,
            required_dependency_kind=context.required_dependency_kind,
            required_dependency_work_item_ids=(
                context.required_dependency_work_item_ids
            ),
            require_dependency_decisions=False,
            tool_list_argument_limits={
                (item.tool_name, item.argument_name): item.max_items
                for item in self._profile.tool_list_argument_limits
            },
            search_candidate_article_ids=tuple(
                item.article_id for item in context.search_candidates
            ),
            graph_candidate_article_ids=tuple(
                item.article_id
                for item in context.graph_review_batch.candidates
                if item.content_status in {"not_requested", "failed", "timeout"}
            ),
            graph_known_article_ids=tuple(
                dict.fromkeys(
                    [
                        *(item.article_id for item in context.graph_review_batch.candidates),
                        *(item.article_id for item in context.graph_review_ledger),
                    ]
                )
            ),
            graph_review_fetch_tool_name=(
                self._profile.graph_review_fetch_tool_name
            ),
            remaining_fetch_capacity=context.remaining_fetch_capacity,
            cycle_close_required=False,
            can_start_next_cycle=context.can_start_next_cycle,
            finalize_only=False,
            allow_dependency_action_without_tool=True,
            allow_parallel_work_item_actions=True,
        )

    def _validate_observation_work_item_decision(
        self,
        state: CaseState,
        decision: SolverDecision,
        context: SolverContext,
    ) -> None:
        """Observation差分を保存せず、単一WorkItemの契約として検証する。"""

        self._apply_observation_work_item_decision(state, decision, context)

    def _mark_integrated_tool_results(
        self,
        candidate: CaseState,
        context: SolverContext,
        *,
        purpose: str,
    ) -> CaseState:
        """今回処理したToolResultを再統合対象から外す。"""

        consumed_result_request_ids: tuple[str, ...] = ()
        if purpose in {"observation_integration", "cycle_close"}:
            consumed_result_request_ids = tuple(
                item.request_id for item in context.recent_tool_results
            )
        elif purpose == "graph_selection":
            consumed_result_request_ids = (
                context.required_graph_review_request_ids
            )
        elif purpose == "search_selection":
            consumed_result_request_ids = (
                context.required_search_review_request_ids
            )
        if not consumed_result_request_ids:
            return candidate
        return self._replace_state(
            candidate,
            integrated_tool_result_request_ids=tuple(
                dict.fromkeys(
                    [
                        *candidate.integrated_tool_result_request_ids,
                        *consumed_result_request_ids,
                    ]
                )
            ),
        )

    def run(self, case_id: str) -> AgentRunResult:
        state = self._store.load(case_id)
        if state.run_status != "running":
            raise ValueError("agent loop requires a running case")

        started_at = self._clock()
        model_traces: list[ModelCallTrace] = []
        tool_traces: list[ToolCallTrace] = []
        reviewer_findings: tuple[ReviewFinding, ...] = ()
        revisions = 0
        failure_code: str | None = None

        try:
            while True:
                state = self._run_solver_until_answer(
                    state,
                    started_at=started_at,
                    reviewer_findings=reviewer_findings,
                    model_traces=model_traces,
                    tool_traces=tool_traces,
                )
                if state.run_status != "running":
                    break
                if not self._profile.reviewer.enabled:
                    state = self._finish(state, "completed")
                    break

                reviewer_view = self._build_reviewer_view(state)
                remaining = self._remaining_wall_time(started_at)
                if remaining <= 0:
                    state = self._finish(
                        state,
                        "failed",
                        stop_reason="max_wall_time",
                    )
                    break
                review_profile = ReviewerProfile.model_validate(
                    {
                        **self._profile.reviewer.model_dump(),
                        "timeout_sec": min(
                            self._profile.reviewer.timeout_sec,
                            remaining,
                        ),
                    }
                )
                review_started = self._clock()
                review_call = self._model.review(
                    reviewer_view,
                    review_profile,
                )
                try:
                    self._validate_review_result(reviewer_view, review_call.review)
                except ContractViolation as exc:
                    if self._diagnostics is not None:
                        self._diagnostics.record_reviewer_contract_violation(
                            view=reviewer_view,
                            review=review_call.review,
                            violation=str(exc),
                        )
                    raise
                model_traces.append(
                    ModelCallTrace(
                        purpose="review",
                        model=review_profile.model,
                        latency_ms=self._elapsed_ms(review_started),
                        input_tokens=review_call.input_tokens,
                        output_tokens=review_call.output_tokens,
                        attempt_count=review_call.attempt_count,
                    )
                )
                state_before_review = state
                state = self._replace_state(state, review=review_call.review)
                self._store.save(state)
                if self._diagnostics is not None:
                    self._diagnostics.record_reviewer_result_applied(
                        state_before=state_before_review,
                        state_after=state,
                        view=reviewer_view,
                        review=review_call.review,
                    )
                if review_call.review.verdict == "accept":
                    state = self._finish(state, "completed")
                    break
                if revisions >= self._profile.reviewer.max_revisions:
                    state = self._finish(
                        state,
                        "failed",
                        stop_reason="review_failed",
                    )
                    break

                revisions += 1
                reviewer_findings = review_call.review.findings
                state = self._replace_state(state, final_answer=None)
                self._store.save(state)
        except ContextCapacityExceeded:
            state = self._store.load(case_id)
            state = self._finish(
                state,
                "failed",
                stop_reason="context_capacity_exceeded",
            )
            failure_code = "context_capacity_exceeded"
        except ContractViolation as exc:
            state = self._store.load(case_id)
            state = self._finish(state, "failed", stop_reason="protocol_error")
            failure_code = f"contract_violation:{exc}"
        except ModelProtocolError as exc:
            state = self._store.load(case_id)
            state = self._finish(state, "failed", stop_reason="protocol_error")
            failure_code = f"model_protocol:{exc}"
        except ValidationError:
            state = self._store.load(case_id)
            state = self._finish(state, "failed", stop_reason="protocol_error")
            failure_code = "schema_validation"
        except TimeoutError:
            state = self._store.load(case_id)
            state = self._finish(state, "failed", stop_reason="model_timeout")
            failure_code = "model_timeout"
        except Exception as exc:
            logger.exception("unexpected AgentLoop failure", exc_info=exc)
            state = self._store.load(case_id)
            state = self._finish(state, "failed", stop_reason="provider_error")
            failure_code = "provider_error"

        run_elapsed_ms = self._elapsed_ms(started_at)
        if self._diagnostics is not None:
            self._diagnostics.record_run_complete(
                state=state,
                failure_code=failure_code,
                elapsed_ms=run_elapsed_ms,
            )
        return AgentRunResult(
            state=state,
            trace=RunTrace(
                reviewer_enabled=self._profile.reviewer.enabled,
                elapsed_ms=run_elapsed_ms,
                model_calls=tuple(model_traces),
                tool_calls=tuple(tool_traces),
                failure_code=failure_code,
            ),
        )

    def _run_solver_until_answer(
        self,
        state: CaseState,
        *,
        started_at: float,
        reviewer_findings: tuple[ReviewFinding, ...],
        model_traces: list[ModelCallTrace],
        tool_traces: list[ToolCallTrace],
    ) -> CaseState:
        while state.final_answer is None:
            remaining = self._remaining_wall_time(started_at)
            if remaining <= 0:
                return self._finish(
                    state,
                    "failed",
                    stop_reason="max_wall_time",
                )

            cycle_limit_reached = (
                state.research_cycle_count >= self._profile.limits.max_research_cycles
            )
            # 現Cycleの探索停止と、次Cycleを開始できるかの判定は別物である。
            # min_next_cycle_budget_secはbuild_solver_contextの
            # can_start_next_cycleだけで使い、現在Cycleを早期終了させない。
            time_reserve_reached = (
                remaining <= self._profile.limits.finalization_reserve_sec
            )
            finalize_only = (
                cycle_limit_reached
                or time_reserve_reached
                or state.cycle_step_timeout
                or state.stop_reason == "exploration_limit_reached"
            )
            stop_reason = None
            if cycle_limit_reached:
                stop_reason = "max_research_cycles"
            elif time_reserve_reached:
                stop_reason = "finalization_reserve"
            if stop_reason is not None and state.stop_reason != stop_reason:
                state = self._replace_state(state, stop_reason=stop_reason)
                self._store.save(state)

            integration_call = bool(state.research_cycle_count or reviewer_findings)
            hypothesis_revision_call = bool(
                state.research_cycle_count
                and state.research_cycle_count
                not in state.hypothesis_revision_cycles
                and self._profile.solver_hypothesis_revision is not None
                and _has_current_cycle_contradicted_hypothesis(state)
                and not reviewer_findings
            )
            pending_grounding_observation = bool(
                integration_call
                and not state.cycle_step_timeout
                and not reviewer_findings
                and _has_unintegrated_grounding_result(
                    state,
                    tool_names=frozenset(
                        item
                        for item in (
                            self._profile.graph_review_fetch_tool_name,
                            LOAD_EVIDENCE_TOOL,
                        )
                        if item is not None
                    ),
                )
            )
            dependency_audit_work_item_ids = _dependency_audit_scope(
                state,
                integration_call=integration_call,
                finalize_only=finalize_only,
                required_dependency_kind=(
                    self._profile.required_dependency_kind
                ),
            )
            dependency_audit_required = bool(dependency_audit_work_item_ids)
            contract_feedback: SolverContractFeedback | None = None
            action_feedback: SolverActionFeedback | None = None
            retained_observation_decisions: dict[str, SolverDecision] = {}
            cycle_step_timed_out = False
            for decision_attempt in range(MAX_SOLVER_DECISION_ATTEMPTS):
                attempt_remaining = self._remaining_wall_time(started_at)
                model_budget = attempt_remaining
                if not finalize_only:
                    model_budget -= self._profile.limits.finalization_reserve_sec
                if model_budget <= 0:
                    if finalize_only:
                        raise TimeoutError("solver contract repair time exhausted")
                    state = self._replace_state(
                        state,
                        cycle_step_timeout=True,
                        stop_reason="cycle_step_timeout",
                        updated_at=utc_now(),
                    )
                    self._store.save(state)
                    cycle_step_timed_out = True
                    break
                context = build_solver_context(
                    state,
                    self._profile.limits,
                    remaining_wall_time_sec=attempt_remaining,
                    finalize_only=(
                        finalize_only and not pending_grounding_observation
                    ),
                    reviewer_findings=reviewer_findings,
                    contract_feedback=contract_feedback,
                    action_feedback=action_feedback,
                    required_dependency_kind=(
                        self._profile.required_dependency_kind
                        if dependency_audit_required
                        else None
                    ),
                    required_dependency_work_item_ids=(
                        dependency_audit_work_item_ids
                        if dependency_audit_required
                        else ()
                    ),
                    available_tools=self._solver_tool_definitions,
                )
                pending_candidate_work_item_ids = (
                    pending_candidate_review_work_item_ids(context)
                )
                revision_ready = bool(
                    hypothesis_revision_call
                    and context.hypothesis_revision_evidence
                    and not pending_grounding_observation
                )
                pending_known_candidate_processing = (
                    _has_pending_known_candidate_processing(context)
                )
                if (
                    not finalize_only
                    and not pending_grounding_observation
                    and not pending_known_candidate_processing
                    and not revision_ready
                    and _all_open_hypothesis_exploration_exhausted(context)
                ):
                    # 未評価候補と未統合本文を先に処理したうえで、active
                    # Hypothesisの探索枠が全て尽きたら空のCycleを増やさない。
                    state = self._replace_state(
                        state,
                        stop_reason="exploration_limit_reached",
                        updated_at=utc_now(),
                    )
                    self._store.save(state)
                    finalize_only = True
                    dependency_audit_required = False
                    model_budget = attempt_remaining
                    context = context.model_copy(
                        update={
                            "can_start_next_cycle": False,
                            "cycle_close_required": True,
                            "finalize_only": True,
                            "required_dependency_kind": None,
                            "required_dependency_work_item_ids": (),
                        }
                    )
                else:
                    # 後続Cycle用の探索セットが残っていても、現在Cycleの
                    # OpenSearch・Graphを使い終えた後の境界処理は専用の
                    # Cycle Closeへ集約する。保留Frontierの引継ぎも同契約で
                    # 一度だけ確定する。
                    context = _project_completed_exploration_set_to_cycle_close(
                        context,
                        finalize_only=finalize_only,
                        pending_grounding_observation=(
                            pending_grounding_observation
                        ),
                        pending_known_candidate_processing=(
                            pending_known_candidate_processing
                        ),
                        revision_ready=revision_ready,
                    )
                if pending_grounding_observation:
                    # A selected Article has already consumed fetch capacity. Integrate
                    # its text before reviewing more discovery candidates. The complete
                    # Graph/Search pools remain in CaseState and are projected again on
                    # the next loop iteration.
                    context = _grounding_observation_context(context)
                elif (
                    (
                        bool(context.work_tree)
                        and not any(
                            item.state == "open" for item in context.work_tree
                        )
                    )
                    or (
                        context.cycle_close_required
                        and not context.can_start_next_cycle
                    )
                ) and not (
                    hypothesis_revision_call
                    and bool(context.hypothesis_revision_evidence)
                    and not pending_grounding_observation
                ):
                    # 全WorkItemが閉じた後は、Cycle Closeや汎用Integrationを
                    # 再実行せず、確認済み根拠だけを見せるFinalizationで
                    # 回答を1回だけ生成する。
                    finalize_only = True
                    dependency_audit_required = False
                    model_budget = attempt_remaining
                    context = context.model_copy(update={"finalize_only": True})
                graph_review_call = bool(
                    context.required_graph_review_request_ids
                    and self._profile.solver_graph_review is not None
                    and not pending_grounding_observation
                    and not finalize_only
                    and not reviewer_findings
                )
                search_review_call = bool(
                    context.required_search_review_request_ids
                    and self._profile.solver_search_review is not None
                    and not graph_review_call
                    and not pending_grounding_observation
                    and not finalize_only
                    and not reviewer_findings
                )
                if graph_review_call and not finalize_only:
                    context = context.model_copy(
                        update={
                            "grounding_evidence_ids": (),
                            "navigation_evidence_ids": (),
                            "evidence_manifest": (),
                            "material_evidence": (),
                            "omitted_evidence_ids": (),
                            "required_dependency_kind": None,
                            "required_dependency_work_item_ids": (),
                            "search_candidates": (),
                            "required_search_review_request_ids": (),
                        }
                    )
                elif search_review_call:
                    navigation_ids = frozenset(
                        evidence_id
                        for item in context.search_candidates
                        for evidence_id in item.navigation_evidence_ids
                    )
                    context = context.model_copy(
                        update={
                            "grounding_evidence_ids": (),
                            "navigation_evidence_ids": tuple(
                                evidence_id
                                for evidence_id in context.navigation_evidence_ids
                                if evidence_id in navigation_ids
                            ),
                            "evidence_manifest": tuple(
                                item
                                for item in context.evidence_manifest
                                if item.evidence_id in navigation_ids
                            ),
                            "material_evidence": tuple(
                                item
                                for item in context.material_evidence
                                if item.evidence_id in navigation_ids
                            ),
                            "omitted_evidence_ids": (),
                            "required_dependency_kind": None,
                            "required_dependency_work_item_ids": (),
                            "graph_review_batch": GraphReviewBatch(),
                            "graph_review_ledger": (),
                            "required_graph_review_request_ids": (),
                        }
                    )
                base_call_profile, purpose = self._solver_profile_for_context(
                    context=context,
                    graph_review_call=graph_review_call,
                    search_review_call=search_review_call,
                    integration_call=integration_call,
                    has_reviewer_findings=bool(reviewer_findings),
                    observation_integration_call=(
                        pending_grounding_observation
                    ),
                    hypothesis_revision_call=(
                        hypothesis_revision_call
                        and (
                            context.cycle_close_required
                            or not any(
                                item.state == "open" for item in context.work_tree
                            )
                        )
                        and bool(context.hypothesis_revision_evidence)
                        and not pending_grounding_observation
                        and not finalize_only
                    ),
                )
                if purpose == "finalization":
                    context = _finalization_decision_context(context)
                if purpose == "cycle_close":
                    model_budget = min(
                        model_budget,
                        self._profile.limits.cycle_close_reserve_sec,
                    )
                if purpose == "cycle_close":
                    # 下位規範状態は逐次統合済みであり、Cycle Closeは遷移だけを判断する。
                    context = context.model_copy(
                        update={
                            "required_dependency_kind": None,
                            "required_dependency_work_item_ids": (),
                        }
                    )
                call_profile = self._bounded_model_profile(
                    base_call_profile,
                    model_budget,
                )
                if self._diagnostics is not None:
                    self._diagnostics.record_solver_input(
                        state=state,
                        context=context,
                        profile=call_profile,
                        purpose=purpose,
                        contract_attempt=decision_attempt,
                    )
                call_started = self._clock()
                try:
                    solver_call = self._model.solve(context, call_profile)
                except SolverCheckpointTimeout as exc:
                    partial_decision = exc.partial_decision
                    call_latency_ms = self._elapsed_ms(call_started)
                    if self._diagnostics is not None and partial_decision is not None:
                        self._diagnostics.record_solver_output(
                            state=state,
                            purpose=f"{purpose}_{exc.completed_stage}_checkpoint",
                            contract_attempt=decision_attempt,
                            decision=partial_decision,
                            latency_ms=call_latency_ms,
                            input_tokens=exc.input_tokens,
                            output_tokens=exc.output_tokens,
                        )
                    checkpoint_state = state
                    if partial_decision is not None:
                        try:
                            checkpoint_state = apply_solver_decision(
                                state,
                                partial_decision,
                                limits=self._profile.limits,
                                known_tool_names=self._read_only_tool_names,
                                material_evidence_ids=context.material_evidence_ids,
                                finalize_only=False,
                            )
                        except ContractViolation as checkpoint_error:
                            logger.warning(
                                "Solver checkpoint was not applied: %s",
                                checkpoint_error,
                            )
                            if self._diagnostics is not None:
                                self._diagnostics.record_contract_violation(
                                    state=state,
                                    purpose=(
                                        f"{purpose}_{exc.completed_stage}_checkpoint"
                                    ),
                                    contract_attempt=decision_attempt,
                                    decision=partial_decision,
                                    violation=str(checkpoint_error),
                                )
                        else:
                            if self._diagnostics is not None:
                                self._diagnostics.record_decision_applied(
                                    state_before=state,
                                    state_after=checkpoint_state,
                                    context=context,
                                    purpose=(
                                        f"{purpose}_{exc.completed_stage}_checkpoint"
                                    ),
                                    contract_attempt=decision_attempt,
                                    decision=partial_decision,
                                )
                    model_traces.append(
                        ModelCallTrace(
                            purpose=(
                                f"{purpose}_{exc.completed_stage}_checkpoint_timeout"
                            ),
                            model=call_profile.model,
                            latency_ms=call_latency_ms,
                            input_tokens=exc.input_tokens,
                            output_tokens=exc.output_tokens,
                            attempt_count=1,
                            finalize_only=False,
                        )
                    )
                    state = self._replace_state(
                        checkpoint_state,
                        cycle_step_timeout=True,
                        stop_reason="cycle_step_timeout",
                        updated_at=utc_now(),
                    )
                    self._store.save(state)
                    cycle_step_timed_out = True
                    break
                except TimeoutError:
                    if finalize_only or state.cycle_step_timeout:
                        raise
                    model_traces.append(
                        ModelCallTrace(
                            purpose=f"{purpose}_cycle_step_timeout",
                            model=call_profile.model,
                            latency_ms=self._elapsed_ms(call_started),
                            attempt_count=1,
                            finalize_only=False,
                        )
                    )
                    state = self._replace_state(
                        state,
                        cycle_step_timeout=True,
                        stop_reason="cycle_step_timeout",
                        updated_at=utc_now(),
                    )
                    self._store.save(state)
                    cycle_step_timed_out = True
                    break
                call_latency_ms = self._elapsed_ms(call_started)
                model_traces.append(
                    ModelCallTrace(
                        purpose=(
                            f"{purpose}_contract_repair"
                            if contract_feedback is not None
                            else (
                                f"{purpose}_action_feedback"
                                if action_feedback is not None
                                else purpose
                            )
                        ),
                        model=call_profile.model,
                        latency_ms=call_latency_ms,
                        input_tokens=solver_call.input_tokens,
                        output_tokens=solver_call.output_tokens,
                        attempt_count=solver_call.attempt_count,
                        finalize_only=context.finalize_only,
                    )
                )
                if self._diagnostics is not None:
                    self._diagnostics.record_solver_output(
                        state=state,
                        purpose=purpose,
                        contract_attempt=decision_attempt,
                        decision=solver_call.decision,
                        latency_ms=call_latency_ms,
                        input_tokens=solver_call.input_tokens,
                        output_tokens=solver_call.output_tokens,
                    )
                try:
                    applied_decision = solver_call.decision
                    if purpose == "observation_integration":
                        applied_decision = (
                            _defer_actions_until_candidate_review(
                                applied_decision,
                                work_item_ids=pending_candidate_work_item_ids,
                            )
                        )
                        if retained_observation_decisions:
                            repaired_by_work_item = (
                                _observation_decisions_by_work_item(
                                    applied_decision,
                                    context,
                                )
                            )
                            if repaired_by_work_item:
                                combined_by_work_item = {
                                    **repaired_by_work_item,
                                    **retained_observation_decisions,
                                }
                                applied_decision = (
                                    _merge_observation_work_item_decisions(
                                        tuple(
                                            combined_by_work_item[item.work_item_id]
                                            for item in context.work_tree
                                            if item.work_item_id
                                            in combined_by_work_item
                                        )
                                    )
                                )
                    if purpose == "hypothesis_revision":
                        revision = solver_call.hypothesis_revision
                        if revision is None:
                            raise ContractViolation(
                                "hypothesis revision result is missing"
                            )
                        candidate = apply_hypothesis_revision(
                            state,
                            revision,
                            material_evidence_ids=(
                                context.material_evidence_ids
                                | frozenset(
                                    item.evidence_id
                                    for item in context.hypothesis_revision_evidence
                                )
                            ),
                            eligible_work_item_ids={
                                item.work_item_id
                                for item in context.work_tree
                                if item.state != "dropped"
                            },
                            eligible_hypothesis_ids={
                                item.hypothesis_id
                                for item in context.hypotheses
                                if item.judgment == "contradicted"
                                and any(
                                    evidence.evidence_id in item.evidence_ids
                                    for evidence in context.hypothesis_revision_evidence
                                )
                            },
                        )
                    else:
                        candidate = apply_solver_decision(
                            state,
                            applied_decision,
                            limits=self._profile.limits,
                            known_tool_names=self._read_only_tool_names,
                            material_evidence_ids=context.material_evidence_ids,
                            fetchable_article_ids=context.fetchable_article_ids,
                            required_dependency_kind=context.required_dependency_kind,
                            required_dependency_work_item_ids=(
                                context.required_dependency_work_item_ids
                            ),
                            hypothesis_revision_work_item_ids=None,
                            require_dependency_decisions=bool(
                                context.required_dependency_work_item_ids
                            )
                            and purpose != "observation_integration",
                            required_review_finding_ids=tuple(
                                item.finding_id for item in context.reviewer_findings
                            ),
                            tool_list_argument_limits={
                                (item.tool_name, item.argument_name): item.max_items
                                for item in self._profile.tool_list_argument_limits
                            },
                            required_graph_review_request_ids=(
                                context.required_graph_review_request_ids
                                if purpose == "graph_selection"
                                else ()
                            ),
                            required_search_review_request_ids=(
                                context.required_search_review_request_ids
                                if purpose == "search_selection"
                                else ()
                            ),
                            search_candidate_article_ids=tuple(
                                item.article_id for item in context.search_candidates
                            ),
                            graph_candidate_article_ids=tuple(
                                item.article_id
                                for item in context.graph_review_batch.candidates
                                if item.content_status
                                in {"not_requested", "failed", "timeout"}
                            ),
                            graph_known_article_ids=tuple(
                                dict.fromkeys(
                                    [
                                        *(
                                            item.article_id
                                            for item in context.graph_review_batch.candidates
                                        ),
                                        *(
                                            item.article_id
                                            for item in context.graph_review_ledger
                                        ),
                                    ]
                                )
                            ),
                            graph_review_fetch_tool_name=(
                                self._profile.graph_review_fetch_tool_name
                            ),
                            graph_review_frontiers={
                                item.frontier_item_id: (
                                    item.article_id,
                                    item.work_item_id,
                                    item.hypothesis_id,
                                )
                                for item in context.graph_review_batch.candidates
                            },
                            graph_review_link_ids=tuple(
                                dict.fromkeys(
                                    link.link_id
                                    for item in context.graph_review_batch.candidates
                                    for link in item.links
                                )
                            ),
                            graph_selectable_frontiers={
                                item.frontier_item_id: (
                                    item.article_id,
                                    item.work_item_id,
                                    item.hypothesis_id,
                                )
                                for item in context.graph_review_batch.candidates
                                if item.content_status
                                in {"not_requested", "failed", "timeout"}
                            },
                            deferred_frontiers={
                                item.frontier_item_id: (
                                    item.article_id,
                                    item.work_item_id,
                                    item.hypothesis_id,
                                )
                                for item in context.graph_review_ledger
                                if item.review_status == "relevant_deferred"
                                and item.content_status
                                in {"not_requested", "failed", "timeout"}
                                and item.deferred_resolution_action
                                != "no_longer_needed"
                            },
                            unreviewed_graph_candidate_count=(
                                context.graph_review_batch.remaining_unreviewed_count
                            ),
                            remaining_fetch_capacity=context.remaining_fetch_capacity,
                            cycle_close_required=(
                                context.cycle_close_required
                                and purpose != "observation_integration"
                            ),
                            can_start_next_cycle=context.can_start_next_cycle,
                            finalize_only=context.finalize_only,
                            allow_dependency_action_without_tool=(
                                purpose == "observation_integration"
                                or bool(context.required_dependency_work_item_ids)
                            ),
                            allow_parallel_work_item_actions=(
                                purpose
                                in {"observation_integration", "search_planning"}
                            ),
                        )
                    candidate = self._mark_integrated_tool_results(
                        candidate,
                        context,
                        purpose=purpose,
                    )
                    if purpose == "hypothesis_revision":
                        candidate = self._replace_state(
                            candidate,
                            hypothesis_revision_cycles=tuple(
                                dict.fromkeys(
                                    (
                                        *candidate.hypothesis_revision_cycles,
                                        max(1, candidate.research_cycle_count),
                                    )
                                )
                            ),
                        )
                    if self._diagnostics is not None:
                        self._diagnostics.record_decision_applied(
                            state_before=state,
                            state_after=candidate,
                            context=context,
                            purpose=purpose,
                            contract_attempt=decision_attempt,
                            decision=applied_decision,
                        )
                    break
                except ActionRejected as exc:
                    logger.info("Solver action was not executed: %s", exc)
                    rejected_feedback = SolverActionFeedback(
                        code="already_completed",
                        message=str(exc),
                        rejected_tool_requests=exc.rejected_requests,
                    )
                    if self._diagnostics is not None:
                        self._diagnostics.record_action_rejected(
                            state=state,
                            purpose=purpose,
                            decision_attempt=decision_attempt,
                            decision=solver_call.decision,
                            feedback=rejected_feedback,
                        )
                    if purpose == "observation_integration":
                        semantic_decision = _without_rejected_tool_requests(
                            applied_decision,
                            exc.rejected_requests,
                        )
                        semantic_applied = False
                        while True:
                            try:
                                candidate = (
                                    self._apply_observation_work_item_decision(
                                        state,
                                        semantic_decision,
                                        context,
                                    )
                                )
                            except ActionRejected as nested_rejection:
                                reduced_decision = _without_rejected_tool_requests(
                                    semantic_decision,
                                    nested_rejection.rejected_requests,
                                )
                                if reduced_decision == semantic_decision:
                                    raise
                                semantic_decision = reduced_decision
                                if self._diagnostics is not None:
                                    self._diagnostics.record_action_rejected(
                                        state=state,
                                        purpose=purpose,
                                        decision_attempt=decision_attempt,
                                        decision=semantic_decision,
                                        feedback=SolverActionFeedback(
                                            code="already_completed",
                                            message=str(nested_rejection),
                                            rejected_tool_requests=(
                                                nested_rejection.rejected_requests
                                            ),
                                        ),
                                    )
                                continue
                            except ContractViolation:
                                # 意味差分自体が不正なら従来どおりLLM修復へ戻す。
                                break
                            candidate = self._mark_integrated_tool_results(
                                candidate,
                                context,
                                purpose=purpose,
                            )
                            applied_decision = semantic_decision
                            semantic_applied = True
                            if self._diagnostics is not None:
                                self._diagnostics.record_decision_applied(
                                    state_before=state,
                                    state_after=candidate,
                                    context=context,
                                    purpose=(
                                        "observation_integration_action_checkpoint"
                                    ),
                                    contract_attempt=decision_attempt,
                                    decision=applied_decision,
                                )
                            break
                        if semantic_applied:
                            break
                    if decision_attempt == MAX_SOLVER_DECISION_ATTEMPTS - 1:
                        raise
                    contract_feedback = None
                    action_feedback = rejected_feedback
                except ContractViolation as exc:
                    logger.warning("Solver contract violation: %s", exc)
                    if self._diagnostics is not None:
                        self._diagnostics.record_contract_violation(
                            state=state,
                            purpose=purpose,
                            contract_attempt=decision_attempt,
                            decision=solver_call.decision,
                            violation=str(exc),
                        )
                    if decision_attempt == MAX_SOLVER_DECISION_ATTEMPTS - 1:
                        raise
                    action_feedback = None
                    if purpose == "observation_integration":
                        decisions_by_work_item = (
                            _observation_decisions_by_work_item(
                                applied_decision,
                                context,
                            )
                        )
                        valid_decisions: dict[str, SolverDecision] = {}
                        invalid_decisions: dict[str, SolverDecision] = {}
                        invalid_violations: dict[str, str] = {}
                        for work_item_id, item_decision in (
                            decisions_by_work_item.items()
                        ):
                            try:
                                self._validate_observation_work_item_decision(
                                    state,
                                    item_decision,
                                    context,
                                )
                            except ContractViolation as item_error:
                                invalid_decisions[work_item_id] = item_decision
                                invalid_violations[work_item_id] = str(item_error)
                            else:
                                valid_decisions[work_item_id] = item_decision
                        if valid_decisions and invalid_decisions:
                            retained_observation_decisions = valid_decisions
                            contract_feedback = SolverContractFeedback(
                                violation="; ".join(
                                    f"{work_item_id}: {invalid_violations[work_item_id]}"
                                    for work_item_id in invalid_decisions
                                ),
                                previous_decision=(
                                    _merge_observation_work_item_decisions(
                                        tuple(invalid_decisions.values())
                                    )
                                ),
                                repair_work_item_ids=tuple(invalid_decisions),
                            )
                            continue
                    contract_feedback = SolverContractFeedback(
                        violation=_merge_contract_violations(
                            contract_feedback.violation
                            if contract_feedback is not None
                            else None,
                            str(exc),
                        ),
                        previous_decision=solver_call.decision,
                    )
            if cycle_step_timed_out:
                continue
            if applied_decision.next == "finalize":
                candidate = self._replace_state(
                    candidate,
                    cycle_step_timeout=False,
                    stop_reason=None,
                    updated_at=utc_now(),
                )
                self._store.save(candidate)
                return candidate

            decision_requests = applied_decision.tool_requests
            if (
                graph_review_call
                and applied_decision.graph_candidate_review is not None
                and applied_decision.graph_candidate_review.selected_article_ids
            ):
                graph_fetch_request = self._graph_review_fetch_request(
                    candidate,
                    applied_decision.graph_candidate_review,
                    fetchable_article_ids={
                        item.article_id
                        for item in context.graph_review_batch.candidates
                        if item.content_status
                        in {"not_requested", "failed", "timeout"}
                    },
                )
                graph_load_request = self._graph_review_load_request(
                    candidate,
                    applied_decision.graph_candidate_review,
                    fetchable_article_ids={
                        item.article_id
                        for item in context.graph_review_batch.candidates
                        if item.content_status
                        in {"not_requested", "failed", "timeout"}
                    },
                )
                decision_requests = tuple(
                    item
                    for item in (graph_fetch_request, graph_load_request)
                    if item is not None
                )
                candidate = self._replace_state(
                    candidate,
                    tool_requests=(*candidate.tool_requests, *decision_requests),
                )
            elif (
                search_review_call
                and applied_decision.search_candidate_review is not None
                and applied_decision.search_candidate_review.selected_article_ids
            ):
                decision_requests = self._search_review_fetch_requests(
                    candidate,
                    applied_decision.search_candidate_review,
                    context.search_candidates,
                )
                candidate = self._replace_state(
                    candidate,
                    tool_requests=(*candidate.tool_requests, *decision_requests),
                )

            deferred_fetch_resolutions = tuple(
                item
                for item in applied_decision.deferred_frontier_resolutions
                if item.action == "fetch_next_cycle"
            )
            has_deferred_article_fetch = any(
                request.tool_name == self._profile.graph_review_fetch_tool_name
                for request in decision_requests
            )
            if deferred_fetch_resolutions and not has_deferred_article_fetch:
                projected_request = self._deferred_frontier_fetch_request(
                    candidate,
                    deferred_fetch_resolutions,
                )
                decision_requests = (*decision_requests, projected_request)
                candidate = self._replace_state(
                    candidate,
                    tool_requests=(*candidate.tool_requests, projected_request),
                )

            if not decision_requests:
                continue_known_graph_candidates = bool(
                    applied_decision.unreviewed_graph_resolution is not None
                    and applied_decision.unreviewed_graph_resolution.action
                    == "review_next_cycle"
                )
                if (
                    applied_decision.start_next_cycle
                    and purpose != "hypothesis_revision"
                    and not graph_review_call
                    and not search_review_call
                ):
                    if (
                        _all_open_hypothesis_new_exploration_sets_exhausted(
                            context
                        )
                        and not continue_known_graph_candidates
                    ):
                        candidate = self._replace_state(
                            candidate,
                            cycle_step_timeout=False,
                            stop_reason="exploration_limit_reached",
                            updated_at=utc_now(),
                        )
                    else:
                        candidate = self._replace_state(
                            candidate,
                            research_cycle_count=max(
                                1,
                                candidate.research_cycle_count + 1,
                            ),
                            cycle_step_timeout=False,
                            stop_reason=None,
                            updated_at=utc_now(),
                        )
                self._store.save(candidate)
                state = candidate
                continue

            remaining_after_decision = self._remaining_wall_time(started_at)
            if (
                remaining_after_decision
                <= self._profile.limits.finalization_reserve_sec
            ):
                state = self._replace_state(
                    candidate,
                    tool_requests=state.tool_requests,
                    stop_reason="finalization_reserve",
                )
                self._store.save(state)
                reviewer_findings = ()
                continue

            state = candidate
            requests = self._with_automatic_tools(
                state,
                decision_requests,
            )
            automatic_requests = requests[len(decision_requests) :]
            if automatic_requests:
                state = self._replace_state(
                    state,
                    tool_requests=(*state.tool_requests, *automatic_requests),
                )
            self._store.save(state)
            state = self._execute_cycle(
                state,
                requests,
                started_at=started_at,
                tool_traces=tool_traces,
                advance_research_cycle=(
                    state.research_cycle_count == 0
                    or applied_decision.start_next_cycle
                )
                and not graph_review_call
                and not search_review_call,
            )
            reviewer_findings = ()

        return state

    def _solver_profile_for_context(
        self,
        *,
        context: SolverContext,
        graph_review_call: bool,
        search_review_call: bool,
        integration_call: bool,
        has_reviewer_findings: bool,
        observation_integration_call: bool = False,
        hypothesis_revision_call: bool = False,
    ) -> tuple[ModelCallProfile, str]:
        if graph_review_call and self._profile.solver_graph_review is not None:
            return self._profile.solver_graph_review, "graph_selection"
        if search_review_call and self._profile.solver_search_review is not None:
            return self._profile.solver_search_review, "search_selection"
        if (
            has_reviewer_findings
            and self._profile.solver_reviewer_revision is not None
        ):
            return self._profile.solver_reviewer_revision, "reviewer_revision"
        if observation_integration_call:
            observation_profile = (
                self._profile.solver_observation_integration
                or self._profile.solver_cycle_close
                or self._profile.solver_integration
            )
            return (
                observation_profile.model_copy(
                    update={"context_projection": "observation_integration"}
                ),
                "observation_integration",
            )
        if (
            hypothesis_revision_call
            and self._profile.solver_hypothesis_revision is not None
        ):
            return self._profile.solver_hypothesis_revision, "hypothesis_revision"
        if context.finalize_only and self._profile.solver_finalization is not None:
            return self._profile.solver_finalization, "finalization"
        if (
            context.cycle_close_required
            and self._profile.solver_cycle_close is not None
        ):
            return self._profile.solver_cycle_close, "cycle_close"
        if (
            integration_call
            and context.recent_tool_results
            and (
                self._profile.solver_observation_integration is not None
                or self._profile.solver_cycle_close is not None
            )
        ):
            observation_profile = (
                self._profile.solver_observation_integration
                or self._profile.solver_cycle_close
            )
            assert observation_profile is not None
            return (
                observation_profile.model_copy(
                    update={"context_projection": "observation_integration"}
                ),
                "observation_integration",
            )
        if integration_call or context.finalize_only:
            return self._profile.solver_integration, "integration"
        work_items_with_hypotheses = {
            item.work_item_id for item in context.hypotheses
        }
        if (
            any(
                item.state == "open"
                and item.work_item_id not in work_items_with_hypotheses
                for item in context.work_tree
            )
            and self._profile.solver_hypothesis_generation is not None
        ):
            return (
                self._profile.solver_hypothesis_generation,
                "hypothesis_generation",
            )
        if (
            context.work_tree
            and context.hypotheses
            and self._profile.solver_search_planning is not None
        ):
            return self._profile.solver_search_planning, "search_planning"
        return self._profile.solver_research, "research"

    def _graph_review_fetch_request(
        self,
        state: CaseState,
        review: GraphCandidateReview,
        *,
        fetchable_article_ids: set[str],
    ) -> ToolRequest | None:
        selected_decisions = tuple(
            item
            for item in review.frontier_decisions
            if item.action == "select" and item.article_id in fetchable_article_ids
        )
        if not selected_decisions:
            return None
        selected_ids = tuple(
            dict.fromkeys(item.article_id for item in selected_decisions)
        )
        primary_work_item_id = selected_decisions[0].work_item_id
        hypothesis_ids = tuple(
            dict.fromkeys(
                item.hypothesis_id
                for item in selected_decisions
                if item.hypothesis_id is not None
            )
        )
        payload = json.dumps(
            {
                "graph_request_ids": review.graph_request_ids,
                "selected_article_ids": selected_ids,
                "review_sequence": len(state.graph_candidate_reviews),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
        return ToolRequest(
            request_id=f"{GRAPH_REVIEW_FETCH_REQUEST_PREFIX}{digest}",
            work_item_id=primary_work_item_id,
            tool_name=self._profile.graph_review_fetch_tool_name or "fetch_articles",
            arguments={"article_ids": list(selected_ids)},
            purpose="Solverが選んだGraph候補Article本文を取得する",
            hypothesis_ids=hypothesis_ids,
        )

    def _graph_review_load_request(
        self,
        state: CaseState,
        review: GraphCandidateReview,
        *,
        fetchable_article_ids: set[str],
    ) -> ToolRequest | None:
        """LLMが選び直した取得済みGraph候補の本文を再提示する。"""

        selected_decisions = tuple(
            item
            for item in review.frontier_decisions
            if item.action == "select"
            and item.article_id not in fetchable_article_ids
        )
        if not selected_decisions:
            return None
        evidence_ids_by_article: dict[str, list[str]] = {}
        for evidence in state.evidence:
            article_id = evidence.metadata.get("articleId")
            if (
                not isinstance(article_id, str)
                or evidence.metadata.get("citationEligible") is False
            ):
                continue
            evidence_ids_by_article.setdefault(article_id, []).append(
                evidence.evidence_id
            )
        hypotheses_by_id = {
            item.hypothesis_id: item for item in state.hypotheses
        }
        evidence_ids: list[str] = []
        applicable_decisions = []
        for decision in selected_decisions:
            article_evidence_ids = evidence_ids_by_article.get(
                decision.article_id,
                (),
            )
            if not article_evidence_ids:
                continue
            hypothesis = (
                hypotheses_by_id.get(decision.hypothesis_id)
                if decision.hypothesis_id is not None
                else None
            )
            if hypothesis is not None and set(article_evidence_ids).issubset(
                hypothesis.evidence_ids
            ):
                continue
            applicable_decisions.append(decision)
            evidence_ids.extend(
                item for item in article_evidence_ids if item not in evidence_ids
            )
        if not applicable_decisions or not evidence_ids:
            return None
        hypothesis_ids = tuple(
            dict.fromkeys(
                item.hypothesis_id
                for item in applicable_decisions
                if item.hypothesis_id is not None
            )
        )
        payload = json.dumps(
            {
                "graph_request_ids": review.graph_request_ids,
                "selected_article_ids": [
                    item.article_id for item in applicable_decisions
                ],
                "evidence_ids": evidence_ids,
                "review_sequence": len(state.graph_candidate_reviews),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
        return ToolRequest(
            request_id=f"graph-review-load-{digest}",
            work_item_id=applicable_decisions[0].work_item_id,
            tool_name=LOAD_EVIDENCE_TOOL,
            arguments={"evidence_ids": evidence_ids},
            purpose="Solverが選んだ取得済みGraph候補本文を再表示する",
            hypothesis_ids=hypothesis_ids,
        )

    def _search_review_fetch_requests(
        self,
        state: CaseState,
        review: SearchCandidateReview,
        candidates: tuple[SearchCandidateArticle, ...],
    ) -> tuple[ToolRequest, ...]:
        if not review.selections:
            raise ContractViolation("Search review fetch requires a selected Article")
        candidates_by_id = {item.article_id: item for item in candidates}
        hypothesis_work_items = {
            item.hypothesis_id: item.work_item_id for item in state.hypotheses
        }
        fetched_by_work_item = fetched_article_ids_by_work_item(
            state,
            cycle_no=None,
        )
        grouped: dict[str, dict[str, list[str]]] = {}
        for selection in review.selections:
            candidate = candidates_by_id[selection.article_id]
            matched_by_work_item: dict[str, list[str]] = {}
            for hypothesis_id in selection.matched_hypothesis_ids:
                work_item_id = hypothesis_work_items.get(hypothesis_id)
                if work_item_id is not None:
                    matched_by_work_item.setdefault(work_item_id, []).append(
                        hypothesis_id
                    )
            if not matched_by_work_item:
                for hypothesis_id in candidate.discovery_hypothesis_ids:
                    work_item_id = hypothesis_work_items.get(hypothesis_id)
                    if work_item_id is not None:
                        matched_by_work_item.setdefault(work_item_id, []).append(
                            hypothesis_id
                        )
            if not matched_by_work_item:
                for work_item_id in candidate.discovery_work_item_ids:
                    matched_by_work_item.setdefault(work_item_id, [])
            if not matched_by_work_item:
                raise ContractViolation(
                    "Search review fetch requires discovery provenance"
                )
            for work_item_id, hypothesis_ids in matched_by_work_item.items():
                if selection.article_id in fetched_by_work_item.get(
                    work_item_id,
                    (),
                ):
                    continue
                item = grouped.setdefault(
                    work_item_id,
                    {"article_ids": [], "hypothesis_ids": []},
                )
                if selection.article_id not in item["article_ids"]:
                    item["article_ids"].append(selection.article_id)
                for hypothesis_id in hypothesis_ids:
                    if hypothesis_id not in item["hypothesis_ids"]:
                        item["hypothesis_ids"].append(hypothesis_id)

        requests: list[ToolRequest] = []
        for work_item_id, item in grouped.items():
            payload = json.dumps(
                {
                    "search_request_ids": review.search_request_ids,
                    "selected_article_ids": item["article_ids"],
                    "work_item_id": work_item_id,
                    "review_sequence": len(state.search_candidate_reviews),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
            requests.append(
                ToolRequest(
                    request_id=f"search-review-fetch-{digest}",
                    work_item_id=work_item_id,
                    tool_name=self._profile.graph_review_fetch_tool_name
                    or "fetch_articles",
                    arguments={"article_ids": item["article_ids"]},
                    purpose="Solverが選んだ検索候補Article本文を取得する",
                    hypothesis_ids=tuple(item["hypothesis_ids"]),
                )
            )
        return tuple(requests)

    def _deferred_frontier_fetch_request(
        self,
        state: CaseState,
        resolutions: tuple[DeferredFrontierResolution, ...],
    ) -> ToolRequest:
        article_ids = tuple(dict.fromkeys(item.article_id for item in resolutions))
        hypothesis_ids = tuple(
            dict.fromkeys(
                item.hypothesis_id
                for item in resolutions
                if item.hypothesis_id is not None
            )
        )
        payload = json.dumps(
            {
                "article_ids": article_ids,
                "frontier_item_ids": tuple(
                    item.frontier_item_id for item in resolutions
                ),
                "cycle_no": max(1, state.research_cycle_count + 1),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
        return ToolRequest(
            request_id=f"deferred-frontier-fetch-{digest}",
            work_item_id=resolutions[0].work_item_id,
            tool_name=self._profile.graph_review_fetch_tool_name or "fetch_articles",
            arguments={"article_ids": list(article_ids)},
            purpose="Solverが次Cycle取得を選んだ保留Article本文を取得する",
            hypothesis_ids=hypothesis_ids,
        )

    @property
    def _read_only_tool_names(self) -> frozenset[str]:
        names = {
            name
            for name in self._tools.names
            if self._tools.get(name).definition.read_only
        }
        names.add(LOAD_EVIDENCE_TOOL)
        for automatic in self._profile.automatic_tools:
            if not automatic.solver_may_request:
                names.discard(automatic.tool_name)
        return frozenset(names)

    @property
    def _solver_tool_definitions(self) -> tuple[ToolDefinition, ...]:
        allowed_names = self._read_only_tool_names
        definitions = [
            definition
            for definition in self._tools.definitions
            if definition.name in allowed_names
        ]
        if LOAD_EVIDENCE_TOOL in allowed_names:
            definitions.append(LOAD_EVIDENCE_DEFINITION)
        return tuple(definitions)

    def _with_automatic_tools(
        self,
        state: CaseState,
        requests: tuple[ToolRequest, ...],
    ) -> tuple[ToolRequest, ...]:
        automatic_requests: list[ToolRequest] = []
        for automatic in self._profile.automatic_tools:
            tool = self._tools.get(automatic.tool_name)
            if not tool.definition.read_only:
                raise ContractViolation(
                    f"automatic tool is not read-only: {automatic.tool_name}"
                )

            seen_values: set[str] = set()
            one_hop_candidates = self._evidence_metadata_values(
                state,
                automatic.one_hop_candidate_metadata_key,
            )
            independent_roots = self._evidence_metadata_values(
                state,
                automatic.independent_root_metadata_key,
                evidence_role=automatic.independent_root_evidence_role,
            )
            # 同じArticleがGraph候補としても検索起点としても発見された場合、
            # 独立したdepth 0経路をGraph由来depth 1で上書きしない。
            one_hop_candidates.difference_update(independent_roots)
            deduplicate_name = automatic.deduplicate_list_argument
            if deduplicate_name is not None:
                successful_request_ids = {
                    result.request_id
                    for result in state.tool_results
                    if result.status == "succeeded"
                }
                for existing in state.tool_requests:
                    if existing.tool_name != automatic.tool_name:
                        continue
                    if existing.request_id not in successful_request_ids:
                        continue
                    values = existing.arguments.get(deduplicate_name, ())
                    if isinstance(values, (list, tuple)):
                        seen_values.update(
                            value for value in values if isinstance(value, str)
                        )
                for existing in automatic_requests:
                    if existing.tool_name != automatic.tool_name:
                        continue
                    values = existing.arguments.get(deduplicate_name, ())
                    if isinstance(values, (list, tuple)):
                        seen_values.update(
                            value for value in values if isinstance(value, str)
                        )

            for request in requests:
                if request.tool_name != automatic.trigger_tool_name:
                    continue
                arguments = dict(automatic.fixed_arguments)
                for name in automatic.copied_argument_names:
                    if name not in request.arguments:
                        raise ContractViolation(
                            f"automatic tool trigger is missing argument: {name}"
                        )
                    arguments[name] = request.arguments[name]

                if deduplicate_name is not None:
                    values = arguments[deduplicate_name]
                    if not isinstance(values, (list, tuple)) or any(
                        not isinstance(value, str) for value in values
                    ):
                        raise ContractViolation(
                            "automatic tool deduplicated argument must be a string list"
                        )
                    unique_values = [
                        value
                        for value in dict.fromkeys(values)
                        if value not in seen_values
                        and value not in one_hop_candidates
                    ]
                    if not unique_values:
                        continue
                    arguments[deduplicate_name] = unique_values
                    seen_values.update(unique_values)

                automatic_requests.append(
                    ToolRequest(
                        request_id=self._automatic_request_id(
                            request,
                            automatic.tool_name,
                            arguments,
                        ),
                        work_item_id=request.work_item_id,
                        tool_name=automatic.tool_name,
                        arguments=arguments,
                        purpose=automatic.purpose,
                        hypothesis_ids=request.hypothesis_ids,
                    )
                )
        return (*requests, *automatic_requests)

    @staticmethod
    def _evidence_metadata_values(
        state: CaseState,
        metadata_key: str | None,
        *,
        evidence_role: str | None = None,
    ) -> set[str]:
        if metadata_key is None:
            return set()
        values: set[str] = set()
        for evidence in state.evidence:
            if (
                evidence_role is not None
                and evidence.metadata.get("evidenceRole") != evidence_role
            ):
                continue
            value = evidence.metadata.get(metadata_key)
            if isinstance(value, str):
                values.add(value)
            elif isinstance(value, (list, tuple)):
                values.update(item for item in value if isinstance(item, str))
        return values

    @staticmethod
    def _automatic_request_id(
        trigger: ToolRequest,
        tool_name: str,
        arguments: dict[str, object],
    ) -> str:
        payload = json.dumps(
            {
                "trigger_request_id": trigger.request_id,
                "tool_name": tool_name,
                "arguments": arguments,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
        return f"automatic-{digest}"

    def _execute_cycle(
        self,
        state: CaseState,
        requests: tuple[ToolRequest, ...],
        *,
        started_at: float,
        tool_traces: list[ToolCallTrace],
        advance_research_cycle: bool = True,
    ) -> CaseState:
        cycle_no = (
            state.research_cycle_count + 1
            if advance_research_cycle
            else max(1, state.research_cycle_count)
        )
        remaining = self._remaining_wall_time(started_at)
        timeout_sec = max(
            0.001,
            remaining - self._profile.limits.finalization_reserve_sec,
        )

        if self._can_run_in_parallel(requests):
            with ThreadPoolExecutor(
                max_workers=min(
                    len(requests),
                    self._profile.limits.max_parallel_tools,
                )
            ) as executor:
                executions = tuple(
                    executor.map(
                        lambda request: self._execute_one(
                            state,
                            request,
                            cycle_no=cycle_no,
                            timeout_sec=timeout_sec,
                        ),
                        requests,
                    )
                )
        else:
            executions = tuple(
                self._execute_one(
                    state,
                    request,
                    cycle_no=cycle_no,
                    timeout_sec=timeout_sec,
                )
                for request in requests
            )

        evidence_by_id = {item.evidence_id: item for item in state.evidence}
        new_evidence: list[Evidence] = []
        new_results: list[ToolResult] = []
        for request, execution in zip(requests, executions, strict=True):
            self._validate_tool_execution(request, execution, cycle_no)
            if self._diagnostics is not None:
                self._diagnostics.record_tool_execution(
                    request=request,
                    result=execution.result,
                )
            new_results.append(execution.result)
            tool_traces.append(
                ToolCallTrace(
                    request_id=request.request_id,
                    tool_name=request.tool_name,
                    arguments=request.arguments,
                    purpose=request.purpose,
                    status=execution.result.status,
                    elapsed_ms=execution.result.elapsed_ms,
                    cycle_no=cycle_no,
                )
            )
            for evidence in execution.evidence:
                existing = evidence_by_id.get(evidence.evidence_id)
                if existing is not None and not self._same_evidence(
                    existing,
                    evidence,
                ):
                    raise ContractViolation(
                        f"tool returned conflicting evidence ID: {evidence.evidence_id}"
                    )
                if existing is None:
                    evidence_by_id[evidence.evidence_id] = evidence
                    new_evidence.append(evidence)

        state = self._replace_state(
            state,
            research_cycle_count=(
                cycle_no if advance_research_cycle else state.research_cycle_count
            ),
            evidence=(*state.evidence, *new_evidence),
            tool_results=(*state.tool_results, *new_results),
            updated_at=utc_now(),
            cycle_step_timeout=False,
            stop_reason=None,
        )
        self._store.save(state)
        return state

    def _execute_one(
        self,
        state: CaseState,
        request: ToolRequest,
        *,
        cycle_no: int,
        timeout_sec: float,
    ) -> ToolExecution:
        started = self._clock()
        if request.tool_name == LOAD_EVIDENCE_TOOL:
            return self._load_evidence(state, request, cycle_no, started)

        tool = self._tools.get(request.tool_name)
        if not tool.definition.read_only:
            raise ContractViolation(f"write tool is not allowed: {request.tool_name}")
        try:
            return tool.execute(
                request,
                cycle_no=cycle_no,
                timeout_sec=timeout_sec,
            )
        except TimeoutError:
            return ToolExecution(
                result=ToolResult(
                    request_id=request.request_id,
                    status="timeout",
                    error_code="tool_timeout",
                    elapsed_ms=self._elapsed_ms(started),
                    cycle_no=cycle_no,
                )
            )
        except Exception:  # noqa: BLE001 - Tool境界を失敗結果へ正規化する
            return ToolExecution(
                result=ToolResult(
                    request_id=request.request_id,
                    status="failed",
                    error_code="tool_error",
                    elapsed_ms=self._elapsed_ms(started),
                    cycle_no=cycle_no,
                )
            )

    def _load_evidence(
        self,
        state: CaseState,
        request: ToolRequest,
        cycle_no: int,
        started: float,
    ) -> ToolExecution:
        requested_ids = request.arguments.get("evidence_ids")
        if (
            not isinstance(requested_ids, list)
            or not requested_ids
            or any(not isinstance(item, str) for item in requested_ids)
            or len(requested_ids) != len(set(requested_ids))
        ):
            raise ContractViolation(
                "load_evidence requires a non-empty unique evidence_ids list"
            )
        known_ids = {item.evidence_id for item in state.evidence}
        unknown_ids = set(requested_ids) - known_ids
        if unknown_ids:
            raise ContractViolation(
                f"load_evidence references unknown IDs: {sorted(unknown_ids)}"
            )
        return ToolExecution(
            result=ToolResult(
                request_id=request.request_id,
                status="succeeded",
                evidence_ids=tuple(requested_ids),
                elapsed_ms=self._elapsed_ms(started),
                cycle_no=cycle_no,
            )
        )

    def _can_run_in_parallel(self, requests: tuple[ToolRequest, ...]) -> bool:
        if len(requests) <= 1:
            return False
        for request in requests:
            if request.tool_name == LOAD_EVIDENCE_TOOL:
                continue
            definition = self._tools.get(request.tool_name).definition
            if not definition.read_only or not definition.parallel_safe:
                return False
        return True

    def _validate_tool_execution(
        self,
        request: ToolRequest,
        execution: ToolExecution,
        cycle_no: int,
    ) -> None:
        result = execution.result
        if result.request_id != request.request_id:
            raise ContractViolation("tool result request ID does not match")
        if result.cycle_no != cycle_no:
            raise ContractViolation("tool result cycle does not match")
        evidence_ids = tuple(item.evidence_id for item in execution.evidence)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ContractViolation("tool returned duplicate evidence IDs")
        if request.tool_name != LOAD_EVIDENCE_TOOL and set(result.evidence_ids) != set(
            evidence_ids
        ):
            raise ContractViolation("tool result evidence IDs do not match payload")
        if any(item.created_cycle != cycle_no for item in execution.evidence):
            raise ContractViolation("tool evidence cycle does not match")

    def _build_reviewer_view(self, state: CaseState) -> ReviewerView:
        if state.final_answer is None:
            raise ContractViolation("review requires a final answer")
        evidence_ids = {item.evidence_id for item in state.evidence}
        unknown_citations = set(state.final_answer.citation_ids) - evidence_ids
        if unknown_citations:
            raise ContractViolation(
                f"review citation is not stored: {sorted(unknown_citations)}"
            )
        grounding_evidence = tuple(
            item
            for item in state.evidence
            if item.metadata.get("citationEligible") is not False
        )
        return ReviewerView(
            case_id=state.case_id,
            question=state.question,
            answer_options=state.answer_options,
            answer=state.final_answer,
            work_items=state.work_items,
            hypotheses=active_hypotheses(state),
            dependency_decisions=state.dependency_decisions,
            evidence=grounding_evidence,
        )

    def _validate_review_result(
        self,
        view: ReviewerView,
        review: ReviewResult,
    ) -> None:
        finding_ids = [item.finding_id for item in review.findings]
        if len(finding_ids) != len(set(finding_ids)):
            raise ContractViolation("Reviewer finding IDs must be unique")
        work_items = {item.work_item_id: item for item in view.work_items}
        hypotheses = {item.hypothesis_id: item for item in view.hypotheses}
        evidence_ids = {item.evidence_id for item in view.evidence}
        for finding in review.findings:
            if (
                finding.work_item_id is not None
                and finding.work_item_id not in work_items
            ):
                raise ContractViolation(
                    f"Reviewer finding references unknown WorkItem: "
                    f"{finding.work_item_id}"
                )
            if finding.hypothesis_id is not None:
                hypothesis = hypotheses.get(finding.hypothesis_id)
                if hypothesis is None:
                    raise ContractViolation(
                        f"Reviewer finding references unknown Hypothesis: "
                        f"{finding.hypothesis_id}"
                    )
                if (
                    finding.work_item_id is not None
                    and hypothesis.work_item_id != finding.work_item_id
                ):
                    raise ContractViolation(
                        "Reviewer finding Hypothesis does not belong to its WorkItem"
                    )
            unknown_evidence = set(finding.basis_evidence_ids) - evidence_ids
            if unknown_evidence:
                raise ContractViolation(
                    "Reviewer finding references unknown Evidence: "
                    f"{sorted(unknown_evidence)}"
                )

    def _finish(
        self,
        state: CaseState,
        status: RunStatus,
        *,
        stop_reason: str | None = None,
    ) -> CaseState:
        state = self._replace_state(
            state,
            run_status=status,
            stop_reason=stop_reason if stop_reason is not None else state.stop_reason,
            updated_at=utc_now(),
        )
        self._store.save(state)
        return state

    def _bounded_model_profile(
        self,
        profile: ModelCallProfile,
        remaining_sec: float,
    ) -> ModelCallProfile:
        return ModelCallProfile.model_validate(
            {
                **profile.model_dump(),
                "timeout_sec": min(profile.timeout_sec, remaining_sec),
            }
        )

    def _remaining_wall_time(self, started_at: float) -> float:
        return max(
            0.0,
            self._profile.limits.max_wall_time_sec - (self._clock() - started_at),
        )

    def _elapsed_ms(self, started_at: float) -> int:
        return max(0, round((self._clock() - started_at) * 1000))

    @staticmethod
    def _replace_state(state: CaseState, **updates) -> CaseState:
        return CaseState.model_validate({**state.model_dump(), **updates})

    @staticmethod
    def _same_evidence(first: Evidence, second: Evidence) -> bool:
        return (
            first.evidence_id == second.evidence_id
            and first.source_ref == second.source_ref
            and first.content == second.content
            and first.title == second.title
            and first.metadata == second.metadata
        )


def _merge_contract_violations(previous: str | None, current: str) -> str:
    """同じ未適用Decisionの修復中に判明した違反を失わない。"""

    if not previous:
        return current
    known = previous.split("\n")
    if current in known:
        return previous
    return f"{previous}\n{current}"


def _dependency_audit_work_item_ids(state: CaseState) -> tuple[str, ...]:
    """未統合の本文取得へ関連付けたopen WorkItemだけを監査対象へ投影する。"""

    open_work_item_ids = {
        item.work_item_id for item in state.work_items if item.state == "open"
    }
    hypothesis_work_item_ids = {
        item.hypothesis_id: item.work_item_id for item in state.hypotheses
    }
    grounding_evidence_ids = {
        evidence.evidence_id
        for evidence in state.evidence
        if evidence.metadata.get("citationEligible") is not False
    }
    grounding_request_ids = {
        result.request_id
        for result in state.tool_results
        if result.status == "succeeded"
        and result.request_id not in state.integrated_tool_result_request_ids
        and grounding_evidence_ids.intersection(result.evidence_ids)
    }
    projected: list[str] = []
    for request in state.tool_requests:
        if request.request_id not in grounding_request_ids:
            continue
        candidates = (
            request.work_item_id,
            *(hypothesis_work_item_ids.get(item) for item in request.hypothesis_ids),
        )
        for work_item_id in candidates:
            if (
                work_item_id in open_work_item_ids
                and work_item_id not in projected
            ):
                projected.append(work_item_id)
    return tuple(projected)


def _grounding_observation_context(context: SolverContext) -> SolverContext:
    """本文統合中は、未Reviewの探索結果だけを未処理のまま残す。"""

    grounding_ids = set(context.grounding_evidence_ids)
    grounding_results = tuple(
        result
        for result in context.recent_tool_results
        if not grounding_ids.isdisjoint(result.evidence_ids)
    )
    grounding_request_ids = {
        result.request_id for result in grounding_results
    }
    update: dict[str, object] = {
        "recent_tool_requests": tuple(
            request
            for request in context.recent_tool_requests
            if request.request_id in grounding_request_ids
        ),
        "recent_tool_results": grounding_results,
    }
    return context.model_copy(update=update)


def _defer_actions_until_candidate_review(
    decision: SolverDecision,
    *,
    work_item_ids: frozenset[str],
) -> SolverDecision:
    """未評価候補のReview境界を越える新規Tool要求を保留する。"""

    if not work_item_ids:
        return decision
    deferred_request_ids = {
        request.request_id
        for request in decision.tool_requests
        if request.work_item_id in work_item_ids
    }
    if not deferred_request_ids:
        return decision
    return decision.model_copy(
        update={
            "tool_requests": tuple(
                request
                for request in decision.tool_requests
                if request.request_id not in deferred_request_ids
            ),
            "dependency_decisions": tuple(
                item.model_copy(update={"action_request_id": None})
                if item.action_request_id in deferred_request_ids
                else item
                for item in decision.dependency_decisions
            ),
        }
    )


def _finalization_decision_context(context: SolverContext) -> SolverContext:
    """最終回答では、探索中だけ必要な未処理要求を再判定させない。"""

    return context.model_copy(
        update={
            "required_dependency_kind": None,
            "required_dependency_work_item_ids": (),
            "required_graph_review_request_ids": (),
            "required_search_review_request_ids": (),
            "search_candidates": (),
            "evidence_hypothesis_candidates": (),
            "fetchable_article_ids": (),
        }
    )


def _has_unintegrated_grounding_result(
    state: CaseState,
    *,
    tool_names: frozenset[str],
) -> bool:
    """現在Cycleに、まだ意味統合していない本文結果があるかを返す。"""

    requests_by_id = {item.request_id: item for item in state.tool_requests}
    grounding_evidence_ids = {
        evidence.evidence_id
        for evidence in state.evidence
        if evidence.metadata.get("citationEligible") is not False
    }
    return any(
        result.cycle_no == state.research_cycle_count
        and result.status == "succeeded"
        and (request := requests_by_id.get(result.request_id)) is not None
        and request.tool_name in tool_names
        and result.request_id not in state.integrated_tool_result_request_ids
        and not grounding_evidence_ids.isdisjoint(result.evidence_ids)
        for result in state.tool_results
    )


def _has_current_cycle_contradicted_hypothesis(state: CaseState) -> bool:
    """当Cycleの取得本文で反証された、見直し対象Hypothesisがあるかを返す。"""

    active_work_item_ids = {
        item.work_item_id for item in state.work_items if item.state != "dropped"
    }
    current_cycle_evidence_ids = {
        item.evidence_id
        for item in state.evidence
        if item.created_cycle == state.research_cycle_count
        and item.metadata.get("citationEligible") is not False
    }
    return any(
        item.work_item_id in active_work_item_ids
        and item.judgment == "contradicted"
        and not current_cycle_evidence_ids.isdisjoint(item.evidence_ids)
        for item in active_hypotheses(state)
    )


def _dependency_audit_scope(
    state: CaseState,
    *,
    integration_call: bool,
    finalize_only: bool,
    required_dependency_kind: str | None,
) -> tuple[str, ...]:
    """意味統合時だけ下位規範判断を要求し、最終整形では再提出させない。"""

    if (
        finalize_only
        or not integration_call
        or required_dependency_kind is None
    ):
        return ()
    open_work_item_ids = {
        item.work_item_id for item in state.work_items if item.state == "open"
    }
    return tuple(
        dict.fromkeys(
            (
                *_dependency_audit_work_item_ids(state),
                *(
                    item.work_item_id
                    for item in state.dependency_decisions
                    if item.status == "needs_action"
                    and item.work_item_id in open_work_item_ids
                ),
            )
        )
    )
