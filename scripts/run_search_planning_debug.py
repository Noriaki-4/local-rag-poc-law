#!/usr/bin/env python3
"""確定済みWorkItem・Hypothesisから検索要求作成だけを実モデル診断する。"""

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
from app.domains.legal.search_planning_diagnostic import (  # noqa: E402
    run_search_planning_diagnostic,
)
from app.llm import LLMClient  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--question-decomposition-fixture", required=True, type=Path)
    parser.add_argument(
        "--hypothesis-fixture",
        required=True,
        action="append",
        type=Path,
        help="Hypothesis生成結果。WorkItemごとに複数回指定できる。",
    )
    parser.add_argument(
        "--work-item-id",
        help="1つのWorkItemと所属Hypothesisだけを隔離して診断する既知ID。",
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


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"fixture root must be an object: {path}")
    return value


def _normalized_output(fixture: dict[str, Any], path: Path) -> dict[str, Any]:
    value = fixture.get("normalizedOutput")
    if not isinstance(value, dict):
        raise ValueError(f"fixture requires normalizedOutput: {path}")
    return value


def _load_input(
    question_fixture_path: Path,
    hypothesis_fixture_paths: list[Path],
    *,
    work_item_id: str | None,
) -> tuple[str, list[Any], list[Any], list[str]]:
    question_fixture = _read_object(question_fixture_path)
    question = question_fixture.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question decomposition fixture requires question")
    normalized = _normalized_output(question_fixture, question_fixture_path)
    work_items = normalized.get("workItems")
    if not isinstance(work_items, list) or not work_items:
        raise ValueError("question decomposition fixture requires WorkItems")
    requirements = normalized.get("nonWorkItemRequirements") or []
    if not isinstance(requirements, list) or any(
        not isinstance(item, str) for item in requirements
    ):
        raise ValueError("nonWorkItemRequirements must be strings")

    hypotheses: list[Any] = []
    for path in hypothesis_fixture_paths:
        fixture = _read_object(path)
        items = _normalized_output(fixture, path).get("hypotheses")
        if not isinstance(items, list) or not items:
            raise ValueError(f"hypothesis fixture requires hypotheses: {path}")
        for item in items:
            if not isinstance(item, dict):
                raise ValueError(f"hypotheses must be objects: {path}")
            normalized_item = dict(item)
            normalized_item["hypothesis_id"] = f"h-{len(hypotheses) + 1}"
            hypotheses.append(normalized_item)
    if work_item_id is not None:
        work_items = [
            item
            for item in work_items
            if isinstance(item, dict) and item.get("work_item_id") == work_item_id
        ]
        hypotheses = [
            item for item in hypotheses if item.get("work_item_id") == work_item_id
        ]
        if not work_items:
            raise ValueError(f"unknown WorkItem ID: {work_item_id}")
        if not hypotheses:
            raise ValueError(f"WorkItem has no Hypothesis: {work_item_id}")
    return question, work_items, hypotheses, requirements


def main() -> None:
    args = _parse_args()
    question, work_items, hypotheses, requirements = _load_input(
        args.question_decomposition_fixture,
        args.hypothesis_fixture,
        work_item_id=args.work_item_id,
    )
    run = run_search_planning_diagnostic(
        question,
        work_items,
        hypotheses,
        non_work_item_requirements=requirements,
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
        "stage": "search_planning",
        "question": question,
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
