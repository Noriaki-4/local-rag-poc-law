"""既存provider共通JSON transportを新FrameworkのModel Portへ接続する。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, wait
from copy import deepcopy
from time import monotonic
from typing import Any, Literal
from uuid import uuid4

import requests
from pydantic import BaseModel, ValidationError

from app.agent_framework.context import (
    ContextCapacityExceeded,
    GraphReviewLedgerItem,
    HypothesisRevisionEvidence,
    HypothesisRevisionHypothesis,
    HypothesisRevisionInput,
    HypothesisRevisionWorkItem,
    ResearchStepHypothesis,
    ResearchStepInput,
    ResearchStepWorkItem,
    SearchAssessmentCandidate,
    SearchAssessmentExcerpt,
    SearchCandidateContentAssessmentInput,
    SearchSelectionInput,
    SolverContext,
    WorkTreeItem,
    pending_candidate_review_work_item_ids,
)
from app.agent_framework.contract_rendering import (
    contract_field_description,
    render_model_input_glossary,
    render_research_step_input_glossary,
    render_solver_contract_glossary,
)
from app.agent_framework.contracts import (
    CaseUpdate,
    CycleCloseDecision,
    DependencyActionDecision,
    DependencyAssessmentDecision,
    HypothesisGapAddition,
    HypothesisUpdate,
    HypothesisRevisionDecision,
    ObservationIntegrationDecision,
    SearchAssessmentDecision,
    SearchCandidateAssessment,
    SearchReselectionDecision,
    SolverDecision,
    WorkItemImpactDecision,
    WorkItemUpdate,
)
from app.agent_framework.diagnostics import AgentDiagnostics
from app.agent_framework.model_call_artifacts import (
    RUNTIME_INPUT_MARKER,
    RenderedModelCall,
    build_rendered_model_call,
)
from app.agent_framework.ports.model import (
    ModelProtocolError,
    ReviewCallResult,
    ReviewerView,
    SolverCallResult,
    SolverCheckpointTimeout,
)
from app.agent_framework.profiles import ModelCallProfile, ReviewerProfile
from app.agent_framework.prompt_assets import (
    PromptAssetTrace,
    prompt_asset_trace,
    render_prompt_section,
)
from app.agent_framework.state import (
    DeferredFrontierResolution,
    DependencyDecision,
    FinalAnswer,
    FrontierReAdoption,
    GraphCandidateReview,
    GraphFrontierDecision,
    Hypothesis,
    ReviewFindingResolution,
    ReviewResult,
    SearchCandidateReview,
    SearchCandidateSelection,
    UnreviewedGraphResolution,
    ToolRequest,
    WorkItem,
    apply_hypothesis_gap_diff,
    merge_hypothesis_evidence_ids,
    structured_hypothesis_gaps,
)
from app.agent_framework.tool_contracts import ToolDefinition
from app.agent_framework.work_item_sessions import (
    WorkItemSession,
    WorkItemSessionCoordinator,
)
from app.llm import LLMClient


_SEARCH_REVIEW_BATCH_SIZE = 8


def _project_next_hypothesis_work_item(context: SolverContext) -> SolverContext:
    """Hypothesis未作成のWorkItemを1件だけLLMへ提示する。"""

    covered_ids = {item.work_item_id for item in context.hypotheses}
    pending = tuple(
        item
        for item in context.work_tree
        if item.state == "open" and item.work_item_id not in covered_ids
    )
    if not pending:
        return context
    return context.model_copy(update={"work_tree": (pending[0],)})


class StructuredJSONModelAdapter:
    def __init__(
        self,
        client: LLMClient,
        diagnostics: AgentDiagnostics | None = None,
    ) -> None:
        self._client = client
        self._diagnostics = diagnostics
        self._work_item_sessions = WorkItemSessionCoordinator()

    def solve(
        self,
        context: SolverContext,
        profile: ModelCallProfile,
    ) -> SolverCallResult:
        provider = getattr(self._client, "provider", None)
        if profile.context_projection == "research_hypothesis":
            context = _project_next_hypothesis_work_item(context)
        if profile.context_projection == "hypothesis_revision":
            return self._solve_hypothesis_revision(context, profile)
        if profile.context_projection == "observation_integration":
            return self._solve_observation_only(context, profile)
        if (
            context.required_search_review_request_ids
            and not context.graph_review_batch.candidates
            and not context.finalize_only
        ):
            return self._solve_search_review(context, profile)
        if (
            profile.context_projection == "graph_review"
            and context.graph_review_batch.candidates
            and not context.finalize_only
        ):
            return self._solve_graph_review(context, profile)
        if profile.context_projection == "cycle_close":
            return self._solve_cycle_close(context, profile)
        dependency_action_call = _is_dependency_action_call(context, profile)
        if dependency_action_call:
            context = _project_dependency_action_context(context)
        rendered = render_solver_model_call(context, profile, provider=provider)
        transport_schema = rendered.output_schema
        prompt = rendered.request
        input_tokens = 0
        output_tokens = 0
        input_tokens_known = True
        output_tokens_known = True
        attempt_count = 0
        last_error: ModelProtocolError | ValidationError | None = None
        started_at = monotonic()

        for repair_index in range(2):
            remaining_timeout = profile.timeout_sec - (monotonic() - started_at)
            if remaining_timeout <= 1:
                if self._diagnostics is not None:
                    self._diagnostics.record_transport_timeout(
                        context=context,
                        repair_index=repair_index,
                        reason="solver contract repair time exhausted",
                    )
                raise TimeoutError("solver contract repair time exhausted")
            if self._diagnostics is not None:
                self._diagnostics.record_transport_input(
                    context=context,
                    profile=profile,
                    rendered=rendered,
                    repair_index=repair_index,
                    transport_stage="solver",
                    provider=provider,
                )
            try:
                result = self._client.generate_structured_json(
                    prompt=prompt,
                    schema=transport_schema,
                    model=profile.model,
                    max_tokens=profile.max_output_tokens,
                    timeout_sec=max(1, round(remaining_timeout)),
                )
            except requests.Timeout as exc:
                if self._diagnostics is not None:
                    self._diagnostics.record_transport_timeout(
                        context=context,
                        repair_index=repair_index,
                        reason="model provider request timed out",
                    )
                raise TimeoutError("model provider request timed out") from exc
            attempt_count += 1 + result.retryCount
            if result.inputTokens is None:
                input_tokens_known = False
            else:
                input_tokens += result.inputTokens
            if result.outputTokens is None:
                output_tokens_known = False
            else:
                output_tokens += result.outputTokens

            if result.validationError or result.payload is None:
                last_error = ModelProtocolError(
                    f"solver transport invalid: {result.validationError or 'empty'}"
                )
            else:
                try:
                    if profile.context_projection in {
                        "research_decomposition",
                        "research_hypothesis",
                        "research_search",
                    }:
                        decision = normalize_staged_research_decision(
                            result.payload,
                            projection=profile.context_projection,
                            context=context,
                        )
                    elif dependency_action_call:
                        decision = normalize_dependency_action_decision(
                            result.payload,
                            context=context,
                        )
                    else:
                        normalized = _normalize_solver_payload(result.payload)
                        if normalized.get("next") == "finalize":
                            _include_required_finalization_citations(
                                normalized,
                                context,
                            )
                        _assign_tool_request_ids(normalized, context)
                        _normalize_absent_context_branches(normalized, context)
                        if _preserve_previous_update_for_contract_repair(context):
                            normalized["update"] = (
                                context.contract_feedback.previous_decision.update
                            )
                        if _preserve_previous_answer_for_contract_repair(
                            context,
                            normalized,
                        ):
                            normalized["answer"] = (
                                context.contract_feedback.previous_decision.answer
                            )
                        decision = SolverDecision.model_validate(normalized)
                        _validate_hypothesis_update_evidence(decision, context)
                    if self._diagnostics is not None:
                        self._diagnostics.record_transport_output(
                            context=context,
                            repair_index=repair_index,
                            payload=result.payload,
                            validation_error=None,
                            input_tokens=result.inputTokens,
                            output_tokens=result.outputTokens,
                            provider_retry_count=result.retryCount,
                            latency_ms=result.latencyMs,
                            transport_stage="solver",
                        )
                    return SolverCallResult(
                        decision=decision,
                        input_tokens=(input_tokens if input_tokens_known else None),
                        output_tokens=(output_tokens if output_tokens_known else None),
                        attempt_count=attempt_count,
                    )
                except (ModelProtocolError, ValidationError) as exc:
                    last_error = exc

            if self._diagnostics is not None:
                self._diagnostics.record_transport_output(
                    context=context,
                    repair_index=repair_index,
                    payload=result.payload,
                    validation_error=str(last_error),
                    input_tokens=result.inputTokens,
                    output_tokens=result.outputTokens,
                    provider_retry_count=result.retryCount,
                    latency_ms=result.latencyMs,
                    transport_stage="solver",
                )

            if repair_index == 0:
                if profile.context_projection in {
                    "research_decomposition",
                    "research_hypothesis",
                    "research_search",
                }:
                    rendered = _render_staged_research_repair_model_call(
                        context,
                        base_call=rendered,
                        payload=result.payload,
                        error=last_error,
                    )
                else:
                    rendered = render_solver_transport_repair_model_call(
                        context,
                        base_call=rendered,
                        payload=result.payload,
                        error=last_error,
                    )
                prompt = rendered.request
                _ensure_solver_prompt_capacity(prompt, context.max_solver_input_chars)

        if isinstance(last_error, ValidationError):
            detail = last_error.errors(
                include_url=False,
                include_input=False,
            )[0]
            location = ".".join(str(item) for item in detail.get("loc", ()))
            message = str(detail.get("msg") or "validation failed")
            raise ModelProtocolError(
                "solver decision violates schema: "
                f"{location or '<root>'}: {message}"
            ) from last_error
        if isinstance(last_error, ModelProtocolError):
            raise last_error
        raise ModelProtocolError("solver decision is unavailable")

    def _solve_graph_review(
        self,
        context: SolverContext,
        profile: ModelCallProfile,
    ) -> SolverCallResult:
        """Graph候補の意味選別だけを専用の小さい契約で実行する。"""

        rendered = render_graph_review_model_call(context, profile)
        provider = getattr(self._client, "provider", None)
        if self._diagnostics is not None:
            self._diagnostics.record_transport_input(
                context=context,
                profile=profile,
                rendered=rendered,
                repair_index=0,
                transport_stage="graph_review",
                provider=provider,
            )
        try:
            result = self._client.generate_structured_json(
                prompt=rendered.request,
                schema=rendered.output_schema,
                model=profile.model,
                max_tokens=profile.max_output_tokens,
                timeout_sec=max(1, round(profile.timeout_sec)),
            )
        except requests.Timeout as exc:
            if self._diagnostics is not None:
                self._diagnostics.record_transport_timeout(
                    context=context,
                    repair_index=0,
                    reason="graph review timed out",
                    transport_stage="graph_review",
                )
            raise TimeoutError("graph review timed out") from exc

        validation_error = result.validationError
        review: GraphCandidateReview | None = None
        if validation_error is None and result.payload is not None:
            try:
                review = GraphCandidateReview.model_validate(result.payload)
            except ValidationError as exc:
                validation_error = str(exc)
        elif validation_error is None:
            validation_error = "empty"

        if self._diagnostics is not None:
            self._diagnostics.record_transport_output(
                context=context,
                repair_index=0,
                payload=result.payload,
                validation_error=validation_error,
                input_tokens=result.inputTokens,
                output_tokens=result.outputTokens,
                provider_retry_count=result.retryCount,
                latency_ms=result.latencyMs,
                transport_stage="graph_review",
            )
        if validation_error is not None or review is None:
            raise ModelProtocolError(
                f"graph review transport invalid: {validation_error}"
            )

        decision = SolverDecision(
            next="continue",
            decision_reason=review.reason,
            graph_candidate_review=review,
        )
        return SolverCallResult(
            decision=decision,
            input_tokens=result.inputTokens,
            output_tokens=result.outputTokens,
            attempt_count=1 + result.retryCount,
        )

    def _solve_hypothesis_revision(
        self,
        context: SolverContext,
        profile: ModelCallProfile,
    ) -> SolverCallResult:
        """Cycle境界で、Hypothesisの現在版更新又は独立命題追加を行う。"""

        rendered = render_hypothesis_revision_model_call(context, profile)
        provider = getattr(self._client, "provider", None)
        started_at = monotonic()
        input_tokens = 0
        output_tokens = 0
        input_tokens_known = True
        output_tokens_known = True
        attempt_count = 0
        last_error: ModelProtocolError | ValidationError | None = None

        for repair_index in range(2):
            remaining_timeout = profile.timeout_sec - (monotonic() - started_at)
            if remaining_timeout <= 1:
                raise TimeoutError("hypothesis revision repair timed out")
            if self._diagnostics is not None:
                self._diagnostics.record_transport_input(
                    context=context,
                    profile=profile,
                    rendered=rendered,
                    repair_index=repair_index,
                    transport_stage="hypothesis_revision",
                    provider=provider,
                )
            try:
                result = self._client.generate_structured_json(
                    prompt=rendered.request,
                    schema=rendered.output_schema,
                    model=profile.model,
                    max_tokens=profile.max_output_tokens,
                    timeout_sec=max(1, round(remaining_timeout)),
                )
            except requests.Timeout as exc:
                if self._diagnostics is not None:
                    self._diagnostics.record_transport_timeout(
                        context=context,
                        repair_index=repair_index,
                        reason="hypothesis revision timed out",
                        transport_stage="hypothesis_revision",
                    )
                raise TimeoutError("hypothesis revision timed out") from exc

            attempt_count += 1 + result.retryCount
            if result.inputTokens is None:
                input_tokens_known = False
            else:
                input_tokens += result.inputTokens
            if result.outputTokens is None:
                output_tokens_known = False
            else:
                output_tokens += result.outputTokens
            revision: HypothesisRevisionDecision | None = None
            if result.validationError or result.payload is None:
                last_error = ModelProtocolError(
                    "hypothesis revision transport invalid: "
                    f"{result.validationError or 'empty'}"
                )
            else:
                try:
                    revision = HypothesisRevisionDecision.model_validate(result.payload)
                except ValidationError as exc:
                    last_error = exc
            if self._diagnostics is not None:
                self._diagnostics.record_transport_output(
                    context=context,
                    repair_index=repair_index,
                    payload=result.payload,
                    validation_error=(None if revision is not None else str(last_error)),
                    input_tokens=result.inputTokens,
                    output_tokens=result.outputTokens,
                    provider_retry_count=result.retryCount,
                    latency_ms=result.latencyMs,
                    transport_stage="hypothesis_revision",
                )
            if revision is not None:
                return SolverCallResult(
                    decision=SolverDecision(
                        next="continue",
                        decision_reason=revision.decision_reason,
                        start_next_cycle=True,
                    ),
                    hypothesis_revision=revision,
                    input_tokens=(input_tokens if input_tokens_known else None),
                    output_tokens=(output_tokens if output_tokens_known else None),
                    attempt_count=attempt_count,
                )
            if repair_index == 0 and last_error is not None:
                rendered = render_hypothesis_revision_transport_repair_model_call(
                    context,
                    base_call=rendered,
                    payload=result.payload,
                    error=last_error,
                )

        raise ModelProtocolError(
            f"hypothesis revision transport invalid: {last_error or 'unavailable'}"
        )

    def _solve_observation_only(
        self,
        context: SolverContext,
        profile: ModelCallProfile,
    ) -> SolverCallResult:
        """Persist new Tool observations before asking for another action."""

        grounding_ids = set(context.grounding_evidence_ids)
        has_new_grounding = any(
            grounding_ids.intersection(result.evidence_ids)
            for result in context.recent_tool_results
        )
        if context.recent_tool_results and not has_new_grounding:
            open_work_item_ids = {
                item.work_item_id
                for item in context.work_tree
                if item.state == "open"
            }
            next_focus_work_item_ids = tuple(
                dict.fromkeys(
                    request.work_item_id
                    for request in context.recent_tool_requests
                    if request.work_item_id in open_work_item_ids
                )
            )
            if not next_focus_work_item_ids:
                next_focus_work_item_ids = tuple(
                    item.work_item_id
                    for item in context.work_tree
                    if item.work_item_id in open_work_item_ids
                )
            return SolverCallResult(
                decision=SolverDecision(
                    next="continue",
                    decision_reason=(
                        "新しい回答根拠本文がないため、本文評価を省略した。"
                    ),
                    next_focus_work_item_ids=next_focus_work_item_ids,
                ),
                input_tokens=0,
                output_tokens=0,
                attempt_count=1,
            )
        started_at = monotonic()
        observation, input_tokens, output_tokens, attempt_count = (
            self._solve_observation_integrations(
                context,
                profile,
                started_at=started_at,
            )
        )
        observation = _derive_observation_work_item_updates(context, observation)
        closed_work_item_ids = {
            item.work_item_id
            for item in observation.update_work_items
            if item.state != "open"
        }
        next_focus_work_item_ids = tuple(
            dict.fromkeys(
                request.work_item_id
                for request in observation.tool_requests
                if request.work_item_id not in closed_work_item_ids
            )
        )
        if not next_focus_work_item_ids:
            open_work_item_ids = {
                item.work_item_id
                for item in context.work_tree
                if item.state == "open"
            }
            next_focus_work_item_ids = tuple(
                dict.fromkeys(
                    request.work_item_id
                    for request in context.recent_tool_requests
                    if request.work_item_id in open_work_item_ids
                    and request.work_item_id not in closed_work_item_ids
                )
            )
        return SolverCallResult(
            decision=SolverDecision(
                next="continue",
                decision_reason=observation.decision_reason,
                update=CaseUpdate(
                    update_work_items=observation.update_work_items,
                    update_hypotheses=observation.update_hypotheses,
                ),
                dependency_decisions=observation.dependency_decisions,
                next_focus_work_item_ids=next_focus_work_item_ids,
                tool_requests=observation.tool_requests,
            ),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            attempt_count=attempt_count,
        )

    def _solve_dependency_assessment(
        self,
        context: SolverContext,
        observation: ObservationIntegrationDecision,
        profile: ModelCallProfile,
        *,
        started_at: float,
        prior_input_tokens: int | None,
        prior_output_tokens: int | None,
    ) -> tuple[
        ObservationIntegrationDecision,
        int | None,
        int | None,
        int,
        bool,
    ]:
        """Evaluate lower-norm status after each new full-text observation."""

        if (
            context.required_dependency_kind is None
            or not context.required_dependency_work_item_ids
        ):
            return observation, 0, 0, 0, False

        decisions: list[DependencyDecision] = []
        input_tokens: list[int | None] = []
        output_tokens: list[int | None] = []
        attempt_count = 0
        projected_contexts = _dependency_work_item_contexts(context)
        if not projected_contexts:
            raise ModelProtocolError(
                "dependency assessment has no matching WorkItem context"
            )
        for projected_context in projected_contexts:
            projected_observation = _project_observation_decision(
                observation,
                context=projected_context,
            )
            dependency_call = render_dependency_assessment_model_call(
                projected_context,
                projected_observation,
                profile,
            )
            remaining_timeout = profile.timeout_sec - (monotonic() - started_at)
            if remaining_timeout <= 1:
                raise _cycle_close_checkpoint_timeout(
                    "dependency assessment time exhausted",
                    observation=observation,
                    completed_stage="observation_integration",
                    input_tokens=prior_input_tokens,
                    output_tokens=prior_output_tokens,
                )
            if self._diagnostics is not None:
                self._diagnostics.record_transport_input(
                    context=projected_context,
                    profile=profile,
                    rendered=dependency_call,
                    repair_index=0,
                    transport_stage="dependency_assessment",
                    provider=getattr(self._client, "provider", None),
                )
            try:
                dependency_result = self._client.generate_structured_json(
                    prompt=dependency_call.request,
                    schema=dependency_call.output_schema,
                    model=profile.model,
                    max_tokens=profile.max_output_tokens,
                    timeout_sec=max(1, round(remaining_timeout)),
                )
            except requests.Timeout as exc:
                raise _cycle_close_checkpoint_timeout(
                    "dependency assessment timed out",
                    observation=observation,
                    completed_stage="observation_integration",
                    input_tokens=prior_input_tokens,
                    output_tokens=prior_output_tokens,
                ) from exc
            dependency_error = dependency_result.validationError
            if dependency_result.payload is None and dependency_error is None:
                dependency_error = "empty"
            if self._diagnostics is not None:
                self._diagnostics.record_transport_output(
                    context=projected_context,
                    repair_index=0,
                    payload=dependency_result.payload,
                    validation_error=dependency_error,
                    input_tokens=dependency_result.inputTokens,
                    output_tokens=dependency_result.outputTokens,
                    provider_retry_count=dependency_result.retryCount,
                    latency_ms=dependency_result.latencyMs,
                    transport_stage="dependency_assessment",
                )
            if dependency_error is not None or dependency_result.payload is None:
                raise ModelProtocolError(
                    "dependency assessment transport invalid: "
                    f"{dependency_error}"
                )
            try:
                dependency = DependencyAssessmentDecision.model_validate(
                    _normalize_observation_integration_payload(
                        dependency_result.payload,
                        context=projected_context,
                    )
                )
                dependency = _downgrade_unproven_dependency_confirmations(
                    dependency,
                    article_id_by_evidence={
                        item.evidence_id: article_id
                        for item in projected_context.material_evidence
                        if item.evidence_id
                        in projected_context.grounding_evidence_ids
                        and isinstance(
                            article_id := item.metadata.get("articleId"),
                            str,
                        )
                    },
                )
            except ValidationError as exc:
                detail = exc.errors(include_url=False, include_input=False)[0]
                raise ModelProtocolError(
                    "dependency assessment contract invalid: "
                    f"{detail['msg']}"
                ) from exc
            decisions.extend(dependency.dependency_decisions)
            input_tokens.append(dependency_result.inputTokens)
            output_tokens.append(dependency_result.outputTokens)
            attempt_count += 1 + dependency_result.retryCount
        observation = observation.model_copy(
            update={"dependency_decisions": tuple(decisions)}
        )
        return (
            observation,
            _sum_optional_tokens(*input_tokens),
            _sum_optional_tokens(*output_tokens),
            attempt_count,
            True,
        )

    def _solve_cycle_close(
        self,
        context: SolverContext,
        profile: ModelCallProfile,
    ) -> SolverCallResult:
        """逐次統合済みの状態からCycle境界だけを判断する。"""

        started_at = monotonic()
        observation = ObservationIntegrationDecision(
            decision_reason="Cycle内の新規取得本文は逐次統合済み。",
        )
        completed_stage = "cycle_state_ready"

        transition_call = render_cycle_close_model_call(
            context,
            observation,
            profile,
        )
        remaining_timeout = profile.timeout_sec - (monotonic() - started_at)
        if remaining_timeout <= 1:
            raise _cycle_close_checkpoint_timeout(
                "cycle close transition time exhausted",
                observation=observation,
                completed_stage=completed_stage,
                input_tokens=0,
                output_tokens=0,
            )
        if self._diagnostics is not None:
            self._diagnostics.record_transport_input(
                context=context,
                profile=profile,
                rendered=transition_call,
                repair_index=0,
                transport_stage="cycle_close",
                provider=getattr(self._client, "provider", None),
            )
        try:
            transition_result = self._client.generate_structured_json(
                prompt=transition_call.request,
                schema=transition_call.output_schema,
                model=profile.model,
                max_tokens=profile.max_output_tokens,
                timeout_sec=max(1, round(remaining_timeout)),
            )
        except requests.Timeout as exc:
            if self._diagnostics is not None:
                self._diagnostics.record_transport_timeout(
                    context=context,
                    repair_index=0,
                    reason="cycle close transition timed out",
                    transport_stage="cycle_close",
                )
            raise _cycle_close_checkpoint_timeout(
                "cycle close transition timed out",
                observation=observation,
                completed_stage=completed_stage,
                input_tokens=0,
                output_tokens=0,
            ) from exc
        transition_error = transition_result.validationError
        if transition_result.payload is None and transition_error is None:
            transition_error = "empty"
        if self._diagnostics is not None:
            self._diagnostics.record_transport_output(
                context=context,
                repair_index=0,
                payload=transition_result.payload,
                validation_error=transition_error,
                input_tokens=transition_result.inputTokens,
                output_tokens=transition_result.outputTokens,
                provider_retry_count=transition_result.retryCount,
                latency_ms=transition_result.latencyMs,
                transport_stage="cycle_close",
            )
        if transition_error is not None or transition_result.payload is None:
            raise ModelProtocolError(
                f"cycle close transport invalid: {transition_error}"
            )
        try:
            transition = CycleCloseDecision.model_validate(
                transition_result.payload
            )
        except ValidationError as exc:
            detail = exc.errors(include_url=False, include_input=False)[0]
            raise ModelProtocolError(
                f"cycle close contract invalid: {detail['msg']}"
            ) from exc

        required_transition = _required_cycle_close_transition(
            context,
            observation,
        )
        if required_transition != "start_next_cycle":
            raise ModelProtocolError(
                "cycle close contract invalid: final answers require finalization"
            )

        decision = _normalize_cycle_close_decisions(
            context,
            observation,
            transition,
        )

        return SolverCallResult(
            decision=decision,
            input_tokens=transition_result.inputTokens,
            output_tokens=transition_result.outputTokens,
            attempt_count=1 + transition_result.retryCount,
        )

    def _solve_observation_integrations(
        self,
        context: SolverContext,
        profile: ModelCallProfile,
        *,
        started_at: float,
    ) -> tuple[ObservationIntegrationDecision, int | None, int | None, int]:
        """直近本文の評価と次行動を小さい統合単位で並列実行する。"""

        projected_contexts = _allocate_observation_fetch_capacity(
            _observation_work_item_contexts(
                _project_available_tools(context, profile.available_tool_names)
            )
        )
        if not projected_contexts:
            return ObservationIntegrationDecision(
                decision_reason="評価対象のopen WorkItemはない。"
            ), 0, 0, 0

        assignments = self._work_item_sessions.assign(projected_contexts)
        pending_action_contexts = (
            _pending_dependency_action_contexts(context)
            if profile.dependency_action_system_prompt is not None
            else ()
        )
        action_profile = profile.model_copy(update={"context_projection": "full"})

        def solve_one(
            assignment: tuple[WorkItemSession, SolverContext],
        ) -> tuple[ObservationIntegrationDecision, int | None, int | None, int]:
            session, projected_context = assignment
            provider = getattr(self._client, "provider", None)
            rendered = render_observation_integration_model_call(
                projected_context,
                profile,
                work_item_session=session,
                provider=provider,
            )
            if self._diagnostics is not None:
                self._diagnostics.record_transport_input(
                    context=projected_context,
                    profile=profile,
                    rendered=rendered,
                    repair_index=0,
                    transport_stage="observation_integration",
                    provider=provider,
                    work_item_session_id=session.session_id,
                    work_item_session_turn=session.turn,
                )
            remaining_timeout = profile.timeout_sec - (monotonic() - started_at)
            if remaining_timeout <= 1:
                raise TimeoutError("observation integration time exhausted")
            try:
                result = self._client.generate_structured_json(
                    prompt=rendered.request,
                    schema=rendered.output_schema,
                    model=profile.model,
                    max_tokens=profile.max_output_tokens,
                    timeout_sec=max(1, int(remaining_timeout)),
                )
            except requests.Timeout as exc:
                if self._diagnostics is not None:
                    self._diagnostics.record_transport_timeout(
                        context=projected_context,
                        repair_index=0,
                        reason="observation integration timed out",
                        transport_stage="observation_integration",
                        work_item_session_id=session.session_id,
                        work_item_session_turn=session.turn,
                    )
                raise TimeoutError("observation integration timed out") from exc
            error = result.validationError
            if result.payload is None and error is None:
                error = "empty"
            if self._diagnostics is not None:
                self._diagnostics.record_transport_output(
                    context=projected_context,
                    repair_index=0,
                    payload=result.payload,
                    validation_error=error,
                    input_tokens=result.inputTokens,
                    output_tokens=result.outputTokens,
                    provider_retry_count=result.retryCount,
                    latency_ms=result.latencyMs,
                    transport_stage="observation_integration",
                    work_item_session_id=session.session_id,
                    work_item_session_turn=session.turn,
                )
            if error is not None or result.payload is None:
                raise ModelProtocolError(
                    f"observation integration transport invalid: {error}"
                )
            try:
                normalized_observation = _normalize_observation_integration_payload(
                    result.payload,
                    context=projected_context,
                )
                _assign_tool_request_ids(normalized_observation, projected_context)
                observation = ObservationIntegrationDecision.model_validate(
                    normalized_observation
                )
            except ValidationError as exc:
                detail = exc.errors(include_url=False, include_input=False)[0]
                raise ModelProtocolError(
                    "observation integration contract invalid: "
                    f"{detail['msg']}"
                ) from exc
            return (
                observation,
                result.inputTokens,
                result.outputTokens,
                1 + result.retryCount,
            )

        def solve_pending_action(
            projected_context: SolverContext,
        ) -> tuple[ObservationIntegrationDecision, int | None, int | None, int]:
            action_result = self.solve(projected_context, action_profile)
            return (
                ObservationIntegrationDecision(
                    decision_reason=action_result.decision.decision_reason,
                    dependency_decisions=action_result.decision.dependency_decisions,
                    tool_requests=action_result.decision.tool_requests,
                ),
                action_result.input_tokens,
                action_result.output_tokens,
                action_result.attempt_count,
            )

        executor = ThreadPoolExecutor(
            max_workers=min(
                context.max_parallel_work_items,
                len(assignments) + len(pending_action_contexts),
            )
        )
        futures = {
            executor.submit(solve_one, assignment): ("observation", index)
            for index, assignment in enumerate(assignments)
        }
        futures.update(
            {
                executor.submit(solve_pending_action, projected_context): (
                    "action",
                    index,
                )
                for index, projected_context in enumerate(pending_action_contexts)
            }
        )
        remaining_timeout = max(
            0.0,
            profile.timeout_sec - (monotonic() - started_at),
        )
        completed, pending = wait(futures, timeout=remaining_timeout)
        for future in pending:
            future.cancel()
        executor.shutdown(wait=not pending, cancel_futures=True)

        indexed_observations = []
        indexed_actions = []
        observation_timed_out = any(
            futures[future][0] == "observation" for future in pending
        )
        protocol_errors: list[ModelProtocolError] = []
        for future in completed:
            try:
                kind, index = futures[future]
                target = (
                    indexed_observations
                    if kind == "observation"
                    else indexed_actions
                )
                target.append((index, future.result()))
            except TimeoutError:
                if futures[future][0] == "observation":
                    observation_timed_out = True
            except ModelProtocolError as exc:
                if futures[future][0] == "observation":
                    protocol_errors.append(exc)

        if protocol_errors:
            raise protocol_errors[0]

        observation_results = [
            result
            for _, result in sorted(
                indexed_observations,
                key=lambda item: item[0],
            )
        ]
        action_results = [
            result
            for _, result in sorted(indexed_actions, key=lambda item: item[0])
        ]
        results = [*observation_results, *action_results]
        if observation_timed_out:
            if not observation_results:
                raise TimeoutError("observation integration batch timed out")
            observation = _derive_observation_work_item_updates(
                context,
                _merge_observation_integrations(
                    [item[0] for item in observation_results]
                ),
            )
            raise _cycle_close_checkpoint_timeout(
                "observation integration batch partially timed out",
                observation=observation,
                completed_stage="work_item_sessions_partial",
                input_tokens=_sum_optional_tokens(
                    *(item[1] for item in observation_results)
                ),
                output_tokens=_sum_optional_tokens(
                    *(item[2] for item in observation_results)
                ),
            )
        return (
            _merge_observation_integrations([item[0] for item in results]),
            _sum_optional_tokens(*(item[1] for item in results)),
            _sum_optional_tokens(*(item[2] for item in results)),
            sum(item[3] for item in results),
        )

    def _solve_search_review(
        self,
        context: SolverContext,
        profile: ModelCallProfile,
    ) -> SolverCallResult:
        """候補理解、Hypothesis対応、本文取得選択を一度に実行する。"""

        provider = getattr(self._client, "provider", None)
        selection_call = render_search_selection_model_call(context, profile)
        if self._diagnostics is not None:
            self._diagnostics.record_transport_input(
                context=context,
                profile=profile,
                rendered=selection_call,
                repair_index=0,
                transport_stage="search_selection",
                provider=provider,
            )
        try:
            selection_result = self._client.generate_structured_json(
                prompt=selection_call.request,
                schema=selection_call.output_schema,
                model=profile.model,
                max_tokens=profile.max_output_tokens,
                timeout_sec=max(1, round(profile.timeout_sec)),
            )
        except requests.Timeout as exc:
            if self._diagnostics is not None:
                self._diagnostics.record_transport_timeout(
                    context=context,
                    repair_index=0,
                    reason="search selection timed out",
                    transport_stage="search_selection",
                )
            raise TimeoutError("search selection timed out") from exc
        selection_error = selection_result.validationError
        if selection_result.payload is None and selection_error is None:
            selection_error = "empty"
        if self._diagnostics is not None:
            self._diagnostics.record_transport_output(
                context=context,
                repair_index=0,
                payload=selection_result.payload,
                validation_error=selection_error,
                input_tokens=selection_result.inputTokens,
                output_tokens=selection_result.outputTokens,
                provider_retry_count=selection_result.retryCount,
                latency_ms=selection_result.latencyMs,
                transport_stage="search_selection",
            )
        if selection_error is not None or selection_result.payload is None:
            raise ModelProtocolError(
                f"search selection transport invalid: {selection_error}"
            )
        selection_payload = _normalize_search_selection_transport_payload(
            selection_result.payload,
            context,
        )
        try:
            _validate_selected_search_assessments(selection_payload, context)
            SearchReselectionDecision.model_validate(
                {
                    "selections": selection_payload.get("selections") or [],
                    "reason": selection_payload.get("reason"),
                }
            )
            _validate_search_reselection_payload(
                selection_payload,
                context,
            )
        except ValidationError as exc:
            detail = exc.errors(include_url=False, include_input=False)[0]
            raise ModelProtocolError(
                f"search selection contract invalid: {detail['msg']}"
            ) from exc
        combined_payload = {
            "search_request_ids": list(
                context.required_search_review_request_ids
            ),
            **selection_payload,
        }
        try:
            decision = SolverDecision.model_validate(
                _normalize_search_review_payload(combined_payload, context)
            )
        except ValidationError as exc:
            detail = exc.errors(include_url=False, include_input=False)[0]
            location = ".".join(str(item) for item in detail.get("loc", ()))
            raise ModelProtocolError(
                "search review violates schema: "
                f"{location or '<root>'}: {detail.get('msg')}"
            ) from exc

        return SolverCallResult(
            decision=decision,
            input_tokens=selection_result.inputTokens,
            output_tokens=selection_result.outputTokens,
            attempt_count=1 + selection_result.retryCount,
        )

    def review(
        self,
        context: ReviewerView,
        profile: ReviewerProfile,
    ) -> ReviewCallResult:
        rendered = render_reviewer_model_call(context, profile)
        prompt = rendered.request
        schema = rendered.output_schema
        if self._diagnostics is not None:
            self._diagnostics.record_reviewer_input(
                view=context,
                profile=profile,
                rendered=rendered,
                provider=getattr(self._client, "provider", None),
            )
        try:
            result = self._client.generate_structured_json(
                prompt=prompt,
                schema=schema,
                model=profile.model,
                max_tokens=profile.max_output_tokens,
                timeout_sec=max(1, round(profile.timeout_sec)),
            )
        except requests.Timeout as exc:
            if self._diagnostics is not None:
                self._diagnostics.record_reviewer_timeout(
                    view=context,
                    reason="model provider request timed out",
                )
            raise TimeoutError("model provider request timed out") from exc
        if result.validationError or result.payload is None:
            if self._diagnostics is not None:
                self._diagnostics.record_reviewer_output(
                    view=context,
                    payload=result.payload,
                    review=None,
                    validation_error=result.validationError or "empty",
                    input_tokens=result.inputTokens,
                    output_tokens=result.outputTokens,
                    provider_retry_count=result.retryCount,
                )
            raise ModelProtocolError(
                f"review structured output invalid: {result.validationError or 'empty'}"
            )
        try:
            review = ReviewResult.model_validate(result.payload)
        except ValidationError as exc:
            if self._diagnostics is not None:
                self._diagnostics.record_reviewer_output(
                    view=context,
                    payload=result.payload,
                    review=None,
                    validation_error=str(exc),
                    input_tokens=result.inputTokens,
                    output_tokens=result.outputTokens,
                    provider_retry_count=result.retryCount,
                )
            raise ModelProtocolError("review result violates schema") from exc
        if self._diagnostics is not None:
            self._diagnostics.record_reviewer_output(
                view=context,
                payload=result.payload,
                review=review,
                validation_error=None,
                input_tokens=result.inputTokens,
                output_tokens=result.outputTokens,
                provider_retry_count=result.retryCount,
            )
        return ReviewCallResult(
            review=review,
            input_tokens=result.inputTokens,
            output_tokens=result.outputTokens,
            attempt_count=1 + result.retryCount,
        )


def render_solver_model_call(
    context: SolverContext,
    profile: ModelCallProfile,
    *,
    provider: str | None,
    stage: str = "solver",
) -> RenderedModelCall:
    """Providerへ送るSolver呼出しとレビュー成果物を同時に作る。"""

    if profile.context_projection == "research_hypothesis":
        context = _project_next_hypothesis_work_item(context)
    projected_context = _project_available_tools(
        context,
        profile.available_tool_names,
    )
    if profile.context_projection == "finalization":
        projected_context = _project_finalization_context(projected_context)
    if profile.context_projection == "cycle_close":
        return render_observation_integration_model_call(
            projected_context,
            profile,
            provider=provider,
        )
    if profile.context_projection == "hypothesis_revision":
        return render_hypothesis_revision_model_call(projected_context, profile)
    if profile.context_projection == "graph_review":
        return render_graph_review_model_call(projected_context, profile)
    if profile.context_projection in {
        "research_decomposition",
        "research_hypothesis",
        "research_search",
    }:
        return _render_staged_research_model_call(
            projected_context,
            profile,
            stage=stage,
        )
    dependency_action = _is_dependency_action_call(projected_context, profile)
    if dependency_action:
        projected_context = _project_dependency_action_context(
            projected_context
        )
    initial_research = profile.context_projection == "initial_research"
    output_schema = (
        _strip_runtime_id_enums(
            _initial_research_transport_schema(projected_context)
        )
        if initial_research
        else _finalization_transport_schema(projected_context)
        if profile.context_projection == "finalization"
        else _solver_anthropic_json_transport_schema(projected_context)
        if provider == "anthropic"
        else _solver_common_transport_schema(projected_context)
    )
    return _render_solver_model_call(
        context,
        (
            profile.dependency_action_system_prompt
            if dependency_action
            else profile.system_prompt
        ),
        completion_check_prompt=(
            profile.dependency_action_completion_check_prompt
            if dependency_action
            else profile.completion_check_prompt
        ),
        output_schema=(
            _dependency_action_transport_schema(
                projected_context,
                json_transport=provider == "anthropic",
            )
            if dependency_action
            else output_schema
        ),
        input_payload=_solver_context_payload(
            projected_context,
            projection=profile.context_projection,
        ),
        minimal_contract=(
            _DEPENDENCY_ACTION_CONTRACT
            if dependency_action
            else _INITIAL_RESEARCH_SOLVER_CONTRACT
            if initial_research
            else _MINIMAL_SOLVER_CONTRACT
        ),
        normalized_schema=(
            DependencyActionDecision.model_json_schema()
            if dependency_action
            else SolverDecision.model_json_schema()
        ),
        stage=stage,
    )


def _is_dependency_action_call(
    context: SolverContext,
    profile: ModelCallProfile,
) -> bool:
    return bool(
        context.required_dependency_work_item_ids
        and profile.context_projection in {"full", "integration"}
        and profile.dependency_action_system_prompt is not None
    )


def render_graph_review_model_call(
    context: SolverContext,
    profile: ModelCallProfile,
) -> RenderedModelCall:
    """Graph候補選別に必要な指示・入力・出力だけを組み立てる。"""

    input_payload = _solver_context_payload(context, projection="graph_review")
    instructions = (
        f"{profile.system_prompt}\n\n"
        f"{RUNTIME_INPUT_MARKER}"
        f"{_post_context_completion_check(profile.completion_check_prompt)}"
    )
    rendered = build_rendered_model_call(
        stage="graph_review",
        instructions=instructions,
        input_tag="graph_review_input",
        input_payload=input_payload,
        output_schema=_graph_review_transport_schema(context),
        normalized_schema=GraphCandidateReview.model_json_schema(),
    )
    _ensure_solver_prompt_capacity(rendered.request, context.max_solver_input_chars)
    return rendered


def _render_staged_research_model_call(
    context: SolverContext,
    profile: ModelCallProfile,
    *,
    stage: str,
) -> RenderedModelCall:
    """初回Researchの各単一責務Stepへ、必要な入力と出力だけを渡す。"""

    projection = profile.context_projection
    input_payload = _solver_context_payload(context, projection=projection)
    output_schema = _staged_research_transport_schema(context, projection)
    collection_item_fields = tuple(
        (field_name, tuple(value[0]))
        for field_name, value in input_payload.items()
        if isinstance(value, list)
        and value
        and isinstance(value[0], dict)
    )
    instructions = (
        f"{profile.system_prompt}\n\n"
        f"{render_research_step_input_glossary(tuple(input_payload), collection_item_fields)}\n\n"
        f"{RUNTIME_INPUT_MARKER}"
        f"{_post_context_completion_check(profile.completion_check_prompt)}"
    )
    rendered = build_rendered_model_call(
        stage=f"{stage}_{projection}",
        instructions=instructions,
        input_tag="research_step_input",
        input_payload=input_payload,
        output_schema=output_schema,
        normalized_schema=SolverDecision.model_json_schema(),
    )
    _ensure_solver_prompt_capacity(rendered.request, context.max_solver_input_chars)
    return rendered


def _render_solver_model_call(
    context: SolverContext,
    system_prompt: str,
    *,
    completion_check_prompt: str | None = None,
    output_schema: dict[str, Any] | None = None,
    input_payload: dict[str, Any] | None = None,
    minimal_contract: str = "",
    normalized_schema: dict[str, Any] | None = None,
    stage: str = "solver",
) -> RenderedModelCall:
    if output_schema is None:
        output_schema = _solver_common_transport_schema(context)
    repair_instructions = _contract_repair_catalog(context)
    if input_payload is None:
        input_payload = context.model_dump(mode="json")
    else:
        input_payload = deepcopy(input_payload)
    if "decision_json" in output_schema.get("properties", {}):
        decision_field_names = tuple(
            _solver_common_transport_schema(context)["properties"]
        )
    else:
        decision_field_names = tuple(
            name
            for name in output_schema.get("properties", {})
            if name in SolverDecision.model_fields
        )
    instructions = (
        f"{system_prompt}\n\n"
        f"{render_solver_contract_glossary(tuple(input_payload), decision_field_names)}\n\n"
        f"{minimal_contract or _MINIMAL_SOLVER_CONTRACT}\n\n"
        f"{repair_instructions}"
        f"{_solver_transport_instruction(output_schema)}"
        f"{RUNTIME_INPUT_MARKER}"
        f"{_post_context_completion_check(completion_check_prompt)}"
    )
    rendered = build_rendered_model_call(
        stage=(
            f"{stage}_contract_repair"
            if context.contract_feedback is not None
            else stage
        ),
        instructions=instructions,
        input_tag="solver_context",
        input_payload=input_payload,
        output_schema=output_schema,
        normalized_schema=(
            normalized_schema or SolverDecision.model_json_schema()
        ),
        prompt_assets=_solver_prompt_assets(context),
    )
    _ensure_solver_prompt_capacity(rendered.request, context.max_solver_input_chars)
    return rendered


def _project_available_tools(
    context: SolverContext,
    available_tool_names: tuple[str, ...] | None,
) -> SolverContext:
    """Profileが指定したToolだけを、意味選別せずProviderへ投影する。"""

    if available_tool_names is None:
        return context
    requested = set(available_tool_names)
    available_tools = tuple(
        definition
        for definition in context.available_tools
        if definition.name in requested
    )
    found = {definition.name for definition in available_tools}
    missing = requested - found
    if missing:
        raise ValueError(f"profile references unavailable tools: {sorted(missing)}")
    return context.model_copy(update={"available_tools": available_tools})


def _project_finalization_context(context: SolverContext) -> SolverContext:
    """最終回答へ、確認済み命題の根拠本文と未確認範囲を投影する。"""

    resolved_work_item_ids = {
        item.work_item_id
        for item in context.work_tree
        if item.state == "resolved"
    }
    visible_hypothesis_ids = {
        item.hypothesis_id for item in context.hypotheses
    }
    citable_basis_evidence_ids = {
        evidence_id
        for hypothesis in context.hypotheses
        for evidence_id in hypothesis.evidence_ids
    }
    citable_basis_evidence_ids.update(
        evidence_id
        for decision in context.dependency_decisions
        if decision.status == "resolved"
        for evidence_id in decision.basis_evidence_ids
    )
    visible_grounding_ids = tuple(
        evidence_id
        for evidence_id in context.grounding_evidence_ids
        if evidence_id in citable_basis_evidence_ids
        and any(
            item.evidence_id == evidence_id
            for item in context.material_evidence
        )
    )
    visible_grounding_id_set = set(visible_grounding_ids)
    visible_hypotheses = tuple(
        item
        for item in context.hypotheses
        if item.hypothesis_id in visible_hypothesis_ids
    )
    visible_hypothesis_ids_by_work_item: dict[str, tuple[str, ...]] = {
        work_item.work_item_id: tuple(
            item.hypothesis_id
            for item in visible_hypotheses
            if item.work_item_id == work_item.work_item_id
        )
        for work_item in context.work_tree
    }
    return context.model_copy(
        update={
            "available_tools": (),
            "work_tree": tuple(
                item.model_copy(
                    update={
                        "hypothesis_ids": visible_hypothesis_ids_by_work_item[
                            item.work_item_id
                        ],
                        "evidence_count": len(
                            {
                                evidence_id
                                for hypothesis in visible_hypotheses
                                if hypothesis.work_item_id == item.work_item_id
                                for evidence_id in hypothesis.evidence_ids
                            }
                        ),
                    }
                )
                for item in context.work_tree
            ),
            "hypotheses": visible_hypotheses,
            "grounding_evidence_ids": visible_grounding_ids,
            "navigation_evidence_ids": (),
            "evidence_manifest": tuple(
                item
                for item in context.evidence_manifest
                if item.evidence_id in visible_grounding_id_set
            ),
            "material_evidence": tuple(
                item
                for item in context.material_evidence
                if item.evidence_id in visible_grounding_id_set
            ),
            "omitted_evidence_ids": (),
            "fetchable_article_ids": (),
            "search_candidates": (),
            "evidence_hypothesis_candidates": (),
            "required_search_review_request_ids": (),
            "dependency_decisions": tuple(
                item
                for item in context.dependency_decisions
                if item.work_item_id in resolved_work_item_ids
                or item.status == "needs_action"
            ),
        }
    )


def _dependency_action_blocked_tool_names(
    context: SolverContext,
) -> frozenset[str]:
    """既存のLLM判断と実行履歴から、今回選べないTool種類を返す。"""

    blocked = {
        request.tool_name
        for request in (
            context.action_feedback.rejected_tool_requests
            if context.action_feedback is not None
            else ()
        )
    }
    if _dependency_action_has_scoped_fetch_candidate(context):
        blocked.add("legal_search")
    elif _dependency_action_requires_graph(context):
        blocked.add("legal_search")
    return frozenset(blocked)


def _dependency_action_has_scoped_fetch_candidate(
    context: SolverContext,
) -> bool:
    """対象作業の探索で得た未取得候補がModel入力にあるか。"""

    required_ids = set(context.required_dependency_work_item_ids)
    required_hypothesis_ids = {
        hypothesis.hypothesis_id
        for hypothesis in context.hypotheses
        if hypothesis.work_item_id in required_ids
    }
    fetchable_ids = set(context.fetchable_article_ids)
    return any(
        candidate.article_id in fetchable_ids
        and _search_candidate_matches_scope(
            candidate,
            work_item_ids=required_ids,
            hypothesis_ids=required_hypothesis_ids,
        )
        for candidate in context.search_candidates
    )


def _search_candidate_matches_scope(
    candidate: SearchCandidateArticle,
    *,
    work_item_ids: set[str],
    hypothesis_ids: set[str],
) -> bool:
    """候補の採用先または発見元が現在の作業範囲と重なるか。"""

    return bool(
        hypothesis_ids.intersection(candidate.matched_hypothesis_ids)
        or hypothesis_ids.intersection(candidate.discovery_hypothesis_ids)
        or work_item_ids.intersection(candidate.discovery_work_item_ids)
    )


def _project_dependency_action_context(
    context: SolverContext,
) -> SolverContext:
    """候補評価で対象Hypothesisに対応した未取得Articleだけを提示する。"""

    required_work_item_ids = set(context.required_dependency_work_item_ids)
    required_hypothesis_ids = {
        item.hypothesis_id
        for item in context.hypotheses
        if item.work_item_id in required_work_item_ids
    }
    candidates = tuple(
        candidate
        for candidate in context.search_candidates
        if _search_candidate_matches_scope(
            candidate,
            work_item_ids=required_work_item_ids,
            hypothesis_ids=required_hypothesis_ids,
        )
    )
    candidate_ids = {candidate.article_id for candidate in candidates}
    return context.model_copy(
        update={
            "search_candidates": candidates,
            "fetchable_article_ids": tuple(
                article_id
                for article_id in context.fetchable_article_ids
                if article_id in candidate_ids
            ),
        }
    )


def _dependency_action_has_unexplored_graph_root(
    context: SolverContext,
) -> bool:
    """Graphをまだ探索していない下位規範WorkItemがあるか。"""

    required_ids = set(context.required_dependency_work_item_ids)
    evidence_by_id = {
        item.evidence_id: item for item in context.material_evidence
    }
    completed_ids = {
        item.work_item_id
        for item in context.completed_graph_searches
        if item.work_item_id in required_ids
    }
    return any(
        decision.work_item_id not in completed_ids
        and any(
            (evidence := evidence_by_id.get(evidence_id)) is not None
            and isinstance(evidence.metadata.get("articleId"), str)
            for evidence_id in decision.basis_evidence_ids
        )
        for decision in context.dependency_decisions
        if decision.work_item_id in required_ids
        and decision.status == "needs_action"
    )


def _dependency_action_requires_graph(
    context: SolverContext,
) -> bool:
    return (
        not _dependency_action_has_scoped_fetch_candidate(context)
        and _dependency_action_has_unexplored_graph_root(context)
    )


def _dependency_action_available_tools(
    context: SolverContext,
) -> tuple[ToolDefinition, ...]:
    """現在の下位規範Actionで実行可能なTool契約だけを返す。"""

    blocked = _dependency_action_blocked_tool_names(context)
    return tuple(
        definition
        for definition in context.available_tools
        if definition.name not in blocked
    )


def _dependency_action_must_start_next_cycle(
    context: SolverContext,
) -> bool:
    """棄却後に実行可能な別Actionがなければ次Cycleへ進める。"""

    return bool(
        context.action_feedback is not None
        and context.can_start_next_cycle
        and not _dependency_action_available_tools(context)
    )


def _canonicalize_dependency_graph_roots(
    payload: dict[str, Any],
    context: SolverContext,
) -> dict[str, Any]:
    """Paragraph・Item Evidence参照を所属Article IDへ戻す。"""

    evidence_to_article = {
        evidence.evidence_id: article_id
        for evidence in context.material_evidence
        if isinstance(
            article_id := evidence.metadata.get("articleId"),
            str,
        )
        and article_id
    }
    requests = payload.get("tool_requests")
    if not evidence_to_article or not isinstance(requests, list):
        return payload

    normalized_requests = []
    for request in requests:
        if not isinstance(request, dict) or request.get("tool_name") != (
            "legal_graph_neighbors"
        ):
            normalized_requests.append(request)
            continue
        arguments = request.get("arguments")
        article_ids = (
            arguments.get("article_ids")
            if isinstance(arguments, dict)
            else None
        )
        if not isinstance(article_ids, list):
            normalized_requests.append(request)
            continue
        normalized_requests.append(
            {
                **request,
                "arguments": {
                    **arguments,
                    "article_ids": [
                        evidence_to_article.get(article_id, article_id)
                        for article_id in article_ids
                    ],
                },
            }
        )
    return {**payload, "tool_requests": normalized_requests}


def _solver_context_payload(
    context: SolverContext,
    *,
    projection: str,
) -> dict[str, Any]:
    """CaseStoreを変えず、用途に無関係な実行値をModel入力から除く。"""

    payload = context.model_dump(mode="json")
    if context.graph_review_batch.candidates:
        payload["graph_review_selection_limit"] = (
            context.graph_review_selection_limit
        )
    if projection in {"full", "integration"} and context.required_dependency_work_item_ids:
        # A prior LLM decision already established which WorkItems need a
        # lower-norm action. Keep this planning step focused; other open work
        # remains in CaseStore and is projected again after these actions.
        active_work_item_ids = set(context.required_dependency_work_item_ids)
        payload["work_tree"] = [
            item
            for item in payload["work_tree"]
            if item["work_item_id"] in active_work_item_ids
        ]
        payload["hypotheses"] = [
            item
            for item in payload["hypotheses"]
            if item["work_item_id"] in active_work_item_ids
        ]
        payload["focus_work_items"] = list(payload["work_tree"])

        active_hypothesis_ids = {
            item["hypothesis_id"] for item in payload["hypotheses"]
        }
        active_candidates = [
            item
            for item in payload["search_candidates"]
            if (
                active_hypothesis_ids.intersection(
                    item["matched_hypothesis_ids"]
                )
                or active_hypothesis_ids.intersection(
                    item["discovery_hypothesis_ids"]
                )
                or active_work_item_ids.intersection(
                    item["discovery_work_item_ids"]
                )
            )
        ]
        active_candidate_ids = {
            item["article_id"] for item in active_candidates
        }
        payload["search_candidates"] = active_candidates
        payload["fetchable_article_ids"] = [
            article_id
            for article_id in payload["fetchable_article_ids"]
            if article_id in active_candidate_ids
        ]

        dependency_basis_ids = {
            evidence_id
            for item in payload["dependency_decisions"]
            if item["work_item_id"] in active_work_item_ids
            and item["status"] == "needs_action"
            for evidence_id in item["basis_evidence_ids"]
        }
        payload["grounding_evidence_ids"] = [
            evidence_id
            for evidence_id in payload["grounding_evidence_ids"]
            if evidence_id in dependency_basis_ids
        ]
        payload["material_evidence"] = [
            item
            for item in payload["material_evidence"]
            if item["evidence_id"] in dependency_basis_ids
        ]
        payload["evidence_manifest"] = [
            item
            for item in payload["evidence_manifest"]
            if item["evidence_id"] in dependency_basis_ids
        ]
        payload["omitted_evidence_ids"] = []
        payload["recent_tool_requests"] = [
            item
            for item in payload["recent_tool_requests"]
            if item["work_item_id"] in active_work_item_ids
        ]
        active_request_ids = {
            item["request_id"] for item in payload["recent_tool_requests"]
        }
        payload["recent_tool_results"] = [
            item
            for item in payload["recent_tool_results"]
            if item["request_id"] in active_request_ids
        ]
        payload["completed_legal_searches"] = [
            item
            for item in payload["completed_legal_searches"]
            if item["work_item_id"] in active_work_item_ids
        ]
        payload["completed_graph_searches"] = [
            item
            for item in payload["completed_graph_searches"]
            if item["work_item_id"] in active_work_item_ids
        ]
        payload["graph_review_ledger"] = [
            item
            for item in payload["graph_review_ledger"]
            if item["work_item_id"] in active_work_item_ids
        ]
        payload["dependency_decisions"] = [
            item
            for item in payload["dependency_decisions"]
            if item["work_item_id"] in active_work_item_ids
        ]
        # Dependency Actionは既存の意味判断を変えず、次のToolを1件選ぶ処理。
        # 全Solver状態を送るとaction_feedbackと候補が埋もれるため、この判断に
        # 必要なread modelだけを投影する。
        dependency_action_fields = (
            "action_feedback",
            "question",
            "can_start_next_cycle",
            "work_tree",
            "hypotheses",
            "dependency_decisions",
            "required_dependency_kind",
            "required_dependency_work_item_ids",
            "material_evidence",
            "omitted_evidence_ids",
            "fetchable_article_ids",
            "search_candidates",
            "graph_review_ledger",
            "completed_legal_searches",
            "completed_graph_searches",
            "available_tools",
            "remaining_fetch_capacity",
            "remaining_fetch_capacity_by_work_item",
        )
        payload["remaining_fetch_capacity_by_work_item"] = {
            work_item_id: context.remaining_fetch_capacity_by_work_item.get(
                work_item_id,
                context.remaining_fetch_capacity,
            )
            for work_item_id in active_work_item_ids
        }
        available_action_tools = list(
            _dependency_action_available_tools(context)
        )
        if not payload["fetchable_article_ids"]:
            available_action_tools = [
                item
                for item in available_action_tools
                if item.name != "fetch_articles"
            ]
        if not payload["omitted_evidence_ids"]:
            available_action_tools = [
                item
                for item in available_action_tools
                if item.name != "load_evidence"
            ]
        payload["available_tools"] = [
            item.model_dump(mode="json")
            for item in available_action_tools
        ]
        return {name: payload[name] for name in dependency_action_fields}
    if projection == "finalization":
        verified_evidence_ids = set(
            _verified_answer_evidence_ids(
                context,
                ObservationIntegrationDecision(
                    decision_reason="逐次統合済み状態から引用可能な根拠を投影する。"
                ),
            )
        )
        payload["material_evidence"] = [
            {
                "evidence_id": item["evidence_id"],
                "content": item["content"],
                "title": item["title"],
            }
            for item in payload["material_evidence"]
            if item["evidence_id"] in verified_evidence_ids
        ]
        material_evidence_ids = {
            item["evidence_id"] for item in payload["material_evidence"]
        }
        payload["grounding_evidence_ids"] = [
            evidence_id
            for evidence_id in payload["grounding_evidence_ids"]
            if evidence_id in material_evidence_ids
        ]
        payload["graph_review_ledger"] = [
            item
            for item in payload["graph_review_ledger"]
            if item["review_status"] == "relevant_deferred"
            and item["content_status"] in {"not_requested", "failed", "timeout"}
            and item["deferred_resolution_action"] != "no_longer_needed"
        ]
        payload["resolved_work_item_ids"] = [
            item["work_item_id"]
            for item in payload["work_tree"]
            if item["state"] == "resolved"
        ]
        payload["open_work_item_ids"] = [
            item["work_item_id"]
            for item in payload["work_tree"]
            if item["state"] == "open"
        ]
        payload["unresolved_hypothesis_ids"] = [
            item["hypothesis_id"]
            for item in payload["hypotheses"]
            if item["judgment"] == "unresolved"
        ]
        payload["verified_hypothesis_ids"] = [
            item["hypothesis_id"]
            for item in payload["hypotheses"]
            if item["judgment"] in {"supported", "contradicted"}
        ]
        payload["required_answer_evidence_ids"] = list(
            _required_answer_evidence_ids(
                context,
                ObservationIntegrationDecision(
                    decision_reason="逐次統合済み状態から根拠IDを投影する。"
                ),
            )
        )
        finalization_fields = (
            "question",
            "non_work_item_requirements",
            "finalize_only",
            "work_tree",
            "hypotheses",
            "grounding_evidence_ids",
            "material_evidence",
            "graph_review_ledger",
            "contract_feedback",
            "resolved_work_item_ids",
            "open_work_item_ids",
            "unresolved_hypothesis_ids",
            "verified_hypothesis_ids",
            "required_answer_evidence_ids",
        )
        finalization_payload = {
            name: payload[name] for name in finalization_fields
        }
        if context.answer_options:
            finalization_payload["answer_options"] = payload["answer_options"]
        return finalization_payload
    if projection == "integration":
        payload["remaining_fetch_capacity_by_work_item"] = dict(
            context.remaining_fetch_capacity_by_work_item
        )
        follow_up_hypothesis_ids = {
            item.hypothesis_id
            for item in context.hypotheses
            if item.requires_follow_up
        }
        follow_up_work_item_ids = {
            item.work_item_id
            for item in context.hypotheses
            if item.hypothesis_id in follow_up_hypothesis_ids
        }
        active_candidates = [
            item
            for item in payload["search_candidates"]
            if (
                follow_up_hypothesis_ids.intersection(
                    item["matched_hypothesis_ids"]
                )
                or follow_up_hypothesis_ids.intersection(
                    item["discovery_hypothesis_ids"]
                )
                or follow_up_work_item_ids.intersection(
                    item["discovery_work_item_ids"]
                )
            )
        ]
        active_candidate_ids = {
            item["article_id"] for item in active_candidates
        }
        # Observation Integrationで本文の意味は状態へ反映済みである。次行動・完了判断には
        # 本文を残す一方、同じ出典情報を重複するmanifestと、解決済み論点の候補を送らない。
        grounding_evidence_ids = set(payload["grounding_evidence_ids"])
        payload["material_evidence"] = [
            {
                "evidence_id": item["evidence_id"],
                "content": item["content"],
                "title": item["title"],
            }
            for item in payload["material_evidence"]
            if item["evidence_id"] in grounding_evidence_ids
        ]
        material_evidence_ids = {
            item["evidence_id"] for item in payload["material_evidence"]
        }
        payload["grounding_evidence_ids"] = [
            evidence_id
            for evidence_id in payload["grounding_evidence_ids"]
            if evidence_id in material_evidence_ids
        ]
        payload["search_candidates"] = active_candidates
        payload["fetchable_article_ids"] = [
            article_id
            for article_id in payload["fetchable_article_ids"]
            if article_id in active_candidate_ids
        ]
        integration_fields = (
            "question",
            "answer_options",
            "non_work_item_requirements",
            "research_cycle_count",
            "remaining_research_cycles",
            "remaining_wall_time_sec",
            "min_next_cycle_budget_sec",
            "can_start_next_cycle",
            "max_tool_requests_per_step",
            "remaining_fetch_capacity",
            "remaining_fetch_capacity_by_work_item",
            "cycle_budget_reached",
            "cycle_close_required",
            "finalize_only",
            "available_tools",
            "grounding_evidence_ids",
            "material_evidence",
            "omitted_evidence_ids",
            "fetchable_article_ids",
            "search_candidates",
            "work_tree",
            "hypotheses",
            "focus_work_items",
            "completed_legal_searches",
            "completed_graph_searches",
            "graph_fetch_completed_hypothesis_ids_this_cycle",
            "graph_review_ledger",
            "dependency_decisions",
            "required_dependency_kind",
            "required_dependency_work_item_ids",
            "reviewer_findings",
            "contract_feedback",
            "action_feedback",
        )
        return {name: payload[name] for name in integration_fields}
    if projection == "full":
        return payload
    if projection == "graph_review":
        graph_work_item_ids = {
            item.work_item_id for item in context.graph_review_batch.candidates
        }
        graph_hypothesis_ids = {
            item.hypothesis_id
            for item in context.graph_review_batch.candidates
            if item.hypothesis_id is not None
        }
        return {
            "case_id": payload["case_id"],
            "question": payload["question"],
            "work_tree": [
                item
                for item in payload["work_tree"]
                if item["work_item_id"] in graph_work_item_ids
            ],
            "hypotheses": [
                item
                for item in payload["hypotheses"]
                if item["hypothesis_id"] in graph_hypothesis_ids
            ],
            "graph_review_batch": payload["graph_review_batch"],
            "required_graph_review_request_ids": payload[
                "required_graph_review_request_ids"
            ],
            "graph_review_selection_limit": (
                context.graph_review_selection_limit
            ),
        }
    follow_up_hypotheses = tuple(
        item for item in context.hypotheses if item.requires_follow_up
    )
    follow_up_work_item_ids = {
        item.work_item_id for item in follow_up_hypotheses
    }
    research_work_items = tuple(context.work_tree)
    if projection == "research_search":
        research_work_items = tuple(
            item
            for item in context.work_tree
            if item.state == "open"
            and item.work_item_id in follow_up_work_item_ids
        )
    research_work_item_ids = {
        item.work_item_id for item in research_work_items
    }
    work_item_by_id = {item.work_item_id: item for item in context.work_tree}
    research_input = ResearchStepInput(
        question=context.question,
        answer_options=context.answer_options,
        work_items=tuple(
            ResearchStepWorkItem(
                work_item_id=item.work_item_id,
                question=item.question,
                action_actor=item.action_actor,
            )
            for item in research_work_items
        ),
        non_work_item_requirements=context.non_work_item_requirements,
        hypotheses=tuple(
            ResearchStepHypothesis(
                hypothesis_id=item.hypothesis_id,
                work_item_id=item.work_item_id,
                statement=item.statement,
                action_actor=(
                    work_item_by_id[item.work_item_id].action_actor
                    if item.work_item_id in work_item_by_id
                    else None
                ),
                gaps=item.gaps,
            )
            for item in follow_up_hypotheses
            if item.work_item_id in research_work_item_ids
        ),
        available_tools=context.available_tools,
        max_tool_requests_per_step=context.max_tool_requests_per_step,
    )
    if projection == "research_decomposition":
        research_payload = research_input.model_dump(
            mode="json",
            include={"question"},
        )
        if research_input.answer_options:
            research_payload["answer_options"] = [
                item.model_dump(mode="json")
                for item in research_input.answer_options
            ]
        return research_payload
    if projection == "research_hypothesis":
        research_payload = {
            "work_items": [
                {
                    "work_item_id": item.work_item_id,
                    "question": item.question,
                    "action_actor": item.action_actor,
                }
                for item in research_input.work_items
            ],
        }
        if research_input.answer_options:
            research_payload["answer_options"] = [
                item.model_dump(mode="json")
                for item in research_input.answer_options
            ]
        return research_payload
    if projection == "research_search":
        research_payload = research_input.model_dump(
            mode="json",
            include={
                "question",
                "work_items",
                "hypotheses",
                "available_tools",
                "max_tool_requests_per_step",
            },
        )
        if research_input.answer_options:
            research_payload["answer_options"] = [
                item.model_dump(mode="json")
                for item in research_input.answer_options
            ]
        return research_payload
    if projection != "initial_research":
        raise ValueError(f"unknown solver context projection: {projection}")
    included_fields = (
        "case_id",
        "question",
        "answer_options",
        "research_cycle_count",
        "remaining_research_cycles",
        "max_tool_requests_per_step",
        "work_tree",
        "hypotheses",
        "available_tools",
        "contract_feedback",
        "action_feedback",
    )
    return {name: payload[name] for name in included_fields}


def _staged_research_transport_schema(
    context: SolverContext,
    projection: str,
) -> dict[str, Any]:
    """単一責務StepごとのProvider向け出力契約。"""

    if projection == "research_decomposition":
        work_item = _strict_object(
            {
                "question": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 1000,
                    "description": (
                        "法令の解釈又は適用について個別に結論を出す"
                        "1つの法的論点を、自然言語の問いで表したもの。"
                        "質問が上位概念だけを示す場合は、質問にない構成要素を"
                        "予想して列挙せず、同じ抽象度を保つ。"
                    ),
                },
                "action_actor": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 600,
                    "description": (
                        "質問から行為者が明確な場合はその役割。"
                        "明確でない場合は不明。"
                    ),
                },
            }
        )
        return _strict_object(
            {
                "work_items": {
                    "type": "array",
                    "items": work_item,
                    "minItems": 1,
                    "description": "質問に含まれる法的論点の一覧。",
                },
                "non_work_item_requirements": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "maxItems": 24,
                    "description": (
                        "法令の内容自体ではなく、回答の示し方を指定する要求。"
                        "根拠条文・出典・引用の提示や、文体・長さ・表等の"
                        "出力形式だけを含む。「説明」「列挙」「示す」等の表現でも、"
                        "法令本文を調べて答える内容はwork_itemsに入れる。"
                    ),
                },
            }
        )
    work_item_ids = tuple(item.work_item_id for item in context.work_tree)
    if projection == "research_search":
        follow_up_work_item_ids = {
            item.work_item_id
            for item in context.hypotheses
            if item.requires_follow_up
        }
        work_item_ids = tuple(
            item.work_item_id
            for item in context.work_tree
            if item.state == "open"
            and item.work_item_id in follow_up_work_item_ids
        )
    if projection == "research_hypothesis":
        hypothesis = _strict_object(
            {
                "work_item_id": _enum_string(work_item_ids),
                "statement": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                    "description": (
                        "WorkItemの範囲内で、法令本文の一つの規定内容により個別に"
                        "支持又は否定できる1つの法的命題。WorkItemから分からない"
                        "分類、要件、手続、数値又は条文番号を予想して追加せず、"
                        "暫定的な法的効果を1つの文で示す。"
                    ),
                },
                "gaps": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "maxItems": 1,
                    "description": (
                        "statementを判定するため、WorkItemの範囲内で法令本文による"
                        "確認が必要な事項。未知の分類、要件又は手続の候補を予想して"
                        "追加せず、WorkItemで問われた未確認内容を1項目にまとめる。"
                        "未知の答え全体は予想した内訳、例又は括弧書きへ分けない。前提、"
                        "例外又は周辺事項へ広げない。該当しなければ空にする。"
                    ),
                },
            }
        )
        return _strict_object(
            {
                "hypotheses": {
                    "type": "array",
                    "items": hypothesis,
                    "minItems": len(work_item_ids),
                    "description": (
                        "提示されたWorkItemを検証するHypothesis。WorkItem自体に"
                        "独立した確認事項が複数ある場合だけ分ける。"
                    ),
                }
            }
        )
    if projection == "research_search":
        hypothesis_ids = tuple(
            item.hypothesis_id
            for item in context.hypotheses
            if item.requires_follow_up
        )
        search_request = _strict_object(
            {
                "work_item_id": _enum_string(work_item_ids),
                "hypothesis_ids": {
                    "type": "array",
                    "items": _enum_string(hypothesis_ids),
                    "minItems": 1,
                    "maxItems": min(8, max(1, len(hypothesis_ids))),
                    "description": "この検索で検証する既知Hypothesis ID。",
                },
                "purpose": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 1000,
                    "description": "この検索で確認する法的内容を説明する文章。",
                },
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 1000,
                    "description": (
                        "検索欄へ入力する短い法令用語・法令表現の組合せ。"
                        "purposeや質問を言い換えた文章ではない。"
                    ),
                },
                "doc_types": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["law", "guideline"]},
                    "minItems": 1,
                    "maxItems": 2,
                    "description": "法令本文はlaw、行政解釈やガイドはguideline。",
                },
            }
        )
        return _strict_object(
            {
                "search_requests": {
                    "type": "array",
                    "items": search_request,
                    "minItems": 1,
                    "maxItems": context.max_tool_requests_per_step,
                    "description": "今回実行するlegal_search要求。",
                }
            }
        )
    raise ValueError(f"unknown staged research projection: {projection}")


def _initial_research_transport_schema(
    context: SolverContext,
) -> dict[str, Any]:
    """初回分解で使う差分だけを返すProvider schema。"""

    update_schema = _case_update_transport_schema()
    update_properties = update_schema["properties"]
    update_schema = _strict_object(
        {
            "add_work_items": update_properties["add_work_items"],
            "add_hypotheses": update_properties["add_hypotheses"],
        }
    )
    update_schema["properties"]["add_work_items"]["minItems"] = 1
    update_schema["properties"]["add_hypotheses"]["minItems"] = 1
    return _strict_object(
        {
            "next": _described(
                {"type": "string", "enum": ["continue"]},
                SolverDecision,
                "next",
            ),
            "decision_reason": _described(
                {"type": "string", "minLength": 1},
                SolverDecision,
                "decision_reason",
            ),
            "start_next_cycle": _described(
                {"type": "boolean", "enum": [False]},
                SolverDecision,
                "start_next_cycle",
            ),
            "update": _described(
                update_schema,
                SolverDecision,
                "update",
            ),
            "next_focus_work_item_ids": _described(
                _string_array_schema(),
                SolverDecision,
                "next_focus_work_item_ids",
            ),
            "tool_requests": _described(
                _tool_requests_transport_schema(context),
                SolverDecision,
                "tool_requests",
            ),
        }
    )

def _solver_prompt(
    context: SolverContext,
    system_prompt: str,
    *,
    completion_check_prompt: str | None = None,
    compact_transport: bool = False,
    structured_tool_transport: bool = False,
) -> str:
    """既存呼出し向け。新規コードはRenderedModelCallを使う。"""

    return _render_solver_model_call(
        context,
        system_prompt,
        completion_check_prompt=completion_check_prompt,
    ).request


def _solver_transport_instruction(
    output_schema: dict[str, Any] | None = None,
) -> str:
    if output_schema is not None and "decision_json" in output_schema.get(
        "properties", {}
    ):
        return (
            "以下は現在のSolverContextです。decision_jsonへ、上記の役割と"
            "契約に従うSolverDecisionをJSON object形式の文字列として返してください。"
            "AdapterがJSONを復元し、既知ID、件数、参照整合を含む共通契約で"
            "完全検証します。\n"
        )
    encoded_fields = tuple(
        name
        for name in (output_schema or {}).get("properties", {})
        if name.endswith("_json")
    )
    if encoded_fields:
        encoded_field_names = "、".join(f"`{name}`" for name in encoded_fields)
        return (
            "以下は現在のSolverContextです。出力schemaに従ってください。"
            f"{encoded_field_names}には、各descriptionが指定する値をJSON形式の"
            "文字列として返し、同名の`_json`なし項目は返しません。"
            "AdapterがJSONを復元し、既知ID、件数、参照整合を検証します。\n"
        )
    return (
        "以下は現在のSolverContextです。コンパクト輸送schemaに従い、"
        "復元後SolverDecisionのうちupdateを構造化object、tool_requestsを"
        "構造化配列として直接返してください。各ToolRequestのargumentsは、"
        "available_toolsにある該当Toolのinput_schemaへ一致するJSON objectとして返します。"
        "update_json、tool_requests_json、arguments_jsonは返しません。"
        "schemaにないSolverDecision項目は返さず、既定値へ復元します。"
        "Adapterが既知ID、件数、参照整合を含む共通契約で完全検証します。\n"
    )


def _contract_repair_catalog(context: SolverContext) -> str:
    if context.contract_feedback is None:
        return ""
    section_names = _CONTRACT_REPAIR_SECTIONS
    rules = "\n".join(
        f"### {section_name}\n"
        f"{render_prompt_section('solver_contract_repair.md', section_name)}"
        for section_name in section_names
    )
    return (
        f"{render_prompt_section('solver_contract_repair.md', 'contract_feedback_rule')}\n"
        "contract_feedback.violationに該当する規則だけを適用してください。\n"
        f"<contract_repair_rules>\n{rules}\n</contract_repair_rules>\n"
    )


def render_observation_integration_model_call(
    context: SolverContext,
    profile: ModelCallProfile,
    *,
    work_item_session: WorkItemSession | None = None,
    provider: str | None = None,
) -> RenderedModelCall:
    input_payload = _observation_integration_context_payload(
        context,
        work_item_session=work_item_session,
    )
    output_schema = (
        _observation_integration_anthropic_transport_schema(context)
        if provider == "anthropic"
        else _observation_integration_transport_schema(context)
    )
    transport_instruction = (
        "出力schemaの`tool_requests_json`には、fetch_articlesとload_evidence以外の要求を"
        "JSON array文字列として返してください。fetch_articlesとload_evidenceは"
        "各専用欄へ返し、対象はschemaの既知別名から選びます。"
        "Adapterが復元後に、既知ID、件数、Tool入力を含む共通契約で"
        "完全検証します。\n"
        if provider == "anthropic"
        else ""
    )
    instructions = (
        f"{profile.system_prompt}\n\n"
        f"{transport_instruction}"
        f"{RUNTIME_INPUT_MARKER}"
        f"{_post_context_completion_check(profile.completion_check_prompt)}"
    )
    rendered = build_rendered_model_call(
        stage="observation_integration",
        instructions=instructions,
        input_tag="observation_input",
        input_payload=input_payload,
        output_schema=output_schema,
        normalized_schema=ObservationIntegrationDecision.model_json_schema(),
    )
    _ensure_solver_prompt_capacity(rendered.request, context.max_solver_input_chars)
    return rendered


def render_cycle_close_model_call(
    context: SolverContext,
    observation: ObservationIntegrationDecision,
    profile: ModelCallProfile,
) -> RenderedModelCall:
    if profile.followup_system_prompt is None:
        raise ModelProtocolError("cycle close followup prompt is unavailable")
    input_payload = _cycle_close_context_payload(context, observation)
    instructions = (
        f"{profile.followup_system_prompt}\n\n"
        f"{RUNTIME_INPUT_MARKER}"
        f"{_post_context_completion_check(profile.followup_completion_check_prompt)}"
    )
    rendered = build_rendered_model_call(
        stage="cycle_close",
        instructions=instructions,
        input_tag="cycle_close_input",
        input_payload=input_payload,
        output_schema=_strip_runtime_id_enums(
            _cycle_close_transport_schema(context, observation)
        ),
        normalized_schema=CycleCloseDecision.model_json_schema(),
    )
    _ensure_solver_prompt_capacity(rendered.request, context.max_solver_input_chars)
    return rendered


def render_hypothesis_revision_model_call(
    context: SolverContext,
    profile: ModelCallProfile,
) -> RenderedModelCall:
    """反証済みHypothesisの現在版を見直すCycle境界の専用呼出し。"""

    (
        eligible_work_item_ids,
        eligible_hypotheses,
        eligible_evidence_ids,
    ) = _hypothesis_revision_scope(context)
    hypotheses_by_work: dict[str, list[Hypothesis]] = {}
    for item in eligible_hypotheses:
        if item.work_item_id in eligible_work_item_ids:
            hypotheses_by_work.setdefault(item.work_item_id, []).append(item)
    input_model = HypothesisRevisionInput(
        work_items=tuple(
            HypothesisRevisionWorkItem(
                work_item_id=item.work_item_id,
                question=item.question,
                hypothesis_ids=tuple(
                    hypothesis.hypothesis_id
                    for hypothesis in hypotheses_by_work.get(item.work_item_id, ())
                ),
            )
            for item in context.work_tree
            if item.work_item_id in eligible_work_item_ids
        ),
        hypotheses=tuple(
            HypothesisRevisionHypothesis(
                hypothesis_id=item.hypothesis_id,
                work_item_id=item.work_item_id,
                statement=item.statement,
                judgment=item.judgment,
                gaps=structured_hypothesis_gaps(
                    item.hypothesis_id,
                    item.gaps,
                ),
            )
            for item in eligible_hypotheses
        ),
        acquired_evidence=tuple(
            HypothesisRevisionEvidence(
                evidence_id=item.evidence_id,
                content=item.content,
                title=item.title,
            )
            for item in context.hypothesis_revision_evidence
            if item.evidence_id in eligible_evidence_ids
        ),
    )
    input_payload = input_model.model_dump(mode="json")
    output_schema = _hypothesis_revision_transport_schema(context)
    instructions = (
        f"{profile.system_prompt}\n\n"
        f"{render_model_input_glossary(HypothesisRevisionInput)}\n\n"
        f"{RUNTIME_INPUT_MARKER}"
        f"{_post_context_completion_check(profile.completion_check_prompt)}"
    )
    rendered = build_rendered_model_call(
        stage="hypothesis_revision",
        instructions=instructions,
        input_tag="hypothesis_revision_input",
        input_payload=input_payload,
        output_schema=output_schema,
        normalized_schema=HypothesisRevisionDecision.model_json_schema(),
    )
    _ensure_solver_prompt_capacity(rendered.request, context.max_solver_input_chars)
    return rendered


def render_final_answer_check_model_call(
    context: SolverContext,
    observation: ObservationIntegrationDecision,
    draft_answer: FinalAnswer,
    profile: ModelCallProfile,
) -> RenderedModelCall:
    """意味判断済み根拠だけで、最終回答本文の欠落・誤読を確認する。"""

    if profile.final_answer_check_system_prompt is None:
        raise ModelProtocolError("final answer check prompt is unavailable")
    input_payload = {
        "question": context.question,
        "non_work_item_requirements": list(context.non_work_item_requirements),
        "draft_answer": draft_answer.model_dump(mode="json"),
        "answer_basis_by_work_item": _answer_basis_by_work_item(
            context,
            observation,
        ),
    }
    instructions = (
        f"{profile.final_answer_check_system_prompt}\n\n"
        f"{RUNTIME_INPUT_MARKER}"
        f"{_post_context_completion_check(profile.final_answer_check_completion_prompt)}"
    )
    output_schema = _strict_object(
        {
            "text": _described({"type": "string"}, FinalAnswer, "text"),
        }
    )
    rendered = build_rendered_model_call(
        stage="final_answer_check",
        instructions=instructions,
        input_tag="final_answer_check_input",
        input_payload=input_payload,
        output_schema=output_schema,
        normalized_schema=output_schema,
    )
    _ensure_solver_prompt_capacity(rendered.request, context.max_solver_input_chars)
    return rendered


def render_dependency_assessment_model_call(
    context: SolverContext,
    observation: ObservationIntegrationDecision,
    profile: ModelCallProfile,
) -> RenderedModelCall:
    if profile.dependency_system_prompt is None:
        raise ModelProtocolError("dependency assessment prompt is unavailable")
    work_items, hypotheses = _project_observation_integration_state(
        context,
        observation,
    )
    required_ids = set(context.required_dependency_work_item_ids)
    input_payload: dict[str, Any] = {
        "question": context.question,
        "required_dependency_kind": context.required_dependency_kind,
        "work_items": [
            item.model_dump(mode="json")
            for item in work_items
            if item.work_item_id in required_ids
        ],
        "hypotheses": [
            item.model_dump(mode="json")
            for item in hypotheses
            if item.work_item_id in required_ids
        ],
        "grounding_evidence": [
            item.model_dump(mode="json")
            for item in context.material_evidence
            if item.evidence_id in set(context.grounding_evidence_ids)
        ],
    }
    if context.contract_feedback is not None:
        previous = context.contract_feedback.previous_decision
        input_payload["contract_feedback"] = {
            "violation": context.contract_feedback.violation,
        }
        input_payload["previous_dependency_assessment"] = {
            "decision_reason": previous.decision_reason,
            "dependency_decisions": [
                _dependency_decision_transport_payload(item)
                for item in previous.dependency_decisions
                if item.work_item_id in required_ids
                and item.dependency_kind == context.required_dependency_kind
            ],
        }
    instructions = (
        f"{profile.dependency_system_prompt}\n\n"
        f"{RUNTIME_INPUT_MARKER}"
        f"{_post_context_completion_check(profile.dependency_completion_check_prompt)}"
    )
    rendered = build_rendered_model_call(
        stage="dependency_assessment",
        instructions=instructions,
        input_tag="dependency_input",
        input_payload=input_payload,
        output_schema=_dependency_assessment_transport_schema(context),
        normalized_schema=DependencyAssessmentDecision.model_json_schema(),
    )
    _ensure_solver_prompt_capacity(rendered.request, context.max_solver_input_chars)
    return rendered


def _observation_integration_context_payload(
    context: SolverContext,
    *,
    work_item_session: WorkItemSession | None = None,
) -> dict[str, Any]:
    grounding_ids = set(context.grounding_evidence_ids)
    active_work_items, active_hypotheses = _active_observation_items(context)
    active_work_item_ids = {item.work_item_id for item in active_work_items}
    active_hypothesis_ids = {
        item.hypothesis_id for item in active_hypotheses
    }
    payload: dict[str, Any] = {
        "non_work_item_requirements": list(
            context.non_work_item_requirements
        ),
        "work_items": [
            {
                "work_item_id": item.work_item_id,
                "question": item.question,
            }
            for item in active_work_items
        ],
        "hypotheses": [
            {
                **item.model_dump(mode="json"),
                "gaps": [
                    gap.model_dump(mode="json")
                    for gap in structured_hypothesis_gaps(
                        item.hypothesis_id,
                        item.gaps,
                    )
                ],
            }
            for item in active_hypotheses
        ],
        "evidence_hypothesis_candidates": [
            {
                "article_id": item.article_id,
                "hypothesis_ids": list(item.hypothesis_ids),
                "assessment_summary": item.assessment_summary,
            }
            for item in context.evidence_hypothesis_candidates
            if set(item.hypothesis_ids).intersection(
                hypothesis.hypothesis_id for hypothesis in active_hypotheses
            )
        ],
        "grounding_evidence": [
            {
                "evidence_id": item.evidence_id,
                "content": item.content,
                "title": item.title,
                "metadata": {
                    key: item.metadata[key]
                    for key in (
                        "articleId",
                        "sourceContentUnitId",
                        "docType",
                        "heading",
                    )
                    if key in item.metadata
                },
            }
            for item in context.material_evidence
            if item.evidence_id in grounding_ids
        ],
        "required_dependency_kind": (
            context.required_dependency_kind
            if context.required_dependency_work_item_ids
            else None
        ),
        "dependency_decisions": [
            item.model_dump(mode="json")
            for item in context.dependency_decisions
            if item.work_item_id in active_work_item_ids
        ],
        "remaining_fetch_capacity": context.remaining_fetch_capacity,
        "fetchable_article_ids": list(context.fetchable_article_ids),
        "omitted_evidence_ids": list(context.omitted_evidence_ids),
        "search_candidates": [
            item.model_dump(mode="json")
            for item in context.search_candidates
            if active_hypothesis_ids.intersection(
                (
                    *item.matched_hypothesis_ids,
                    *item.discovery_hypothesis_ids,
                )
            )
            or active_work_item_ids.intersection(
                item.discovery_work_item_ids
            )
        ],
        "completed_legal_searches": [
            item.model_dump(mode="json")
            for item in context.completed_legal_searches
            if item.work_item_id in active_work_item_ids
        ],
        "completed_graph_searches": [
            item.model_dump(mode="json")
            for item in context.completed_graph_searches
            if item.work_item_id in active_work_item_ids
        ],
        "graph_fetch_completed_hypothesis_ids_this_cycle": [
            hypothesis_id
            for hypothesis_id in (
                context.graph_fetch_completed_hypothesis_ids_this_cycle
            )
            if hypothesis_id in active_hypothesis_ids
        ],
        "cycle_close_required": context.cycle_close_required,
    }
    if work_item_session is not None:
        payload["work_item_session"] = work_item_session.as_input()
    if context.contract_feedback is not None:
        previous = context.contract_feedback.previous_decision
        active_hypothesis_ids = {
            item.hypothesis_id for item in active_hypotheses
        }
        payload["contract_feedback"] = {
            "violation": context.contract_feedback.violation,
        }
        payload["previous_observation"] = {
            "decision_reason": previous.decision_reason,
            "update_hypotheses": [
                item.model_dump(mode="json")
                for item in previous.update.update_hypotheses
                if item.hypothesis_id in active_hypothesis_ids
            ],
        }
    return payload


def _observation_work_item_contexts(
    context: SolverContext,
) -> tuple[SolverContext, ...]:
    """直近取得本文を、前段LLMの対応付けに従ってWorkItem別へ投影する。"""

    active_work_items, active_hypotheses = _active_observation_items(context)
    repair_work_item_ids = (
        set(context.contract_feedback.repair_work_item_ids)
        if context.contract_feedback is not None
        else set()
    )
    if repair_work_item_ids:
        active_work_items = tuple(
            item
            for item in active_work_items
            if item.work_item_id in repair_work_item_ids
        )
        active_hypotheses = tuple(
            item
            for item in active_hypotheses
            if item.work_item_id in repair_work_item_ids
        )
    if not active_work_items:
        return ()
    grounding_ids = set(context.grounding_evidence_ids)
    recent_grounding_ids = {
        evidence_id
        for result in context.recent_tool_results
        for evidence_id in result.evidence_ids
        if evidence_id in grounding_ids
    }
    observation_grounding_ids = recent_grounding_ids or grounding_ids
    observation_article_ids = {
        article_id
        for item in context.material_evidence
        if item.evidence_id in observation_grounding_ids
        and isinstance(article_id := item.metadata.get("articleId"), str)
    }
    pending_review_work_item_ids = pending_candidate_review_work_item_ids(
        context
    )
    has_candidate_mappings = bool(context.evidence_hypothesis_candidates)
    projected: list[SolverContext] = []
    for work_item in active_work_items:
        hypotheses = tuple(
            item
            for item in active_hypotheses
            if item.work_item_id == work_item.work_item_id
        )
        hypothesis_ids = {item.hypothesis_id for item in hypotheses}
        search_candidates = tuple(
            item
            for item in context.search_candidates
            if hypothesis_ids.intersection(item.matched_hypothesis_ids)
        )
        search_candidate_article_ids = {
            item.article_id for item in search_candidates
        }
        candidate_review_pending = (
            work_item.work_item_id in pending_review_work_item_ids
        )
        candidates = tuple(
            item.model_copy(
                update={
                    "hypothesis_ids": tuple(
                        hypothesis_id
                        for hypothesis_id in item.hypothesis_ids
                        if hypothesis_id in hypothesis_ids
                    )
                }
            )
            for item in context.evidence_hypothesis_candidates
            if item.article_id in observation_article_ids
            if set(item.hypothesis_ids).intersection(hypothesis_ids)
        )
        candidate_article_ids = {item.article_id for item in candidates}
        existing_evidence_ids = {
            evidence_id
            for hypothesis in hypotheses
            if hypothesis.gaps
            for evidence_id in hypothesis.evidence_ids
        }
        visible_ids = tuple(
            item.evidence_id
            for item in context.material_evidence
            if (
                item.evidence_id in existing_evidence_ids
                or (
                    item.evidence_id in observation_grounding_ids
                    and (
                        not has_candidate_mappings
                        or item.metadata.get("articleId")
                        in candidate_article_ids
                    )
                )
            )
        )
        visible_id_set = set(visible_ids)
        completed_load_ids = set(
            context.completed_load_evidence_ids_by_work_item.get(
                work_item.work_item_id,
                (),
            )
        )
        scoped_omitted_evidence_ids = (
            context.omitted_evidence_ids_by_work_item.get(
                work_item.work_item_id,
                (),
            )
            if context.omitted_evidence_ids_by_work_item
            else context.omitted_evidence_ids
        )
        omitted_evidence_ids = tuple(
            evidence_id
            for evidence_id in scoped_omitted_evidence_ids
            if evidence_id not in completed_load_ids
            and evidence_id not in visible_id_set
        )
        projected.append(
            context.model_copy(
                update={
                    "work_tree": (work_item,),
                    "hypotheses": hypotheses,
                    "grounding_evidence_ids": visible_ids,
                    "evidence_manifest": tuple(
                        item
                        for item in context.evidence_manifest
                        if item.evidence_id in visible_id_set
                    ),
                    "material_evidence": tuple(
                        item
                        for item in context.material_evidence
                        if item.evidence_id in visible_id_set
                    ),
                    "omitted_evidence_ids": omitted_evidence_ids,
                    "completed_load_evidence_ids_by_work_item": {
                        work_item.work_item_id: tuple(sorted(completed_load_ids))
                    },
                    "omitted_evidence_ids_by_work_item": {
                        work_item.work_item_id: omitted_evidence_ids
                    },
                    "evidence_hypothesis_candidates": candidates,
                    "search_candidates": (
                        () if candidate_review_pending else search_candidates
                    ),
                    "fetchable_article_ids": tuple(
                        article_id
                        for article_id in context.fetchable_article_ids
                        if article_id in search_candidate_article_ids
                    )
                    if not candidate_review_pending
                    else (),
                    "available_tools": (
                        ()
                        if candidate_review_pending
                        else context.available_tools
                    ),
                    "required_dependency_work_item_ids": tuple(
                        item_id
                        for item_id in context.required_dependency_work_item_ids
                        if item_id == work_item.work_item_id
                    ),
                }
            )
        )
    return tuple(projected)


def _allocate_observation_fetch_capacity(
    contexts: tuple[SolverContext, ...],
) -> tuple[SolverContext, ...]:
    """WorkItem別の本文取得残量を、対応する専属入力へ投影する。"""

    if not contexts:
        return ()

    projected: list[SolverContext] = []
    for context in contexts:
        work_item_id = next(
            (item.work_item_id for item in context.work_tree),
            None,
        )
        quota = context.remaining_fetch_capacity_by_work_item.get(
            work_item_id or "",
            context.remaining_fetch_capacity,
        )
        fetched_ids = context.fetched_resource_ids_by_work_item_this_cycle.get(
            work_item_id or "",
            (),
        )
        available_tools = context.available_tools
        fetchable_article_ids = context.fetchable_article_ids
        if quota == 0:
            available_tools = tuple(
                item for item in available_tools if item.name != "fetch_articles"
            )
            fetchable_article_ids = ()
        if not context.omitted_evidence_ids:
            available_tools = tuple(
                item for item in available_tools if item.name != "load_evidence"
            )
        projected.append(
            context.model_copy(
                update={
                    "remaining_fetch_capacity": quota,
                    "remaining_fetch_capacity_by_work_item": (
                        {work_item_id: quota} if work_item_id is not None else {}
                    ),
                    "fetched_resource_ids_this_cycle": fetched_ids,
                    "fetched_resource_ids_by_work_item_this_cycle": (
                        {work_item_id: fetched_ids}
                        if work_item_id is not None
                        else {}
                    ),
                    "fetchable_article_ids": fetchable_article_ids,
                    "available_tools": available_tools,
                }
            )
        )
    return tuple(projected)


def _dependency_work_item_contexts(
    context: SolverContext,
) -> tuple[SolverContext, ...]:
    """下位規範確認を、関連本文を持つWorkItem単位へ分ける。"""

    required_ids = set(context.required_dependency_work_item_ids)
    projected = []
    for item in _observation_work_item_contexts(context):
        item_ids = tuple(
            work_item.work_item_id
            for work_item in item.work_tree
            if work_item.work_item_id in required_ids
        )
        if not item_ids:
            continue
        projected.append(
            item.model_copy(
                update={"required_dependency_work_item_ids": item_ids}
            )
        )
    return tuple(projected)


def _pending_dependency_action_contexts(
    context: SolverContext,
) -> tuple[SolverContext, ...]:
    """直近本文の担当外に残るneeds_actionをWorkItem別へ投影する。"""

    observed_work_item_ids = {
        item.work_item_id for item in _active_observation_items(context)[0]
    }
    open_work_item_ids = {
        item.work_item_id
        for item in context.work_tree
        if item.state == "open"
    }
    pending_review_work_item_ids = pending_candidate_review_work_item_ids(
        context
    )
    pending_ids = tuple(
        dict.fromkeys(
            item.work_item_id
            for item in context.dependency_decisions
            if item.status == "needs_action"
            and item.work_item_id in context.required_dependency_work_item_ids
            and item.work_item_id in open_work_item_ids
            and item.work_item_id not in observed_work_item_ids
            and item.work_item_id not in pending_review_work_item_ids
        )
    )
    projected = []
    for work_item_id in pending_ids:
        quota = context.remaining_fetch_capacity_by_work_item.get(
            work_item_id,
            context.remaining_fetch_capacity,
        )
        projected.append(
            context.model_copy(
                update={
                    "required_dependency_work_item_ids": (work_item_id,),
                    "remaining_fetch_capacity": quota,
                    "remaining_fetch_capacity_by_work_item": {
                        work_item_id: quota
                    },
                }
            )
        )
    return tuple(projected)


def _project_observation_decision(
    observation: ObservationIntegrationDecision,
    *,
    context: SolverContext,
) -> ObservationIntegrationDecision:
    """WorkItem別の下位規範確認へ、同じWorkItemの更新だけを渡す。"""

    work_item_ids = {item.work_item_id for item in context.work_tree}
    hypothesis_ids = {
        item.hypothesis_id for item in context.hypotheses
    }
    return observation.model_copy(
        update={
            "update_work_items": tuple(
                item
                for item in observation.update_work_items
                if item.work_item_id in work_item_ids
            ),
            "update_hypotheses": tuple(
                item
                for item in observation.update_hypotheses
                if item.hypothesis_id in hypothesis_ids
            ),
            "dependency_decisions": tuple(
                item
                for item in observation.dependency_decisions
                if item.work_item_id in work_item_ids
            ),
        }
    )


def _merge_observation_integrations(
    observations: list[ObservationIntegrationDecision],
) -> ObservationIntegrationDecision:
    """独立したWorkItem評価を、意味を変えず一つのCycle差分へ連結する。"""

    if not observations:
        return ObservationIntegrationDecision(
            decision_reason="評価対象のopen WorkItemはない。"
        )
    dependency_decisions = [
        decision.model_dump(mode="json")
        for item in observations
        for decision in item.dependency_decisions
    ]
    tool_requests = [
        request.model_dump(mode="json")
        for item in observations
        for request in item.tool_requests
    ]
    tool_requests = _consolidate_identical_graph_requests(
        tool_requests,
        dependency_decisions,
    )
    return ObservationIntegrationDecision(
        decision_reason=(
            f"{len(observations)}件のWorkItemについて、"
            "取得本文、下位規範状態及び直後の行動を統合した。"
        ),
        update_hypotheses=tuple(
            update
            for item in observations
            for update in item.update_hypotheses
        ),
        dependency_decisions=tuple(
            DependencyDecision.model_validate(item)
            for item in dependency_decisions
        ),
        tool_requests=tuple(
            ToolRequest.model_validate(item) for item in tool_requests
        ),
    )


def _active_observation_items(
    context: SolverContext,
) -> tuple[tuple[WorkTreeItem, ...], tuple[Hypothesis, ...]]:
    """逐次統合対象を、直近Tool結果に対応するopenの作業へ限定する。"""

    hypothesis_work_item_ids = {
        item.hypothesis_id: item.work_item_id for item in context.hypotheses
    }
    recent_work_item_ids = set()
    for request in context.recent_tool_requests:
        recent_work_item_ids.add(request.work_item_id)
        recent_work_item_ids.update(
            hypothesis_work_item_ids[hypothesis_id]
            for hypothesis_id in request.hypothesis_ids
            if hypothesis_id in hypothesis_work_item_ids
        )
    work_items = tuple(
        item
        for item in context.work_tree
        if item.state == "open"
        and (
            not recent_work_item_ids
            or item.work_item_id in recent_work_item_ids
        )
    )
    work_item_ids = {item.work_item_id for item in work_items}
    hypotheses = tuple(
        item
        for item in context.hypotheses
        if item.work_item_id in work_item_ids
    )
    return work_items, hypotheses


def _cycle_close_context_payload(
    context: SolverContext,
    observation: ObservationIntegrationDecision,
) -> dict[str, Any]:
    work_items, hypotheses = _project_observation_integration_state(
        context,
        observation,
    )
    dependency_decisions = _merged_dependency_decisions(
        context.dependency_decisions,
        observation.dependency_decisions,
    )
    retainable_evidence_ids = set(
        _retainable_cycle_evidence_ids(context, observation)
    )
    active_deferred, _ = _cycle_close_deferred_frontiers(
        context,
        observation,
    )
    active_deferred_payload = [
        item.model_dump(mode="json")
        for item in active_deferred
    ]
    payload: dict[str, Any] = {
        "question": context.question,
        "required_transition": _required_cycle_close_transition(
            context,
            observation,
        ),
        "work_items_after_observation": [
            item.model_dump(mode="json") for item in work_items
        ],
        "hypotheses_after_observation": [
            item.model_dump(mode="json") for item in hypotheses
        ],
        "observation_summary": observation.decision_reason,
        "dependency_decisions_after_observation": [
            item.model_dump(mode="json")
            for item in dependency_decisions
        ],
        "max_retained_evidence": context.max_retained_evidence,
        "retainable_evidence": [
            item.model_dump(mode="json")
            for item in context.evidence_manifest
            if item.evidence_id in retainable_evidence_ids
        ],
        "active_deferred_frontiers": active_deferred_payload,
        "unreviewed_graph_candidate_count": (
            context.graph_review_batch.remaining_unreviewed_count
        ),
    }
    if context.contract_feedback is not None:
        payload["contract_feedback"] = {
            "violation": context.contract_feedback.violation,
        }
    return payload


def _required_answer_evidence_ids(
    context: SolverContext,
    observation: ObservationIntegrationDecision,
) -> tuple[str, ...]:
    """LLMが確定したWorkItem・依存根拠から、最終回答の根拠IDを集約する。"""

    work_items, hypotheses = _project_observation_integration_state(
        context,
        observation,
    )
    resolved_work_item_ids = {
        item.work_item_id for item in work_items if item.state == "resolved"
    }
    basis_hypothesis_ids = {
        hypothesis_id
        for item in work_items
        if item.work_item_id in resolved_work_item_ids
        for hypothesis_id in item.basis_hypothesis_ids
    }
    dependency_decisions = _merged_dependency_decisions(
        context.dependency_decisions,
        observation.dependency_decisions,
    )
    return tuple(
        dict.fromkeys(
            [
                *(
                    evidence_id
                    for hypothesis in hypotheses
                    if hypothesis.hypothesis_id in basis_hypothesis_ids
                    for evidence_id in hypothesis.evidence_ids
                ),
                *(
                    evidence_id
                    for decision in dependency_decisions
                    if decision.work_item_id in resolved_work_item_ids
                    and decision.status == "resolved"
                    for evidence_id in decision.basis_evidence_ids
                ),
            ]
        )
    )


def _verified_answer_evidence_ids(
    context: SolverContext,
    observation: ObservationIntegrationDecision,
) -> tuple[str, ...]:
    """Hypothesisの確認済み部分・解決済みDependencyの本文IDを返す。"""

    _, hypotheses = _project_observation_integration_state(
        context,
        observation,
    )
    dependency_decisions = _merged_dependency_decisions(
        context.dependency_decisions,
        observation.dependency_decisions,
    )
    citable_ids = {
        evidence_id
        for hypothesis in hypotheses
        for evidence_id in hypothesis.evidence_ids
    }
    citable_ids.update(
        evidence_id
        for decision in dependency_decisions
        if decision.status == "resolved"
        for evidence_id in decision.basis_evidence_ids
    )
    return tuple(
        evidence_id
        for evidence_id in context.grounding_evidence_ids
        if evidence_id in citable_ids
    )


def _include_required_finalization_citations(
    payload: dict[str, Any],
    context: SolverContext,
) -> None:
    """解決済みWorkItemへ対応付けた根拠IDを回答へ機械反映する。"""

    answer = payload.get("answer")
    if not isinstance(answer, dict):
        return
    required_ids = _required_answer_evidence_ids(
        context,
        ObservationIntegrationDecision(
            decision_reason="逐次統合済み状態から根拠IDを投影する。"
        ),
    )
    citation_ids = answer.get("citation_ids")
    if not isinstance(citation_ids, list):
        citation_ids = []
    answer["citation_ids"] = list(
        dict.fromkeys([*citation_ids, *required_ids])
    )


def _merged_dependency_decisions(
    existing: tuple[DependencyDecision, ...],
    updates: tuple[DependencyDecision, ...],
) -> tuple[DependencyDecision, ...]:
    """現在Cycleの差分で同じWorkItemの既存判断だけを置換する。"""

    by_key = {
        (item.dependency_kind, item.work_item_id): item for item in existing
    }
    for item in updates:
        by_key[(item.dependency_kind, item.work_item_id)] = item
    return tuple(by_key.values())


def _retainable_cycle_evidence_ids(
    context: SolverContext,
    observation: ObservationIntegrationDecision,
) -> tuple[str, ...]:
    """状態から自動再投影される根拠を除いた追加保持候補を返す。"""

    _, hypotheses = _project_observation_integration_state(
        context,
        observation,
    )
    state_managed_ids = {
        evidence_id
        for hypothesis in hypotheses
        for evidence_id in hypothesis.evidence_ids
    }
    state_managed_ids.update(
        evidence_id
        for decision in observation.dependency_decisions
        for evidence_id in decision.basis_evidence_ids
    )
    return tuple(
        evidence_id
        for evidence_id in context.grounding_evidence_ids
        if evidence_id not in state_managed_ids
    )


def _answer_basis_by_work_item(
    context: SolverContext,
    observation: ObservationIntegrationDecision,
) -> list[dict[str, Any]]:
    """確定済み根拠をWorkItem単位に束ね、意味選別せず回答工程へ渡す。"""

    work_items, hypotheses = _project_observation_integration_state(
        context,
        observation,
    )
    evidence_by_id = {
        item.evidence_id: item for item in context.material_evidence
    }
    result: list[dict[str, Any]] = []
    for work_item in work_items:
        if work_item.state != "resolved":
            continue
        evidence_ids = tuple(
            dict.fromkeys(
                [
                    *(
                        evidence_id
                        for hypothesis in hypotheses
                        if hypothesis.hypothesis_id
                        in set(work_item.basis_hypothesis_ids)
                        for evidence_id in hypothesis.evidence_ids
                    ),
                    *(
                        evidence_id
                        for decision in observation.dependency_decisions
                        if decision.work_item_id == work_item.work_item_id
                        and decision.status == "resolved"
                        for evidence_id in decision.basis_evidence_ids
                    ),
                ]
            )
        )
        result.append(
            {
                "work_item_id": work_item.work_item_id,
                "question": work_item.question,
                "required_evidence": [
                    evidence_by_id[evidence_id].model_dump(mode="json")
                    for evidence_id in evidence_ids
                    if evidence_id in evidence_by_id
                ],
            }
        )
    return result


def _project_observation_integration_state(
    context: SolverContext,
    observation: ObservationIntegrationDecision,
) -> tuple[tuple[WorkTreeItem, ...], tuple[Hypothesis, ...]]:
    """意味判断済みの差分を、Cycle Close向けread modelへ機械適用する。"""

    work_items = {item.work_item_id: item for item in context.work_tree}
    hypotheses = {
        item.hypothesis_id: item for item in context.hypotheses
    }
    updated_work_item_ids: set[str] = set()
    for update in observation.update_work_items:
        if update.work_item_id in updated_work_item_ids:
            raise ModelProtocolError(
                f"duplicate observation WorkItem update: {update.work_item_id}"
            )
        updated_work_item_ids.add(update.work_item_id)
        current = work_items.get(update.work_item_id)
        if current is None:
            raise ModelProtocolError(
                f"unknown observation WorkItem update: {update.work_item_id}"
            )
        work_items[update.work_item_id] = current.model_copy(
            update={
                "state": update.state,
                "resolution": update.resolution,
                "basis_hypothesis_ids": update.basis_hypothesis_ids,
            }
        )

    updated_hypothesis_ids: set[str] = set()
    for update in observation.update_hypotheses:
        if update.hypothesis_id in updated_hypothesis_ids:
            raise ModelProtocolError(
                "duplicate observation Hypothesis update: "
                f"{update.hypothesis_id}"
            )
        updated_hypothesis_ids.add(update.hypothesis_id)
        current = hypotheses.get(update.hypothesis_id)
        if current is None:
            raise ModelProtocolError(
                "unknown observation Hypothesis update: "
                f"{update.hypothesis_id}"
            )
        if update.legacy_replacement_gaps is not None:
            updated_gaps = tuple(
                dict.fromkeys(update.legacy_replacement_gaps)
            )
        else:
            try:
                updated_gaps = apply_hypothesis_gap_diff(
                    current.hypothesis_id,
                    current.gaps,
                    tuple(item.description for item in update.add_gaps),
                    update.resolve_gap_ids,
                )
            except ValueError as exc:
                raise ModelProtocolError(str(exc)) from exc
        hypotheses[update.hypothesis_id] = current.model_copy(
            update={
                "judgment": update.judgment,
                "evidence_ids": merge_hypothesis_evidence_ids(
                    current.evidence_ids,
                    update.evidence_ids,
                ),
                "gaps": updated_gaps,
            }
        )

    hypotheses_by_work_item: dict[str, list[Hypothesis]] = {}
    for hypothesis in hypotheses.values():
        hypotheses_by_work_item.setdefault(
            hypothesis.work_item_id,
            [],
        ).append(hypothesis)
    projected_work_items = tuple(
        item.model_copy(
            update={
                "hypothesis_ids": tuple(
                    hypothesis.hypothesis_id
                    for hypothesis in hypotheses_by_work_item.get(
                        item.work_item_id,
                        (),
                    )
                ),
                "evidence_count": len(
                    {
                        evidence_id
                        for hypothesis in hypotheses_by_work_item.get(
                            item.work_item_id,
                            (),
                        )
                        for evidence_id in hypothesis.evidence_ids
                    }
                ),
            }
        )
        for item in work_items.values()
    )
    return projected_work_items, tuple(hypotheses.values())


def _observation_integration_transport_schema(
    context: SolverContext,
) -> dict[str, Any]:
    _, active_hypotheses = _active_observation_items(context)
    hypothesis_ids = tuple(item.hypothesis_id for item in active_hypotheses)
    schema_hypothesis_ids = hypothesis_ids or tuple(
        item.hypothesis_id for item in context.hypotheses
    )
    evidence_ids = context.grounding_evidence_ids
    gap_ids = tuple(
        gap.gap_id
        for hypothesis in active_hypotheses
        for gap in structured_hypothesis_gaps(
            hypothesis.hypothesis_id,
            hypothesis.gaps,
        )
    )
    gap_addition = _strict_object(
        {
            "description": _described(
                {"type": "string", "minLength": 1, "maxLength": 300},
                HypothesisGapAddition,
                "description",
            )
        }
    )
    hypothesis_update = _strict_object(
        {
            "hypothesis_id": _described(
                _enum_string(schema_hypothesis_ids),
                HypothesisUpdate,
                "hypothesis_id",
            ),
            "judgment": _described(
                {
                    "type": "string",
                    "enum": ["supported", "contradicted", "unresolved"],
                },
                HypothesisUpdate,
                "judgment",
            ),
            "evidence_ids": _described(
                _bounded_enum_array(evidence_ids),
                HypothesisUpdate,
                "evidence_ids",
            ),
            "add_gaps": _described(
                {
                    "type": "array",
                    "items": gap_addition,
                    "maxItems": 12,
                },
                HypothesisUpdate,
                "add_gaps",
            ),
            "resolve_gap_ids": _described(
                _bounded_enum_array(gap_ids, max_items=12),
                HypothesisUpdate,
                "resolve_gap_ids",
            ),
        }
    )
    dependency_decisions = (
        _dependency_assessment_transport_schema(context)["properties"][
            "dependency_decisions"
        ]
        if context.required_dependency_work_item_ids
        else _empty_array_schema()
    )
    tool_requests = (
        _empty_array_schema()
        if context.cycle_close_required
        else _tool_requests_transport_schema(context)
    )
    tool_requests["maxItems"] = min(
        len(
            {
                item.work_item_id
                for item in context.work_tree
                if item.state == "open"
            }
        ),
        tool_requests.get("maxItems", context.max_tool_requests_per_step),
    )
    return _strict_object(
        {
            "decision_reason": {
                "type": "string",
                "minLength": 1,
                "maxLength": 400,
                "description": ObservationIntegrationDecision.model_fields[
                    "decision_reason"
                ].description,
            },
            "update_hypotheses": {
                "type": "array",
                "items": hypothesis_update,
                "maxItems": len(hypothesis_ids),
                "description": ObservationIntegrationDecision.model_fields[
                    "update_hypotheses"
                ].description,
            },
            "dependency_decisions": {
                **dependency_decisions,
                "description": (
                    "同じWorkItemについて、起点規範から末端規範までの"
                    "本文確認状態を判断した結果。"
                ),
            },
            "tool_requests": {
                **tool_requests,
                "description": ObservationIntegrationDecision.model_fields[
                    "tool_requests"
                ].description,
            },
        }
    )


def _observation_integration_anthropic_transport_schema(
    context: SolverContext,
) -> dict[str, Any]:
    """Anthropicのgrammar上限を避け、復元後に正規契約を検証する。"""

    common = _observation_integration_transport_schema(context)
    properties = common["properties"]
    return _strict_object(
        {
            "decision_reason": properties["decision_reason"],
            "update_hypotheses": properties["update_hypotheses"],
            "dependency_decisions": properties["dependency_decisions"],
            "tool_requests_json": {
                "type": "string",
                "description": (
                    "fetch_articlesとload_evidence以外のToolRequestのJSON array文字列。各要素は"
                    "exactly request_id, "
                    "work_item_id, tool_name, arguments, purpose, hypothesis_idsを"
                    "持ち、argumentsは該当available_tools.input_schemaに従う。"
                ),
            },
            "fetch_articles": {
                **_anthropic_fetch_articles_schema(context),
                "description": (
                    "Anthropic輸送専用のfetch_articles ToolRequest。"
                    "本文取得を要求しない場合はnull。"
                ),
            },
            "load_evidence": {
                **_anthropic_load_evidence_schema(context),
                "description": (
                    "Anthropic輸送専用のload_evidence ToolRequest。"
                    "既知Evidenceの再表示を要求しない場合はnull。"
                ),
            },
        }
    )


def _dependency_decision_transport_payload(
    decision: DependencyDecision,
) -> dict[str, Any]:
    payload = decision.model_dump(mode="json")
    payload["status"] = {
        "needs_action": "terminal_text_missing",
        "resolved": "terminal_text_confirmed",
    }.get(decision.status, decision.status)
    return payload


def _normalize_observation_integration_payload(
    payload: dict[str, Any],
    *,
    context: SolverContext | None = None,
) -> dict[str, Any]:
    observation_payload = payload.get("observation_json")
    if isinstance(observation_payload, str):
        normalized = _decode_transport_json(
            observation_payload,
            expected_type=dict,
            label="observation_json",
        )
    else:
        normalized = deepcopy(payload)
    for encoded_name, decoded_name in (
        ("tool_requests_json", "tool_requests"),
    ):
        if encoded_name in normalized:
            encoded_value = normalized.pop(encoded_name)
            try:
                normalized[decoded_name] = _decode_transport_json(
                    encoded_value,
                    expected_type=list,
                    label=encoded_name,
                )
            except ModelProtocolError:
                # Tool要求はこの判断の任意差分である。輸送文字列だけが途中で
                # 切れた場合も、正常な本文評価を失わず次Cycleへ引き継ぐ。
                normalized[decoded_name] = []
                reason = str(normalized.get("decision_reason") or "").rstrip()
                normalized["decision_reason"] = (
                    f"{reason} Tool要求の輸送文字列が不完全だったため、"
                    "このstepでは実行しない。"
                ).strip()
    has_fetch_sidecar = "fetch_articles" in normalized
    fetch_articles = normalized.pop("fetch_articles", None)
    if has_fetch_sidecar:
        generic_fetch_requests = [
            request
            for request in normalized.get("tool_requests") or []
            if isinstance(request, dict)
            and request.get("tool_name") == "fetch_articles"
        ]
        normalized["tool_requests"] = [
            request
            for request in normalized.get("tool_requests") or []
            if not (
                isinstance(request, dict)
                and request.get("tool_name") == "fetch_articles"
            )
        ]
        if isinstance(fetch_articles, dict):
            if context is None:
                raise ModelProtocolError(
                    "fetch_articles transport requires SolverContext"
                )
            aliases = _article_fetch_alias_map(context)
            article_refs = [
                article_ref
                for key in sorted(fetch_articles)
                if key.startswith("article_ref_")
                and isinstance(
                    article_ref := fetch_articles.get(key),
                    str,
                )
            ]
            direct_article_ids = fetch_articles.get("article_ids")
            if isinstance(direct_article_ids, list):
                article_refs.extend(
                    article_id
                    for article_id in direct_article_ids
                    if isinstance(article_id, str)
                )
            fetchable_article_ids = set(context.fetchable_article_ids)
            article_ids = list(
                dict.fromkeys(
                    resolved_id
                    for article_ref in article_refs
                    if (
                        resolved_id := aliases.get(
                            article_ref,
                            article_ref
                            if article_ref in fetchable_article_ids
                            else None,
                        )
                    )
                )
            )
            if article_ids:
                normalized.setdefault("tool_requests", []).append(
                    {
                        "request_id": fetch_articles.get("request_id"),
                        "work_item_id": fetch_articles.get("work_item_id"),
                        "tool_name": "fetch_articles",
                        "arguments": {"article_ids": article_ids},
                        "purpose": fetch_articles.get("purpose"),
                        "hypothesis_ids": fetch_articles.get("hypothesis_ids")
                        or [],
                    }
                )
        elif fetch_articles is None and context is not None:
            fetchable_article_ids = set(context.fetchable_article_ids)
            normalized.setdefault("tool_requests", []).extend(
                request
                for request in generic_fetch_requests
                if isinstance(request.get("arguments"), dict)
                and isinstance(
                    request["arguments"].get("article_ids"),
                    list,
                )
                and request["arguments"]["article_ids"]
                and all(
                    isinstance(article_id, str)
                    and article_id in fetchable_article_ids
                    for article_id in request["arguments"]["article_ids"]
                )
            )
    has_load_sidecar = "load_evidence" in normalized
    load_evidence = normalized.pop("load_evidence", None)
    if has_load_sidecar:
        normalized["tool_requests"] = [
            request
            for request in normalized.get("tool_requests") or []
            if not (
                isinstance(request, dict)
                and request.get("tool_name") == "load_evidence"
            )
        ]
        if isinstance(load_evidence, dict):
            if context is None:
                raise ModelProtocolError(
                    "load_evidence transport requires SolverContext"
                )
            aliases = _evidence_load_alias_map(context)
            evidence_ids = [
                aliases[evidence_ref]
                for key in sorted(load_evidence)
                if key.startswith("evidence_ref_")
                and isinstance(
                    evidence_ref := load_evidence.get(key),
                    str,
                )
                and evidence_ref in aliases
            ]
            normalized.setdefault("tool_requests", []).append(
                {
                    "request_id": load_evidence.get("request_id"),
                    "work_item_id": load_evidence.get("work_item_id"),
                    "tool_name": "load_evidence",
                    "arguments": {"evidence_ids": evidence_ids},
                    "purpose": load_evidence.get("purpose"),
                    "hypothesis_ids": load_evidence.get("hypothesis_ids") or [],
                }
            )
    normalized.setdefault("dependency_decisions", [])
    for request in normalized.get("tool_requests") or []:
        if not isinstance(request, dict):
            continue
        arguments = request.get("arguments")
        if not isinstance(arguments, dict):
            continue
        list_argument = {
            "load_evidence": "evidence_ids",
            "fetch_articles": "article_ids",
        }.get(request.get("tool_name"))
        if list_argument is None:
            continue
        values = arguments.get(list_argument)
        if isinstance(values, list):
            arguments[list_argument] = list(dict.fromkeys(values))
    if context is not None:
        _normalize_article_fetch_requests(normalized, context)
        _normalize_grounding_evidence_aliases(normalized, context)
        _normalize_legacy_hypothesis_gap_replacements(normalized, context)
    for decision in normalized.get("dependency_decisions") or []:
        if not isinstance(decision, dict):
            continue
        decision["status"] = {
            "terminal_text_missing": "needs_action",
            "terminal_text_confirmed": "resolved",
        }.get(decision.get("status"), decision.get("status"))
    return normalized


def _normalize_legacy_hypothesis_gap_replacements(
    payload: dict[str, Any],
    context: SolverContext,
) -> None:
    """保存済み旧fixtureのgaps全置換を正規の差分契約へ移す。"""

    existing_by_id = {
        item.hypothesis_id: item for item in context.hypotheses
    }
    for update in payload.get("update_hypotheses") or []:
        if not isinstance(update, dict) or "gaps" not in update:
            continue
        if "add_gaps" in update or "resolve_gap_ids" in update:
            raise ModelProtocolError(
                "hypothesis update cannot mix legacy gaps with gap diffs"
            )
        existing = existing_by_id.get(update.get("hypothesis_id"))
        desired = update.pop("gaps")
        if existing is None or not isinstance(desired, list):
            continue
        desired_descriptions = tuple(
            item for item in desired if isinstance(item, str)
        )
        desired_set = set(desired_descriptions)
        update["resolve_gap_ids"] = [
            gap.gap_id
            for gap in structured_hypothesis_gaps(
                existing.hypothesis_id,
                existing.gaps,
            )
            if gap.description not in desired_set
        ]
        existing_set = set(existing.gaps)
        update["add_gaps"] = [
            {"description": description}
            for description in desired_descriptions
            if description not in existing_set
        ]


def _normalize_grounding_evidence_aliases(
    payload: dict[str, Any],
    context: SolverContext,
) -> None:
    """取得済みArticle ID表現を、実在するgrounding Evidence IDへ展開する。"""

    grounding_ids = set(context.grounding_evidence_ids)
    evidence_ids_by_article: dict[str, list[str]] = {}
    for evidence in context.material_evidence:
        if evidence.evidence_id not in grounding_ids:
            continue
        article_id = evidence.metadata.get("articleId")
        if isinstance(article_id, str):
            evidence_ids_by_article.setdefault(article_id, []).append(
                evidence.evidence_id
            )

    def normalize_ids(values: Any) -> Any:
        if not isinstance(values, list):
            return values
        result: list[str] = []
        for value in values:
            replacements = (
                [value]
                if value in grounding_ids
                else evidence_ids_by_article.get(value, [value])
            )
            for replacement in replacements:
                if replacement not in result:
                    result.append(replacement)
        return result

    if "retain_evidence_ids" in payload:
        payload["retain_evidence_ids"] = normalize_ids(
            payload.get("retain_evidence_ids")
        )
    for decision in payload.get("dependency_decisions") or []:
        if isinstance(decision, dict) and "basis_evidence_ids" in decision:
            decision["basis_evidence_ids"] = normalize_ids(
                decision.get("basis_evidence_ids")
            )
    update = payload.get("update")
    update_hypotheses = (
        update.get("update_hypotheses")
        if isinstance(update, dict)
        else payload.get("update_hypotheses")
    )
    for hypothesis in update_hypotheses or []:
        if isinstance(hypothesis, dict) and "evidence_ids" in hypothesis:
            hypothesis["evidence_ids"] = normalize_ids(
                hypothesis.get("evidence_ids")
            )
    answer = payload.get("answer")
    if isinstance(answer, dict) and "citation_ids" in answer:
        answer["citation_ids"] = normalize_ids(answer.get("citation_ids"))


def _downgrade_unproven_dependency_confirmations(
    assessment: DependencyAssessmentDecision,
    *,
    article_id_by_evidence: Mapping[str, str],
) -> DependencyAssessmentDecision:
    """異なるArticle本文がない確認済み判断だけを構造違反として戻す。"""

    decisions: list[DependencyDecision] = []
    for decision in assessment.dependency_decisions:
        if decision.status != "resolved":
            decisions.append(decision)
            continue
        article_ids = {
            article_id_by_evidence[evidence_id]
            for evidence_id in decision.basis_evidence_ids
            if evidence_id in article_id_by_evidence
        }
        if len(article_ids) >= 2:
            decisions.append(decision)
            continue
        decisions.append(
            decision.model_copy(
                update={
                    "status": "needs_action",
                    "reason": "委任元と末端下位規範を示す異なるArticle本文が揃っていないため、末端本文は未確認。",
                    "action_request_id": None,
                }
            )
        )
    return assessment.model_copy(
        update={"dependency_decisions": tuple(decisions)}
    )


def _derive_observation_work_item_updates(
    context: SolverContext,
    observation: ObservationIntegrationDecision,
) -> ObservationIntegrationDecision:
    """Hypothesisと下位規範状態からWorkItem完了差分を機械導出する。"""

    hypothesis_only = observation.model_copy(update={"update_work_items": ()})
    work_items, hypotheses = _project_observation_integration_state(
        context,
        hypothesis_only,
    )
    hypotheses_by_work_item: dict[str, list[Any]] = {}
    for hypothesis in hypotheses:
        hypotheses_by_work_item.setdefault(hypothesis.work_item_id, []).append(
            hypothesis
        )

    dependencies = _merged_dependency_decisions(
        context.dependency_decisions,
        observation.dependency_decisions,
    )
    needs_action_ids = {
        item.work_item_id
        for item in dependencies
        if item.status == "needs_action"
    }
    deferred_frontier_work_item_ids = {
        item.work_item_id
        for item in context.graph_review_ledger
        if item.review_status == "relevant_deferred"
        and item.content_status in {"not_requested", "failed", "timeout"}
        and item.deferred_resolution_action != "no_longer_needed"
    }
    children_by_parent: dict[str, list[str]] = {}
    for item in work_items:
        if item.parent_work_item_id is not None and item.state != "dropped":
            children_by_parent.setdefault(item.parent_work_item_id, []).append(
                item.work_item_id
            )

    state_by_id = {item.work_item_id: item.state for item in work_items}
    updates_by_id: dict[str, WorkItemUpdate] = {}
    changed = True
    while changed:
        changed = False
        for item in reversed(work_items):
            if state_by_id[item.work_item_id] != "open":
                continue
            if item.work_item_id in needs_action_ids:
                continue
            if item.work_item_id in deferred_frontier_work_item_ids:
                continue

            child_ids = children_by_parent.get(item.work_item_id, [])
            if any(state_by_id[child_id] != "resolved" for child_id in child_ids):
                continue

            work_hypotheses = hypotheses_by_work_item.get(item.work_item_id, [])
            if not work_hypotheses and not child_ids:
                continue
            if any(
                hypothesis.judgment == "unresolved"
                or hypothesis.gaps
                or not hypothesis.evidence_ids
                for hypothesis in work_hypotheses
            ):
                continue

            basis_ids = tuple(
                hypothesis.hypothesis_id for hypothesis in work_hypotheses
            )
            updates_by_id[item.work_item_id] = WorkItemUpdate(
                work_item_id=item.work_item_id,
                state="resolved",
                resolution=(
                    "所属Hypothesisと下位規範確認の検証が完了した。"
                    if work_hypotheses
                    else "子WorkItemの検証が完了した。"
                ),
                basis_hypothesis_ids=basis_ids,
            )
            state_by_id[item.work_item_id] = "resolved"
            changed = True

    updates = tuple(
        updates_by_id[item.work_item_id]
        for item in work_items
        if item.work_item_id in updates_by_id
    )
    return observation.model_copy(update={"update_work_items": updates})


def _dependency_assessment_transport_schema(
    context: SolverContext,
) -> dict[str, Any]:
    evidence_ids = context.grounding_evidence_ids
    dependency_decision = _strict_object(
        {
            "dependency_kind": _described(
                {
                    "type": "string",
                    "enum": [context.required_dependency_kind],
                },
                DependencyDecision,
                "dependency_kind",
            ),
            "work_item_id": _described(
                _enum_string(context.required_dependency_work_item_ids),
                DependencyDecision,
                "work_item_id",
            ),
            "status": {
                "type": "string",
                "enum": [
                    "not_required",
                    "terminal_text_missing",
                    "terminal_text_confirmed",
                ],
                "description": (
                    "not_requiredは下位規範確認不要、"
                    "terminal_text_missingは末端下位規範本文が未確認、"
                    "terminal_text_confirmedは委任元とそれを具体化する"
                    "末端下位規範の本文を確認済み。同じWorkItemの"
                    "Hypothesisに下位規範で定める未確認内容をgapsとして"
                    "残す場合はterminal_text_missingを使う。"
                ),
            },
            "reason": _described(
                {"type": "string", "minLength": 1, "maxLength": 300},
                DependencyDecision,
                "reason",
            ),
            "basis_evidence_ids": {
                **_bounded_enum_array(evidence_ids),
                "description": (
                    "状態判断に使ったgrounding Evidence ID。"
                    "terminal_text_confirmedでは、委任元と末端下位規範を"
                    "それぞれ示すEvidenceをこの順で含め、両者の"
                    "metadata.articleIdが異なる必要がある。"
                    "terminal_text_missingでは、判断に使える本文がなければ"
                    "空配列でよい。"
                ),
            },
            "action_request_id": _described(
                {"type": "null"},
                DependencyDecision,
                "action_request_id",
            ),
        }
    )
    count = len(context.required_dependency_work_item_ids)
    return _strict_object(
        {
            "decision_reason": {
                "type": "string",
                "minLength": 1,
                "maxLength": 1200,
            },
            "dependency_decisions": {
                "type": "array",
                "items": dependency_decision,
                "minItems": count,
                "maxItems": count,
            },
        }
    )


def _cycle_close_transport_schema(
    context: SolverContext,
    observation: ObservationIntegrationDecision,
) -> dict[str, Any]:
    projected_work_items, _ = (
        _project_observation_integration_state(context, observation)
    )
    open_work_item_ids = tuple(
        item.work_item_id
        for item in projected_work_items
        if item.state == "open"
    )
    retainable_evidence_ids = _retainable_cycle_evidence_ids(
        context,
        observation,
    )
    required_transition = _required_cycle_close_transition(
        context,
        observation,
    )
    must_start_next_cycle = required_transition == "start_next_cycle"
    properties: dict[str, Any] = {
        "decision_reason": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1200,
            "description": CycleCloseDecision.model_fields[
                "decision_reason"
            ].description,
        },
        "next_focus_work_item_ids": {
            **(
                _bounded_enum_array(open_work_item_ids)
                if must_start_next_cycle
                else _empty_array_schema()
            ),
            "description": CycleCloseDecision.model_fields[
                "next_focus_work_item_ids"
            ].description,
        },
        "retain_evidence_ids": {
            **(
                _bounded_enum_array(
                    retainable_evidence_ids,
                    max_items=context.max_retained_evidence,
                )
                if must_start_next_cycle
                else _empty_array_schema()
            ),
            "description": CycleCloseDecision.model_fields[
                "retain_evidence_ids"
            ].description,
        },
    }
    active_deferred, _ = _cycle_close_deferred_frontiers(
        context,
        observation,
    )
    if active_deferred:
        properties["deferred_frontier_resolutions"] = {
            "type": "array",
            "items": _strict_object(
                {
                    "frontier_item_id": _enum_string(
                        tuple(item.frontier_item_id for item in active_deferred)
                    ),
                    "article_id": _enum_string(
                        tuple(item.article_id for item in active_deferred)
                    ),
                    "work_item_id": _enum_string(
                        tuple(item.work_item_id for item in active_deferred)
                    ),
                    "hypothesis_id": {
                        "anyOf": [
                            _enum_string(
                                tuple(
                                    item.hypothesis_id
                                    for item in active_deferred
                                    if item.hypothesis_id is not None
                                )
                            ),
                            {"type": "null"},
                        ]
                    },
                    "action": {
                        "type": "string",
                        "enum": [
                            "fetch_next_cycle",
                            "carry_forward",
                            "no_longer_needed",
                            "unresolved_at_limit",
                        ],
                    },
                    "reason": {"type": "string", "minLength": 1},
                }
            ),
            "minItems": len(active_deferred),
            "maxItems": len(active_deferred),
            "description": CycleCloseDecision.model_fields[
                "deferred_frontier_resolutions"
            ].description,
        }
    if context.graph_review_batch.remaining_unreviewed_count:
        properties["unreviewed_graph_resolution"] = {
            "anyOf": [
                _strict_object(
                    {
                        "action": {
                            "type": "string",
                            "enum": [
                                "review_next_cycle",
                                "no_longer_needed",
                                "unresolved_at_limit",
                            ],
                        },
                        "reason": {"type": "string", "minLength": 1},
                    }
                ),
                {"type": "null"},
            ],
            "description": CycleCloseDecision.model_fields[
                "unreviewed_graph_resolution"
            ].description,
        }
    return _strict_object(properties)


def _normalize_cycle_close_decisions(
    context: SolverContext,
    observation: ObservationIntegrationDecision,
    transition: CycleCloseDecision,
) -> SolverDecision:
    if _required_cycle_close_transition(context, observation) != "start_next_cycle":
        raise ValueError("final answers require the finalization profile")
    active_deferred, closed_work_item_frontiers = _cycle_close_deferred_frontiers(
        context,
        observation,
    )
    automatic_resolutions = tuple(
        DeferredFrontierResolution(
            frontier_item_id=item.frontier_item_id,
            article_id=item.article_id,
            work_item_id=item.work_item_id,
            hypothesis_id=item.hypothesis_id,
            action="no_longer_needed",
            reason=(
                "所属WorkItemがopenでないため、次Cycleへ引き継がない。"
            ),
        )
        for item in closed_work_item_frontiers
    )
    transition_resolutions = transition.deferred_frontier_resolutions
    resolved_frontier_ids = {
        item.frontier_item_id for item in transition_resolutions
    }
    transition_resolutions = (
        *transition_resolutions,
        *(
            DeferredFrontierResolution(
                frontier_item_id=item.frontier_item_id,
                article_id=item.article_id,
                work_item_id=item.work_item_id,
                hypothesis_id=item.hypothesis_id,
                action="carry_forward",
                reason="現在Cycleで本文取得せず、次Cycleへ引き継ぐ。",
            )
            for item in active_deferred
            if item.frontier_item_id not in resolved_frontier_ids
        ),
    )
    return SolverDecision(
        next="continue",
        decision_reason=transition.decision_reason,
        start_next_cycle=True,
        update=CaseUpdate(
            update_work_items=observation.update_work_items,
            update_hypotheses=observation.update_hypotheses,
        ),
        next_focus_work_item_ids=transition.next_focus_work_item_ids,
        retain_evidence_ids=transition.retain_evidence_ids,
        dependency_decisions=observation.dependency_decisions,
        deferred_frontier_resolutions=(
            *transition_resolutions,
            *automatic_resolutions,
        ),
        unreviewed_graph_resolution=transition.unreviewed_graph_resolution,
    )


def _cycle_close_deferred_frontiers(
    context: SolverContext,
    observation: ObservationIntegrationDecision,
) -> tuple[
    tuple[GraphReviewLedgerItem, ...],
    tuple[GraphReviewLedgerItem, ...],
]:
    """Cycle CloseでLLMが扱う保留候補と機械終了する候補を分ける。"""

    projected_work_items, _ = _project_observation_integration_state(
        context,
        observation,
    )
    open_work_item_ids = {
        item.work_item_id
        for item in projected_work_items
        if item.state == "open"
    }
    active_deferred = _all_active_deferred_frontiers(context)
    return (
        tuple(
            item
            for item in active_deferred
            if item.work_item_id in open_work_item_ids
        ),
        tuple(
            item
            for item in active_deferred
            if item.work_item_id not in open_work_item_ids
        ),
    )


def _required_cycle_close_transition(
    context: SolverContext,
    observation: ObservationIntegrationDecision,
) -> Literal["start_next_cycle", "finalize"]:
    """LLMが確定したWorkItem状態から、構造上のCycle遷移を導出する。"""

    projected_work_items, _ = _project_observation_integration_state(
        context,
        observation,
    )
    has_open_work = any(item.state == "open" for item in projected_work_items)
    if context.can_start_next_cycle and has_open_work:
        return "start_next_cycle"
    return "finalize"


def _cycle_close_checkpoint_timeout(
    message: str,
    *,
    observation: ObservationIntegrationDecision,
    completed_stage: str,
    input_tokens: int | None,
    output_tokens: int | None,
) -> SolverCheckpointTimeout:
    has_updates = bool(
        observation.update_work_items or observation.update_hypotheses
    )
    return SolverCheckpointTimeout(
        message,
        partial_decision=(
            SolverDecision(
                next="continue",
                decision_reason=(
                    f"{completed_stage}で確定した更新を保存し、後続処理の時間切れを"
                    "安全な最終化へ引き継ぐ。"
                ),
                update=CaseUpdate(
                    update_work_items=observation.update_work_items,
                    update_hypotheses=observation.update_hypotheses,
                ),
            )
            if has_updates
            else None
        ),
        completed_stage=completed_stage,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _sum_optional_tokens(*values: int | None) -> int | None:
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def render_search_assessment_model_call(
    context: SolverContext,
    profile: ModelCallProfile,
    *,
    provider: str | None = None,
) -> RenderedModelCall:
    input_payload = _search_review_context_payload(context)
    instructions = (
        f"{profile.system_prompt}\n\n"
        f"{render_model_input_glossary(SearchCandidateContentAssessmentInput)}\n\n"
        f"{RUNTIME_INPUT_MARKER}"
        f"{_post_context_completion_check(profile.completion_check_prompt)}"
    )
    rendered = build_rendered_model_call(
        stage="search_assessment",
        instructions=instructions,
        input_tag="solver_context",
        input_payload=input_payload,
        output_schema=_search_review_transport_schema(
            context,
            array_transport=False,
        ),
        normalized_schema=SearchAssessmentDecision.model_json_schema(),
    )
    _ensure_solver_prompt_capacity(rendered.request, context.max_solver_input_chars)
    return rendered


def _search_review_prompt(
    context: SolverContext,
    system_prompt: str,
    *,
    completion_check_prompt: str | None = None,
) -> str:
    profile = ModelCallProfile(
        model="artifact-render-only",
        system_prompt=system_prompt,
        completion_check_prompt=completion_check_prompt,
    )
    return render_search_assessment_model_call(context, profile).request


def _search_review_context_payload(
    context: SolverContext,
) -> dict[str, Any]:
    """Hypothesisを混ぜず、候補自身の検索抜粋だけを投影する。"""

    evidence_by_id = {
        item.evidence_id: item for item in context.material_evidence
    }
    input_model = SearchCandidateContentAssessmentInput(
        search_candidates=tuple(
            SearchAssessmentCandidate(
                article_id=candidate.article_id,
                title=candidate.title,
                headings=candidate.headings,
                search_excerpts=tuple(
                    SearchAssessmentExcerpt(
                        content=evidence_by_id[evidence_id].content,
                    )
                    for evidence_id in candidate.navigation_evidence_ids
                    if evidence_id in evidence_by_id
                ),
            )
            for candidate in context.search_candidates
        ),
    )
    return input_model.model_dump(mode="json")


def render_search_selection_model_call(
    context: SolverContext,
    profile: ModelCallProfile,
) -> RenderedModelCall:
    """検索結果の理解と本文取得対象の選択を同じ呼出しへまとめる。"""

    input_payload = _search_selection_context_payload(context)
    instructions = (
        f"{profile.system_prompt}\n\n"
        f"{render_model_input_glossary(SearchSelectionInput)}\n\n"
        f"{RUNTIME_INPUT_MARKER}"
        f"{_post_context_completion_check(profile.completion_check_prompt)}"
    )
    rendered = build_rendered_model_call(
        stage="search_selection",
        instructions=instructions,
        input_tag="search_selection_input",
        input_payload=input_payload,
        output_schema=_search_selection_transport_schema(context),
        normalized_schema=SearchCandidateReview.model_json_schema(),
    )
    _ensure_solver_prompt_capacity(rendered.request, context.max_solver_input_chars)
    return rendered


def _search_selection_context_payload(context: SolverContext) -> dict[str, Any]:
    evidence_by_id = {
        item.evidence_id: item for item in context.material_evidence
    }
    open_work_item_ids = {
        item.work_item_id for item in context.work_tree if item.state == "open"
    }
    selectable_work_item_ids = {
        work_item_id
        for work_item_id in open_work_item_ids
        if context.remaining_fetch_capacity_by_work_item.get(
            work_item_id,
            context.remaining_fetch_capacity,
        )
        > 0
    }
    active_hypotheses = tuple(
        item
        for item in context.hypotheses
        if item.requires_follow_up
        and item.work_item_id in selectable_work_item_ids
    )
    active_work_item_ids = {
        item.work_item_id for item in active_hypotheses
    }
    input_model = SearchSelectionInput(
        non_work_item_requirements=context.non_work_item_requirements,
        work_tree=tuple(
            {
                "work_item_id": item.work_item_id,
                "question": item.question,
            }
            for item in context.work_tree
            if item.work_item_id in active_work_item_ids
        ),
        hypotheses=tuple(
            {
                "hypothesis_id": item.hypothesis_id,
                "work_item_id": item.work_item_id,
                "statement": item.statement,
                "gaps": item.gaps,
            }
            for item in active_hypotheses
        ),
        search_candidates=tuple(
            SearchAssessmentCandidate(
                article_id=candidate.article_id,
                title=candidate.title,
                headings=candidate.headings,
                search_excerpts=tuple(
                    SearchAssessmentExcerpt(
                        content=evidence_by_id[evidence_id].content,
                    )
                    for evidence_id in candidate.navigation_evidence_ids
                    if evidence_id in evidence_by_id
                ),
            )
            for candidate in context.search_candidates
        ),
        current_fetch_request_capacity=_search_selection_capacity(context),
        remaining_fetch_capacity_by_work_item=(
            context.remaining_fetch_capacity_by_work_item
        ),
    )
    return input_model.model_dump(mode="json")


def _search_review_batch_contexts(
    context: SolverContext,
) -> tuple[SolverContext, ...]:
    """候補の内容評価を小さな独立単位に分ける。"""

    candidates = context.search_candidates
    return tuple(
        context.model_copy(
            update={"search_candidates": candidates[offset:offset + _SEARCH_REVIEW_BATCH_SIZE]}
        )
        for offset in range(0, len(candidates), _SEARCH_REVIEW_BATCH_SIZE)
    )


def render_search_reselection_model_call(
    context: SolverContext,
    assessment_payload: dict[str, Any],
    profile: ModelCallProfile,
) -> RenderedModelCall:
    if profile.followup_system_prompt is None:
        raise ModelProtocolError("search reselection prompt is unavailable")
    eligible_assessments = []
    all_hypothesis_ids = tuple(
        item.hypothesis_id for item in context.hypotheses
    )
    eligible_hypothesis_ids_by_article: dict[str, tuple[str, ...]] = {}
    for item in assessment_payload.get("assessments") or []:
        if not isinstance(item, dict):
            continue
        article_id = item.get("article_id")
        if not isinstance(article_id, str):
            continue
        eligible_hypothesis_ids_by_article[article_id] = all_hypothesis_ids
        eligible_assessments.append(
            {
                "article_id": article_id,
                "legal_function": item.get("legal_function"),
                "summary": item.get("summary"),
            }
        )
    eligible_article_ids = tuple(
        item["article_id"]
        for item in eligible_assessments
        if isinstance(item.get("article_id"), str)
    )
    candidate_by_id = {
        item.article_id: item for item in context.search_candidates
    }
    current_fetch_request_capacity = _search_selection_capacity(context)
    input_payload = {
        "question": context.question,
        "work_items": [
            {
                "work_item_id": item.work_item_id,
                "question": item.question,
            }
            for item in context.work_tree
            if item.work_item_id
            in {hypothesis.work_item_id for hypothesis in context.hypotheses}
        ],
        "hypotheses": [
            item.model_dump(mode="json") for item in context.hypotheses
        ],
        "current_fetch_request_capacity": current_fetch_request_capacity,
        "remaining_fetch_capacity_by_work_item": (
            context.remaining_fetch_capacity_by_work_item
        ),
        "assessments": [
            {
                **item,
                "title": candidate_by_id[item["article_id"]].title,
                "headings": list(
                    candidate_by_id[item["article_id"]].headings
                ),
            }
            for item in eligible_assessments
            if item.get("article_id") in candidate_by_id
        ],
    }
    instructions = (
        f"{profile.followup_system_prompt}\n\n"
        f"{RUNTIME_INPUT_MARKER}"
        f"{_post_context_completion_check(profile.followup_completion_check_prompt)}"
    )
    rendered = build_rendered_model_call(
        stage="search_reselection",
        instructions=instructions,
        input_tag="search_review_summary",
        input_payload=input_payload,
        output_schema=_search_reselection_transport_schema(
            context,
            candidate_ids=eligible_article_ids,
            hypothesis_ids_by_article=eligible_hypothesis_ids_by_article,
            selection_limit=current_fetch_request_capacity,
        ),
        normalized_schema=SearchReselectionDecision.model_json_schema(),
    )
    _ensure_solver_prompt_capacity(rendered.request, context.max_solver_input_chars)
    return rendered


def _search_reselection_prompt(
    context: SolverContext,
    assessment_payload: dict[str, Any],
    system_prompt: str,
    *,
    completion_check_prompt: str | None = None,
) -> str:
    profile = ModelCallProfile(
        model="artifact-render-only",
        system_prompt="search-assessment-unused",
        followup_system_prompt=system_prompt,
        followup_completion_check_prompt=completion_check_prompt,
    )
    return render_search_reselection_model_call(
        context,
        assessment_payload,
        profile,
    ).request


def _post_context_completion_check(prompt: str | None) -> str:
    """長い入力の後で、現在処理の完了条件だけを再提示する。"""

    if prompt is None or not prompt.strip():
        return ""
    return f"\n\n{prompt.strip()}"


def _ensure_solver_prompt_capacity(prompt: str, max_input_chars: int) -> None:
    if len(prompt) > max_input_chars:
        raise ContextCapacityExceeded(
            "context_capacity_exceeded: solver prompt exceeds "
            "max_solver_input_chars"
        )


def render_reviewer_model_call(
    context: ReviewerView,
    profile: ReviewerProfile,
) -> RenderedModelCall:
    instructions = (
        f"{profile.system_prompt}\n\n"
        "以下のReviewerViewだけを確認し、"
        "ReviewResultだけを返してください。\n"
        f"{RUNTIME_INPUT_MARKER}"
    )
    return build_rendered_model_call(
        stage="reviewer",
        instructions=instructions,
        input_tag="reviewer_view",
        input_payload=context.model_dump(mode="json"),
        output_schema=ReviewResult.model_json_schema(),
        normalized_schema=ReviewResult.model_json_schema(),
    )


def _review_prompt(context: ReviewerView, system_prompt: str) -> str:
    profile = ReviewerProfile(
        model="artifact-render-only",
        system_prompt=system_prompt,
    )
    return render_reviewer_model_call(context, profile).request


_TRANSPORT_REPAIR_SECTIONS = (
    "finalize_requires_answer",
    "continue_requires_action",
    "article_fetch_limit",
    "hypothesis_requires_evidence",
)

_CONTRACT_REPAIR_SECTIONS = (
    "review_finding_resolution",
    "unknown_evidence",
    "hypothesis_requires_evidence",
    "navigation_only_evidence",
    "unknown_article_id",
    "open_work_item",
    "work_item_hypothesis_alignment",
    "cycle_boundary",
    "resolved_dependency",
    "dependency_decision",
    "retained_evidence_limit",
    "tool_request_limit",
    "unique_tool_request_ids",
    "article_fetch_contract",
    "known_references",
    "graph_review",
    "citation_coverage",
)


def render_solver_transport_repair_model_call(
    context: SolverContext,
    *,
    base_call: RenderedModelCall,
    payload: dict[str, Any] | None,
    error: ModelProtocolError | ValidationError,
) -> RenderedModelCall:
    """輸送修復も固定指示と動的な違反情報へ分離する。"""

    section_names = _TRANSPORT_REPAIR_SECTIONS
    rules = "\n".join(
        f"### {section_name}\n"
        f"{render_prompt_section('solver_transport_repair.md', section_name)}"
        for section_name in section_names
    )
    fixed_repair_instructions = (
        f"{render_prompt_section('solver_transport_repair.md', 'stable')}\n"
        f"<transport_repair_rules>\n{rules}\n</transport_repair_rules>"
    )
    instructions = f"{base_call.instructions}\n\n{fixed_repair_instructions}"
    input_payload = dict(base_call.input_payload)
    input_payload["transport_repair"] = {
        "validation_error": _transport_error_detail(error),
        "previous_solver_decision": payload,
    }
    prompt_assets = (
        *base_call.prompt_assets,
        prompt_asset_trace(
            "solver_transport_repair.md",
            ("stable", *section_names),
        ),
    )
    output_schema = deepcopy(base_call.output_schema)
    tool_requests = output_schema.get("properties", {}).get("tool_requests")
    error_detail = _transport_error_detail(error)
    if (
        isinstance(tool_requests, dict)
        and "identical legal_graph_neighbors arguments must be consolidated"
        in error_detail
    ):
        tool_requests["maxItems"] = 1
    rendered = build_rendered_model_call(
        stage="solver_transport_repair",
        instructions=instructions,
        input_tag=base_call.input_tag,
        input_payload=input_payload,
        output_schema=output_schema,
        normalized_schema=base_call.normalized_schema,
        prompt_assets=prompt_assets,
    )
    _ensure_solver_prompt_capacity(rendered.request, context.max_solver_input_chars)
    return rendered


def render_hypothesis_revision_transport_repair_model_call(
    context: SolverContext,
    *,
    base_call: RenderedModelCall,
    payload: dict[str, Any] | None,
    error: ModelProtocolError | ValidationError,
) -> RenderedModelCall:
    """仮説見直しの意味を変えず、輸送違反だけを1回修復させる。"""

    instructions = (
        f"{base_call.instructions}\n\n"
        f"{render_prompt_section('solver_transport_repair.md', 'stable')}"
    )
    input_payload = dict(base_call.input_payload)
    input_payload["transport_repair"] = {
        "validation_error": _transport_error_detail(error),
        "previous_solver_decision": payload,
    }
    rendered = build_rendered_model_call(
        stage="hypothesis_revision_transport_repair",
        instructions=instructions,
        input_tag=base_call.input_tag,
        input_payload=input_payload,
        output_schema=deepcopy(base_call.output_schema),
        normalized_schema=base_call.normalized_schema,
        prompt_assets=(
            *base_call.prompt_assets,
            prompt_asset_trace("solver_transport_repair.md", ("stable",)),
        ),
    )
    _ensure_solver_prompt_capacity(rendered.request, context.max_solver_input_chars)
    return rendered


def _render_staged_research_repair_model_call(
    context: SolverContext,
    *,
    base_call: RenderedModelCall,
    payload: dict[str, Any] | None,
    error: ModelProtocolError | ValidationError,
) -> RenderedModelCall:
    """単一責務Stepでは、そのStepの契約だけを使って再出力させる。"""

    instructions = (
        f"{base_call.instructions}\n\n"
        "<contract_repair>\n"
        "直前の出力は次の構造違反により未適用です。意味内容を保ちながら、"
        "現在の出力schemaに一致する完全な出力を返してください。\n"
        f"{_transport_error_detail(error)}\n"
        "</contract_repair>"
    )
    input_payload = dict(base_call.input_payload)
    input_payload["previous_invalid_output"] = payload
    rendered = build_rendered_model_call(
        stage=f"{base_call.stage}_contract_repair",
        instructions=instructions,
        input_tag=base_call.input_tag,
        input_payload=input_payload,
        output_schema=base_call.output_schema,
        normalized_schema=base_call.normalized_schema,
        prompt_assets=base_call.prompt_assets,
    )
    _ensure_solver_prompt_capacity(rendered.request, context.max_solver_input_chars)
    return rendered


def _validate_hypothesis_update_evidence(
    decision: SolverDecision,
    context: SolverContext,
) -> None:
    """状態適用後に必ず失敗するHypothesis更新を輸送修復へ戻す。"""

    existing_evidence_by_hypothesis = {
        item.hypothesis_id: item.evidence_ids for item in context.hypotheses
    }
    if any(
        item.judgment in {"supported", "contradicted"}
        and not item.evidence_ids
        and not existing_evidence_by_hypothesis.get(item.hypothesis_id)
        for item in decision.update.update_hypotheses
    ):
        raise ModelProtocolError(
            "supported or contradicted hypothesis requires evidence"
        )


def _transport_error_detail(
    error: ModelProtocolError | ValidationError,
) -> str:
    if isinstance(error, ValidationError):
        return json.dumps(
            error.errors(include_url=False, include_input=False),
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    return str(error)


def _solver_prompt_assets(context: SolverContext) -> tuple[PromptAssetTrace, ...]:
    if context.contract_feedback is None:
        return ()
    section_names = ["contract_feedback_rule"]
    section_names.extend(_CONTRACT_REPAIR_SECTIONS)
    return (
        prompt_asset_trace(
            "solver_contract_repair.md",
            tuple(section_names),
        ),
    )


def _solver_transport_schema(context: SolverContext) -> dict:
    string_array = {"type": "array", "items": {"type": "string"}}
    open_work_item_ids = tuple(
        item.work_item_id
        for item in context.work_tree
        if item.state == "open"
    )
    open_work_item_id_set = set(open_work_item_ids)
    unresolved_hypothesis_ids = tuple(
        item.hypothesis_id
        for item in context.hypotheses
        if item.work_item_id in open_work_item_id_set
        and item.judgment == "unresolved"
    )
    finalization_limitations = dict(string_array)
    finalization_work_items = string_array
    finalization_hypotheses = string_array
    if context.finalize_only:
        if open_work_item_ids:
            finalization_limitations = {
                **string_array,
                "minItems": 1,
            }
        finalization_work_items = {
            **_bounded_enum_array(open_work_item_ids),
            "minItems": len(open_work_item_ids),
        }
        finalization_hypotheses = {
            **_bounded_enum_array(unresolved_hypothesis_ids),
            "minItems": len(unresolved_hypothesis_ids),
        }
    answer = _strict_object(
        {
            "text": _described({"type": "string"}, FinalAnswer, "text"),
            "selected_option_id": _described(
                (
                    _enum_string(
                        tuple(item.option_id for item in context.answer_options)
                    )
                    if context.answer_options
                    else {"type": "null"}
                ),
                FinalAnswer,
                "selected_option_id",
            ),
            "citation_ids": _described(
                string_array,
                FinalAnswer,
                "citation_ids",
            ),
            "limitations": _described(
                finalization_limitations,
                FinalAnswer,
                "limitations",
            ),
            "unresolved_work_item_ids": _described(
                finalization_work_items,
                FinalAnswer,
                "unresolved_work_item_ids",
            ),
            "unresolved_hypothesis_ids": _described(
                finalization_hypotheses,
                FinalAnswer,
                "unresolved_hypothesis_ids",
            ),
        }
    )
    required_dependency_kind = context.required_dependency_kind
    required_dependency_work_item_ids = context.required_dependency_work_item_ids
    dependency_decision = _strict_object(
        {
            "dependency_kind": _described(
                (
                    {"type": "string", "enum": [required_dependency_kind]}
                    if required_dependency_kind is not None
                    else {"type": "string"}
                ),
                DependencyDecision,
                "dependency_kind",
            ),
            "work_item_id": _described(
                _enum_string(required_dependency_work_item_ids),
                DependencyDecision,
                "work_item_id",
            ),
            "status": _described(
                {
                    "type": "string",
                    "enum": ["not_required", "needs_action", "resolved"],
                },
                DependencyDecision,
                "status",
            ),
            "reason": _described(
                {"type": "string", "minLength": 1},
                DependencyDecision,
                "reason",
            ),
            "basis_evidence_ids": _described(
                _bounded_enum_array(context.grounding_evidence_ids),
                DependencyDecision,
                "basis_evidence_ids",
            ),
            "action_request_id": _described(
                {"anyOf": [{"type": "string"}, {"type": "null"}]},
                DependencyDecision,
                "action_request_id",
            ),
        }
    )
    required_dependency_count = len(required_dependency_work_item_ids)
    dependency_decisions = (
        {
            "type": "array",
            "items": dependency_decision,
            "minItems": required_dependency_count,
            "maxItems": required_dependency_count,
        }
        if required_dependency_count
        else _empty_array_schema()
    )
    review_finding_ids = tuple(
        item.finding_id for item in context.reviewer_findings
    )
    review_finding_resolution = _strict_object(
        {
            "finding_id": _described(
                _enum_string(review_finding_ids),
                ReviewFindingResolution,
                "finding_id",
            ),
            "outcome": _described(
                {
                    "type": "string",
                    "enum": ["addressed", "disputed"],
                },
                ReviewFindingResolution,
                "outcome",
            ),
            "reason": _described(
                {"type": "string", "minLength": 1},
                ReviewFindingResolution,
                "reason",
            ),
            "basis_evidence_ids": _described(
                _bounded_enum_array(context.grounding_evidence_ids),
                ReviewFindingResolution,
                "basis_evidence_ids",
            ),
        }
    )
    review_finding_resolutions = (
        {
            "type": "array",
            "items": review_finding_resolution,
            "minItems": len(review_finding_ids),
            "maxItems": len(review_finding_ids),
        }
        if review_finding_ids
        else _empty_array_schema()
    )
    batch_candidates = context.graph_review_batch.candidates
    graph_review_mode = bool(batch_candidates and not context.finalize_only)
    selection_mode = graph_review_mode
    batch_frontier_ids = tuple(item.frontier_item_id for item in batch_candidates)
    selectable_frontier_ids = batch_frontier_ids
    graph_article_ids = tuple(
        dict.fromkeys(item.article_id for item in batch_candidates)
    )
    graph_work_item_ids = tuple(
        dict.fromkeys(item.work_item_id for item in batch_candidates)
    )
    graph_hypothesis_ids = tuple(
        dict.fromkeys(
            item.hypothesis_id
            for item in batch_candidates
            if item.hypothesis_id is not None
        )
    )
    graph_frontier_decision = _strict_object(
        {
            "frontier_item_id": _described(
                _enum_string(selectable_frontier_ids),
                GraphFrontierDecision,
                "frontier_item_id",
            ),
            "article_id": _described(
                _enum_string(graph_article_ids),
                GraphFrontierDecision,
                "article_id",
            ),
            "work_item_id": _described(
                _enum_string(graph_work_item_ids),
                GraphFrontierDecision,
                "work_item_id",
            ),
            "hypothesis_id": _described(
                {
                    "anyOf": [
                        _enum_string(graph_hypothesis_ids),
                        {"type": "null"},
                    ]
                },
                GraphFrontierDecision,
                "hypothesis_id",
            ),
            "action": _described(
                {
                    "type": "string",
                    "enum": ["select", "defer", "reject"],
                },
                GraphFrontierDecision,
                "action",
            ),
            "reason": _described(
                {"type": "string", "minLength": 1, "maxLength": 300},
                GraphFrontierDecision,
                "reason",
            ),
        }
    )
    reviewed_link_ids = tuple(
        dict.fromkeys(
            link.link_id
            for item in batch_candidates
            for link in item.links
        )
    )
    graph_candidate_review = _strict_object(
        {
            "graph_request_ids": _described(
                {
                    "type": "array",
                    "items": _enum_string(
                        context.required_graph_review_request_ids
                    ),
                    "minItems": len(context.required_graph_review_request_ids),
                    "maxItems": len(context.required_graph_review_request_ids),
                },
                GraphCandidateReview,
                "graph_request_ids",
            ),
            "reviewed_link_ids": _described(
                {
                    "type": "array",
                    "items": _enum_string(reviewed_link_ids),
                    "minItems": len(reviewed_link_ids),
                    "maxItems": len(reviewed_link_ids),
                },
                GraphCandidateReview,
                "reviewed_link_ids",
            ),
            "frontier_decisions": _described(
                {
                    "type": "array",
                    "items": graph_frontier_decision,
                    "minItems": len(batch_frontier_ids),
                    "maxItems": len(batch_frontier_ids),
                },
                GraphCandidateReview,
                "frontier_decisions",
            ),
            "reason": _described(
                {"type": "string", "minLength": 1, "maxLength": 400},
                GraphCandidateReview,
                "reason",
            ),
        }
    )
    known_ledger_article_ids = tuple(
        dict.fromkeys(item.article_id for item in context.graph_review_ledger)
    )
    open_work_item_ids = tuple(
        item.work_item_id for item in context.work_tree if item.state == "open"
    )
    open_hypothesis_ids = tuple(
        item.hypothesis_id
        for item in context.hypotheses
        if item.work_item_id in open_work_item_ids
    )
    frontier_re_adoption = _strict_object(
        {
            "article_id": _described(
                _enum_string(known_ledger_article_ids),
                FrontierReAdoption,
                "article_id",
            ),
            "work_item_id": _described(
                _enum_string(open_work_item_ids),
                FrontierReAdoption,
                "work_item_id",
            ),
            "hypothesis_id": _described(
                _enum_string(open_hypothesis_ids),
                FrontierReAdoption,
                "hypothesis_id",
            ),
            "reason": _described(
                {"type": "string"},
                FrontierReAdoption,
                "reason",
            ),
        }
    )
    all_active_deferred = _all_active_deferred_frontiers(context)
    active_deferred = _llm_active_deferred_frontiers(context)
    deferred_frontier_resolution = _strict_object(
        {
            "frontier_item_id": _described(
                _enum_string(
                    tuple(item.frontier_item_id for item in active_deferred)
                ),
                DeferredFrontierResolution,
                "frontier_item_id",
            ),
            "article_id": _described(
                _enum_string(
                    tuple(
                        dict.fromkeys(item.article_id for item in active_deferred)
                    )
                ),
                DeferredFrontierResolution,
                "article_id",
            ),
            "work_item_id": _described(
                _enum_string(
                    tuple(
                        dict.fromkeys(
                            item.work_item_id for item in active_deferred
                        )
                    )
                ),
                DeferredFrontierResolution,
                "work_item_id",
            ),
            "hypothesis_id": _described(
                {
                    "anyOf": [
                        _enum_string(
                            tuple(
                                dict.fromkeys(
                                    item.hypothesis_id
                                    for item in active_deferred
                                    if item.hypothesis_id is not None
                                )
                            )
                        ),
                        {"type": "null"},
                    ]
                },
                DeferredFrontierResolution,
                "hypothesis_id",
            ),
            "action": _described(
                {
                    "type": "string",
                    "enum": [
                        "fetch_next_cycle",
                        "carry_forward",
                        "no_longer_needed",
                        "unresolved_at_limit",
                    ],
                },
                DeferredFrontierResolution,
                "action",
            ),
            "reason": _described(
                {"type": "string"},
                DeferredFrontierResolution,
                "reason",
            ),
        }
    )
    force_next_cycle_repair = _preserve_previous_update_for_cycle_repair(context)
    force_continue_repair = _force_continue_after_open_finalize_repair(context)
    tool_requests_forbidden = (
        selection_mode
        or context.finalize_only
        or context.cycle_close_required
        or force_next_cycle_repair
    )
    preserve_previous_update = _preserve_previous_update_for_contract_repair(
        context
    )
    repair_update_json: str | None = None
    if preserve_previous_update or selection_mode:
        repair_update_json = "{}"
    repair_open_work_item_ids: tuple[str, ...] = ()
    if context.contract_feedback is not None:
        repair_states = {
            item.work_item_id: item.state for item in context.work_tree
        }
        for item in context.contract_feedback.previous_decision.update.add_work_items:
            repair_states[item.work_item_id] = item.state
        for item in context.contract_feedback.previous_decision.update.update_work_items:
            if item.work_item_id in repair_states:
                repair_states[item.work_item_id] = item.state
        repair_open_work_item_ids = tuple(
            work_item_id
            for work_item_id, state in repair_states.items()
            if state == "open"
        )
    unreviewed_graph_action_values = (
        ["review_next_cycle"]
        if force_next_cycle_repair
        else [
            "review_next_cycle",
            "no_longer_needed",
            "unresolved_at_limit",
        ]
    )
    unreviewed_graph_resolution = _strict_object(
        {
            "action": _described(
                {
                    "type": "string",
                    "enum": unreviewed_graph_action_values,
                },
                UnreviewedGraphResolution,
                "action",
            ),
            "reason": _described(
                {"type": "string"},
                UnreviewedGraphResolution,
                "reason",
            ),
        }
    )
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "next": _described(
                {
                    "type": "string",
                    "enum": (
                        ["continue"]
                        if selection_mode
                        or force_next_cycle_repair
                        or force_continue_repair
                        else ["continue", "finalize"]
                    ),
                },
                SolverDecision,
                "next",
            ),
            "decision_reason": _described(
                {"type": "string"},
                SolverDecision,
                "decision_reason",
            ),
            "start_next_cycle": _described(
                (
                    {"type": "boolean", "enum": [False]}
                    if selection_mode or force_continue_repair
                    else (
                        {"type": "boolean", "enum": [True]}
                        if force_next_cycle_repair
                        else {"type": "boolean"}
                    )
                ),
                SolverDecision,
                "start_next_cycle",
            ),
            "update_json": {
                "type": "string",
                "description": "CaseUpdate encoded as one JSON object string",
                **(
                    {"enum": [repair_update_json]}
                    if repair_update_json is not None
                    else {}
                ),
            },
            "next_focus_work_item_ids": _described(
                (
                    _empty_array_schema()
                    if selection_mode
                    else (
                        _bounded_enum_array(repair_open_work_item_ids)
                        if context.contract_feedback is not None
                        else string_array
                    )
                ),
                SolverDecision,
                "next_focus_work_item_ids",
            ),
            "retain_evidence_ids": _described(
                (
                    _empty_array_schema()
                    if selection_mode
                    else _bounded_enum_array(
                        tuple(
                            item.evidence_id
                            for item in context.evidence_manifest
                        ),
                        max_items=context.max_retained_evidence,
                    )
                ),
                SolverDecision,
                "retain_evidence_ids",
            ),
            "review_finding_resolutions": _described(
                review_finding_resolutions,
                SolverDecision,
                "review_finding_resolutions",
            ),
            "tool_requests_json": {
                "type": "string",
                "description": "ToolRequest array encoded as one JSON array string",
                **({"enum": ["[]"]} if tool_requests_forbidden else {}),
            },
            "dependency_decisions": _described(
                dependency_decisions,
                SolverDecision,
                "dependency_decisions",
            ),
            "graph_candidate_review": (
                _described(
                    graph_candidate_review,
                    SolverDecision,
                    "graph_candidate_review",
                )
                if graph_review_mode
                else _described(
                    {"type": "null"},
                    SolverDecision,
                    "graph_candidate_review",
                )
            ),
            "search_candidate_review": _described(
                {"type": "null"},
                SolverDecision,
                "search_candidate_review",
            ),
            "frontier_re_adoptions": _described(
                (
                    _empty_array_schema()
                    if selection_mode
                    else {
                        "type": "array",
                        "items": frontier_re_adoption,
                        "maxItems": len(context.graph_review_ledger),
                    }
                ),
                SolverDecision,
                "frontier_re_adoptions",
            ),
            "deferred_frontier_resolutions": _described(
                (
                    _empty_array_schema()
                    if selection_mode
                    else {
                        "type": "array",
                        "items": deferred_frontier_resolution,
                        **(
                            {"minItems": len(active_deferred)}
                            if context.finalize_only
                            else {}
                        ),
                        "maxItems": len(active_deferred),
                    }
                ),
                SolverDecision,
                "deferred_frontier_resolutions",
            ),
            "unreviewed_graph_resolution": (
                _described(
                    {"type": "null"},
                    SolverDecision,
                    "unreviewed_graph_resolution",
                )
                if selection_mode
                or context.graph_review_batch.remaining_unreviewed_count == 0
                else _described(
                    unreviewed_graph_resolution,
                    SolverDecision,
                    "unreviewed_graph_resolution",
                )
            ),
            "answer": (
                _described(
                    {"type": "null"},
                    SolverDecision,
                    "answer",
                )
                if selection_mode
                or force_next_cycle_repair
                or force_continue_repair
                else _described(
                    {"anyOf": [answer, {"type": "null"}]},
                    SolverDecision,
                    "answer",
                )
            ),
        },
        "required": [
            "next",
            "decision_reason",
            "start_next_cycle",
            "update_json",
            "next_focus_work_item_ids",
            "retain_evidence_ids",
            "review_finding_resolutions",
            "tool_requests_json",
            "dependency_decisions",
            "graph_candidate_review",
            "search_candidate_review",
            "frontier_re_adoptions",
            "deferred_frontier_resolutions",
            "unreviewed_graph_resolution",
            "answer",
        ],
    }


def _search_review_transport_schema(
    context: SolverContext,
    *,
    array_transport: bool = False,
) -> dict[str, Any]:
    """Search Reviewが意味選択だけへ集中する専用輸送schema。"""

    candidate_ids = tuple(item.article_id for item in context.search_candidates)
    assessment = _strict_object(
        {
            "legal_function": _described(
                {
                    "type": "string",
                    "enum": [
                        "applicability",
                        "exception",
                        "procedure",
                        "scope",
                    ],
                },
                SearchCandidateAssessment,
                "legal_function",
            ),
            "summary": _described(
                {"type": "string", "minLength": 1, "maxLength": 300},
                SearchCandidateAssessment,
                "summary",
            ),
        }
    )
    assessments_schema = (
        {
            "type": "array",
            "items": _strict_object(
                {
                    "article_id": _enum_string(candidate_ids),
                    **assessment["properties"],
                }
            ),
            "minItems": len(candidate_ids),
            "maxItems": len(candidate_ids),
        }
        if array_transport
        else {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                article_id: {"$ref": "#/$defs/search_candidate_assessment"}
                for article_id in candidate_ids
            },
            "required": list(candidate_ids),
        }
    )
    schema = _strict_object(
        {
            "assessments": {
                **assessments_schema,
                "description": (
                    f"{contract_field_description(SearchAssessmentDecision, 'assessments')} "
                    "objectの各keyは、対応する"
                    "search_candidates[].article_idと同じ文字列にする。"
                ),
            },
        }
    )
    if not array_transport:
        schema["$defs"] = {"search_candidate_assessment": assessment}
    return schema


def _search_selection_transport_schema(context: SolverContext) -> dict[str, Any]:
    """全候補を比較し、選択した候補の評価だけを返す契約。"""

    candidate_ids = tuple(item.article_id for item in context.search_candidates)
    open_work_item_ids = {
        item.work_item_id for item in context.work_tree if item.state == "open"
    }
    hypothesis_ids = tuple(
        item.hypothesis_id
        for item in context.hypotheses
        if item.requires_follow_up
        and item.work_item_id in open_work_item_ids
        and context.remaining_fetch_capacity_by_work_item.get(
            item.work_item_id,
            context.remaining_fetch_capacity,
        )
        > 0
    )
    selection_limit = _search_selection_capacity(context)
    selection = _strict_object(
        {
            "article_id": _described(
                _enum_string(candidate_ids),
                SearchCandidateSelection,
                "article_id",
            ),
            "legal_function": _described(
                {
                    "type": "string",
                    "enum": [
                        "applicability",
                        "exception",
                        "procedure",
                        "scope",
                    ],
                },
                SearchCandidateAssessment,
                "legal_function",
            ),
            "summary": _described(
                {"type": "string", "minLength": 1, "maxLength": 300},
                SearchCandidateAssessment,
                "summary",
            ),
            "matched_hypothesis_ids": _described(
                {
                    **_bounded_enum_array(hypothesis_ids),
                    "minItems": 1,
                },
                SearchCandidateAssessment,
                "matched_hypothesis_ids",
            ),
            "reason": _described(
                {"type": "string", "minLength": 1, "maxLength": 300},
                SearchCandidateSelection,
                "reason",
            ),
        }
    )
    return _strict_object(
        {
            "selections": {
                "type": "array",
                "items": selection,
                "maxItems": selection_limit,
                "description": (
                    "全候補を比較して今回本文を取得すると判断した候補。"
                    "内容評価は選択した候補についてだけ返す。"
                ),
            },
            "reason": _described(
                {"type": "string", "minLength": 1, "maxLength": 400},
                SearchReselectionDecision,
                "reason",
            ),
        }
    )


def _search_reselection_transport_schema(
    context: SolverContext,
    *,
    candidate_ids: tuple[str, ...] | None = None,
    hypothesis_ids_by_article: Mapping[str, tuple[str, ...]] | None = None,
    selection_limit: int | None = None,
) -> dict[str, Any]:
    if candidate_ids is None:
        candidate_ids = tuple(item.article_id for item in context.search_candidates)
    if hypothesis_ids_by_article is None:
        all_hypothesis_ids = tuple(
            item.hypothesis_id for item in context.hypotheses
        )
        hypothesis_ids_by_article = {
            article_id: all_hypothesis_ids for article_id in candidate_ids
        }
    if selection_limit is None:
        selection_limit = _search_selection_capacity(context)
    allowed_hypothesis_ids = tuple(
        dict.fromkeys(
            hypothesis_id
            for article_id in candidate_ids
            for hypothesis_id in hypothesis_ids_by_article.get(article_id, ())
        )
    )
    selection_item_schema = _strict_object(
        {
            "article_id": _described(
                _enum_string(candidate_ids),
                SearchCandidateSelection,
                "article_id",
            ),
            "reason": _described(
                {"type": "string", "minLength": 1},
                SearchCandidateSelection,
                "reason",
            ),
            "matched_hypothesis_ids": _described(
                {
                    **_bounded_enum_array(allowed_hypothesis_ids),
                    "minItems": 1,
                },
                SearchCandidateSelection,
                "matched_hypothesis_ids",
            ),
        }
    )
    return _strict_object(
        {
            "selections": _described(
                {
                    "type": "array",
                    "items": selection_item_schema,
                    "maxItems": min(
                        len(candidate_ids),
                        selection_limit,
                    ),
                },
                SearchReselectionDecision,
                "selections",
            ),
            "reason": _described(
                {"type": "string", "minLength": 1},
                SearchReselectionDecision,
                "reason",
            ),
        }
    )


def _tool_array_argument_capacity(
    context: SolverContext,
    *,
    tool_name: str,
    argument_name: str,
    fallback: int,
) -> int:
    """Tool schemaの配列上限と現在の残容量の小さい方を返す。"""

    capacity = max(0, fallback)
    for definition in context.available_tools:
        if definition.name != tool_name:
            continue
        properties = definition.input_schema.get("properties")
        if not isinstance(properties, dict):
            break
        argument_schema = properties.get(argument_name)
        if not isinstance(argument_schema, dict):
            break
        max_items = argument_schema.get("maxItems")
        if isinstance(max_items, int) and max_items >= 0:
            capacity = min(capacity, max_items)
        break
    return capacity


def _search_selection_capacity(context: SolverContext) -> int:
    """WorkItem別本文取得枠の合計を、検索候補選択の上限へ投影する。"""

    capacities = context.remaining_fetch_capacity_by_work_item
    return (
        sum(capacities.values())
        if capacities
        else context.remaining_fetch_capacity
    )


def _dependency_action_transport_schema(
    context: SolverContext,
    *,
    json_transport: bool = False,
) -> dict[str, Any]:
    """下位規範の次Actionだけを返す専用契約。"""

    available_action_tools = _dependency_action_available_tools(context)
    force_graph = _dependency_action_requires_graph(context)
    force_next_cycle = _dependency_action_must_start_next_cycle(context)
    required_ids = set(context.required_dependency_work_item_ids)
    action_context = context.model_copy(
        update={
            "available_tools": available_action_tools,
            "work_tree": tuple(
                item
                for item in context.work_tree
                if item.work_item_id in required_ids
            ),
            "hypotheses": tuple(
                item
                for item in context.hypotheses
                if item.work_item_id in required_ids
            ),
        }
    )

    if json_transport:
        return _strict_object(
            {
                "decision_reason": _described(
                    {"type": "string", "minLength": 1, "maxLength": 1200},
                    DependencyActionDecision,
                    "decision_reason",
                ),
                "start_next_cycle": _described(
                    {
                        "type": "boolean",
                        "enum": (
                            [True]
                            if force_next_cycle
                            else [False]
                            if force_graph
                            else [False, True]
                            if context.can_start_next_cycle
                            else [False]
                        ),
                    },
                    DependencyActionDecision,
                    "start_next_cycle",
                ),
                "tool_requests_json": {
                    "type": "string",
                    "description": (
                        "ToolRequest array encoded as one JSON array string; "
                        "exclude fetch_articles and use its dedicated field. "
                        "Fields: request_id, work_item_id, tool_name, arguments, "
                        "purpose, hypothesis_ids."
                    ),
                    **({"enum": ["[]"]} if force_next_cycle else {}),
                },
                "fetch_articles": {
                    **(
                        {"type": "null"}
                        if force_next_cycle or force_graph
                        else _anthropic_dependency_fetch_articles_schema(
                            action_context
                        )
                    ),
                    "description": "Known Article fetch or null.",
                },
            }
        )
    tool_requests = (
        _empty_array_schema()
        if force_next_cycle
        else _tool_requests_transport_schema(action_context)
    )
    required_count = len(context.required_dependency_work_item_ids)
    if not force_next_cycle:
        tool_requests["minItems"] = 1 if force_graph else 0
        tool_requests["maxItems"] = min(
            required_count,
            context.max_tool_requests_per_step,
        )
    return _strict_object(
        {
            "decision_reason": _described(
                {"type": "string", "minLength": 1, "maxLength": 1200},
                DependencyActionDecision,
                "decision_reason",
            ),
            "start_next_cycle": _described(
                {
                    "type": "boolean",
                    "enum": (
                        [True]
                        if force_next_cycle
                        else [False]
                        if force_graph
                        else [False, True]
                        if context.can_start_next_cycle
                        else [False]
                    ),
                },
                DependencyActionDecision,
                "start_next_cycle",
            ),
            "tool_requests": _described(
                tool_requests,
                DependencyActionDecision,
                "tool_requests",
            ),
        }
    )


def _solver_compact_transport_schema(context: SolverContext) -> dict:
    """provider共通の、長い二重JSONを避けた参照なし輸送schemaを返す。"""

    schema = _solver_transport_schema(context)
    properties = schema["properties"]
    if context.research_cycle_count == 0:
        properties["start_next_cycle"] = _described(
            {
                "type": "boolean",
                "enum": [False],
            },
            SolverDecision,
            "start_next_cycle",
        )
    tool_requests_forbidden = properties["tool_requests_json"].get("enum") == [
        "[]"
    ]
    properties.pop("update_json")
    properties.pop("tool_requests_json")
    selection_mode = bool(
        context.graph_review_batch.candidates
    ) and not context.finalize_only
    properties["update"] = _described(
        (
            _empty_case_update_transport_schema()
            if selection_mode
            or _preserve_previous_update_for_contract_repair(context)
            else _case_update_transport_schema()
        ),
        SolverDecision,
        "update",
    )
    if not context.work_tree and context.contract_feedback is None:
        properties["update"]["properties"]["add_work_items"]["minItems"] = 1
        properties["update"]["properties"]["add_hypotheses"]["minItems"] = 1
    projected_open_work_item_ids = _repair_open_work_item_ids(context)
    evidence_ids = tuple(item.evidence_id for item in context.evidence_manifest)
    properties["retain_evidence_ids"] = _described(
        (
            _empty_array_schema()
            if selection_mode
            else _bounded_enum_array(
                evidence_ids,
                max_items=context.max_retained_evidence,
            )
        ),
        SolverDecision,
        "retain_evidence_ids",
    )
    if selection_mode:
        properties["next_focus_work_item_ids"] = _described(
            _empty_array_schema(),
            SolverDecision,
            "next_focus_work_item_ids",
        )
    elif projected_open_work_item_ids:
        properties["next_focus_work_item_ids"] = _described(
            _bounded_enum_array(projected_open_work_item_ids),
            SolverDecision,
            "next_focus_work_item_ids",
        )
    properties["tool_requests"] = _described(
        (
            _empty_array_schema()
            if tool_requests_forbidden
            else _tool_requests_transport_schema(context)
        ),
        SolverDecision,
        "tool_requests",
    )
    if context.finalize_only:
        properties["next"] = _described(
            {"type": "string", "enum": ["finalize"]},
            SolverDecision,
            "next",
        )
        properties["start_next_cycle"] = _described(
            {"type": "boolean", "enum": [False]},
            SolverDecision,
            "start_next_cycle",
        )
        properties["update"] = _described(
            _empty_case_update_transport_schema(),
            SolverDecision,
            "update",
        )
        properties["next_focus_work_item_ids"] = _described(
            _empty_array_schema(),
            SolverDecision,
            "next_focus_work_item_ids",
        )
        answer_variants = properties["answer"].get("anyOf", ())
        answer_schema = next(
            (
                variant
                for variant in answer_variants
                if isinstance(variant, dict)
                and variant.get("type") == "object"
            ),
            None,
        )
        if answer_schema is not None:
            properties["answer"] = _described(
                answer_schema,
                SolverDecision,
                "answer",
            )
    schema["required"] = [
        "update" if item == "update_json" else (
            "tool_requests" if item == "tool_requests_json" else item
        )
        for item in schema["required"]
    ]
    return schema


def _hypothesis_revision_transport_schema(
    context: SolverContext,
) -> dict[str, Any]:
    """Provider共通の小さな仮説見直しschema。"""

    (
        open_work_item_ids,
        eligible_hypotheses,
        revision_evidence_ids,
    ) = _hypothesis_revision_scope(context)
    existing_hypothesis_ids = tuple(
        item.hypothesis_id for item in eligible_hypotheses
    )
    existing_gap_ids = tuple(
        gap.gap_id
        for item in eligible_hypotheses
        for gap in structured_hypothesis_gaps(item.hypothesis_id, item.gaps)
    )
    shared_fields = {
        "statement": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1200,
            "description": "取得本文を踏まえ、次Cycleで検証する法的命題。",
        },
        "evidence_ids": {
            **_bounded_enum_array(revision_evidence_ids),
            "description": "命題を直接示した取得済み本文Evidence ID。不要なら空配列。",
        },
    }
    revision_update = _strict_object(
        {
            "hypothesis_id": {
                **_enum_string(existing_hypothesis_ids),
                "description": "現在版を更新する既存Hypothesis ID。",
            },
            **shared_fields,
            "judgment": {
                "type": "string",
                "enum": ["supported", "contradicted", "unresolved"],
                "description": "更新後の命題を提示本文で評価した現在の判定。",
            },
            "add_gaps": {
                "type": "array",
                "items": _strict_object(
                    {
                        "description": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 300,
                            "description": "見直しで新たに判明した未確認事項。",
                        }
                    }
                ),
                "maxItems": 12,
                "description": "新しい未確認事項の差分。なければ空配列。",
            },
            "resolve_gap_ids": {
                **_bounded_enum_array(existing_gap_ids),
                "maxItems": 12,
                "description": "見直しで不要又は確認済みとなった既存gap ID。",
            },
        }
    )
    proposal = _strict_object(
        {
            "hypothesis_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 160,
                "description": "新しいHypothesisのCase内一意ID。既存IDは再利用しない。",
            },
            "work_item_id": {
                **_enum_string(open_work_item_ids),
                "description": "追加先となる既存の非dropped WorkItemのID。",
            },
            **shared_fields,
            "gaps": {
                "type": "array",
                "items": {"type": "string", "maxLength": 300},
                "maxItems": 12,
                "description": "新しい命題について本文で残る具体的な未確認事項。",
            },
        }
    )
    return _strict_object(
        {
            "decision_reason": {
                "type": "string",
                "minLength": 1,
                "maxLength": 1200,
                "description": "更新、追加または維持した理由。",
            },
            "revise_hypotheses": {
                "type": "array",
                "items": revision_update,
                "maxItems": 8,
                "description": "同じ命題の見立てを直す場合だけ返す。なければ空配列。",
            },
            "add_hypotheses": {
                "type": "array",
                "items": proposal,
                "maxItems": 8,
                "description": "独立した新しい未解決命題がある場合だけ返す。なければ空配列。",
            },
        }
    )


def _hypothesis_revision_scope(
    context: SolverContext,
) -> tuple[tuple[str, ...], tuple[Hypothesis, ...], tuple[str, ...]]:
    """Promptとschemaが共有する、反証済みHypothesisの最小投影。"""

    active_work_item_ids = {
        item.work_item_id
        for item in context.work_tree
        if item.state != "dropped"
    }
    visible_evidence_ids = {
        item.evidence_id for item in context.hypothesis_revision_evidence
    }
    eligible_hypotheses = tuple(
        item
        for item in context.hypotheses
        if item.work_item_id in active_work_item_ids
        and item.judgment == "contradicted"
        and not visible_evidence_ids.isdisjoint(item.evidence_ids)
    )
    eligible_work_item_ids = tuple(
        dict.fromkeys(item.work_item_id for item in eligible_hypotheses)
    )
    referenced_evidence_ids = {
        evidence_id
        for item in eligible_hypotheses
        for evidence_id in item.evidence_ids
    }
    eligible_evidence_ids = tuple(
        item.evidence_id
        for item in context.hypothesis_revision_evidence
        if item.evidence_id in referenced_evidence_ids
    )
    return (
        eligible_work_item_ids,
        eligible_hypotheses,
        eligible_evidence_ids,
    )


def _graph_review_transport_schema(context: SolverContext) -> dict[str, Any]:
    """Graph Review専用の直列化契約を共通契約から切り出す。"""

    schema = _solver_compact_transport_schema(context)
    return deepcopy(schema["properties"]["graph_candidate_review"])


def _finalization_transport_schema(
    context: SolverContext,
) -> dict[str, Any]:
    """最終回答に不要な共通Solver枝をProvider共通schemaから除く。"""

    common = _solver_common_transport_schema(context)
    included = {
        "next",
        "decision_reason",
        "answer",
        "deferred_frontier_resolutions",
        "unreviewed_graph_resolution",
    }
    common["properties"] = {
        name: schema
        for name, schema in common["properties"].items()
        if name in included
    }
    common["required"] = [
        name for name in common["required"] if name in included
    ]
    return common


def _solver_common_transport_schema(context: SolverContext) -> dict[str, Any]:
    """全Providerへ渡す、現在の処理に必要な意味項目だけのschema。"""

    schema = _solver_compact_transport_schema(context)
    properties = schema["properties"]
    finalization_citation_ids = (
        _verified_answer_evidence_ids(
            context,
            ObservationIntegrationDecision(
                decision_reason="確認済み根拠を最終回答へ投影する。"
            ),
        )
        if context.finalize_only
        else ()
    )
    graph_review_mode = bool(
        context.graph_review_batch.candidates and not context.finalize_only
    )
    if graph_review_mode:
        included = {
            "next",
            "decision_reason",
            "graph_candidate_review",
        }
    else:
        included = {
            "next",
            "decision_reason",
            "start_next_cycle",
            "update",
            "next_focus_work_item_ids",
        }
        if context.evidence_manifest and not context.finalize_only:
            included.add("retain_evidence_ids")
        if context.reviewer_findings:
            included.add("review_finding_resolutions")
        if context.required_dependency_work_item_ids:
            included.add("dependency_decisions")
        if context.graph_review_ledger:
            included.add("frontier_re_adoptions")
        if _llm_active_deferred_frontiers(context):
            included.add("deferred_frontier_resolutions")
        if context.graph_review_batch.remaining_unreviewed_count:
            included.add("unreviewed_graph_resolution")
        if not _schema_is_empty_array(properties["tool_requests"]):
            included.add("tool_requests")
        if properties["answer"].get("type") != "null":
            included.add("answer")

    if context.finalize_only and "answer" in properties:
        answer_schema = properties["answer"]
        if answer_schema.get("type") == "object":
            answer_schema["properties"]["citation_ids"] = _described(
                _bounded_enum_array(finalization_citation_ids),
                FinalAnswer,
                "citation_ids",
            )

    schema["properties"] = {
        name: value
        for name, value in properties.items()
        if name in included
    }
    schema["required"] = [
        name for name in schema["required"] if name in included
    ]
    schema = _strip_runtime_id_enums(schema)
    if (
        context.finalize_only
        and "deferred_frontier_resolutions" in schema.get("properties", {})
    ):
        schema["properties"]["deferred_frontier_resolutions"] = (
            _finalization_deferred_frontier_transport_schema(context)
        )
    if context.finalize_only and "answer" in schema.get("properties", {}):
        _constrain_finalization_citation_ids(
            schema,
            finalization_citation_ids,
        )
    _constrain_active_deferred_frontier_ids(schema, context)
    if (
        context.contract_feedback is not None
        and "tool request references unknown Article IDs"
        in context.contract_feedback.violation
    ):
        _constrain_fetch_article_ids(schema, context.fetchable_article_ids)
    if (
        context.contract_feedback is not None
        and "unknown retained evidence IDs"
        in context.contract_feedback.violation
    ):
        _constrain_repair_retain_evidence_ids(
            schema,
            tuple(item.evidence_id for item in context.evidence_manifest),
        )
    return schema


def _finalization_deferred_frontier_transport_schema(
    context: SolverContext,
) -> dict[str, Any]:
    """Use known Frontier IDs as keys; the Program restores their references."""

    active_deferred = _llm_active_deferred_frontiers(context)
    resolution = _strict_object(
        {
            "action": _described(
                {
                    "type": "string",
                    "enum": ["no_longer_needed", "unresolved_at_limit"],
                },
                DeferredFrontierResolution,
                "action",
            ),
            "reason": _described(
                {"type": "string", "minLength": 1},
                DeferredFrontierResolution,
                "reason",
            ),
        }
    )
    frontier_ids = tuple(item.frontier_item_id for item in active_deferred)
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            frontier_id: deepcopy(resolution) for frontier_id in frontier_ids
        },
        "required": list(frontier_ids),
        "description": (
            "各keyは入力のactive deferred Frontier ID。各候補について、"
            "上限時の扱いと理由を1件ずつ返す。Article、WorkItem、Hypothesisの"
            "対応はProgramが既知Frontierから復元する。"
        ),
    }


def _all_active_deferred_frontiers(
    context: SolverContext,
) -> tuple[GraphReviewLedgerItem, ...]:
    return tuple(
        item
        for item in context.graph_review_ledger
        if item.review_status == "relevant_deferred"
        and item.content_status in {"not_requested", "failed", "timeout"}
        and item.deferred_resolution_action != "no_longer_needed"
    )


def _llm_active_deferred_frontiers(
    context: SolverContext,
) -> tuple[GraphReviewLedgerItem, ...]:
    active = _all_active_deferred_frontiers(context)
    if not context.finalize_only:
        return active
    open_work_item_ids = {
        item.work_item_id for item in context.work_tree if item.state == "open"
    }
    return tuple(
        item for item in active if item.work_item_id in open_work_item_ids
    )


def _constrain_finalization_citation_ids(
    schema: dict[str, Any],
    evidence_ids: tuple[str, ...],
) -> None:
    """限定回答の引用を、投影済みの解決済み根拠IDへ制約する。"""

    citation_schema = (
        schema.get("properties", {})
        .get("answer", {})
        .get("properties", {})
        .get("citation_ids")
    )
    if not isinstance(citation_schema, dict):
        return
    citation_schema.clear()
    citation_schema.update(
        _described(
            _bounded_enum_array(evidence_ids),
            FinalAnswer,
            "citation_ids",
        )
    )


def _constrain_active_deferred_frontier_ids(
    schema: dict[str, Any],
    context: SolverContext,
) -> None:
    """Prevent stale ledger generations from satisfying a current boundary."""

    active_ids = tuple(
        item.frontier_item_id
        for item in _llm_active_deferred_frontiers(context)
    )
    if not active_ids:
        return
    field_schema = (
        schema.get("properties", {})
        .get("deferred_frontier_resolutions", {})
        .get("items", {})
        .get("properties", {})
        .get("frontier_item_id")
    )
    if isinstance(field_schema, dict):
        field_schema["enum"] = list(active_ids)


def _constrain_fetch_article_ids(
    schema: dict[str, Any],
    fetchable_article_ids: tuple[str, ...],
) -> None:
    """本文取得IDを現在の既知候補へ制約する。"""

    request_schema = schema.get("properties", {}).get("tool_requests", {})
    item_schema = request_schema.get("items", {})
    variants = item_schema.get("anyOf", (item_schema,))
    for variant in variants:
        properties = variant.get("properties", {})
        if properties.get("tool_name", {}).get("enum") != ["fetch_articles"]:
            continue
        article_items = (
            properties.get("arguments", {})
            .get("properties", {})
            .get("article_ids", {})
            .get("items")
        )
        if isinstance(article_items, dict):
            article_items["enum"] = list(fetchable_article_ids)


def _constrain_repair_retain_evidence_ids(
    schema: dict[str, Any],
    evidence_ids: tuple[str, ...],
) -> None:
    """未知Evidence IDの修復時だけ、保持対象を既知IDへ制約する。"""

    items = (
        schema.get("properties", {})
        .get("retain_evidence_ids", {})
        .get("items")
    )
    if isinstance(items, dict):
        items["enum"] = list(evidence_ids)


def _schema_is_empty_array(schema: dict[str, Any]) -> bool:
    return schema.get("type") == "array" and schema.get("maxItems") == 0


def _strip_runtime_id_enums(
    value: Any,
    field_name: str | None = None,
) -> Any:
    """実行時IDをschemaへ複製せず、既知性は共通validatorへ委ねる。"""

    if isinstance(value, list):
        return [_strip_runtime_id_enums(item, field_name) for item in value]
    if not isinstance(value, dict):
        return value
    converted: dict[str, Any] = {}
    for key, item in value.items():
        if key == "properties" and isinstance(item, dict):
            converted[key] = {
                name: _strip_runtime_id_enums(schema, name)
                for name, schema in item.items()
            }
        else:
            converted[key] = _strip_runtime_id_enums(item, field_name)
    if field_name is not None and (
        field_name.endswith("_id") or field_name.endswith("_ids")
    ):
        converted.pop("enum", None)
    return converted


def _solver_anthropic_transport_schema(context: SolverContext) -> dict:
    """Anthropicのgrammar上限内でTool参照とArticle取得枠を構造化する。"""

    schema = _solver_transport_schema(context)
    properties = schema["properties"]
    properties["dependency_decisions"] = _described(
        _anthropic_dependency_decisions_schema(context),
        SolverDecision,
        "dependency_decisions",
    )
    grounding_article_ids = tuple(
        dict.fromkeys(
            article_id
            for evidence in context.material_evidence
            if (article_id := evidence.metadata.get("articleId"))
            and isinstance(article_id, str)
        )
    )
    properties["dependency_article_bindings"] = {
        **(
            {
                "type": "array",
                "items": _strict_object(
                    {
                        "work_item_id": {
                            **_enum_string(
                                context.required_dependency_work_item_ids
                            ),
                            "description": (
                                "DependencyDecisionへ復元する対象WorkItem ID。"
                            ),
                        },
                        "article_ids": {
                            **_bounded_enum_array(grounding_article_ids),
                            "description": (
                                "basis_evidence_idsへ機械変換する取得済みArticle ID。"
                            ),
                        },
                    }
                ),
            }
            if context.required_dependency_work_item_ids
            else {"type": "null"}
        ),
        "description": (
            "Anthropic輸送専用。DependencyDecisionの判断根拠Articleを既知IDから指定する。"
        ),
    }
    properties["hypothesis_evidence_bindings"] = {
        **(
            {
                "type": "array",
                "items": _strict_object(
                    {
                        "hypothesis_id": {
                            "type": "string",
                            "description": (
                                "今回追加・更新するHypothesis ID。"
                            ),
                        },
                        "evidence_ids": {
                            **_bounded_enum_array(
                                context.grounding_evidence_ids
                            ),
                            "description": (
                                "Hypothesis判定へ復元する取得済みgrounding Evidence ID。"
                            ),
                        },
                    }
                ),
            }
            if context.grounding_evidence_ids
            else {"type": "null"}
        ),
        "description": (
            "Anthropic輸送専用。Hypothesis更新JSONと既知Evidenceの対応を指定する。"
        ),
    }
    tool_requests_forbidden = properties["tool_requests_json"].get("enum") == [
        "[]"
    ]
    properties.pop("tool_requests_json")
    fetch_articles_schema = _anthropic_fetch_articles_schema(context)
    non_fetch_capacity = (
        0
        if tool_requests_forbidden
        else context.max_tool_requests_per_step
        - (0 if fetch_articles_schema == {"type": "null"} else 1)
    )
    non_fetch_tool_names = [
        definition.name
        for definition in context.available_tools
        if definition.name != "fetch_articles"
    ] or ["legal_search", "legal_graph_neighbors", "load_evidence"]
    properties["tool_requests"] = {
        **_strict_object(
            {
                f"tool_request_{index}_json": {
                    "anyOf": [
                        _strict_object(
                            {
                                "tool_name": {
                                    "type": "string",
                                    "enum": non_fetch_tool_names,
                                    "description": (
                                        "request_jsonを復元するときの正規Tool名。"
                                    ),
                                },
                                "request_json": {
                                    "type": "string",
                                    "description": (
                                        "tool_nameを除く1件のToolRequest JSON object。"
                                    ),
                                },
                            }
                        ),
                        {"type": "null"},
                    ],
                    "description": (
                        "Anthropic輸送専用のToolRequest slot。使わない場合はnull。"
                    ),
                }
                for index in range(1, non_fetch_capacity + 1)
            }
        ),
        "description": contract_field_description(
            SolverDecision,
            "tool_requests",
        ),
    }
    properties["fetch_articles"] = {
        **fetch_articles_schema,
        "description": (
            "Anthropic輸送専用のfetch_articles ToolRequest。候補がない場合はnull。"
        ),
    }
    properties["retain_evidence_ids"] = _described(
        _bounded_enum_array(
            tuple(item.evidence_id for item in context.evidence_manifest),
            max_items=context.max_retained_evidence,
        ),
        SolverDecision,
        "retain_evidence_ids",
    )
    answer_schema = properties.get("answer")
    if isinstance(answer_schema, dict):
        variants = answer_schema.get("anyOf") or (answer_schema,)
        for variant in variants:
            if not isinstance(variant, dict) or variant.get("type") != "object":
                continue
            variant["properties"]["citation_ids"] = _described(
                _bounded_enum_array(context.grounding_evidence_ids),
                FinalAnswer,
                "citation_ids",
            )
    schema["required"] = [
        "tool_requests" if item == "tool_requests_json" else item
        for item in schema["required"]
    ]
    schema["required"].append("hypothesis_evidence_bindings")
    schema["required"].append("dependency_article_bindings")
    schema["required"].append("fetch_articles")

    return schema


def _solver_anthropic_json_transport_schema(
    context: SolverContext,
) -> dict[str, Any]:
    """Keep Anthropic grammar small while constraining Article fetch IDs."""

    common_schema = _solver_common_transport_schema(context)
    expected_fields = tuple(common_schema["properties"])
    nested_contracts: list[str] = []
    answer_schema = common_schema["properties"].get("answer")
    if isinstance(answer_schema, dict):
        answer_variants = answer_schema.get("anyOf") or (answer_schema,)
        answer_object = next(
            (
                variant
                for variant in answer_variants
                if isinstance(variant, dict)
                and variant.get("type") == "object"
            ),
            None,
        )
        if answer_object is not None:
            nested_contracts.append(
                " If answer is an object, use exactly these answer fields: "
                + ", ".join(answer_object.get("properties", ()))
                + "."
            )
    return _strict_object(
        {
            "decision_json": {
                "type": "string",
                "description": (
                    "JSON object string for the current SolverDecision. "
                    "Use exactly these current-step top-level fields and no "
                    "SolverContext input fields: "
                    + ", ".join(expected_fields)
                    + "."
                    + "".join(nested_contracts)
                    + " Do not put fetch_articles in tool_requests; use the "
                    "dedicated fetch_articles field."
                ),
            },
            "fetch_articles": {
                **_anthropic_fetch_articles_schema(context),
                "description": (
                    "Anthropic輸送専用のfetch_articles ToolRequest。"
                    "既知候補を取得しない場合はnull。"
                ),
            },
        }
    )


def _anthropic_dependency_decisions_schema(
    context: SolverContext,
) -> dict[str, Any]:
    """配列件数制約を保持しないProviderでもWorkItem全件を提示させる。"""

    if not context.required_dependency_work_item_ids:
        return _strict_object({})
    return _strict_object(
        {
            f"dependency_decision_{index}_json": {
                "type": "string",
                "description": (
                    "one DependencyDecision JSON object for exact work_item_id "
                    f"{work_item_id}; keep basis_evidence_ids empty and select "
                    "Article IDs in dependency_article_bindings; status=resolved "
                    "requires at least two distinct Article IDs "
                    "(delegating source and terminal target); if the target body "
                    "is not shown, use needs_action; restored and fully validated "
                    "after transport"
                ),
            }
            for index, work_item_id in enumerate(
                context.required_dependency_work_item_ids,
                start=1,
            )
        }
    )


def _anthropic_fetch_articles_schema(context: SolverContext) -> dict[str, Any]:
    graph_review_mode = bool(
        context.graph_review_batch.candidates and not context.finalize_only
    )
    available_work_item_ids = tuple(
        work_item_id
        for work_item_id in _repair_open_work_item_ids(context)
        if not context.remaining_fetch_capacity_by_work_item
        or context.remaining_fetch_capacity_by_work_item.get(work_item_id, 0) > 0
    )
    remaining_capacity = (
        max(
            (
                context.remaining_fetch_capacity_by_work_item[work_item_id]
                for work_item_id in available_work_item_ids
            ),
            default=0,
        )
        if context.remaining_fetch_capacity_by_work_item
        else context.remaining_fetch_capacity
    )
    capacity = min(
        _tool_array_argument_capacity(
            context,
            tool_name="fetch_articles",
            argument_name="article_ids",
            fallback=remaining_capacity,
        ),
        len(context.fetchable_article_ids),
    )
    if (
        context.finalize_only
        or context.cycle_close_required
        or graph_review_mode
        or not available_work_item_ids
        or capacity < 1
    ):
        return {"type": "null"}

    article_properties: dict[str, Any] = {
        "request_id": _described(
            {"type": "string"},
            ToolRequest,
            "request_id",
        ),
        "work_item_id": _described(
            _enum_string(available_work_item_ids),
            ToolRequest,
            "work_item_id",
        ),
        "purpose": _described(
            {"type": "string"},
            ToolRequest,
            "purpose",
        ),
        "hypothesis_ids": _described(
            _bounded_enum_array(_repair_hypothesis_ids(context)),
            ToolRequest,
            "hypothesis_ids",
        ),
        "article_ids": {
            "type": "array",
            "items": _enum_string(tuple(context.fetchable_article_ids)),
            "minItems": 1,
            "maxItems": capacity,
            "description": "fetchable_article_idsにある既知Article ID。",
        },
    }
    return {
        "anyOf": [
            _strict_object(article_properties),
            {"type": "null"},
        ]
    }


def _anthropic_dependency_fetch_articles_schema(
    context: SolverContext,
) -> dict[str, Any]:
    """Dependency Action用の小さい既知Article取得schema。"""

    article_ids = tuple(context.fetchable_article_ids)
    work_item_ids = tuple(
        work_item_id
        for work_item_id in context.required_dependency_work_item_ids
        if not context.remaining_fetch_capacity_by_work_item
        or context.remaining_fetch_capacity_by_work_item.get(work_item_id, 0) > 0
    )
    remaining_capacity = (
        max(
            (
                context.remaining_fetch_capacity_by_work_item[work_item_id]
                for work_item_id in work_item_ids
            ),
            default=0,
        )
        if context.remaining_fetch_capacity_by_work_item
        else context.remaining_fetch_capacity
    )
    capacity = min(
        _tool_array_argument_capacity(
            context,
            tool_name="fetch_articles",
            argument_name="article_ids",
            fallback=remaining_capacity,
        ),
        len(article_ids),
    )
    if capacity < 1 or not work_item_ids:
        return {"type": "null"}
    return {
        "anyOf": [
            {
                "type": "array",
                "items": _strict_object(
                    {
                        "work_item_id": _enum_string(work_item_ids),
                        "article_ids": {
                            "type": "array",
                            "items": _enum_string(article_ids),
                            "minItems": 1,
                            "maxItems": capacity,
                        },
                    }
                ),
                "minItems": 1,
                "maxItems": min(
                    len(work_item_ids),
                    context.max_tool_requests_per_step,
                ),
            },
            {"type": "null"},
        ]
    }


def _article_fetch_alias_map(context: SolverContext) -> dict[str, str]:
    return {
        f"a{index}": article_id
        for index, article_id in enumerate(context.fetchable_article_ids, start=1)
    }


def _anthropic_load_evidence_schema(context: SolverContext) -> dict[str, Any]:
    aliases = tuple(_evidence_load_alias_map(context))
    load_available = any(
        definition.name == "load_evidence"
        for definition in context.available_tools
    )
    if context.finalize_only or not load_available or not aliases:
        return {"type": "null"}

    capacity = min(4, len(aliases))
    properties: dict[str, Any] = {
        "request_id": _described(
            {"type": "string"},
            ToolRequest,
            "request_id",
        ),
        "work_item_id": _described(
            _enum_string(_repair_open_work_item_ids(context)),
            ToolRequest,
            "work_item_id",
        ),
        "purpose": _described(
            {"type": "string"},
            ToolRequest,
            "purpose",
        ),
        "hypothesis_ids": _described(
            _bounded_enum_array(_repair_hypothesis_ids(context)),
            ToolRequest,
            "hypothesis_ids",
        ),
    }
    for index in range(1, capacity + 1):
        evidence_schema = _enum_string(aliases)
        properties[f"evidence_ref_{index}"] = (
            {
                **evidence_schema,
                "description": "omitted_evidence_idsに対応する既知Evidence別名。",
            }
            if index == 1
            else {
                "anyOf": [evidence_schema, {"type": "null"}],
                "description": "追加表示する既知Evidence別名。使わない場合はnull。",
            }
        )
    return {
        "anyOf": [
            _strict_object(properties),
            {"type": "null"},
        ]
    }


def _evidence_load_alias_map(context: SolverContext) -> dict[str, str]:
    return {
        f"e{index}": evidence_id
        for index, evidence_id in enumerate(context.omitted_evidence_ids, start=1)
    }


def _tool_requests_transport_schema(context: SolverContext) -> dict[str, Any]:
    if not context.available_tools:
        return {
            **_empty_array_schema(),
            "description": contract_field_description(
                SolverDecision,
                "tool_requests",
            ),
        }
    projected_open_work_item_ids = _repair_open_work_item_ids(context)
    if (
        not projected_open_work_item_ids
        and context.contract_feedback is not None
        and "tool requests must reference open WorkItem IDs"
        in context.contract_feedback.violation
    ):
        return {
            **_empty_array_schema(),
            "description": contract_field_description(
                SolverDecision,
                "tool_requests",
            ),
        }
    projected_hypothesis_ids = _repair_hypothesis_ids(context)
    common_properties = {
        "request_id": _described(
            {"type": "string"},
            ToolRequest,
            "request_id",
        ),
        "work_item_id": _described(
            _enum_string(projected_open_work_item_ids),
            ToolRequest,
            "work_item_id",
        ),
        "purpose": _described(
            {"type": "string"},
            ToolRequest,
            "purpose",
        ),
        "hypothesis_ids": _described(
            (
                _bounded_enum_array(projected_hypothesis_ids)
                if projected_hypothesis_ids
                else _string_array_schema()
            ),
            ToolRequest,
            "hypothesis_ids",
        ),
    }
    variants: list[dict[str, Any]] = []
    for definition in context.available_tools:
        if definition.name == "fetch_articles":
            hypotheses_by_work_item = {
                work_item_id: tuple(
                    item.hypothesis_id
                    for item in context.hypotheses
                    if item.work_item_id == work_item_id
                )
                for work_item_id in projected_open_work_item_ids
            }
            fetch_work_item_ids: tuple[str | None, ...] = (
                tuple(projected_open_work_item_ids)
                if projected_open_work_item_ids
                else (None,)
            )
            for work_item_id in fetch_work_item_ids:
                work_item_capacity = (
                    context.remaining_fetch_capacity_by_work_item.get(
                        work_item_id,
                        context.remaining_fetch_capacity,
                    )
                    if work_item_id is not None
                    else context.remaining_fetch_capacity
                )
                if (
                    context.remaining_fetch_capacity_by_work_item
                    and work_item_capacity <= 0
                ):
                    continue
                argument_schema = deepcopy(definition.input_schema)
                article_ids = argument_schema.get("properties", {}).get(
                    "article_ids"
                )
                if isinstance(article_ids, dict):
                    article_ids["items"] = _enum_string(
                        context.fetchable_article_ids
                    )
                    article_ids["maxItems"] = min(
                        work_item_capacity,
                        len(context.fetchable_article_ids),
                    )
                work_item_hypothesis_ids = (
                    hypotheses_by_work_item[work_item_id]
                    if work_item_id is not None
                    else projected_hypothesis_ids
                )
                fetch_properties = {
                    **common_properties,
                    "tool_name": _described(
                        {
                            "type": "string",
                            "enum": [definition.name],
                            "description": definition.description,
                        },
                        ToolRequest,
                        "tool_name",
                        append=True,
                    ),
                    "arguments": _described(
                        argument_schema,
                        ToolRequest,
                        "arguments",
                    ),
                }
                if work_item_id is not None:
                    fetch_properties["work_item_id"] = _described(
                        _enum_string((work_item_id,)),
                        ToolRequest,
                        "work_item_id",
                    )
                    fetch_properties["hypothesis_ids"] = _described(
                        (
                            _bounded_enum_array(work_item_hypothesis_ids)
                            if work_item_hypothesis_ids
                            else _string_array_schema()
                        ),
                        ToolRequest,
                        "hypothesis_ids",
                    )
                variants.append(
                    _strict_object(fetch_properties)
                )
            continue

        argument_schema = deepcopy(definition.input_schema)
        if definition.name == "load_evidence":
            evidence_ids = argument_schema.get("properties", {}).get("evidence_ids")
            if isinstance(evidence_ids, dict):
                evidence_ids["items"] = _enum_string(context.omitted_evidence_ids)
        variants.append(
            _strict_object(
                {
                    **common_properties,
                    "tool_name": _described(
                        {
                            "type": "string",
                            "enum": [definition.name],
                            "description": definition.description,
                        },
                        ToolRequest,
                        "tool_name",
                        append=True,
                    ),
                    "arguments": _described(
                        argument_schema,
                        ToolRequest,
                        "arguments",
                    ),
                }
            )
        )
    if variants:
        item_schema = variants[0] if len(variants) == 1 else {"anyOf": variants}
    else:
        item_schema = _strict_object(
            {
                **common_properties,
                "tool_name": _described(
                    {"type": "string"},
                    ToolRequest,
                    "tool_name",
                ),
                "arguments": _described(
                    {"type": "object"},
                    ToolRequest,
                    "arguments",
                ),
            }
        )
    max_items = context.max_tool_requests_per_step
    if context.contract_feedback is not None:
        requires_single_request = (
            "identical legal_graph_neighbors arguments must be consolidated"
            in context.contract_feedback.violation
        )
        if requires_single_request:
            max_items = 1
    return {
        "type": "array",
        "items": item_schema,
        "maxItems": max_items,
        "description": contract_field_description(SolverDecision, "tool_requests"),
    }


def _case_update_transport_schema() -> dict[str, Any]:
    string_array = _string_array_schema()
    nullable_string = {
        "anyOf": [{"type": "string"}, {"type": "null"}],
    }
    work_item = _strict_object(
        {
            "work_item_id": _described({"type": "string"}, WorkItem, "work_item_id"),
            "parent_work_item_id": _described(nullable_string, WorkItem, "parent_work_item_id"),
            "question": _described({"type": "string"}, WorkItem, "question"),
            "state": _described(
                {"type": "string", "enum": ["open"]},
                WorkItem,
                "state",
            ),
            "resolution": _described({"type": "null"}, WorkItem, "resolution"),
            "basis_hypothesis_ids": _described(
                string_array,
                WorkItem,
                "basis_hypothesis_ids",
            ),
            "replaces_work_item_id": _described(
                nullable_string,
                WorkItem,
                "replaces_work_item_id",
            ),
        }
    )
    work_item_update = _strict_object(
        {
            "work_item_id": _described({"type": "string"}, WorkItemUpdate, "work_item_id"),
            "state": _described(
                {"type": "string", "enum": ["open", "resolved", "dropped"]},
                WorkItemUpdate,
                "state",
            ),
            "resolution": _described(nullable_string, WorkItemUpdate, "resolution"),
            "basis_hypothesis_ids": _described(
                string_array,
                WorkItemUpdate,
                "basis_hypothesis_ids",
            ),
        }
    )
    hypothesis = _strict_object(
        {
            "hypothesis_id": _described({"type": "string"}, Hypothesis, "hypothesis_id"),
            "work_item_id": _described({"type": "string"}, Hypothesis, "work_item_id"),
            "statement": _described({"type": "string"}, Hypothesis, "statement"),
            "judgment": _described(
                {"type": "string", "enum": ["supported", "contradicted", "unresolved"]},
                Hypothesis,
                "judgment",
            ),
            "evidence_ids": _described(string_array, Hypothesis, "evidence_ids"),
            "gaps": _described(string_array, Hypothesis, "gaps"),
        }
    )
    hypothesis_update = _strict_object(
        {
            "hypothesis_id": _described(
                {"type": "string"},
                HypothesisUpdate,
                "hypothesis_id",
            ),
            "judgment": _described(
                {"type": "string", "enum": ["supported", "contradicted", "unresolved"]},
                HypothesisUpdate,
                "judgment",
            ),
            "evidence_ids": _described(
                string_array,
                HypothesisUpdate,
                "evidence_ids",
            ),
            "add_gaps": _described(
                {
                    "type": "array",
                    "items": _strict_object(
                        {
                            "description": _described(
                                {"type": "string", "maxLength": 300},
                                HypothesisGapAddition,
                                "description",
                            )
                        }
                    ),
                    "maxItems": 12,
                },
                HypothesisUpdate,
                "add_gaps",
            ),
            "resolve_gap_ids": _described(
                string_array,
                HypothesisUpdate,
                "resolve_gap_ids",
            ),
        }
    )
    impact = _strict_object(
        {
            "work_item_id": _described(
                {"type": "string"},
                WorkItemImpactDecision,
                "work_item_id",
            ),
            "action": _described(
                {
                    "type": "string",
                    "enum": ["retain", "replace", "drop"],
                },
                WorkItemImpactDecision,
                "action",
            ),
            "reason": _described(
                {"type": "string"},
                WorkItemImpactDecision,
                "reason",
            ),
            "new_basis_hypothesis_ids": _described(
                string_array,
                WorkItemImpactDecision,
                "new_basis_hypothesis_ids",
            ),
            "replacement_work_item_id": _described(
                nullable_string,
                WorkItemImpactDecision,
                "replacement_work_item_id",
            ),
            "drop_subtree": _described(
                {"type": "boolean"},
                WorkItemImpactDecision,
                "drop_subtree",
            ),
        }
    )
    return _strict_object(
        {
            "add_work_items": _described(
                {"type": "array", "items": work_item},
                CaseUpdate,
                "add_work_items",
            ),
            "update_work_items": _described(
                {"type": "array", "items": work_item_update},
                CaseUpdate,
                "update_work_items",
            ),
            "add_hypotheses": _described(
                {"type": "array", "items": hypothesis},
                CaseUpdate,
                "add_hypotheses",
            ),
            "update_hypotheses": _described(
                {"type": "array", "items": hypothesis_update},
                CaseUpdate,
                "update_hypotheses",
            ),
            "impact_decisions": _described(
                {"type": "array", "items": impact},
                CaseUpdate,
                "impact_decisions",
            ),
        }
    )


def _empty_case_update_transport_schema() -> dict[str, Any]:
    schema = _case_update_transport_schema()
    for value in schema["properties"].values():
        value["maxItems"] = 0
    return schema


def _repair_open_work_item_ids(context: SolverContext) -> tuple[str, ...]:
    states = {item.work_item_id: item.state for item in context.work_tree}
    if context.contract_feedback is not None:
        previous = context.contract_feedback.previous_decision
        for item in previous.update.add_work_items:
            states[item.work_item_id] = item.state
        for item in previous.update.update_work_items:
            if item.work_item_id in states:
                states[item.work_item_id] = item.state
    return tuple(key for key, value in states.items() if value == "open")


def _repair_hypothesis_ids(context: SolverContext) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (
                *(item.hypothesis_id for item in context.hypotheses),
                *(
                    item.hypothesis_id
                    for item in (
                        context.contract_feedback.previous_decision.update.add_hypotheses
                        if context.contract_feedback is not None
                        else ()
                    )
                ),
            )
        )
    )


def _string_array_schema() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}}


def _strict_object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


def _described(
    schema: dict[str, Any],
    model_type: type[BaseModel],
    field_name: str,
    *,
    append: bool = False,
) -> dict[str, Any]:
    result = deepcopy(schema)
    description = contract_field_description(model_type, field_name)
    if append and result.get("description"):
        description = f"{description} {result['description']}"
    result["description"] = description
    return result


def _preserve_previous_update_for_cycle_repair(context: SolverContext) -> bool:
    feedback = context.contract_feedback
    return bool(
        feedback is not None
        and "finalize must account for every open WorkItem" in feedback.violation
        and context.can_start_next_cycle
        and context.cycle_close_required
    )


def _force_continue_after_open_finalize_repair(context: SolverContext) -> bool:
    """継続可能なのにopenを残したfinalizeの再出力をschemaでも防ぐ。"""

    feedback = context.contract_feedback
    return bool(
        feedback is not None
        and "finalize must account for every open WorkItem" in feedback.violation
        and not context.finalize_only
        and context.can_start_next_cycle
        and not context.cycle_close_required
    )


def _preserve_previous_update_for_contract_repair(
    context: SolverContext,
) -> bool:
    feedback = context.contract_feedback
    if feedback is None:
        return False
    if _preserve_previous_update_for_cycle_repair(context):
        return True
    return any(
        marker in feedback.violation
        for marker in (
            "focus must reference open WorkItem IDs",
            "tool requests must reference open WorkItem IDs",
            "tool requests reference unknown Hypothesis IDs",
            "unknown retained evidence IDs",
            "retained evidence count exceeds the profile limit",
            "completed dependency decision cannot reference an action request",
            "dependency action must reference a ToolRequest in the same decision",
        )
    )


def _preserve_previous_answer_for_contract_repair(
    context: SolverContext,
    normalized: Mapping[str, Any],
) -> bool:
    """引用の対応付けだけを直す再試行では、検証済みの回答本文を保持する。"""

    feedback = context.contract_feedback
    if feedback is None:
        return False
    previous = feedback.previous_decision
    return bool(
        "answer citations require verified Hypothesis or resolved dependency basis"
        in feedback.violation
        and previous.next == "finalize"
        and previous.answer is not None
        and normalized.get("next") == "finalize"
    )


def _empty_array_schema() -> dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": "string"},
        "minItems": 0,
        "maxItems": 0,
    }


def _bounded_enum_array(
    values: tuple[str, ...],
    *,
    max_items: int | None = None,
) -> dict[str, Any]:
    # Anthropicの構造化出力方言ではmaxItemsを受け付けないため、候補0件を
    # string items + maxItems=0で表すと変換後に任意文字列の配列へ緩む。
    # null要素だけを許す配列にして、空配列以外は復元後の型検証で拒否する。
    items = _enum_string(values) if values else {"type": "null"}
    return {
        "type": "array",
        "items": items,
        "maxItems": (
            min(len(values), max_items)
            if max_items is not None
            else len(values)
        ),
    }


def _enum_string(values: tuple[str, ...]) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string"}
    if values:
        schema["enum"] = list(values)
    return schema


def _normalize_fetch_articles_transport(
    payload: dict[str, Any],
    context: SolverContext | None = None,
) -> None:
    """専用欄だけを本文取得要求として採用する。"""

    has_fetch_sidecar = (
        "fetch_articles" in payload or "article_fetch" in payload
    )
    fetch_articles = payload.pop(
        "fetch_articles",
        payload.pop("article_fetch", None),
    )
    if not has_fetch_sidecar:
        return
    generic_fetch_requests = [
        request
        for request in payload.get("tool_requests") or []
        if isinstance(request, dict)
        and request.get("tool_name") in {"fetch_articles", "article_fetch"}
    ]
    payload["tool_requests"] = [
        request
        for request in payload.get("tool_requests") or []
        if not (
            isinstance(request, dict)
            and request.get("tool_name")
            in {"fetch_articles", "article_fetch"}
        )
    ]
    if fetch_articles is None and context is not None:
        fetchable_article_ids = set(context.fetchable_article_ids)
        payload["tool_requests"].extend(
            request
            for request in generic_fetch_requests
            if isinstance(request.get("arguments"), dict)
            and isinstance(request["arguments"].get("article_ids"), list)
            and request["arguments"]["article_ids"]
            and all(
                isinstance(article_id, str)
                and article_id in fetchable_article_ids
                for article_id in request["arguments"]["article_ids"]
            )
        )
        return
    fetch_requests = (
        fetch_articles
        if isinstance(fetch_articles, list)
        else [fetch_articles]
        if isinstance(fetch_articles, dict)
        else []
    )
    if not fetch_requests:
        return
    for index, fetch_request in enumerate(fetch_requests, start=1):
        if not isinstance(fetch_request, dict):
            continue
        article_ids = [
            fetch_request[key]
            for key in sorted(fetch_request)
            if key.startswith(("article_id_", "article_ref_"))
            and isinstance(fetch_request[key], str)
            and fetch_request[key]
        ]
        for field_name in ("article_refs", "article_ids"):
            values = fetch_request.get(field_name)
            if isinstance(values, list):
                article_ids.extend(
                    item for item in values if isinstance(item, str) and item
                )
        article_ids = list(dict.fromkeys(article_ids))
        work_item_id = fetch_request.get("work_item_id")
        hypothesis_ids = fetch_request.get("hypothesis_ids") or []
        if (
            not hypothesis_ids
            and context is not None
            and isinstance(work_item_id, str)
        ):
            hypothesis_ids = [
                item.hypothesis_id
                for item in context.hypotheses
                if item.work_item_id == work_item_id and item.requires_follow_up
            ]
        request = {
            "request_id": (
                fetch_request.get("request_id") or f"fetch-articles-{index}"
            ),
            "work_item_id": work_item_id,
            "tool_name": "fetch_articles",
            "arguments": {"article_ids": article_ids},
            "purpose": (
                fetch_request.get("purpose")
                or payload.get("decision_reason")
                or "既知Article本文を取得する。"
            ),
            "hypothesis_ids": hypothesis_ids,
        }
        payload.setdefault("tool_requests", []).append(request)


def _normalize_flattened_tool_request_transport(
    request: dict[str, Any],
) -> dict[str, Any]:
    """Providerが平坦化したToolRequestを共通契約へ戻す。"""

    normalized = dict(request)
    tool_name = normalized.get("tool_name")
    if not isinstance(tool_name, str):
        legacy_type = normalized.get("type")
        if isinstance(legacy_type, str):
            tool_name = legacy_type
            normalized["tool_name"] = legacy_type
            normalized.pop("type", None)
    if not isinstance(tool_name, str) or "arguments" in normalized:
        return normalized

    request_fields = {
        "request_id",
        "work_item_id",
        "tool_name",
        "purpose",
        "hypothesis_ids",
        "arguments_json",
    }
    arguments = {
        key: value
        for key, value in normalized.items()
        if key not in request_fields
    }
    if arguments:
        for key in arguments:
            normalized.pop(key, None)
        normalized["arguments"] = arguments
    return normalized


def _normalize_solver_payload(payload: dict) -> dict:
    decision_payload = payload.get("decision_json")
    if isinstance(decision_payload, str):
        try:
            decoded = json.loads(decision_payload)
        except json.JSONDecodeError as exc:
            raise ModelProtocolError(
                f"decision_json invalid_json_at_{exc.pos}"
            ) from exc
        if not isinstance(decoded, dict):
            raise ModelProtocolError("decision_json root must be an object")
    else:
        decoded = payload

    normalized = dict(decoded)
    if isinstance(decision_payload, str) and "fetch_articles" in payload:
        normalized["fetch_articles"] = payload.get("fetch_articles")
    if "update_json" in normalized:
        normalized["update"] = _decode_transport_json(
            normalized.pop("update_json"),
            expected_type=dict,
            label="update_json",
        )
    has_evidence_binding_sidecar = "hypothesis_evidence_bindings" in normalized
    evidence_bindings = normalized.pop("hypothesis_evidence_bindings", None)
    if evidence_bindings is not None:
        _apply_hypothesis_evidence_bindings(normalized, evidence_bindings)
    elif has_evidence_binding_sidecar:
        # Anthropic transportではsidecarがEvidence選択の正本である。
        # 候補0件時のnullは、二重JSON側の予測IDを採用しないことを表す。
        _apply_hypothesis_evidence_bindings(normalized, [])
    if "tool_requests_json" in normalized:
        normalized["tool_requests"] = _decode_transport_json(
            normalized.pop("tool_requests_json"),
            expected_type=list,
            label="tool_requests_json",
        )
    if "dependency_decisions_json" in normalized:
        normalized["dependency_decisions"] = _decode_transport_json(
            normalized.pop("dependency_decisions_json"),
            expected_type=list,
            label="dependency_decisions_json",
        )
    dependency_article_bindings = normalized.pop(
        "dependency_article_bindings",
        None,
    )
    if isinstance(normalized.get("tool_requests"), dict):
        request_slots = normalized["tool_requests"]
        normalized_requests = []
        for key in sorted(request_slots):
            value = request_slots[key]
            if value is None:
                continue
            if isinstance(value, dict) and "request_json" in value:
                request = _decode_transport_json(
                    value["request_json"],
                    expected_type=dict,
                    label=f"{key}.request_json",
                )
                request["tool_name"] = value.get("tool_name")
                normalized_requests.append(request)
            else:
                # 旧transport payloadとの読み取り互換。新schemaでは生成されない。
                normalized_requests.append(
                    _decode_transport_json(
                        value,
                        expected_type=dict,
                        label=key,
                    )
                )
        normalized["tool_requests"] = normalized_requests
    _normalize_fetch_articles_transport(normalized)
    # `next` is the LLM's control decision. The unused answer branch is only
    # transport noise, so remove it without changing that control decision.
    if normalized.get("next") == "continue":
        normalized["answer"] = None
    elif normalized.get("next") == "finalize":
        normalized["start_next_cycle"] = False
        normalized["tool_requests"] = []
        normalized["frontier_re_adoptions"] = []
        answer = normalized.get("answer")
        if isinstance(answer, dict):
            answer = dict(answer)
            if answer.get("limitations") is None or answer.get("limitations") == "":
                answer["limitations"] = []
            normalized["answer"] = answer
    raw_dependencies = normalized.get("dependency_decisions") or []
    if isinstance(raw_dependencies, dict):
        raw_dependencies = [
            _decode_transport_json(
                raw_dependencies[key],
                expected_type=dict,
                label=key,
            )
            for key in sorted(raw_dependencies)
        ]
    dependency_decisions = []
    for raw_dependency in raw_dependencies:
        if not isinstance(raw_dependency, dict):
            dependency_decisions.append(raw_dependency)
            continue
        dependency = dict(raw_dependency)
        status = dependency.get("status")
        if status in {"not_required", "resolved"} or (
            status == "needs_action" and normalized.get("start_next_cycle") is True
        ):
            dependency["action_request_id"] = None
        dependency_decisions.append(dependency)
    normalized["dependency_decisions"] = dependency_decisions
    if dependency_article_bindings is not None:
        normalized["_dependency_article_bindings"] = dependency_article_bindings
    requests = []
    for raw_request in normalized.get("tool_requests") or []:
        if not isinstance(raw_request, dict):
            requests.append(raw_request)
            continue
        request = _normalize_flattened_tool_request_transport(raw_request)
        arguments = request.get("arguments")
        if "arguments_json" in request:
            if arguments is not None:
                raise ModelProtocolError(
                    "tool request cannot contain both arguments and arguments_json"
                )
            arguments = _decode_transport_json(
                request.pop("arguments_json"),
                expected_type=dict,
                label="arguments_json",
            )
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise ModelProtocolError(
                    "tool arguments string is not valid JSON"
                ) from exc
        if not isinstance(arguments, dict):
            raise ModelProtocolError("tool arguments must decode to an object")
        request["arguments"] = arguments
        requests.append(request)
    consolidated_requests = _consolidate_fetch_article_requests(
        requests,
        normalized["dependency_decisions"],
    )
    normalized["tool_requests"] = _consolidate_identical_graph_requests(
        consolidated_requests,
        normalized["dependency_decisions"],
    )
    return normalized


def _consolidate_fetch_article_requests(
    requests: list[Any],
    dependency_decisions: list[Any],
) -> list[Any]:
    """本文取得をWorkItemごとに束ね、所有関係を保つ。"""

    groups: dict[str, list[dict[str, Any]]] = {}
    for request in requests:
        if (
            isinstance(request, dict)
            and request.get("tool_name") == "fetch_articles"
        ):
            groups.setdefault(str(request.get("work_item_id")), []).append(request)

    replacements: dict[int, dict[str, Any]] = {}
    removed_ids: set[int] = set()
    for work_item_id, fetches in groups.items():
        if len(fetches) <= 1:
            continue
        retained = dict(fetches[0])
        retained_arguments = dict(retained["arguments"])
        retained_arguments["article_ids"] = list(
            dict.fromkeys(
                article_id
                for request in fetches
                for article_id in request["arguments"].get("article_ids", ())
            )
        )
        retained["arguments"] = retained_arguments
        retained["hypothesis_ids"] = list(
            dict.fromkeys(
                hypothesis_id
                for request in fetches
                for hypothesis_id in request.get("hypothesis_ids", ())
            )
        )
        replacements[id(fetches[0])] = retained
        removed_ids.update(id(request) for request in fetches[1:])

        old_request_ids = {
            request.get("request_id")
            for request in fetches
            if isinstance(request.get("request_id"), str)
        }
        retained_request_id = retained.get("request_id")
        if isinstance(retained_request_id, str):
            for dependency in dependency_decisions:
                if (
                    isinstance(dependency, dict)
                    and dependency.get("status") == "needs_action"
                    and dependency.get("work_item_id") == work_item_id
                    and dependency.get("action_request_id") in old_request_ids
                ):
                    dependency["action_request_id"] = retained_request_id

    return [
        replacements.get(id(request), request)
        for request in requests
        if id(request) not in removed_ids
    ]


def _consolidate_identical_graph_requests(
    requests: list[Any],
    dependency_decisions: list[Any],
) -> list[Any]:
    """同じWorkItem内の同一Graph探索だけを1回にまとめる。"""

    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for request in requests:
        if (
            not isinstance(request, dict)
            or request.get("tool_name") != "legal_graph_neighbors"
            or not isinstance(request.get("arguments"), dict)
        ):
            continue
        scope = (
            str(request.get("work_item_id")),
            str(request.get("tool_name")),
            json.dumps(
                request["arguments"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        groups.setdefault(scope, []).append(request)

    replacements: dict[int, dict[str, Any]] = {}
    removed_ids: set[int] = set()
    for grouped in groups.values():
        if len(grouped) <= 1:
            continue
        retained = dict(grouped[0])
        retained["hypothesis_ids"] = list(
            dict.fromkeys(
                hypothesis_id
                for request in grouped
                for hypothesis_id in request.get("hypothesis_ids", ())
            )
        )
        replacements[id(grouped[0])] = retained
        removed_ids.update(id(request) for request in grouped[1:])

        grouped_request_ids = {
            request.get("request_id")
            for request in grouped
            if isinstance(request.get("request_id"), str)
        }
        grouped_work_item_ids = {
            request.get("work_item_id")
            for request in grouped
            if isinstance(request.get("work_item_id"), str)
        }
        retained_request_id = retained.get("request_id")
        if isinstance(retained_request_id, str):
            for dependency in dependency_decisions:
                if (
                    isinstance(dependency, dict)
                    and dependency.get("status") == "needs_action"
                    and (
                        dependency.get("action_request_id") in grouped_request_ids
                        or dependency.get("work_item_id") in grouped_work_item_ids
                    )
                ):
                    dependency["action_request_id"] = retained_request_id

    return [
        replacements.get(id(request), request)
        for request in requests
        if id(request) not in removed_ids
    ]


def _normalize_staged_research_payload(
    payload: dict[str, Any],
    *,
    projection: str,
    context: SolverContext,
) -> dict[str, Any]:
    """段階別の意味出力を、意味を変えず共通SolverDecisionへ包む。"""

    if projection == "research_decomposition":
        raw_items = payload.get("work_items") or []
        requirements = payload.get("non_work_item_requirements") or []
        questions = [item.get("question") for item in raw_items]
        if any(not isinstance(question, str) or not question for question in questions):
            raise ModelProtocolError("all WorkItems require a non-empty question")
        if len(questions) != len(set(questions)):
            raise ModelProtocolError("WorkItem questions must be unique")
        if any(not isinstance(item, str) or not item for item in requirements):
            raise ModelProtocolError(
                "all non-WorkItem requirements must be non-empty strings"
            )
        if len(requirements) != len(set(requirements)):
            raise ModelProtocolError("non-WorkItem requirements must be unique")
        work_items = [
            WorkItem(
                work_item_id=f"wi-{index}",
                question=question,
                action_actor=(
                    item.get("action_actor") or item.get("actor_scope")
                ),
            ).model_dump(mode="json")
            for index, (item, question) in enumerate(
                zip(raw_items, questions, strict=True),
                start=1,
            )
        ]
        return {
            "next": "continue",
            "start_next_cycle": False,
            "update": {
                "set_non_work_item_requirements": requirements,
                "add_work_items": work_items,
            },
            "next_focus_work_item_ids": [
                item["work_item_id"] for item in work_items
            ],
            "tool_requests": [],
        }

    known_work_item_ids = {
        item.work_item_id for item in context.work_tree if item.state == "open"
    }
    if projection == "research_hypothesis":
        raw_hypotheses = payload.get("hypotheses") or []
        referenced_work_item_ids = [
            item.get("work_item_id") for item in raw_hypotheses
        ]
        unknown = set(referenced_work_item_ids) - known_work_item_ids
        if unknown:
            raise ModelProtocolError(
                f"hypotheses reference unknown WorkItem IDs: {sorted(unknown)}"
            )
        missing = known_work_item_ids - set(referenced_work_item_ids)
        if missing:
            raise ModelProtocolError(
                f"open WorkItems require at least one Hypothesis: {sorted(missing)}"
            )
        used_hypothesis_ids = {item.hypothesis_id for item in context.hypotheses}
        next_hypothesis_number = 1
        generated_hypothesis_ids: list[str] = []
        for _ in raw_hypotheses:
            while f"h-{next_hypothesis_number}" in used_hypothesis_ids:
                next_hypothesis_number += 1
            hypothesis_id = f"h-{next_hypothesis_number}"
            generated_hypothesis_ids.append(hypothesis_id)
            used_hypothesis_ids.add(hypothesis_id)
            next_hypothesis_number += 1
        hypotheses = [
            Hypothesis(
                hypothesis_id=hypothesis_id,
                work_item_id=item["work_item_id"],
                statement=item["statement"],
                gaps=tuple(item.get("gaps") or ()),
            ).model_dump(mode="json")
            for hypothesis_id, item in zip(
                generated_hypothesis_ids,
                raw_hypotheses,
                strict=True,
            )
        ]
        return {
            "next": "continue",
            "start_next_cycle": False,
            "update": {"add_hypotheses": hypotheses},
            "next_focus_work_item_ids": sorted(known_work_item_ids),
            "tool_requests": [],
        }

    if projection == "research_search":
        known_hypotheses = {
            item.hypothesis_id: item
            for item in context.hypotheses
            if item.requires_follow_up
        }
        requests = []
        for index, item in enumerate(payload.get("search_requests") or [], start=1):
            work_item_id = item.get("work_item_id")
            hypothesis_ids = item.get("hypothesis_ids") or []
            unknown_hypotheses = set(hypothesis_ids) - set(known_hypotheses)
            if unknown_hypotheses:
                raise ModelProtocolError(
                    "search request references unknown Hypothesis IDs: "
                    f"{sorted(unknown_hypotheses)}"
                )
            mismatched = [
                hypothesis_id
                for hypothesis_id in hypothesis_ids
                if known_hypotheses[hypothesis_id].work_item_id != work_item_id
            ]
            if mismatched:
                raise ModelProtocolError(
                    "search request Hypotheses must belong to its WorkItem: "
                    f"{sorted(mismatched)}"
                )
            requests.append(
                {
                    "request_id": f"search-{index}",
                    "work_item_id": work_item_id,
                    "tool_name": "legal_search",
                    "arguments": {
                        "query": item["query"],
                        "doc_types": list(dict.fromkeys(item["doc_types"])),
                        "document_ids": [],
                    },
                    "purpose": item["purpose"],
                    "hypothesis_ids": hypothesis_ids,
                }
            )
        if not requests:
            raise ModelProtocolError("search planning requires at least one request")
        return {
            "next": "continue",
            "start_next_cycle": False,
            "update": {},
            "next_focus_work_item_ids": list(
                dict.fromkeys(item["work_item_id"] for item in requests)
            ),
            "tool_requests": requests,
        }
    raise ModelProtocolError(f"unknown staged research projection: {projection}")


def normalize_staged_research_decision(
    payload: dict[str, Any],
    *,
    projection: str,
    context: SolverContext,
) -> SolverDecision:
    """本番と単独診断で共有する、段階別Research応答の正規化境界。"""

    normalized = _normalize_staged_research_payload(
        payload,
        projection=projection,
        context=context,
    )
    _assign_tool_request_ids(normalized, context)
    _normalize_absent_context_branches(normalized, context)
    decision = SolverDecision.model_validate(normalized)
    _validate_hypothesis_update_evidence(decision, context)
    return decision


def normalize_dependency_action_decision(
    payload: dict[str, Any],
    *,
    context: SolverContext,
) -> SolverDecision:
    """専用のAction出力を、既存のDependency判断を保ったDecisionへ包む。"""

    decoded_payload = dict(payload)
    if "tool_requests_json" in decoded_payload:
        decoded_payload["tool_requests"] = _decode_transport_json(
            decoded_payload.pop("tool_requests_json"),
            expected_type=list,
            label="tool_requests_json",
        )
    _normalize_fetch_articles_transport(decoded_payload, context)
    _assign_tool_request_ids(decoded_payload, context)
    if (
        _dependency_action_must_start_next_cycle(context)
        and not decoded_payload.get("tool_requests")
    ):
        # Providerがwire schemaのenumを外しても、棄却済みActionを
        # 繰り返さずに済む一意な状態遷移へ正規化する。
        decoded_payload["start_next_cycle"] = True
    decoded_payload = _canonicalize_dependency_graph_roots(
        decoded_payload,
        context,
    )
    try:
        action = DependencyActionDecision.model_validate(decoded_payload)
    except ValidationError as exc:
        detail = exc.errors(include_url=False, include_input=False)[0]
        location = ".".join(str(part) for part in detail["loc"])
        raise ModelProtocolError(
            "dependency action contract invalid: "
            f"{location}: {detail['msg']}"
        ) from exc

    if action.start_next_cycle and not context.can_start_next_cycle:
        raise ModelProtocolError("next research Cycle is not available")
    if (
        action.start_next_cycle
        and _dependency_action_requires_graph(context)
    ):
        raise ModelProtocolError(
            "dependency action must inspect the available "
            "Graph before starting the next Cycle"
        )

    rejected_tool_names = _dependency_action_blocked_tool_names(context)
    repeated_tool_names = sorted(
        {
            request.tool_name
            for request in action.tool_requests
            if request.tool_name in rejected_tool_names
        }
    )
    if repeated_tool_names:
        raise ModelProtocolError(
            "dependency action cannot use a blocked Tool kind: "
            + ", ".join(repeated_tool_names)
        )
    if _dependency_action_requires_graph(context):
        invalid_graph_requests = [
            request.request_id
            for request in action.tool_requests
            if request.tool_name != "legal_graph_neighbors"
        ]
        if invalid_graph_requests:
            raise ModelProtocolError(
                "lower-norm Graph action requires legal_graph_neighbors"
            )

    required_ids = set(context.required_dependency_work_item_ids)
    requests_by_work_item: dict[str, list[ToolRequest]] = {}
    for request in action.tool_requests:
        requests_by_work_item.setdefault(request.work_item_id, []).append(request)
    unknown_request_work_items = set(requests_by_work_item) - required_ids
    if unknown_request_work_items:
        raise ModelProtocolError(
            "dependency action references WorkItems outside the needs_action scope: "
            f"{sorted(unknown_request_work_items)}"
        )
    if any(len(requests) != 1 for requests in requests_by_work_item.values()):
        raise ModelProtocolError(
            "dependency action allows at most one ToolRequest per needs_action "
            "WorkItem"
        )

    active_dependencies = []
    for dependency in context.dependency_decisions:
        if dependency.work_item_id not in required_ids:
            continue
        request = (
            None
            if action.start_next_cycle
            else next(
                iter(requests_by_work_item.get(dependency.work_item_id, ())),
                None,
            )
        )
        active_dependencies.append(
            dependency.model_copy(
                update={
                    "action_request_id": (
                        request.request_id if request is not None else None
                    )
                }
            ).model_dump(mode="json")
        )
    if {item["work_item_id"] for item in active_dependencies} != required_ids:
        raise ModelProtocolError(
            "dependency action is missing the prior needs_action decision"
        )

    normalized = _normalize_solver_payload(
        {
            "next": "continue",
            "decision_reason": action.decision_reason,
            "start_next_cycle": action.start_next_cycle,
            "update": {},
            "next_focus_work_item_ids": list(
                context.required_dependency_work_item_ids
            ),
            "dependency_decisions": active_dependencies,
            "tool_requests": [
                request.model_dump(mode="json")
                for request in action.tool_requests
            ],
            "unreviewed_graph_resolution": (
                {
                    "action": "review_next_cycle",
                    "reason": (
                        "次Cycleへの移行に伴い、未評価Graph候補を次Cycleへ"
                        "引き継ぐ。"
                    ),
                }
                if (
                    action.start_next_cycle
                    and context.graph_review_batch.remaining_unreviewed_count > 0
                )
                else None
            ),
            "answer": None,
        }
    )
    _assign_tool_request_ids(normalized, context)
    _normalize_absent_context_branches(normalized, context)
    decision = SolverDecision.model_validate(normalized)
    _validate_hypothesis_update_evidence(decision, context)
    return decision


def _assign_tool_request_ids(
    normalized: dict[str, Any],
    context: SolverContext,
) -> None:
    """永続化用IDを機械採番し、曖昧でない局所参照だけを結び直す。"""

    requests = normalized.get("tool_requests") or []
    if not requests:
        return
    local_ids = [
        request.get("request_id") if isinstance(request, dict) else None
        for request in requests
    ]
    if any(not isinstance(request_id, str) or not request_id for request_id in local_ids):
        return
    local_id_counts = {
        local_id: local_ids.count(local_id) for local_id in set(local_ids)
    }

    used_ids = set(context.used_tool_request_ids)
    assigned_ids: set[str] = set()
    id_map: dict[str, str] = {}
    for request, local_id in zip(requests, local_ids, strict=True):
        assert isinstance(request, dict)
        assert isinstance(local_id, str)
        while True:
            assigned_id = f"solver-tool-{uuid4().hex}"
            if assigned_id not in used_ids and assigned_id not in assigned_ids:
                break
        request["request_id"] = assigned_id
        assigned_ids.add(assigned_id)
        if local_id_counts[local_id] == 1:
            id_map[local_id] = assigned_id

    requests_by_work_item: dict[str, list[str]] = {}
    for request in requests:
        work_item_id = request.get("work_item_id")
        request_id = request.get("request_id")
        if isinstance(work_item_id, str) and isinstance(request_id, str):
            requests_by_work_item.setdefault(work_item_id, []).append(request_id)

    for dependency in normalized.get("dependency_decisions") or []:
        if not isinstance(dependency, dict) or dependency.get("status") != "needs_action":
            continue
        action_request_id = dependency.get("action_request_id")
        if isinstance(action_request_id, str) and action_request_id in id_map:
            dependency["action_request_id"] = id_map[action_request_id]
            continue
        matching_ids = requests_by_work_item.get(dependency.get("work_item_id"), [])
        if (
            action_request_id is None
            or action_request_id in used_ids
        ) and len(matching_ids) == 1:
            dependency["action_request_id"] = matching_ids[0]


def _normalize_search_review_payload(
    payload: dict[str, Any],
    context: SolverContext,
) -> dict[str, Any]:
    """専用輸送を意味判断せず通常のSolverDecisionへ包む。"""

    selected_ids = {
        item.get("article_id")
        for item in payload.get("selections") or []
        if isinstance(item, dict) and isinstance(item.get("article_id"), str)
    }
    selections = [
        dict(item)
        for item in payload.get("selections") or []
        if isinstance(item, dict)
    ]
    review = {
        "search_request_ids": payload.get("search_request_ids") or [],
        "selections": selections,
        "assessments": payload.get("assessments") or [],
        "reason": payload.get("reason") or "検索候補を選択した",
        "deferred_article_ids": [
            item.article_id
            for item in context.search_candidates
            if item.article_id not in selected_ids
        ],
    }
    return {
        "next": "continue",
        "decision_reason": payload.get("reason") or "検索候補を評価した",
        "start_next_cycle": False,
        "update": {},
        "next_focus_work_item_ids": [],
        "retain_evidence_ids": [],
        "review_finding_resolutions": [],
        "dependency_decisions": [],
        "graph_candidate_review": None,
        "search_candidate_review": review,
        "frontier_re_adoptions": [],
        "deferred_frontier_resolutions": [],
        "unreviewed_graph_resolution": None,
        "tool_requests": [],
        "answer": None,
    }


def _normalize_search_assessment_transport_payload(
    payload: dict[str, Any],
    context: SolverContext,
) -> dict[str, Any]:
    """Article IDをキーにした輸送objectを内部の評価配列へ機械変換する。"""

    normalized = deepcopy(payload)
    assessments = normalized.get("assessments")
    if not isinstance(assessments, dict):
        return normalized
    normalized["assessments"] = [
        {
            "article_id": candidate.article_id,
            **assessment,
        }
        for candidate in context.search_candidates
        if isinstance(
            assessment := assessments.get(candidate.article_id),
            dict,
        )
    ]
    return normalized


def _normalize_search_reselection_transport_payload(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """同じArticleの重複選択を、提示順を保った1件へ正規化する。"""

    normalized = deepcopy(payload)
    selections = normalized.get("selections")
    if not isinstance(selections, list):
        return normalized
    merged_by_article: dict[str, dict[str, Any]] = {}
    ordered_article_ids: list[str] = []
    for selection in selections:
        if not isinstance(selection, dict):
            continue
        article_id = selection.get("article_id")
        if not isinstance(article_id, str):
            continue
        existing = merged_by_article.get(article_id)
        if existing is None:
            existing = deepcopy(selection)
            existing["matched_hypothesis_ids"] = list(
                dict.fromkeys(selection.get("matched_hypothesis_ids") or ())
            )
            merged_by_article[article_id] = existing
            ordered_article_ids.append(article_id)
            continue
        existing["matched_hypothesis_ids"] = list(
            dict.fromkeys(
                [
                    *(existing.get("matched_hypothesis_ids") or ()),
                    *(selection.get("matched_hypothesis_ids") or ()),
                ]
            )
        )
    normalized["selections"] = [
        merged_by_article[article_id] for article_id in ordered_article_ids
    ]
    return normalized


def _normalize_search_selection_transport_payload(
    payload: dict[str, Any],
    context: SolverContext,
) -> dict[str, Any]:
    """選択候補の一回答を保存用評価と取得選択へ機械的に分ける。"""

    del context
    merged_items: dict[str, dict[str, Any]] = {}
    ordered_article_ids: list[str] = []
    for item in payload.get("selections") or []:
        if not isinstance(item, dict):
            continue
        article_id = item.get("article_id")
        if not isinstance(article_id, str):
            continue
        existing = merged_items.get(article_id)
        if existing is None:
            existing = deepcopy(item)
            existing["matched_hypothesis_ids"] = list(
                dict.fromkeys(item.get("matched_hypothesis_ids") or ())
            )
            merged_items[article_id] = existing
            ordered_article_ids.append(article_id)
            continue
        existing["matched_hypothesis_ids"] = list(
            dict.fromkeys(
                [
                    *(existing.get("matched_hypothesis_ids") or ()),
                    *(item.get("matched_hypothesis_ids") or ()),
                ]
            )
        )
        for field in ("summary", "reason"):
            previous = existing.get(field)
            current = item.get(field)
            if (
                isinstance(previous, str)
                and isinstance(current, str)
                and current != previous
            ):
                existing[field] = f"{previous} {current}"

    selections: list[dict[str, Any]] = []
    assessments: list[dict[str, Any]] = []
    for article_id in ordered_article_ids:
        item = merged_items[article_id]
        matched_hypothesis_ids = list(
            dict.fromkeys(item.get("matched_hypothesis_ids") or ())
        )
        selections.append(
            {
                "article_id": item.get("article_id"),
                "reason": item.get("reason"),
                "matched_hypothesis_ids": matched_hypothesis_ids,
            }
        )
        assessments.append(
            {
                "article_id": item.get("article_id"),
                "legal_function": item.get("legal_function"),
                "summary": item.get("summary"),
                "matched_hypothesis_ids": matched_hypothesis_ids,
            }
        )
    normalized = {
        "selections": selections,
        "assessments": assessments,
        "reason": payload.get("reason"),
    }
    return normalized


def _validate_selected_search_assessments(
    payload: dict[str, Any],
    context: SolverContext,
) -> None:
    """選択候補だけに意味評価があり、選択と評価が対応するか検証する。"""

    del context
    selections = payload.get("selections") or []
    assessments = payload.get("assessments") or []
    selected_ids = [
        item.get("article_id") for item in selections if isinstance(item, dict)
    ]
    assessment_ids = [
        item.get("article_id") for item in assessments if isinstance(item, dict)
    ]
    if len(selected_ids) != len(set(selected_ids)):
        raise ModelProtocolError("selected search candidates must be unique")
    if assessment_ids != selected_ids:
        raise ModelProtocolError(
            "selected search candidate assessments must match selections"
        )
    try:
        SearchAssessmentDecision.model_validate({"assessments": assessments})
    except ValidationError as exc:
        detail = exc.errors(include_url=False, include_input=False)[0]
        raise ModelProtocolError(
            f"search assessment contract invalid: {detail['msg']}"
        ) from exc


def _attach_selected_hypotheses_to_assessments(
    assessment_payload: dict[str, Any],
    selection_payload: dict[str, Any],
) -> dict[str, Any]:
    """選択時の対応判断を、保存する候補評価へ機械的に転記する。"""

    selected_by_article = {
        item.get("article_id"): list(item.get("matched_hypothesis_ids") or ())
        for item in selection_payload.get("selections") or []
        if isinstance(item, dict) and isinstance(item.get("article_id"), str)
    }
    normalized = deepcopy(assessment_payload)
    for assessment in normalized.get("assessments") or []:
        if not isinstance(assessment, dict):
            continue
        assessment["matched_hypothesis_ids"] = selected_by_article.get(
            assessment.get("article_id"),
            [],
        )
    return normalized


def _validate_search_assessment_payload(
    payload: dict[str, Any],
    context: SolverContext,
) -> None:
    assessment_ids = [
        item.get("article_id")
        for item in payload.get("assessments") or []
        if isinstance(item, dict)
    ]
    expected_ids = {item.article_id for item in context.search_candidates}
    if len(assessment_ids) != len(set(assessment_ids)):
        raise ModelProtocolError("search assessments must be unique")
    if set(assessment_ids) != expected_ids:
        raise ModelProtocolError("search assessments must cover every candidate")
    known_hypothesis_ids = {
        item.hypothesis_id for item in context.hypotheses
    }
    for assessment in payload.get("assessments") or []:
        matched_ids = assessment.get("matched_hypothesis_ids") or []
        if len(matched_ids) != len(set(matched_ids)):
            raise ModelProtocolError(
                "search assessment hypothesis IDs must be unique"
            )
        if not set(matched_ids).issubset(known_hypothesis_ids):
            raise ModelProtocolError(
                "search assessment references unknown hypothesis IDs"
            )
    try:
        SearchAssessmentDecision.model_validate(
            {"assessments": payload.get("assessments") or []}
        )
    except ValidationError as exc:
        detail = exc.errors(include_url=False, include_input=False)[0]
        raise ModelProtocolError(
            f"search assessment contract invalid: {detail['msg']}"
        ) from exc


def _validate_search_reselection_payload(
    payload: dict[str, Any],
    context: SolverContext,
) -> None:
    """選択が提示済みArticle・Hypothesis IDだけを参照するか検証する。"""

    known_article_ids = {item.article_id for item in context.search_candidates}
    known_hypothesis_ids = {
        item.hypothesis_id for item in context.hypotheses
    }
    for selection in payload.get("selections") or []:
        if not isinstance(selection, dict):
            continue
        article_id = selection.get("article_id")
        selected_ids = selection.get("matched_hypothesis_ids") or []
        if article_id not in known_article_ids:
            raise ModelProtocolError(
                "selected search candidate is unknown: "
                f"{article_id}"
            )
        if not selected_ids or not set(selected_ids).issubset(
            known_hypothesis_ids
        ):
            raise ModelProtocolError(
                "selected search candidate hypothesis IDs must be known and "
                f"non-empty: {article_id}"
            )


def _apply_hypothesis_evidence_bindings(
    normalized: dict[str, Any],
    raw_bindings: Any,
) -> None:
    """Apply only the Evidence IDs selected in the provider-constrained sidecar."""

    if not isinstance(raw_bindings, list):
        raise ModelProtocolError("hypothesis_evidence_bindings must be an array")
    bindings: dict[str, list[str]] = {}
    for index, raw_binding in enumerate(raw_bindings):
        if not isinstance(raw_binding, dict):
            raise ModelProtocolError(
                f"hypothesis_evidence_bindings[{index}] must be an object"
            )
        hypothesis_id = raw_binding.get("hypothesis_id")
        evidence_ids = raw_binding.get("evidence_ids")
        if not isinstance(hypothesis_id, str) or not hypothesis_id:
            raise ModelProtocolError(
                f"hypothesis_evidence_bindings[{index}].hypothesis_id is invalid"
            )
        if hypothesis_id in bindings:
            raise ModelProtocolError(
                "hypothesis_evidence_bindings hypothesis IDs must be unique"
            )
        if not isinstance(evidence_ids, list) or any(
            not isinstance(evidence_id, str) for evidence_id in evidence_ids
        ):
            raise ModelProtocolError(
                f"hypothesis_evidence_bindings[{index}].evidence_ids is invalid"
            )
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ModelProtocolError(
                "hypothesis_evidence_bindings evidence IDs must be unique"
            )
        bindings[hypothesis_id] = evidence_ids

    update = normalized.get("update")
    if not isinstance(update, dict):
        raise ModelProtocolError(
            "hypothesis_evidence_bindings requires a decoded update object"
        )
    referenced_hypothesis_ids: set[str] = set()
    for field_name in ("add_hypotheses", "update_hypotheses"):
        raw_items = update.get(field_name) or []
        if not isinstance(raw_items, list):
            raise ModelProtocolError(f"update.{field_name} must be an array")
        normalized_items = []
        for index, raw_item in enumerate(raw_items):
            if not isinstance(raw_item, dict):
                raise ModelProtocolError(
                    f"update.{field_name}[{index}] must be an object"
                )
            item = dict(raw_item)
            hypothesis_id = item.get("hypothesis_id")
            if isinstance(hypothesis_id, str):
                referenced_hypothesis_ids.add(hypothesis_id)
                item["evidence_ids"] = bindings.get(hypothesis_id, [])
            normalized_items.append(item)
        update[field_name] = normalized_items
    unknown_binding_ids = set(bindings) - referenced_hypothesis_ids
    if unknown_binding_ids:
        raise ModelProtocolError(
            "hypothesis_evidence_bindings reference hypotheses absent from update: "
            f"{sorted(unknown_binding_ids)}"
        )


def _apply_dependency_article_bindings(
    dependency_decisions: list[Any],
    raw_bindings: Any,
    context: SolverContext,
) -> None:
    """Expand LLM-selected material Article IDs into their known Evidence IDs."""

    if not isinstance(raw_bindings, list):
        raise ModelProtocolError("dependency_article_bindings must be an array")
    bindings: dict[str, list[str]] = {}
    for index, raw_binding in enumerate(raw_bindings):
        if not isinstance(raw_binding, dict):
            raise ModelProtocolError(
                f"dependency_article_bindings[{index}] must be an object"
            )
        work_item_id = raw_binding.get("work_item_id")
        article_ids = raw_binding.get("article_ids")
        if not isinstance(work_item_id, str) or not work_item_id:
            raise ModelProtocolError(
                f"dependency_article_bindings[{index}].work_item_id is invalid"
            )
        if work_item_id in bindings:
            raise ModelProtocolError(
                "dependency_article_bindings work item IDs must be unique"
            )
        if not isinstance(article_ids, list) or any(
            not isinstance(article_id, str) for article_id in article_ids
        ):
            raise ModelProtocolError(
                f"dependency_article_bindings[{index}].article_ids is invalid"
            )
        if len(article_ids) != len(set(article_ids)):
            raise ModelProtocolError(
                "dependency_article_bindings Article IDs must be unique"
            )
        bindings[work_item_id] = article_ids

    evidence_ids_by_article: dict[str, list[str]] = {}
    for evidence in context.material_evidence:
        article_id = evidence.metadata.get("articleId")
        if isinstance(article_id, str) and article_id:
            evidence_ids_by_article.setdefault(article_id, []).append(
                evidence.evidence_id
            )

    decision_work_item_ids: set[str] = set()
    for index, dependency in enumerate(dependency_decisions):
        if not isinstance(dependency, dict):
            raise ModelProtocolError(
                f"dependency_decisions[{index}] must be an object"
            )
        work_item_id = dependency.get("work_item_id")
        if not isinstance(work_item_id, str) or not work_item_id:
            raise ModelProtocolError(
                f"dependency_decisions[{index}].work_item_id is invalid"
            )
        decision_work_item_ids.add(work_item_id)
        article_ids = bindings.get(work_item_id, [])
        unknown_article_ids = set(article_ids) - set(evidence_ids_by_article)
        if unknown_article_ids:
            raise ModelProtocolError(
                "dependency_article_bindings reference Articles absent from "
                f"material evidence: {sorted(unknown_article_ids)}"
            )
        dependency["basis_evidence_ids"] = [
            evidence_id
            for article_id in article_ids
            for evidence_id in evidence_ids_by_article[article_id]
        ]

    if set(bindings) != decision_work_item_ids:
        raise ModelProtocolError(
            "dependency_article_bindings must match dependency decision work "
            f"items: expected={sorted(decision_work_item_ids)}, "
            f"actual={sorted(bindings)}"
        )


def _normalize_article_fetch_requests(
    normalized: dict[str, Any],
    context: SolverContext,
) -> None:
    """本文取得要求のIDを解決し、同じWorkItemの取得済みIDを除く。"""

    article_aliases = _article_fetch_alias_map(context)
    retained_requests = []
    removed_request_ids: set[str] = set()
    for request in normalized.get("tool_requests") or []:
        if (
            not isinstance(request, dict)
            or request.get("tool_name") != "fetch_articles"
        ):
            retained_requests.append(request)
            continue
        arguments = request.get("arguments")
        if not isinstance(arguments, dict):
            retained_requests.append(request)
            continue
        article_ids = arguments.get("article_ids")
        if not isinstance(article_ids, list):
            retained_requests.append(request)
            continue
        request_work_item_id = request.get("work_item_id")
        fetched_article_ids = set(
            context.fetched_resource_ids_by_work_item.get(
                request_work_item_id
                if isinstance(request_work_item_id, str)
                else "",
                (),
            )
        )
        if not context.fetched_resource_ids_by_work_item:
            # Older replay artifacts do not have WorkItem-scoped acquisition
            # state. Preserve their former case-wide fallback behavior.
            fetched_article_ids.update(
                context.fetched_resource_ids_by_work_item_this_cycle.get(
                    request_work_item_id
                    if isinstance(request_work_item_id, str)
                    else "",
                    (),
                )
            )
            fetched_article_ids.update(context.fetched_resource_ids_this_cycle)
            fetched_article_ids.update(
                article_id
                for evidence in context.material_evidence
                if evidence.evidence_id in context.grounding_evidence_ids
                if isinstance(
                    article_id := evidence.metadata.get("articleId"),
                    str,
                )
                and article_id
            )
        resolved_article_ids = [
            article_aliases.get(article_id, article_id)
            for article_id in article_ids
        ]
        remaining_article_ids = [
            article_id
            for article_id in resolved_article_ids
            if article_id not in fetched_article_ids
        ]
        if not remaining_article_ids:
            request_id = request.get("request_id")
            if isinstance(request_id, str):
                removed_request_ids.add(request_id)
            continue
        arguments["article_ids"] = remaining_article_ids
        retained_requests.append(request)

    normalized["tool_requests"] = retained_requests
    if not removed_request_ids:
        return
    for dependency in normalized.get("dependency_decisions") or []:
        if (
            isinstance(dependency, dict)
            and dependency.get("action_request_id") in removed_request_ids
        ):
            dependency["action_request_id"] = None


def _normalize_absent_context_branches(
    normalized: dict[str, Any],
    context: SolverContext,
) -> None:
    """現在の処理で意味を持たない制御欄を機械的に空へ揃える。"""

    _normalize_grounding_evidence_aliases(normalized, context)

    if context.finalize_only or normalized.get("next") == "finalize":
        # Evidenceの引継ぎは次Cycle用であり、最終回答では保存対象を選ばない。
        normalized["retain_evidence_ids"] = []

    if not (
        normalized.get("next") == "finalize"
        or normalized.get("start_next_cycle") is True
    ):
        normalized["deferred_frontier_resolutions"] = []
        normalized["unreviewed_graph_resolution"] = None

    dependency_article_bindings = normalized.pop(
        "_dependency_article_bindings",
        None,
    )
    if dependency_article_bindings is not None:
        _apply_dependency_article_bindings(
            normalized.get("dependency_decisions") or [],
            dependency_article_bindings,
            context,
        )

    _normalize_article_fetch_requests(normalized, context)
    for request in normalized.get("tool_requests") or []:
        if not isinstance(request, dict):
            continue
        arguments = request.get("arguments")
        if not isinstance(arguments, dict):
            continue
        if request.get("tool_name") == "load_evidence":
            evidence_ids = arguments.get("evidence_ids")
            if isinstance(evidence_ids, list):
                grounding_aliases: dict[str, list[str]] = {}
                for evidence in context.material_evidence:
                    article_id = evidence.metadata.get("articleId")
                    if (
                        evidence.evidence_id in context.grounding_evidence_ids
                        and isinstance(article_id, str)
                    ):
                        grounding_aliases.setdefault(article_id, []).append(
                            evidence.evidence_id
                        )
                normalized_evidence_ids: list[str] = []
                for evidence_id in evidence_ids:
                    for resolved_id in grounding_aliases.get(
                        evidence_id, [evidence_id]
                    ):
                        if resolved_id not in normalized_evidence_ids:
                            normalized_evidence_ids.append(resolved_id)
                arguments["evidence_ids"] = normalized_evidence_ids

    fetch_requests = [
        request
        for request in normalized.get("tool_requests") or []
        if isinstance(request, dict)
        and request.get("tool_name") == "fetch_articles"
    ]
    fetch_groups: dict[str, list[dict[str, Any]]] = {}
    if context.remaining_fetch_capacity_by_work_item:
        for request in fetch_requests:
            work_item_id = request.get("work_item_id")
            if isinstance(work_item_id, str):
                fetch_groups.setdefault(work_item_id, []).append(request)
    elif fetch_requests:
        # Older replay fixtures predate WorkItem-scoped capacity. Preserve their
        # single shared fetch group so historical diagnostics remain replayable.
        fetch_groups[""] = fetch_requests

    for work_item_id, requests in fetch_groups.items():
        current_limit = context.remaining_fetch_capacity_by_work_item.get(
            work_item_id,
            context.remaining_fetch_capacity,
        )
        if current_limit <= 0:
            continue
        primary_request = requests[0]
        requested_article_ids = list(
            dict.fromkeys(
                article_id
                for request in requests
                for article_id in request.get("arguments", {}).get(
                    "article_ids",
                    (),
                )
                if isinstance(article_id, str)
            )
        )
        primary_request.setdefault("arguments", {})["article_ids"] = (
            requested_article_ids[:current_limit]
        )
        primary_request["hypothesis_ids"] = list(
            dict.fromkeys(
                hypothesis_id
                for request in requests
                for hypothesis_id in request.get("hypothesis_ids", ())
                if isinstance(hypothesis_id, str)
            )
        )
        primary_request_id = primary_request.get("request_id")
        merged_request_ids = {
            request.get("request_id")
            for request in requests[1:]
            if isinstance(request.get("request_id"), str)
        }
        for dependency in normalized.get("dependency_decisions") or []:
            if (
                isinstance(dependency, dict)
                and dependency.get("action_request_id") in merged_request_ids
            ):
                dependency["action_request_id"] = primary_request_id
        normalized["tool_requests"] = [
            request
            for request in normalized.get("tool_requests") or []
            if request is primary_request or request not in requests[1:]
        ]

    if not context.cycle_close_required:
        validation_groups: dict[str, list[dict[str, Any]]] = {}
        for request in normalized.get("tool_requests") or []:
            if not isinstance(request, dict) or request.get("tool_name") != "fetch_articles":
                continue
            work_item_id = (
                request.get("work_item_id")
                if context.remaining_fetch_capacity_by_work_item
                and isinstance(request.get("work_item_id"), str)
                else ""
            )
            validation_groups.setdefault(work_item_id, []).append(request)
        for work_item_id, requests in validation_groups.items():
            requested_article_ids = {
                article_id
                for request in requests
                for article_id in request.get("arguments", {}).get(
                    "article_ids",
                    (),
                )
                if isinstance(article_id, str)
            }
            current_limit = context.remaining_fetch_capacity_by_work_item.get(
                work_item_id,
                context.remaining_fetch_capacity,
            )
            if len(requested_article_ids) > current_limit:
                scope = f" for WorkItem {work_item_id}" if work_item_id else ""
                raise ModelProtocolError(
                    "fetch_articles requests"
                    f"{scope} must contain at most {current_limit} unique "
                    "Article IDs; the LLM must choose the current verification set"
                )

    if (
        context.cycle_close_required
        and context.can_start_next_cycle
        and normalized.get("next") == "continue"
    ):
        # `continue`というLLM判断を保ったまま、取得枠を使い切ったCycleの
        # 唯一の合法な制御形（Toolなしで次Cycleへ移る）へ正規化する。
        normalized["start_next_cycle"] = True
        normalized["tool_requests"] = []
        for dependency in normalized.get("dependency_decisions") or []:
            if (
                isinstance(dependency, dict)
                and dependency.get("status") == "needs_action"
            ):
                dependency["action_request_id"] = None

    if not context.graph_review_batch.candidates:
        normalized["graph_candidate_review"] = None
    normalized["search_candidate_review"] = None
    if not context.graph_review_ledger:
        normalized["frontier_re_adoptions"] = []
    all_active_deferred = _all_active_deferred_frontiers(context)
    active_deferred = _llm_active_deferred_frontiers(context)
    raw_deferred_resolutions = normalized.get("deferred_frontier_resolutions")
    active_deferred_by_id = {
        item.frontier_item_id: item for item in active_deferred
    }
    if isinstance(raw_deferred_resolutions, dict):
        normalized["deferred_frontier_resolutions"] = [
            {
                **resolution,
                "frontier_item_id": frontier_id,
                "article_id": active_deferred_by_id[frontier_id].article_id,
                "work_item_id": active_deferred_by_id[frontier_id].work_item_id,
                "hypothesis_id": active_deferred_by_id[
                    frontier_id
                ].hypothesis_id,
            }
            for frontier_id, resolution in raw_deferred_resolutions.items()
            if frontier_id in active_deferred_by_id
            and isinstance(resolution, dict)
        ]
    elif isinstance(raw_deferred_resolutions, list):
        canonical_resolutions = []
        for raw_resolution in raw_deferred_resolutions:
            if not isinstance(raw_resolution, dict):
                canonical_resolutions.append(raw_resolution)
                continue
            frontier_id = raw_resolution.get("frontier_item_id")
            known = active_deferred_by_id.get(frontier_id)
            if known is None:
                canonical_resolutions.append(raw_resolution)
                continue
            canonical_resolutions.append(
                {
                    **raw_resolution,
                    "article_id": known.article_id,
                    "work_item_id": known.work_item_id,
                    "hypothesis_id": known.hypothesis_id,
                }
            )
        normalized["deferred_frontier_resolutions"] = canonical_resolutions
    if not active_deferred:
        normalized["deferred_frontier_resolutions"] = []
    if context.finalize_only:
        llm_frontier_ids = {
            item.frontier_item_id for item in active_deferred
        }
        automatic_resolutions = [
            {
                "frontier_item_id": item.frontier_item_id,
                "article_id": item.article_id,
                "work_item_id": item.work_item_id,
                "hypothesis_id": item.hypothesis_id,
                "action": "no_longer_needed",
                "reason": "所属WorkItemが終了済みのため、最終回答後へ保持しない。",
            }
            for item in all_active_deferred
            if item.frontier_item_id not in llm_frontier_ids
        ]
        normalized["deferred_frontier_resolutions"] = [
            *(normalized.get("deferred_frontier_resolutions") or []),
            *automatic_resolutions,
        ]
    if context.graph_review_batch.remaining_unreviewed_count == 0:
        normalized["unreviewed_graph_resolution"] = None
    if not context.reviewer_findings:
        normalized["review_finding_resolutions"] = []


def _decode_transport_json(value: Any, *, expected_type: type, label: str) -> Any:
    if not isinstance(value, str):
        raise ModelProtocolError(f"{label} must be a JSON string")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ModelProtocolError(f"{label} invalid_json_at_{exc.pos}") from exc
    if not isinstance(decoded, expected_type):
        raise ModelProtocolError(f"{label} has an invalid root type")
    return decoded


_INITIAL_RESEARCH_SOLVER_CONTRACT = """
出力原則:
- Provider schemaに従い、CaseState全体ではなく今回の差分だけを返す。
- continueを返し、answerやCycle終了判断は返さない。
- updateには新しいWorkItemとHypothesisだけを返す。

