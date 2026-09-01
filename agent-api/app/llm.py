import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from time import perf_counter, sleep
from typing import Any, Literal

import requests
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .agent_core import MaterialItem, ProjectionPolicy, Projector
from .config import settings
from .legal_issue_planner import (
    HARD_MAX_PRIMARY_ISSUES,
    IssuePlan,
    build_issue_plan_prompt,
    fallback_issue_plan,
    issue_plan_json_schema,
    parse_issue_plan,
)
from .llm_directed_research import (
    EvidenceCatalog,
    ResearchCheckpoint,
    ResearchTurn,
    build_research_checkpoint_prompt,
    build_research_turn_prompt,
    checkpoint_integration_prompt_content_ids,
    hydrate_relation_decision_candidates,
    parse_research_checkpoint,
    parse_research_turn,
    research_checkpoint_json_schema,
    research_turn_prompt_content_ids,
    research_turn_json_schema,
    validate_research_checkpoint,
)
from .models import AnswerRequest, Citation


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    latencyMs: int
    inputTokens: int | None
    outputTokens: int | None
    estimatedCost: int
    answer: str
    predictedAnswer: str | None
    choiceJudgements: dict[str, str] | None
    validationError: str | None = None
    stopReason: str | None = None
    contentBlockTypes: list[str] | None = None
    outputChars: int | None = None
    retryCount: int = 0
    questionPolarity: str | None = None
    choiceAssessments: dict[str, dict[str, Any]] | None = None
    answerStatus: str | None = None
    answerCitationIds: list[str] | None = None
    missing: list[str] | None = None
    answerIssueDecisions: list[dict[str, Any]] | None = None


@dataclass
class GroundingReviewResult:
    verdict: str
    issues: list[str]
    provider: str
    model: str
    latencyMs: int
    inputTokens: int | None
    outputTokens: int | None
    researchQueries: list[str] = field(default_factory=list)
    validationError: str | None = None
    stopReason: str | None = None
    retryCount: int = 0
    issueFindings: list[dict[str, str]] = field(default_factory=list)


@dataclass
class StructuredJSONResult:
    payload: dict[str, Any] | None
    provider: str
    model: str
    latencyMs: int
    inputTokens: int | None
    outputTokens: int | None
    validationError: str | None = None
    stopReason: str | None = None
    retryCount: int = 0


@dataclass
class SearchPlanResult:
    queries: list[str]
    graphRequired: bool
    provider: str
    model: str
    latencyMs: int
    inputTokens: int | None
    outputTokens: int | None
    validationError: str | None = None
    stopReason: str | None = None
    retryCount: int = 0


@dataclass
class IssuePlanResult:
    """structured issue plannerの結果 (layered_legal_evidence_retrieval_plan.md §7.2)。"""

    plan: IssuePlan
    provider: str
    model: str
    latencyMs: int
    inputTokens: int | None
    outputTokens: int | None
    validationError: str | None = None
    stopReason: str | None = None
    retryCount: int = 0


@dataclass
class EvidenceEvaluationResult:
    choiceCoverage: dict[str, str]
    followUpQueries: list[str]
    graphRequired: bool
    stop: bool
    provider: str
    model: str
    latencyMs: int
    inputTokens: int | None
    outputTokens: int | None
    validationError: str | None = None
    stopReason: str | None = None
    retryCount: int = 0


@dataclass
class ResearchTurnResult:
    """LLM主導調査の1ターン分の判断と呼び出しメタデータ。"""

    turn: ResearchTurn | None
    provider: str
    model: str
    latencyMs: int
    inputTokens: int | None
    outputTokens: int | None
    validationError: str | None = None
    stopReason: str | None = None
    retryCount: int = 0
    promptContentUnitIds: tuple[str, ...] | None = None

    def as_trace(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "latencyMs": self.latencyMs,
            "inputTokens": self.inputTokens,
            "outputTokens": self.outputTokens,
            "validationError": self.validationError,
            "stopReason": self.stopReason,
            "retryCount": self.retryCount,
            "promptContentUnitIds": list(self.promptContentUnitIds or ()),
            "decision": self.turn.model_dump() if self.turn is not None else None,
        }


@dataclass
class ResearchCheckpointResult:
    checkpoint: ResearchCheckpoint | None
    provider: str
    model: str
    latencyMs: int
    inputTokens: int | None
    outputTokens: int | None
    validationError: str | None = None
    stopReason: str | None = None
    retryCount: int = 0
    promptContentUnitIds: tuple[str, ...] | None = None
    finalCycle: bool = False

    def as_trace(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "latencyMs": self.latencyMs,
            "inputTokens": self.inputTokens,
            "outputTokens": self.outputTokens,
            "validationError": self.validationError,
            "stopReason": self.stopReason,
            "retryCount": self.retryCount,
            "promptContentUnitIds": list(self.promptContentUnitIds or ()),
            "finalCycle": self.finalCycle,
            "checkpoint": (
                self.checkpoint.model_dump() if self.checkpoint is not None else None
            ),
        }


class LLMChoiceAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["entailed", "contradicted", "insufficient"]
    citationIds: list[str] = Field(max_length=3)
    reason: str = Field(max_length=300)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class LLMAnswerIssueDecision(BaseModel):
    """Mainが論点ごとに行う判断。意味はLLMが決め、コードは整合性だけを見る。"""

    model_config = ConfigDict(extra="forbid")

    issueId: str = Field(min_length=1, max_length=80)
    status: Literal["ready", "partial", "insufficient"]
    conclusion: str = Field(max_length=600)
    citationIds: list[str] = Field(max_length=5)
    missing: list[str] = Field(max_length=4)


class LLMAnswerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    questionPolarity: Literal["select_entailed", "select_contradicted"] | None
    predictedAnswer: str | None
    choiceAssessments: dict[str, LLMChoiceAssessment] | None
    answerStatus: Literal["ready", "partial", "insufficient"] | None
    citationIds: list[str] | None
    missing: list[str] | None
    issueDecisions: list[LLMAnswerIssueDecision] | None = None


class GroundingReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issueId: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=300)


class GroundingReviewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal[
        "supported",
        "needs_revision",
        "needs_research",
        "insufficient",
    ]
    findings: list[GroundingReviewFinding] = Field(
        default_factory=list, max_length=3
    )
    # 旧保存データとparser単体テストの読込み互換。新しい生成schemaはfindingsのみを出す。
    issues: list[str] = Field(default_factory=list, max_length=3)
    researchQueries: list[str] = Field(max_length=2)


class SearchPlanPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    queries: list[str] = Field(min_length=1, max_length=8)
    graphRequired: bool = False


class EvidenceEvaluationPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    choiceCoverage: dict[
        str, Literal["sufficient", "missing", "missing_definition", "missing_exception"]
    ]
    followUpQueries: list[str] = Field(default_factory=list, max_length=8)
    graphRequired: bool = False
    stop: bool = False


