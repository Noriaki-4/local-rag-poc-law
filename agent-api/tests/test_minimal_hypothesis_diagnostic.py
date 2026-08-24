from pathlib import Path

import pytest
from app.domains.legal.minimal_hypothesis_diagnostic import (
    MinimalHypothesisOutput,
    minimal_hypothesis_schema,
    render_minimal_hypothesis_call,
)


def test_minimal_hypothesis_call_contains_only_question_and_meaning_output() -> None:
    rendered = render_minimal_hypothesis_call("法的な確認事項は何か。")

    assert rendered.input_payload == {"question": "法的な確認事項は何か。"}
    assert set(rendered.output_schema["properties"]) == {"work_items"}
    work_item = rendered.output_schema["properties"]["work_items"]["items"]
    assert set(work_item["properties"]) == {"question", "hypotheses"}
    hypothesis = work_item["properties"]["hypotheses"]["items"]
    assert set(hypothesis["properties"]) == {"statement"}

    excluded_terms = (
        "ToolRequest",
        "gaps",
        "status",
        "Evidence",
        "CaseStore",
        "basis_hypothesis_ids",
        "start_next_cycle",
    )
    assert all(term not in rendered.request for term in excluded_terms)


def test_minimal_hypothesis_schema_matches_pydantic_contract() -> None:
    payload = {
        "work_items": [
            {
                "question": "許可が必要になる条件は何か。",
                "hypotheses": [
                    {"statement": "一定の行為と規模を満たす場合に許可が必要になる。"}
                ],
            }
        ]
    }

    assert minimal_hypothesis_schema()["required"] == ["work_items"]
    assert MinimalHypothesisOutput.model_validate(payload).work_items[0].question


def test_minimal_hypothesis_call_rejects_empty_question() -> None:
    with pytest.raises(ValueError, match="question must not be empty"):
        render_minimal_hypothesis_call("  ")


def test_minimal_hypothesis_prompt_is_external_asset() -> None:
    prompt_path = (
        Path(__file__).parents[1]
        / "app/domains/legal/prompts/minimal_hypothesis_diagnostic.md"
    )

    assert prompt_path.is_file()
    assert "{{runtime_input}}" in prompt_path.read_text(encoding="utf-8")