状態契約:
- WorkItemはopen、resolutionはnullにする。
- Hypothesisはunresolved、evidence_idsは空にする。
- next_focus_work_item_idsには、今回優先するopen WorkItem IDを指定する。

Tool契約:
- tool_requestsは、Solverが次にProgramへ実行させるTool名と引数を返す出力である。
- 各要求を、今回検証するopen WorkItemとHypothesisへ結び付ける。
- Tool名とargumentsはavailable_toolsの名前とinput_schemaに一致させる。
- request_idは同じDecision内で重複しない短い局所IDにする。
""".strip()


_MINIMAL_SOLVER_CONTRACT = """
出力原則:
- Provider schemaに従い、CaseState全体ではなく今回の差分だけを返す。
- decision_reasonには、提示された根拠・gap・上限から今回continueまたはfinalizeを選ぶ理由を一文で書く。内部思考の逐語記録や長い検討過程は書かない。
- 正規契約のupdateに許されるキーはadd_work_items、update_work_items、add_hypotheses、update_hypotheses、impact_decisionsだけ。work_tree等の現在状態を返さない。
- continueは同Cycleの次step、またはstart_next_cycle=trueによる次Cycle開始であり、answerは返さない。
- finalizeは追加Toolを返さず、通常完了では全WorkItemを閉じる。上限時の限定回答だけ未解決IDとlimitationsを対応させる。

