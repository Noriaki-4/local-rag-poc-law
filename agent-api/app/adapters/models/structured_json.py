"""既存provider共通JSON transportを新FrameworkのModel Portへ接続する。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from time import monotonic
from typing import Any
from uuid import uuid4

import requests
from pydantic import BaseModel, ValidationError

from app.agent_framework.context import (
    ContextCapacityExceeded,
    ResearchStepHypothesis,
    ResearchStepInput,
    ResearchStepWorkItem,
    SearchAssessmentCandidate,
    SearchAssessmentExcerpt,
    SearchAssessmentHypothesis,
    SearchAssessmentInput,
    SearchAssessmentWorkItem,
    SolverContext,
    WorkTreeItem,
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
    EvidenceIntegrationDecision,
    HypothesisUpdate,
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
    SearchCandidateSelection,
    UnreviewedGraphResolution,
    ToolRequest,
    WorkItem,
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

    def solve(
        self,
        context: SolverContext,
        profile: ModelCallProfile,
    ) -> SolverCallResult:
        provider = getattr(self._client, "provider", None)
        if profile.context_projection == "research_hypothesis":
            context = _project_next_hypothesis_work_item(context)
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
        if profile.context_projection == "observation_integration":
            return self._solve_observation_only(context, profile)
        if profile.context_projection == "cycle_close":
            return self._solve_cycle_close(context, profile)
        dependency_action_call = _is_dependency_action_call(context, profile)
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
                        _assign_tool_request_ids(normalized, context)
                        _normalize_absent_context_branches(normalized, context)
                        if _preserve_previous_update_for_contract_repair(context):
                            normalized["update"] = (
                                context.contract_feedback.previous_decision.update
                            )
                        decision = SolverDecision.model_validate(normalized)
                        _validate_hypothesis_update_evidence(decision)
                    if self._diagnostics is not None:
                        self._diagnostics.record_transport_output(
                            context=context,
                            repair_index=repair_index,
                            payload=result.payload,
                            validation_error=None,
                            input_tokens=result.inputTokens,
                            output_tokens=result.outputTokens,
                            provider_retry_count=result.retryCount,
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

    def _solve_observation_only(
        self,
        context: SolverContext,
        profile: ModelCallProfile,
    ) -> SolverCallResult:
        """Persist new Tool observations before asking for another action."""

        started_at = monotonic()
        observation, input_tokens, output_tokens, attempt_count = (
            self._solve_observation_integrations(
                context,
                profile,
                started_at=started_at,
            )
        )
        (
            observation,
            dependency_input_tokens,
            dependency_output_tokens,
            dependency_retry_count,
            dependency_assessed,
        ) = self._solve_dependency_assessment(
            context,
            observation,
            profile,
            started_at=started_at,
            prior_input_tokens=input_tokens,
            prior_output_tokens=output_tokens,
        )
        return SolverCallResult(
            decision=SolverDecision(
                next="continue",
                decision_reason=observation.decision_reason,
                update=CaseUpdate(
                    update_hypotheses=observation.update_hypotheses,
                ),
                dependency_decisions=observation.dependency_decisions,
            ),
            input_tokens=_sum_optional_tokens(
                input_tokens,
                dependency_input_tokens,
            ),
            output_tokens=_sum_optional_tokens(
                output_tokens,
                dependency_output_tokens,
            ),
            attempt_count=(
                attempt_count
                + dependency_retry_count
                + int(dependency_assessed)
            ),
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

        dependency_call = render_dependency_assessment_model_call(
            context,
            observation,
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
                context=context,
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
                context=context,
                repair_index=0,
                payload=dependency_result.payload,
                validation_error=dependency_error,
                input_tokens=dependency_result.inputTokens,
                output_tokens=dependency_result.outputTokens,
                provider_retry_count=dependency_result.retryCount,
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
                    context=context,
                )
            )
            dependency = _downgrade_unproven_dependency_confirmations(
                dependency,
                article_id_by_evidence={
                    item.evidence_id: article_id
                    for item in context.material_evidence
                    if item.evidence_id in context.grounding_evidence_ids
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
        observation = observation.model_copy(
            update={"dependency_decisions": dependency.dependency_decisions}
        )
        observation = _align_observation_with_dependency_decisions(observation)
        return (
            observation,
            dependency_result.inputTokens,
            dependency_result.outputTokens,
            dependency_result.retryCount,
            True,
        )

    def _solve_cycle_close(
        self,
        context: SolverContext,
        profile: ModelCallProfile,
    ) -> SolverCallResult:
        """取得本文の意味統合とCycle境界判断を別コンテキストで実行する。"""

        started_at = monotonic()
        (
            observation,
            observation_input_tokens,
            observation_output_tokens,
            observation_attempt_count,
        ) = self._solve_observation_integrations(
            context,
            profile,
            started_at=started_at,
        )

        (
            observation,
            dependency_input_tokens,
            dependency_output_tokens,
            dependency_retry_count,
            dependency_assessed,
        ) = self._solve_dependency_assessment(
            context,
            observation,
            profile,
            started_at=started_at,
            prior_input_tokens=observation_input_tokens,
            prior_output_tokens=observation_output_tokens,
        )
        completed_stage = (
            "dependency_assessment"
            if dependency_assessed
            else "observation_integration"
        )

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
                input_tokens=_sum_optional_tokens(
                    observation_input_tokens,
                    dependency_input_tokens,
                ),
                output_tokens=_sum_optional_tokens(
                    observation_output_tokens,
                    dependency_output_tokens,
                ),
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
            raise _cycle_close_checkpoint_timeout(
                "cycle close transition timed out",
                observation=observation,
                completed_stage=completed_stage,
                input_tokens=_sum_optional_tokens(
                    observation_input_tokens,
                    dependency_input_tokens,
                ),
                output_tokens=_sum_optional_tokens(
                    observation_output_tokens,
                    dependency_output_tokens,
                ),
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
                transport_stage="cycle_close",
            )
        if transition_error is not None or transition_result.payload is None:
            raise ModelProtocolError(
                f"cycle close transport invalid: {transition_error}"
            )
        answer_check_input_tokens = 0
        answer_check_output_tokens = 0
        answer_check_retry_count = 0
        answer_check_attempted = False
        try:
            transition = CycleCloseDecision.model_validate(
                transition_result.payload
            )
        except ValidationError as exc:
            detail = exc.errors(include_url=False, include_input=False)[0]
            raise ModelProtocolError(
                f"cycle close contract invalid: {detail['msg']}"
            ) from exc

        if transition.answer is not None:
            answer_check_call = render_final_answer_check_model_call(
                context,
                observation,
                transition.answer,
                profile,
            )
            remaining_timeout = profile.timeout_sec - (monotonic() - started_at)
            if remaining_timeout <= 1:
                raise _cycle_close_checkpoint_timeout(
                    "final answer check time exhausted",
                    observation=observation,
                    completed_stage="cycle_close",
                    input_tokens=_sum_optional_tokens(
                        observation_input_tokens,
                        dependency_input_tokens,
                        transition_result.inputTokens,
                    ),
                    output_tokens=_sum_optional_tokens(
                        observation_output_tokens,
                        dependency_output_tokens,
                        transition_result.outputTokens,
                    ),
                )
            if self._diagnostics is not None:
                self._diagnostics.record_transport_input(
                    context=context,
                    profile=profile,
                    rendered=answer_check_call,
                    repair_index=0,
                    transport_stage="final_answer_check",
                    provider=getattr(self._client, "provider", None),
                )
            try:
                answer_check_result = self._client.generate_structured_json(
                    prompt=answer_check_call.request,
                    schema=answer_check_call.output_schema,
                    model=profile.model,
                    max_tokens=profile.max_output_tokens,
                    timeout_sec=max(1, round(remaining_timeout)),
                )
            except requests.Timeout as exc:
                raise _cycle_close_checkpoint_timeout(
                    "final answer check timed out",
                    observation=observation,
                    completed_stage="cycle_close",
                    input_tokens=_sum_optional_tokens(
                        observation_input_tokens,
                        dependency_input_tokens,
                        transition_result.inputTokens,
                    ),
                    output_tokens=_sum_optional_tokens(
                        observation_output_tokens,
                        dependency_output_tokens,
                        transition_result.outputTokens,
                    ),
                ) from exc
            answer_check_error = answer_check_result.validationError
            if answer_check_result.payload is None and answer_check_error is None:
                answer_check_error = "empty"
            if self._diagnostics is not None:
                self._diagnostics.record_transport_output(
                    context=context,
                    repair_index=0,
                    payload=answer_check_result.payload,
                    validation_error=answer_check_error,
                    input_tokens=answer_check_result.inputTokens,
                    output_tokens=answer_check_result.outputTokens,
                    provider_retry_count=answer_check_result.retryCount,
                    transport_stage="final_answer_check",
                )
            if answer_check_error is not None or answer_check_result.payload is None:
                raise ModelProtocolError(
                    "final answer check transport invalid: "
                    f"{answer_check_error}"
                )
            checked_text = answer_check_result.payload.get("text")
            required_answer_ids = _required_answer_evidence_ids(
                context,
                observation,
            )
            if not isinstance(checked_text, str) or not checked_text.strip():
                raise ModelProtocolError("final answer check requires answer text")
            transition = transition.model_copy(
                update={
                    "answer": transition.answer.model_copy(
                        update={
                            "text": checked_text,
                            "citation_ids": required_answer_ids,
                        }
                    )
                }
            )
            answer_check_input_tokens = answer_check_result.inputTokens
            answer_check_output_tokens = answer_check_result.outputTokens
            answer_check_retry_count = answer_check_result.retryCount
            answer_check_attempted = True

        decision = _normalize_cycle_close_decisions(
            observation,
            transition,
        )

        input_tokens = _sum_optional_tokens(
            observation_input_tokens,
            dependency_input_tokens,
            transition_result.inputTokens,
            answer_check_input_tokens,
        )
        output_tokens = _sum_optional_tokens(
            observation_output_tokens,
            dependency_output_tokens,
            transition_result.outputTokens,
            answer_check_output_tokens,
        )
        return SolverCallResult(
            decision=decision,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            attempt_count=(
                1
                + observation_attempt_count
                + dependency_retry_count
                + transition_result.retryCount
                + int(dependency_assessed)
                + int(answer_check_attempted)
                + answer_check_retry_count
            ),
        )

    def _solve_observation_integrations(
        self,
        context: SolverContext,
        profile: ModelCallProfile,
        *,
        started_at: float,
    ) -> tuple[ObservationIntegrationDecision, int | None, int | None, int]:
        """各open WorkItemの本文評価を独立した単一タスクとして実行する。"""

        observations: list[ObservationIntegrationDecision] = []
        input_tokens: list[int | None] = []
        output_tokens: list[int | None] = []
        attempt_count = 0
        for projected_context in _observation_work_item_contexts(context):
            rendered = render_observation_integration_model_call(
                projected_context,
                profile,
            )
            if self._diagnostics is not None:
                self._diagnostics.record_transport_input(
                    context=projected_context,
                    profile=profile,
                    rendered=rendered,
                    repair_index=0,
                    transport_stage="observation_integration",
                    provider=getattr(self._client, "provider", None),
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
                    timeout_sec=max(1, round(remaining_timeout)),
                )
            except requests.Timeout as exc:
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
                    transport_stage="observation_integration",
                )
            if error is not None or result.payload is None:
                raise ModelProtocolError(
                    f"observation integration transport invalid: {error}"
                )
            try:
                observations.append(
                    ObservationIntegrationDecision.model_validate(
                        _normalize_observation_integration_payload(
                            result.payload,
                            context=projected_context,
                        )
                    )
                )
            except ValidationError as exc:
                detail = exc.errors(include_url=False, include_input=False)[0]
                raise ModelProtocolError(
                    "observation integration contract invalid: "
                    f"{detail['msg']}"
                ) from exc
            input_tokens.append(result.inputTokens)
            output_tokens.append(result.outputTokens)
            attempt_count += 1 + result.retryCount

        return (
            _merge_observation_integrations(observations),
            _sum_optional_tokens(*input_tokens),
            _sum_optional_tokens(*output_tokens),
            attempt_count,
        )

    def _solve_search_review(
        self,
        context: SolverContext,
        profile: ModelCallProfile,
    ) -> SolverCallResult:
        """候補理解と比較選択を別コンテキストで順に実行する。"""

        provider = getattr(self._client, "provider", None)
        assessment_results = []
        assessed_items: list[dict[str, Any]] = []
        for batch_context in _search_review_batch_contexts(context):
            assessment_call = render_search_assessment_model_call(
                batch_context,
                profile,
                provider=provider,
            )
            if self._diagnostics is not None:
                self._diagnostics.record_transport_input(
                    context=batch_context,
                    profile=profile,
                    rendered=assessment_call,
                    repair_index=0,
                    transport_stage="search_assessment",
                    provider=provider,
                )
            try:
                assessment_result = self._client.generate_structured_json(
                    prompt=assessment_call.request,
                    schema=assessment_call.output_schema,
                    model=profile.model,
                    max_tokens=profile.max_output_tokens,
                    timeout_sec=max(1, round(profile.timeout_sec)),
                )
            except requests.Timeout as exc:
                raise TimeoutError("search assessment timed out") from exc
            assessment_error = assessment_result.validationError
            if assessment_result.payload is None and assessment_error is None:
                assessment_error = "empty"
            if self._diagnostics is not None:
                self._diagnostics.record_transport_output(
                    context=batch_context,
                    repair_index=0,
                    payload=assessment_result.payload,
                    validation_error=assessment_error,
                    input_tokens=assessment_result.inputTokens,
                    output_tokens=assessment_result.outputTokens,
                    provider_retry_count=assessment_result.retryCount,
                    transport_stage="search_assessment",
                )
            if assessment_error is not None or assessment_result.payload is None:
                raise ModelProtocolError(
                    f"search assessment transport invalid: {assessment_error}"
                )
            batch_payload = _normalize_search_assessment_transport_payload(
                assessment_result.payload,
                batch_context,
            )
            _validate_search_assessment_payload(batch_payload, batch_context)
            assessed_items.extend(batch_payload.get("assessments") or [])
            assessment_results.append(assessment_result)

        assessment_payload = {
            "assessments": assessed_items,
        }
        _validate_search_assessment_payload(assessment_payload, context)

        selection_call = render_search_reselection_model_call(
            context,
            assessment_payload,
            profile,
        )
        selection_prompt = selection_call.request
        selection_schema = selection_call.output_schema
        if self._diagnostics is not None:
            self._diagnostics.record_transport_input(
                context=context,
                profile=profile,
                rendered=selection_call,
                repair_index=0,
                transport_stage="search_reselection",
                provider=getattr(self._client, "provider", None),
            )
        try:
            selection_result = self._client.generate_structured_json(
                prompt=selection_prompt,
                schema=selection_schema,
                model=profile.model,
                max_tokens=profile.max_output_tokens,
                timeout_sec=max(1, round(profile.timeout_sec)),
            )
        except requests.Timeout as exc:
            if self._diagnostics is not None:
                self._diagnostics.record_transport_timeout(
                    context=context,
                    repair_index=0,
                    reason="search reselection timed out",
                    transport_stage="search_reselection",
                )
            raise TimeoutError("search reselection timed out") from exc
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
                transport_stage="search_reselection",
            )
        if selection_error is not None or selection_result.payload is None:
            raise ModelProtocolError(
                f"search reselection transport invalid: {selection_error}"
            )
        selection_payload = _normalize_search_reselection_transport_payload(
            selection_result.payload
        )
        try:
            SearchReselectionDecision.model_validate(selection_payload)
            _validate_search_reselection_payload(
                selection_payload,
                assessment_payload,
            )
        except ValidationError as exc:
            detail = exc.errors(include_url=False, include_input=False)[0]
            raise ModelProtocolError(
                f"search reselection contract invalid: {detail['msg']}"
            ) from exc

        combined_payload = {
            "search_request_ids": list(
                context.required_search_review_request_ids
            ),
            **assessment_payload,
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

        input_tokens = _sum_optional_tokens(
            *(result.inputTokens for result in assessment_results),
            selection_result.inputTokens,
        )
        output_tokens = _sum_optional_tokens(
            *(result.outputTokens for result in assessment_results),
            selection_result.outputTokens,
        )
        return SolverCallResult(
            decision=decision,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            attempt_count=(
                len(assessment_results)
                + 1
                + sum(result.retryCount for result in assessment_results)
                + selection_result.retryCount
            ),
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
        )
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
    initial_research = profile.context_projection == "initial_research"
    output_schema = (
        _strip_runtime_id_enums(
            _initial_research_transport_schema(projected_context)
        )
        if initial_research
        else _solver_anthropic_json_transport_schema(projected_context)
        if provider == "anthropic"
        else _solver_common_transport_schema(projected_context)
    )
    dependency_action = _is_dependency_action_call(projected_context, profile)
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
        and profile.context_projection == "full"
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
    """最終回答へ、解決済みWorkItemの根拠本文だけを投影する。"""

    hypotheses_by_id = {
        item.hypothesis_id: item for item in context.hypotheses
    }
    resolved_work_item_ids = {
        item.work_item_id
        for item in context.work_tree
        if item.state == "resolved"
    }
    resolved_basis_evidence_ids = {
        evidence_id
        for work_item in context.work_tree
        if work_item.work_item_id in resolved_work_item_ids
        for hypothesis_id in work_item.basis_hypothesis_ids
        for evidence_id in hypotheses_by_id[hypothesis_id].evidence_ids
    }
    resolved_basis_evidence_ids.update(
        evidence_id
        for decision in context.dependency_decisions
        if decision.work_item_id in resolved_work_item_ids
        and decision.status == "resolved"
        for evidence_id in decision.basis_evidence_ids
    )
    visible_grounding_ids = tuple(
        evidence_id
        for evidence_id in context.grounding_evidence_ids
        if evidence_id in resolved_basis_evidence_ids
        and any(
            item.evidence_id == evidence_id
            for item in context.material_evidence
        )
    )
    visible_grounding_id_set = set(visible_grounding_ids)
    return context.model_copy(
        update={
            "available_tools": (),
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
        }
    )


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
    if projection == "full" and context.required_dependency_work_item_ids:
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
            if active_hypothesis_ids.intersection(
                item["matched_hypothesis_ids"]
                or item["discovery_hypothesis_ids"]
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
        return payload
    if projection == "finalization":
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
        return payload
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
            "graph_review_ledger": payload["graph_review_ledger"],
            "required_graph_review_request_ids": payload[
                "required_graph_review_request_ids"
            ],
            "graph_review_selection_limit": (
                context.graph_review_selection_limit
            ),
        }
    work_item_by_id = {item.work_item_id: item for item in context.work_tree}
    research_input = ResearchStepInput(
        question=context.question,
        work_items=tuple(
            ResearchStepWorkItem(
                work_item_id=item.work_item_id,
                question=item.question,
                action_actor=item.action_actor,
            )
            for item in context.work_tree
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
            for item in context.hypotheses
            if item.judgment == "unresolved"
        ),
        available_tools=context.available_tools,
        max_tool_requests_per_step=context.max_tool_requests_per_step,
    )
    if projection == "research_decomposition":
        return research_input.model_dump(mode="json", include={"question"})
    if projection == "research_hypothesis":
        return {
            "work_items": [
                {
                    "work_item_id": item.work_item_id,
                    "question": item.question,
                    "action_actor": item.action_actor,
                }
                for item in research_input.work_items
            ],
        }
    if projection == "research_search":
        return research_input.model_dump(
            mode="json",
            include={
                "question",
                "work_items",
                "hypotheses",
                "available_tools",
                "max_tool_requests_per_step",
            },
        )
    if projection != "initial_research":
        raise ValueError(f"unknown solver context projection: {projection}")
    included_fields = (
        "case_id",
        "question",
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
                        "質問の明示要求のうち法的論点ではない要求。"
                        "法的結論を裏付ける根拠条文・出典・引用の提示や"
                        "出力形式等を含む。法的根拠自体が質問対象なら"
                        "work_itemsに入れる。"
                    ),
                },
            }
        )
    work_item_ids = tuple(item.work_item_id for item in context.work_tree)
    if projection == "research_hypothesis":
        hypothesis = _strict_object(
            {
                "work_item_id": _enum_string(work_item_ids),
                "statement": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                    "description": (
                        "WorkItemへの回答を構成し得る、法令本文の一つの規定内容で"
                        "個別に支持又は否定できる1つの法的命題。一般的な法的知識を"
                        "使ってよいが、WorkItemにない行為者、具体的な数値又は条文番号を"
                        "確定事項として作らない。規定の存在や検索"
                        "方針ではなく、確認する法的命題を書く。"
                    ),
                },
                "gaps": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "maxItems": 8,
                    "description": (
                        "statementに残る、法令本文で確認すべき具体的な規律要素。"
                        "該当する要素がなければ空にする。行為者が不明で結論を左右す"
                        "る場合は、その役割の確認も含める。根拠条文の提示や検索作業"
                        "は書かない。"
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
                        "提示されたWorkItemを検証するHypothesis。独立して適用され得る"
                        "条件、義務又は回答事項ごとに返す。"
                    ),
                }
            }
        )
    if projection == "research_search":
        hypothesis_ids = tuple(
            item.hypothesis_id
            for item in context.hypotheses
            if item.judgment == "unresolved"
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
) -> RenderedModelCall:
    input_payload = _observation_integration_context_payload(context)
    instructions = (
        f"{profile.system_prompt}\n\n"
        f"{RUNTIME_INPUT_MARKER}"
        f"{_post_context_completion_check(profile.completion_check_prompt)}"
    )
    rendered = build_rendered_model_call(
        stage="observation_integration",
        instructions=instructions,
        input_tag="observation_input",
        input_payload=input_payload,
        output_schema=_strip_runtime_id_enums(
            _observation_integration_transport_schema(context)
        ),
        normalized_schema=EvidenceIntegrationDecision.model_json_schema(),
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
) -> dict[str, Any]:
    grounding_ids = set(context.grounding_evidence_ids)
    active_work_items, active_hypotheses = _active_observation_items(context)
    payload: dict[str, Any] = {
        "question": context.question,
        "work_items": [
            item.model_dump(mode="json") for item in active_work_items
        ],
        "hypotheses": [
            item.model_dump(mode="json") for item in active_hypotheses
        ],
        "evidence_hypothesis_candidates": [
            item.model_dump(mode="json")
            for item in context.evidence_hypothesis_candidates
            if set(item.hypothesis_ids).intersection(
                hypothesis.hypothesis_id for hypothesis in active_hypotheses
            )
        ],
        "grounding_evidence": [
            item.model_dump(mode="json")
            for item in context.material_evidence
            if item.evidence_id in grounding_ids
        ],
    }
    if context.contract_feedback is not None:
        previous = context.contract_feedback.previous_decision
        active_work_item_ids = {
            item.work_item_id for item in active_work_items
        }
        active_hypothesis_ids = {
            item.hypothesis_id for item in active_hypotheses
        }
        payload["contract_feedback"] = {
            "violation": context.contract_feedback.violation,
        }
        payload["previous_observation"] = {
            "decision_reason": previous.decision_reason,
            "update_work_items": [
                item.model_dump(mode="json")
                for item in previous.update.update_work_items
                if item.work_item_id in active_work_item_ids
            ],
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
    """前段LLMが結び付けたArticleだけをWorkItem単位で本文評価へ投影する。"""

    active_work_items, active_hypotheses = _active_observation_items(context)
    if not active_work_items:
        return (context,)
    grounding_ids = set(context.grounding_evidence_ids)
    has_candidate_mappings = bool(context.evidence_hypothesis_candidates)
    projected: list[SolverContext] = []
    for work_item in active_work_items:
        hypotheses = tuple(
            item
            for item in active_hypotheses
            if item.work_item_id == work_item.work_item_id
        )
        hypothesis_ids = {item.hypothesis_id for item in hypotheses}
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
            if set(item.hypothesis_ids).intersection(hypothesis_ids)
        )
        candidate_article_ids = {item.article_id for item in candidates}
        existing_evidence_ids = {
            evidence_id
            for hypothesis in hypotheses
            for evidence_id in hypothesis.evidence_ids
        }
        visible_ids = tuple(
            item.evidence_id
            for item in context.material_evidence
            if item.evidence_id in grounding_ids
            and (
                not has_candidate_mappings
                or item.metadata.get("articleId") in candidate_article_ids
                or item.evidence_id in existing_evidence_ids
            )
        )
        visible_id_set = set(visible_ids)
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
                    "evidence_hypothesis_candidates": candidates,
                }
            )
        )
    return tuple(projected)


def _merge_observation_integrations(
    observations: list[ObservationIntegrationDecision],
) -> ObservationIntegrationDecision:
    """独立したWorkItem評価を、意味を変えず一つのCycle差分へ連結する。"""

    if not observations:
        return ObservationIntegrationDecision(
            decision_reason="評価対象のopen WorkItemはない。"
        )
    return ObservationIntegrationDecision(
        decision_reason=" ".join(
            item.decision_reason for item in observations
        ),
        update_work_items=tuple(
            update
            for item in observations
            for update in item.update_work_items
        ),
        update_hypotheses=tuple(
            update
            for item in observations
            for update in item.update_hypotheses
        ),
        dependency_decisions=tuple(
            decision
            for item in observations
            for decision in item.dependency_decisions
        ),
    )


def _active_observation_items(
    context: SolverContext,
) -> tuple[tuple[WorkTreeItem, ...], tuple[Hypothesis, ...]]:
    """通常の逐次統合対象を、現在openの作業へ限定する。"""

    work_items = tuple(
        item for item in context.work_tree if item.state == "open"
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
    required_answer_evidence_ids = _required_answer_evidence_ids(
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
    active_deferred = [
        item.model_dump(mode="json")
        for item in context.graph_review_ledger
        if item.review_status == "relevant_deferred"
        and item.content_status in {"not_requested", "failed", "timeout"}
        and item.deferred_resolution_action != "no_longer_needed"
    ]
    payload: dict[str, Any] = {
        "question": context.question,
        "non_work_item_requirements": list(context.non_work_item_requirements),
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
        "can_start_next_cycle": context.can_start_next_cycle,
        "remaining_research_cycles": context.remaining_research_cycles,
        "max_retained_evidence": context.max_retained_evidence,
        "retainable_evidence": [
            item.model_dump(mode="json")
            for item in context.evidence_manifest
            if item.evidence_id in retainable_evidence_ids
        ],
        "grounding_evidence": [
            item.model_dump(mode="json")
            for item in context.material_evidence
            if item.evidence_id in set(required_answer_evidence_ids)
        ],
        "required_answer_evidence_ids": list(required_answer_evidence_ids),
        "active_deferred_frontiers": active_deferred,
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
        hypotheses[update.hypothesis_id] = current.model_copy(
            update={
                "judgment": update.judgment,
                "evidence_ids": update.evidence_ids,
                "gaps": update.gaps,
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
    active_work_items, active_hypotheses = _active_observation_items(context)
    work_item_ids = tuple(item.work_item_id for item in active_work_items)
    hypothesis_ids = tuple(item.hypothesis_id for item in active_hypotheses)
    schema_work_item_ids = work_item_ids or tuple(
        item.work_item_id for item in context.work_tree
    )
    schema_hypothesis_ids = hypothesis_ids or tuple(
        item.hypothesis_id for item in context.hypotheses
    )
    evidence_ids = context.grounding_evidence_ids
    nullable_string = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    work_item_update = _strict_object(
        {
            "work_item_id": _described(
                _enum_string(schema_work_item_ids),
                WorkItemUpdate,
                "work_item_id",
            ),
            "state": _described(
                {"type": "string", "enum": ["open", "resolved", "dropped"]},
                WorkItemUpdate,
                "state",
            ),
            "resolution": _described(
                nullable_string,
                WorkItemUpdate,
                "resolution",
            ),
            "basis_hypothesis_ids": _described(
                _bounded_enum_array(schema_hypothesis_ids),
                WorkItemUpdate,
                "basis_hypothesis_ids",
            ),
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
            "gaps": _described(
                {"type": "array", "items": {"type": "string"}, "maxItems": 12},
                HypothesisUpdate,
                "gaps",
            ),
        }
    )
    return _strict_object(
        {
            "decision_reason": {
                "type": "string",
                "minLength": 1,
                "maxLength": 1200,
                "description": ObservationIntegrationDecision.model_fields[
                    "decision_reason"
                ].description,
            },
            "update_work_items": {
                "type": "array",
                "items": work_item_update,
                "maxItems": len(work_item_ids),
                "description": ObservationIntegrationDecision.model_fields[
                    "update_work_items"
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
    normalized = deepcopy(payload)
    if context is not None:
        _normalize_grounding_evidence_aliases(normalized, context)
    for decision in normalized.get("dependency_decisions") or []:
        if not isinstance(decision, dict):
            continue
        decision["status"] = {
            "terminal_text_missing": "needs_action",
            "terminal_text_confirmed": "resolved",
        }.get(decision.get("status"), decision.get("status"))
    return normalized


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


def _align_observation_with_dependency_decisions(
    observation: ObservationIntegrationDecision,
) -> ObservationIntegrationDecision:
    """下位規範が未確認のWorkItemを、Cycle境界でopenに保つ。"""

    needs_action_ids = {
        item.work_item_id
        for item in observation.dependency_decisions
        if item.status == "needs_action"
    }
    if not needs_action_ids:
        return observation
    updates = tuple(
        item.model_copy(
            update={
                "state": "open",
                "resolution": None,
                "basis_hypothesis_ids": (),
            }
        )
        if item.work_item_id in needs_action_ids
        else item
        for item in observation.update_work_items
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
                    "末端下位規範の本文を確認済み。"
                ),
            },
            "reason": _described(
                {"type": "string", "minLength": 1},
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
    projected_work_items, projected_hypotheses = (
        _project_observation_integration_state(context, observation)
    )
    open_work_item_ids = tuple(
        item.work_item_id
        for item in projected_work_items
        if item.state == "open"
    )
    unresolved_hypothesis_ids = tuple(
        item.hypothesis_id
        for item in projected_hypotheses
        if item.judgment == "unresolved"
    )
    evidence_ids = context.grounding_evidence_ids
    retainable_evidence_ids = _retainable_cycle_evidence_ids(
        context,
        observation,
    )
    answer = _strict_object(
        {
            "text": _described({"type": "string"}, FinalAnswer, "text"),
            "citation_ids": _described(
                _bounded_enum_array(evidence_ids),
                FinalAnswer,
                "citation_ids",
            ),
            "limitations": _described(
                (
                    _string_array_schema()
                    if open_work_item_ids or unresolved_hypothesis_ids
                    else _empty_array_schema()
                ),
                FinalAnswer,
                "limitations",
            ),
            "unresolved_work_item_ids": _described(
                _bounded_enum_array(open_work_item_ids),
                FinalAnswer,
                "unresolved_work_item_ids",
            ),
            "unresolved_hypothesis_ids": _described(
                _bounded_enum_array(unresolved_hypothesis_ids),
                FinalAnswer,
                "unresolved_hypothesis_ids",
            ),
        }
    )
    dependency_needs_action = any(
        item.status == "needs_action"
        for item in observation.dependency_decisions
    )
    must_start_next_cycle = (
        context.can_start_next_cycle
        and bool(
            open_work_item_ids
            or unresolved_hypothesis_ids
            or dependency_needs_action
        )
    )
    properties: dict[str, Any] = {
        "outcome": {
            "type": "string",
            "enum": (
                ["start_next_cycle"]
                if must_start_next_cycle
                else ["finalize"]
            ),
            "description": CycleCloseDecision.model_fields["outcome"].description,
        },
        "decision_reason": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1200,
            "description": CycleCloseDecision.model_fields[
                "decision_reason"
            ].description,
        },
        "next_focus_work_item_ids": {
            **_bounded_enum_array(open_work_item_ids),
            "description": CycleCloseDecision.model_fields[
                "next_focus_work_item_ids"
            ].description,
        },
        "retain_evidence_ids": {
            **_bounded_enum_array(
                retainable_evidence_ids,
                max_items=context.max_retained_evidence,
            ),
            "description": CycleCloseDecision.model_fields[
                "retain_evidence_ids"
            ].description,
        },
        "answer": {
            "anyOf": (
                [{"type": "null"}]
                if must_start_next_cycle
                else [answer, {"type": "null"}]
            ),
            "description": CycleCloseDecision.model_fields["answer"].description,
        },
    }
    active_deferred = tuple(
        item
        for item in context.graph_review_ledger
        if item.review_status == "relevant_deferred"
        and item.content_status in {"not_requested", "failed", "timeout"}
        and item.deferred_resolution_action != "no_longer_needed"
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
    observation: ObservationIntegrationDecision,
    transition: CycleCloseDecision,
) -> SolverDecision:
    start_next_cycle = transition.outcome == "start_next_cycle"
    return SolverDecision(
        next="continue" if start_next_cycle else "finalize",
        decision_reason=transition.decision_reason,
        start_next_cycle=start_next_cycle,
        update=CaseUpdate(
            update_work_items=observation.update_work_items,
            update_hypotheses=observation.update_hypotheses,
        ),
        next_focus_work_item_ids=transition.next_focus_work_item_ids,
        retain_evidence_ids=transition.retain_evidence_ids,
        dependency_decisions=observation.dependency_decisions,
        deferred_frontier_resolutions=(
            transition.deferred_frontier_resolutions
        ),
        unreviewed_graph_resolution=transition.unreviewed_graph_resolution,
        answer=transition.answer,
    )


def _cycle_close_checkpoint_timeout(
    message: str,
    *,
    observation: ObservationIntegrationDecision,
    completed_stage: str,
    input_tokens: int | None,
    output_tokens: int | None,
) -> SolverCheckpointTimeout:
    return SolverCheckpointTimeout(
        message,
        partial_decision=SolverDecision(
            next="continue",
            decision_reason=(
                f"{completed_stage}で確定した更新を保存し、後続処理の時間切れを"
                "安全な最終化へ引き継ぐ。"
            ),
            update=CaseUpdate(
                update_work_items=observation.update_work_items,
                update_hypotheses=observation.update_hypotheses,
            ),
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
        f"{render_model_input_glossary(SearchAssessmentInput)}\n\n"
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
    """検索抜粋を候補ごとに機械結合したSearch Review専用View。"""

    evidence_by_id = {
        item.evidence_id: item for item in context.material_evidence
    }
    input_model = SearchAssessmentInput(
        question=context.question,
        work_tree=tuple(
            SearchAssessmentWorkItem(
                work_item_id=item.work_item_id,
                question=item.question,
            )
            for item in context.work_tree
        ),
        hypotheses=tuple(
            SearchAssessmentHypothesis(
                hypothesis_id=item.hypothesis_id,
                work_item_id=item.work_item_id,
                statement=item.statement,
                gaps=item.gaps,
            )
            for item in context.hypotheses
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
    eligible_hypothesis_ids_by_article: dict[str, tuple[str, ...]] = {}
    for item in assessment_payload.get("assessments") or []:
        if not isinstance(item, dict):
            continue
        article_id = item.get("article_id")
        if not isinstance(article_id, str):
            continue
        selectable_ids = tuple(
            hypothesis_id
            for hypothesis_id in item.get("matched_hypothesis_ids") or []
        )
        if not selectable_ids:
            continue
        eligible_hypothesis_ids_by_article[article_id] = selectable_ids
        eligible_assessments.append(
            {
                "article_id": article_id,
                "legal_function": item.get("legal_function"),
                "summary": item.get("summary"),
                "matched_hypothesis_ids": list(selectable_ids),
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
    current_fetch_request_capacity = _tool_array_argument_capacity(
        context,
        tool_name="fetch_articles",
        argument_name="article_ids",
        fallback=context.remaining_fetch_capacity,
    )
    input_payload = {
        "question": context.question,
        "hypotheses": [
            item.model_dump(mode="json") for item in context.hypotheses
        ],
        "current_fetch_request_capacity": current_fetch_request_capacity,
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
    if isinstance(tool_requests, dict) and any(
        message in error_detail
        for message in (
            "all fetch_articles requests combined must contain at most",
            "Article body fetches in one SolverDecision must be consolidated",
        )
    ):
        # Usually each fetch request is bounded separately. During this repair the
        # aggregate one-request rule must also be visible in provider grammar.
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


def _validate_hypothesis_update_evidence(decision: SolverDecision) -> None:
    """状態適用後に必ず失敗するHypothesis更新を輸送修復へ戻す。"""

    if any(
        item.judgment in {"supported", "contradicted"} and not item.evidence_ids
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
        item.work_item_id for item in context.work_tree if item.state == "open"
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
    selectable_ledger = tuple(
        item
        for item in context.graph_review_ledger
        if (
            item.review_status == "relevant_deferred"
            and item.content_status in {"not_requested", "failed", "timeout"}
        )
        or (
            item.review_status == "selected"
            and item.content_status in {"failed", "timeout"}
        )
    )
    selectable_frontier_ids = tuple(
        dict.fromkeys(
            [
                *batch_frontier_ids,
                *(item.frontier_item_id for item in selectable_ledger),
            ]
        )
    )
    graph_article_ids = tuple(
        dict.fromkeys(
            [
                *(item.article_id for item in batch_candidates),
                *(item.article_id for item in selectable_ledger),
            ]
        )
    )
    graph_work_item_ids = tuple(
        dict.fromkeys(
            [
                *(item.work_item_id for item in batch_candidates),
                *(item.work_item_id for item in selectable_ledger),
            ]
        )
    )
    graph_hypothesis_ids = tuple(
        dict.fromkeys(
            item.hypothesis_id
            for item in (*batch_candidates, *selectable_ledger)
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
                {"type": "string"},
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
                    "maxItems": len(selectable_frontier_ids),
                },
                GraphCandidateReview,
                "frontier_decisions",
            ),
            "reason": _described(
                {"type": "string"},
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
    active_deferred = tuple(
        item
        for item in context.graph_review_ledger
        if item.review_status == "relevant_deferred"
        and item.content_status in {"not_requested", "failed", "timeout"}
        and item.deferred_resolution_action != "no_longer_needed"
    )
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
    hypothesis_ids = tuple(item.hypothesis_id for item in context.hypotheses)
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
                {"type": "string", "minLength": 1},
                SearchCandidateAssessment,
                "summary",
            ),
            "matched_hypothesis_ids": {
                **_bounded_enum_array(hypothesis_ids),
                "description": (
                    "見出しと検索抜粋が同じ行為と規律を直接扱い、本文を確認する"
                    "価値があるHypothesis ID。同じ制度名や語句を含むだけの場合は"
                    "含めない。行為者の一致は次の独立処理で確認する。"
                ),
            },
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
        selection_limit = _tool_array_argument_capacity(
            context,
            tool_name="fetch_articles",
            argument_name="article_ids",
            fallback=context.remaining_fetch_capacity,
        )
    selection_variants = [
        _strict_object(
            {
                "article_id": _described(
                    _enum_string((article_id,)),
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
                        **_bounded_enum_array(
                            hypothesis_ids_by_article.get(article_id, ())
                        ),
                        "minItems": 1,
                    },
                    SearchCandidateSelection,
                    "matched_hypothesis_ids",
                ),
            }
        )
        for article_id in candidate_ids
    ]
    selection_item_schema = (
        selection_variants[0]
        if len(selection_variants) == 1
        else {"anyOf": selection_variants}
        if selection_variants
        else _strict_object(
            {
                "article_id": {"type": "string"},
                "reason": {"type": "string"},
                "matched_hypothesis_ids": _empty_array_schema(),
            }
        )
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


def _dependency_action_transport_schema(
    context: SolverContext,
    *,
    json_transport: bool = False,
) -> dict[str, Any]:
    """下位規範の次Actionだけを返す専用契約。"""

    if json_transport:
        return _strict_object(
            {
                "decision_reason": _described(
                    {"type": "string", "minLength": 1, "maxLength": 1200},
                    DependencyActionDecision,
                    "decision_reason",
                ),
                "tool_requests_json": {
                    "type": "string",
                    "description": "ToolRequest array encoded as one JSON array string",
                },
            }
        )
    required_ids = set(context.required_dependency_work_item_ids)
    action_context = context.model_copy(
        update={
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
    tool_requests = _tool_requests_transport_schema(action_context)
    required_count = len(context.required_dependency_work_item_ids)
    tool_requests["minItems"] = required_count
    tool_requests["maxItems"] = required_count
    return _strict_object(
        {
            "decision_reason": _described(
                {"type": "string", "minLength": 1, "maxLength": 1200},
                DependencyActionDecision,
                "decision_reason",
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


def _graph_review_transport_schema(context: SolverContext) -> dict[str, Any]:
    """Graph Review専用の直列化契約を共通契約から切り出す。"""

    schema = _solver_compact_transport_schema(context)
    return deepcopy(schema["properties"]["graph_candidate_review"])


def _solver_common_transport_schema(context: SolverContext) -> dict[str, Any]:
    """全Providerへ渡す、現在の処理に必要な意味項目だけのschema。"""

    schema = _solver_compact_transport_schema(context)
    properties = schema["properties"]
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
        if context.evidence_manifest:
            included.add("retain_evidence_ids")
        if context.reviewer_findings:
            included.add("review_finding_resolutions")
        if context.required_dependency_work_item_ids:
            included.add("dependency_decisions")
        if context.graph_review_ledger:
            included.add("frontier_re_adoptions")
        if any(
            item.review_status == "relevant_deferred"
            and item.content_status in {"not_requested", "failed", "timeout"}
            and item.deferred_resolution_action != "no_longer_needed"
            for item in context.graph_review_ledger
        ):
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
                _bounded_enum_array(context.grounding_evidence_ids),
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
    _constrain_active_deferred_frontier_ids(schema, context)
    if (
        context.contract_feedback is not None
        and "tool request references unknown Article IDs"
        in context.contract_feedback.violation
    ):
        _constrain_repair_fetch_article_ids(schema, context.fetchable_article_ids)
    return schema


def _constrain_active_deferred_frontier_ids(
    schema: dict[str, Any],
    context: SolverContext,
) -> None:
    """Prevent stale ledger generations from satisfying a current boundary."""

    active_ids = tuple(
        item.frontier_item_id
        for item in context.graph_review_ledger
        if item.review_status == "relevant_deferred"
        and item.content_status in {"not_requested", "failed", "timeout"}
        and item.deferred_resolution_action != "no_longer_needed"
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


def _constrain_repair_fetch_article_ids(
    schema: dict[str, Any],
    fetchable_article_ids: tuple[str, ...],
) -> None:
    """未知ID修復時だけ、本文取得IDを現在の既知候補へ制約する。"""

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
    """Keep Anthropic grammar small; validate the decoded common contract."""

    expected_fields = tuple(
        _solver_common_transport_schema(context)["properties"]
    )
    return _strict_object(
        {
            "decision_json": {
                "type": "string",
                "description": (
                    "JSON object string for the current SolverDecision. "
                    "Use only these current-step fields: "
                    + ", ".join(expected_fields)
                    + "."
                ),
            }
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
    capacity = min(
        4,
        context.remaining_fetch_capacity,
        len(context.fetchable_article_ids),
    )
    if (
        context.finalize_only
        or context.cycle_close_required
        or graph_review_mode
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
    aliases = tuple(_article_fetch_alias_map(context))
    for index in range(1, capacity + 1):
        article_schema = _enum_string(aliases)
        article_properties[f"article_ref_{index}"] = (
            {
                **article_schema,
                "description": (
                    "fetchable_article_idsに対応する既知Article別名。"
                ),
            }
            if index == 1
            else {
                "anyOf": [article_schema, {"type": "null"}],
                "description": (
                    "追加取得する既知Article別名。使わない場合はnull。"
                ),
            }
        )
    return {
        "anyOf": [
            _strict_object(article_properties),
            {"type": "null"},
        ]
    }


def _article_fetch_alias_map(context: SolverContext) -> dict[str, str]:
    return {
        f"a{index}": article_id
        for index, article_id in enumerate(context.fetchable_article_ids, start=1)
    }


def _tool_requests_transport_schema(context: SolverContext) -> dict[str, Any]:
    projected_open_work_item_ids = _repair_open_work_item_ids(context)
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
        argument_schema = deepcopy(definition.input_schema)
        if definition.name == "fetch_articles":
            article_ids = argument_schema.get("properties", {}).get("article_ids")
            if isinstance(article_ids, dict):
                article_ids["items"] = _enum_string(context.fetchable_article_ids)
                article_ids["maxItems"] = min(
                    4,
                    context.remaining_fetch_capacity,
                    len(context.fetchable_article_ids),
                )
        elif definition.name == "load_evidence":
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
        previous_tool_names = {
            request.tool_name
            for request in context.contract_feedback.previous_decision.tool_requests
        }
        requires_single_request = any(
            message in context.contract_feedback.violation
            for message in (
                "Article body fetches in one SolverDecision must be consolidated",
                "identical legal_graph_neighbors arguments must be consolidated",
            )
        )
        # A repair may reveal a second violation after consolidating fetches. Keep
        # the already-correct single-request shape while that violation is fixed.
        if previous_tool_names == {"fetch_articles"}:
            requires_single_request = True
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
            "gaps": _described(string_array, HypothesisUpdate, "gaps"),
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
    has_fetch_sidecar = (
        "fetch_articles" in normalized or "article_fetch" in normalized
    )
    fetch_articles = normalized.pop(
        "fetch_articles",
        normalized.pop("article_fetch", None),
    )
    if has_fetch_sidecar:
        for request in normalized.get("tool_requests") or []:
            if (
                isinstance(request, dict)
                and request.get("tool_name") == "article_fetch"
            ):
                request["tool_name"] = "fetch_articles"
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
    if normalized.get("next") == "continue" and isinstance(fetch_articles, dict):
        if any(
            isinstance(request, dict)
            and request.get("tool_name") == "fetch_articles"
            for request in normalized.get("tool_requests", ())
        ):
            raise ModelProtocolError(
                "article body fetch is duplicated across generic and dedicated slots"
            )
        article_ids = [
            fetch_articles[key]
            for key in sorted(fetch_articles)
            if key.startswith(("article_id_", "article_ref_"))
            and isinstance(fetch_articles[key], str)
            and fetch_articles[key]
        ]
        normalized.setdefault("tool_requests", []).append(
            {
                "request_id": fetch_articles.get("request_id"),
                "work_item_id": fetch_articles.get("work_item_id"),
                "tool_name": "fetch_articles",
                "arguments": {"article_ids": article_ids},
                "purpose": fetch_articles.get("purpose"),
                "hypothesis_ids": fetch_articles.get("hypothesis_ids") or [],
            }
        )
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
        request = dict(raw_request)
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
    """本文取得の選択内容を変えず、同じDecision内の1要求へ束ねる。"""

    fetches = [
        request
        for request in requests
        if isinstance(request, dict)
        and request.get("tool_name") == "fetch_articles"
    ]
    if len(fetches) <= 1:
        return requests

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

    old_request_ids = {
        request.get("request_id")
        for request in fetches
        if isinstance(request.get("request_id"), str)
    }
    fetch_work_item_ids = {
        request.get("work_item_id")
        for request in fetches
        if isinstance(request.get("work_item_id"), str)
    }
    retained_request_id = retained.get("request_id")
    if isinstance(retained_request_id, str):
        for dependency in dependency_decisions:
            if (
                isinstance(dependency, dict)
                and dependency.get("status") == "needs_action"
                and (
                    dependency.get("action_request_id") in old_request_ids
                    or dependency.get("work_item_id") in fetch_work_item_ids
                )
            ):
                dependency["action_request_id"] = retained_request_id

    fetch_ids = {id(request) for request in fetches}
    consolidated: list[Any] = []
    inserted = False
    for request in requests:
        if id(request) not in fetch_ids:
            consolidated.append(request)
        elif not inserted:
            consolidated.append(retained)
            inserted = True
    return consolidated


def _consolidate_identical_graph_requests(
    requests: list[Any],
    dependency_decisions: list[Any],
) -> list[Any]:
    """同じGraph探索を1回にし、Hypothesisとの対応を保持する。"""

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for request in requests:
        if (
            not isinstance(request, dict)
            or request.get("tool_name") != "legal_graph_neighbors"
            or not isinstance(request.get("arguments"), dict)
        ):
            continue
        scope = (
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
            if item.judgment == "unresolved"
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
    _validate_hypothesis_update_evidence(decision)
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
    try:
        action = DependencyActionDecision.model_validate(decoded_payload)
    except ValidationError as exc:
        detail = exc.errors(include_url=False, include_input=False)[0]
        raise ModelProtocolError(
            f"dependency action contract invalid: {detail['msg']}"
        ) from exc

    required_ids = set(context.required_dependency_work_item_ids)
    requests_by_work_item: dict[str, list[ToolRequest]] = {}
    for request in action.tool_requests:
        requests_by_work_item.setdefault(request.work_item_id, []).append(request)
    if set(requests_by_work_item) != required_ids or any(
        len(requests) != 1 for requests in requests_by_work_item.values()
    ):
        raise ModelProtocolError(
            "dependency action requires exactly one ToolRequest per needs_action "
            "WorkItem"
        )

    active_dependencies = []
    for dependency in context.dependency_decisions:
        if dependency.work_item_id not in required_ids:
            continue
        request = requests_by_work_item[dependency.work_item_id][0]
        active_dependencies.append(
            dependency.model_copy(
                update={"action_request_id": request.request_id}
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
            "start_next_cycle": False,
            "update": {},
            "next_focus_work_item_ids": list(
                context.required_dependency_work_item_ids
            ),
            "dependency_decisions": active_dependencies,
            "tool_requests": [
                request.model_dump(mode="json")
                for request in action.tool_requests
            ],
            "answer": None,
        }
    )
    _assign_tool_request_ids(normalized, context)
    _normalize_absent_context_branches(normalized, context)
    decision = SolverDecision.model_validate(normalized)
    _validate_hypothesis_update_evidence(decision)
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
        SearchAssessmentDecision.model_validate(payload)
    except ValidationError as exc:
        detail = exc.errors(include_url=False, include_input=False)[0]
        raise ModelProtocolError(
            f"search assessment contract invalid: {detail['msg']}"
        ) from exc


def _validate_search_reselection_payload(
    payload: dict[str, Any],
    assessment_payload: dict[str, Any],
) -> None:
    """LLMが直接検証可能とした候補だけが選択されたことを検証する。"""

    matched_by_article = {
        item.get("article_id"): tuple(item.get("matched_hypothesis_ids") or ())
        for item in assessment_payload.get("assessments") or []
        if isinstance(item, dict)
    }
    for selection in payload.get("selections") or []:
        if not isinstance(selection, dict):
            continue
        article_id = selection.get("article_id")
        eligible_ids = set(matched_by_article.get(article_id) or ())
        selected_ids = selection.get("matched_hypothesis_ids") or []
        if not eligible_ids:
            raise ModelProtocolError(
                "selected search candidate must directly match a hypothesis: "
                f"{article_id}"
            )
        if not selected_ids or not set(selected_ids).issubset(eligible_ids):
            raise ModelProtocolError(
                "selected search candidate hypothesis IDs must be a non-empty "
                f"subset of its assessment: {article_id}"
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


def _normalize_absent_context_branches(
    normalized: dict[str, Any],
    context: SolverContext,
) -> None:
    """参照対象が存在しないGraph制御欄だけを機械的に空へ揃える。"""

    _normalize_grounding_evidence_aliases(normalized, context)

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

    article_aliases = _article_fetch_alias_map(context)
    fetched_article_ids = {
        article_id
        for evidence in context.material_evidence
        if evidence.evidence_id in context.grounding_evidence_ids
        if isinstance(
            article_id := evidence.metadata.get("articleId"),
            str,
        )
        and article_id
    }
    fetched_article_ids.update(context.fetched_resource_ids_this_cycle)
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
            continue
        if request.get("tool_name") != "fetch_articles":
            continue
        article_ids = arguments.get("article_ids")
        if isinstance(article_ids, list):
            resolved_article_ids = [
                article_aliases.get(article_id, article_id)
                for article_id in article_ids
            ]
            remaining_article_ids = [
                article_id
                for article_id in resolved_article_ids
                if article_id not in fetched_article_ids
            ]
            # Removing an already fetched Article is deterministic deduplication,
            # not legal relevance selection. Keep an all-redundant request intact
            # so normal contract repair can ask the Solver for another action.
            arguments["article_ids"] = (
                remaining_article_ids
                if remaining_article_ids
                else resolved_article_ids
            )

    fetch_requests = [
        request
        for request in normalized.get("tool_requests") or []
        if isinstance(request, dict)
        and request.get("tool_name") == "fetch_articles"
    ]
    current_limit = min(4, context.remaining_fetch_capacity)
    if fetch_requests and current_limit > 0:
        primary_request = fetch_requests[0]
        requested_article_ids = list(
            dict.fromkeys(
                article_id
                for request in fetch_requests
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
                for request in fetch_requests
                for hypothesis_id in request.get("hypothesis_ids", ())
                if isinstance(hypothesis_id, str)
            )
        )
        primary_request_id = primary_request.get("request_id")
        merged_request_ids = {
            request.get("request_id")
            for request in fetch_requests[1:]
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
            if request is primary_request
            or not (
                isinstance(request, dict)
                and request.get("tool_name") == "fetch_articles"
            )
        ]

    if not context.cycle_close_required:
        requested_article_ids: set[str] = set()
        for request in normalized.get("tool_requests") or []:
            if (
                not isinstance(request, dict)
                or request.get("tool_name") != "fetch_articles"
            ):
                continue
            arguments = request.get("arguments")
            article_ids = (
                arguments.get("article_ids")
                if isinstance(arguments, dict)
                else None
            )
            if isinstance(article_ids, list):
                requested_article_ids.update(
                    article_id
                    for article_id in article_ids
                    if isinstance(article_id, str)
                )
        if len(requested_article_ids) > current_limit:
            raise ModelProtocolError(
                "all fetch_articles requests combined must contain at most "
                f"{current_limit} unique Article IDs; the LLM must choose the "
                "current verification set"
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
    active_deferred = tuple(
        item
        for item in context.graph_review_ledger
        if item.review_status == "relevant_deferred"
        and item.content_status in {"not_requested", "failed", "timeout"}
        and item.deferred_resolution_action != "no_longer_needed"
    )
    if not active_deferred:
        normalized["deferred_frontier_resolutions"] = []
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
- update_hypotheses要素: hypothesis_id、judgment、evidence_ids、gaps。
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
- 今回の処理では`decision_reason`と`tool_requests`だけを返す。
- ToolRequestは、対応するopen WorkItemと未確認Hypothesisへ結び付ける。
- 各`needs_action` WorkItemについて、未確認事項を直接進めるToolRequestを1件返す。
- request_idは同じ出力内で重複しない短い局所IDにする。Programが永続化用IDへ置き換え、既存のDependencyDecisionへ対応付ける。
- Tool名とargumentsは`available_tools`に従う。
""".strip()
