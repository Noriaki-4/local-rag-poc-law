from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.adapters.models.structured_json import (
    _normalize_observation_integration_payload,
    _observation_work_item_contexts,
    render_observation_integration_model_call,
    render_hypothesis_revision_model_call,
)
from app.agent_framework.context import SolverContext, build_solver_context
from app.agent_framework.contracts import (
    HypothesisGapAddition,
    HypothesisRevisionDecision,
    HypothesisRevisionUpdate,
)
from app.agent_framework.ports.model import ModelProtocolError
from app.agent_framework.profiles import AgentLimits
from app.agent_framework.state import (
    CaseState,
    Evidence,
    Hypothesis,
    WorkItem,
    apply_hypothesis_gap_diff,
    structured_hypothesis_gaps,
)
from app.agent_framework.validation import apply_hypothesis_revision
from app.domains.legal.profiles import legal_agent_profile


def _observation_context() -> SolverContext:
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures/framework/"
            "tob_overview_cycle1_three_articles_before_cycle_close_v1.json"
        ).read_text(encoding="utf-8")
    )
    context = _observation_work_item_contexts(
        SolverContext.model_validate(fixture["solverContext"])
    )[0]
    hypothesis = context.hypotheses[0].model_copy(
        update={
            "gaps": (
                "公告方法について府令が定める具体的内容",
                "公告不能時の周知方法",
            )
        }
    )
    return context.model_copy(update={"hypotheses": (hypothesis,)})


def test_gap_diff_preserves_adds_resolves_and_rewords() -> None:
    hypothesis_id = "h-1"
    existing = ("既存の未確認事項", "文言を直す未確認事項")
    structured = structured_hypothesis_gaps(hypothesis_id, existing)

    updated = apply_hypothesis_gap_diff(
        hypothesis_id,
        existing,
        ("新しい未確認事項", "修正後の未確認事項", "既存の未確認事項"),
        (structured[1].gap_id,),
    )

    assert updated == (
        "既存の未確認事項",
        "新しい未確認事項",
        "修正後の未確認事項",
    )
    assert structured_hypothesis_gaps(hypothesis_id, updated)[0].gap_id == (
        structured[0].gap_id
    )


def test_gap_diff_rejects_unknown_resolution_id() -> None:
    with pytest.raises(ValueError, match="unknown hypothesis gap IDs"):
        apply_hypothesis_gap_diff(
            "h-1",
            ("既存の未確認事項",),
            (),
            ("gap-unknown",),
        )


def test_observation_contract_uses_structured_gap_diffs() -> None:
    context = _observation_context()
    profile = legal_agent_profile().solver_observation_integration
    assert profile is not None

    rendered = render_observation_integration_model_call(context, profile)

    gaps = rendered.input_payload["hypotheses"][0]["gaps"]
    assert [item["description"] for item in gaps] == [
        "公告方法について府令が定める具体的内容",
        "公告不能時の周知方法",
    ]
    assert all(item["gap_id"].startswith("gap-") for item in gaps)
    update = rendered.output_schema["properties"]["update_hypotheses"][
        "items"
    ]["properties"]
    assert "gaps" not in update
    assert set(update) == {
        "hypothesis_id",
        "judgment",
        "evidence_ids",
        "add_gaps",
        "resolve_gap_ids",
    }
    assert set(update["resolve_gap_ids"]["items"]["enum"]) == {
        item["gap_id"] for item in gaps
    }


def test_observation_reprojects_prior_evidence_while_gap_remains() -> None:
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures/framework/"
            "tob_overview_cycle1_three_articles_before_cycle_close_v1.json"
        ).read_text(encoding="utf-8")
    )
    context = SolverContext.model_validate(fixture["solverContext"])
    prior = Evidence(
        evidence_id="prior-grounding-evidence",
        source_ref="fixture:prior",
        content="既に確認済みの起点規定本文。",
        created_cycle=1,
        metadata={"articleId": "law-prior-article-1"},
    )
    first_hypothesis = context.hypotheses[0].model_copy(
        update={
            "evidence_ids": (prior.evidence_id,),
            "gaps": ("起点規定と新規本文を組み合わせて確認する事項",),
        }
    )
    context = context.model_copy(
        update={
            "hypotheses": (first_hypothesis, *context.hypotheses[1:]),
            "material_evidence": (*context.material_evidence, prior),
        }
    )

    projected = {
        item.work_tree[0].work_item_id: item
        for item in _observation_work_item_contexts(context)
    }[first_hypothesis.work_item_id]

    assert prior.evidence_id in projected.grounding_evidence_ids
    assert prior.evidence_id in {
        item.evidence_id for item in projected.material_evidence
    }

    resolved_context = context.model_copy(
        update={
            "hypotheses": (
                first_hypothesis.model_copy(update={"gaps": ()}),
                *context.hypotheses[1:],
            )
        }
    )
    resolved_projection = {
        item.work_tree[0].work_item_id: item
        for item in _observation_work_item_contexts(resolved_context)
    }[first_hypothesis.work_item_id]

    assert prior.evidence_id not in resolved_projection.grounding_evidence_ids


