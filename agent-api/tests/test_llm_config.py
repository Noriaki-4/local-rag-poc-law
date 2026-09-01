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
        "AGENT_FRAMEWORK_INTEGRATION_MODEL",
        "AGENT_FRAMEWORK_EVIDENCE_INTEGRATION_MODEL",
        "AGENT_FRAMEWORK_DEPENDENCY_ASSESSMENT_MODEL",
        "AGENT_FRAMEWORK_REVIEWER_MODEL",
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
        AGENT_FRAMEWORK_DEPENDENCY_ASSESSMENT_MODEL="claude-sonnet-5",
    )

    assert models["dependencyAssessment"] == "claude-sonnet-5"
    assert {
        model
        for role, model in models.items()
        if role != "dependencyAssessment"
    } == {"claude-haiku-4-5-20251001"}
