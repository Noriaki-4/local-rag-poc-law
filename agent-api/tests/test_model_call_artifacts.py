from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.adapters.models.structured_json import render_solver_model_call
from app.agent_framework.context import build_solver_context
from app.agent_framework.context import SolverContext
from app.agent_framework.model_call_artifacts import (
    model_call_artifact_contents,
    write_model_call_artifacts,
)
from app.agent_framework.profiles import AgentLimits, ModelCallProfile
from app.agent_framework.state import CaseState
from app.domains.legal.profiles import legal_agent_profile


def _context(case_id: str, question: str):
    return build_solver_context(
        CaseState(case_id=case_id, question=question),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )


def test_dynamic_input_does_not_change_solver_instructions() -> None:
    profile = ModelCallProfile(model="test-model", system_prompt="固定指示")
    first = render_solver_model_call(
        _context("case-1", "質問1"),
        profile,
        provider="openai",
        stage="research",
    )
    second = render_solver_model_call(
        _context("case-2", "質問2"),
        profile,
        provider="openai",
        stage="research",
    )

    assert first.instructions == second.instructions
    assert first.instructions_hash == second.instructions_hash
    assert first.input_payload != second.input_payload
    assert first.input_hash != second.input_hash
    assert first.request != second.request
    assert "質問1" not in first.instructions
    assert "質問1" in first.request


def test_provider_transport_is_explicit_in_artifacts(tmp_path) -> None:
    context = _context("case-1", "質問")
    profile = ModelCallProfile(model="test-model", system_prompt="固定指示")
    openai = render_solver_model_call(
        context,
        profile,
        provider="openai",
        stage="research",
    )
    anthropic = render_solver_model_call(
        context,
        profile,
        provider="anthropic",
        stage="research",
    )

    assert openai.output_schema != anthropic.output_schema
    assert "transport_values" not in openai.input_payload
    assert "transport_values" in anthropic.input_payload
    assert openai.normalized_schema == anthropic.normalized_schema

    paths = write_model_call_artifacts(
        openai,
        tmp_path,
        provider="openai",
        profile_name="legal-default",
        profile_version="test",
        model=profile.model,
    )
    assert {path.name for path in paths} == {
        "instructions.md",
        "input.json",
        "output_schema.json",
        "normalized_schema.json",
        "request.txt",
        "manifest.json",
    }
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["stage"] == "research"
    assert manifest["instructionsHash"] == openai.instructions_hash
    assert manifest["inputHash"] == openai.input_hash
    assert manifest["outputSchemaHash"] == openai.output_schema_hash
    assert manifest["normalizedSchemaHash"] == openai.normalized_schema_hash
    assert manifest["requestHash"] == openai.request_hash


@pytest.mark.parametrize("provider", ["openai", "anthropic", "ollama"])
def test_research_baseline_artifacts_are_current(provider: str) -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures/framework/tob_overview_initial_research_decomposition_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    agent_profile = legal_agent_profile()
    profile = agent_profile.solver_research.model_copy(
        update={"model": fixture["source"]["model"]}
    )
    rendered = render_solver_model_call(
        context,
        profile,
        provider=provider,
        stage="research",
    )
    expected = model_call_artifact_contents(
        rendered,
        provider=provider,
        profile_name=agent_profile.name,
        profile_version=agent_profile.version,
        model=profile.model,
    )
    artifact_dir = (
        Path(__file__).parent
        / f"fixtures/model_call_artifacts/legal-research-v1/{provider}"
    )

    assert {path.name for path in artifact_dir.iterdir()} == set(expected)
    for name, content in expected.items():
        assert (artifact_dir / name).read_text(encoding="utf-8") == content
