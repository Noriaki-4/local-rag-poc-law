from __future__ import annotations

from typing import Any

from app.adapters.models.structured_json import (
    StructuredJSONModelAdapter,
    render_hypothesis_revision_model_call,
)
from app.agent_framework.context import build_solver_context
from app.agent_framework.profiles import AgentLimits
from app.agent_framework.state import CaseState, Evidence, Hypothesis, WorkItem
from app.agent_framework.contracts import CaseUpdate, SolverDecision
from app.agent_framework.validation import apply_solver_decision
from app.domains.legal.profiles import legal_agent_profile
from app.llm import StructuredJSONResult


class FakeRevisionClient:
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
            inputTokens=1,
            outputTokens=1,
            validationError=None,
            retryCount=0,
            stopReason="stop",
        )


class FakeRevisionSequenceClient(FakeRevisionClient):
    def __init__(self, payloads: list[dict[str, Any]]):
        super().__init__({})
        self.payloads = list(payloads)

    def generate_structured_json(self, **kwargs: Any) -> StructuredJSONResult:
        self.payload = self.payloads.pop(0)
        return super().generate_structured_json(**kwargs)


def _context(*, resolved: bool = False):
    return build_solver_context(
        CaseState(
            case_id="revision-case",
            question="確認事項",
            research_cycle_count=1,
            work_items=(
                WorkItem(
                    work_item_id="wi-1",
                    question="既存の確認事項",
                    state="resolved" if resolved else "open",
                    resolution="既存命題を確認済み" if resolved else None,
                    basis_hypothesis_ids=("h-1",) if resolved else (),
                ),
            ),
            hypotheses=(
                Hypothesis(
                    hypothesis_id="h-1",
                    work_item_id="wi-1",
                    statement="既存の命題",
                    judgment="supported" if resolved else "unresolved",
                    evidence_ids=("e-1",) if resolved else (),
                ),
            ),
            evidence=(
                Evidence(
                    evidence_id="e-1",
                    source_ref="source-1",
                    content="Cycleで取得した本文",
                    created_cycle=1,
                ),
            ),
        ),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )


def test_revision_adds_child_and_reopens_resolved_work_item() -> None:
    client = FakeRevisionClient(
        {
            "decision_reason": "本文に別の未確認事項がある",
            "add_hypotheses": [
                {
                    "hypothesis_id": "h-2",
                    "work_item_id": "wi-1",
                    "statement": "本文から判明した別の命題",
                    "evidence_ids": ["e-1"],
                    "gaps": ["追加確認事項"],
                }
            ],
        }
    )
    context = _context(resolved=True)
    profile = legal_agent_profile().solver_hypothesis_revision
    assert profile is not None

    result = StructuredJSONModelAdapter(client).solve(context, profile)
    assert result.hypothesis_revision is not None
    updated = apply_solver_decision(
        CaseState(
            case_id="revision-case",
            question="確認事項",
            research_cycle_count=1,
            work_items=(
                WorkItem(
                    work_item_id="wi-1",
                    question="既存の確認事項",
                    state="resolved",
                    resolution="既存命題を確認済み",
                    basis_hypothesis_ids=("h-1",),
                ),
            ),
            hypotheses=(
                Hypothesis(
                    hypothesis_id="h-1",
                    work_item_id="wi-1",
                    statement="既存の命題",
                    judgment="supported",
                    evidence_ids=("e-1",),
                ),
            ),
            evidence=(
                Evidence(
                    evidence_id="e-1",
                    source_ref="source-1",
                    content="Cycleで取得した本文",
                    created_cycle=1,
                ),
            ),
        ),
        SolverDecision(
            next="continue",
            decision_reason="x",
            update=CaseUpdate(
                add_hypotheses=tuple(
                    Hypothesis(
                        hypothesis_id=item.hypothesis_id,
                        work_item_id=item.work_item_id,
                        statement=item.statement,
                        evidence_ids=item.evidence_ids,
                        gaps=item.gaps,
                    )
                    for item in result.hypothesis_revision.add_hypotheses
                )
            ),
        ),
        limits=AgentLimits(),
        known_tool_names=(),
        material_evidence_ids={"e-1"},
        finalize_only=False,
        hypothesis_revision_work_item_ids={"wi-1"},
    )
    assert [item.hypothesis_id for item in updated.hypotheses] == ["h-1", "h-2"]
    assert updated.hypotheses[0].statement == "既存の命題"
    assert updated.work_items[0].state == "open"


