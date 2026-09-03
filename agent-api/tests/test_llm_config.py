import json
import os
import subprocess
import sys


def _load_models(**environment: str) -> dict[str, str]:
    env = os.environ.copy()
    for name in (
        "LLM_PROVIDER",
        "LLM_MODEL",
        "ANSWER_MODEL",
        "REVIEWER_MODEL",
        "PLANNER_MODEL",
        "EVALUATOR_MODEL",
        "LLM_RESEARCH_MODEL",
        "LLM_RESEARCH_STAGE_MODEL",
        "LLM_RESEARCH_INTEGRATION_MODEL",
        "AGENT_FRAMEWORK_RESEARCH_MODEL",
        "AGENT_FRAMEWORK_HYPOTHESIS_REVISION_MODEL",
        "AGENT_FRAMEWORK_INTEGRATION_MODEL",
        "AGENT_FRAMEWORK_EVIDENCE_INTEGRATION_MODEL",
        "AGENT_FRAMEWORK_DEPENDENCY_ASSESSMENT_MODEL",
        "AGENT_FRAMEWORK_REVIEWER_MODEL",
        "AGENT_FRAMEWORK_LOW_MODEL",
        "AGENT_FRAMEWORK_MIDDLE_MODEL",
        "AGENT_FRAMEWORK_HIGH_MODEL",
        "AGENT_FRAMEWORK_REASONING_EFFORT",
        "AGENT_FRAMEWORK_ANTHROPIC_REASONING_EFFORT",
    ):
        env.pop(name, None)
    env.update(environment)
    script = """
import json
from app.config import Settings
print(json.dumps({
    "answer": Settings.answer_model,
    "reviewer": Settings.reviewer_model,
    "planner": Settings.planner_model,
    "evaluator": Settings.evaluator_model,
    "research": Settings.agent_framework_research_model,
    "integration": Settings.agent_framework_integration_model,
    "evidenceIntegration": Settings.agent_framework_evidence_integration_model,
    "dependencyAssessment": Settings.agent_framework_dependency_assessment_model,
    "frameworkReviewer": Settings.agent_framework_reviewer_model,
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return json.loads(completed.stdout)


def _load_framework_routing(**environment: str) -> dict[str, object]:
    env = os.environ.copy()
    for name in (
        "LLM_PROVIDER",
        "LLM_MODEL",
        "AGENT_FRAMEWORK_LOW_MODEL",
        "AGENT_FRAMEWORK_MIDDLE_MODEL",
        "AGENT_FRAMEWORK_HIGH_MODEL",
        "AGENT_FRAMEWORK_REASONING_EFFORT",
        "AGENT_FRAMEWORK_ANTHROPIC_REASONING_EFFORT",
    ):
        env.pop(name, None)
    env.update(environment)
    script = """
