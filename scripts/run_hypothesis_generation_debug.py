#!/usr/bin/env python3
"""本番のHypothesis生成Promptだけを実モデルで1回診断する。"""

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
from app.domains.legal.hypothesis_generation_diagnostic import (  # noqa: E402
    run_hypothesis_generation_diagnostic,
)
from app.domains.legal.profiles import legal_agent_profile  # noqa: E402
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
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--work-item-id",
        help="複数あるWorkItemのうち、単独で診断する既知ID。",
    )
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--timeout-sec", type=int, default=90)
    return parser.parse_args()


def _read_fixture(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("fixture root must be a JSON object")
    return value


def _first_mapping(value: Any, *keys: str) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, dict):
            return candidate
    return None


def _first_list(value: Any, *keys: str) -> list[Any] | None:
    if not isinstance(value, dict):
        return None
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, list):
            return candidate
    return None


def _load_input(path: Path) -> tuple[str, list[Any], list[str], str | None]:
    fixture = _read_fixture(path)
    solver_context = _first_mapping(fixture, "solverContext", "solver_context")
    case_state = _first_mapping(fixture, "caseState", "case_state")
    normalized = _first_mapping(fixture, "normalizedOutput", "normalized_output")

    question = fixture.get("question")
    for container in (solver_context, case_state):
        if not isinstance(question, str) and container is not None:
            question = container.get("question")
    if not isinstance(question, str):
        artifact_input = path.with_name("input.json")
        if artifact_input.is_file():
            question = _read_fixture(artifact_input).get("question")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("fixture requires a non-empty question")

    work_items = _first_list(normalized, "workItems", "work_items")
    if work_items is None:
        work_items = _first_list(fixture, "workItems", "work_items")
    if work_items is None and solver_context is not None:
        work_items = _first_list(solver_context, "work_tree", "workItems")
    if work_items is None and case_state is not None:
        work_items = _first_list(case_state, "work_items", "workItems")
    if not work_items:
        raise ValueError("fixture requires normalized WorkItems with IDs")
    work_items = [
        {
            key: value
            for key, value in item.items()
            if key not in {"hypothesis_ids", "evidence_count"}
        }
        if isinstance(item, dict)
        else item
        for item in work_items
    ]

    requirements = _first_list(
        normalized,
        "nonWorkItemRequirements",
        "non_work_item_requirements",
    )
    if requirements is None:
        for container in (fixture, solver_context, case_state):
            requirements = _first_list(
                container,
                "nonWorkItemRequirements",
                "non_work_item_requirements",
            )
            if requirements is not None:
                break
    if requirements is None:
        requirements = []
    if any(not isinstance(item, str) for item in requirements):
        raise ValueError("non-WorkItem requirements must be strings")

    fixture_id = fixture.get("fixtureId")
    return (
        question,
        work_items,
        requirements,
        fixture_id if isinstance(fixture_id, str) else None,
    )


def _result_payload(
    run: Any,
    *,
    fixture_id: str | None,
    question: str,
    provider: str,
    model: str,
) -> dict[str, Any]:
    result = run.transport_result
    decision = run.decision
    return {
        "fixtureId": fixture_id,
        "stage": "hypothesis_generation",
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
                "hypotheses": [
                    item.model_dump(mode="json")
                    for item in decision.update.add_hypotheses
                ]
            }
            if decision is not None
            else None
        ),
    }


def main() -> None:
    args = _parse_args()
    question, work_items, requirements, fixture_id = _load_input(args.fixture)
    if args.work_item_id is not None:
        work_items = [
            item
            for item in work_items
            if (item.get("work_item_id") if isinstance(item, dict) else None)
            == args.work_item_id
        ]
        if not work_items:
            raise ValueError(f"unknown WorkItem ID: {args.work_item_id}")
    run = run_hypothesis_generation_diagnostic(
        question,
        work_items,
        non_work_item_requirements=requirements,
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
        fixture_id=fixture_id,
        question=question,
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