class LLMClient:
    supports_iterative_research = True

    def __init__(
        self,
        *,
        provider: str | None = None,
        ollama_num_ctx: int | None = None,
        ollama_think: bool | None = None,
    ) -> None:
        self.provider = (provider or settings.llm_provider).lower()
        self.ollama_num_ctx = ollama_num_ctx
        self.ollama_think = ollama_think

    def health(self) -> dict[str, Any]:
        if self.provider == "anthropic":
            return self._anthropic_health()
        if self.provider == "openai":
            return self._openai_health()
        if self.provider != "ollama":
            return {
                "provider": self.provider,
                "ok": False,
                "reason": "Unsupported LLM_PROVIDER",
            }
        return self._ollama_health()

    def generate_structured_json(
        self,
        *,
        prompt: str,
        schema: dict[str, Any],
        model: str,
        max_tokens: int,
        timeout_sec: int,
    ) -> StructuredJSONResult:
        """用途固有の意味判断を持たないprovider共通structured-output入口。"""

        def parse(raw_text: str) -> tuple[dict[str, Any] | None, str | None]:
            try:
                payload = json.loads(raw_text)
            except (TypeError, json.JSONDecodeError):
                return None, "invalid_json"
            if not isinstance(payload, dict):
                return None, "json_root_not_object"
            return payload, None

        (
            (payload, validation_error),
            latency_ms,
            input_tokens,
            output_tokens,
            stop_reason,
            retry_count,
        ) = self._call_with_retry(
            prompt,
            schema,
            model,
            max_tokens,
            timeout_sec,
            parse,
        )
        return StructuredJSONResult(
            payload=payload,
            provider=self.provider,
            model=model,
            latencyMs=latency_ms,
            inputTokens=input_tokens,
            outputTokens=output_tokens,
            validationError=validation_error,
            stopReason=stop_reason,
            retryCount=retry_count,
        )

    def _ollama_health(self) -> dict[str, Any]:
        try:
            response = requests.get(
                f"{settings.ollama_base_url.rstrip('/')}/api/tags", timeout=3
            )
            response.raise_for_status()
            models = [model["name"] for model in response.json().get("models", [])]
            required_models = {
                settings.answer_model,
                settings.reviewer_model,
                settings.planner_model,
                settings.evaluator_model,
                settings.llm_research_stage_model,
                settings.llm_research_integration_model,
            }
            if settings.relation_classifier_provider == "ollama":
                required_models.update(
                    {
                        settings.relation_classifier_model,
                        settings.relation_classifier_reviewer_model,
                    }
                )
            return {
                "provider": "ollama",
                "ok": all(model in models for model in required_models),
                "baseUrl": settings.ollama_base_url,
                "answerModel": settings.answer_model,
                "reviewerModel": settings.reviewer_model,
                "plannerModel": settings.planner_model,
                "evaluatorModel": settings.evaluator_model,
                "researchStageModel": settings.llm_research_stage_model,
                "researchIntegrationModel": (
                    settings.llm_research_integration_model
                ),
                "researchModel": settings.llm_research_model,
                "relationClassifierProvider": (
                    settings.relation_classifier_provider
                ),
                "relationClassifierModel": settings.relation_classifier_model,
                "relationClassifierReviewerModel": (
                    settings.relation_classifier_reviewer_model
                ),
                "availableModels": models,
            }
        except Exception:
            return {
                "provider": "ollama",
                "ok": False,
                "baseUrl": settings.ollama_base_url,
                "answerModel": settings.answer_model,
                "reviewerModel": settings.reviewer_model,
                "researchStageModel": settings.llm_research_stage_model,
                "researchIntegrationModel": (
                    settings.llm_research_integration_model
                ),
                "relationClassifierProvider": (
                    settings.relation_classifier_provider
                ),
                "relationClassifierModel": settings.relation_classifier_model,
                "relationClassifierReviewerModel": (
                    settings.relation_classifier_reviewer_model
                ),
                "reasonCode": "ollama_health_check_failed",
            }

    def _anthropic_health(self) -> dict[str, Any]:
        if not settings.anthropic_api_key:
            return {
                "provider": "anthropic",
                "ok": False,
                "baseUrl": settings.anthropic_base_url,
                "answerModel": settings.answer_model,
                "reviewerModel": settings.reviewer_model,
                "researchStageModel": settings.llm_research_stage_model,
                "researchIntegrationModel": (
                    settings.llm_research_integration_model
                ),
                "relationClassifierProvider": (
                    settings.relation_classifier_provider
                ),
                "relationClassifierModel": settings.relation_classifier_model,
                "relationClassifierReviewerModel": (
                    settings.relation_classifier_reviewer_model
                ),
                "reasonCode": "anthropic_api_key_missing",
            }
        configured_models = [
            settings.answer_model,
            settings.reviewer_model,
            settings.planner_model,
            settings.evaluator_model,
            settings.llm_research_stage_model,
            settings.llm_research_integration_model,
        ]
        if settings.relation_classifier_provider == "anthropic":
            configured_models.extend(
                [
                    settings.relation_classifier_model,
                    settings.relation_classifier_reviewer_model,
                ]
            )
        models = tuple(dict.fromkeys(configured_models))
        checks = []
        try:
            for model in models:
                response = requests.get(
                    f"{settings.anthropic_base_url.rstrip('/')}/v1/models/{model}",
                    headers={
                        "x-api-key": settings.anthropic_api_key,
                        "anthropic-version": settings.anthropic_version,
                    },
                    timeout=5,
                )
                checks.append(
                    {
                        "model": model,
                        "available": response.ok,
                        "supportsEffort": _anthropic_model_supports_effort(
                            model
                        ),
                        "supportsManualThinking": (
                            _anthropic_model_supports_manual_thinking(model)
                        ),
                    }
                )
        except requests.RequestException:
            return {
                "provider": "anthropic",
                "ok": False,
                "baseUrl": settings.anthropic_base_url,
                "reasonCode": "anthropic_model_check_failed",
                "modelChecks": checks,
            }
        unavailable = [
            check["model"] for check in checks if not check["available"]
        ]
        return {
            "provider": "anthropic",
            "ok": not unavailable,
            "baseUrl": settings.anthropic_base_url,
            "answerModel": settings.answer_model,
            "reviewerModel": settings.reviewer_model,
            "plannerModel": settings.planner_model,
            "evaluatorModel": settings.evaluator_model,
            "researchStageModel": settings.llm_research_stage_model,
            "researchIntegrationModel": (
                settings.llm_research_integration_model
            ),
            "researchModel": settings.llm_research_model,
            "relationClassifierProvider": settings.relation_classifier_provider,
            "relationClassifierModel": settings.relation_classifier_model,
            "relationClassifierReviewerModel": (
                settings.relation_classifier_reviewer_model
            ),
            "modelChecks": checks,
            "researchEffort": {
                "stageRequested": settings.llm_research_stage_effort,
                "integrationRequested": (
                    settings.llm_research_integration_effort
                ),
                "stageEffective": (
                    settings.llm_research_stage_effort
                    if _anthropic_model_supports_effort(
                        settings.llm_research_stage_model
                    )
                    else None
                ),
                "integrationEffective": (
                    settings.llm_research_integration_effort
                    if _anthropic_model_supports_effort(
                        settings.llm_research_integration_model
                    )
                    else None
                ),
            },
            "manualThinking": {
                "requestedBudgetTokens": (
                    settings.anthropic_thinking_budget_tokens
                ),
                "enabledModels": [
                    model
                    for model in models
                    if _anthropic_model_supports_manual_thinking(model)
                    and settings.anthropic_thinking_budget_tokens >= 1024
                ],
            },
            **(
                {"reasonCode": "anthropic_model_unavailable"}
                if unavailable
                else {}
            ),
        }

    def _openai_health(self) -> dict[str, Any]:
        base = {
            "provider": "openai",
            "baseUrl": settings.openai_base_url,
            "answerModel": settings.answer_model,
            "reviewerModel": settings.reviewer_model,
            "plannerModel": settings.planner_model,
            "evaluatorModel": settings.evaluator_model,
            "researchStageModel": settings.llm_research_stage_model,
            "researchIntegrationModel": settings.llm_research_integration_model,
            "researchModel": settings.llm_research_model,
            "reasoningEffort": settings.openai_reasoning_effort,
            "relationClassifierProvider": settings.relation_classifier_provider,
            "relationClassifierModel": settings.relation_classifier_model,
            "relationClassifierReviewerModel": (
                settings.relation_classifier_reviewer_model
            ),
        }
        if not settings.openai_api_key:
            return {
                **base,
                "ok": False,
                "reasonCode": "openai_api_key_missing",
            }
        configured_models = [
            settings.answer_model,
            settings.reviewer_model,
            settings.planner_model,
            settings.evaluator_model,
            settings.llm_research_stage_model,
            settings.llm_research_integration_model,
        ]
        if settings.relation_classifier_provider == "openai":
            configured_models.extend(
                [
                    settings.relation_classifier_model,
                    settings.relation_classifier_reviewer_model,
                ]
            )
        models = tuple(dict.fromkeys(configured_models))
        checks = []
        try:
            for model in models:
                response = requests.get(
                    f"{settings.openai_base_url.rstrip('/')}/models/{model}",
                    headers=_openai_headers(),
                    timeout=5,
                )
                checks.append({"model": model, "available": response.ok})
        except requests.RequestException:
            return {
                **base,
                "ok": False,
                "reasonCode": "openai_model_check_failed",
                "modelChecks": checks,
            }
        unavailable = [
            check["model"] for check in checks if not check["available"]
        ]
        return {
            **base,
            "ok": not unavailable,
            "modelChecks": checks,
            **(
                {"reasonCode": "openai_model_unavailable"}
                if unavailable
                else {}
            ),
        }

    def generate_answer(
        self,
        request: AnswerRequest,
        route: list[str],
        citations: list[Citation],
        timeout_sec: int | None = None,
        evidence_by_choice: dict[str, list[str]] | None = None,
        answer_scope: dict[str, Any] | None = None,
        research_context: dict[str, Any] | None = None,
        review_feedback: list[str] | None = None,
        review_verdict: str | None = None,
        previous_answer: str | None = None,
        previous_answer_status: str | None = None,
        previous_citation_ids: list[str] | None = None,
        previous_missing: list[str] | None = None,
        previous_issue_decisions: list[dict[str, Any]] | None = None,
        review_findings: list[dict[str, str]] | None = None,
    ) -> LLMResult | None:
        prompt = build_answer_prompt(
            request,
            route,
            citations,
            evidence_by_choice,
            answer_scope=answer_scope,
            research_context=research_context,
            review_feedback=review_feedback,
            review_verdict=review_verdict,
            previous_answer=previous_answer,
            previous_answer_status=previous_answer_status,
            previous_citation_ids=previous_citation_ids,
            previous_missing=previous_missing,
            previous_issue_decisions=previous_issue_decisions,
            review_findings=review_findings,
        )
        timeout = timeout_sec or settings.llm_timeout_sec
        max_tokens = settings.anthropic_max_tokens
        started = perf_counter()
        result = self._generate_once(request, prompt, timeout, citations, max_tokens)
        if result is not None and not result.validationError:
            contract_error = _answer_contract_error(result, research_context)
            if contract_error:
                result = replace(result, validationError=contract_error)
        if result is not None and result.validationError:
            retry_timeout = _retry_timeout(timeout, started)
            if retry_timeout is not None:
                retried = self._retry_answer(
                    request,
                    _build_contract_retry_prompt(
                        prompt,
                        result.validationError,
                        role="Main Agent",
                    ),
                    retry_timeout,
                    citations,
                    _retry_max_tokens(max_tokens, result.stopReason),
                )
                if retried is not None:
                    if not retried.validationError:
                        contract_error = _answer_contract_error(
                            retried, research_context
                        )
                        if contract_error:
                            retried = replace(
                                retried, validationError=contract_error
                            )
                    result = replace(
                        retried,
                        retryCount=1,
                        latencyMs=result.latencyMs + retried.latencyMs,
                        inputTokens=_sum_optional(
                            result.inputTokens, retried.inputTokens
                        ),
                        outputTokens=_sum_optional(
                            result.outputTokens, retried.outputTokens
                        ),
                    )
        return result

    def review_answer_grounding(
        self,
        request: AnswerRequest,
        answer: str,
        citations: list[Citation],
        *,
        timeout_sec: int,
        research_context: dict[str, Any] | None = None,
        answer_status: str | None = None,
        citation_ids: list[str] | None = None,
        missing: list[str] | None = None,
        issue_decisions: list[dict[str, Any]] | None = None,
        available_citations: list[Citation] | None = None,
    ) -> GroundingReviewResult:
        """Main LLMの最終判断を批評する。回答本文は生成・書換えしない。"""

        prompt = build_grounding_review_prompt(
            request,
            answer,
            citations,
            research_context=research_context,
            answer_status=answer_status,
            citation_ids=citation_ids,
            missing=missing,
            issue_decisions=issue_decisions,
            available_citations=available_citations,
        )
        issue_ids = _answer_contract_issue_ids(research_context)
        schema = _grounding_review_json_schema(issue_ids)
        max_tokens = settings.reviewer_max_tokens
        started = perf_counter()
        (
            raw_text,
            latency_ms,
            input_tokens,
            output_tokens,
            stop_reason,
        ) = self._json_transport(
            prompt,
            schema,
            settings.reviewer_model,
            max_tokens,
            timeout_sec,
        )
        (
            verdict,
            issues,
            findings,
            research_queries,
            validation_error,
        ) = _parse_grounding_review(raw_text, issue_ids)
        retry_count = 0
        retry_timeout = (
            _retry_timeout(timeout_sec, started)
            if validation_error
            else None
        )
        retry_max_tokens = _retry_max_tokens(max_tokens, stop_reason)
        if retry_timeout is not None:
            (
                retry_raw_text,
                retry_latency_ms,
                retry_input_tokens,
                retry_output_tokens,
                retry_stop_reason,
            ) = self._json_transport(
                _build_contract_retry_prompt(
                    prompt,
                    validation_error,
                    role="Reviewer",
                ),
                schema,
                settings.reviewer_model,
                retry_max_tokens,
                retry_timeout,
            )
            (
                verdict,
                issues,
                findings,
                research_queries,
                validation_error,
            ) = _parse_grounding_review(retry_raw_text, issue_ids)
            latency_ms += retry_latency_ms
            input_tokens = _sum_optional(input_tokens, retry_input_tokens)
            output_tokens = _sum_optional(output_tokens, retry_output_tokens)
            stop_reason = retry_stop_reason
            retry_count = 1
        if validation_error and stop_reason == "max_tokens":
            validation_error = "grounding_review_output_truncated"
        return GroundingReviewResult(
            verdict=verdict,
            issues=issues,
            researchQueries=research_queries,
            provider=self.provider,
            model=settings.reviewer_model,
            latencyMs=latency_ms,
            inputTokens=input_tokens,
            outputTokens=output_tokens,
            validationError=validation_error,
            stopReason=stop_reason,
            retryCount=retry_count,
            issueFindings=findings,
        )

    def _retry_answer(
        self,
        request: AnswerRequest,
        prompt: str,
        timeout_sec: int,
        citations: list[Citation],
        max_tokens: int,
    ) -> LLMResult | None:
        """再試行が失敗しても1回目の結果を残せるよう、例外を呼び出し元へ伝えない。"""
        try:
            return self._generate_once(
                request, prompt, timeout_sec, citations, max_tokens
            )
        except Exception:
            return None

    def _generate_once(
        self,
        request: AnswerRequest,
        prompt: str,
        timeout_sec: int | None,
        citations: list[Citation],
        max_tokens: int | None = None,
    ) -> LLMResult | None:
        if self.provider == "ollama":
            return self._generate_ollama(request, prompt, timeout_sec, citations)
        if self.provider == "anthropic":
            return self._generate_anthropic(
                request, prompt, timeout_sec, citations, max_tokens
            )
        if self.provider == "openai":
            return self._generate_openai(
                request, prompt, timeout_sec, citations, max_tokens
            )
        return None

    def _call_with_retry(
        self,
        prompt: str,
        schema: dict[str, Any],
        model: str,
        max_tokens: int,
        timeout: int,
        parse_fn: Callable[[str], tuple],
    ) -> tuple[tuple, int, int | None, int | None, str | None, int]:
        """`_json_transport`を呼び出し、parse_fnの結果の最終要素(validation_error)が
        真の場合のみ残り時間内で1回だけ再試行してトークンを合算する。
        戻り値: (parse_fnの結果, latencyMs, inputTokens, outputTokens, stopReason, retryCount)"""
        started = perf_counter()
        retry_count = 0
        try:
            raw_text, latency_ms, input_tokens, output_tokens, stop_reason = (
                self._json_transport(prompt, schema, model, max_tokens, timeout)
            )
        except requests.ConnectionError:
            retry_timeout = _retry_timeout(timeout, started)
            if retry_timeout is None:
                raise
            retry_count = 1
            sleep(min(0.25, retry_timeout / 2))
            raw_text, _, input_tokens, output_tokens, stop_reason = (
                self._json_transport(
                    prompt,
                    schema,
                    model,
                    max_tokens,
                    retry_timeout,
                )
            )
            latency_ms = round((perf_counter() - started) * 1000)
        parsed = parse_fn(raw_text)
        validation_error = parsed[-1]
        retry_timeout = (
            _retry_timeout(timeout, started)
            if validation_error and retry_count == 0
            else None
        )
        if retry_timeout is not None:
            retry_count = 1
            first_input_tokens, first_output_tokens = input_tokens, output_tokens
            (
                raw_text,
                retry_latency,
                retry_input_tokens,
                retry_output_tokens,
                stop_reason,
            ) = self._json_transport(prompt, schema, model, max_tokens, retry_timeout)
            latency_ms += retry_latency
            input_tokens = _sum_optional(first_input_tokens, retry_input_tokens)
            output_tokens = _sum_optional(first_output_tokens, retry_output_tokens)
            parsed = parse_fn(raw_text)
        return parsed, latency_ms, input_tokens, output_tokens, stop_reason, retry_count

    def plan_search(
        self,
        request: AnswerRequest,
        max_queries: int,
        timeout_sec: int | None = None,
    ) -> SearchPlanResult:
        prompt = build_search_plan_prompt(request, max_queries)
        schema = _search_plan_json_schema(max_queries)
        timeout = timeout_sec or settings.planner_timeout_sec
        (
            (queries, graph_required, validation_error),
            latency_ms,
            input_tokens,
            output_tokens,
            stop_reason,
            retry_count,
        ) = self._call_with_retry(
            prompt,
            schema,
            settings.planner_model,
            settings.planner_max_tokens,
            timeout,
            lambda raw_text: _parse_search_plan(raw_text, max_queries),
        )
        return SearchPlanResult(
            queries=queries,
            graphRequired=graph_required,
            provider=self.provider,
            model=settings.planner_model,
            latencyMs=latency_ms,
            inputTokens=input_tokens,
            outputTokens=output_tokens,
            validationError=validation_error,
            stopReason=stop_reason,
            retryCount=retry_count,
        )

    def plan_legal_issues(
        self,
        request: AnswerRequest,
        max_issues: int = HARD_MAX_PRIMARY_ISSUES,
        timeout_sec: int | None = None,
    ) -> IssuePlanResult:
        """質問を法的論点へ分解する。失敗時はルールベースのfallback planを返す(§12)。"""
        prompt = build_issue_plan_prompt(
            request.question, choices=request.choices, max_issues=max_issues
        )
        schema = issue_plan_json_schema(max_issues)
        timeout = timeout_sec or settings.planner_timeout_sec
        (
            (plan, validation_error),
            latency_ms,
            input_tokens,
            output_tokens,
            stop_reason,
            retry_count,
        ) = self._call_with_retry(
            prompt,
            schema,
            settings.planner_model,
            settings.planner_max_tokens,
            timeout,
            lambda raw_text: _parse_issue_plan(raw_text, request.question),
        )
        return IssuePlanResult(
            plan=plan,
            provider=self.provider,
            model=settings.planner_model,
            latencyMs=latency_ms,
            inputTokens=input_tokens,
            outputTokens=output_tokens,
            validationError=validation_error,
            stopReason=stop_reason,
            retryCount=retry_count,
        )

    def decide_legal_research_turn(
        self,
        request: AnswerRequest,
        catalog: EvidenceCatalog,
        tool_history: list[dict[str, Any]] | None = None,
        timeout_sec: int | None = None,
        *,
        remaining_turns: int | None = None,
        remaining_tool_calls: int | None = None,
        finalize_only: bool = False,
        preferred_content_ids: tuple[str, ...] | list[str] = (),
        phase: str | None = None,
        cycle_index: int | None = None,
        cycle_count: int | None = None,
        checkpoint: ResearchCheckpoint | None = None,
        case_context: dict[str, Any] | None = None,
    ) -> ResearchTurnResult:
        """調査手順を固定せず、次の操作または回答可能性をLLMへ判断させる。"""
        max_actions = (
            0
            if finalize_only
            else min(
                settings.llm_research_max_actions_per_turn,
                max(
                    0,
                    remaining_tool_calls
                    if remaining_tool_calls is not None
                    else settings.llm_research_max_tool_calls,
                ),
            )
        )
        max_evidence_items = (
            min(settings.llm_research_max_evidence_items, 32)
            if finalize_only or phase == "deepen"
            else (
                min(settings.llm_research_max_evidence_items, 20)
                if phase == "explore"
                else settings.llm_research_max_evidence_items
            )
        )
        prompt = build_research_turn_prompt(
            question=request.question,
            choices=request.choices,
            catalog=catalog,
            tool_history=tool_history,
            max_actions=max_actions,
            max_evidence_items=max_evidence_items,
            evidence_chars=(
                min(settings.llm_research_evidence_chars, 12000)
                if finalize_only
                else (
                    min(
                        settings.llm_research_evidence_chars,
                        8000 if phase == "explore" else 12000,
                    )
                    if phase in {"explore", "deepen"}
                    else settings.llm_research_evidence_chars
                )
            ),
            remaining_turns=remaining_turns,
            remaining_tool_calls=remaining_tool_calls,
            finalize_only=finalize_only,
            max_selected_evidence=settings.llm_research_max_selected_evidence,
            preferred_content_ids=preferred_content_ids,
            phase=phase,
            cycle_index=cycle_index,
            cycle_count=cycle_count,
            checkpoint=checkpoint,
            case_context=case_context,
            output_token_limit=settings.llm_research_max_tokens,
        )
        allowed_content_unit_ids = research_turn_prompt_content_ids(
            catalog=catalog,
            checkpoint=checkpoint,
            preferred_content_ids=preferred_content_ids,
            max_evidence_items=max_evidence_items,
            evidence_chars=(
                min(settings.llm_research_evidence_chars, 12000)
                if finalize_only
                else (
                    min(
                        settings.llm_research_evidence_chars,
                        8000 if phase == "explore" else 12000,
                    )
                    if phase in {"explore", "deepen"}
                    else settings.llm_research_evidence_chars
                )
            ),
        )
        allowed_article_ids = _scoped_research_article_ids(
            catalog,
            checkpoint=checkpoint,
            preferred_content_ids=preferred_content_ids,
            case_context=case_context,
        )
        schema = research_turn_json_schema(
            max_actions=max_actions,
            max_selected_evidence=settings.llm_research_max_selected_evidence,
            finalize_only=finalize_only,
            known_article_ids=allowed_article_ids,
            known_document_ids=catalog.known_document_ids,
            known_content_unit_ids=allowed_content_unit_ids,
        )
        timeout = timeout_sec or settings.llm_research_timeout_sec
        # 同じpromptを内部で即再試行すると、調査loopの共有予算を見えない形で消費する。
        # ここは1回だけ呼び、形式エラーはloopの次ターンで明示的に自己修正させる。
        (
            raw_text,
            latency_ms,
            input_tokens,
            output_tokens,
            stop_reason,
        ) = self._json_transport(
            prompt,
            schema,
            settings.llm_research_stage_model,
            settings.llm_research_max_tokens,
            timeout,
            effort=settings.llm_research_stage_effort,
        )
        turn, validation_error = parse_research_turn(
            raw_text,
            max_actions=max_actions,
            max_selected_evidence=settings.llm_research_max_selected_evidence,
        )
        return ResearchTurnResult(
            turn=turn,
            provider=self.provider,
            model=settings.llm_research_stage_model,
            latencyMs=latency_ms,
            inputTokens=input_tokens,
            outputTokens=output_tokens,
            validationError=validation_error,
            stopReason=stop_reason,
            retryCount=0,
            promptContentUnitIds=allowed_content_unit_ids,
        )

    def integrate_legal_research_cycle(
        self,
        request: AnswerRequest,
        catalog: EvidenceCatalog,
        checkpoint: ResearchCheckpoint,
        *,
        cycle_index: int,
        cycle_count: int,
        cycle_new_content_ids: tuple[str, ...] | list[str],
        tool_history: list[dict[str, Any]] | None,
        timeout_sec: int,
        case_context: dict[str, Any] | None = None,
    ) -> ResearchCheckpointResult:
        prompt_content_unit_ids = checkpoint_integration_prompt_content_ids(
            catalog=catalog,
            checkpoint=checkpoint,
            cycle_new_content_ids=cycle_new_content_ids,
            tool_history=tool_history,
            max_selected_evidence=(settings.llm_research_max_selected_evidence),
        )
        prompt = build_research_checkpoint_prompt(
            question=request.question,
            choices=request.choices,
            catalog=catalog,
            checkpoint=checkpoint,
            cycle_index=cycle_index,
            cycle_count=cycle_count,
            cycle_new_content_ids=cycle_new_content_ids,
            tool_history=tool_history,
            max_selected_evidence=(settings.llm_research_max_selected_evidence),
            answer_evidence_limit=request.topK,
            case_context=case_context,
        )
        schema = research_checkpoint_json_schema(
            max_selected_evidence=(settings.llm_research_max_selected_evidence),
            # 統合schemaでは同じ長いID enumがDAGの複数箇所へ反復展開される。
            # Promptに実IDを示したうえで自由文字列として出力させ、直後の
            # validate/sanitizeで既知ID完全一致を強制する。未知IDは採用しない。
            known_article_ids=(),
            known_content_unit_ids=(),
            final_cycle=cycle_index + 1 >= cycle_count,
        )
        started = perf_counter()
        (
            raw_text,
            latency_ms,
            input_tokens,
            output_tokens,
            stop_reason,
        ) = self._json_transport(
            prompt,
            schema,
            settings.llm_research_integration_model,
            settings.llm_research_integration_max_tokens,
            timeout_sec,
            effort=settings.llm_research_integration_effort,
        )
        raw_text = hydrate_relation_decision_candidates(raw_text, catalog)
        parsed, validation_error = parse_research_checkpoint(
            raw_text,
            max_selected_evidence=(settings.llm_research_max_selected_evidence),
        )
        validation_error = _research_checkpoint_contract_error(
            parsed,
            catalog,
            validation_error,
            allowed_content_unit_ids=prompt_content_unit_ids,
            required_issue_ids=tuple(
                issue.issueId
                for issue in checkpoint.logicalStructure.issues
            ),
            final_cycle=cycle_index + 1 >= cycle_count,
        )
        retry_count = 0
        retry_timeout = (
            _retry_timeout(timeout_sec, started)
            if validation_error
            else None
        )
        if retry_timeout is not None:
            retry_count = 1
            retry_max_tokens = _retry_max_tokens(
                settings.llm_research_integration_max_tokens,
                stop_reason,
            )
            (
                retry_raw_text,
                retry_latency_ms,
                retry_input_tokens,
                retry_output_tokens,
                retry_stop_reason,
            ) = self._json_transport(
                _build_contract_retry_prompt(
                    prompt,
                    validation_error,
                    role="Research Integration Agent",
                ),
                schema,
                settings.llm_research_integration_model,
                retry_max_tokens,
                retry_timeout,
                effort=settings.llm_research_integration_effort,
            )
            retry_raw_text = hydrate_relation_decision_candidates(
                retry_raw_text,
                catalog,
            )
            parsed, validation_error = parse_research_checkpoint(
                retry_raw_text,
                max_selected_evidence=(
                    settings.llm_research_max_selected_evidence
                ),
            )
            validation_error = _research_checkpoint_contract_error(
                parsed,
                catalog,
                validation_error,
                allowed_content_unit_ids=prompt_content_unit_ids,
                required_issue_ids=tuple(
                    issue.issueId
                    for issue in checkpoint.logicalStructure.issues
                ),
                final_cycle=cycle_index + 1 >= cycle_count,
            )
            latency_ms += retry_latency_ms
            input_tokens = _sum_optional(input_tokens, retry_input_tokens)
            output_tokens = _sum_optional(output_tokens, retry_output_tokens)
            stop_reason = retry_stop_reason
        return ResearchCheckpointResult(
            checkpoint=parsed,
            provider=self.provider,
            model=settings.llm_research_integration_model,
            latencyMs=latency_ms,
            inputTokens=input_tokens,
            outputTokens=output_tokens,
            validationError=validation_error,
            stopReason=stop_reason,
            retryCount=retry_count,
            promptContentUnitIds=prompt_content_unit_ids,
            finalCycle=cycle_index + 1 >= cycle_count,
        )

    def evaluate_evidence(
        self,
        request: AnswerRequest,
        citations: list[Citation],
        max_queries: int = 2,
        timeout_sec: int | None = None,
    ) -> EvidenceEvaluationResult:
        prompt = build_evidence_evaluation_prompt(request, citations, max_queries)
        schema = _evidence_evaluation_json_schema(request, max_queries)
        timeout = timeout_sec or settings.evaluator_timeout_sec
        (
            (coverage, queries, graph_required, stop, validation_error),
            latency_ms,
            input_tokens,
            output_tokens,
            stop_reason,
            retry_count,
        ) = self._call_with_retry(
            prompt,
            schema,
            settings.evaluator_model,
            settings.evaluator_max_tokens,
            timeout,
            lambda raw_text: _parse_evidence_evaluation(
                raw_text, request.choices, max_queries
            ),
        )
        return EvidenceEvaluationResult(
            choiceCoverage=coverage,
            followUpQueries=queries,
            graphRequired=graph_required,
            stop=stop,
            provider=self.provider,
            model=settings.evaluator_model,
            latencyMs=latency_ms,
            inputTokens=input_tokens,
            outputTokens=output_tokens,
            validationError=validation_error,
            stopReason=stop_reason,
            retryCount=retry_count,
        )

    def _generate_ollama(
        self,
        request: AnswerRequest,
        prompt: str,
        timeout_sec: int | None,
        citations: list[Citation],
    ) -> LLMResult:
        started = perf_counter()
        prompt_citations = _shown_citations_for_prompt(citations)
        response = requests.post(
            f"{settings.ollama_base_url.rstrip('/')}/api/generate",
            json={
                "model": settings.answer_model,
                "prompt": prompt,
                "format": _answer_json_schema(request, prompt_citations),
                "stream": False,
                "options": {
                    "temperature": 0,
                    "top_p": 1,
                },
            },
            timeout=timeout_sec or settings.llm_timeout_sec,
        )
        response.raise_for_status()
        data = response.json()
        raw_text = str(data.get("response", "")).strip()
        (
            answer,
            predicted_answer,
            choice_judgements,
            assessments,
            polarity,
            validation_error,
        ) = _parse_answer_payload(
            raw_text,
            request.choices,
            _citation_ids(prompt_citations),
            request.topK,
        )
        (
            answer_status,
            answer_citation_ids,
            missing,
            issue_decisions,
        ) = _final_decision_fields(raw_text)
        return LLMResult(
            text=answer,
            provider="ollama",
            model=settings.answer_model,
            latencyMs=int((perf_counter() - started) * 1000),
            inputTokens=data.get("prompt_eval_count"),
            outputTokens=data.get("eval_count"),
            estimatedCost=0,
            answer=answer,
            predictedAnswer=predicted_answer,
            choiceJudgements=choice_judgements,
            validationError=validation_error,
            stopReason=data.get("done_reason"),
            outputChars=len(raw_text),
            questionPolarity=polarity,
            choiceAssessments=assessments,
            answerStatus=answer_status,
            answerCitationIds=answer_citation_ids,
            missing=missing,
            answerIssueDecisions=issue_decisions,
        )

    def _generate_anthropic(
        self,
        request: AnswerRequest,
        prompt: str,
        timeout_sec: int | None,
        citations: list[Citation],
        max_tokens: int | None = None,
    ) -> LLMResult:
        if not settings.anthropic_api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic"
            )

        started = perf_counter()
        prompt_citations = _shown_citations_for_prompt(citations)
        effective_max_tokens = max_tokens or settings.anthropic_max_tokens
        payload: dict[str, Any] = {
            "model": settings.answer_model,
            "max_tokens": effective_max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {
                "format": {
                    "type": "json_schema",
                    "schema": _to_anthropic_schema(
                        _answer_json_schema(request, prompt_citations)
                    ),
                }
            },
        }
        thinking = _anthropic_manual_thinking(
            settings.answer_model,
            effective_max_tokens,
        )
        if thinking is not None:
            payload["thinking"] = thinking
        response = _post_anthropic_with_overload_retry(
            payload=payload,
            timeout_sec=timeout_sec or settings.llm_timeout_sec,
        )
        if not response.ok:
            raise ValueError(f"{response.status_code}: {_anthropic_error(response)}")
        data = response.json()
        raw_text = _anthropic_text(data)
        (
            answer,
            predicted_answer,
            choice_judgements,
            assessments,
            polarity,
            validation_error,
        ) = _parse_answer_payload(
            raw_text,
            request.choices,
            _citation_ids(prompt_citations),
            request.topK,
        )
        (
            answer_status,
            answer_citation_ids,
            missing,
            issue_decisions,
        ) = _final_decision_fields(raw_text)
        usage = data.get("usage", {})
        return LLMResult(
            text=answer,
            provider="anthropic",
            model=settings.answer_model,
            latencyMs=int((perf_counter() - started) * 1000),
            inputTokens=usage.get("input_tokens"),
            outputTokens=usage.get("output_tokens"),
            estimatedCost=0,
            answer=answer,
            predictedAnswer=predicted_answer,
            choiceJudgements=choice_judgements,
            validationError=validation_error,
            stopReason=data.get("stop_reason"),
            contentBlockTypes=[
                str(block.get("type"))
                for block in data.get("content", [])
                if isinstance(block, dict)
            ],
            outputChars=len(raw_text),
            questionPolarity=polarity,
            choiceAssessments=assessments,
            answerStatus=answer_status,
            answerCitationIds=answer_citation_ids,
            missing=missing,
            answerIssueDecisions=issue_decisions,
        )

    def _generate_openai(
        self,
        request: AnswerRequest,
        prompt: str,
        timeout_sec: int | None,
        citations: list[Citation],
        max_tokens: int | None = None,
    ) -> LLMResult:
        started = perf_counter()
        prompt_citations = _shown_citations_for_prompt(citations)
        data = _post_openai_chat_completion(
            prompt=prompt,
            schema=_answer_json_schema(request, prompt_citations),
            model=settings.answer_model,
            max_tokens=min(
                max_tokens or settings.openai_max_tokens_ceiling,
                settings.openai_max_tokens_ceiling,
            ),
            timeout_sec=timeout_sec or settings.llm_timeout_sec,
        )
        raw_text = _openai_text(data)
        (
            answer,
            predicted_answer,
            choice_judgements,
            assessments,
            polarity,
            validation_error,
        ) = _parse_answer_payload(
            raw_text,
            request.choices,
            _citation_ids(prompt_citations),
            request.topK,
        )
        (
            answer_status,
            answer_citation_ids,
            missing,
            issue_decisions,
        ) = _final_decision_fields(raw_text)
        usage = data.get("usage", {})
        return LLMResult(
            text=answer,
            provider="openai",
            model=settings.answer_model,
            latencyMs=int((perf_counter() - started) * 1000),
            inputTokens=usage.get("prompt_tokens"),
            outputTokens=usage.get("completion_tokens"),
            estimatedCost=0,
            answer=answer,
            predictedAnswer=predicted_answer,
            choiceJudgements=choice_judgements,
            validationError=validation_error,
            stopReason=_openai_finish_reason(data),
            contentBlockTypes=["text"] if raw_text else [],
            outputChars=len(raw_text),
            questionPolarity=polarity,
            choiceAssessments=assessments,
            answerStatus=answer_status,
            answerCitationIds=answer_citation_ids,
            missing=missing,
            answerIssueDecisions=issue_decisions,
        )

    def _json_transport(
        self,
        prompt: str,
        schema: dict[str, Any],
        model: str,
        max_tokens: int,
        timeout_sec: int,
        effort: str | None = None,
    ) -> tuple[str, int, int | None, int | None, str | None]:
        """provider共通のJSON生成トランスポート。(raw_text, latencyMs, inTokens, outTokens, stopReason)を返す。"""
        if self.provider == "ollama":
            return self._ollama_json(prompt, schema, model, timeout_sec)
        if self.provider == "anthropic":
            return self._anthropic_json(
                prompt,
                schema,
                model,
                max_tokens,
                timeout_sec,
                effort=effort,
            )
        if self.provider == "openai":
            return self._openai_json(
                prompt,
                schema,
                model,
                max_tokens,
                timeout_sec,
            )
        raise ValueError(f"Unsupported LLM_PROVIDER: {self.provider}")

    def _ollama_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        model: str,
        timeout_sec: int,
    ) -> tuple[str, int, int | None, int | None, str | None]:
        started = perf_counter()
        options: dict[str, Any] = {"temperature": 0, "top_p": 1}
        if self.ollama_num_ctx is not None:
            options["num_ctx"] = self.ollama_num_ctx
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "format": schema,
            "stream": False,
            "options": options,
        }
        if self.ollama_think is not None:
            payload["think"] = self.ollama_think
        response = requests.post(
            f"{settings.ollama_base_url.rstrip('/')}/api/generate",
            json=payload,
            timeout=timeout_sec,
        )
        response.raise_for_status()
        data = response.json()
        return (
            str(data.get("response", "")).strip(),
            int((perf_counter() - started) * 1000),
            data.get("prompt_eval_count"),
            data.get("eval_count"),
            data.get("done_reason"),
        )

    def _anthropic_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        model: str,
        max_tokens: int,
        timeout_sec: int,
        *,
        effort: str | None = None,
    ) -> tuple[str, int, int | None, int | None, str | None]:
        if not settings.anthropic_api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic"
            )
        started = perf_counter()
        output_config: dict[str, Any] = {
            "format": {
                "type": "json_schema",
                "schema": _to_anthropic_schema(schema),
            }
        }
        if effort and _anthropic_model_supports_effort(model):
            output_config["effort"] = effort
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": output_config,
        }
        thinking = _anthropic_manual_thinking(model, max_tokens)
        if thinking is not None:
            payload["thinking"] = thinking
        response = _post_anthropic_with_overload_retry(
            payload=payload,
            timeout_sec=timeout_sec,
        )
        if not response.ok:
            raise ValueError(f"{response.status_code}: {_anthropic_error(response)}")
        data = response.json()
        usage = data.get("usage", {})
        return (
            _anthropic_text(data),
            int((perf_counter() - started) * 1000),
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            data.get("stop_reason"),
        )

    def _openai_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        model: str,
        max_tokens: int,
        timeout_sec: int,
    ) -> tuple[str, int, int | None, int | None, str | None]:
        started = perf_counter()
        data = _post_openai_chat_completion(
            prompt=prompt,
            schema=schema,
            model=model,
            max_tokens=min(max_tokens, settings.openai_max_tokens_ceiling),
            timeout_sec=timeout_sec,
        )
        usage = data.get("usage", {})
        return (
            _openai_text(data),
            int((perf_counter() - started) * 1000),
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            _openai_finish_reason(data),
        )


