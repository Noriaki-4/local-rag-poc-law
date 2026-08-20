"""法令検索Domain Pack。公開factoryは循環importを避けるため遅延解決する。"""

from typing import Any

__all__ = ["legal_agent_profile", "legal_tool_registry"]


def __getattr__(name: str) -> Any:
    if name == "legal_agent_profile":
        from .profiles import legal_agent_profile

        return legal_agent_profile
    if name == "legal_tool_registry":
        from .tools import legal_tool_registry

        return legal_tool_registry
    raise AttributeError(name)
