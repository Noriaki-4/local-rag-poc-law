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
        "assessments": {
            candidate.article_id: {
                "legal_function": "applicability",
                "summary": f"{candidate.title}の候補内容。",
                "matched_hypothesis_ids": ["h-1"],
            }
            for candidate in context.search_candidates
        },
    }


def test_render_uses_only_production_search_assessment_prompt_and_schema() -> None:
    context = _context()

    rendered = render_search_assessment_call(
        context,
        provider="openai",
        model="gpt-4o-mini",
    )

    assert rendered.stage == "search_assessment"
    assert "# 法令調査Solver：本文取得候補の内容評価" in rendered.instructions
    assert "# 法令調査Solver：検索候補の主体照合" not in rendered.instructions
    assert "# 法令調査Solver：検索候補の選択" not in rendered.instructions
    assert set(rendered.input_payload) == {
        "question",
        "work_tree",
        "hypotheses",
        "search_candidates",
    }
    assert "`search_candidates[]`: legal_searchの検索結果" in (
        rendered.instructions
    )
    assert set(rendered.output_schema["properties"]) == {"assessments"}
    assessments_schema = rendered.output_schema["properties"]["assessments"]
    assert "search_candidates[].article_idと同じ文字列" in (
        assessments_schema["description"]
    )
    assert set(assessments_schema["properties"]) == {
        item.article_id for item in context.search_candidates
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
    assert "actor_matches" not in run.decision.assessments[0].model_dump()


def test_v309_real_model_search_assessment_fixtures_preserve_call_boundary() -> None:
    fixture_names = (
        "tob_announcement_search_assessment_v309_observed_v1.json",
        "tob_exceptions_focused_search_assessment_v309_observed_v1.json",
        "tob_overview_search_assessment_v309_observed_v1.json",
    )

    for fixture_name in fixture_names:
        fixture = json.loads(
            (FIXTURE.parent / fixture_name).read_text(encoding="utf-8")
        )
        source = fixture["source"]
        transport_input = fixture["observedTransportInput"]
        transport_output = fixture["observedTransportOutput"]
        input_candidates = transport_input["inputPayload"]["search_candidates"]
        candidate_ids = tuple(item["article_id"] for item in input_candidates)

        assert fixture["checkpoint"]["approved"] is True
        assert fixture["checkpoint"]["sourceProvider"] == "openai"
        assert source["model"] == "gpt-4o-mini-2024-07-18"
        assert source["profileVersion"] == "309"
        assert source["transportStage"] == "search_assessment"
        assert transport_output["validationError"] is None
        assert set(transport_output["payload"]) == {"assessments"}
        assert tuple(transport_output["payload"]["assessments"]) == candidate_ids

    exception_fixture = json.loads(
        (
            FIXTURE.parent
            / "tob_exceptions_focused_search_assessment_v309_observed_v1.json"
        ).read_text(encoding="utf-8")
    )
    assert exception_fixture["expectations"]["workItemCount"] == 1
    assert exception_fixture["expectations"]["hypothesisCount"] == 1
