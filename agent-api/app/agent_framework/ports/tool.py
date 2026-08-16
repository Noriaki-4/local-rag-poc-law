"""Toolの能力宣言と実行Port。"""

from __future__ import annotations

from typing import Protocol

from pydantic import Field

from ..state import Evidence, FrameworkModel, ToolRequest, ToolResult


class ToolDefinition(FrameworkModel):
    name: str = Field(min_length=1, max_length=160)
    read_only: bool = True
    parallel_safe: bool = True


class ToolExecution(FrameworkModel):
    result: ToolResult
    evidence: tuple[Evidence, ...] = ()


class ToolPort(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...

    def execute(
        self,
        request: ToolRequest,
        *,
        cycle_no: int,
        timeout_sec: float,
    ) -> ToolExecution: ...


class ToolRegistry:
    def __init__(self, tools: tuple[ToolPort, ...]):
        self._tools = {tool.definition.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("tool names must be unique")

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def get(self, name: str) -> ToolPort:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {name}") from exc
