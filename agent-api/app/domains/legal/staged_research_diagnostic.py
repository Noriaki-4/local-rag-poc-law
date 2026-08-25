"""初回Researchの単一Stepだけを1回呼ぶ共通診断境界。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.adapters.models.structured_json import (
    normalize_staged_research_decision,
)
from app.agent_framework.context import SolverContext
from app.agent_framework.contracts import SolverDecision
from app.agent_framework.model_call_artifacts import RenderedModelCall


class StructuredJSONClient(Protocol):
    provider: str

    def generate_structured_json(
        self,
        *,
        prompt: str,
        schema: dict[str, Any],
        model: str,
        max_tokens: int,
        timeout_sec: int,
    ) -> Any: ...


@dataclass(frozen=True)
class StagedResearchDiagnosticRun:
    rendered: RenderedModelCall
    transport_result: Any
    decision: SolverDecision | None
    validation_error: str | None


def run_staged_research_diagnostic(
    rendered: RenderedModelCall,
    context: SolverContext,
    *,
    projection: str,
    model: str,
    max_tokens: int,
    timeout_sec: int,
    client: StructuredJSONClient,
) -> StagedResearchDiagnosticRun:
    """修復や後続Stepを起動せず、指定Stepの初回応答だけを観測する。"""

    result = client.generate_structured_json(
        prompt=rendered.request,
        schema=rendered.output_schema,
        model=model,
        max_tokens=max_tokens,
        timeout_sec=timeout_sec,
    )
    if result.validationError is not None or result.payload is None:
        return StagedResearchDiagnosticRun(
            rendered=rendered,
            transport_result=result,
            decision=None,
            validation_error=(result.validationError or "empty model output"),
        )
    try:
        decision = normalize_staged_research_decision(
            result.payload,
            projection=projection,
            context=context,
        )
    except (TypeError, ValueError) as exc:
        return StagedResearchDiagnosticRun(
            rendered=rendered,
            transport_result=result,
            decision=None,
            validation_error=str(exc),
        )
    return StagedResearchDiagnosticRun(
        rendered=rendered,
        transport_result=result,
        decision=decision,
        validation_error=None,
    )
