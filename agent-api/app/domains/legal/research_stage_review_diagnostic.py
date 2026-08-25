"""Research前半の生成結果を独立した1回のLLM呼出しでレビューする診断。"""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from app.agent_framework.model_call_artifacts import (
    RenderedModelCall,
    build_rendered_model_call,
)
from app.agent_framework.state import Hypothesis, WorkItem

_PROMPT_DIR = Path(__file__).with_name("prompts")


class _ReviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ReviewedWorkItem(_ReviewModel):
    work_item_id: str = Field(min_length=1, max_length=160)
    question: str = Field(min_length=1, max_length=1000)
    action_actor: str = Field(min_length=1, max_length=600)


class QuestionDecompositionCheck(_ReviewModel):
    work_item_id: str = Field(min_length=1, max_length=160)
    one_legal_question: bool
    response_instruction_removed: bool
    action_actor_matches_regulated_action: bool
    action_target_preserved_in_question: bool
    note: str = Field(min_length=1, max_length=1000)


class QuestionDecompositionReview(_ReviewModel):
    checks: tuple[QuestionDecompositionCheck, ...] = Field(min_length=1)
    work_items: tuple[ReviewedWorkItem, ...] = Field(min_length=1)
    non_work_item_requirements: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()


class ReviewedHypothesis(_ReviewModel):
    hypothesis_id: str = Field(min_length=1, max_length=160)
    work_item_id: str = Field(min_length=1, max_length=160)
    statement: str = Field(min_length=1, max_length=2000)
    gaps: tuple[str, ...] = ()


class HypothesisCheck(_ReviewModel):
    hypothesis_id: str = Field(min_length=1, max_length=160)
    predicts_legal_proposition: bool
    has_searchable_legal_axis: bool
    gaps_only_unresolved_meaning: bool
    actors_match_work_item: bool
    note: str = Field(min_length=1, max_length=1000)


class HypothesisReview(_ReviewModel):
    checks: tuple[HypothesisCheck, ...] = Field(min_length=1)
    hypotheses: tuple[ReviewedHypothesis, ...] = Field(min_length=1)
    findings: tuple[str, ...] = ()


class StructuredJSONClient(Protocol):
    provider: str

    def generate_structured_json(
        self,
        *,
        prompt: str,
        schema: dict[str, Any],
        model: str,
        max_tokens: int,
        timeout_sec: int,
    ) -> Any: ...


ReviewOutput = TypeVar(
    "ReviewOutput",
    QuestionDecompositionReview,
    HypothesisReview,
)


@dataclass(frozen=True)
class ResearchStageReviewRun:
    rendered: RenderedModelCall
    transport_result: Any
    review: QuestionDecompositionReview | HypothesisReview | None
    validation_error: str | None


def render_question_decomposition_review_call(
    question: str,
    work_items: Iterable[WorkItem | dict[str, object]],
    *,
    non_work_item_requirements: Iterable[str] = (),
) -> RenderedModelCall:
    """質問分解の生成済み出力を読む独立レビュー呼出しを完成させる。"""

    normalized_question = _non_empty(question, name="question")
    normalized_work_items = tuple(
        item if isinstance(item, WorkItem) else WorkItem.model_validate(item)
        for item in work_items
    )
    if not normalized_work_items:
        raise ValueError("at least one WorkItem is required")
    payload = {
        "question": normalized_question,
        "draft": {
            "work_items": [
                {
                    "work_item_id": item.work_item_id,
                    "question": item.question,
                    "action_actor": item.action_actor or "不明",
                }
                for item in normalized_work_items
            ],
            "non_work_item_requirements": [
                _non_empty(value, name="non_work_item_requirement")
                for value in non_work_item_requirements
            ],
        },
    }
    return _render_review_call(
        stage="question_decomposition_review_diagnostic",
        prompt_name="diagnostic_question_decomposition_review.md",
        input_payload=payload,
        output_model=QuestionDecompositionReview,
    )