def test_legacy_gap_replacement_is_normalized_to_diff() -> None:
    context = _observation_context()
    hypothesis = context.hypotheses[0]
    old_gaps = structured_hypothesis_gaps(
        hypothesis.hypothesis_id,
        hypothesis.gaps,
    )
    payload = {
        "decision_reason": "既存gapを一つ残し、新しいgapを追加する。",
        "update_hypotheses": [
            {
                "hypothesis_id": hypothesis.hypothesis_id,
                "judgment": "unresolved",
                "evidence_ids": [],
                "gaps": [hypothesis.gaps[0], "新しい未確認事項"],
            }
        ],
        "dependency_decisions": [],
        "tool_requests": [],
    }

    normalized = _normalize_observation_integration_payload(
        payload,
        context=context,
    )
    update = normalized["update_hypotheses"][0]

    assert update["resolve_gap_ids"] == [old_gaps[1].gap_id]
    assert update["add_gaps"] == [{"description": "新しい未確認事項"}]
    assert "gaps" not in update


def test_legacy_and_new_gap_updates_cannot_be_mixed() -> None:
    context = _observation_context()
    hypothesis_id = context.hypotheses[0].hypothesis_id
    with pytest.raises(ModelProtocolError, match="cannot mix legacy gaps"):
        _normalize_observation_integration_payload(
            {
                "update_hypotheses": [
                    {
                        "hypothesis_id": hypothesis_id,
                        "gaps": [],
                        "add_gaps": [],
                    }
                ]
            },
            context=context,
        )


def _revision_state() -> CaseState:
    return CaseState(
        case_id="revision-gap-case",
        question="未確認事項を見直す。",
        research_cycle_count=1,
        work_items=(WorkItem(work_item_id="wi-1", question="確認事項"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h-1",
                work_item_id="wi-1",
                statement="更新前の命題",
                judgment="contradicted",
                evidence_ids=("e-1",),
                gaps=("維持する未確認事項", "解消する未確認事項"),
            ),
        ),
        evidence=(
            Evidence(
                evidence_id="e-1",
                source_ref="source-1",
                content="命題を見直す根拠本文",
                created_cycle=1,
            ),
        ),
    )


def test_hypothesis_revision_uses_the_same_gap_diff_contract() -> None:
    state = _revision_state()
    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    profile = legal_agent_profile().solver_hypothesis_revision
    assert profile is not None

    rendered = render_hypothesis_revision_model_call(context, profile)

    gaps = rendered.input_payload["hypotheses"][0]["gaps"]
    assert [item["description"] for item in gaps] == list(
        state.hypotheses[0].gaps
    )
    revision = rendered.output_schema["properties"]["revise_hypotheses"][
        "items"
    ]["properties"]
    assert "gaps" not in revision
    assert {"add_gaps", "resolve_gap_ids"}.issubset(revision)
    assert set(revision["resolve_gap_ids"]["items"]["enum"]) == {
        item["gap_id"] for item in gaps
    }


def test_hypothesis_revision_preserves_gap_history_and_applies_content_diff() -> None:
    state = _revision_state()
    old_gaps = structured_hypothesis_gaps("h-1", state.hypotheses[0].gaps)
    revision = HypothesisRevisionDecision(
        decision_reason="本文に合わせて命題と未確認事項を見直す。",
        revise_hypotheses=(
            HypothesisRevisionUpdate(
                hypothesis_id="h-1",
                statement="更新後の命題",
                judgment="unresolved",
                evidence_ids=("e-1",),
                add_gaps=(
                    HypothesisGapAddition(description="追加する未確認事項"),
                ),
                resolve_gap_ids=(old_gaps[1].gap_id,),
            ),
        ),
    )

    updated = apply_hypothesis_revision(
        state,
        revision,
        material_evidence_ids={"e-1"},
        eligible_work_item_ids={"wi-1"},
        eligible_hypothesis_ids={"h-1"},
    )

    assert updated.hypotheses[0].gaps == (
        "維持する未確認事項",
        "追加する未確認事項",
    )
    assert updated.hypothesis_history[0].hypothesis.gaps == (
        "維持する未確認事項",
        "解消する未確認事項",
    )
