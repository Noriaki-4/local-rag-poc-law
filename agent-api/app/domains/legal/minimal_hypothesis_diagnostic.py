"""本番Agent契約から分離した、最小の法的仮説立案診断。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agent_framework.model_call_artifacts import (
    RenderedModelCall,
    build_rendered_model_call,
)

_PROMPT_PATH = Path(__file__).with_name("prompts") / "minimal_hypothesis_diagnostic.md"


class MinimalHypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    statement: str = Field(min_length=1, max_length=2000)


class MinimalWorkItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=1000)
    hypotheses: list[MinimalHypothesis] = Field(min_length=1, max_length=8)


class MinimalHypothesisOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_items: list[MinimalWorkItem] = Field(min_length=1, max_length=12)


def minimal_hypothesis_schema() -> dict[str, Any]:
    """意味項目をWorkItemとHypothesisだけに限定したProvider契約。"""

    return {
        "type": "object",
        "properties": {
            "work_items": {
                "type": "array",
                "minItems": 1,
                "maxItems": 12,
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 1000,
                        },
                        "hypotheses": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 8,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "statement": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": 2000,
                                    }
                                },
                                "required": ["statement"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["question", "hypotheses"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["work_items"],
        "additionalProperties": False,
    }


def render_minimal_hypothesis_call(question: str) -> RenderedModelCall:
    """質問以外の案件状態を持たない、レビュー可能な呼出しを組み立てる。"""

    normalized_question = question.strip()
    if not normalized_question:
        raise ValueError("question must not be empty")
    instructions = _PROMPT_PATH.read_text(encoding="utf-8").strip()
    schema = minimal_hypothesis_schema()
    return build_rendered_model_call(
        stage="minimal_legal_hypothesis_diagnostic",
        instructions=instructions,
        input_tag="question_input",
        input_payload={"question": normalized_question},
        output_schema=schema,
        normalized_schema=MinimalHypothesisOutput.model_json_schema(),
    )
