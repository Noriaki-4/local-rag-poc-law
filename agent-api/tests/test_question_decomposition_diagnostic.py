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
    assert "## 完了条件" in rendered.instructions
    assert "## 出力" in rendered.instructions
    assert "法令の解釈又は適用について個別に結論を出す問い" in rendered.instructions
    assert "複数の法的論点がある場合は、WorkItemを分けます" in rendered.instructions
    assert "明確でなければ`不明`" in rendered.instructions
    assert "法的根拠の有無又は内容自体について結論を求める問い" in rendered.instructions
    assert "solver_hypothesis_generation" not in str(rendered.prompt_assets)
    assert set(rendered.output_schema["properties"]) == {
        "work_items",
        "non_work_item_requirements",
    }
    work_item_properties = rendered.output_schema["properties"]["work_items"][
        "items"
    ]["properties"]
    assert "1つの法的論点" in work_item_properties["question"]["description"]
    assert "法的論点ではない要求" in rendered.output_schema["properties"][
        "non_work_item_requirements"
    ]["description"]
    assert "action_actor" in work_item_properties
    assert "target_actor" not in work_item_properties
    assert "actor_relation" not in work_item_properties
    assert "actor_scope" not in work_item_properties


def test_run_calls_model_once_and_normalizes_with_production_contract() -> None:
    client = FakeStructuredJSONClient(
        {
            "work_items": [
                {
                    "question": "質問者が許可を要する条件は何か。",
                    "action_actor": "質問者",
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
    work_item = run.decision.update.add_work_items[0]
    assert work_item.action_actor == "質問者"
    assert not hasattr(work_item, "target_actor")
    assert not hasattr(work_item, "actor_relation")
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
