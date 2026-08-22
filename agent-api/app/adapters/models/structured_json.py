"""既存provider共通JSON transportを新FrameworkのModel Portへ接続する。"""

from __future__ import annotations

import json
from time import monotonic
from typing import Any

import requests
from pydantic import ValidationError

from app.agent_framework.context import ContextCapacityExceeded, SolverContext
from app.agent_framework.contracts import SolverDecision
from app.agent_framework.diagnostics import AgentDiagnostics
from app.agent_framework.ports.model import (
    ModelProtocolError,
    ReviewCallResult,
    ReviewContext,
    SolverCallResult,
)
from app.agent_framework.profiles import ModelCallProfile, ReviewerProfile
from app.agent_framework.prompt_assets import (
    PromptAssetTrace,
    prompt_asset_trace,
    render_prompt_section,
)
from app.agent_framework.state import ReviewResult
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
        # OpenAI Structured OutputsでもToolRequestをobjectとして拘束する。
        # JSON文字列へ落とすと内側の必須fieldをprovider schemaで検証できない。
        compact_transport = provider in {"ollama", "openai"}
        structured_tool_transport = provider == "anthropic"
        base_prompt = _solver_prompt(
            context,
            profile.system_prompt,
            compact_transport=compact_transport,
            structured_tool_transport=structured_tool_transport,
        )
        transport_schema = (
            _solver_compact_transport_schema(context)
            if compact_transport
            else (
                _solver_anthropic_transport_schema(context)
                if structured_tool_transport
                else _solver_transport_schema(context)
            )
        )
        prompt = base_prompt
        prompt_assets = _solver_prompt_assets(context)
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
                    prompt=prompt,
                    schema=transport_schema,
                    repair_index=repair_index,
                    prompt_assets=prompt_assets,
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
                )

            if repair_index == 0:
                transport_repair_sections = _transport_repair_section_names(
                    last_error
                )
                prompt = _solver_repair_prompt(
                    base_prompt,
                    result.payload,
                    last_error,
                )
                prompt_assets = (
                    *_solver_prompt_assets(context),
                    prompt_asset_trace(
                        "solver_transport_repair.md",
                        ("base", *transport_repair_sections),
                    ),
                )
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

    def review(
        self,
        context: ReviewContext,
        profile: ReviewerProfile,
    ) -> ReviewCallResult:
        prompt = _review_prompt(context, profile.system_prompt)
        try:
            result = self._client.generate_structured_json(
                prompt=prompt,
                schema=ReviewResult.model_json_schema(),
                model=profile.model,
                max_tokens=profile.max_output_tokens,
                timeout_sec=max(1, round(profile.timeout_sec)),
            )
        except requests.Timeout as exc:
            raise TimeoutError("model provider request timed out") from exc
        if result.validationError or result.payload is None:
            raise ModelProtocolError(
                f"review structured output invalid: {result.validationError or 'empty'}"
            )
        try:
            review = ReviewResult.model_validate(result.payload)
        except ValidationError as exc:
            raise ModelProtocolError("review result violates schema") from exc
        return ReviewCallResult(
            review=review,
            input_tokens=result.inputTokens,
            output_tokens=result.outputTokens,
            attempt_count=1 + result.retryCount,
        )


def _solver_prompt(
    context: SolverContext,
    system_prompt: str,
    *,
    compact_transport: bool = False,
    structured_tool_transport: bool = False,
) -> str:
    payload = json.dumps(
        context.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    contract_repair_instruction = _focused_contract_repair_instruction(context)
    article_fetch_aliases = _article_fetch_alias_map(context)
    article_fetch_alias_instruction = (
        "article_fetchのarticle_ref_NにはArticle ID本体でなく、次の既知候補別名を指定します。"
        "Adapterが対応するArticle IDへ機械変換します。"
        f"<article_fetch_aliases>{json.dumps(article_fetch_aliases, ensure_ascii=False, separators=(',', ':'))}"
        "</article_fetch_aliases>"
        if article_fetch_aliases
        else ""
    )
    transport_instruction = (
        "以下は現在のSolverContextです。コンパクト輸送schemaに従い、"
        "復元後SolverDecisionのうちupdateを構造化object、tool_requestsを"
        "構造化配列として直接返してください。各ToolRequestのargumentsだけは"
        "arguments_jsonへ1個のJSON object文字列として格納します。"
        "legal_searchのarguments_jsonは例として"
        "{\"query\":\"公開買付け 公告 届出\",\"doc_types\":[\"law\"],\"document_ids\":[]}"
        "の形にし、scope、tool_input、mode、predicate等を追加しません。"
        "update_jsonとtool_requests_jsonは返しません。Adapterがarguments_jsonを"
        "復元し、SolverDecisionとして上記契約で完全検証します。\n"
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
            "article_fetchへ1件だけ返し、article_ref_1から順に上記の既知候補別名を指定してください。"
            "article_fetchはfetch_articles ToolRequestそのものの輸送表現であり、追加情報ではありません。"
            "article_fetchを返す場合も返さない場合も、tool_requestsの各slotへ"
            "tool_name=fetch_articlesを決して再掲しません。"
            "不要な残りslotはnullにします。"
            "各ToolRequest内のargumentsはJSON objectのまま格納し、arguments_jsonと"
            "tool_requests_jsonは返しません。"
            "Adapterがupdate_json、Evidence対応、各ToolRequest文字列、article_fetchを復元し、SolverDecisionとして"
            f"上記契約で完全検証します。{article_fetch_alias_instruction}\n"
            if structured_tool_transport
            else (
                "以下は現在のSolverContextです。Provider輸送schemaに従い、"
                "next、next_focus_work_item_ids、retain_evidence_ids、answerは直接返し、"
                "update全体をupdate_json、tool_requests全体をtool_requests_jsonへ"
                "JSON文字列化し、dependency_decisionsはschemaどおりの配列として直接"
                "返し、graph_candidate_review、frontier_re_adoptions、"
                "deferred_frontier_resolutions、unreviewed_graph_resolutionもschemaどおり直接返してください。"
                "Adapterが2つのJSON文字列を復元し、"
                "SolverDecisionとして上記契約で完全検証します。\n"
            )
        )
    )
    contract_feedback_rule = render_prompt_section(
        "solver_contract_repair.md",
        "contract_feedback_rule",
    )
    prompt = (
        f"{system_prompt}\n\n"
        f"{_MINIMAL_SOLVER_CONTRACT}\n\n"
        f"{contract_feedback_rule}\n"
        f"{contract_repair_instruction}"
        f"{transport_instruction}"
        f"<solver_context>{payload}</solver_context>"
    )
    _ensure_solver_prompt_capacity(prompt, context.max_solver_input_chars)
    return prompt


