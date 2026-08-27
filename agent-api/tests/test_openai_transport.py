from types import SimpleNamespace

import pytest
from app.llm import LLMClient, _openai_finish_reason, _to_openai_schema

SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "note": {"type": ["string", "null"], "default": None},
    },
    "required": ["ok"],
    "additionalProperties": False,
}


def test_openai_json_transport_uses_chat_completions_structured_outputs(
    monkeypatch,
) -> None:
    captured: dict = {}

    class FakeResponse:
        ok = True
        status_code = 200

        def json(self) -> dict:
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"ok":true,"note":null}',
                            "refusal": None,
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 7},
            }

    def fake_post(url, *, headers, json, timeout):
        captured.update(
            {"url": url, "headers": headers, "payload": json, "timeout": timeout}
        )
        return FakeResponse()

    monkeypatch.setattr("app.llm.settings.openai_api_key", "test-key")
    monkeypatch.setattr("app.llm.requests.post", fake_post)

    result = LLMClient(provider="openai")._openai_json(
        "prompt", SCHEMA, "gpt-4o-mini", 4096, 30
    )

    assert result[0] == '{"ok":true,"note":null}'
    assert result[2:] == (12, 7, "stop")
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer test-key"
    assert captured["timeout"] == 30
    assert captured["payload"]["model"] == "gpt-4o-mini"
    assert captured["payload"]["max_completion_tokens"] == 4096
    assert captured["payload"]["temperature"] == 0
    assert captured["payload"]["store"] is False
    response_format = captured["payload"]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"]["required"] == ["ok", "note"]


def test_openai_transport_clamps_output_tokens_to_model_limit(monkeypatch) -> None:
    captured: dict = {}

    def fake_post(url, *, headers, json, timeout):
        captured["max_tokens"] = json["max_completion_tokens"]
        return SimpleNamespace(
            ok=True,
            status_code=200,
            json=lambda: {
                "choices": [
                    {
                        "message": {"content": '{"ok":true,"note":null}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {},
            },
        )

    monkeypatch.setattr("app.llm.settings.openai_api_key", "test-key")
    monkeypatch.setattr("app.llm.settings.openai_max_tokens_ceiling", 16384)
    monkeypatch.setattr("app.llm.requests.post", fake_post)

    LLMClient(provider="openai")._openai_json(
        "prompt", SCHEMA, "gpt-4o-mini", 32768, 30
    )

    assert captured["max_tokens"] == 16384


def test_openai_transport_omits_unsupported_temperature_for_gpt5(
    monkeypatch,
) -> None:
    captured: dict = {}

    def fake_post(url, *, headers, json, timeout):
        captured["payload"] = json
        return SimpleNamespace(
            ok=True,
            status_code=200,
            json=lambda: {
                "choices": [
                    {
                        "message": {"content": '{"ok":true,"note":null}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {},
            },
        )

    monkeypatch.setattr("app.llm.settings.openai_api_key", "test-key")
    monkeypatch.setattr("app.llm.settings.openai_reasoning_effort", "low")
    monkeypatch.setattr("app.llm.requests.post", fake_post)

    LLMClient(provider="openai")._openai_json(
        "prompt", SCHEMA, "gpt-5.6-luna", 4096, 30
    )

    assert "temperature" not in captured["payload"]
    assert captured["payload"]["reasoning_effort"] == "low"


def test_openai_transport_requires_api_key(monkeypatch) -> None:
    monkeypatch.setattr("app.llm.settings.openai_api_key", None)

    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        LLMClient(provider="openai")._openai_json(
            "prompt", SCHEMA, "gpt-4o-mini", 1024, 30
        )


def test_openai_health_checks_each_configured_api_model_once(monkeypatch) -> None:
    requested_urls: list[str] = []

    def fake_get(url, *, headers, timeout):
        requested_urls.append(url)
        assert headers["authorization"] == "Bearer test-key"
        assert timeout == 5
        return SimpleNamespace(ok=True)

    monkeypatch.setattr("app.llm.settings.openai_api_key", "test-key")
    for setting_name in (
        "answer_model",
        "reviewer_model",
        "planner_model",
        "evaluator_model",
        "llm_research_stage_model",
        "llm_research_integration_model",
    ):
        monkeypatch.setattr(f"app.llm.settings.{setting_name}", "gpt-4o-mini")
    monkeypatch.setattr("app.llm.settings.relation_classifier_provider", "ollama")
    monkeypatch.setattr("app.llm.settings.openai_reasoning_effort", "low")
    monkeypatch.setattr("app.llm.requests.get", fake_get)

    health = LLMClient(provider="openai").health()

    assert health["ok"] is True
    assert requested_urls == ["https://api.openai.com/v1/models/gpt-4o-mini"]
    assert health["modelChecks"] == [{"model": "gpt-4o-mini", "available": True}]
    assert health["reasoningEffort"] == "low"


def test_openai_schema_makes_optional_properties_required_and_nullable() -> None:
    converted = _to_openai_schema(SCHEMA)

    assert converted["required"] == ["ok", "note"]
    assert converted["additionalProperties"] is False
    assert converted["properties"]["note"] == {
        "anyOf": [{"type": "string"}, {"type": "null"}]
    }
    assert "default" not in str(converted)


def test_openai_length_finish_reason_uses_common_token_limit_status() -> None:
    assert (
        _openai_finish_reason({"choices": [{"finish_reason": "length"}]})
        == "max_tokens"
    )
