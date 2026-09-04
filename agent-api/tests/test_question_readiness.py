from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app import main
from app.agent_framework.model_call_artifacts import model_call_artifact_contents
from app.domains.legal.question_readiness import (
    QUESTION_READINESS_PROFILE_NAME,
    QUESTION_READINESS_PROFILE_VERSION,
    QuestionReadinessModelProtocolError,
    QuestionReadinessService,
    question_readiness_profile,
    render_question_readiness_call,
)
from app.llm import StructuredJSONResult
from app.models import QuestionReadiness, QuestionReadinessRequest


class FakeStructuredJSONClient:
    provider = "fake"

    def __init__(
        self,
        payload: dict[str, Any] | None,
        *,
        validation_error: str | None = None,
    ) -> None:
        self.payload = payload
        self.validation_error = validation_error
        self.calls: list[dict[str, Any]] = []

    def generate_structured_json(self, **kwargs: Any) -> StructuredJSONResult:
        self.calls.append(kwargs)
        return StructuredJSONResult(
            payload=self.payload,
            provider=self.provider,
            model=kwargs["model"],
            latencyMs=1,
            inputTokens=10,
            outputTokens=20,
            validationError=self.validation_error,
        )


def test_render_question_readiness_uses_only_question_and_typed_contract() -> None:
    rendered = render_question_readiness_call(
        QuestionReadinessRequest(question="会社が株式を買う場合の手続は何ですか。")
    )

    assert QUESTION_READINESS_PROFILE_NAME == "legal-question-readiness"
    assert QUESTION_READINESS_PROFILE_VERSION == "11"
    assert question_readiness_profile().name == QUESTION_READINESS_PROFILE_NAME
    assert question_readiness_profile().version == QUESTION_READINESS_PROFILE_VERSION
    assert rendered.stage == "question_readiness"
    assert rendered.input_payload == {
        "question": "会社が株式を買う場合の手続は何ですか。"
    }
    assert "# 法令調査Solver：検索精度の確認" in rendered.instructions
    assert "検索精度を高める案内" in rendered.instructions
    assert "相手方、対象又は条件" in rendered.instructions
    assert "回答として調べる内容" in rendered.instructions
    assert "原文にある確認事項と限定を保ち" in rendered.instructions
    assert "判断は質問に書かれた内容だけ" in rendered.instructions
    assert "`question`" in rendered.instructions
    assert "<question_readiness_input>" in rendered.request
    assert set(rendered.output_schema["properties"]) == {
        "decision",
        "reason",
        "recommendation",
    }
    assert set(rendered.output_schema["required"]) == {
        "decision",
        "reason",
        "recommendation",
    }
    assert "検索対象を特定できる行為" in rendered.output_schema["properties"][
        "decision"
    ]["description"]


def test_question_readiness_model_call_artifacts_are_current() -> None:
    rendered = render_question_readiness_call(
        QuestionReadinessRequest(
            question="会社が市場外で株式を買う場合、何か手続が必要ですか。"
        )
    )
    expected = model_call_artifact_contents(
        rendered,
        provider="openai",
        profile_name=QUESTION_READINESS_PROFILE_NAME,
        profile_version=QUESTION_READINESS_PROFILE_VERSION,
        model="gpt-4o-mini",
    )
    artifact_dir = (
        Path(__file__).parent
        / "fixtures/model_call_artifacts/legal-question-readiness-v11/openai"
    )

    assert {path.name for path in artifact_dir.iterdir()} == set(expected)
    for name, content in expected.items():
        assert (artifact_dir / name).read_text(encoding="utf-8") == content


def test_service_returns_ready_without_changing_the_original_question(
    monkeypatch,
) -> None:
    client = FakeStructuredJSONClient(
        {
            "decision": "ready",
            "reason": "一般的な制度説明として調査を開始できるため。",
            "recommendation": (
                "会社が有価証券報告書を提出する期限はいつですか。"
            ),
        }
    )
    monkeypatch.setattr(
        "app.domains.legal.question_readiness.settings.agent_framework_model_tiers",
        {"low": "test-model", "middle": "middle-model", "high": "high-model"},
    )

    result = QuestionReadinessService(client).check(
        QuestionReadinessRequest(
            question="会社が有価証券報告書を提出する期限はいつですか。"
        )
    )

    assert result.decision == "ready"
    assert result.recommendation == (
        "会社が有価証券報告書を提出する期限はいつですか。"
    )
    assert len(client.calls) == 1
    assert client.calls[0]["model"] == "middle-model"
    assert "会社が有価証券報告書を提出する期限はいつですか。" in client.calls[0][
        "prompt"
    ]