def _ensure_solver_prompt_capacity(prompt: str, max_input_chars: int) -> None:
    if len(prompt) > max_input_chars:
        raise ContextCapacityExceeded(
            "context_capacity_exceeded: solver prompt exceeds "
            "max_solver_input_chars"
        )


def _review_prompt(context: ReviewContext, system_prompt: str) -> str:
    payload = json.dumps(
        context.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        f"{system_prompt}\n\n"
        "以下の質問・回答・実際に引用された根拠だけを確認し、"
        "ReviewResultだけを返してください。\n"
        f"<review_context>{payload}</review_context>"
    )


_TRANSPORT_REPAIR_RULES = (
    (
        "continue decision requires a tool request",
        "continue_requires_action",
    ),
    (
        "all fetch_articles requests combined must contain at most",
        "article_fetch_limit",
    ),
    (
        "supported or contradicted hypothesis requires evidence",
        "hypothesis_requires_evidence",
    ),
)

_CONTRACT_REPAIR_RULES = (
    (("unknown evidence", "hypothesis has unknown evidence"), "unknown_evidence"),
    (
        ("supported or contradicted hypothesis requires evidence",),
        "hypothesis_requires_evidence",
    ),
    (("navigation-only evidence",), "navigation_only_evidence"),
    (("unknown Article ID",), "unknown_article_id"),
    (("open WorkItem", "unresolved answer scope"), "open_work_item"),
    (("Cycle boundary", "remaining Cycle capacity"), "cycle_boundary"),
    (("resolved dependency requires",), "resolved_dependency"),
    (
        (
            "dependency decision",
            "dependency action",
            "decisions do not match required work items",
        ),
        "dependency_decision",
    ),
    (("retained evidence count exceeds",), "retained_evidence_limit"),
    (
        ("tool request count", "fetch_articles.article_ids exceeds"),
        "tool_request_limit",
    ),
    (("Article body fetch", "fetch_articles"), "article_fetch_contract"),
    (
        ("focus", "ToolRequest", "tool request references"),
        "known_references",
    ),
    (("Graph", "graph review", "Frontier"), "graph_review"),
    (("citations omit",), "citation_coverage"),
)


def _solver_repair_prompt(
    base_prompt: str,
    payload: dict | None,
    error: ModelProtocolError | ValidationError,
) -> str:
    previous = ""
    if isinstance(payload, dict):
        previous = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    error_detail = _transport_error_detail(error)
    focused_instruction = "".join(
        render_prompt_section("solver_transport_repair.md", section_name)
        for section_name in _transport_repair_section_names(error_detail)
    )
    return render_prompt_section(
        "solver_transport_repair.md",
        "base",
        {
            "base_prompt": base_prompt,
            "focused_instruction": focused_instruction,
            "validation_error": error_detail,
            "previous_solver_decision": previous,
        },
    )


def _validate_hypothesis_update_evidence(decision: SolverDecision) -> None:
    """状態適用後に必ず失敗するHypothesis更新を輸送修復へ戻す。"""

    if any(
        item.judgment in {"supported", "contradicted"} and not item.evidence_ids
        for item in decision.update.update_hypotheses
    ):
        raise ModelProtocolError(
            "supported or contradicted hypothesis requires evidence"
        )


def _focused_contract_repair_instruction(context: SolverContext) -> str:
    feedback = context.contract_feedback
    if feedback is None:
        return ""
    violation = feedback.violation
    instructions: list[str] = []
    for markers, section_name in _CONTRACT_REPAIR_RULES:
        if any(marker in violation for marker in markers):
            instructions.append(
                render_prompt_section(
                    "solver_contract_repair.md",
                    section_name,
                )
            )
    return "\n" + render_prompt_section(
        "solver_contract_repair.md",
        "base",
        {
            "focused_instructions": "\n".join(instructions),
            "violation": violation,
        },
    ) + "\n"


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


def _transport_repair_section_names(
    error: ModelProtocolError | ValidationError | str,
) -> tuple[str, ...]:
    error_detail = (
        error if isinstance(error, str) else _transport_error_detail(error)
    )
    return tuple(
        section_name
        for marker, section_name in _TRANSPORT_REPAIR_RULES
        if marker in error_detail
    )[:1]


