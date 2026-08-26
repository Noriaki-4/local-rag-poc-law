from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.agent_framework.context import SolverContext
from app.domains.legal.search_assessment_diagnostic import (
    render_search_assessment_call,
    run_search_assessment_diagnostic,
)
from app.llm import StructuredJSONResult


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "framework"
    / "tob_actor_relation_search_v191.json"
)


class FakeStructuredJSONClient:
    provider = "openai"

    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def generate_structured_json(self, **kwargs: Any) -> StructuredJSONResult:
        self.calls.append(kwargs)
        return StructuredJSONResult(
            payload=self.payload,
            provider=self.provider,
            model=kwargs["model"],
            latencyMs=12,
            inputTokens=100,
            outputTokens=50,
            validationError=None,
            stopReason="stop",
        )


def _context() -> SolverContext:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return SolverContext.model_validate(fixture["solverContext"])


def _assessment_payload(context: SolverContext) -> dict[str, Any]:
    return {
        "search_request_ids": list(context.required_search_review_request_ids),
        "assessments": {
            candidate.article_id: {
                "legal_function": "applicability",
                "summary": f"{candidate.title}の候補内容。",
                "matched_hypothesis_ids": ["h-1"],
            }
            for candidate in context.search_candidates
        },
        "reason": "全候補の内容を評価した。",
    }


def test_render_uses_only_production_search_assessment_prompt_and_schema() -> None:
    context = _context()

    rendered = render_search_assessment_call(
        context,
        provider="openai",
        model="gpt-4o-mini",
    )

    assert rendered.stage == "search_assessment"
    assert "# 法令調査Solver：検索抜粋の整理" in rendered.instructions
    assert "# 法令調査Solver：検索候補の主体照合" not in rendered.instructions
    assert "# 法令調査Solver：検索候補の選択" not in rendered.instructions
    assert set(rendered.input_payload) == {
        "question",
        "work_tree",
        "hypotheses",
        "search_candidates",
        "candidate_count",
        "required_search_review_request_ids",
    }
    assert set(rendered.output_schema["properties"]) == {
        "search_request_ids",
        "assessments",
        "reason",
    }


def test_run_calls_model_once_and_normalizes_assessment_map() -> None:
    context = _context()
    client = FakeStructuredJSONClient(_assessment_payload(context))

    run = run_search_assessment_diagnostic(
        context,
        provider="openai",
        model="gpt-4o-mini",
        max_tokens=4096,
        timeout_sec=90,
        client=client,
    )

    assert len(client.calls) == 1
    assert run.validation_error is None
    assert run.decision is not None
    assert tuple(
        item.article_id for item in run.decision.assessments
    ) == tuple(item.article_id for item in context.search_candidates)
    assert run.decision.assessments[0].actor_matches == ()