def _post_anthropic_with_overload_retry(
    *,
    payload: dict[str, Any],
    timeout_sec: int,
) -> requests.Response:
    """Anthropicの一時的な529だけを、元の時間予算内で指数再試行する。"""
    started = perf_counter()
    response: requests.Response | None = None
    backoff_seconds = (2.0, 4.0, 8.0)
    for attempt in range(len(backoff_seconds) + 1):
        remaining = timeout_sec - (perf_counter() - started)
        if remaining <= 0.5:
            break
        response = requests.post(
            f"{settings.anthropic_base_url.rstrip('/')}/v1/messages",
            headers={
                "content-type": "application/json",
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": settings.anthropic_version,
            },
            json=payload,
            timeout=max(0.5, remaining),
        )
        if response.status_code != 529 or attempt == len(backoff_seconds):
            return response
        # 529は入力不正ではなくprovider過負荷である。長い待機で共有deadlineを
        # 食い潰さず、短いbackoff後に同一payloadを一度だけ再試行する。
        remaining = timeout_sec - (perf_counter() - started)
        backoff = backoff_seconds[attempt]
        if remaining <= backoff + 0.5:
            return response
        sleep(backoff)
    if response is not None:
        return response
    raise requests.Timeout("Anthropic request budget exhausted before retry")


