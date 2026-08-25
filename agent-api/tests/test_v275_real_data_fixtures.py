from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agent_framework.context import SolverContext
from app.agent_framework.state import CaseState


_FIXTURE_DIR = Path(__file__).parent / "fixtures/framework"


@pytest.mark.parametrize(
    "fixture_name",
    [
        "tob_announcement_final_answer_incomplete_v275.json",
        "tob_exceptions_cycle2_finalize_tool_conflict_v275.json",
        "tob_overview_issuer_actor_mismatch_v275.json",
        "tob_overview_cycle2_finalize_tool_conflict_v275.json",
    ],
)
def test_v275_real_data_fixture_contracts_are_loadable(fixture_name: str) -> None:
    fixture = json.loads((_FIXTURE_DIR / fixture_name).read_text(encoding="utf-8"))

    assert fixture["source"]["profileVersion"] == "275"
    assert fixture["source"]["model"] == "gpt-4o-mini-2024-07-18"
    CaseState.model_validate(fixture["caseState"])
    SolverContext.model_validate(fixture["solverContext"])


def test_v275_announcement_fixture_preserves_incomplete_answer() -> None:
    fixture = json.loads(
        (_FIXTURE_DIR / "tob_announcement_final_answer_incomplete_v275.json")
        .read_text(encoding="utf-8")
    )
    answer = fixture["observedTransportOutput"]["payload"]["text"]

    assert "買付けの目的" in answer
    assert "氏名又は名称" not in answer
    assert "縦覧に供する場所" not in answer


@pytest.mark.parametrize(
    "fixture_name",
    [
        "tob_exceptions_cycle2_finalize_tool_conflict_v275.json",
        "tob_overview_cycle2_finalize_tool_conflict_v275.json",
    ],
)
def test_v275_cycle2_fixture_preserves_finalize_tool_conflict(
    fixture_name: str,
) -> None:
    fixture = json.loads((_FIXTURE_DIR / fixture_name).read_text(encoding="utf-8"))
    output = fixture["observedTransportOutput"]

    assert output["payload"]["next"] == "finalize"
    assert output["payload"]["answer"] is None
    assert output["payload"]["tool_requests"]
    assert "finalize decision requires an answer" in output["validationError"]


def test_v275_overview_fixture_preserves_issuer_actor_mismatch() -> None:
    fixture = json.loads(
        (_FIXTURE_DIR / "tob_overview_issuer_actor_mismatch_v275.json")
        .read_text(encoding="utf-8")
    )
    article_ids = {
        item["article_id"]
        for item in fixture["observedTransportOutput"]["payload"]["selections"]
    }

    assert "law-323AC0000000025-article-27_22_2" in article_ids
    assert "law-323AC0000000025-article-27_2" not in article_ids
