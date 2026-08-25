from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.domains.legal.action_actor_only_diagnostic import (
    render_action_actor_only_call,
    run_action_actor_only_diagnostic,
)
from app.llm import StructuredJSONResult


class FakeStructuredJSONClient:
    provider = "openai"

    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def generate_structured_json(self, **kwargs: Any) -> StructuredJSONResult:
        self.calls.append(kwargs)
        return StructuredJSONResult(
            payload=self.payload,
            provider=self.provider,
            model=kwargs["model"],
            latencyMs=1,
            inputTokens=100,
            outputTokens=50,
            validationError=None,
            stopReason="stop",
        )


def _fixture() -> dict[str, Any]:
    path = (
        Path(__file__).parent
        / "fixtures/framework/action_actor_only_selection_v1.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_input_has_no_target_actor_or_actor_relation() -> None:
    rendered = render_action_actor_only_call(_fixture()["cases"])

    serialized = json.dumps(rendered.input_payload, ensure_ascii=False)
    assert "action_actor" in serialized
    assert "target_actor" not in serialized
    assert "actor_relation" not in serialized


def test_run_calls_model_once_and_accepts_known_partition() -> None:
    fixture = _fixture()
    payload = {
        "cases": [
            {
                "case_id": "tob-buyer-vs-issuer",
                "selected_article_ids": ["fiea-27-2"],
                "deferred_article_ids": ["fiea-27-22-2"],
                "reason": "買付者を規律する候補を選んだ。",
            },
            {
                "case_id": "developer-vs-landowner",
                "selected_article_ids": ["developer-permit"],
                "deferred_article_ids": ["landowner-duty"],
                "reason": "開発事業者を規律する候補を選んだ。",
            },
        ]
    }
    client = FakeStructuredJSONClient(payload)

    run = run_action_actor_only_diagnostic(
        render_action_actor_only_call(fixture["cases"]),
        model="gpt-4o-mini",
        max_tokens=1024,
        timeout_sec=30,
        client=client,
    )

    assert len(client.calls) == 1
    assert run.validation_error is None
    assert run.output is not None
    actual = {
        item.case_id: list(item.selected_article_ids)
        for item in run.output.cases
    }
    assert actual == fixture["expected"]
