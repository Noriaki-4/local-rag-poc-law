#!/usr/bin/env python3
"""本番の質問分解Promptだけを実モデルで1回診断する。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "agent-api"))

from app.agent_framework.model_call_artifacts import (  # noqa: E402
    write_model_call_artifacts,
)
from app.domains.legal.profiles import legal_agent_profile  # noqa: E402
from app.domains.legal.question_decomposition_diagnostic import (  # noqa: E402
    run_question_decomposition_diagnostic,
)
from app.llm import LLMClient  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--question")
    source.add_argument("--fixture", type=Path)
    parser.add_argument(
        "--provider",
        required=True,
        choices=("anthropic", "openai", "ollama"),
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--timeout-sec", type=int, default=90)
    return parser.parse_args()


def _load_question(args: argparse.Namespace) -> tuple[str, str | None]:
    if args.question is not None:
        return args.question, None
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    if not isinstance(fixture, dict):
        raise TypeError("fixture root must be a JSON object")
    question = fixture.get("question")
    if not isinstance(question, str):
        solver_context = fixture.get("solverContext")
        if isinstance(solver_context, dict):
            question = solver_context.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("fixture requires question or solverContext.question")
    fixture_id = fixture.get("fixtureId")
    return question, fixture_id if isinstance(fixture_id, str) else None


def _result_payload(
    run: Any,
    *,
    question: str,
    fixture_id: str | None,
    provider: str,
    model: str,
) -> dict[str, Any]:
    result = run.transport_result
    decision = run.decision
    return {
        "fixtureId": fixture_id,
        "stage": "question_decomposition",
        "question": question,
        "provider": provider,
        "model": model,
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
            {
                "workItems": [
                    item.model_dump(mode="json")
                    for item in decision.update.add_work_items
                ],
                "nonWorkItemRequirements": list(
                    decision.update.set_non_work_item_requirements or ()
                ),
            }
            if decision is not None
            else None
        ),
    }


def main() -> None:
    args = _parse_args()
    question, fixture_id = _load_question(args)
    run = run_question_decomposition_diagnostic(
        question,
        provider=args.provider,
        model=args.model,
        max_tokens=args.max_tokens,
        timeout_sec=args.timeout_sec,
        client=LLMClient(provider=args.provider),
    )
    agent_profile = legal_agent_profile()
    write_model_call_artifacts(
        run.rendered,
        args.output,
        provider=args.provider,
        profile_name=agent_profile.name,
        profile_version=agent_profile.version,
        model=args.model,
    )
    payload = _result_payload(
        run,
        question=question,
        fixture_id=fixture_id,
        provider=args.provider,
        model=args.model,
    )
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
