"""本番の検索候補内容評価Promptだけを1回呼ぶ実モデル診断。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.adapters.models.structured_json import (
    _normalize_search_assessment_transport_payload,
    _validate_search_assessment_payload,
    render_search_assessment_model_call,
)
from app.agent_framework.context import SolverContext
from app.agent_framework.contracts import SearchAssessmentDecision
from app.agent_framework.model_call_artifacts import RenderedModelCall
from app.domains.legal.profiles import legal_agent_profile
from app.domains.legal.staged_research_diagnostic import StructuredJSONClient


_PROMPT_DIR = Path(__file__).with_name("prompts")


@dataclass(frozen=True)
class SearchAssessmentDiagnosticRun:
    rendered: RenderedModelCall
    transport_result: Any
    decision: SearchAssessmentDecision | None
    validation_error: str | None


def render_search_assessment_call(
    context: SolverContext,
    *,
    provider: str,
    model: str,
) -> RenderedModelCall:
    """本番Profileの内容評価Prompt・入力・schemaを完成形にする。"""

    profile = legal_agent_profile().solver_search_review
    if profile is None:
        raise ValueError("search review profile is unavailable")
    profile = profile.model_copy(
        update={
            "model": model,
            "system_prompt": (
                _PROMPT_DIR / "solver_search_review.md"
            ).read_text(encoding="utf-8").strip(),
            "completion_check_prompt": (
                _PROMPT_DIR / "solver_search_review_check.md"
            ).read_text(encoding="utf-8").strip(),
        }
    )
    return render_search_assessment_model_call(
        context,
        profile,
        provider=provider,
    )


def run_search_assessment_diagnostic(
    context: SolverContext,
    *,
    provider: str,
    model: str,
    max_tokens: int,
    timeout_sec: int,
    client: StructuredJSONClient,
) -> SearchAssessmentDiagnosticRun:
    """主体分類・候補選択・修復を起動せず、内容評価を1回だけ観測する。"""

    rendered = render_search_assessment_call(
        context,
        provider=provider,
        model=model,
    )
    result = client.generate_structured_json(
        prompt=rendered.request,
        schema=rendered.output_schema,
        model=model,
        max_tokens=max_tokens,
        timeout_sec=timeout_sec,
    )
    if result.validationError is not None or result.payload is None:
        return SearchAssessmentDiagnosticRun(
            rendered=rendered,
            transport_result=result,
            decision=None,
            validation_error=(result.validationError or "empty model output"),
        )
    try:
        normalized = _normalize_search_assessment_transport_payload(
            result.payload,
            context,
        )
        _validate_search_assessment_payload(normalized, context)
        decision = SearchAssessmentDecision.model_validate(normalized)
    except (TypeError, ValueError, ValidationError) as exc:
        return SearchAssessmentDiagnosticRun(
            rendered=rendered,
            transport_result=result,
            decision=None,
            validation_error=str(exc),
        )
    return SearchAssessmentDiagnosticRun(
        rendered=rendered,
        transport_result=result,
        decision=decision,
        validation_error=None,
    )
