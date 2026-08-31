import pytest
from fastapi import HTTPException

from app import main
from app.models import AnswerRequest


def test_answer_error_does_not_expose_internal_exception(monkeypatch) -> None:
    secret = "provider-key=do-not-leak"

    def fail(_request):
        raise RuntimeError(secret)

    monkeypatch.setattr(main.framework_agent_service, "answer", fail)

    with pytest.raises(HTTPException) as caught:
        main.answer(AnswerRequest(question="質問"))

    assert caught.value.status_code == 500
    assert caught.value.detail == {
        "code": "answer_failed",
        "message": "回答処理に失敗しました。",
    }
    assert secret not in str(caught.value.detail)


def test_health_component_failure_changes_overall_status(monkeypatch) -> None:
    monkeypatch.setattr(main.os_client, "health", lambda: True)
    monkeypatch.setattr(main.graph_client, "health", lambda: True)
    monkeypatch.setattr(
        main.llm_client,
        "health",
        lambda: {"provider": "anthropic", "ok": False},
    )
    result = main.health()

    assert result["status"] == "degraded"
