"""Toolの能力宣言と実行Port。"""

from __future__ import annotations

from typing import Protocol

from ..state import Evidence, FrameworkModel, ToolRequest, ToolResult
from ..tool_contracts import ToolDefinition


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

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(tool.definition for tool in self._tools.values())

    def get(self, name: str) -> ToolPort:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {name}") from exc
