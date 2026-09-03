from __future__ import annotations

import pytest

from app.agent_framework.contracts import HypothesisGapAddition, HypothesisUpdate
from app.agent_framework.state import (
    apply_hypothesis_gap_diff,
    structured_hypothesis_gaps,
)


def test_gap_diff_preserves_adds_resolves_and_discards() -> None:
    existing = ("維持する事項", "解消する事項", "破棄する事項")
    structured = structured_hypothesis_gaps("h-1", existing)

    updated = apply_hypothesis_gap_diff(
        "h-1",
        existing,
        ("追加する事項",),
        (structured[1].gap_id,),
        (structured[2].gap_id,),
    )

    assert updated == ("維持する事項", "追加する事項")


def test_gap_diff_rejects_unknown_resolution_id() -> None:
    with pytest.raises(ValueError, match="unknown hypothesis gap"):
        apply_hypothesis_gap_diff("h-1", ("事項",), (), ("gap-unknown",))


def test_gap_diff_rejects_unknown_discard_id() -> None:
    with pytest.raises(ValueError, match="unknown hypothesis gap"):
        apply_hypothesis_gap_diff(
            "h-1", ("事項",), (), (), ("gap-unknown",)
        )


def test_gap_diff_rewords_by_discarding_then_adding() -> None:
    existing = ("具体的な条件",)
    old_gap_id = structured_hypothesis_gaps("h-1", existing)[0].gap_id

    updated = apply_hypothesis_gap_diff(
        "h-1",
        existing,
        ("適用対象となる者の要件",),
        (),
        (old_gap_id,),
    )

    assert updated == ("適用対象となる者の要件",)


def test_hypothesis_update_uses_structured_gap_diffs() -> None:
    schema = HypothesisUpdate.model_json_schema()
    properties = schema["properties"]

    assert "gaps" not in properties
    assert {
        "add_gaps",
        "discard_gap_ids",
        "resolve_gap_ids",
    }.issubset(properties)
    update = HypothesisUpdate(
        hypothesis_id="h-1",
        judgment="unresolved",
        add_gaps=(HypothesisGapAddition(description="追加する事項"),),
    )
    assert update.add_gaps[0].description == "追加する事項"


def test_hypothesis_update_rejects_resolving_and_discarding_same_gap() -> None:
    with pytest.raises(ValueError, match="both resolved and discarded"):
        HypothesisUpdate(
            hypothesis_id="h-1",
            judgment="unresolved",
            resolve_gap_ids=("gap-1",),
            discard_gap_ids=("gap-1",),
        )


def test_legacy_gap_replacement_remains_read_compatible() -> None:
    update = HypothesisUpdate.model_validate(
        {
            "hypothesis_id": "h-1",
            "judgment": "unresolved",
            "gaps": ["旧保存形式の事項"],
        }
    )

    assert update.legacy_replacement_gaps == ("旧保存形式の事項",)
