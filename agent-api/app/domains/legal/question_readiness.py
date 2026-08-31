"""検索前に質問の意味上の曖昧さだけを確認する独立Solver処理。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from app.agent_framework.contract_rendering import render_model_input_glossary
from app.agent_framework.model_call_artifacts import (
    RUNTIME_INPUT_MARKER,
    RenderedModelCall,
    build_rendered_model_call,
)
from app.config import settings
from app.llm import StructuredJSONResult
from app.models import QuestionReadiness, QuestionReadinessRequest

QUESTION_READINESS_PROFILE_NAME = "legal-question-readiness"
QUESTION_READINESS_PROFILE_VERSION = "7"
_PROMPT_PATH = Path(__file__).with_name("prompts") / "solver_question_readiness.md"


@dataclass(frozen=True)
class QuestionReadinessProfile:
    name: str
    version: str
    model: str
    max_output_tokens: int
    timeout_sec: int


def question_readiness_profile() -> QuestionReadinessProfile:
    return QuestionReadinessProfile(
        name=QUESTION_READINESS_PROFILE_NAME,
        version=QUESTION_READINESS_PROFILE_VERSION,
        model=settings.agent_framework_research_model,
        max_output_tokens=min(settings.agent_framework_research_max_tokens, 2048),
        timeout_sec=settings.agent_framework_model_timeout_sec,
    )


class QuestionReadinessModelProtocolError(ValueError):
    """Provider応答をQuestionReadiness契約として検証できない。"""


class StructuredJSONClient(Protocol):
    provider: str

    def generate_structured_json(
        self,
        *,
        prompt: str,
        schema: dict,
        model: str,
        max_tokens: int,
        timeout_sec: int,
    ) -> StructuredJSONResult: ...


def render_question_readiness_call(
    request: QuestionReadinessRequest,
) -> RenderedModelCall:
    """固定指示、入力、出力契約から実送信内容を決定的に生成する。"""

    prompt = _PROMPT_PATH.read_text(encoding="utf-8").strip()
    instructions = "\n\n".join(
        (
            prompt,
            render_model_input_glossary(QuestionReadinessRequest),
            f"## 入力\n\n{RUNTIME_INPUT_MARKER}",
        )
    )
    schema = QuestionReadiness.model_json_schema()
    return build_rendered_model_call(
        stage="question_readiness",
        instructions=instructions,
        input_tag="question_readiness_input",
        input_payload=request.model_dump(mode="json"),
        output_schema=schema,
        normalized_schema=schema,
    )


class QuestionReadinessService:
    def __init__(self, llm_client: StructuredJSONClient) -> None:
        self._llm_client = llm_client

    def check(self, request: QuestionReadinessRequest) -> QuestionReadiness:
        rendered = render_question_readiness_call(request)
        profile = question_readiness_profile()
        result = self._llm_client.generate_structured_json(
            prompt=rendered.request,
            schema=rendered.output_schema,
            model=profile.model,
            max_tokens=profile.max_output_tokens,
            timeout_sec=profile.timeout_sec,
        )
        if result.validationError or result.payload is None:
            raise QuestionReadinessModelProtocolError(
                "question readiness transport invalid"
            )
        try:
            return QuestionReadiness.model_validate(result.payload)
        except ValidationError as exc:
            raise QuestionReadinessModelProtocolError(
                "question readiness result violates schema"
            ) from exc
