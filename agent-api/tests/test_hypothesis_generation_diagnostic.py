from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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

    assert set(rendered.input_payload) == {"question", "work_items"}
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
    assert "質問から読み取れる範囲の行為者の役割と行為" in rendered.instructions
    properties = rendered.output_schema["properties"]["hypotheses"][
        "items"
    ]["properties"]
    assert set(properties) == {"work_item_id", "statement", "gaps"}


def test_run_calls_model_once_and_normalizes_without_actor_copy() -> None:
    client = FakeStructuredJSONClient(
        {
            "hypotheses": [
                {
                    "work_item_id": "wi-1",
                    "statement": "質問者は、対象事業の規模に応じて許可を要する。",
                    "gaps": ["許可が必要になる事業規模"],
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
    assert "行為者の法的立場によって適用が変わり" in prompt
    assert "分からない条件、数値、列挙、法的効果を作りません" in prompt
    assert "質問にない観点、検索語、根拠条文" in completion