def render_hypothesis_review_call(
    question: str,
    work_items: Iterable[WorkItem | dict[str, object]],
    hypotheses: Iterable[Hypothesis | dict[str, object]],
) -> RenderedModelCall:
    """Hypothesisの生成済み出力を読む独立レビュー呼出しを完成させる。"""

    normalized_question = _non_empty(question, name="question")
    normalized_work_items = tuple(
        item if isinstance(item, WorkItem) else WorkItem.model_validate(item)
        for item in work_items
    )
    normalized_hypotheses = tuple(
        item if isinstance(item, Hypothesis) else Hypothesis.model_validate(item)
        for item in hypotheses
    )
    if not normalized_work_items:
        raise ValueError("at least one WorkItem is required")
    if not normalized_hypotheses:
        raise ValueError("at least one Hypothesis is required")
    work_item_ids = {item.work_item_id for item in normalized_work_items}
    if any(item.work_item_id not in work_item_ids for item in normalized_hypotheses):
        raise ValueError("Hypothesis references an unknown WorkItem")
    payload = {
        "question": normalized_question,
        "work_items": [
            {
                "work_item_id": item.work_item_id,
                "question": item.question,
                "action_actor": item.action_actor or "不明",
            }
            for item in normalized_work_items
        ],
        "draft_hypotheses": [
            {
                "hypothesis_id": item.hypothesis_id,
                "work_item_id": item.work_item_id,
                "statement": item.statement,
                "gaps": list(item.gaps),
            }
            for item in normalized_hypotheses
        ],
    }
    return _render_review_call(
        stage="hypothesis_review_diagnostic",
        prompt_name="diagnostic_hypothesis_review.md",
        input_payload=payload,
        output_model=HypothesisReview,
    )


def run_question_decomposition_review(
    rendered: RenderedModelCall,
    *,
    model: str,
    max_tokens: int,
    timeout_sec: int,
    client: StructuredJSONClient,
) -> ResearchStageReviewRun:
    return _run_review(
        rendered,
        output_model=QuestionDecompositionReview,
        model=model,
        max_tokens=max_tokens,
        timeout_sec=timeout_sec,
        client=client,
    )


def run_hypothesis_review(
    rendered: RenderedModelCall,
    *,
    model: str,
    max_tokens: int,
    timeout_sec: int,
    client: StructuredJSONClient,
) -> ResearchStageReviewRun:
    return _run_review(
        rendered,
        output_model=HypothesisReview,
        model=model,
        max_tokens=max_tokens,
        timeout_sec=timeout_sec,
        client=client,
    )


def _render_review_call(
    *,
    stage: str,
    prompt_name: str,
    input_payload: dict[str, Any],
    output_model: type[ReviewOutput],
) -> RenderedModelCall:
    instructions = (_PROMPT_DIR / prompt_name).read_text(encoding="utf-8").strip()
    schema = output_model.model_json_schema()
    return build_rendered_model_call(
        stage=stage,
        instructions=instructions,
        input_tag="review_input",
        input_payload=deepcopy(input_payload),
        output_schema=schema,
        normalized_schema=schema,
    )


def _run_review(
    rendered: RenderedModelCall,
    *,
    output_model: type[ReviewOutput],
    model: str,
    max_tokens: int,
    timeout_sec: int,
    client: StructuredJSONClient,
) -> ResearchStageReviewRun:
    result = client.generate_structured_json(
        prompt=rendered.request,
        schema=rendered.output_schema,
        model=model,
        max_tokens=max_tokens,
        timeout_sec=timeout_sec,
    )
    if result.validationError is not None or result.payload is None:
        return ResearchStageReviewRun(
            rendered=rendered,
            transport_result=result,
            review=None,
            validation_error=result.validationError or "empty model output",
        )
    try:
        review = output_model.model_validate(result.payload)
    except (TypeError, ValueError) as exc:
        return ResearchStageReviewRun(
            rendered=rendered,
            transport_result=result,
            review=None,
            validation_error=str(exc),
        )
    return ResearchStageReviewRun(
        rendered=rendered,
        transport_result=result,
        review=review,
        validation_error=None,
    )


def _non_empty(value: str, *, name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


__all__ = [
    "HypothesisReview",
    "QuestionDecompositionReview",
    "ResearchStageReviewRun",
    "render_hypothesis_review_call",
    "render_question_decomposition_review_call",
    "run_hypothesis_review",
    "run_question_decomposition_review",
]
