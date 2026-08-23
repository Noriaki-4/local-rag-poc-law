"""既存provider共通JSON transportを新FrameworkのModel Portへ接続する。"""

from __future__ import annotations

import json
from copy import deepcopy
from time import monotonic
from typing import Any
from uuid import uuid4

import requests
from pydantic import BaseModel, ValidationError

from app.agent_framework.context import ContextCapacityExceeded, SolverContext
from app.agent_framework.contract_rendering import (
    contract_field_description,
    render_solver_contract_glossary,
)
from app.agent_framework.contracts import (
    CaseUpdate,
    HypothesisUpdate,
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
        if (
            context.required_search_review_request_ids
            and not context.graph_review_batch.candidates
            and not context.finalize_only
        ):
            return self._solve_search_review(context, profile)
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

    def _solve_search_review(
        self,
        context: SolverContext,
        profile: ModelCallProfile,
    ) -> SolverCallResult:
        """候補理解と比較選択を別コンテキストで順に実行する。"""

        assessment_call = render_search_assessment_model_call(context, profile)
        assessment_prompt = assessment_call.request
        assessment_schema = assessment_call.output_schema
        if self._diagnostics is not None:
            self._diagnostics.record_transport_input(
                context=context,
                profile=profile,
                rendered=assessment_call,
                repair_index=0,
                transport_stage="search_assessment",
                provider=getattr(self._client, "provider", None),
            )
        started_at = monotonic()
        try:
            assessment_result = self._client.generate_structured_json(
                prompt=assessment_prompt,
                schema=assessment_schema,
                model=profile.model,
                max_tokens=profile.max_output_tokens,
                timeout_sec=max(1, round(profile.timeout_sec)),
            )
        except requests.Timeout as exc:
            if self._diagnostics is not None:
                self._diagnostics.record_transport_timeout(
                    context=context,
                    repair_index=0,
                    reason="search assessment timed out",
                    transport_stage="search_assessment",
                )
            raise TimeoutError("search assessment timed out") from exc
        assessment_error = assessment_result.validationError
        if assessment_result.payload is None and assessment_error is None:
            assessment_error = "empty"
        if self._diagnostics is not None:
            self._diagnostics.record_transport_output(
                context=context,
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
        _validate_search_assessment_payload(
            assessment_result.payload,
            context,
        )

        selection_call = render_search_reselection_model_call(
            context,
            assessment_result.payload,
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
        remaining_timeout = profile.timeout_sec - (monotonic() - started_at)
        if remaining_timeout <= 1:
            raise TimeoutError("search reselection time exhausted")
        try:
            selection_result = self._client.generate_structured_json(
                prompt=selection_prompt,
                schema=selection_schema,
                model=profile.model,
                max_tokens=profile.max_output_tokens,
                timeout_sec=max(1, round(remaining_timeout)),
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
        try:
            SearchReselectionDecision.model_validate(selection_result.payload)
        except ValidationError as exc:
            detail = exc.errors(include_url=False, include_input=False)[0]
            raise ModelProtocolError(
                f"search reselection contract invalid: {detail['msg']}"
            ) from exc

        combined_payload = {
            **assessment_result.payload,
            **selection_result.payload,
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

        input_tokens = (
            assessment_result.inputTokens + selection_result.inputTokens
            if assessment_result.inputTokens is not None
            and selection_result.inputTokens is not None
            else None
        )
        output_tokens = (
            assessment_result.outputTokens + selection_result.outputTokens
            if assessment_result.outputTokens is not None
            and selection_result.outputTokens is not None
            else None
        )
        return SolverCallResult(
            decision=decision,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            attempt_count=(
                2
                + assessment_result.retryCount
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

    initial_research = profile.context_projection == "initial_research"
    projected_context = _project_available_tools(
        context,
        profile.available_tool_names,
    )
    compact_transport = initial_research or provider in {"ollama", "openai"}
    structured_tool_transport = not initial_research and provider == "anthropic"
    output_schema = (
        _initial_research_transport_schema(projected_context)
        if initial_research
        else (
            _solver_compact_transport_schema(context)
            if compact_transport
            else (
                _solver_anthropic_transport_schema(context)
                if structured_tool_transport
                else _solver_transport_schema(context)
            )
        )
    )
    return _render_solver_model_call(
        context,
        profile.system_prompt,
        completion_check_prompt=profile.completion_check_prompt,
        compact_transport=compact_transport,
        structured_tool_transport=structured_tool_transport,
        output_schema=output_schema,
        input_payload=_solver_context_payload(
            projected_context,
            projection=profile.context_projection,
        ),
        minimal_contract=(
            _INITIAL_RESEARCH_SOLVER_CONTRACT
            if initial_research
            else _MINIMAL_SOLVER_CONTRACT
        ),
        stage=stage,
    )


def _render_solver_model_call(
    context: SolverContext,
    system_prompt: str,
    *,
    completion_check_prompt: str | None = None,
    compact_transport: bool = False,
    structured_tool_transport: bool = False,
    output_schema: dict[str, Any] | None = None,
    input_payload: dict[str, Any] | None = None,
    minimal_contract: str = "",
    stage: str = "solver",
) -> RenderedModelCall:
    if output_schema is None:
        output_schema = (
            _solver_compact_transport_schema(context)
            if compact_transport
            else (
                _solver_anthropic_transport_schema(context)
                if structured_tool_transport
                else _solver_transport_schema(context)
            )
        )
    transport_instruction = _solver_transport_instruction(
        compact_transport=compact_transport,
        structured_tool_transport=structured_tool_transport,
    )
    repair_instructions = _contract_repair_catalog(context)
    if input_payload is None:
        input_payload = context.model_dump(mode="json")
    else:
        input_payload = deepcopy(input_payload)
    if structured_tool_transport:
        input_payload["transport_values"] = {
            "fetch_articles_aliases": _article_fetch_alias_map(context),
        }
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
        f"{transport_instruction}"
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
        normalized_schema=SolverDecision.model_json_schema(),
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


def _solver_context_payload(
    context: SolverContext,
    *,
    projection: str,
) -> dict[str, Any]:
    """CaseStoreを変えず、用途に無関係な実行値をModel入力から除く。"""

    payload = context.model_dump(mode="json")
    if projection == "full":
        return payload
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
    )
    return {name: payload[name] for name in included_fields}


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
        compact_transport=compact_transport,
        structured_tool_transport=structured_tool_transport,
    ).request


def _solver_transport_instruction(
    *,
    compact_transport: bool,
    structured_tool_transport: bool,
) -> str:
    return (
        "以下は現在のSolverContextです。コンパクト輸送schemaに従い、"
        "復元後SolverDecisionのうちupdateを構造化object、tool_requestsを"
        "構造化配列として直接返してください。各ToolRequestのargumentsは、"
        "available_toolsにある該当Toolのinput_schemaへ一致するJSON objectとして返します。"
        "update_json、tool_requests_json、arguments_jsonは返しません。"
        "AdapterがSolverDecisionとして上記契約で完全検証します。\n"
        if compact_transport
        else (
            "以下は現在のSolverContextです。Anthropic軽量輸送schemaに従い、"
            "update全体はupdate_jsonへJSON object文字列として格納します。"
            "add_hypothesesとupdate_hypothesesのevidence_idsはupdate_json内では空配列にし、"
            "実際に選ぶ既知Evidence IDはhypothesis_evidence_bindingsへ返してください。"
            "hypothesis_evidence_bindingsには今回のupdate_jsonのadd_hypothesesまたは"
            "update_hypothesesに含めたhypothesis_idだけを返し、変更しない既存Hypothesisは返しません。"
            "grounding_evidence_idsが空ならhypothesis_evidence_bindingsはnullにし、update_jsonで"
            "追加・更新するHypothesisはjudgment=unresolved、evidence_ids=[]のままにします。"
            "検索候補だけでsupported/contradictedにせず、必要なArticle本文を取得します。"
            "dependency_decisionsはWorkItemごとの固定JSON文字列slotです。各slotへ"
            "dependency_kind、work_item_id、status、reason、basis_evidence_ids、action_request_idを持つ"
            "1個のobjectをJSON文字列化して返します。指定されたwork_item_idは変更せず、"
            "basis_evidence_idsは空配列にします。実際に使う既知Evidence IDは"
            "直接返さず、dependency_article_bindingsへ判断に使った取得済みArticle IDを"
            "WorkItemごとに1件返します。Adapterが選ばれたArticleの取得済みEvidence IDを"
            "basis_evidence_idsへ機械転記します。"
            "legal_search、legal_graph_neighbors、load_evidenceはtool_requestsへ"
            "固定slotとして返してください。各tool_request_N_jsonにはtool_nameとrequest_jsonを持つ"
            "objectを返し、request_jsonにはrequest_id、work_item_id、arguments、purpose、"
            "hypothesis_idsを持つToolRequestをJSON文字列化して格納します。外側のtool_nameが正本で、"
            "使わないslotはnullにします。新規request_idは"
            "160文字以内の短いASCII識別子にし、説明文はpurposeへ入れます。"
            "fetch_articlesだけはtool_requestsへ入れず、"
            "専用fetch_articles欄へ1件だけ返し、article_ref_1から順に上記の既知候補別名を指定してください。"
            "既知候補別名とArticle IDの対応はSolverContext.transport_values.fetch_articles_aliasesにあります。"
            "専用fetch_articles欄は正規のfetch_articles ToolRequestの輸送表現であり、追加情報ではありません。"
            "専用fetch_articles欄を返す場合も返さない場合も、tool_requestsの各slotへ"
            "tool_name=fetch_articlesを決して再掲しません。"
            "不要な残りslotはnullにします。"
            "各ToolRequest内のargumentsはJSON objectのまま格納し、arguments_jsonと"
            "tool_requests_jsonは返しません。"
            "Adapterがupdate_json、Evidence対応、各ToolRequest文字列、専用fetch_articles欄を復元し、SolverDecisionとして"
            "上記契約で完全検証します。\n"
            if structured_tool_transport
            else (
                "以下は現在のSolverContextです。Provider輸送schemaに従い、"
                "next、next_focus_work_item_ids、retain_evidence_ids、answerは直接返し、"
                "update全体をupdate_json、tool_requests全体をtool_requests_jsonへ"
                "JSON文字列化し、dependency_decisionsはschemaどおりの配列として直接"
                "返し、graph_candidate_review、search_candidate_review、frontier_re_adoptions、"
                "deferred_frontier_resolutions、unreviewed_graph_resolutionもschemaどおり直接返してください。"
                "Adapterが2つのJSON文字列を復元し、"
                "SolverDecisionとして上記契約で完全検証します。\n"
            )
        )
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


def render_search_assessment_model_call(
    context: SolverContext,
    profile: ModelCallProfile,
) -> RenderedModelCall:
    input_payload = _search_review_context_payload(context)
    input_payload["candidate_checklist"] = _search_candidate_checklist(context)
    instructions = (
        f"{profile.system_prompt}\n\n"
        f"{RUNTIME_INPUT_MARKER}"
        f"{_post_context_completion_check(profile.completion_check_prompt)}"
    )
    rendered = build_rendered_model_call(
        stage="search_assessment",
        instructions=instructions,
        input_tag="solver_context",
        input_payload=input_payload,
        output_schema=_search_review_transport_schema(context),
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


def _search_candidate_checklist(context: SolverContext) -> dict[str, Any]:
    """Search Assessmentが照合する候補IDを入力順に再掲する。"""

    return {
        "candidate_count": len(context.search_candidates),
        "article_ids_in_input_order": [
            item.article_id for item in context.search_candidates
        ],
    }


def _search_review_context_payload(
    context: SolverContext,
) -> dict[str, Any]:
    """検索抜粋を候補ごとに機械結合したSearch Review専用View。"""

    evidence_by_id = {
        item.evidence_id: item for item in context.material_evidence
    }
    return {
        "question": context.question,
        "work_tree": [item.model_dump(mode="json") for item in context.work_tree],
        "hypotheses": [
            item.model_dump(mode="json") for item in context.hypotheses
        ],
        "remaining_fetch_capacity": context.remaining_fetch_capacity,
        "required_search_review_request_ids": list(
            context.required_search_review_request_ids
        ),
        "candidate_count": len(context.search_candidates),
        "search_candidates": [
            {
                "article_id": candidate.article_id,
                "document_id": candidate.document_id,
                "title": candidate.title,
                "headings": list(candidate.headings),
                "discovery_work_item_ids": list(
                    candidate.discovery_work_item_ids
                ),
                "discovery_hypothesis_ids": list(
                    candidate.discovery_hypothesis_ids
                ),
                "search_request_ids": list(candidate.search_request_ids),
                "search_excerpts": [
                    {
                        "evidence_id": evidence_id,
                        "content": evidence_by_id[evidence_id].content,
                    }
                    for evidence_id in candidate.navigation_evidence_ids
                    if evidence_id in evidence_by_id
                ],
            }
            for candidate in context.search_candidates
        ],
    }


def render_search_reselection_model_call(
    context: SolverContext,
    assessment_payload: dict[str, Any],
    profile: ModelCallProfile,
) -> RenderedModelCall:
    if profile.followup_system_prompt is None:
        raise ModelProtocolError("search reselection prompt is unavailable")
    input_payload = {
        "question": context.question,
        "hypotheses": [
            item.model_dump(mode="json") for item in context.hypotheses
        ],
        "remaining_fetch_capacity": context.remaining_fetch_capacity,
        "assessments": assessment_payload.get("assessments") or [],
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
        output_schema=_search_reselection_transport_schema(context),
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
    "repeated_successful_search",
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
    rendered = build_rendered_model_call(
        stage="solver_transport_repair",
        instructions=instructions,
        input_tag=base_call.input_tag,
        input_payload=input_payload,
        output_schema=base_call.output_schema,
        normalized_schema=base_call.normalized_schema,
        prompt_assets=prompt_assets,
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
    answer = _strict_object(
        {
            "text": _described({"type": "string"}, FinalAnswer, "text"),
            "citation_ids": _described(
                string_array,
                FinalAnswer,
                "citation_ids",
            ),
            "limitations": _described(
                string_array,
                FinalAnswer,
                "limitations",
            ),
            "unresolved_work_item_ids": _described(
                string_array,
                FinalAnswer,
                "unresolved_work_item_ids",
            ),
            "unresolved_hypothesis_ids": _described(
                string_array,
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


def _search_review_transport_schema(context: SolverContext) -> dict[str, Any]:
    """Search Reviewが意味選択だけへ集中する専用輸送schema。"""

    candidate_ids = tuple(item.article_id for item in context.search_candidates)
    return _strict_object(
        {
            "search_request_ids": _described(
                {
                    "type": "array",
                    "items": _enum_string(
                        context.required_search_review_request_ids
                    ),
                    "minItems": len(
                        context.required_search_review_request_ids
                    ),
                    "maxItems": len(
                        context.required_search_review_request_ids
                    ),
                },
                SearchAssessmentDecision,
                "search_request_ids",
            ),
            "assessments": _described(
                {
                    "type": "array",
                    "items": _strict_object(
                        {
                            "article_id": _described(
                                _enum_string(candidate_ids),
                                SearchCandidateAssessment,
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
                                {"type": "string", "minLength": 1},
                                SearchCandidateAssessment,
                                "summary",
                            ),
                        }
                    ),
                    "minItems": len(candidate_ids),
                    "maxItems": len(candidate_ids),
                },
                SearchAssessmentDecision,
                "assessments",
            ),
            "reason": _described(
                {"type": "string", "minLength": 1},
                SearchAssessmentDecision,
                "reason",
            ),
        }
    )


def _search_reselection_transport_schema(
    context: SolverContext,
) -> dict[str, Any]:
    candidate_ids = tuple(item.article_id for item in context.search_candidates)
    return _strict_object(
        {
            "selections": _described(
                {
                    "type": "array",
                    "items": _strict_object(
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
                        }
                    ),
                    "maxItems": min(
                        len(candidate_ids),
                        context.remaining_fetch_capacity,
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
    schema["required"] = [
        "update" if item == "update_json" else (
            "tool_requests" if item == "tool_requests_json" else item
        )
        for item in schema["required"]
    ]
    return schema


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
    return {
        "type": "array",
        "items": item_schema,
        "maxItems": context.max_tool_requests_per_step,
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
    normalized["tool_requests"] = requests
    return normalized


def _assign_tool_request_ids(
    normalized: dict[str, Any],
    context: SolverContext,
) -> None:
    """LLMの局所参照を保ち、永続化用ToolRequest IDを機械採番する。"""

    requests = normalized.get("tool_requests") or []
    if not requests:
        return
    local_ids = [
        request.get("request_id") if isinstance(request, dict) else None
        for request in requests
    ]
    if any(not isinstance(request_id, str) or not request_id for request_id in local_ids):
        return
    if len(local_ids) != len(set(local_ids)):
        raise ModelProtocolError(
            "tool request local IDs must be unique within the decision"
        )

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
        if action_request_id is None and len(matching_ids) == 1:
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
    review = {
        "search_request_ids": payload.get("search_request_ids") or [],
        "selections": payload.get("selections") or [],
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
    try:
        SearchAssessmentDecision.model_validate(payload)
    except ValidationError as exc:
        detail = exc.errors(include_url=False, include_input=False)[0]
        raise ModelProtocolError(
            f"search assessment contract invalid: {detail['msg']}"
        ) from exc


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
    for request in normalized.get("tool_requests") or []:
        if not isinstance(request, dict) or request.get("tool_name") != "fetch_articles":
            continue
        arguments = request.get("arguments")
        if not isinstance(arguments, dict):
            continue
        article_ids = arguments.get("article_ids")
        if isinstance(article_ids, list):
            arguments["article_ids"] = [
                article_aliases.get(article_id, article_id)
                for article_id in article_ids
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
        current_limit = min(4, context.remaining_fetch_capacity)
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