def _openai_headers() -> dict[str, str]:
    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
    return {
        "authorization": f"Bearer {settings.openai_api_key}",
        "content-type": "application/json",
    }


def _post_openai_chat_completion(
    *,
    prompt: str,
    schema: dict[str, Any],
    model: str,
    max_tokens: int,
    timeout_sec: int,
) -> dict[str, Any]:
    """OpenAI Chat CompletionsのStructured Outputsを共通JSON契約へ接続する。"""

    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_completion_tokens": max_tokens,
        "store": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "legal_agent_response",
                "strict": True,
                "schema": _to_openai_schema(schema),
            },
        },
    }
    # GPT-5系はtemperature=0を受け付けない。指定しなければProviderの
    # 対応値が使われるため、非対応パラメータを輸送層から除く。
    if model.lower().startswith("gpt-5"):
        if settings.openai_reasoning_effort:
            payload["reasoning_effort"] = settings.openai_reasoning_effort
    else:
        payload["temperature"] = 0

    response = requests.post(
        f"{settings.openai_base_url.rstrip('/')}/chat/completions",
        headers=_openai_headers(),
        json=payload,
        timeout=timeout_sec,
    )
    if not response.ok:
        raise ValueError(f"{response.status_code}: {_openai_error(response)}")
    data = response.json()
    if not isinstance(data, dict):
        raise TypeError("OpenAI response root is not an object")
    return data


