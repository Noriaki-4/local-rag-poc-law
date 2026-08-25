from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.adapters.models.structured_json import (
    render_cycle_close_model_call,
    render_dependency_assessment_model_call,
    render_observation_integration_model_call,
    render_solver_model_call,
)
from app.adapters.tools.legal_search import LegalSearchTool
from app.agent_framework.context import build_solver_context
from app.agent_framework.context import SolverContext
from app.agent_framework.contracts import (
    ObservationIntegrationDecision,
    SolverDecision,
)
from app.agent_framework.model_call_artifacts import (
    model_call_artifact_contents,
    write_model_call_artifacts,
)
from app.agent_framework.profiles import AgentLimits, ModelCallProfile
from app.agent_framework.state import CaseState
from app.domains.legal.profiles import legal_agent_profile
from app.llm import _to_anthropic_schema


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


def test_question_decomposition_uses_only_question_and_small_contract() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_initial_research_decomposition_v1.json"
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

    assert set(rendered.input_payload) == {"question"}
    assert "remaining_fetch_capacity" not in rendered.input_payload
    assert "max_fetched_resources_per_cycle" not in rendered.input_payload
    assert "remaining_fetch_capacity" not in rendered.instructions
    assert "最初の探索" not in rendered.instructions
    assert "# 法令調査Solver：質問の要求分解" in rendered.instructions
    assert "Tool結果を受け取った後" not in rendered.instructions
    assert "共通ルール" not in rendered.instructions
    assert "Graph" not in rendered.instructions
    assert "<input_contract>" in rendered.instructions
    assert "`question`: 利用者が回答を求めている元の質問。" in (
        rendered.instructions
    )
    assert set(rendered.output_schema["properties"]) == {
        "work_items",
        "non_work_item_requirements",
    }
    assert "行為者、行為、対象、限定条件" in (
        rendered.output_schema["properties"]["work_items"]["items"]
        ["properties"]["question"]["description"]
    )
    assert "重複させない" in (
        rendered.output_schema["properties"]["non_work_item_requirements"]
        ["description"]
    )
    assert "graph_candidate_review" in rendered.normalized_schema["properties"]


def test_real_model_v145_fixture_documents_duplicate_checklist() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_initial_research_v145_observed_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    transport_input = fixture["observedTransportInput"]
    transport_output = fixture["observedTransportOutput"]
    payload = transport_output["payload"]
    decision = SolverDecision.model_validate(fixture["observedSolverDecision"])

    assert fixture["source"]["model"] == "gpt-4o-mini"
    assert fixture["source"]["profileVersion"] == "145"
    assert context.case_id == transport_input["inputPayload"]["case_id"]
    assert "最初の探索行動" in transport_input["instructions"]
    assert "remaining_fetch_capacity" in transport_input["instructions"]
    assert transport_output["validationError"] is None
    assert transport_output["providerRetryCount"] == 0
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

    assert "decision_json" not in openai.output_schema["properties"]
    assert set(anthropic.output_schema["properties"]) == {"decision_json"}
    assert "transport_values" not in openai.input_payload
    assert "transport_values" not in anthropic.input_payload
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


def test_dependency_assessment_uses_small_provider_common_schema() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures/framework/tob_overview_cycle1_close_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    profile = legal_agent_profile().solver_cycle_close
    assert profile is not None

    observation = ObservationIntegrationDecision(
        decision_reason="取得本文を評価した",
    )
    rendered = [
        render_dependency_assessment_model_call(
            context,
            observation,
            profile,
        )
        for _provider in ("openai", "anthropic", "ollama")
    ]

    assert rendered[0].output_schema == rendered[1].output_schema
    assert rendered[1].output_schema == rendered[2].output_schema
    schema = rendered[0].output_schema
    assert "update_json" not in schema["properties"]
    assert "hypothesis_evidence_bindings" not in schema["properties"]
    assert "dependency_article_bindings" not in schema["properties"]
    assert "fetch_articles" not in schema["properties"]
    dependency = schema["properties"]["dependency_decisions"]
    work_item_id = dependency["items"]["properties"]["work_item_id"]
    assert work_item_id["type"] == "string"
    assert set(work_item_id["enum"]) == set(
        context.required_dependency_work_item_ids
    )
    serialized = json.dumps(schema, ensure_ascii=False)
    assert context.fetchable_article_ids[0] not in serialized
    assert len(serialized) < 8_000
    anthropic_schema = _to_anthropic_schema(schema)

    def count_any_of(value) -> int:
        if isinstance(value, dict):
            return int("anyOf" in value) + sum(
                count_any_of(item) for item in value.values()
            )
        if isinstance(value, list):
            return sum(count_any_of(item) for item in value)
        return 0

    assert count_any_of(anthropic_schema) <= 16