updateの状態契約:
- add_work_items要素: work_item_id、parent_work_item_id、question、state、resolution、basis_hypothesis_ids、replaces_work_item_id。statusは使わない。
- update_work_items要素: work_item_id、state、resolution、basis_hypothesis_ids。
- add_hypotheses要素: hypothesis_id、work_item_id、statement、judgment、evidence_ids、gaps。statusは使わない。
- update_hypotheses要素: hypothesis_id、judgment、evidence_ids、add_gaps、resolve_gap_ids。既存gaps全体は返さない。
- 各差分配列では同じWorkItem IDまたはHypothesis IDを複数回返さず、今回適用する最終差分を1件だけ返す。
- WorkItemのstate=openは未完了なのでresolution=null、resolved/droppedは終了状態なので空でないresolutionを持つ。
- next_focus_work_item_idsと各ToolRequest.work_item_idは、このupdate適用後もstate=openのWorkItemだけを参照する。Toolが必要ならWorkItemを閉じない。
- Hypothesisのjudgment=unresolvedは未確認、supported/contradictedは本文根拠で確認済みなので空でないevidence_idsを持つ。
- impact_decisions要素: work_item_id、action、reason、new_basis_hypothesis_ids、replacement_work_item_id、drop_subtree。既存Hypothesisをcontradictedへ変える場合だけ使い、actionはretain / replace / dropのいずれか。それ以外は空配列にする。
- required_dependency_work_item_idsがあれば各WorkItemのDependencyDecisionを1件ずつ返す。not_required/resolvedはaction_request_id=null。needs_actionは通常は同じDecisionのToolを参照するが、Cycle境界でstart_next_cycle=trueならToolを返さずaction_request_id=nullにする。
- 通常finalizeでは現在openの全WorkItemを同じupdateでresolved/droppedへ閉じる。未確認なら閉じずcontinueし、上限時だけ未解決IDとlimitationsを対応させる。
- finalize時のanswer.citation_idsには、resolved WorkItemのbasis Hypothesisが選んだEvidenceを漏れなく含める。不要なEvidenceならHypothesis側から外す。

