from __future__ import annotations

from typing import Any

import pytest

from app.domains.legal.search_planning_diagnostic import (
    build_search_planning_context,
    render_search_planning_call,
    run_search_planning_diagnostic,
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


_WORK_ITEMS = [
    {
        "work_item_id": "wi-1",
        "question": "許可が必要になる条件は何か。",
        "action_actor": "申請者",
    }
]
_HYPOTHESES = [
    {
        "hypothesis_id": "h-1",
        "work_item_id": "wi-1",
        "statement": "対象事業の規模が許可要否を決める。",
    }
]


def test_render_uses_production_search_prompt_tool_and_schema() -> None:
    rendered, context = render_search_planning_call(
        "事業の許可条件を説明してください。",
        _WORK_ITEMS,
        _HYPOTHESES,
        provider="openai",
        model="gpt-4o-mini",
    )

    assert rendered.stage.endswith("research_search")
    assert "# 法令調査Solver：検索要求の作成" in rendered.instructions
    assert "## 出力前の確認" in rendered.instructions
    assert set(rendered.input_payload) == {
        "question",
        "work_items",
        "hypotheses",
        "available_tools",
        "max_tool_requests_per_step",
    }
    assert rendered.input_payload["hypotheses"][0]["hypothesis_id"] == "h-1"
    assert rendered.input_payload["available_tools"][0]["name"] == "legal_search"
    assert context.available_tools[0].name == "legal_search"
    assert "`gaps`がある場合" in rendered.instructions
    assert "別々の検索にすることは強制しません" in rendered.instructions
    assert "Hypothesisまたは`gaps`を検証" not in rendered.instructions
    request_properties = rendered.output_schema["properties"]["search_requests"][
        "items"
    ]["properties"]
    assert set(request_properties) == {
        "work_item_id",
        "hypothesis_ids",
        "purpose",
        "query",
        "doc_types",
    }
    assert "説明する文章" in request_properties["purpose"]["description"]
    assert "短い法令用語・法令表現" in request_properties["query"]["description"]
    tool_query = rendered.input_payload["available_tools"][0]["input_schema"][
        "properties"
    ]["query"]
    assert "短い法令用語・法令表現" in tool_query["description"]


def test_run_calls_model_once_and_normalizes_legal_search_request() -> None:
    client = FakeStructuredJSONClient(
        {
            "search_requests": [
                {
                    "work_item_id": "wi-1",
                    "hypothesis_ids": ["h-1"],
                    "purpose": "許可対象となる事業規模を確認する。",
                    "query": "事業 規模 許可 要件",
                    "doc_types": ["law"],
                }
            ]
        }
    )

    run = run_search_planning_diagnostic(
        "事業の許可条件を説明してください。",
        _WORK_ITEMS,
        _HYPOTHESES,
        provider="openai",
        model="gpt-4o-mini",
        max_tokens=1000,
        timeout_sec=30,
        client=client,
    )

    assert len(client.calls) == 1
    assert run.validation_error is None
    assert run.decision is not None
    request = run.decision.tool_requests[0]
    assert request.request_id.startswith("solver-tool-")
    assert request.tool_name == "legal_search"
    assert request.work_item_id == "wi-1"
    assert request.hypothesis_ids == ("h-1",)
    assert request.arguments == {
        "query": "事業 規模 許可 要件",
        "doc_types": ["law"],
        "document_ids": [],
    }


def test_context_rejects_hypothesis_for_unknown_work_item() -> None:
    with pytest.raises(ValueError, match="unknown WorkItem IDs"):
        build_search_planning_context(
            "事業の許可条件を説明してください。",
            _WORK_ITEMS,
            [
                {
                    "hypothesis_id": "h-unknown",
                    "work_item_id": "wi-unknown",
                    "statement": "許可要件がある。",
                }
            ],
        )