def _solver_prompt_assets(context: SolverContext) -> tuple[PromptAssetTrace, ...]:
    section_names = ["contract_feedback_rule"]
    feedback = context.contract_feedback
    if feedback is not None:
        section_names.append("base")
        section_names.extend(
            section_name
            for markers, section_name in _CONTRACT_REPAIR_RULES
            if any(marker in feedback.violation for marker in markers)
        )
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
            "text": {"type": "string"},
            "citation_ids": string_array,
            "limitations": string_array,
            "unresolved_work_item_ids": string_array,
            "unresolved_hypothesis_ids": string_array,
        }
    )
    required_dependency_kind = context.required_dependency_kind
    required_dependency_work_item_ids = context.required_dependency_work_item_ids
    dependency_decision = _strict_object(
        {
            "dependency_kind": (
                {"type": "string", "enum": [required_dependency_kind]}
                if required_dependency_kind is not None
                else {"type": "string"}
            ),
            "work_item_id": _enum_string(required_dependency_work_item_ids),
            "status": {
                "type": "string",
                "enum": ["not_required", "needs_action", "resolved"],
            },
            "reason": {"type": "string"},
            "basis_evidence_ids": {
                **_bounded_enum_array(context.grounding_evidence_ids),
                "description": (
                    "Grounding body Evidence used by the LLM for this audit decision"
                ),
            },
            "action_request_id": {
                "anyOf": [{"type": "string"}, {"type": "null"}]
            },
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
    batch_candidates = context.graph_review_batch.candidates
    graph_review_mode = bool(batch_candidates and not context.finalize_only)
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
            "frontier_item_id": _enum_string(selectable_frontier_ids),
            "article_id": _enum_string(graph_article_ids),
            "work_item_id": _enum_string(graph_work_item_ids),
            "hypothesis_id": {
                "anyOf": [_enum_string(graph_hypothesis_ids), {"type": "null"}]
            },
            "action": {
                "type": "string",
                "enum": ["select", "defer", "reject"],
            },
            "reason": {"type": "string"},
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
            "graph_request_ids": {
                "type": "array",
                "items": _enum_string(context.required_graph_review_request_ids),
                "minItems": len(context.required_graph_review_request_ids),
                "maxItems": len(context.required_graph_review_request_ids),
            },
            "reviewed_link_ids": {
                "type": "array",
                "items": _enum_string(reviewed_link_ids),
                "minItems": len(reviewed_link_ids),
                "maxItems": len(reviewed_link_ids),
            },
            "frontier_decisions": {
                "type": "array",
                "items": graph_frontier_decision,
                "minItems": len(batch_frontier_ids),
                "maxItems": len(selectable_frontier_ids),
            },
            "reason": {"type": "string"},
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
            "article_id": _enum_string(known_ledger_article_ids),
            "work_item_id": _enum_string(open_work_item_ids),
            "hypothesis_id": _enum_string(open_hypothesis_ids),
            "reason": {"type": "string"},
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
            "frontier_item_id": _enum_string(
                tuple(item.frontier_item_id for item in active_deferred)
            ),
            "article_id": _enum_string(
                tuple(dict.fromkeys(item.article_id for item in active_deferred))
            ),
            "work_item_id": _enum_string(
                tuple(dict.fromkeys(item.work_item_id for item in active_deferred))
            ),
            "hypothesis_id": {
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
            "action": {
                "type": "string",
                "enum": [
                    "fetch_next_cycle",
                    "carry_forward",
                    "no_longer_needed",
                    "unresolved_at_limit",
                ],
            },
            "reason": {"type": "string"},
        }
    )
    force_next_cycle_repair = _preserve_previous_update_for_cycle_repair(context)
    force_continue_repair = _force_continue_after_open_finalize_repair(context)
    tool_requests_forbidden = (
        graph_review_mode
        or context.finalize_only
        or context.cycle_close_required
        or force_next_cycle_repair
    )
    preserve_previous_update = _preserve_previous_update_for_contract_repair(
        context
    )
    repair_update_json: str | None = None
    if preserve_previous_update:
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
            "action": {
                "type": "string",
                "enum": unreviewed_graph_action_values,
            },
            "reason": {"type": "string"},
        }
    )
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "next": {
                "type": "string",
                "enum": (
                    ["continue"]
                    if graph_review_mode
                    or force_next_cycle_repair
                    or force_continue_repair
                    else ["continue", "finalize"]
                ),
            },
            "decision_reason": {
                "type": "string",
                "description": (
                    "One concise sentence explaining why this step chooses "
                    "continue or finalize from the shown evidence, gaps, and limits; "
                    "do not provide hidden chain-of-thought"
                ),
            },
            "start_next_cycle": (
                {"type": "boolean", "enum": [False]}
                if graph_review_mode or force_continue_repair
                else (
                    {"type": "boolean", "enum": [True]}
                    if force_next_cycle_repair
                    else {"type": "boolean"}
                )
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
            "next_focus_work_item_ids": (
                _bounded_enum_array(repair_open_work_item_ids)
                if context.contract_feedback is not None
                else string_array
            ),
            "retain_evidence_ids": _bounded_enum_array(
                tuple(item.evidence_id for item in context.evidence_manifest),
                max_items=context.max_retained_evidence,
            ),
            "tool_requests_json": {
                "type": "string",
                "description": "ToolRequest array encoded as one JSON array string",
                **({"enum": ["[]"]} if tool_requests_forbidden else {}),
            },
            "dependency_decisions": dependency_decisions,
            "graph_candidate_review": (
                graph_candidate_review
                if graph_review_mode
                else {"type": "null"}
            ),
            "frontier_re_adoptions": (
                _empty_array_schema()
                if graph_review_mode
                else {
                    "type": "array",
                    "items": frontier_re_adoption,
                    "maxItems": len(context.graph_review_ledger),
                }
            ),
            "deferred_frontier_resolutions": (
                _empty_array_schema()
                if graph_review_mode
                else {
                    "type": "array",
                    "items": deferred_frontier_resolution,
                    "maxItems": len(active_deferred),
                }
            ),
            "unreviewed_graph_resolution": (
                {"type": "null"}
                if graph_review_mode
                or context.graph_review_batch.remaining_unreviewed_count == 0
                else unreviewed_graph_resolution
            ),
            "answer": (
                {"type": "null"}
                if graph_review_mode
                or force_next_cycle_repair
                or force_continue_repair
                else {"anyOf": [answer, {"type": "null"}]}
            ),
        },
        "required": [
            "next",
            "decision_reason",
            "start_next_cycle",
            "update_json",
            "next_focus_work_item_ids",
            "retain_evidence_ids",
            "tool_requests_json",
            "dependency_decisions",
            "graph_candidate_review",
            "frontier_re_adoptions",
            "deferred_frontier_resolutions",
            "unreviewed_graph_resolution",
            "answer",
        ],
    }


