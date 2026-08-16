"""実行判断を再現するための、本文を含まない最小trace。"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from .state import CaseState, FrameworkModel


class ModelCallTrace(FrameworkModel):
    purpose: str
    model: str
    latency_ms: int = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    attempt_count: int = Field(default=1, ge=1)
    finalize_only: bool = False


class ToolCallTrace(FrameworkModel):
    request_id: str
    tool_name: str
    arguments: dict[str, Any]
    purpose: str
    status: str
    elapsed_ms: int = Field(ge=0)
    cycle_no: int = Field(ge=1)


class RunTrace(FrameworkModel):
    reviewer_enabled: bool
    model_calls: tuple[ModelCallTrace, ...] = ()
    tool_calls: tuple[ToolCallTrace, ...] = ()
    failure_code: str | None = None


class AgentRunResult(FrameworkModel):
    state: CaseState
    trace: RunTrace