import json
from app.config import settings
from app.domains.legal.model_routing import legal_model_routing
print(json.dumps({
    "tiers": settings.agent_framework_model_tiers,
    "reasoningEffort": settings.agent_framework_reasoning_effort,
    "routing": legal_model_routing(),
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return json.loads(completed.stdout)


def test_openai_provider_defaults_all_roles_to_gpt_5_6_luna() -> None:
    models = _load_models(LLM_PROVIDER="openai")

    assert set(models.values()) == {"gpt-5.6-luna"}


def test_llm_model_overrides_stale_role_specific_settings() -> None:
    models = _load_models(
        LLM_PROVIDER="openai",
        LLM_MODEL="gpt-4o-mini",
        ANSWER_MODEL="claude-haiku-old",
        REVIEWER_MODEL="claude-haiku-old",
        AGENT_FRAMEWORK_RESEARCH_MODEL="claude-haiku-old",
    )

    assert set(models.values()) == {"gpt-4o-mini"}


def test_evidence_integration_model_can_override_global_framework_model() -> None:
    models = _load_models(
        LLM_PROVIDER="openai",
        LLM_MODEL="gpt-5.6-luna",
        AGENT_FRAMEWORK_EVIDENCE_INTEGRATION_MODEL="gpt-5.6-terra",
    )

    assert models["evidenceIntegration"] == "gpt-5.6-terra"
    assert models["dependencyAssessment"] == "gpt-5.6-terra"
    assert {
        model
        for role, model in models.items()
        if role not in {"evidenceIntegration", "dependencyAssessment"}
    } == {"gpt-5.6-luna"}


def test_dependency_assessment_model_can_override_evidence_model() -> None:
    models = _load_models(
        LLM_PROVIDER="anthropic",
        LLM_MODEL="claude-haiku-4-5-20251001",
        AGENT_FRAMEWORK_DEPENDENCY_ASSESSMENT_MODEL="claude-sonnet-4-6",
    )

    assert models["dependencyAssessment"] == "claude-sonnet-4-6"
    assert {
        model
        for role, model in models.items()
        if role != "dependencyAssessment"
    } == {"claude-haiku-4-5-20251001"}


def test_openai_framework_level_routes_middle_only_to_selected_purposes() -> None:
    config = _load_framework_routing(
        LLM_PROVIDER="openai",
    )

    assert config["tiers"] == {
        "low": "gpt-5.6-luna",
        "middle": "gpt-5.6-terra",
        "high": "gpt-5.6-sol",
    }
    assert config["reasoningEffort"] == "high"
    routing = config["routing"]
    assert isinstance(routing, dict)
    middle_purposes = {
        purpose
        for purpose, route in routing.items()
        if isinstance(route, dict) and route["level"] == "middle"
    }
    assert middle_purposes == {
        "hypothesis_generation",
        "hypothesis_revision",
        "integration",
        "evidence_integration",
        "finalization",
    }
    assert routing["hypothesis_generation"]["model"] == "gpt-5.6-terra"
    assert routing["search_planning"]["model"] == "gpt-5.6-luna"
    assert routing["dependency_assessment"] == {
        "level": "low",
        "model": "gpt-5.6-luna",
    }


def test_anthropic_framework_level_uses_provider_tiers() -> None:
    config = _load_framework_routing(
        LLM_PROVIDER="anthropic",
    )

    routing = config["routing"]
    assert isinstance(routing, dict)
    assert config["reasoningEffort"] == "none"
    assert routing["search_planning"]["model"] == "claude-haiku-4-5-20251001"
    assert routing["integration"]["model"] == "claude-sonnet-4-6"
    assert routing["finalization"]["model"] == "claude-sonnet-4-6"
    assert routing["dependency_assessment"]["model"] == (
        "claude-haiku-4-5-20251001"
    )
    assert routing["reviewer"]["model"] == "claude-haiku-4-5-20251001"


def test_anthropic_framework_thinking_requires_provider_specific_opt_in() -> None:
    disabled = _load_framework_routing(
        LLM_PROVIDER="anthropic",
        AGENT_FRAMEWORK_REASONING_EFFORT="high",
    )
    config = _load_framework_routing(
        LLM_PROVIDER="anthropic",
        AGENT_FRAMEWORK_REASONING_EFFORT="high",
        AGENT_FRAMEWORK_ANTHROPIC_REASONING_EFFORT="medium",
    )

    assert disabled["reasoningEffort"] == "none"
    assert config["reasoningEffort"] == "medium"


def test_framework_tier_model_names_can_be_overridden() -> None:
    config = _load_framework_routing(
        LLM_PROVIDER="openai",
        AGENT_FRAMEWORK_MIDDLE_MODEL="account-specific-middle-model",
    )

    routing = config["routing"]
    assert isinstance(routing, dict)
    middle_models = {
        route["model"]
        for route in routing.values()
        if isinstance(route, dict) and route["level"] == "middle"
    }
    assert middle_models == {"account-specific-middle-model"}