def _to_openai_schema(schema: Any) -> Any:
    """内部JSON SchemaをOpenAIのstrict Structured Outputs向けに正規化する。

    strict modeではobjectの全propertyをrequiredにし、未知propertyを禁止する必要がある。
    Pydanticが付けるdefaultはStructured Outputsで不要なため除去する。内部契約による
    Pydantic検証は応答後にも実施するので、provider変換に意味判断は持たせない。
    """

    if isinstance(schema, list):
        return [_to_openai_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    source = {key: value for key, value in schema.items() if key != "default"}
    type_value = source.get("type")
    if isinstance(type_value, list):
        shared = {key: value for key, value in source.items() if key != "type"}
        branches = []
        for type_name in type_value:
            if type_name == "null":
                branches.append({"type": "null"})
                continue
            branch = {**shared, "type": type_name}
            if "enum" in branch:
                allowed = [
                    value
                    for value in branch["enum"]
                    if _enum_value_matches_type(value, type_name)
                ]
                if allowed:
                    branch["enum"] = allowed
                else:
                    branch.pop("enum")
            if type_name != "object":
                for object_key in ("properties", "required", "additionalProperties"):
                    branch.pop(object_key, None)
            branches.append(_to_openai_schema(branch))
        return {"anyOf": branches}

    converted = {
        key: _to_openai_schema(value) for key, value in source.items()
    }
    if converted.get("type") == "object":
        properties = converted.get("properties")
        if isinstance(properties, dict):
            converted["required"] = list(properties)
        converted["additionalProperties"] = False
    return converted


def _scoped_research_article_ids(
    catalog: EvidenceCatalog,
    *,
    checkpoint: ResearchCheckpoint | None,
    preferred_content_ids: tuple[str, ...] | list[str],
    case_context: dict[str, Any] | None,
) -> tuple[str, ...]:
    """今回のPromptに現れるArticleだけを構造化出力の許可集合にする。"""
    article_ids: list[str] = []
    article_ids.extend(
        str(item.get("articleId") or "")
        for item in catalog.items_by_ids(list(preferred_content_ids))
    )
    if checkpoint is not None:
        article_ids.extend(checkpoint.nextArticleIds)
        article_ids.extend(
            node.articleId or ""
            for issue in checkpoint.logicalStructure.issues
            for node in issue.authorityNodes
        )
        article_ids.extend(
            str(item.get("articleId") or "")
            for item in catalog.items_by_ids(
                [*checkpoint.evidenceIds, *checkpoint.openEvidenceIds]
            )
        )
    article_ids.extend(
        str(article_id or "")
        for article_id in (case_context or {}).get("allowedArticleIds") or []
    )
    known = set(catalog.known_article_ids)
    return tuple(
        article_id
        for article_id in dict.fromkeys(article_ids)
        if article_id and article_id in known
    )


def _scoped_research_content_ids(
    catalog: EvidenceCatalog,
    *,
    checkpoint: ResearchCheckpoint | None,
    preferred_content_ids: tuple[str, ...] | list[str],
    max_items: int,
) -> tuple[str, ...]:
    content_ids = list(preferred_content_ids)
    if checkpoint is not None:
        content_ids.extend(checkpoint.evidenceIds)
        content_ids.extend(checkpoint.openEvidenceIds)
    known = set(catalog.content_unit_ids)
    return tuple(
        content_unit_id
        for content_unit_id in dict.fromkeys(content_ids)
        if content_unit_id in known
    )[: max(0, max_items)]


def _parse_issue_plan(raw_text: str, question: str) -> tuple[IssuePlan, str | None]:
    try:
        plan = parse_issue_plan(raw_text, question=question)
    except Exception as exc:  # プランナー障害で回答経路を落とさない
        plan = fallback_issue_plan(
            question, reason=f"issue_plan_error: {type(exc).__name__}"
        )
    return plan, plan.validation_error


def build_answer_prompt(
    request: AnswerRequest,
    route: list[str],
    citations: list[Citation],
    evidence_by_choice: dict[str, list[str]] | None = None,
    *,
    answer_scope: dict[str, Any] | None = None,
    research_context: dict[str, Any] | None = None,
    review_feedback: list[str] | None = None,
    review_verdict: str | None = None,
    previous_answer: str | None = None,
    previous_answer_status: str | None = None,
    previous_citation_ids: list[str] | None = None,
    previous_missing: list[str] | None = None,
    previous_issue_decisions: list[dict[str, Any]] | None = None,
    review_findings: list[dict[str, str]] | None = None,
) -> str:
    citation_block, citation_manifest = _format_citations_with_stats(
        citations,
        settings.llm_max_context_chars,
        max_items=settings.llm_finalization_material_max_items,
    )

    choices_block = ""
    if request.choices:
        choices_block = "\n選択肢:\n" + "\n".join(
            f"{label.upper()}: {text}"
            for label, text in sorted(request.choices.items())
        )
    choice_commitment_rule = (
        "まず設問が、条文に適合する選択肢を選ぶ select_entailed か、誤り・非該当を選ぶ select_contradicted かを判定してください。"
        " 次に各選択肢の記述を独立に検証し、entailed、contradicted、insufficient のいずれかを設定してください。"
        " 各判定には、実際に根拠とした引用候補のcontentUnitIdだけをcitationIdsへ設定し、短い理由と0から1のconfidenceを付けてください。"
        " あなたが最終判断主体としてquestionPolarityと各評価を統合し、最終選択肢をpredictedAnswerへ設定してください。"
        f" predictedAnswerに対応するcitationIdsは最大{min(3, request.topK)}件としてください。"
        " answer本文にはcontentUnitIdを書かず、根拠の選択は構造化citationIdsだけで返してください。"
        " プログラムはpredictedAnswerを選び直さず、既知ラベルと構造上の整合性だけを検証します。"
        " 選択式ではanswerStatus、citationIds、missing、issueDecisionsをnullにしてください。"
        " answerにはpredictedAnswerの根拠を簡潔に説明してください。"
        " 根拠不足はinsufficientとし、answer内で不確実性を明記してください。"
        if request.choices
        else (
            "選択肢がない場合、あなたがMain Agentとして最終回答、完全性、使用根拠を一体で判断してください。"
            f" citationIdsには実際に回答へ使用した既知contentUnitIdだけを最大{request.topK}件、重要な順に設定してください。"
            " answerに含める実質的な主張は、citationIdsに選んだ引用だけで直接支持できる範囲へ限定してください。"
            " 候補として表示されていてもcitationIdsに選ばない引用へ依存する説明は、回答へ入れないでください。"
            " answer本文にはcontentUnitIdを書かず、根拠の選択は構造化citationIdsだけで返してください。"
            f" 質問全体に{request.topK}件を超える根拠が必要なら、周辺的な説明を追加せず、"
            "支持できる範囲だけをpartialとして回答し、残りをmissingへ移してください。"
            " answerStatusは、質問全体を引用本文で回答できるならready、一部だけならpartial、"
            "中心的結論を回答できないならinsufficientとしてください。"
            " partialまたはinsufficientではmissingに未確認事項を具体的に設定し、answerにも回答範囲と不足を明示してください。"
            " readyではmissingを空にしてください。questionPolarity、predictedAnswer、choiceAssessmentsはnullにしてください。"
            " 共有回答契約が表示されない場合はissueDecisionsをnullにしてください。"
            " 質問で明示されていない手続、みなし規定その他の周辺事項を付け足さないでください。"
            "ただし、質問が発生条件、対象、例外、手続などを複数明示している場合は、"
            "一つを中心事項とみなして他を省略せず、各事項へ回答するかmissingへ明示してください。"
            "適用除外を一部だけ示す場合は例示と明記してください。"
        )
    )
    scope_rule = _answer_scope_rule(answer_scope)
    research_rule = _research_answer_rule(research_context)

    revision_rule = ""
    if review_feedback is not None:
        previous_decision = {
            "answer": previous_answer or "",
            "answerStatus": previous_answer_status,
            "citationIds": previous_citation_ids or [],
            "missing": previous_missing or [],
            "issueDecisions": previous_issue_decisions or [],
        }
        reviewer_decision = {
            "verdict": review_verdict,
            "findings": review_findings or [
                {"issueId": "overall", "description": issue}
                for issue in review_feedback
            ],
        }
        revision_rule = (
            "\nこれはReviewerの批評を受けた再判断です。Reviewerは回答主体ではありません。"
            "各指摘を引用本文と質問に照らして検討し、妥当な指摘はすべて解消してください。"
            "同じ引用で訂正できない事項は推測せず、answerから除くかanswerStatusをpartialまたは"
            "insufficientへ変更してmissingへ移してください。妥当でない指摘は機械的に反映せず、"
            "あなた自身が回答本文、完全性、根拠選択を一体で再判断してください。"
            "前回の構造化判断を部分修正するだけでなく、変更後のanswer、answerStatus、citationIds、"
            "missing、issueDecisionsが相互に整合する完全なJSONを返してください。"
            f"\n前回のMain Agent判断: {json.dumps(previous_decision, ensure_ascii=False)}"
            f"\nReviewer判断: {json.dumps(reviewer_decision, ensure_ascii=False)}"
        )

    return f"""あなたはローカル検証環境の法務RAG Main Agentです。
外部APIや外部検索は使わず、法的結論は下の引用候補だけに基づいて日本語で簡潔に回答してください。
一般知識を、引用候補にない要件・例外・結論の補完に使わないでください。
法的判断を断定しすぎず、必要に応じて専門家確認が必要であることが伝わる表現にしてください。
answer本文には内部識別子であるcontentUnitIdを書かないでください。
実際に使った根拠は構造化citationIdsだけで選び、本文とcitationIdsを合わせて一つの判断として返してください。
引用表示manifestのomittedContentUnitIdsは本文が表示されていないためcitationIdsへ選ばないでください。
truncatedContentUnitIdsは表示部分だけが確認済みです。表示されていない末尾に要件・例外が無い、
又は列挙が完結したとは判断せず、表示部分が直接支える主張だけに限定してください。
必ずJSONだけを返してください。JSON以外の説明文やMarkdownコードフェンスは不要です。
answer には正解ラベルだけでなく、引用候補に基づく短い根拠説明を含めてください。
{choice_commitment_rule}
{scope_rule}
{research_rule}
{revision_rule}

検索ルート: {" -> ".join(route)}
    質問: {request.question}{choices_block}

引用表示manifest（意味上の採否ではなく、表示上の切詰め情報）:
{json.dumps(citation_manifest, ensure_ascii=False)}

引用候補:
{citation_block}

JSON:"""


def build_grounding_review_prompt(
    request: AnswerRequest,
    answer: str,
    citations: list[Citation],
    *,
    research_context: dict[str, Any] | None = None,
    answer_status: str | None = None,
    citation_ids: list[str] | None = None,
    missing: list[str] | None = None,
    issue_decisions: list[dict[str, Any]] | None = None,
    available_citations: list[Citation] | None = None,
) -> str:
    citation_block, citation_manifest = _format_citations_with_stats(
        citations,
        settings.llm_max_context_chars,
        max_items=settings.llm_finalization_material_max_items,
    )
    answer_contract = _answer_contract(research_context)
    selected_ids = set(citation_ids or [])
    unselected_available = [
        citation
        for citation in (available_citations or [])
        if citation.contentUnitId not in selected_ids
    ]
    available_block, available_manifest = _format_citations_with_stats(
        unselected_available,
        settings.llm_max_context_chars,
        max_items=settings.llm_finalization_material_max_items,
    )
    return f"""あなたは法務RAG Main Agentの判断を検証するReviewerです。
回答を書き換えたり、新しい最終回答を作ったりしてはいけません。問題点を批評として返してください。
下の回答に含まれる各要件、数値、例外、適用条件、列挙を、Main Agentが選択した引用本文だけで検証してください。
ここに表示される引用はMain AgentのcitationIdsをプログラムが既知IDで展開したものです。
未選択の利用可能候補は現在の回答を支持する根拠にしてはいけません。ただしMainが引用を
選び直せば訂正できるか、追加検索が必要かを区別するためには確認してください。
一般知識や会話履歴で補わないでください。本文の「ただし」「除く」「場合」等を逆転させないでください。
最初に質問が明示して求める事項を独立に分け、各事項が回答済み、missingとして明示済み、
又は欠落のどれかを確認してください。発生条件、対象、例外、手続などが併記されている場合、
一つを中心事項として他を周辺扱いしてはいけません。回答にもmissingにも無い明示事項は欠落です。
下位法令の例外や要件を回答に使い、その適用関係が委任元・参照先に依存する場合は、
その関係を示す引用本文も確認してください。別の条項に対する例外・手続を質問対象へ
流用しないでください。質問対象への適用関係が引用本文から確認できなければ、
findingsで削除または限定が必要だと指摘してください。
引用表示manifestのtruncatedContentUnitIdsは表示部分だけが確認済みであり、未表示の末尾に
要件・例外が無い、又は列挙が完結したとは判断しないでください。omittedContentUnitIdsは
検証材料として使わないでください。
共有された各issueIdについて、引用本文から直接支持できない主張、欠落した重要論点、
誤った適用関係をfindingsへ具体的に示してください。
利用可能候補の範囲で表現・限定・引用選択を直せば解決できる場合はneeds_revisionにしてください。
必要な法令本文が利用可能候補全体に無く、同じ材料で書き直しても解決しない場合はneeds_researchとし、
不足本文を投入済みデータから探すための具体的な検索語をresearchQueriesへ最大2件設定してください。
検索語には、探す法令名・条項・要件または例外が分かる語を含めてください。
質問が完全な列挙や全要件を求めているのに引用が一部しかない場合、readyは誤りです。ただし、
Main AgentがanswerStatus=partialとし、確認済み部分だけを正確に回答して不足項目をmissingと回答本文へ
明示しているなら、「完全回答でない」という理由だけで棄却せずsupportedにしてください。
answerStatus、citationIds、missingの整合性も確認してください。citationIdsにない根拠へ依存している場合や、
readyなのに重要な不足がある場合はsupportedにしないでください。
質問の中心範囲と関係しない周辺説明の追加を要求せず、法的結論へ影響する問題を優先してください。
判定と配列の契約は次のとおりです。
- supported: 回答が正確で、明示事項が回答済み又はpartialのmissingへ適切に明示済み。findings=[]、researchQueries=[]。
- needs_revision: 利用可能候補で訂正・削除・限定・引用選択を直せば解決できる。findingsを1件以上、researchQueries=[]。
- needs_research: 必要本文が利用可能候補全体に無い。findingsとresearchQueriesを各1件以上。
- insufficient: 中心的結論も安全な部分回答も成立せず、具体的な追加検索語も示せない。findingsを1件以上、researchQueries=[]。
supported以外でfindingsを空にしてはいけません。insufficientを単なる「回答がpartialである」の意味に使ってはいけません。
findingsは重大な順に最大3件とし、各要素へ共有契約のissueIdと300文字以内のdescriptionを設定してください。
共有契約に無いissueIdを作ってはいけません。思考過程や回答の再掲は出力せず、
verdict、findings、researchQueriesだけを返してください。
必ずJSONだけを返してください。

質問:
{request.question}

Main・Reviewer共有回答契約（論点の問いと既知ID境界だけ。結論ではありません）:
{json.dumps(answer_contract, ensure_ascii=False)}

検証対象回答:
{answer}

Main Agentの構造化判断:
answerStatus: {answer_status}
citationIds: {json.dumps(citation_ids or [], ensure_ascii=False)}
missing: {json.dumps(missing or [], ensure_ascii=False)}
issueDecisions: {json.dumps(issue_decisions or [], ensure_ascii=False)}

Main Agentが選択した引用本文:
{citation_block}

引用表示manifest:
{json.dumps(citation_manifest, ensure_ascii=False)}

Mainが未選択の利用可能引用候補（現在の回答の支持根拠には使わない）:
{available_block}

未選択候補の引用表示manifest:
{json.dumps(available_manifest, ensure_ascii=False)}

JSON:"""


def _research_answer_rule(research_context: dict[str, Any] | None) -> str:
    if not research_context:
        return ""
    answer_contract = _answer_contract(research_context)
    reviewer_follow_up = research_context.get("reviewerFollowUp") or {}
    return "\n".join(
        [
            "共有回答契約は、調査LLMが分解した論点の問いと、プログラムが確認した既知ID境界だけです。",
            "調査途中の結論・仮説・verified等の状態はMainの根拠にならないため、この入力には含めていません。",
            "あなたが最終判断主体として各issueIdのquestionと引用本文を読み、法的結論を独立に判断してください。",
            "各issueIdについてstatus、conclusion、使用するcitationIds、不足するmissingをissueDecisionsへ返してください。",
            "issueDecisionsは共有契約の全issueIdを重複なく一度ずつ含め、未知のissueIdを作らないでください。",
            "各論点のcitationIdsは、その論点のconclusionを直接支える引用だけにしてください。",
            "根拠を確認できない論点は推測せずpartialまたはinsufficientとし、missingへ明示してください。",
            "引用候補に無い法令知識で結論を補完せず、根拠が不足する部分は明示してください。",
            "ガイドは法令本文と区別し、法的結論の直接根拠にしないでください。",
            "トップレベルのcitationIdsは全issueDecisionsのcitationIdsの和集合、missingはmissingの和集合にしてください。",
            "全IssueがreadyのときだけanswerStatus=readyとし、それ以外は内容に応じてpartial又はinsufficientと判断してください。",
            "Main・Reviewer共有回答契約（意味上の結論ではない）: "
            f"{json.dumps(answer_contract, ensure_ascii=False)}",
            *(
                [
                    "Reviewer指示による追加検索後の再判断です。追加取得本文を引用候補で確認し、"
                    "追加検索前の調査状態や未確認記録は履歴として扱ってください。",
                    "Reviewer追加検索: "
                    f"{json.dumps(reviewer_follow_up, ensure_ascii=False)}",
                ]
                if reviewer_follow_up
                else []
            ),
        ]
    )


def _answer_contract(
    research_context: dict[str, Any] | None,
) -> dict[str, Any]:
    contract = (research_context or {}).get("answerContract")
    return dict(contract) if isinstance(contract, dict) else {}


def _answer_contract_issue_ids(
    research_context: dict[str, Any] | None,
) -> list[str]:
    issue_ids: list[str] = []
    for issue in _answer_contract(research_context).get("issues") or []:
        if not isinstance(issue, dict):
            continue
        issue_id = str(issue.get("issueId") or "").strip()
        if issue_id and issue_id not in issue_ids:
            issue_ids.append(issue_id)
    return issue_ids


def _answer_contract_error(
    result: LLMResult,
    research_context: dict[str, Any] | None,
) -> str | None:
    """Mainの法的意味には触れず、共有Issue契約の形だけを検証する。"""

    expected_issue_ids = _answer_contract_issue_ids(research_context)
    if not expected_issue_ids:
        return None
    decisions = result.answerIssueDecisions
    if decisions is None:
        return "answer_contract_validation_error: issueDecisions is required"
    actual_issue_ids = [str(item.get("issueId") or "") for item in decisions]
    if len(actual_issue_ids) != len(set(actual_issue_ids)):
        return "answer_contract_validation_error: duplicate issueId"
    if set(actual_issue_ids) != set(expected_issue_ids):
        return (
            "answer_contract_validation_error: issueIds must match shared contract; "
            f"expected={expected_issue_ids}, actual={actual_issue_ids}"
        )

    issue_citation_ids = {
        citation_id
        for decision in decisions
        for citation_id in decision.get("citationIds") or []
    }
    if issue_citation_ids != set(result.answerCitationIds or []):
        return (
            "answer_contract_validation_error: top-level citationIds must equal "
            "the union of issueDecisions citationIds"
        )
    issue_missing = {
        str(item)
        for decision in decisions
        for item in decision.get("missing") or []
    }
    if issue_missing != set(result.missing or []):
        return (
            "answer_contract_validation_error: top-level missing must equal "
            "the union of issueDecisions missing"
        )
    statuses = {str(decision.get("status") or "") for decision in decisions}
    if result.answerStatus == "ready" and statuses != {"ready"}:
        return (
            "answer_contract_validation_error: ready answer requires every issue ready"
        )
    if result.answerStatus != "ready" and statuses == {"ready"}:
        return (
            "answer_contract_validation_error: non-ready answer cannot mark every issue ready"
        )
    return None


def _answer_scope_rule(answer_scope: dict[str, Any] | None) -> str:
    if not answer_scope:
        return ""
    status = str(answer_scope.get("answerStatus") or "")
    omitted = "、".join(answer_scope.get("omittedPrimaryIssueLabels") or [])
    out_of_scope = "、".join(answer_scope.get("outOfScopeIssueLabels") or [])
    lines = [
        f"根拠被覆状態: {status}",
        "回答コンテキストに含まれない論点を、周辺条文や一般知識から推測して補わないでください。",
    ]
    if omitted:
        lines.append(f"回答してはならない根拠不足の主論点: {omitted}")
    if out_of_scope:
        lines.append(f"今回の回答範囲外の論点: {out_of_scope}")
    if status == "partial_primary_evidence":
        lines.append("根拠が揃った主論点だけを回答し、不足部分を明示してください。")
    return "\n".join(lines)


def build_evidence_evaluation_prompt(
    request: AnswerRequest,
    citations: list[Citation],
    max_queries: int,
) -> str:
    choices_block = ""
    if request.choices:
        choices_block = "\n選択肢:\n" + "\n".join(
            f"{label.upper()}: {text}"
            for label, text in sorted(request.choices.items())
        )
    citation_block = _format_citations_with_budget(
        citations, min(settings.llm_max_context_chars, 3500)
    )
    return f"""あなたは法令検索の根拠充足度を判定するEvaluatorです。
正解の選択肢は回答せず、各選択肢を検証する根拠が十分かだけを判定してください。
定義条文が不足する場合は missing_definition、ただし書・例外が不足する場合は missing_exception、
その他の不足は missing、十分なら sufficient としてください。
不足がある場合だけfollowUpQueriesを最大{max_queries}件作ってください。
条文間参照の解決が必要ならgraphRequiredをtrueにしてください。
全選択肢の根拠が十分ならstopをtrueにしてください。必ずJSONだけを返してください。

質問: {request.question}{choices_block}

根拠候補:
{citation_block}

JSON:"""


def build_search_plan_prompt(request: AnswerRequest, max_queries: int) -> str:
    choices_block = ""
    if request.choices:
        choices_block = "\n選択肢:\n" + "\n".join(
            f"{label.upper()}: {text}"
            for label, text in sorted(request.choices.items())
        )
    return f"""あなたは日本法令コーパスを検索するクエリプランナーです。
質問を、条文本文を取得するための互いに重複しすぎない検索クエリへ分解してください。
正解の選択肢を推測するのではなく、各選択肢を検証できる検索語を作ってください。
「前条」「同項」「ただし書」「定義」「準用」など参照先の探索が必要なら graphRequired を true にしてください。
queries は最大 {max_queries} 件、各クエリは200文字以内にしてください。
必ずJSONだけを返してください。

質問: {request.question}{choices_block}

JSON:"""


def _answer_json_schema(
    request: AnswerRequest, citations: list[Citation] | None = None
) -> dict[str, Any]:
    labels = sorted(label.upper() for label in (request.choices or {}))
    citation_ids = _citation_ids(citations or [])
    citation_id_schema: dict[str, Any] = {"type": "string"}
    if citation_ids:
        citation_id_schema["enum"] = citation_ids
    assessment_schema = {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["entailed", "contradicted", "insufficient"],
            },
            "citationIds": {
                "type": "array",
                "items": citation_id_schema,
                "maxItems": min(3, request.topK),
            },
            "reason": {"type": "string", "maxLength": 300},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["verdict", "citationIds", "reason", "confidence"],
        "additionalProperties": False,
    }
    assessment_properties = {label: assessment_schema for label in labels}
    polarity_schema: dict[str, Any]
    assessments_schema: dict[str, Any]
    if labels:
        polarity_schema = {
            "type": "string",
            "enum": ["select_entailed", "select_contradicted"],
        }
        assessments_schema = {
            "type": "object",
            "properties": assessment_properties,
            "required": labels,
            "additionalProperties": False,
        }
        predicted_answer_schema: dict[str, Any] = {
            "type": "string",
            "enum": labels,
        }
        answer_status_schema: dict[str, Any] = {"type": "null"}
        final_citation_ids_schema: dict[str, Any] = {"type": "null"}
        missing_schema: dict[str, Any] = {"type": "null"}
        issue_decisions_schema: dict[str, Any] = {"type": "null"}
    else:
        polarity_schema = {"type": "null"}
        assessments_schema = {"type": "null"}
        predicted_answer_schema = {"type": "null"}
        answer_status_schema = {
            "type": "string",
            "enum": ["ready", "partial", "insufficient"],
        }
        final_citation_ids_schema = {
            "type": "array",
            "items": citation_id_schema,
            "maxItems": request.topK,
        }
        missing_schema = {
            "type": "array",
            "items": {"type": "string", "maxLength": 300},
            "maxItems": 8,
        }
        issue_decisions_schema = {
            "type": ["array", "null"],
            "items": {
                "type": "object",
                "properties": {
                    "issueId": {"type": "string", "maxLength": 80},
                    "status": {
                        "type": "string",
                        "enum": ["ready", "partial", "insufficient"],
                    },
                    "conclusion": {"type": "string", "maxLength": 600},
                    "citationIds": {
                        "type": "array",
                        "items": citation_id_schema,
                        "maxItems": min(5, request.topK),
                    },
                    "missing": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 300},
                        "maxItems": 4,
                    },
                },
                "required": [
                    "issueId",
                    "status",
                    "conclusion",
                    "citationIds",
                    "missing",
                ],
                "additionalProperties": False,
            },
            "maxItems": 4,
        }
    return {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "questionPolarity": polarity_schema,
            "predictedAnswer": predicted_answer_schema,
            "choiceAssessments": assessments_schema,
            "answerStatus": answer_status_schema,
            "citationIds": final_citation_ids_schema,
            "missing": missing_schema,
            "issueDecisions": issue_decisions_schema,
        },
        "required": [
            "answer",
            "questionPolarity",
            "predictedAnswer",
            "choiceAssessments",
            "answerStatus",
            "citationIds",
            "missing",
            "issueDecisions",
        ],
        "additionalProperties": False,
    }


