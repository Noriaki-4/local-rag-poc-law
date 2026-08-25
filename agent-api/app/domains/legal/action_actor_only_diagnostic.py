"""WorkItem本文と行為者だけで検索候補を選べるか確認する最小診断。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.agent_framework.model_call_artifacts import (
    RenderedModelCall,
    build_rendered_model_call,
)

_PROMPT_PATH = (
    Path(__file__).with_name("prompts")
    / "diagnostic_action_actor_only_selection.md"
)


class _DiagnosticModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ActorOnlySelection(_DiagnosticModel):
    case_id: str = Field(min_length=1)
    selected_article_ids: tuple[str, ...]
    deferred_article_ids: tuple[str, ...]
    reason: str = Field(min_length=1)


class ActorOnlySelectionOutput(_DiagnosticModel):
    cases: tuple[ActorOnlySelection, ...] = Field(min_length=1)


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
class ActorOnlyDiagnosticRun:
    rendered: RenderedModelCall
    transport_result: Any
    output: ActorOnlySelectionOutput | None
    validation_error: str | None


def render_action_actor_only_call(cases: list[dict[str, Any]]) -> RenderedModelCall:
    """対象関連主体を含めない診断入力と出力契約を組み立てる。"""

    if not cases:
        raise ValueError("at least one case is required")
    for case in cases:
        if "target_actor" in case or "actor_relation" in case:
            raise ValueError("actor-only diagnostic must not receive target fields")
    schema = ActorOnlySelectionOutput.model_json_schema()
    return build_rendered_model_call(
        stage="action_actor_only_selection_diagnostic",
        instructions=_PROMPT_PATH.read_text(encoding="utf-8").strip(),
        input_tag="selection_input",
        input_payload={"cases": cases},
        output_schema=schema,
        normalized_schema=schema,
    )


def run_action_actor_only_diagnostic(
    rendered: RenderedModelCall,
    *,
    model: str,
    max_tokens: int,
    timeout_sec: int,
    client: StructuredJSONClient,
) -> ActorOnlyDiagnosticRun:
    result = client.generate_structured_json(
        prompt=rendered.request,
        schema=rendered.output_schema,
        model=model,
        max_tokens=max_tokens,
        timeout_sec=timeout_sec,
    )
    if result.validationError is not None or result.payload is None:
        return ActorOnlyDiagnosticRun(
            rendered=rendered,
            transport_result=result,
            output=None,
            validation_error=result.validationError or "empty model output",
        )
    try:
        output = ActorOnlySelectionOutput.model_validate(result.payload)
        _validate_known_ids(rendered.input_payload["cases"], output)
    except (TypeError, ValueError) as exc:
        return ActorOnlyDiagnosticRun(
            rendered=rendered,
            transport_result=result,
            output=None,
            validation_error=str(exc),
        )
    return ActorOnlyDiagnosticRun(
        rendered=rendered,
        transport_result=result,
        output=output,
        validation_error=None,
    )


def _validate_known_ids(
    cases: list[dict[str, Any]],
    output: ActorOnlySelectionOutput,
) -> None:
    expected = {
        case["case_id"]: {
            candidate["article_id"] for candidate in case["candidates"]
        }
        for case in cases
    }
    if {item.case_id for item in output.cases} != set(expected):
        raise ValueError("output must cover every known case once")
    for item in output.cases:
        selected = set(item.selected_article_ids)
        deferred = set(item.deferred_article_ids)
        if selected & deferred:
            raise ValueError("selected and deferred Article IDs must not overlap")
        if selected | deferred != expected[item.case_id]:
            raise ValueError("output must partition every known candidate Article ID")


__all__ = [
    "ActorOnlyDiagnosticRun",
    "ActorOnlySelectionOutput",
    "render_action_actor_only_call",
    "run_action_actor_only_diagnostic",
]
