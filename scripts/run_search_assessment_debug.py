#!/usr/bin/env python3
"""承認済みcheckpointから検索候補の内容評価だけを実モデル診断する。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "agent-api"))

from app.agent_framework.context import SolverContext  # noqa: E402
from app.agent_framework.model_call_artifacts import (  # noqa: E402
    write_model_call_artifacts,
)
from app.domains.legal.profiles import legal_agent_profile  # noqa: E402
from app.domains.legal.search_assessment_diagnostic import (  # noqa: E402
    run_search_assessment_diagnostic,
)
from app.llm import LLMClient  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument(
        "--provider",
        required=True,
        choices=("anthropic", "openai", "ollama"),
    )
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--article-id",
        action="append",
        help="指定した既知Article候補だけを隔離する。複数回指定可。",
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--timeout-sec", type=int, default=90)
    return parser.parse_args()


def _load_context(
    path: Path,
    *,
    article_ids: list[str] | None,
) -> tuple[dict[str, Any], SolverContext]:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(fixture, dict):
        raise TypeError("fixture root must be an object")
    checkpoint = fixture.get("checkpoint")
    if not isinstance(checkpoint, dict) or checkpoint.get("approved") is not True:
        raise ValueError("checkpoint fixture must be approved before model replay")
    context = fixture.get("solverContext")
    if not isinstance(context, dict):
        raise ValueError("checkpoint fixture requires solverContext")
    solver_context = SolverContext.model_validate(context)
    if article_ids:
        requested = set(article_ids)
        known = {item.article_id for item in solver_context.search_candidates}
        unknown = requested - known
        if unknown:
            raise ValueError(f"unknown Article IDs: {sorted(unknown)}")
        solver_context = solver_context.model_copy(
            update={
                "search_candidates": tuple(
                    item
                    for item in solver_context.search_candidates
                    if item.article_id in requested
                )
            }
        )
    return fixture, solver_context


def main() -> None:
    args = _parse_args()
    fixture, context = _load_context(
        args.fixture,
        article_ids=args.article_id,
    )
    run = run_search_assessment_diagnostic(
        context,
        provider=args.provider,
        model=args.model,
        max_tokens=args.max_tokens,
        timeout_sec=args.timeout_sec,
        client=LLMClient(provider=args.provider),
    )
    profile = legal_agent_profile()
    write_model_call_artifacts(
        run.rendered,
        args.output,
        provider=args.provider,
        profile_name=profile.name,
        profile_version=profile.version,
        model=args.model,
    )
    result = run.transport_result
    payload = {
        "stage": "search_assessment",
        "fixtureId": fixture.get("fixtureId"),
        "provider": args.provider,
        "model": args.model,
        "callCount": 1,
        "repairAttempted": False,
        "inputTokens": result.inputTokens,
        "outputTokens": result.outputTokens,
        "latencyMs": result.latencyMs,
        "stopReason": result.stopReason,
        "providerRetryCount": result.retryCount,
        "validationError": run.validation_error,
        "rawOutput": result.payload,
        "normalizedOutput": (
            run.decision.model_dump(mode="json")
            if run.decision is not None
            else None
        ),
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if run.validation_error is not None:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
