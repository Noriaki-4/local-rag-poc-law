"""SolverがCaseStateへ適用できる、全体再生成ではない変更契約。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .state import (
    DependencyDecision,
    FinalAnswer,
    FrontierReAdoption,
    FrameworkModel,
    GraphCandidateReview,
    Hypothesis,
    HypothesisJudgment,
    ToolRequest,
    WorkItem,
    WorkItemState,
)


class WorkItemUpdate(FrameworkModel):
    work_item_id: str = Field(min_length=1, max_length=160)
    state: WorkItemState
    resolution: str | None = Field(default=None, max_length=2000)
    basis_hypothesis_ids: tuple[str, ...] = ()


class HypothesisUpdate(FrameworkModel):
    hypothesis_id: str = Field(min_length=1, max_length=160)
    judgment: HypothesisJudgment
    evidence_ids: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()


class WorkItemImpactDecision(FrameworkModel):
    work_item_id: str = Field(min_length=1, max_length=160)
    action: Literal["retain", "replace", "drop"]
    reason: str = Field(min_length=1, max_length=1000)
    new_basis_hypothesis_ids: tuple[str, ...] = ()
    replacement_work_item_id: str | None = Field(default=None, max_length=160)
    drop_subtree: bool = False

    @model_validator(mode="after")
    def validate_action_shape(self) -> WorkItemImpactDecision:
        if self.action == "replace" and self.replacement_work_item_id is None:
            raise ValueError("replace impact requires replacement work item ID")
        if self.action != "replace" and self.replacement_work_item_id is not None:
            raise ValueError("only replace impact may name a replacement")
        if self.action != "drop" and self.drop_subtree:
            raise ValueError("only drop impact may drop a subtree")
        if self.action == "drop" and self.new_basis_hypothesis_ids:
            raise ValueError("drop impact cannot assign a new basis")
        return self


class CaseUpdate(FrameworkModel):
    add_work_items: tuple[WorkItem, ...] = ()
    update_work_items: tuple[WorkItemUpdate, ...] = ()
    add_hypotheses: tuple[Hypothesis, ...] = ()
    update_hypotheses: tuple[HypothesisUpdate, ...] = ()
    impact_decisions: tuple[WorkItemImpactDecision, ...] = ()


class SolverDecision(FrameworkModel):
    next: Literal["continue", "finalize"]
    start_next_cycle: bool = False
    update: CaseUpdate = Field(default_factory=CaseUpdate)
    next_focus_work_item_ids: tuple[str, ...] = ()
    retain_evidence_ids: tuple[str, ...] = ()
    dependency_decisions: tuple[DependencyDecision, ...] = ()
    graph_candidate_review: GraphCandidateReview | None = None
    frontier_re_adoptions: tuple[FrontierReAdoption, ...] = ()
    tool_requests: tuple[ToolRequest, ...] = ()
    answer: FinalAnswer | None = None

    @model_validator(mode="after")
    def validate_next_shape(self) -> SolverDecision:
        if self.next == "finalize" and self.start_next_cycle:
            raise ValueError("finalize decision cannot start the next cycle")
        if self.next == "continue":
            if (
                not self.tool_requests
                and self.graph_candidate_review is None
                and not self.frontier_re_adoptions
            ):
                raise ValueError("continue decision requires a tool request")
            if self.answer is not None:
                raise ValueError("continue decision cannot contain an answer")
        else:
            if self.tool_requests:
                raise ValueError("finalize decision cannot contain tool requests")
            if self.frontier_re_adoptions:
                raise ValueError("finalize decision cannot re-adopt a Frontier")
            if self.answer is None:
                raise ValueError("finalize decision requires an answer")
        return self
