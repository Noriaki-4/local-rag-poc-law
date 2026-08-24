#!/usr/bin/env python3
"""最小Promptで法的仮説立案だけを実モデル診断する。"""

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
from app.domains.legal.minimal_hypothesis_diagnostic import (  # noqa: E402
    MinimalHypothesisOutput,
    render_minimal_hypothesis_call,
)
from app.llm import LLMClient  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument(
        "--provider",
        required=True,
        choices=("openai", "anthropic", "ollama"),
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--timeout-sec", type=int, default=90)
    return parser.parse_args()


def _load_fixture(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("fixture root must be a JSON object")
    if not isinstance(value.get("question"), str) or not value["question"].strip():
        raise ValueError("fixture requires a non-empty question")
    return value


def run_diagnostic(
    fixture: dict[str, Any],
    *,
    provider: str,
    model: str,
    max_tokens: int,
    timeout_sec: int,
) -> tuple[Any, MinimalHypothesisOutput, Any]:
    rendered = render_minimal_hypothesis_call(fixture["question"])
    result = LLMClient(provider=provider).generate_structured_json(
        prompt=rendered.request,
        schema=rendered.output_schema,
        model=model,
        max_tokens=max_tokens,
        timeout_sec=timeout_sec,
    )
    if result.payload is None:
        raise ValueError(
            "model did not return a JSON object: "
            f"validation_error={result.validationError}, stop_reason={result.stopReason}"
        )
    output = MinimalHypothesisOutput.model_validate(result.payload)
    return rendered, output, result


def main() -> None:
    args = _parse_args()
    fixture = _load_fixture(args.fixture)
    rendered, output, result = run_diagnostic(
        fixture,
        provider=args.provider,
        model=args.model,
        max_tokens=args.max_tokens,
        timeout_sec=args.timeout_sec,
    )
    write_model_call_artifacts(
        rendered,
        args.output,
        provider=args.provider,
        profile_name="minimal-legal-hypothesis-diagnostic",
        profile_version="1",
        model=args.model,
    )
    result_payload = {
        "fixtureId": fixture.get("fixtureId"),
        "provider": args.provider,
        "model": args.model,
        "inputTokens": result.inputTokens,
        "outputTokens": result.outputTokens,
        "stopReason": result.stopReason,
        "retryCount": result.retryCount,
        "output": output.model_dump(mode="json"),
    }
    (args.output / "result.json").write_text(
        json.dumps(result_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result_payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