参照契約:
- 既存のWorkItem、Hypothesis、Evidence、Articleを参照するIDは、SolverContextに表示された値だけを完全一致で使う。Article IDやEvidence IDを名前から生成しない。
- add_work_itemsとadd_hypothesesでは新しいIDを作る。ToolRequestのrequest_idは同じDecision内だけで重複しない短い局所IDとし、Programが永続化用IDへ置き換える。
- retain_evidence_idsはmax_retained_evidence件以内で、次Cycleにも本文提示が必要なEvidenceだけを選ぶ。
- reviewer_findingsがあれば、review_finding_resolutionsで全finding_idを1回ずつ処理する。指摘を受け入れて回答修正または追加調査へ反映する場合はaddressed、提示済み本文と照合して指摘を採用しない場合だけdisputedとし、reasonと実際に使ったbasis_evidence_idsを返す。reviewer_findingsがなければ空配列にする。
- statusの意味、根拠の十分性、追加調査、Graph候補の採否はsystem promptに従ってSolverが判断する。
- 対象がない任意配列は空、任意objectはnull、更新がなければupdateは空objectにする。
""".strip()


_DEPENDENCY_ACTION_CONTRACT = """
出力原則:
- 今回の処理では`decision_reason`、`start_next_cycle`、`tool_requests`だけを返す。
- ToolRequestは、対応するopen WorkItemと未確認Hypothesisへ結び付ける。
- 各ToolRequestには`request_id`、`work_item_id`、`tool_name`、`arguments`、`purpose`、`hypothesis_ids`を含める。`purpose`には、その要求で確認する未確認事項を短く書く。
- `action_feedback`がある場合は、棄却されたTool種類を使わず、別種のToolまたはstart_next_cycle=trueを選ぶ。
- 現在Cycleに有効な行動があればstart_next_cycle=falseとし、処理上限内で進める`needs_action` WorkItemについて、未確認事項を直接進めるToolRequestを1件返す。今回選ばないWorkItemは次stepへ残す。
- 重複しない有効なTool要求がなく次Cycleを開始できる場合は、start_next_cycle=true、tool_requests=[]にする。
- request_idは同じ出力内で重複しない短い局所IDにする。Programが永続化用IDへ置き換え、既存のDependencyDecisionへ対応付ける。
- Tool名とargumentsは`available_tools`に従う。
""".strip()
