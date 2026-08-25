#!/usr/bin/env python3
"""保存済みResearch出力を独立レビューで1回だけ診断する。"""

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
from app.domains.legal.research_stage_review_diagnostic import (  # noqa: E402
    render_hypothesis_review_call,
    render_question_decomposition_review_call,
    run_hypothesis_review,
    run_question_decomposition_review,
)
from app.llm import LLMClient  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument(
        "--stage",
        required=True,
        choices=("question-decomposition", "hypothesis"),
    )
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


def _load_baseline(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(fixture, dict):
        raise TypeError("fixture root must be a JSON object")
    baseline = fixture.get("baseline")
    if not isinstance(baseline, dict):
        raise ValueError("fixture requires baseline")
    return fixture, baseline


def _result_payload(
    run: Any,
    *,
    fixture: dict[str, Any],
    stage: str,
    provider: str,
    model: str,
) -> dict[str, Any]:
    result = run.transport_result
    return {
        "fixtureId": fixture.get("fixtureId"),
        "stage": stage,
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
            run.review.model_dump(mode="json")
            if run.review is not None
            else None
        ),
    }


def main() -> None:
    args = _parse_args()
    fixture, baseline = _load_baseline(args.fixture)
    question = fixture.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("fixture requires question")
    work_items = baseline.get("workItems")
    if not isinstance(work_items, list) or not work_items:
        raise ValueError("fixture baseline requires workItems")

    client = LLMClient(provider=args.provider)
    if args.stage == "question-decomposition":
        requirements = baseline.get("nonWorkItemRequirements", [])
        rendered = render_question_decomposition_review_call(
            question,
            work_items,
            non_work_item_requirements=requirements,
        )
        run = run_question_decomposition_review(
            rendered,
            model=args.model,
            max_tokens=args.max_tokens,
            timeout_sec=args.timeout_sec,
            client=client,
        )
    else:
        hypotheses = baseline.get("hypotheses")
        if not isinstance(hypotheses, list) or not hypotheses:
            raise ValueError("fixture baseline requires hypotheses")
        rendered = render_hypothesis_review_call(
            question,
            work_items,
            hypotheses,
        )
        run = run_hypothesis_review(
            rendered,
            model=args.model,
            max_tokens=args.max_tokens,
            timeout_sec=args.timeout_sec,
            client=client,
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
    payload = _result_payload(
        run,
        fixture=fixture,
        stage=args.stage,
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