def _grounding_review_json_schema(
    issue_ids: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    issue_id_schema: dict[str, Any] = {"type": "string"}
    if issue_ids:
        issue_id_schema["enum"] = list(issue_ids)
    return {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": [
                    "supported",
                    "needs_revision",
                    "needs_research",
                    "insufficient",
                ],
            },
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "issueId": issue_id_schema,
                        "description": {"type": "string", "maxLength": 300},
                    },
                    "required": ["issueId", "description"],
                    "additionalProperties": False,
                },
                "maxItems": 3,
            },
            "researchQueries": {
                "type": "array",
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                },
                "maxItems": 2,
            },
        },
        "required": ["verdict", "findings", "researchQueries"],
        "additionalProperties": False,
    }


def _search_plan_json_schema(max_queries: int) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "queries": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 200},
                "minItems": 1,
                "maxItems": max_queries,
            },
            "graphRequired": {"type": "boolean"},
        },
        "required": ["queries", "graphRequired"],
        "additionalProperties": False,
    }


def _evidence_evaluation_json_schema(
    request: AnswerRequest, max_queries: int
) -> dict[str, Any]:
    labels = sorted(label.upper() for label in (request.choices or {})) or ["overall"]
    coverage_properties = {
        label: {
            "type": "string",
            "enum": [
                "sufficient",
                "missing",
                "missing_definition",
                "missing_exception",
            ],
        }
        for label in labels
    }
    return {
        "type": "object",
        "properties": {
            "choiceCoverage": {
                "type": "object",
                "properties": coverage_properties,
                "required": labels,
                "additionalProperties": False,
            },
            "followUpQueries": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 200},
                "maxItems": max_queries,
            },
            "graphRequired": {"type": "boolean"},
            "stop": {"type": "boolean"},
        },
        "required": ["choiceCoverage", "followUpQueries", "graphRequired", "stop"],
        "additionalProperties": False,
    }