def test_revision_returns_no_hypothesis_for_search_strategy_only() -> None:
    client = FakeRevisionClient(
        {
            "decision_reason": "本文に独立した新しい命題はない",
            "add_hypotheses": [],
        }
    )
    context = _context()
    profile = legal_agent_profile().solver_hypothesis_revision
    assert profile is not None

    result = StructuredJSONModelAdapter(client).solve(context, profile)

    assert result.hypothesis_revision is not None
    assert result.hypothesis_revision.add_hypotheses == ()
    rendered = render_hypothesis_revision_model_call(context, profile)
    assert rendered.input_payload["acquired_evidence"][0]["evidence_id"] == "e-1"


def test_revision_repairs_transport_shape_once_without_changing_meaning() -> None:
    proposal = {
        "hypothesis_id": "h-2",
        "work_item_id": "wi-1",
        "statement": "本文から判明した別の命題",
        "evidence_ids": ["e-1"],
        "gaps": ["追加確認事項"],
    }
    client = FakeRevisionSequenceClient(
        [
            {
                "decision_reason": "本文に別の未確認事項がある",
                "add_hypotheses": [
                    {**proposal, "evidence_ids": ["e-1"] * 13}
                ],
            },
            {
                "decision_reason": "本文に別の未確認事項がある",
                "add_hypotheses": [proposal],
            },
        ]
    )
    context = _context()
    profile = legal_agent_profile().solver_hypothesis_revision
    assert profile is not None

    result = StructuredJSONModelAdapter(client).solve(context, profile)

    assert result.hypothesis_revision is not None
    assert result.hypothesis_revision.add_hypotheses[0].evidence_ids == ("e-1",)
    assert result.attempt_count == 2
    assert len(client.calls) == 2
    assert "transport_repair" in client.calls[1]["prompt"]


def test_revision_projection_only_includes_current_cycle_evidence() -> None:
    state = CaseState(
        case_id="revision-cycle-scope",
        question="確認事項",
        research_cycle_count=2,
        work_items=(
            WorkItem(work_item_id="wi-1", question="既存の確認事項"),
        ),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h-1",
                work_item_id="wi-1",
                statement="既存の命題",
            ),
        ),
        evidence=(
            Evidence(
                evidence_id="e-old",
                source_ref="source-old",
                content="前Cycleで取得した本文",
                created_cycle=1,
            ),
            Evidence(
                evidence_id="e-new",
                source_ref="source-new",
                content="現在Cycleで取得した本文",
                created_cycle=2,
            ),
        ),
    )
    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    profile = legal_agent_profile().solver_hypothesis_revision
    assert profile is not None

    rendered = render_hypothesis_revision_model_call(context, profile)

    assert [
        item["evidence_id"] for item in rendered.input_payload["acquired_evidence"]
    ] == ["e-new"]
    evidence_schema = rendered.output_schema["properties"]["add_hypotheses"][
        "items"
    ]["properties"]["evidence_ids"]["items"]
    assert evidence_schema["enum"] == ["e-new"]


def test_revision_does_not_change_the_common_solver_contract() -> None:
    assert "hypothesis_revision" not in SolverDecision.model_json_schema()[
        "properties"
    ]


def test_revision_cycle_history_must_be_positive_unique_and_completed() -> None:
    CaseState(
        case_id="valid-history",
        question="確認事項",
        research_cycle_count=2,
        hypothesis_revision_cycles=(1, 2),
    )
    for invalid_cycles in ((0,), (1, 1), (3,)):
        try:
            CaseState(
                case_id="invalid-history",
                question="確認事項",
                research_cycle_count=2,
                hypothesis_revision_cycles=invalid_cycles,
            )
        except ValueError:
            continue
        raise AssertionError(f"invalid history was accepted: {invalid_cycles}")