def test_service_returns_a_clarification_recommendation() -> None:
    client = FakeStructuredJSONClient(
        {
            "decision": "clarification_recommended",
            "reason": "取得者によって確認対象となる規律が分かれるため。",
            "recommendation": "株式を取得する主体を質問文で明確にすると検索精度が上がります。",
        }
    )

    result = QuestionReadinessService(client).check(
        QuestionReadinessRequest(question="会社が株式を買う場合の手続は何ですか。")
    )

    assert result.decision == "clarification_recommended"
    assert result.recommendation == (
        "株式を取得する主体を質問文で明確にすると検索精度が上がります。"
    )


def test_contract_accepts_a_recommendation_for_independent_pairs() -> None:
    result = QuestionReadiness.model_validate(
        {
            "decision": "clarification_recommended",
            "reason": "二つの主体・行為ペアは検索を分ける必要があるため。",
            "recommendation": (
                "主に調べる主体と行為の組を一つに絞り、残りを別の質問にすると"
                "検索精度が上がります。"
            ),
        }
    )

    assert "別の質問" in result.recommendation


def test_question_readiness_contract_accepts_search_question_on_ready() -> None:
    result = QuestionReadiness.model_validate(
        {
            "decision": "ready",
            "reason": "調査可能",
            "recommendation": "会社が株式を取得する場合の手続は何ですか。",
        }
    )

    assert result.recommendation == "会社が株式を取得する場合の手続は何ですか。"


def test_question_readiness_contract_requires_recommendation() -> None:
    with pytest.raises(ValidationError):
        QuestionReadiness.model_validate(
            {
                "decision": "clarification_recommended",
                "reason": "主体で分かれる",
                "recommendation": None,
            }
        )


def test_question_readiness_contract_accepts_declarative_recommendation() -> None:
    result = QuestionReadiness.model_validate(
        {
            "decision": "clarification_recommended",
            "reason": "提出対象がなく検索対象を特定できないため。",
            "recommendation": "提出する文書を質問文に加えると検索精度が上がります。",
        }
    )

    assert result.decision == "clarification_recommended"
    assert result.recommendation.endswith("検索精度が上がります。")


def test_service_rejects_invalid_provider_output() -> None:
    client = FakeStructuredJSONClient(None, validation_error="invalid_json")

    with pytest.raises(QuestionReadinessModelProtocolError):
        QuestionReadinessService(client).check(
            QuestionReadinessRequest(question="質問です。")
        )


def test_question_readiness_endpoint_returns_validated_output(monkeypatch) -> None:
    expected = QuestionReadiness(
        decision="ready",
        reason="原文のまま調査できるため。",
        recommendation="要件を確認してください。",
    )
    monkeypatch.setattr(
        main.question_readiness_service,
        "check",
        lambda request: expected,
    )

    response = main.question_readiness(
        QuestionReadinessRequest(question="要件は何ですか。")
    )

    assert response == expected.model_dump(mode="json")


def test_question_readiness_endpoint_hides_model_protocol_details(monkeypatch) -> None:
    secret = "provider-key=do-not-leak"

    def fail(_request):
        raise QuestionReadinessModelProtocolError(secret)

    monkeypatch.setattr(main.question_readiness_service, "check", fail)

    with pytest.raises(HTTPException) as caught:
        main.question_readiness(
            QuestionReadinessRequest(question="要件は何ですか。")
        )

    assert caught.value.status_code == 502
    assert caught.value.detail == {
        "code": "question_readiness_model_protocol_error",
        "message": "質問確認モデルの応答を検証できませんでした。",
    }
    assert secret not in str(caught.value.detail)