# Anthropic構造化出力が未サポートの制約キーワード。件数・文字数の上限はコード側の
# パーサー(_parse_search_plan等)が同じ値で強制しているため、除去しても安全。
_ANTHROPIC_UNSUPPORTED_KEYS = (
    "minItems",
    "maxItems",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
)


def _anthropic_model_supports_effort(model: str) -> bool:
    """Haiku系では未対応のeffortを送らない。

    capability差は呼出し側へ漏らさずAnthropic Adapter内で吸収する。
    """

    return "haiku" not in model.lower()


def _anthropic_model_supports_manual_thinking(model: str) -> bool:
    """Claude Haiku 4.5はadaptive thinkingではなく手動予算を使う。"""

    normalized = model.lower().replace("_", "-")
    return "haiku-4-5" in normalized


def _anthropic_manual_thinking(
    model: str,
    max_tokens: int,
) -> dict[str, int | str] | None:
    """本文用tokenを残せる呼出しだけmanual extended thinkingを有効にする。"""

    configured_budget = settings.anthropic_thinking_budget_tokens
    if (
        configured_budget < 1024
        or not _anthropic_model_supports_manual_thinking(model)
    ):
        return None
    budget_tokens = min(configured_budget, max_tokens - 1024)
    if budget_tokens < 1024:
        return None
    return {"type": "enabled", "budget_tokens": budget_tokens}


