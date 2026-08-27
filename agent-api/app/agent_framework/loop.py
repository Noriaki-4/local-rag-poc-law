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
    ContextCapacityExceeded,
    GraphReviewBatch,
    SearchCandidateArticle,
    SolverActionFeedback,
    SolverContext,
    SolverContractFeedback,
    build_solver_context,
)
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
    utc_now,
)
from .store import CaseStore
from .validation import ActionRejected, ContractViolation, apply_solver_decision

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

        if self._diagnostics is not None:
            self._diagnostics.record_run_complete(
                state=state,
                failure_code=failure_code,
            )
        return AgentRunResult(
            state=state,
            trace=RunTrace(
                reviewer_enabled=self._profile.reviewer.enabled,
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
            completion_reserve = (
                self._profile.limits.finalization_reserve_sec
                + self._profile.limits.cycle_close_reserve_sec
                + self._profile.limits.min_next_cycle_budget_sec
            )
            time_reserve_reached = remaining <= completion_reserve
            finalize_only = (
                cycle_limit_reached
                or time_reserve_reached
                or state.cycle_step_timeout
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
                    finalize_only=finalize_only,
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
                graph_review_call = bool(
                    context.required_graph_review_request_ids
                    and self._profile.solver_graph_review is not None
                    and not finalize_only
                    and not reviewer_findings
                )
                search_review_call = bool(
                    context.required_search_review_request_ids
                    and self._profile.solver_search_review is not None
                    and not graph_review_call
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
                    if self._diagnostics is not None:
                        self._diagnostics.record_solver_output(
                            state=state,
                            purpose=f"{purpose}_{exc.completed_stage}_checkpoint",
                            contract_attempt=decision_attempt,
                            decision=partial_decision,
                        )
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
                        checkpoint_state = state
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
                            latency_ms=self._elapsed_ms(call_started),
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
                        latency_ms=self._elapsed_ms(call_started),
                        input_tokens=solver_call.input_tokens,
                        output_tokens=solver_call.output_tokens,
                        attempt_count=solver_call.attempt_count,
                        finalize_only=finalize_only,
                    )
                )
                if self._diagnostics is not None:
                    self._diagnostics.record_solver_output(
                        state=state,
                        purpose=purpose,
                        contract_attempt=decision_attempt,
                        decision=solver_call.decision,
                    )
                try:
                    candidate = apply_solver_decision(
                        state,
                        solver_call.decision,
                        limits=self._profile.limits,
                        known_tool_names=self._read_only_tool_names,
                        material_evidence_ids=context.material_evidence_ids,
                        fetchable_article_ids=context.fetchable_article_ids,
                        required_dependency_kind=context.required_dependency_kind,
                        required_dependency_work_item_ids=(
                            context.required_dependency_work_item_ids
                        ),
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
                        ),
                        required_search_review_request_ids=(
                            context.required_search_review_request_ids
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
                            for item in (
                                *(
                                    candidate
                                    for candidate in context.graph_review_batch.candidates
                                    if candidate.content_status
                                    in {"not_requested", "failed", "timeout"}
                                ),
                                *(
                                    ledger_item
                                    for ledger_item in context.graph_review_ledger
                                    if (
                                        ledger_item.review_status
                                        == "relevant_deferred"
                                        and ledger_item.content_status
                                        in {"not_requested", "failed", "timeout"}
                                        and ledger_item.deferred_resolution_action
                                        != "no_longer_needed"
                                    )
                                    or (
                                        ledger_item.review_status == "selected"
                                        and ledger_item.content_status
                                        in {"failed", "timeout"}
                                    )
                                ),
                            )
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
                        cycle_close_required=context.cycle_close_required,
                        can_start_next_cycle=context.can_start_next_cycle,
                        finalize_only=finalize_only,
                        allow_dependency_action_without_tool=(
                            purpose == "observation_integration"
                            or bool(context.required_dependency_work_item_ids)
                        ),
                    )
                    if purpose in {"observation_integration", "cycle_close"}:
                        integrated_request_ids = tuple(
                            dict.fromkeys(
                                [
                                    *candidate.integrated_tool_result_request_ids,
                                    *(
                                        item.request_id
                                        for item in context.recent_tool_results
                                    ),
                                ]
                            )
                        )
                        candidate = self._replace_state(
                            candidate,
                            integrated_tool_result_request_ids=(
                                integrated_request_ids
                            ),
                        )
                    if self._diagnostics is not None:
                        self._diagnostics.record_decision_applied(
                            state_before=state,
                            state_after=candidate,
                            context=context,
                            purpose=purpose,
                            contract_attempt=decision_attempt,
                            decision=solver_call.decision,
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
            if solver_call.decision.next == "finalize":
                candidate = self._replace_state(
                    candidate,
                    cycle_step_timeout=False,
                    stop_reason=None,
                    updated_at=utc_now(),
                )
                self._store.save(candidate)
                return candidate

            decision_requests = solver_call.decision.tool_requests
            if (
                graph_review_call
                and solver_call.decision.graph_candidate_review is not None
                and solver_call.decision.graph_candidate_review.selected_article_ids
            ):
                graph_fetch_request = self._graph_review_fetch_request(
                    candidate,
                    solver_call.decision.graph_candidate_review,
                    fetchable_article_ids={
                        item.article_id
                        for item in context.graph_review_batch.candidates
                        if item.content_status
                        in {"not_requested", "failed", "timeout"}
                    },
                )
                decision_requests = (
                    (graph_fetch_request,) if graph_fetch_request is not None else ()
                )
                candidate = self._replace_state(
                    candidate,
                    tool_requests=(*candidate.tool_requests, *decision_requests),
                )
            elif (
                search_review_call
                and solver_call.decision.search_candidate_review is not None
                and solver_call.decision.search_candidate_review.selected_article_ids
            ):
                decision_requests = (
                    self._search_review_fetch_request(
                        candidate,
                        solver_call.decision.search_candidate_review,
                        context.search_candidates,
                    ),
                )
                candidate = self._replace_state(
                    candidate,
                    tool_requests=(*candidate.tool_requests, *decision_requests),
                )

            deferred_fetch_resolutions = tuple(
                item
                for item in solver_call.decision.deferred_frontier_resolutions
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
                if (
                    solver_call.decision.start_next_cycle
                    and not graph_review_call
                    and not search_review_call
                ):
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
                    or solver_call.decision.start_next_cycle
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
            and self._profile.solver_cycle_close is not None
        ):
            return (
                self._profile.solver_cycle_close.model_copy(
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
            request_id=f"graph-review-fetch-{digest}",
            work_item_id=primary_work_item_id,
            tool_name=self._profile.graph_review_fetch_tool_name or "fetch_articles",
            arguments={"article_ids": list(selected_ids)},
            purpose="Solverが選んだGraph候補Article本文を取得する",
            hypothesis_ids=hypothesis_ids,
        )

    def _search_review_fetch_request(
        self,
        state: CaseState,
        review: SearchCandidateReview,
        candidates: tuple[SearchCandidateArticle, ...],
    ) -> ToolRequest:
        if not review.selections:
            raise ContractViolation("Search review fetch requires a selected Article")
        candidates_by_id = {item.article_id: item for item in candidates}
        selected_candidates = tuple(
            candidates_by_id[item.article_id] for item in review.selections
        )
        primary = selected_candidates[0]
        if not primary.discovery_work_item_ids:
            raise ContractViolation(
                "Search review fetch requires discovery provenance"
            )
        selected_ids = tuple(item.article_id for item in review.selections)
        hypothesis_ids = tuple(
            dict.fromkeys(
                hypothesis_id
                for item in review.selections
                for hypothesis_id in item.matched_hypothesis_ids
            )
        )
        if not hypothesis_ids:
            hypothesis_ids = tuple(
                dict.fromkeys(
                    hypothesis_id
                    for item in selected_candidates
                    for hypothesis_id in item.discovery_hypothesis_ids
                )
            )
        payload = json.dumps(
            {
                "search_request_ids": review.search_request_ids,
                "selected_article_ids": selected_ids,
                "review_sequence": len(state.search_candidate_reviews),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
        return ToolRequest(
            request_id=f"search-review-fetch-{digest}",
            work_item_id=primary.discovery_work_item_ids[0],
            tool_name=self._profile.graph_review_fetch_tool_name
            or "fetch_articles",
            arguments={"article_ids": list(selected_ids)},
            purpose="Solverが選んだ検索候補Article本文を取得する",
            hypothesis_ids=hypothesis_ids,
        )

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
            answer=state.final_answer,
            work_items=state.work_items,
            hypotheses=state.hypotheses,
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
    fresh_scope = _dependency_audit_work_item_ids(state)
    if fresh_scope:
        return fresh_scope

    open_work_item_ids = {
        item.work_item_id for item in state.work_items if item.state == "open"
    }
    return tuple(
        dict.fromkeys(
            item.work_item_id
            for item in state.dependency_decisions
            if item.status == "needs_action"
            and item.work_item_id in open_work_item_ids
        )
    )
