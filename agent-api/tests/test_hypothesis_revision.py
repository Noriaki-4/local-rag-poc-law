from __future__ import annotations

import pytest

from app.adapters.models import StructuredJSONModelAdapter
from app.adapters.models.structured_json import render_hypothesis_revision_model_call
from app.agent_framework.context import build_solver_context
from app.agent_framework.contracts import (
    HypothesisRevisionDecision,
    HypothesisRevisionProposal,
)
from app.agent_framework.profiles import AgentLimits
from app.agent_framework.state import CaseState, Evidence, Hypothesis, WorkItem
from app.agent_framework.validation import ContractViolation, apply_hypothesis_revision
from app.domains.legal.profiles import legal_agent_profile
from app.llm import StructuredJSONResult


def _state(*, resolved: bool = False) -> CaseState:
    return CaseState(
        case_id="revision-case",
        question="確認事項",
        research_cycle_count=1,
        work_items=(
            WorkItem(
                work_item_id="wi-1",
                question="既存の確認事項",
                state="resolved" if resolved else "open",
                resolution="確認済み" if resolved else None,
                basis_hypothesis_ids=("h-1",) if resolved else (),
            ),
        ),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h-1",
                work_item_id="wi-1",
                statement="既存の命題",
                judgment="contradicted",
                evidence_ids=("e-1",),
            ),
        ),
        evidence=(
            Evidence(
                evidence_id="e-1",
                source_ref="source-1",
                content="反証した本文",
                created_cycle=1,
            ),
        ),
    )


def _replacement() -> HypothesisRevisionDecision:
    return HypothesisRevisionDecision(
        decision_reason="本文から命題を見直す",
        add_hypotheses=(
            HypothesisRevisionProposal(
                hypothesis_id="h-2",
                work_item_id="wi-1",
                replaces_hypothesis_id="h-1",
                statement="本文を踏まえた別の命題",
                gaps=("具体的条件",),
            ),
        ),
    )


