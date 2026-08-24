#!/usr/bin/env python3
"""固定fixtureから、LLMを呼ばずにPrompt・契約成果物を生成する。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "agent-api"))

from app.adapters.models.structured_json import (  # noqa: E402
    render_cycle_close_model_call,
    render_dependency_assessment_model_call,
    render_observation_integration_model_call,
    render_reviewer_model_call,
    render_search_assessment_model_call,
    render_search_reselection_model_call,
    render_solver_model_call,
)
from app.adapters.tools.legal_search import (  # noqa: E402
    LegalFetchArticlesTool,
    LegalGraphNeighborsTool,
    LegalSearchTool,
)
from app.agent_framework.context import SolverContext  # noqa: E402
from app.agent_framework.contracts import (  # noqa: E402
    ObservationIntegrationDecision,
)
from app.agent_framework.model_call_artifacts import (  # noqa: E402
    model_call_artifact_contents,
    write_model_call_artifacts,
)
from app.agent_framework.ports.model import ReviewerView  # noqa: E402
from app.agent_framework.profiles import ModelCallProfile  # noqa: E402
from app.domains.legal.profiles import legal_agent_profile  # noqa: E402

_SOLVER_STAGES = {
    "research": "solver_research",
    "hypothesis_generation": "solver_hypothesis_generation",
    "search_planning": "solver_search_planning",
    "integration": "solver_integration",
    "observation_integration": "solver_cycle_close",
    "dependency_assessment": "solver_cycle_close",
    "cycle_close": "solver_cycle_close",
    "finalization": "solver_finalization",
    "reviewer_revision": "solver_reviewer_revision",
    "graph_review": "solver_graph_review",
}

_LEGAL_TOOL_DEFINITIONS = {
    definition.name: definition
    for definition in (
        LegalSearchTool.definition,
        LegalFetchArticlesTool.definition,
        LegalGraphNeighborsTool.definition,
    )
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument(
        "--provider",
        required=True,
        choices=("openai", "anthropic", "ollama"),
    )
    parser.add_argument(
        "--stage",
        choices=(
            *_SOLVER_STAGES,
            "search_assessment",
            "search_reselection",
            "reviewer",
        ),
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--check", action="store_true")
    return parser.parse_args()


def _load_fixture(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("fixture root must be a JSON object")
    return value


def _fixture_stage(fixture: dict[str, Any]) -> str:
    source = fixture.get("source")
    if isinstance(source, dict) and isinstance(source.get("purpose"), str):
        return str(source["purpose"])
    purpose = fixture.get("purpose")
    if isinstance(purpose, str):
        return purpose
    raise ValueError("--stage is required when fixture has no source purpose")


def _fixture_model(fixture: dict[str, Any], fallback: str) -> str:
    source = fixture.get("source")
    if isinstance(source, dict) and isinstance(source.get("model"), str):
        return str(source["model"])
    return fallback


def _resolve_profile(
    fixture: dict[str, Any],
    stage: str,
) -> tuple[str, str, ModelCallProfile]:
    agent_profile = legal_agent_profile()
    if stage == "reviewer":
        profile = agent_profile.reviewer
    elif stage in {"search_assessment", "search_reselection"}:
        profile = agent_profile.solver_search_review
    else:
        attribute = _SOLVER_STAGES.get(stage)
        if attribute is None:
            raise ValueError(f"unsupported fixture stage: {stage}")
        profile = getattr(agent_profile, attribute)
    if profile is None:
        raise ValueError(f"profile is unavailable for stage: {stage}")
    profile = profile.model_copy(
        update={"model": _fixture_model(fixture, profile.model)}
    )
    return agent_profile.name, agent_profile.version, profile


def _render(
    fixture: dict[str, Any],
    *,
    stage: str,
    provider: str,
):
    profile_name, profile_version, profile = _resolve_profile(fixture, stage)
    if stage == "reviewer":
        reviewer_value = fixture.get("reviewerView")
        if not isinstance(reviewer_value, dict):
            raise ValueError("reviewer fixture requires reviewerView")
        rendered = render_reviewer_model_call(
            ReviewerView.model_validate(reviewer_value),
            profile,
        )
        return rendered, profile_name, profile_version, profile
    context_value = fixture.get("solverContext")
    if not isinstance(context_value, dict):
        raise ValueError("fixture must contain solverContext")
    context = SolverContext.model_validate(context_value)
    if profile.available_tool_names is not None:
        fixture_definitions = {item.name: item for item in context.available_tools}
        requested_definitions = []
        for name in profile.available_tool_names:
            definition = _LEGAL_TOOL_DEFINITIONS.get(name) or fixture_definitions.get(
                name
            )
            if definition is None:
                raise ValueError(f"fixture cannot resolve Tool definition: {name}")
            requested_definitions.append(definition)
        context = context.model_copy(
            update={"available_tools": tuple(requested_definitions)}
        )
    if stage == "search_assessment":
        rendered = render_search_assessment_model_call(context, profile)
    elif stage == "search_reselection":
        assessment = fixture.get("assessmentPayload")
        if not isinstance(assessment, dict):
            raise ValueError("search_reselection fixture requires assessmentPayload")
        rendered = render_search_reselection_model_call(
            context,
            assessment,
            profile,
        )
    elif stage == "observation_integration":
        rendered = render_observation_integration_model_call(context, profile)
    elif stage == "dependency_assessment":
        observation_value = fixture.get("observationIntegration")
        if not isinstance(observation_value, dict):
            observed_decision = fixture.get("observedSolverDecision")
            if not isinstance(observed_decision, dict):
                raise ValueError(
                    "dependency_assessment fixture requires "
                    "observationIntegration or observedSolverDecision"
                )
            update = observed_decision.get("update") or {}
            observation_value = {
                "decision_reason": observed_decision.get("decision_reason")
                or "保存済みの本文評価",
                "update_work_items": update.get("update_work_items") or [],
                "update_hypotheses": update.get("update_hypotheses") or [],
            }
        rendered = render_dependency_assessment_model_call(
            context,
            ObservationIntegrationDecision.model_validate(observation_value),
            profile,
        )
    elif stage == "cycle_close":
        observation_value = fixture.get("observationIntegration")
        if not isinstance(observation_value, dict):
            observed_decision = fixture.get("observedSolverDecision")
            if not isinstance(observed_decision, dict):
                raise ValueError(
                    "cycle_close fixture requires observationIntegration or "
                    "observedSolverDecision"
                )
            update = observed_decision.get("update") or {}
            observation_value = {
                "decision_reason": observed_decision.get("decision_reason")
                or "保存済みの本文評価",
                "update_work_items": update.get("update_work_items") or [],
                "update_hypotheses": update.get("update_hypotheses") or [],
                "dependency_decisions": (
                    observed_decision.get("dependency_decisions") or []
                ),
            }
        rendered = render_cycle_close_model_call(
            context,
            ObservationIntegrationDecision.model_validate(observation_value),
            profile,
        )
    else:
        rendered = render_solver_model_call(
            context,
            profile,
            provider=provider,
            stage=stage,
        )
    return rendered, profile_name, profile_version, profile


def _check_output(output_dir: Path, expected: dict[str, str]) -> None:
    actual_names = (
        {path.name for path in output_dir.iterdir() if path.is_file()}
        if output_dir.is_dir()
        else set()
    )
    if actual_names != set(expected):
        raise SystemExit(
            "artifact files differ: "
            f"expected={sorted(expected)}, actual={sorted(actual_names)}"
        )
    changed = [
        name
        for name, content in expected.items()
        if (output_dir / name).read_text(encoding="utf-8") != content
    ]
    if changed:
        raise SystemExit(f"agent model call artifacts are stale: {changed}")


def main() -> None:
    args = _parse_args()
    fixture = _load_fixture(args.fixture)
    stage = args.stage or _fixture_stage(fixture)
    rendered, profile_name, profile_version, profile = _render(
        fixture,
        stage=stage,
        provider=args.provider,
    )
    metadata = {
        "provider": args.provider,
        "profile_name": profile_name,
        "profile_version": profile_version,
        "model": profile.model,
    }
    if args.check:
        _check_output(
            args.output,
            model_call_artifact_contents(rendered, **metadata),
        )
        return
    write_model_call_artifacts(rendered, args.output, **metadata)


if __name__ == "__main__":
    main()