def _to_anthropic_schema(schema: Any) -> Any:
    """内部スキーマ(Ollama互換のunion型記法)をAnthropic構造化出力の方言へ変換する。

    Anthropicのスキーマ検証は `"type": ["string", "null"]` のようなunion型と enum の併用を
    拒否するため、union型を anyOf のブランチへ再帰的に展開する。enum値は各ブランチの型に
    一致するものだけを残す。また minItems/maxItems 等の未サポート制約は除去する。
    number の minimum/maximum も同様にAnthropic側では除去し、Pydanticで検証する。
    スキーマ定義自体は1ソース(内部表現)のまま、プロバイダ差はこの変換に閉じ込める。
    """
    if isinstance(schema, list):
        return [_to_anthropic_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    converted = {
        key: _to_anthropic_schema(value)
        for key, value in schema.items()
        if key not in _ANTHROPIC_UNSUPPORTED_KEYS
    }
    type_value = converted.get("type")
    if not isinstance(type_value, list):
        return converted

    branches = []
    for type_name in type_value:
        if type_name == "null":
            branches.append({"type": "null"})
            continue
        branch = {key: value for key, value in converted.items() if key != "type"}
        branch["type"] = type_name
        if "enum" in branch:
            allowed = [
                value
                for value in branch["enum"]
                if _enum_value_matches_type(value, type_name)
            ]
            if allowed:
                branch["enum"] = allowed
            else:
                del branch["enum"]
        if type_name != "object":
            for object_key in ("properties", "required", "additionalProperties"):
                branch.pop(object_key, None)
        branches.append(branch)
    return {"anyOf": branches}


def _enum_value_matches_type(value: Any, type_name: str) -> bool:
    if type_name == "null":
        return value is None
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return value is not None


def _retry_max_tokens(max_tokens: int, stop_reason: str | None) -> int:
    """出力上限に達して打ち切られた場合だけ、再試行の枠を広げる。

    応答にthinkingブロックが含まれると、思考だけで上限に達しtextブロックが返らない。
    同じ枠で投げ直しても同じ結果になるため、枠を倍にして本文まで到達させる。
    """
    if stop_reason != "max_tokens":
        return max_tokens
    return min(max_tokens * 2, settings.anthropic_max_tokens_ceiling)


def _build_contract_retry_prompt(
    prompt: str,
    validation_error: str,
    *,
    role: str,
) -> str:
    """意味を補正せず、LLMへ構造契約の再出力だけを要求する。"""
    role_instruction = {
        "Main Agent": (
            "answer本文へcontentUnitIdを書かず、使用根拠はcitationIdsだけで指定してください。"
        ),
        "Reviewer": (
            "判定別契約を厳守してください。supportedではfindings=[]かつresearchQueries=[]、"
            "needs_revisionではfindingsを1件以上かつresearchQueries=[]、needs_researchでは"
            "findingsとresearchQueriesを各1件以上、insufficientではfindingsを1件以上かつ"
            "researchQueries=[]です。insufficientを単なるpartialの意味に使わないでください。"
        ),
        "Research Integration Agent": (
            "articleIdには段落・項・号のcontentUnitIdを入れず、提示されたArticle単位の"
            "既知IDだけを使い、本文IDはevidenceIdsへ入れてください。前回Checkpointの"
            "Issueを省略せず、readyでは各Issueをverifiedにして、そのIssueの確認済み根拠を"
            "少なくとも1件はトップレベルのevidenceIdsへ選んでください。"
        ),
    }.get(role, "指定されたJSON schemaのフィールドだけを返してください。")
    return (
        f"{prompt}\n\n"
        f"前回の{role}出力は次の構造契約エラーで受理されませんでした:\n"
        f"{validation_error[:1000]}\n"
        "プログラムは法的判断や次動作を推測して補正しません。元の資料を読み直し、"
        "あなた自身が同じ役割として判断したうえで、指定されたJSON契約に完全に適合する"
        "出力を最初から返してください。前回出力の説明や謝罪は不要です。\n"
        f"{role_instruction}\nJSON:"
    )


def _research_checkpoint_contract_error(
    checkpoint: ResearchCheckpoint | None,
    catalog: EvidenceCatalog,
    parse_error: str | None,
    *,
    allowed_content_unit_ids: tuple[str, ...] | list[str] | None = None,
    required_issue_ids: tuple[str, ...] | list[str] = (),
    final_cycle: bool = False,
) -> str | None:
    """統合結果の形と既知ID境界だけを検証し、意味は変更しない。"""
    if parse_error:
        return parse_error
    if checkpoint is None:
        return "research_checkpoint_missing"
    validation = validate_research_checkpoint(
        checkpoint,
        catalog,
        allowed_content_unit_ids=allowed_content_unit_ids,
        required_issue_ids=required_issue_ids,
        final_cycle=final_cycle,
        require_structured_follow_up=True,
    )
    if validation.valid:
        return None
    return "research_checkpoint_validation_error: " + ", ".join(
        validation.errors
    )


def _retry_timeout(total_timeout_sec: int, started: float) -> int | None:
    remaining = int(total_timeout_sec - (perf_counter() - started))
    return remaining if remaining > 1 else None


def _sum_optional(first: int | None, second: int | None) -> int | None:
    if first is None and second is None:
        return None
    return int(first or 0) + int(second or 0)


def _strip_markdown_fence(text: str) -> str:
    """一部モデルはJSON出力指示があってもMarkdownコードフェンスで囲むため除去する。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def _parse_answer_payload(
    raw_text: str,
    choices: dict[str, str] | None,
    allowed_citation_ids: list[str] | None = None,
    max_citations: int | None = None,
) -> tuple[
    str,
    str | None,
    dict[str, str] | None,
    dict[str, dict[str, Any]] | None,
    str | None,
    str | None,
]:
    try:
        raw_payload = json.loads(_strip_markdown_fence(raw_text))
    except json.JSONDecodeError as exc:
        return raw_text, None, None, None, None, f"json_parse_error: {exc}"

    try:
        payload = LLMAnswerPayload.model_validate(raw_payload)
        _validate_choice_fields(
            payload,
            choices,
            set(allowed_citation_ids or []),
            max_citations=max_citations,
        )
    except (ValidationError, ValueError) as exc:
        return (
            _answer_from_raw_payload(raw_payload, raw_text),
            None,
            None,
            None,
            None,
            f"validation_error: {exc}",
        )

    answer_text = (
        payload.answer
        or "（回答テキストが取得できなかったため、選択肢判定のみ返します。）"
    )
    assessments = None
    if payload.choiceAssessments:
        assessments = {}
        for label, assessment in payload.choiceAssessments.items():
            normalized = assessment.model_dump()
            normalized["citationIds"] = list(
                dict.fromkeys(assessment.citationIds)
            )
            assessments[label] = normalized
    predicted_answer = (
        payload.predictedAnswer.upper() if payload.predictedAnswer else None
    )
    judgements = None
    if predicted_answer is not None and choices:
        judgements = {
            label.upper(): "supported"
            if label.upper() == predicted_answer
            else "not_supported"
            for label in choices
        }
        answer_text = f"結論: 選択肢{predicted_answer}。{answer_text}"
    return (
        answer_text,
        predicted_answer,
        judgements,
        assessments,
        payload.questionPolarity,
        None,
    )


def _parse_grounding_review(
    raw_text: str,
    expected_issue_ids: list[str] | tuple[str, ...] = (),
) -> tuple[
    str,
    list[str],
    list[dict[str, str]],
    list[str],
    str | None,
]:
    try:
        raw_payload = json.loads(_strip_markdown_fence(raw_text))
        payload = GroundingReviewPayload.model_validate(raw_payload)
    except (json.JSONDecodeError, ValidationError) as exc:
        return "insufficient", [], [], [], (
            f"grounding_review_validation_error: {type(exc).__name__}"
        )
    findings = [finding.model_dump() for finding in payload.findings]
    if not findings and payload.issues:
        fallback_issue_id = (
            expected_issue_ids[0] if expected_issue_ids else "overall"
        )
        findings = [
            {"issueId": fallback_issue_id, "description": issue[:300]}
            for issue in payload.issues
        ]
    issues = [finding["description"][:300] for finding in findings]
    research_queries = [query[:200] for query in payload.researchQueries]
    finding_ids = [finding["issueId"] for finding in findings]
    if expected_issue_ids and not set(finding_ids).issubset(expected_issue_ids):
        return (
            "insufficient",
            issues,
            findings,
            [],
            "grounding_review_unknown_issue_id",
        )
    if payload.verdict != "supported" and not issues:
        return (
            "insufficient",
            [],
            [],
            [],
            "grounding_review_missing_findings",
        )
    if payload.verdict == "needs_research" and not research_queries:
        return (
            "insufficient",
            issues,
            findings,
            [],
            "grounding_review_missing_research_queries",
        )
    if payload.verdict != "needs_research" and research_queries:
        return (
            "insufficient",
            issues,
            findings,
            [],
            "grounding_review_unexpected_research_queries",
        )
    if payload.verdict == "supported" and findings:
        return (
            "insufficient",
            issues,
            findings,
            [],
            "grounding_review_unexpected_findings",
        )
    return payload.verdict, issues, findings, research_queries, None


def _parse_search_plan(
    raw_text: str, max_queries: int
) -> tuple[list[str], bool, str | None]:
    try:
        raw_payload = json.loads(_strip_markdown_fence(raw_text))
        payload = SearchPlanPayload.model_validate(raw_payload)
    except (json.JSONDecodeError, ValidationError) as exc:
        return [], False, f"search_plan_validation_error: {exc}"

    queries = []
    seen = set()
    for query in payload.queries:
        normalized = " ".join(query.split()).strip()[:200]
        if normalized and normalized not in seen:
            queries.append(normalized)
            seen.add(normalized)
        if len(queries) >= max_queries:
            break
    if not queries:
        return [], False, "search_plan_validation_error: no usable queries"
    return queries, payload.graphRequired, None


def _parse_evidence_evaluation(
    raw_text: str,
    choices: dict[str, str] | None,
    max_queries: int,
) -> tuple[dict[str, str], list[str], bool, bool, str | None]:
    try:
        raw_payload = json.loads(_strip_markdown_fence(raw_text))
        payload = EvidenceEvaluationPayload.model_validate(raw_payload)
        expected_labels = {label.upper() for label in (choices or {})} or {"overall"}
        if set(payload.choiceCoverage) != expected_labels:
            raise ValueError(
                f"choiceCoverage keys must match {sorted(expected_labels)}"
            )
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        return {}, [], False, False, f"evidence_evaluation_validation_error: {exc}"
    queries = []
    for query in payload.followUpQueries:
        normalized = " ".join(query.split()).strip()[:200]
        if normalized and normalized not in queries:
            queries.append(normalized)
        if len(queries) >= max_queries:
            break
    return (
        dict(payload.choiceCoverage),
        queries,
        payload.graphRequired,
        payload.stop,
        None,
    )


def _validate_choice_fields(
    payload: LLMAnswerPayload,
    choices: dict[str, str] | None,
    allowed_citation_ids: set[str],
    *,
    max_citations: int | None = None,
) -> None:
    labels = {label.upper() for label in (choices or {})}
    if not labels:
        if any(
            value is not None
            for value in (
                payload.questionPolarity,
                payload.predictedAnswer,
                payload.choiceAssessments,
            )
        ):
            raise ValueError(
                "Choice assessment fields must be null when choices are absent"
            )
        if payload.answerStatus is None or payload.citationIds is None or payload.missing is None:
            raise ValueError(
                "answerStatus, citationIds and missing are required when choices are absent"
            )
        citation_ids = list(dict.fromkeys(payload.citationIds))
        unknown = set(citation_ids) - allowed_citation_ids
        if unknown:
            raise ValueError(f"citationIds contains unknown IDs: {sorted(unknown)}")
        limit = max_citations if max_citations is not None else len(allowed_citation_ids)
        if len(citation_ids) > limit:
            raise ValueError(f"citationIds exceeds max count: {limit}")
        if payload.answerStatus == "ready" and payload.missing:
            raise ValueError("ready answer cannot contain missing items")
        if payload.answerStatus in {"partial", "insufficient"} and not payload.missing:
            raise ValueError(f"{payload.answerStatus} answer requires missing items")
        if payload.answerStatus in {"ready", "partial"} and not citation_ids:
            raise ValueError(f"{payload.answerStatus} answer requires citationIds")
        for issue in payload.issueDecisions or []:
            issue_citation_ids = list(dict.fromkeys(issue.citationIds))
            unknown = set(issue_citation_ids) - allowed_citation_ids
            if unknown:
                raise ValueError(
                    f"issueDecisions.{issue.issueId}.citationIds contains unknown IDs: "
                    f"{sorted(unknown)}"
                )
            if issue.status == "ready" and issue.missing:
                raise ValueError(
                    f"ready issue {issue.issueId} cannot contain missing items"
                )
            if issue.status in {"partial", "insufficient"} and not issue.missing:
                raise ValueError(
                    f"{issue.status} issue {issue.issueId} requires missing items"
                )
            if issue.status in {"ready", "partial"} and not issue_citation_ids:
                raise ValueError(
                    f"{issue.status} issue {issue.issueId} requires citationIds"
                )
        referenced_ids = {
            citation_id
            for citation_id in allowed_citation_ids
            if re.search(
                rf"(?<![A-Za-z0-9_-]){re.escape(citation_id)}(?![A-Za-z0-9_-])",
                payload.answer,
            )
        }
        unselected_references = referenced_ids - set(citation_ids)
        if unselected_references:
            raise ValueError(
                "answer contentUnitId references must be included in citationIds: "
                f"unselected={sorted(unselected_references)}, "
                f"citationIds={sorted(citation_ids)}"
            )
        return

    if any(
        value is not None
        for value in (
            payload.answerStatus,
            payload.citationIds,
            payload.missing,
            payload.issueDecisions,
        )
    ):
        raise ValueError(
            "Final free-text decision fields must be null when choices are present"
        )

    if payload.questionPolarity is None:
        raise ValueError("questionPolarity is required when choices are present")
    if payload.predictedAnswer is None or payload.predictedAnswer.upper() not in labels:
        raise ValueError(f"predictedAnswer must be one of {sorted(labels)}")
    if payload.choiceAssessments is None or set(payload.choiceAssessments) != labels:
        raise ValueError(f"choiceAssessments keys must match {sorted(labels)}")
    for label, assessment in payload.choiceAssessments.items():
        unknown = set(assessment.citationIds) - allowed_citation_ids
        if unknown:
            raise ValueError(
                f"choiceAssessments.{label}.citationIds contains unknown IDs: {sorted(unknown)}"
            )

    selected_assessment = payload.choiceAssessments[payload.predictedAnswer.upper()]
    expected_verdict = (
        "entailed"
        if payload.questionPolarity == "select_entailed"
        else "contradicted"
    )
    if selected_assessment.verdict != expected_verdict:
        raise ValueError(
            "predictedAnswer assessment verdict is inconsistent with questionPolarity"
        )
    selected_citation_ids = list(
        dict.fromkeys(selected_assessment.citationIds)
    )
    if max_citations is not None and len(selected_citation_ids) > min(
        3, max_citations
    ):
        raise ValueError(
            "predictedAnswer citationIds exceeds max count: "
            f"{min(3, max_citations)}"
        )
    referenced_ids = {
        citation_id
        for citation_id in allowed_citation_ids
        if re.search(
            rf"(?<![A-Za-z0-9_-]){re.escape(citation_id)}(?![A-Za-z0-9_-])",
            payload.answer,
        )
    }
    unselected_references = referenced_ids - set(selected_citation_ids)
    if unselected_references:
        raise ValueError(
            "answer contentUnitId references must be included in predictedAnswer "
            "citationIds: "
            f"unselected={sorted(unselected_references)}, "
            f"citationIds={sorted(selected_citation_ids)}"
        )


def _final_decision_fields(
    raw_text: str,
) -> tuple[
    str | None,
    list[str] | None,
    list[str] | None,
    list[dict[str, Any]] | None,
]:
    """検証済み回答JSONからMain Agentの最終判断フィールドを取り出す。"""
    try:
        payload = LLMAnswerPayload.model_validate_json(
            _strip_markdown_fence(raw_text)
        )
    except ValidationError:
        return None, None, None, None
    citation_ids = (
        list(dict.fromkeys(payload.citationIds))
        if payload.citationIds is not None
        else None
    )
    missing = (
        [item[:300] for item in payload.missing]
        if payload.missing is not None
        else None
    )
    issue_decisions = (
        [decision.model_dump() for decision in payload.issueDecisions]
        if payload.issueDecisions is not None
        else None
    )
    return payload.answerStatus, citation_ids, missing, issue_decisions


def _citation_ids(citations: list[Citation]) -> list[str]:
    return list(
        dict.fromkeys(
            citation.contentUnitId for citation in citations if citation.contentUnitId
        )
    )


def _answer_from_raw_payload(raw_payload: Any, raw_text: str) -> str:
    if isinstance(raw_payload, dict) and isinstance(raw_payload.get("answer"), str):
        return raw_payload["answer"]
    return raw_text


def _anthropic_text(data: dict[str, Any]) -> str:
    parts = []
    for block in data.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts).strip()


def _anthropic_error(response: requests.Response) -> str:
    try:
        return str(response.json().get("error", {}).get("message", ""))
    except Exception:
        return response.text[:200]


def _openai_text(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("OpenAI response has no choices")
    choice = choices[0]
    message = choice.get("message", {}) if isinstance(choice, dict) else {}
    refusal = message.get("refusal") if isinstance(message, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        text = "".join(parts).strip()
        if text:
            return text
    if refusal:
        raise ValueError("OpenAI model refused the request")
    return ""


def _openai_finish_reason(data: dict[str, Any]) -> str | None:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    choice = choices[0]
    if not isinstance(choice, dict):
        return None
    reason = choice.get("finish_reason")
    if reason == "length":
        return "max_tokens"
    return str(reason) if reason is not None else None


def _openai_error(response: requests.Response) -> str:
    try:
        message = response.json().get("error", {}).get("message", "")
        return str(message)[:500]
    except (AttributeError, TypeError, ValueError):
        return response.text[:200]


def _format_citation(index: int, citation: Citation) -> str:
    text = citation.text or ""
    # ガイドは法令本文の代替にしない。位置づけを引用ごとに明示する(§10-7)。
    lane = ""
    if citation.evidenceLane == "guidance":
        lane = f"資料区分: {citation.evidenceRole or '行政解釈・実務上の取扱い(法令本文ではない)'}\n"
    return (
        f"[{index}]\n"
        f"documentId: {citation.documentId}\n"
        f"contentUnitId: {citation.contentUnitId}\n"
        f"title: {citation.title or ''}\n"
        f"heading: {citation.heading or ''}\n"
        f"{lane}"
        f"text: {text}"
    )


def _format_citations_with_budget(citations: list[Citation], max_chars: int) -> str:
    return _format_citations_with_stats(citations, max_chars)[0]


def citation_context_stats(
    citations: list[Citation],
    max_chars: int | None = None,
) -> dict[str, Any]:
    """回答promptと同じ整形を行い、chunk切り詰め量だけを返す。"""
    return _format_citations_with_stats(
        citations,
        max_chars if max_chars is not None else settings.llm_max_context_chars,
        max_items=settings.llm_finalization_material_max_items,
    )[1]


def _format_citations_with_stats(
    citations: list[Citation],
    max_chars: int,
    *,
    max_items: int | None = None,
) -> tuple[str, dict[str, Any]]:
    if not citations:
        return (
            "引用候補なし",
            {
                "occurred": False,
                "truncatedChunkCount": 0,
                "droppedChunkCount": 0,
                "originalChars": 0,
                "includedChars": 0,
                "totalItems": 0,
                "shownItems": 0,
                "omittedItems": 0,
                "nextCursor": None,
                "complete": True,
                "shownContentUnitIds": [],
                "truncatedContentUnitIds": [],
                "omittedContentUnitIds": [],
            },
        )
    raw_blocks = [
        _format_citation(index, citation)
        for index, citation in enumerate(citations, start=1)
    ]
    projection = Projector().project_material(
        [
            MaterialItem(item_id=citation.contentUnitId or str(index), rendered=raw)
            for index, (citation, raw) in enumerate(
                zip(citations, raw_blocks, strict=True), start=1
            )
        ],
        ProjectionPolicy(
            material_max_items=max_items or len(citations),
            material_max_chars=max_chars,
        ),
    )
    manifest = projection.manifest
    truncated_count = int(manifest["truncatedItemCount"])
    dropped_count = int(manifest["omittedItems"])
    included_text = projection.text
    citation_ids = [
        citation.contentUnitId
        for citation in citations
        if citation.contentUnitId
    ]
    shown_content_unit_ids = [
        content_unit_id
        for content_unit_id in citation_ids
        if content_unit_id in included_text
    ]
    shown_set = set(shown_content_unit_ids)
    return (
        included_text,
        {
            "occurred": bool(truncated_count or dropped_count),
            "truncatedChunkCount": truncated_count,
            "droppedChunkCount": dropped_count,
            "originalChars": manifest["originalChars"],
            "includedChars": manifest["includedChars"],
            "totalItems": manifest["totalItems"],
            "shownItems": manifest["shownItems"],
            "omittedItems": manifest["omittedItems"],
            "nextCursor": manifest["nextCursor"],
            "complete": manifest["complete"],
            "shownContentUnitIds": shown_content_unit_ids,
            "truncatedContentUnitIds": [
                content_unit_id
                for content_unit_id in manifest["truncatedItemIds"]
                if content_unit_id in shown_set
            ],
            "omittedContentUnitIds": [
                content_unit_id
                for content_unit_id in citation_ids
                if content_unit_id not in shown_set
            ],
        },
    )


def _shown_citations_for_prompt(
    citations: list[Citation],
) -> list[Citation]:
    """Projectorが実際に表示した引用だけを構造化出力の候補にする。"""
    _, manifest = _format_citations_with_stats(
        citations,
        settings.llm_max_context_chars,
        max_items=settings.llm_finalization_material_max_items,
    )
    shown_ids = set(manifest["shownContentUnitIds"])
    return [
        citation
        for citation in citations
        if citation.contentUnitId in shown_ids
    ]
