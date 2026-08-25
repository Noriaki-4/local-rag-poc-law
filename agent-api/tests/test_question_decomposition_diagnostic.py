from __future__ import annotations

from typing import Any

from app.domains.legal.question_decomposition_diagnostic import (
    render_question_decomposition_call,
    run_question_decomposition_diagnostic,
)
from app.llm import StructuredJSONResult


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


def test_render_uses_only_production_question_decomposition_stage() -> None:
    rendered, _ = render_question_decomposition_call(
        "許可が必要になる条件は何か。",
        provider="openai",
        model="gpt-4o-mini",
    )

    assert rendered.input_payload == {"question": "許可が必要になる条件は何か。"}
    assert rendered.stage.endswith("research_decomposition")
    assert "# 法令調査Solver：質問の要求分解" in rendered.instructions
    assert "## 出力前の確認" in rendered.instructions
    assert "仮説、検索語、Tool要求は作りません" in rendered.instructions
    assert "solver_hypothesis_generation" not in str(rendered.prompt_assets)
    assert set(rendered.output_schema["properties"]) == {
        "work_items",
        "non_work_item_requirements",
    }


def test_run_calls_model_once_and_normalizes_with_production_contract() -> None:
    client = FakeStructuredJSONClient(
        {
            "work_items": [
                {
                    "question": "質問者が許可を要する条件は何か。",
                    "actor_scope": "行為者＝質問者、対象関連主体＝不明",
                    "actor_relation": "unknown",
                }
            ],
            "non_work_item_requirements": ["根拠条文を示す"],
        }
    )

    run = run_question_decomposition_diagnostic(
        "許可が必要になる条件を根拠条文とともに説明してください。",
        provider="openai",
        model="gpt-4o-mini",
        max_tokens=2048,
        timeout_sec=30,
        client=client,
    )

    assert len(client.calls) == 1
    assert run.validation_error is None
    assert run.decision is not None
    assert [
        item.question for item in run.decision.update.add_work_items
    ] == ["質問者が許可を要する条件は何か。"]
    assert run.decision.update.set_non_work_item_requirements == (
        "根拠条文を示す",
    )
    assert run.decision.update.add_hypotheses == ()
    assert run.decision.tool_requests == ()


def test_run_does_not_repair_invalid_first_output() -> None:
    client = FakeStructuredJSONClient(None, error="invalid structured output")

    run = run_question_decomposition_diagnostic(
        "許可が必要になる条件は何か。",
        provider="openai",
        model="gpt-4o-mini",
        max_tokens=2048,
        timeout_sec=30,
        client=client,
    )

    assert len(client.calls) == 1
    assert run.decision is None
    assert run.validation_error == "invalid structured output"
