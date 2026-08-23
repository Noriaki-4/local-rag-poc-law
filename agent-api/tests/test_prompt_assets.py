"""外部Prompt assetとPython側の適用規則の整合性。"""

from __future__ import annotations

import pytest

from app.adapters.models.structured_json import (
    _CONTRACT_REPAIR_SECTIONS,
    _TRANSPORT_REPAIR_SECTIONS,
)
from app.agent_framework.prompt_assets import (
    PromptAssetError,
    prompt_asset_trace,
    prompt_sections,
    render_prompt_section,
)


def test_contract_repair_prompt_sections_match_the_rule_registry() -> None:
    assert set(prompt_sections("solver_contract_repair.md")) == {
        "contract_feedback_rule",
        *_CONTRACT_REPAIR_SECTIONS,
    }


def test_transport_repair_prompt_sections_match_the_rule_registry() -> None:
    assert set(prompt_sections("solver_transport_repair.md")) == {
        "stable",
        *_TRANSPORT_REPAIR_SECTIONS,
    }


def test_repair_prompt_assets_do_not_embed_dynamic_values() -> None:
    for asset in ("solver_contract_repair.md", "solver_transport_repair.md"):
        assert all("$" not in section for section in prompt_sections(asset).values())


def test_prompt_asset_rejects_an_unknown_section() -> None:
    with pytest.raises(PromptAssetError, match="section is unavailable"):
        render_prompt_section(
            "solver_transport_repair.md",
            "unknown",
        )


def test_prompt_asset_trace_identifies_selected_sections_without_their_body() -> None:
    trace = prompt_asset_trace(
        "solver_transport_repair.md",
        ("stable", "continue_requires_action", "stable"),
    )

    assert trace["asset"] == ("agent_framework/prompts/solver_transport_repair.md")
    assert len(trace["sha256"]) == 64
    assert [item["name"] for item in trace["sections"]] == [
        "stable",
        "continue_requires_action",
    ]
    assert all(len(item["sha256"]) == 64 for item in trace["sections"])
    assert "次の形式違反" not in str(trace)
