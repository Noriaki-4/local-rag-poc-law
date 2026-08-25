from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.adapters.models import StructuredJSONModelAdapter
from app.agent_framework.context import build_solver_context
from app.agent_framework.profiles import AgentLimits
from app.agent_framework.state import CaseState, Hypothesis, WorkItem
from app.domains.legal.hypothesis_generation_diagnostic import (
    render_hypothesis_generation_call,
    run_hypothesis_generation_diagnostic,
)
from app.llm import StructuredJSONResult
from app.domains.legal.profiles import legal_agent_profile


class FakeStructuredJSONClient:
    provider = "openai"

    def __init__(self, payload: dict[str, Any] | None, error: str | None = None):
        self.payload = payload
        self.error = error
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
            validationError=self.error,
            stopReason="stop",
        )


_WORK_ITEMS = [
    {
        "work_item_id": "wi-1",
        "question": "質問者が許可を要する条件は何か。",
        "action_actor": "質問者",
    }
]


def test_render_uses_only_production_hypothesis_generation_stage() -> None:
    rendered, context = render_hypothesis_generation_call(
        "許可が必要になる条件を根拠条文とともに説明してください。",
        _WORK_ITEMS,
        non_work_item_requirements=("根拠条文を示す",),
        provider="openai",
        model="gpt-4o-mini",
    )

    assert set(rendered.input_payload) == {"work_items"}
    assert "non_work_item_requirements" not in rendered.input_payload
    work_item = rendered.input_payload["work_items"][0]
    assert "action_actor" not in work_item
    assert "target_actor" not in work_item
    assert "actor_relation" not in work_item
    assert "actor_scope" not in work_item
    assert context.non_work_item_requirements == ("根拠条文を示す",)
    assert rendered.stage.endswith("research_hypothesis")
    assert "# 法令調査Solver：法的仮説の立案" in rendered.instructions
    assert "## 出力前の確認" in rendered.instructions
    assert "`work_items[].action_actor`" not in rendered.instructions
    assert "法令本文によって支持又は否定できる暫定的な結論" in (
        rendered.instructions
    )
    assert "検索前に未知の除外事由" in rendered.instructions
    assert "下位法令へ委任" in rendered.instructions
    properties = rendered.output_schema["properties"]["hypotheses"][
        "items"
    ]["properties"]
    assert set(properties) == {"work_item_id", "statement"}


def test_run_calls_model_once_and_normalizes_without_actor_copy() -> None:
    client = FakeStructuredJSONClient(
        {
            "hypotheses": [
                {
                    "work_item_id": "wi-1",
                    "statement": "質問者は、対象事業の規模に応じて許可を要する。",
                }
            ]
        }
    )

    run = run_hypothesis_generation_diagnostic(
        "許可が必要になる条件は何か。",
        _WORK_ITEMS,
        provider="openai",
        model="gpt-4o-mini",
        max_tokens=2048,
        timeout_sec=30,
        client=client,
    )

    assert len(client.calls) == 1
    assert run.validation_error is None
    assert run.decision is not None
    hypothesis = run.decision.update.add_hypotheses[0]
    assert hypothesis.hypothesis_id == "h-1"
    assert hypothesis.work_item_id == "wi-1"
    assert not hasattr(hypothesis, "actor_scope")
    assert not hasattr(hypothesis, "actor_relation")
    assert hypothesis.gaps == ()
    assert run.decision.tool_requests == ()


def test_run_does_not_repair_invalid_first_output() -> None:
    client = FakeStructuredJSONClient(None, error="invalid structured output")

    run = run_hypothesis_generation_diagnostic(
        "許可が必要になる条件は何か。",
        _WORK_ITEMS,
        provider="openai",
        model="gpt-4o-mini",
        max_tokens=2048,
        timeout_sec=30,
        client=client,
    )

    assert len(client.calls) == 1
    assert run.decision is None
    assert run.validation_error == "invalid structured output"


def test_production_adapter_generates_hypotheses_one_work_item_at_a_time() -> None:
    client = FakeStructuredJSONClient(
        {
            "hypotheses": [
                {
                    "work_item_id": "wi-2",
                    "statement": "第二の論点には暫定的な結論がある。",
                }
            ]
        }
    )
    context = build_solver_context(
        CaseState(
            case_id="case-hypothesis-step",
            question="二つの論点を確認する。",
            work_items=(
                WorkItem(work_item_id="wi-1", question="第一の論点"),
                WorkItem(work_item_id="wi-2", question="第二の論点"),
            ),
            hypotheses=(
                Hypothesis(
                    hypothesis_id="h-1",
                    work_item_id="wi-1",
                    statement="第一の論点に対する暫定的な結論。",
                ),
            ),
        ),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    profile = legal_agent_profile().solver_hypothesis_generation
    assert profile is not None

    result = StructuredJSONModelAdapter(client).solve(context, profile)

    schema = client.calls[0]["schema"]
    work_item_id_schema = schema["properties"]["hypotheses"]["items"][
        "properties"
    ]["work_item_id"]
    assert work_item_id_schema["enum"] == ["wi-2"]
    hypothesis = result.decision.update.add_hypotheses[0]
    assert hypothesis.hypothesis_id == "h-2"
    assert hypothesis.work_item_id == "wi-2"
    assert hypothesis.gaps == ()


def test_v269_observed_failures_are_preserved_as_prompt_regression() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures/framework/legal_hypothesis_generation_v269_observed_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert fixture["source"]["model"] == "gpt-4o-mini"
    observed = "\n".join(
        item["statement"]
        for case in fixture["cases"]
        for item in case["observedHypotheses"]
    )
    assert "法令により異なる" in observed

    profile = legal_agent_profile().solver_hypothesis_generation
    assert profile is not None
    prompt = profile.system_prompt
    completion = profile.completion_check_prompt or ""
    assert "一般的な法的知識" in prompt
    assert "検索前に未知の除外事由" in prompt
    assert "下位法令へ委任" in prompt
    assert "根拠法令名、条文番号、検索語、検索作業" in completion
