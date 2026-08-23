from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.adapters.models.structured_json import (
    _normalize_solver_payload,
    _validate_initial_research_transport_payload,
    render_solver_model_call,
)
from app.agent_framework.context import build_solver_context
from app.agent_framework.context import SolverContext
from app.agent_framework.contracts import SolverDecision
from app.agent_framework.model_call_artifacts import (
    model_call_artifact_contents,
    write_model_call_artifacts,
)
from app.agent_framework.profiles import AgentLimits, ModelCallProfile
from app.agent_framework.ports.model import ModelProtocolError
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


def test_initial_research_omits_irrelevant_execution_limits() -> None:
    context = _context("case-1", "質問")
    profile = legal_agent_profile().solver_research

    rendered = render_solver_model_call(
        context,
        profile,
        provider="openai",
        stage="research",
    )

    assert set(rendered.input_payload) == {
        "case_id",
        "question",
        "research_cycle_count",
        "remaining_research_cycles",
        "max_tool_requests_per_step",
        "work_tree",
        "hypotheses",
        "available_tools",
        "contract_feedback",
    }
    assert "remaining_fetch_capacity" not in rendered.input_payload
    assert "max_fetched_resources_per_cycle" not in rendered.input_payload
    assert set(rendered.output_schema["properties"]) == {
        "question_requirement_checklist",
        "next",
        "decision_reason",
        "start_next_cycle",
        "update",
        "next_focus_work_item_ids",
        "tool_requests",
    }
    assert "graph_candidate_review" in rendered.normalized_schema["properties"]
    checklist_schema = rendered.output_schema["properties"][
        "question_requirement_checklist"
    ]
    assert "根拠・出典・引用・出力形式・詳しさの指定は含めない" in (
        checklist_schema["description"]
    )


def test_initial_research_checklist_validates_only_structure() -> None:
    payload = {
        "question_requirement_checklist": ["確認事項A", "確認事項B"],
        "update": {"add_work_items": [{"id": "a"}, {"id": "b"}]},
    }

    _validate_initial_research_transport_payload(payload)
    assert "question_requirement_checklist" not in _normalize_solver_payload(
        payload
    )

    payload["question_requirement_checklist"] = ["確認事項A"]
    with pytest.raises(
        ModelProtocolError,
        match="must match add_work_items count",
    ):
        _validate_initial_research_transport_payload(payload)

    payload["question_requirement_checklist"] = ["確認事項A", "確認事項A"]
    with pytest.raises(ModelProtocolError, match="entries must be unique"):
        _validate_initial_research_transport_payload(payload)


def test_real_model_v145_initial_research_transport_fixture_is_reproducible() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_initial_research_v145_observed_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    profile = legal_agent_profile().solver_research
    rendered = render_solver_model_call(
        context,
        profile,
        provider="openai",
        stage="research",
    )
    transport_input = fixture["observedTransportInput"]
    transport_output = fixture["observedTransportOutput"]
    payload = transport_output["payload"]
    decision = SolverDecision.model_validate(fixture["observedSolverDecision"])

    assert fixture["source"]["model"] == "gpt-4o-mini"
    assert fixture["source"]["profileVersion"] == "145"
    assert rendered.instructions == transport_input["instructions"]
    assert rendered.input_payload == transport_input["inputPayload"]
    assert rendered.output_schema == transport_input["transportSchema"]
    assert rendered.normalized_schema == transport_input["normalizedSchema"]
    assert transport_output["validationError"] is None
    assert transport_output["providerRetryCount"] == 0
    _validate_initial_research_transport_payload(payload)

    checklist = payload["question_requirement_checklist"]
    questions = [item.question for item in decision.update.add_work_items]
    assert checklist == questions
    assert {
        "公開買付けの手続が必要になる場合",
        "対象となる株券等の範囲",
        "主な例外",
        "必要な手続",
    } <= set(questions)
    assert "根拠となる条文" in questions


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
