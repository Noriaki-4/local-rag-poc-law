#!/usr/bin/env python3
"""承認済みSolver checkpointを、指定Provider・modelで1回だけ再生する。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "agent-api"))

from app.adapters.models.structured_json import StructuredJSONModelAdapter  # noqa: E402
from app.agent_framework.context import SolverContext  # noqa: E402
from app.agent_framework.diagnostics import AgentDiagnostics  # noqa: E402
from app.agent_framework.profiles import ModelCallProfile  # noqa: E402
from app.domains.legal.profiles import legal_agent_profile  # noqa: E402
from app.llm import LLMClient  # noqa: E402


_PROFILE_ATTRIBUTES = {
    "research": "solver_research",
    "hypothesis_generation": "solver_hypothesis_generation",
    "search_planning": "solver_search_planning",
    "integration": "solver_integration",
    "cycle_close": "solver_cycle_close",
    "finalization": "solver_finalization",
    "reviewer_revision": "solver_reviewer_revision",
    "graph_review": "solver_graph_review",
    "search_selection": "solver_search_review",
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument(
        "--provider",
        required=True,
        choices=("anthropic", "openai", "ollama"),
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--stage", choices=tuple(_PROFILE_ATTRIBUTES))
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--diagnostics-output",
        type=Path,
        help="指定時だけ中間LLM入出力をsnapshotで保存するディレクトリ。",
    )
    return parser.parse_args()


def _load_checkpoint(path: Path) -> dict[str, Any]:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(fixture, dict):
        raise TypeError("fixture root must be a JSON object")
    checkpoint = fixture.get("checkpoint")
    if not isinstance(checkpoint, dict) or checkpoint.get("approved") is not True:
        raise ValueError(
            "checkpoint fixture must be explicitly approved before model replay"
        )
    if not isinstance(fixture.get("solverContext"), dict):
        raise ValueError("checkpoint fixture requires solverContext")
    return fixture


def _resolve_stage(fixture: dict[str, Any], explicit: str | None) -> str:
    if explicit is not None:
        return explicit
    checkpoint = fixture["checkpoint"]
    stage = checkpoint.get("resumeFrom")
    if not isinstance(stage, str) or stage not in _PROFILE_ATTRIBUTES:
        raise ValueError("--stage is required for this checkpoint")
    return stage


def _resolve_profile(stage: str, model: str) -> ModelCallProfile:
    profile = getattr(legal_agent_profile(), _PROFILE_ATTRIBUTES[stage])
    if profile is None:
        raise ValueError(f"profile is unavailable for stage: {stage}")
    return profile.model_copy(update={"model": model})


def replay_checkpoint(
    fixture: dict[str, Any],
    *,
    provider: str,
    model: str,
    stage: str,
    diagnostics_output: Path | None = None,
) -> dict[str, Any]:
    context = SolverContext.model_validate(fixture["solverContext"])
    profile = _resolve_profile(stage, model)
    diagnostics = None
    if diagnostics_output is not None:
        diagnostics = AgentDiagnostics(
            mode="snapshot",
            output_dir=diagnostics_output,
            case_id=context.case_id,
            profile_name=legal_agent_profile().name,
            profile_version=legal_agent_profile().version,
        )
    client = LLMClient(provider=provider)
    adapter = (
        StructuredJSONModelAdapter(client)
        if diagnostics is None
        else StructuredJSONModelAdapter(client, diagnostics=diagnostics)
    )
    result = adapter.solve(context, profile)
    return {
        "fixtureId": fixture.get("fixtureId"),
        "stage": stage,
        "provider": provider,
        "model": model,
        "inputTokens": result.input_tokens,
        "outputTokens": result.output_tokens,
        "attemptCount": result.attempt_count,
        "decision": result.decision.model_dump(mode="json"),
    }


def main() -> None:
    args = _parse_args()
    fixture = _load_checkpoint(args.fixture)
    stage = _resolve_stage(fixture, args.stage)
    output = replay_checkpoint(
        fixture,
        provider=args.provider,
        model=args.model,
        stage=stage,
        diagnostics_output=args.diagnostics_output,
    )
    rendered = json.dumps(output, ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        print(rendered, end="")
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
