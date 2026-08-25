#!/usr/bin/env python3
"""対象関連主体なしの候補選別を実モデルで1回確認する。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "agent-api"))

from app.domains.legal.action_actor_only_diagnostic import (  # noqa: E402
    render_action_actor_only_call,
    run_action_actor_only_diagnostic,
)
from app.llm import LLMClient  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--provider", default="openai")
    parser.add_argument("--model", default="gpt-4o-mini")
    args = parser.parse_args()

    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    run = run_action_actor_only_diagnostic(
        render_action_actor_only_call(fixture["cases"]),
        model=args.model,
        max_tokens=1024,
        timeout_sec=90,
        client=LLMClient(provider=args.provider),
    )
    payload = {
        "validationError": run.validation_error,
        "inputTokens": run.transport_result.inputTokens,
        "outputTokens": run.transport_result.outputTokens,
        "latencyMs": run.transport_result.latencyMs,
        "output": (
            run.output.model_dump(mode="json") if run.output is not None else None
        ),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if run.validation_error is not None:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
