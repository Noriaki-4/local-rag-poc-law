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
    assert QUESTION_READINESS_PROFILE_VERSION == "8"
    assert question_readiness_profile().name == QUESTION_READINESS_PROFILE_NAME
    assert question_readiness_profile().version == QUESTION_READINESS_PROFILE_VERSION
    assert rendered.stage == "question_readiness"
    assert rendered.input_payload == {
        "question": "会社が株式を買う場合の手続は何ですか。"
    }
    assert "# 法令調査Solver：検索単位の確認" in rendered.instructions
    assert "適用される規律を検索できる程度に対象を含めて特定" in rendered.instructions
    assert "相手方、対象又は条件" in rendered.instructions
    assert "回答として求めている未知事項" in rendered.instructions
    assert "安全に候補を作れる場合だけ" in rendered.instructions
    assert "`A / X`と`B / Y`" in rendered.instructions
    assert "`question`" in rendered.instructions
    assert "<question_readiness_input>" in rendered.request
    assert set(rendered.output_schema["properties"]) == {
        "decision",
        "reason",
        "clarification_question",
        "choices",
    }
    assert set(rendered.output_schema["required"]) == {
        "decision",
        "reason",
        "clarification_question",
        "choices",
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
        / "fixtures/model_call_artifacts/legal-question-readiness-v8/openai"
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
            "clarification_question": None,
            "choices": [],
        }
    )
    monkeypatch.setattr(
        "app.domains.legal.question_readiness.settings.agent_framework_research_model",
        "test-model",
    )

    result = QuestionReadinessService(client).check(
        QuestionReadinessRequest(
            question="会社が有価証券報告書を提出する期限はいつですか。"
        )
    )

    assert result.decision == "ready"
    assert result.clarification_question is None
    assert result.choices == []
    assert len(client.calls) == 1
    assert client.calls[0]["model"] == "test-model"
    assert "会社が有価証券報告書を提出する期限はいつですか。" in client.calls[0][
        "prompt"
    ]


def test_service_returns_distinct_refined_questions_for_clarification() -> None:
    client = FakeStructuredJSONClient(
        {
            "decision": "clarification_required",
            "reason": "取得者によって確認対象となる規律が分かれるため。",
            "clarification_question": "株式を取得するのは誰ですか。",
            "choices": [
                {
                    "choice_id": "issuer",
                    "label": "対象会社自身",
                    "refined_question": "対象会社自身が株式を取得する場合の手続は何ですか。",
                },
                {
                    "choice_id": "third_party",
                    "label": "第三者",
                    "refined_question": "第三者が対象会社の株式を取得する場合の手続は何ですか。",
                },
            ],
        }
    )

    result = QuestionReadinessService(client).check(
        QuestionReadinessRequest(question="会社が株式を買う場合の手続は何ですか。")
    )

    assert result.decision == "clarification_required"
    assert [choice.choice_id for choice in result.choices] == [
        "issuer",
        "third_party",
    ]


def test_contract_accepts_one_search_question_for_each_independent_pair() -> None:
    result = QuestionReadiness.model_validate(
        {
            "decision": "clarification_required",
            "reason": "二つの主体・行為ペアは検索を分ける必要があるため。",
            "clarification_question": "どちらを先に調べますか。",
            "choices": [
                {
                    "choice_id": "company_acquisition",
                    "label": "会社による株式取得",
                    "refined_question": (
                        "会社が株主から株式を取得する場合の手続を調べたい。"
                    ),
                },
                {
                    "choice_id": "shareholder_transfer",
                    "label": "株主による株式譲渡",
                    "refined_question": (
                        "株主が会社へ株式を譲渡する場合の手続を調べたい。"
                    ),
                },
            ],
        }
    )

    assert all(
        "手続を調べたい" in choice.refined_question for choice in result.choices
    )


def test_question_readiness_contract_rejects_clarification_on_ready() -> None:
    with pytest.raises(ValidationError, match="must not include clarification"):
        QuestionReadiness.model_validate(
            {
                "decision": "ready",
                "reason": "調査可能",
                "clarification_question": "誰ですか。",
                "choices": [],
            }
        )


def test_question_readiness_contract_rejects_duplicate_choice_ids() -> None:
    with pytest.raises(ValidationError, match="choice IDs must be unique"):
        QuestionReadiness.model_validate(
            {
                "decision": "clarification_required",
                "reason": "主体で分かれる",
                "clarification_question": "誰ですか。",
                "choices": [
                    {
                        "choice_id": "actor",
                        "label": "会社",
                        "refined_question": "会社が行う場合はどうか。",
                    },
                    {
                        "choice_id": "actor",
                        "label": "個人",
                        "refined_question": "個人が行う場合はどうか。",
                    },
                ],
            }
        )


def test_question_readiness_contract_accepts_clarification_without_choices() -> None:
    result = QuestionReadiness.model_validate(
        {
            "decision": "clarification_required",
            "reason": "提出対象がなく検索対象を特定できないため。",
            "clarification_question": "何を提出する場合について調べますか。",
            "choices": [],
        }
    )

    assert result.decision == "clarification_required"
    assert result.choices == []


def test_question_readiness_contract_rejects_single_choice() -> None:
    with pytest.raises(ValidationError, match="must be empty or include at least 2"):
        QuestionReadiness.model_validate(
            {
                "decision": "clarification_required",
                "reason": "確認が必要",
                "clarification_question": "確認してください。",
                "choices": [
                    {
                        "choice_id": "only",
                        "label": "唯一の候補",
                        "refined_question": "唯一の候補を調べる。",
                    }
                ],
            }
        )


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
        clarification_question=None,
        choices=[],
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