def _solver_compact_transport_schema(context: SolverContext) -> dict:
    """provider共通の、長い二重JSONを避けた参照なし輸送schemaを返す。"""

    schema = _solver_transport_schema(context)
    properties = schema["properties"]
    if context.research_cycle_count == 0:
        properties["start_next_cycle"] = {
            "type": "boolean",
            "enum": [False],
        }
    tool_requests_forbidden = properties["tool_requests_json"].get("enum") == [
        "[]"
    ]
    properties.pop("update_json")
    properties.pop("tool_requests_json")
    properties["update"] = (
        _empty_case_update_transport_schema()
        if _preserve_previous_update_for_contract_repair(context)
        else _case_update_transport_schema()
    )
    if not context.work_tree and context.contract_feedback is None:
        properties["update"]["properties"]["add_work_items"]["minItems"] = 1
        properties["update"]["properties"]["add_hypotheses"]["minItems"] = 1
    projected_open_work_item_ids = _repair_open_work_item_ids(context)
    evidence_ids = tuple(item.evidence_id for item in context.evidence_manifest)
    properties["retain_evidence_ids"] = _bounded_enum_array(
        evidence_ids,
        max_items=context.max_retained_evidence,
    )
    if projected_open_work_item_ids:
        properties["next_focus_work_item_ids"] = _bounded_enum_array(
            projected_open_work_item_ids
        )
    properties["tool_requests"] = (
        _empty_array_schema()
        if tool_requests_forbidden
        else _tool_requests_transport_schema(context)
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
    properties["dependency_decisions"] = _anthropic_dependency_decisions_schema(
        context
    )
    grounding_article_ids = tuple(
        dict.fromkeys(
            article_id
            for evidence in context.material_evidence
            if (article_id := evidence.metadata.get("articleId"))
            and isinstance(article_id, str)
        )
    )
    properties["dependency_article_bindings"] = (
        {
            "type": "array",
            "items": _strict_object(
                {
                    "work_item_id": _enum_string(
                        context.required_dependency_work_item_ids
                    ),
                    "article_ids": _bounded_enum_array(
                        grounding_article_ids
                    ),
                }
            ),
        }
        if context.required_dependency_work_item_ids
        else {"type": "null"}
    )
    properties["hypothesis_evidence_bindings"] = (
        {
            "type": "array",
            "items": _strict_object(
                {
                    "hypothesis_id": {"type": "string"},
                    "evidence_ids": _bounded_enum_array(
                        context.grounding_evidence_ids
                    ),
                }
            ),
        }
        if context.grounding_evidence_ids
        else {"type": "null"}
    )
    tool_requests_forbidden = properties["tool_requests_json"].get("enum") == [
        "[]"
    ]
    properties.pop("tool_requests_json")
    article_fetch_schema = _anthropic_article_fetch_schema(context)
    non_fetch_capacity = (
        0
        if tool_requests_forbidden
        else context.max_tool_requests_per_step
        - (0 if article_fetch_schema == {"type": "null"} else 1)
    )
    properties["tool_requests"] = _strict_object(
        {
            f"tool_request_{index}_json": {
                "anyOf": [
                    _strict_object(
                        {
                            "tool_name": {
                                "type": "string",
                                "enum": [
                                    "legal_search",
                                    "legal_graph_neighbors",
                                    "load_evidence",
                                ],
                            },
                            "request_json": {
                                "type": "string",
                                "description": (
                                    "one ToolRequest JSON object without tool_name; "
                                    "the outer tool_name is authoritative"
                                ),
                            },
                        }
                    ),
                    {"type": "null"},
                ]
            }
            for index in range(1, non_fetch_capacity + 1)
        }
    )
    properties["article_fetch"] = article_fetch_schema
    properties["retain_evidence_ids"] = _bounded_enum_array(
        tuple(item.evidence_id for item in context.evidence_manifest),
        max_items=context.max_retained_evidence,
    )
    answer_schema = properties.get("answer")
    if isinstance(answer_schema, dict):
        variants = answer_schema.get("anyOf") or (answer_schema,)
        for variant in variants:
            if not isinstance(variant, dict) or variant.get("type") != "object":
                continue
            variant["properties"]["citation_ids"] = _bounded_enum_array(
                context.grounding_evidence_ids
            )
    schema["required"] = [
        "tool_requests" if item == "tool_requests_json" else item
        for item in schema["required"]
    ]
    schema["required"].append("hypothesis_evidence_bindings")
    schema["required"].append("dependency_article_bindings")
    schema["required"].append("article_fetch")
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


def _anthropic_article_fetch_schema(context: SolverContext) -> dict[str, Any]:
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
        "request_id": {"type": "string"},
        "work_item_id": _enum_string(_repair_open_work_item_ids(context)),
        "purpose": {"type": "string"},
        "hypothesis_ids": _bounded_enum_array(_repair_hypothesis_ids(context)),
    }
    aliases = tuple(_article_fetch_alias_map(context))
    for index in range(1, capacity + 1):
        article_schema = _enum_string(aliases)
        article_properties[f"article_ref_{index}"] = (
            article_schema
            if index == 1
            else {"anyOf": [article_schema, {"type": "null"}]}
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
    return {
        "type": "array",
        "items": _strict_object(
            {
                "request_id": {"type": "string"},
                "work_item_id": _enum_string(projected_open_work_item_ids),
                "tool_name": {"type": "string"},
                "arguments_json": {
                    "type": "string",
                    "description": (
                        "one exact Tool arguments object encoded as JSON; "
                        "legal_search allows only query, doc_types, document_ids; "
                        "fetch_articles allows only article_ids; "
                        "legal_graph_neighbors allows only article_ids, mode, "
                        "predicate when semantic, direction, max_relations"
                    ),
                },
                "purpose": {"type": "string"},
                "hypothesis_ids": (
                    _bounded_enum_array(projected_hypothesis_ids)
                    if projected_hypothesis_ids
                    else _string_array_schema()
                ),
            }
        ),
    }


def _case_update_transport_schema() -> dict[str, Any]:
    string_array = _string_array_schema()
    nullable_string = {
        "anyOf": [{"type": "string"}, {"type": "null"}],
    }
    work_item = _strict_object(
        {
            "work_item_id": {"type": "string"},
            "parent_work_item_id": nullable_string,
            "question": {"type": "string"},
            "state": {
                "type": "string",
                "enum": ["open"],
            },
            "resolution": {"type": "null"},
            "basis_hypothesis_ids": string_array,
            "replaces_work_item_id": nullable_string,
        }
    )
    work_item_update = _strict_object(
        {
            "work_item_id": {"type": "string"},
            "state": {
                "type": "string",
                "enum": ["open", "resolved", "dropped"],
            },
            "resolution": nullable_string,
            "basis_hypothesis_ids": string_array,
        }
    )
    hypothesis = _strict_object(
        {
            "hypothesis_id": {"type": "string"},
            "work_item_id": {"type": "string"},
            "statement": {"type": "string"},
            "judgment": {
                "type": "string",
                "enum": ["supported", "contradicted", "unresolved"],
            },
            "evidence_ids": string_array,
            "gaps": string_array,
        }
    )
    hypothesis_update = _strict_object(
        {
            "hypothesis_id": {"type": "string"},
            "judgment": {
                "type": "string",
                "enum": ["supported", "contradicted", "unresolved"],
            },
            "evidence_ids": string_array,
            "gaps": string_array,
        }
    )
    impact = _strict_object(
        {
            "work_item_id": {"type": "string"},
            "action": {
                "type": "string",
                "enum": ["retain", "replace", "drop"],
            },
            "reason": {"type": "string"},
            "new_basis_hypothesis_ids": string_array,
            "replacement_work_item_id": nullable_string,
            "drop_subtree": {"type": "boolean"},
        }
    )
    return _strict_object(
        {
            "add_work_items": {"type": "array", "items": work_item},
            "update_work_items": {
                "type": "array",
                "items": work_item_update,
            },
            "add_hypotheses": {"type": "array", "items": hypothesis},
            "update_hypotheses": {
                "type": "array",
                "items": hypothesis_update,
            },
            "impact_decisions": {"type": "array", "items": impact},
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
    has_article_fetch_sidecar = "article_fetch" in normalized
    article_fetch = normalized.pop("article_fetch", None)
    if has_article_fetch_sidecar:
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
    if normalized.get("next") == "continue" and isinstance(article_fetch, dict):
        if any(
            isinstance(request, dict)
            and request.get("tool_name") == "fetch_articles"
            for request in normalized.get("tool_requests", ())
        ):
            raise ModelProtocolError(
                "article body fetch is duplicated across generic and dedicated slots"
            )
        article_ids = [
            article_fetch[key]
            for key in sorted(article_fetch)
            if key.startswith(("article_id_", "article_ref_"))
            and isinstance(article_fetch[key], str)
            and article_fetch[key]
        ]
        normalized.setdefault("tool_requests", []).append(
            {
                "request_id": article_fetch.get("request_id"),
                "work_item_id": article_fetch.get("work_item_id"),
                "tool_name": "fetch_articles",
                "arguments": {"article_ids": article_ids},
                "purpose": article_fetch.get("purpose"),
                "hypothesis_ids": article_fetch.get("hypothesis_ids") or [],
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


_MINIMAL_SOLVER_CONTRACT = """
出力原則:
- Provider schemaに従い、CaseState全体ではなく今回の差分だけを返す。
- decision_reasonには、提示された根拠・gap・上限から今回continueまたはfinalizeを選ぶ理由を一文で書く。内部思考の逐語記録や長い検討過程は書かない。
- update_jsonに許されるキーはadd_work_items、update_work_items、add_hypotheses、update_hypotheses、impact_decisionsだけ。work_tree等の現在状態を返さない。
- continueは同Cycleの次step、またはstart_next_cycle=trueによる次Cycle開始であり、answerは返さない。
- finalizeは追加Toolを返さず、通常完了では全WorkItemを閉じる。上限時の限定回答だけ未解決IDとlimitationsを対応させる。

update_jsonの状態契約:
- add_work_items要素: work_item_id、parent_work_item_id、question、state、resolution、basis_hypothesis_ids、replaces_work_item_id。statusは使わない。
- update_work_items要素: work_item_id、state、resolution、basis_hypothesis_ids。
- add_hypotheses要素: hypothesis_id、work_item_id、statement、judgment、evidence_ids、gaps。statusは使わない。
- update_hypotheses要素: hypothesis_id、judgment、evidence_ids、gaps。
- WorkItemのstate=openは未完了なのでresolution=null、resolved/droppedは終了状態なので空でないresolutionを持つ。
- next_focus_work_item_idsと各ToolRequest.work_item_idは、このupdate適用後もstate=openのWorkItemだけを参照する。Toolが必要ならWorkItemを閉じない。
- Hypothesisのjudgment=unresolvedは未確認、supported/contradictedは本文根拠で確認済みなので空でないevidence_idsを持つ。
- impact_decisions要素: work_item_id、action、reason、new_basis_hypothesis_ids、replacement_work_item_id、drop_subtree。既存Hypothesisをcontradictedへ変える場合だけ使い、actionはretain / replace / dropのいずれか。それ以外は空配列にする。
- required_dependency_work_item_idsがあれば各WorkItemのDependencyDecisionを1件ずつ返す。not_required/resolvedはaction_request_id=null。needs_actionは通常は同じDecisionのToolを参照するが、Cycle境界でstart_next_cycle=trueならToolを返さずaction_request_id=nullにする。
- 通常finalizeでは現在openの全WorkItemを同じupdate_jsonでresolved/droppedへ閉じる。未確認なら閉じずcontinueし、上限時だけ未解決IDとlimitationsを対応させる。
- finalize時のanswer.citation_idsには、resolved WorkItemのbasis Hypothesisが選んだEvidenceを漏れなく含める。不要なEvidenceならHypothesis側から外す。

参照契約:
- IDはSolverContextまたは直前Decisionに表示された値だけを完全一致で使い、名前から生成しない。
- retain_evidence_idsはmax_retained_evidence件以内で、次Cycleにも本文提示が必要なEvidenceだけを選ぶ。
- statusの意味、根拠の十分性、追加調査、Graph候補の採否はsystem promptに従ってSolverが判断する。
- 対象がない任意配列は空、任意objectはnull、更新がなければupdateは空objectにする。
""".strip()


_SOLVER_CONTRACT = """
復元後のSolverDecision契約:
{
  "next": "continue" | "finalize",
  "decision_reason": str,
  "start_next_cycle": bool,
  "update": {
    "add_work_items": [{"work_item_id": str, "parent_work_item_id": str|null, "question": str, "state": "open"|"resolved"|"dropped", "resolution": str|null, "basis_hypothesis_ids": [str], "replaces_work_item_id": str|null}],
    "update_work_items": [{"work_item_id": str, "state": "open"|"resolved"|"dropped", "resolution": str|null, "basis_hypothesis_ids": [str]}],
    "add_hypotheses": [{"hypothesis_id": str, "work_item_id": str, "statement": str, "judgment": "supported"|"contradicted"|"unresolved", "evidence_ids": [str], "gaps": [str]}],
    "update_hypotheses": [{"hypothesis_id": str, "judgment": "supported"|"contradicted"|"unresolved", "evidence_ids": [str], "gaps": [str]}],
    "impact_decisions": [{"work_item_id": str, "action": "retain"|"replace"|"drop", "reason": str, "new_basis_hypothesis_ids": [str], "replacement_work_item_id": str|null, "drop_subtree": bool}]
  },
  "next_focus_work_item_ids": [str],
  "retain_evidence_ids": [str],
  "dependency_decisions": [{"dependency_kind": str, "work_item_id": str, "status": "not_required"|"needs_action"|"resolved", "reason": str, "basis_evidence_ids": [str], "action_request_id": str|null}],
  "graph_candidate_review": {"graph_request_ids": [str], "reviewed_link_ids": [str], "frontier_decisions": [{"frontier_item_id": str, "article_id": str, "work_item_id": str, "hypothesis_id": str|null, "action": "select"|"defer"|"reject", "reason": str}], "reason": str} | null,
  "frontier_re_adoptions": [{"article_id": str, "work_item_id": str, "hypothesis_id": str, "reason": str}],
  "deferred_frontier_resolutions": [{"frontier_item_id": str, "article_id": str, "work_item_id": str, "hypothesis_id": str|null, "action": "fetch_next_cycle"|"carry_forward"|"no_longer_needed"|"unresolved_at_limit", "reason": str}],
  "unreviewed_graph_resolution": {"action": "review_next_cycle"|"no_longer_needed"|"unresolved_at_limit", "reason": str} | null,
  "tool_requests": [{"request_id": str, "work_item_id": str, "tool_name": str, "arguments": object, "purpose": str, "hypothesis_ids": [str]}],
  "answer": {"text": str, "citation_ids": [str], "limitations": [str], "unresolved_work_item_ids": [str], "unresolved_hypothesis_ids": [str]} | null
}
continueは原則1件以上のtool_requests、graph_candidate_review、frontier_re_adoptions、deferred_frontier_resolutions、unreviewed_graph_resolution、またはstart_next_cycle=trueとanswer=nullを持つ。Graph候補選別判断は選択の有無にかかわらずtool_requests=[]とし、selectした既知Article IDはAgentLoopが本文取得へ機械転記する。Cycle境界で未評価Graph候補を次Cycleに引き継ぐ場合は、ToolRequestなしのstart_next_cycle=trueとunreviewed_graph_resolution.action=review_next_cycleを返す。finalizeはtool_requests=[]とanswerを持つ。省略可能な配列は空配列、updateは空objectにできる。
輸送時だけupdate全体をupdate_jsonへ、tool_requests配列全体をtool_requests_jsonへJSON文字列化する。dependency_decisionsはProvider schemaの構造化配列として直接返し、answer本文も二重エンコードしない。

契約語彙:
- next: continueは同じCycleの次stepまたは次Cycleへ判断・実行を継続する。finalizeは追加Toolなしで現在の確認済み範囲から回答を確定する。
- start_next_cycle: falseは現在のCycleで次のaction-observation stepへ進む。trueは現Cycleを評価して閉じ、次Cycleを開始する。初回Tool実行はfalseでもCycle 1を開始する。通常の検索・本文取得・Graph候補選別ではfalseとする。作業分解・仮説・探索方針を仕切り直す場合、またはCycle取得枠が尽きた後も必要と判断した未取得Evidenceが残る場合はtrueにできる。Graph候補選別モードでは必ずfalse。
- WorkItem.state: openは未完了で追加作業が必要。resolvedは問いへ結論が出てresolutionにその結論を書く。droppedは前提否定・重複・質問との無関係により作業対象から外し、resolutionに除外理由を書く。取得失敗だけを理由にresolvedへしない。
- WorkItemのparent_work_item_idは包含する親作業、basis_hypothesis_idsはその作業を成立させる前提、replaces_work_item_idは置換した旧作業を表す。next_focus_work_item_idsには次サイクルでTool対象にするopen WorkItemだけを入れる。
- Hypothesis.judgment: supportedは提示された根拠がstatementを支持する。contradictedは提示された根拠がstatementを否定する。unresolvedは根拠不足・両義的・未確認で、真偽を確定していない。supportedでもWorkItem全体が完了したとは限らない。
- 1つのHypothesisは独立に検証できる1つの命題とする。適用要件、数値基準、例外、義務・手続など別の根拠で判断できる観点を束ねず、別の観点のEvidenceで未確認の観点を完了しない。
- Hypothesis.gapsはunresolvedの理由または追加確認事項であり、limitationsは最終回答に残る制約である。調査可能なgapsをlimitationsへ移すだけで完了扱いにしない。
- answer.limitationsは質問への回答に残った未確認事項だけを表す。一般的な注意書きはanswer.textへ書く。limitationsがある場合は対応するopen WorkItemをunresolved_work_item_idsへ、そのWorkItemに属するunresolved Hypothesisをunresolved_hypothesis_idsへ指定する。通常の早期finalizeでは3項目をすべて空にする。上限等で次Cycle不能な限定回答だけ、open WorkItemを偽って閉じず3項目を対応させる。
- impact_decisions.action: retainはWorkItemを維持し、否定された前提をnew_basis_hypothesis_idsへ差し替える。replaceは旧WorkItemをdroppedにして、同じupdateで追加するreplacement_work_item_idへ置き換える。dropはWorkItemを不要として閉じ、drop_subtree=trueなら未完了の子孫も閉じる。これはHypothesisを新たにcontradictedへ変更したことで影響を受けるopen WorkItemにだけ使う。
- ToolResult.status: succeededはTool実行が完了した状態であり、得た内容が質問を立証したという意味ではない。failedはToolがエラー終了、timeoutは制限時間内に完了しなかった状態。failed/timeoutではerror_codeを確認し、Evidenceがないことを不存在の根拠にしない。
- Graph ToolResultのgraph_projection_updated=trueは、取得したGraph情報がCaseStoreへ保存され、差分batchまたはledgerへ投影可能になった実行事実であり、候補の関連性や本文確認済みを意味しない。
- Graph Reviewは同じSolverがGraph候補の関連性と本文取得順を判断する処理モードであり、任意のReviewer Agentとは別である。Reviewer無効時も必要なGraph Reviewを行う。
- frontier review statusのunreviewedは現在のHypothesisについて未評価、selectedはSolverが本文取得対象に選択、relevant_deferredは関連ありだが取得枠外、rejectedは現在の質問・Hypothesisに不要との判断を示す。selectedは本文取得成功を意味せず、content_statusと混同しない。
- finalize_only: falseなら必要な追加調査をcontinueできる。trueなら上限到達後の最終化呼出しなので、追加Toolを要求せず確認済み範囲と未確認範囲を分けてfinalizeする。
- material_included: trueのEvidenceだけ本文がmaterial_evidenceに提示されている。falseはmanifest情報だけで本文未提示。
- graph_review_batchは今回判断が必要な新規・再採用・新Link差分だけを示す。graph_review_ledgerは過去の全評価済みfrontierの短い最新状態であり、全Graph履歴の消失を意味しない。Graph navigation Evidenceはこれらの投影がSolver向けの唯一表示で、manifest等へ重複掲載されない。
- graph_review_batchのreview_triggerはnew_frontier=新規、re_adopted=別Hypothesisへの明示的再採用、new_link=既評価候補への新経路追加を表す。new_linkではprior_review_statusを前提にせず、今回提示された全Linkを含めて判断を更新する。
- batch内の全frontierへselect/defer/rejectを返す。selectは質問とHypothesisに関係し今回本文取得、deferは関係するが今回の枠外、rejectは関係しないとの意味判断である。表示順や枠外を理由にrejectしない。選択はmax_selected_frontier_per_stepとremaining_fetch_capacityの小さい方までとする。
- graph_review_ledgerのrelevant_deferredは後続stepまたは次Cycleでselectできる。selectedかつfailed/timeoutは取得再試行だけを判断できる。rejectedを別Hypothesisで使う場合はfrontier_re_adoptionsに既知Article・open WorkItem・所属Hypothesis・理由を明示し、プログラムへ自動転用を要求しない。
- Cycle境界では、本文未取得のactiveなrelevant_deferred全件へdeferred_frontier_resolutionsを返す。fetch_next_cycleは次Cycle最初の本文取得に含める判断で、start_next_cycle=trueを必要とする。そのArticleはProgramが1つのfetch_articlesへ機械転記するため、同じToolRequestを重ねて返さない。carry_forwardは取得上限等により次Cycle以降のactive候補として保持する判断で、start_next_cycle=trueを必要とする。no_longer_neededは後続Evidenceを踏まえて質問への回答に不要と判断した状態である。unresolved_at_limitは上限により次Cycleを開始できず未確認のまま最終化する状態で、answer.limitationsへ明記する。Programは既知ID・全件性・次動作との参照整合だけを検証し、どのactionが法的に妥当かは判断しない。
- graph_review_batch.candidates=[]かつremaining_unreviewed_count>0のCycle境界ではunreviewed_graph_resolutionを必ず返す。review_next_cycleは次Cycleで差分Review、no_longer_neededは質問への回答に不要とのSolver判断、unresolved_at_limitは次Cycle不能のため未確認のまま限定回答する判断である。Programはactionとnext、start_next_cycle、limitationsの構造整合だけを検証する。
- content_statusは本文取得状態である。not_requestedは未要求、pendingは結果待ち、succeededは取得成功、failedはエラー終了、timeoutは時間切れを示し、法的関連性や根拠採用を意味しない。
- Graph探索は1回のlegal_graph_neighbors要求につき1ホップである。Graph候補Articleも、Solverが次のHypothesis検証に必要と判断すれば後続stepの新しい起点にできる。起点の由来を理由に再探索を禁止しない。各要求は1 mode・1 predicate（semantic_assertion時）・1 directionに限定する。
- grounding_evidence_idsは意味判断と引用に使える本文Evidence、navigation_evidence_idsはGraph以外の候補発見専用Evidence、fetchable_article_idsはfetch_articlesへ完全一致で渡せるArticle IDである。
- search_navigationの本文抜粋は次のTool選択専用であり、Hypothesisのjudgment、WorkItemのresolution、確認済みの主張、answerの根拠に使わない。必要な命題がsearch_navigationにしかなければunresolved/openを維持し、Article本文を取得する。
- dependency_decisions.status: not_requiredは当該依存確認が回答に不要、needs_actionは必要だが追加Toolが必要、resolvedは質問に関係する下位規範まで確認済み。lower_normは、質問で求める範囲・要件・例外・手続を具体化する下位規範の確認を表す。basis_evidence_idsには判断に使った取得本文を入れる。needs_actionだけ同じDecisionのToolRequestをaction_request_idで参照し、not_requiredとresolvedはnullにする。どの検索・Article・根拠が必要かはToolRequestとHypothesisで表し、DependencyDecisionへ重複登録しない。required_dependency_kind=nullならこの契約は使わずdependency_decisions=[]とする。

ID用途契約:
- update_hypotheses.evidence_idsとanswer.citation_idsは、solver_context.grounding_evidence_idsから完全一致でコピーする。
- retain_evidence_idsとload_evidenceのevidence_idsは、solver_context.evidence_manifestのevidence_idから完全一致でコピーする。
- fetch_articlesのarticle_idsは、solver_context.fetchable_article_idsから完全一致でコピーする。
- 本文中の条番号が必要でも対応IDがfetchable_article_idsになければ、IDを作らずfetch_articlesから外し、法令名・条番号・確認事項をqueryにしたlegal_searchを使う。
- articleIdとevidenceIdは別の名前空間である。articleIdへparagraph/item等を付加してevidenceIdを生成・推測しない。
- solver_context.navigation_evidence_idsは検索経路候補であり、Hypothesisの根拠やcitation_idsには使わない。
- supported/contradictedのevidence_idsはstatementを直接支持または否定するEvidenceに限る。特定条文の内容をresolutionやanswerで説明するにはそのArticleのgrounding Evidenceが必要であり、別Article、search_navigation、Graph候補で代用しない。
- finalize時は、resolved WorkItemのbasis_hypothesis_idsが参照するHypothesisのevidence_idsをanswer.citation_idsへ含める。これはSolver自身が宣言した解決根拠と回答引用の参照整合である。
- retain_evidence_idsは全取得結果の列挙欄ではない。次回以降も本文が必要なEvidenceだけをSolverが選び、solver_context.max_retained_evidence以下にする。
- tool_requestsはsolver_context.max_tool_requests_per_step以下にする。これは1 Solver Decisionの上限であり、Cycle累計本文数ではない。プログラムは超過分を選別・切捨てしない。
- 1 Cycleの本文取得成功数はmax_fetched_resources_per_cycle、残りはremaining_fetch_capacityである。cycle_close_required=trueなら現Cycleへ新しいToolを追加せず、取得済み結果を評価する。can_start_next_cycle=trueなら次の命題・方針を示してstart_next_cycleを選べるが、falseならfinalizeする。
- 同じDecisionで取得する既知Article IDは、WorkItemが異なっても4個以内なら1つのfetch_articlesへ統合する。4個は上限であり目標ではない。fetch_articlesだけではGraph探索は行われない。
- 1つのDecisionにfetch_articlesを複数返さず、Article IDを重複させない。4個を超える候補はプログラムに統合・切捨てさせず、Solverが今回検証する4個以下を選ぶ。
- solver_context.required_dependency_kindがnullならdependency_decisions=[]とする。nullでなければrequired_dependency_work_item_idsと同じ件数を返し、各IDをちょうど1回使う。dependency_kindはrequired_dependency_kindへ一致させる。
- basis_evidence_idsはsolver_context.grounding_evidence_idsから、監査判断に実際に使った本文だけを選ぶ。needs_actionでは同じDecisionに必要なlegal_search、legal_graph_neighbors、fetch_articles等を返し、そのrequest_idをaction_request_idへ指定する。not_requiredまたはresolvedではaction_request_id=nullとする。下位規範のArticleと法的根拠は通常のToolRequest、Hypothesis、Evidence、回答citationで管理し、DependencyDecisionへ別のtarget・source証明を重複させない。
""".strip()
