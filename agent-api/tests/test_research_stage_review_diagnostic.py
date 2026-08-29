from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.domains.legal.research_stage_review_diagnostic import (
    HypothesisReview,
    QuestionDecompositionReview,
    render_hypothesis_review_call,
    render_question_decomposition_review_call,
    run_hypothesis_review,
    run_question_decomposition_review,
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


_QUESTION = (
    "株券等の所有者が少数である場合に、公開買付けによらずに株券等を"
    "買い付けることができる具体的な条件を、根拠条文とともに説明してください。"
)

_WORK_ITEMS = [
    {
        "work_item_id": "wi-1",
        "question": "所有者が少数の場合に公開買付けによらずに買い付けられる条件は何か。",
        "action_actor": "株券等を買い付ける者",
    },
]

_HYPOTHESES = [
    {
        "hypothesis_id": "h-1",
        "work_item_id": "wi-1",
        "statement": "所有者数と所有者の同意が適用除外の判定軸になる。",
        "gaps": ["人数の基準", "必要な同意の範囲"],
    },
]


def test_question_review_renders_external_prompt_and_complete_contract() -> None:
    original = deepcopy(_WORK_ITEMS)
    rendered = render_question_decomposition_review_call(
        _QUESTION,
        _WORK_ITEMS,
        non_work_item_requirements=("根拠条文を示す",),
    )

    assert _WORK_ITEMS == original
    assert rendered.stage == "question_decomposition_review_diagnostic"
    assert rendered.request.count("<review_input>") == 1
    assert "質問分解の独立レビュー" in rendered.instructions
    assert set(rendered.output_schema["properties"]) == {
        "checks",
        "work_items",
        "non_work_item_requirements",
        "findings",
    }


def test_question_review_calls_model_once_without_mutating_draft() -> None:
    original = deepcopy(_WORK_ITEMS)
    rendered = render_question_decomposition_review_call(_QUESTION, _WORK_ITEMS)
    client = FakeStructuredJSONClient(
        {
            "checks": [
                {
                    "work_item_id": "wi-1",
                    "one_legal_question": True,
                    "response_instruction_removed": True,
                    "action_actor_matches_regulated_action": True,
                    "action_target_preserved_in_question": True,
                    "note": "修正後は全観点を満たす",
                },
            ],
            "work_items": _WORK_ITEMS,
            "non_work_item_requirements": ["根拠条文を示す"],
            "findings": ["回答方法をWorkItemから分離した"],
        }
    )

    run = run_question_decomposition_review(
        rendered,
        model="gpt-4o-mini",
        max_tokens=2048,
        timeout_sec=30,
        client=client,
    )

    assert len(client.calls) == 1
    assert _WORK_ITEMS == original
    assert run.validation_error is None
    assert isinstance(run.review, QuestionDecompositionReview)
    assert run.review.work_items[0].action_actor == "株券等を買い付ける者"


def test_hypothesis_review_calls_model_once_without_mutating_draft() -> None:
    original = deepcopy(_HYPOTHESES)
    rendered = render_hypothesis_review_call(
        _QUESTION,
        _WORK_ITEMS,
        _HYPOTHESES,
    )
    client = FakeStructuredJSONClient(
        {
            "checks": [
                {
                    "hypothesis_id": "h-1",
                    "predicts_legal_proposition": True,
                    "has_searchable_legal_axis": True,
                    "gaps_only_unresolved_meaning": True,
                    "actors_match_work_item": True,
                    "note": "修正後は全観点を満たす",
                },
            ],
            "hypotheses": _HYPOTHESES,
            "findings": ["抽象的な条件を検索可能な判定軸へ直した"],
        }
    )

    run = run_hypothesis_review(
        rendered,
        model="gpt-4o-mini",
        max_tokens=2048,
        timeout_sec=30,
        client=client,
    )

    assert len(client.calls) == 1
    assert _HYPOTHESES == original
    assert run.validation_error is None
    assert isinstance(run.review, HypothesisReview)
    assert run.review.hypotheses[0].gaps == (
        "人数の基準",
        "必要な同意の範囲",
    )


def test_review_does_not_repair_invalid_first_output() -> None:
    rendered = render_hypothesis_review_call(
        _QUESTION,
        _WORK_ITEMS,
        _HYPOTHESES,
    )
    client = FakeStructuredJSONClient(None, error="invalid structured output")

    run = run_hypothesis_review(
        rendered,
        model="gpt-4o-mini",
        max_tokens=2048,
        timeout_sec=30,
        client=client,
    )

    assert len(client.calls) == 1
    assert run.review is None
    assert run.validation_error == "invalid structured output"
