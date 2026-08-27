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