def test_cycle_boundary_artifacts_are_current() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures/framework/"
        "tob_overview_cycle1_three_articles_before_cycle_close_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    agent_profile = legal_agent_profile()
    profile = agent_profile.solver_cycle_close
    assert profile is not None
    profile = profile.model_copy(update={"model": fixture["source"]["model"]})
    observed = fixture["observedSolverDecision"]
    update = observed["update"]
    observation = ObservationIntegrationDecision(
        decision_reason=observed["decision_reason"],
        update_work_items=update["update_work_items"],
        update_hypotheses=update["update_hypotheses"],
    )
    observation_with_dependency = ObservationIntegrationDecision.model_validate(
        {
            **observation.model_dump(mode="json"),
            "dependency_decisions": observed["dependency_decisions"],
        }
    )
    rendered_calls = {
        "step-4-observation-integration": (
            render_observation_integration_model_call(context, profile)
        ),
        "step-5-dependency-assessment": (
            render_dependency_assessment_model_call(
                context,
                observation,
                profile,
            )
        ),
        "step-6-cycle-close": render_cycle_close_model_call(
            context,
            observation_with_dependency,
            profile,
        ),
    }

    for artifact_stage, rendered in rendered_calls.items():
        expected = model_call_artifact_contents(
            rendered,
            provider="openai",
            profile_name=agent_profile.name,
            profile_version=agent_profile.version,
            model=profile.model,
        )
        artifact_dir = (
            Path(__file__).parent
            / "fixtures/model_call_artifacts/legal-research-v1"
            / artifact_stage
            / "openai"
        )
        assert {path.name for path in artifact_dir.iterdir()} == set(expected)
        for name, content in expected.items():
            assert (artifact_dir / name).read_text(encoding="utf-8") == content


_INITIAL_RESEARCH_ARTIFACT_CASES = [
    (
        "research",
        "solver_research",
        "tob_overview_initial_research_decomposition_v1.json",
        "step-1-question-decomposition",
    ),
    (
        "hypothesis_generation",
        "solver_hypothesis_generation",
        "tob_overview_cycle1_after_search_v1.json",
        "step-2-hypothesis-generation",
    ),
    (
        "search_planning",
        "solver_search_planning",
        "tob_overview_cycle1_after_search_v1.json",
        "step-3-search-planning",
    ),
]


@pytest.mark.parametrize(
    ("stage", "profile_name", "fixture_name", "artifact_stage"),
    _INITIAL_RESEARCH_ARTIFACT_CASES,
)
def test_initial_research_artifacts_are_current(
    stage: str,
    profile_name: str,
    fixture_name: str,
    artifact_stage: str,
) -> None:
    provider = "openai"
    fixture_path = Path(__file__).parent / "fixtures/framework" / fixture_name
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    agent_profile = legal_agent_profile()
    profile = getattr(agent_profile, profile_name)
    assert profile is not None
    profile = profile.model_copy(
        update={"model": fixture["source"]["model"]}
    )
    if profile.available_tool_names is not None:
        context = context.model_copy(
            update={"available_tools": (LegalSearchTool.definition,)}
        )
    rendered = render_solver_model_call(
        context,
        profile,
        provider=provider,
        stage=stage,
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
        / "fixtures/model_call_artifacts/legal-research-v1"
        / artifact_stage
        / provider
    )

    assert {path.name for path in artifact_dir.iterdir()} == set(expected)
    for name, content in expected.items():
        assert (artifact_dir / name).read_text(encoding="utf-8") == content


@pytest.mark.parametrize("provider", ["anthropic", "ollama"])
@pytest.mark.parametrize(
    ("stage", "profile_name", "fixture_name", "_artifact_stage"),
    _INITIAL_RESEARCH_ARTIFACT_CASES,
)
def test_initial_research_provider_contracts_match_canonical(
    provider: str,
    stage: str,
    profile_name: str,
    fixture_name: str,
    _artifact_stage: str,
) -> None:
    fixture_path = Path(__file__).parent / "fixtures/framework" / fixture_name
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    profile = getattr(legal_agent_profile(), profile_name)
    assert profile is not None
    if profile.available_tool_names is not None:
        context = context.model_copy(
            update={"available_tools": (LegalSearchTool.definition,)}
        )

    canonical = render_solver_model_call(
        context,
        profile,
        provider="openai",
        stage=stage,
    )
    rendered = render_solver_model_call(
        context,
        profile,
        provider=provider,
        stage=stage,
    )

    assert rendered == canonical


def test_search_planning_artifact_explains_available_tools() -> None:
    artifact_dir = (
        Path(__file__).parent
        / "fixtures/model_call_artifacts/legal-research-v1"
        / "step-3-search-planning/openai"
    )
    instructions = (artifact_dir / "instructions.md").read_text(encoding="utf-8")
    input_payload = json.loads(
        (artifact_dir / "input.json").read_text(encoding="utf-8")
    )

    assert "`available_tools`: 現在のStepで要求できるTool一覧。" in instructions
    assert "`available_tools[].description`" in instructions
    assert "`available_tools[].input_schema`" in instructions
    assert "`available_tools[].result_description`" in instructions
    assert [item["name"] for item in input_payload["available_tools"]] == [
        "legal_search"
    ]
    assert input_payload["available_tools"][0] == (
        LegalSearchTool.definition.model_dump(mode="json")
    )
