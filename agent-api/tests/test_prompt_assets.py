"""外部Prompt assetとPython側の適用規則の整合性。"""

from __future__ import annotations

import pytest

from app.adapters.models.structured_json import (
    _CONTRACT_REPAIR_RULES,
    _TRANSPORT_REPAIR_RULES,
)
from app.agent_framework.prompt_assets import (
    PromptAssetError,
    prompt_asset_trace,
    prompt_sections,
    render_prompt_section,
)


def test_contract_repair_prompt_sections_match_the_rule_registry() -> None:
    registered = {section_name for _, section_name in _CONTRACT_REPAIR_RULES}
    assert set(prompt_sections("solver_contract_repair.md")) == {
        "base",
        "contract_feedback_rule",
        *registered,
    }


def test_transport_repair_prompt_sections_match_the_rule_registry() -> None:
    registered = {section_name for _, section_name in _TRANSPORT_REPAIR_RULES}
    assert set(prompt_sections("solver_transport_repair.md")) == {
        "base",
        *registered,
    }


def test_prompt_asset_requires_every_template_variable() -> None:
    with pytest.raises(PromptAssetError, match="variables are invalid"):
        render_prompt_section(
            "solver_transport_repair.md",
            "base",
            {"base_prompt": "base"},
        )


def test_prompt_asset_rejects_an_unknown_section() -> None:
    with pytest.raises(PromptAssetError, match="section is unavailable"):
        render_prompt_section(
            "solver_transport_repair.md",
            "unknown",
        )


def test_prompt_asset_trace_identifies_selected_sections_without_their_body() -> None:
    trace = prompt_asset_trace(
        "solver_transport_repair.md",
        ("base", "continue_requires_action", "base"),
    )

    assert trace["asset"] == ("agent_framework/prompts/solver_transport_repair.md")
    assert len(trace["sha256"]) == 64
    assert [item["name"] for item in trace["sections"]] == [
        "base",
        "continue_requires_action",
    ]
    assert all(len(item["sha256"]) == 64 for item in trace["sections"])
    assert "次の形式違反" not in str(trace)