def test_revision_adds_replacement_without_overwriting_or_evidence_inheritance() -> None:
    state = _state(resolved=True)

    updated = apply_hypothesis_revision(
        state,
        _replacement(),
        material_evidence_ids={"e-1"},
        eligible_work_item_ids={"wi-1"},
        eligible_hypothesis_ids={"h-1"},
    )

    assert [item.hypothesis_id for item in updated.hypotheses] == ["h-1", "h-2"]
    assert updated.hypotheses[0] == state.hypotheses[0]
    assert updated.hypotheses[1].replaces_hypothesis_id == "h-1"
    assert updated.hypotheses[1].judgment == "unresolved"
    assert updated.hypotheses[1].evidence_ids == ()
    assert updated.work_items[0].state == "open"

    context = build_solver_context(
        updated,
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    assert [item.hypothesis_id for item in context.hypotheses] == ["h-2"]


def test_revision_updates_an_open_work_items_existing_basis_to_the_replacement() -> None:
    state = _state().model_copy(
        update={
            "work_items": (
                _state().work_items[0].model_copy(
                    update={"basis_hypothesis_ids": ("h-1",)}
                ),
            )
        }
    )

    updated = apply_hypothesis_revision(
        state,
        _replacement(),
        material_evidence_ids={"e-1"},
        eligible_work_item_ids={"wi-1"},
        eligible_hypothesis_ids={"h-1"},
    )

    assert updated.work_items[0].basis_hypothesis_ids == ("h-2",)


def test_revision_can_add_an_independent_hypothesis() -> None:
    decision = HypothesisRevisionDecision(
        decision_reason="独立命題を追加する",
        add_hypotheses=(
            HypothesisRevisionProposal(
                hypothesis_id="h-2",
                work_item_id="wi-1",
                statement="独立して確認する命題",
                gaps=("要件",),
            ),
        ),
    )

    updated = apply_hypothesis_revision(
        _state(),
        decision,
        material_evidence_ids={"e-1"},
        eligible_work_item_ids={"wi-1"},
        eligible_hypothesis_ids={"h-1"},
    )

    assert updated.hypotheses[1].replaces_hypothesis_id is None


def test_revision_rejects_cross_work_item_replacement() -> None:
    state = _state().model_copy(
        update={
            "work_items": (
                *_state().work_items,
                WorkItem(work_item_id="wi-2", question="別の事項"),
            )
        }
    )
    decision = HypothesisRevisionDecision(
        decision_reason="不正な置換",
        add_hypotheses=(
            HypothesisRevisionProposal(
                hypothesis_id="h-2",
                work_item_id="wi-2",
                replaces_hypothesis_id="h-1",
                statement="別WorkItemの命題",
            ),
        ),
    )

    with pytest.raises(ContractViolation, match="same WorkItem"):
        apply_hypothesis_revision(
            state,
            decision,
            material_evidence_ids={"e-1"},
            eligible_work_item_ids={"wi-1", "wi-2"},
            eligible_hypothesis_ids={"h-1"},
        )


def test_revision_rejects_duplicate_statement_with_a_new_id() -> None:
    decision = HypothesisRevisionDecision(
        decision_reason="同じ命題を追加する",
        add_hypotheses=(
            HypothesisRevisionProposal(
                hypothesis_id="h-2",
                work_item_id="wi-1",
                replaces_hypothesis_id="h-1",
                statement="既存の命題",
            ),
        ),
    )
    with pytest.raises(ContractViolation, match="duplicates"):
        apply_hypothesis_revision(
            _state(),
            decision,
            material_evidence_ids={"e-1"},
            eligible_work_item_ids={"wi-1"},
            eligible_hypothesis_ids={"h-1"},
        )


def test_revision_prompt_contract_only_adds_new_hypotheses() -> None:
    context = build_solver_context(
        _state(),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    profile = legal_agent_profile().solver_hypothesis_revision
    assert profile is not None

    rendered = render_hypothesis_revision_model_call(context, profile)

    assert set(rendered.output_schema["properties"]) == {
        "decision_reason",
        "add_hypotheses",
    }
    proposal = rendered.output_schema["properties"]["add_hypotheses"]["items"]
    assert "hypothesis_id" not in proposal["properties"]
    assert "replaces_hypothesis_id" in proposal["properties"]
    assert "evidence_ids" not in proposal["properties"]


def test_revision_adapter_assigns_an_id_unused_by_the_whole_case() -> None:
    base = _state()
    state = base.model_copy(
        update={
            "work_items": (
                *base.work_items,
                WorkItem(work_item_id="wi-2", question="別の確認事項"),
            ),
            "hypotheses": (
                *base.hypotheses,
                Hypothesis(
                    hypothesis_id="h-2",
                    work_item_id="wi-2",
                    statement="別の命題",
                ),
            ),
        }
    )
    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    profile = legal_agent_profile().solver_hypothesis_revision
    assert profile is not None

    class RevisionLLM:
        provider = "openai"

        def generate_structured_json(self, **kwargs) -> StructuredJSONResult:
            proposal = kwargs["schema"]["properties"]["add_hypotheses"]["items"]
            assert "hypothesis_id" not in proposal["properties"]
            return StructuredJSONResult(
                payload={
                    "decision_reason": "反証された命題を置き換える。",
                    "add_hypotheses": [
                        {
                            "work_item_id": "wi-1",
                            "replaces_hypothesis_id": "h-1",
                            "statement": "本文を踏まえた別の命題",
                            "gaps": ["具体的条件"],
                        }
                    ],
                },
                provider=self.provider,
                model=kwargs["model"],
                latencyMs=1,
                inputTokens=1,
                outputTokens=1,
            )

    result = StructuredJSONModelAdapter(RevisionLLM()).solve(context, profile)

    assert result.hypothesis_revision is not None
    assert result.hypothesis_revision.add_hypotheses[0].hypothesis_id == "h-3"
