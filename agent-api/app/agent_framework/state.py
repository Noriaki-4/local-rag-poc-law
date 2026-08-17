"""反復ループがサイクル間で引き継ぐ最小状態。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

RunStatus = Literal["running", "completed", "failed", "cancelled"]
WorkItemState = Literal["open", "resolved", "dropped"]
HypothesisJudgment = Literal["supported", "contradicted", "unresolved"]
ToolStatus = Literal["succeeded", "failed", "timeout"]
ReviewVerdict = Literal["accept", "revise"]
DependencyStatus = Literal["not_required", "needs_action", "resolved"]
DependencyAction = Literal[
    "discover_source",
    "assess_source",
    "discover_target",
    "fetch_target",
]
FrontierReviewStatus = Literal[
    "unreviewed",
    "selected",
    "relevant_deferred",
    "rejected",
]
FrontierReviewAction = Literal["select", "defer", "reject"]
DeferredFrontierResolutionAction = Literal[
    "fetch_next_cycle",
    "carry_forward",
    "no_longer_needed",
    "unresolved_at_limit",
]
UnreviewedGraphResolutionAction = Literal[
    "review_next_cycle",
    "no_longer_needed",
    "unresolved_at_limit",
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class FrameworkModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class WorkItem(FrameworkModel):
    work_item_id: str = Field(min_length=1, max_length=160)
    parent_work_item_id: str | None = Field(default=None, max_length=160)
    question: str = Field(min_length=1, max_length=1000)
    state: WorkItemState = "open"
    resolution: str | None = Field(default=None, max_length=2000)
    basis_hypothesis_ids: tuple[str, ...] = ()
    replaces_work_item_id: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def validate_identity_and_state(self) -> WorkItem:
        if self.parent_work_item_id == self.work_item_id:
            raise ValueError("work item cannot be its own parent")
        if self.replaces_work_item_id == self.work_item_id:
            raise ValueError("work item cannot replace itself")
        if len(self.basis_hypothesis_ids) != len(set(self.basis_hypothesis_ids)):
            raise ValueError("basis hypothesis IDs must be unique")
        if self.state == "open" and self.resolution is not None:
            raise ValueError("open work item cannot have a resolution")
        if self.state != "open" and not self.resolution:
            raise ValueError("closed work item requires a resolution")
        return self


class Hypothesis(FrameworkModel):
    hypothesis_id: str = Field(min_length=1, max_length=160)
    work_item_id: str = Field(min_length=1, max_length=160)
    statement: str = Field(min_length=1, max_length=1200)
    judgment: HypothesisJudgment = "unresolved"
    evidence_ids: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()

    @model_validator(mode="after")
    def require_evidence_for_semantic_judgment(self) -> Hypothesis:
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("hypothesis evidence IDs must be unique")
        if self.judgment in {"supported", "contradicted"} and not self.evidence_ids:
            raise ValueError("supported or contradicted hypothesis requires evidence")
        return self


class Evidence(FrameworkModel):
    evidence_id: str = Field(min_length=1, max_length=160)
    source_ref: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1)
    title: str | None = Field(default=None, max_length=500)
    created_cycle: int = Field(ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DependencyDecision(FrameworkModel):
    dependency_kind: str = Field(min_length=1, max_length=160)
    work_item_id: str = Field(min_length=1, max_length=160)
    status: DependencyStatus
    reason: str = Field(min_length=1, max_length=1000)
    source_evidence_ids: tuple[str, ...] = ()
    action: DependencyAction | None = None
    action_request_id: str | None = Field(default=None, max_length=160)
    target_article_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()


class GraphFrontierDecision(FrameworkModel):
    frontier_item_id: str = Field(min_length=1, max_length=160)
    article_id: str = Field(min_length=1, max_length=500)
    work_item_id: str = Field(min_length=1, max_length=160)
    hypothesis_id: str | None = Field(default=None, max_length=160)
    action: FrontierReviewAction
    reason: str = Field(min_length=1, max_length=1000)


class FrontierReAdoption(FrameworkModel):
    article_id: str = Field(min_length=1, max_length=500)
    work_item_id: str = Field(min_length=1, max_length=160)
    hypothesis_id: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=1000)


class DeferredFrontierResolution(FrameworkModel):
    """Solverが以前のdefer判断をCycle境界でどう扱うかを明示する。"""

    frontier_item_id: str = Field(min_length=1, max_length=160)
    article_id: str = Field(min_length=1, max_length=500)
    work_item_id: str = Field(min_length=1, max_length=160)
    hypothesis_id: str | None = Field(default=None, max_length=160)
    action: DeferredFrontierResolutionAction
    reason: str = Field(min_length=1, max_length=1000)
    decided_cycle: int | None = Field(default=None, ge=1)


class UnreviewedGraphResolution(FrameworkModel):
    """SolverがCycle境界で未評価Graph候補群をどう扱うかを明示する。"""

    action: UnreviewedGraphResolutionAction
    reason: str = Field(min_length=1, max_length=1000)
    candidate_count: int | None = Field(default=None, ge=1)
    decided_cycle: int | None = Field(default=None, ge=1)


class GraphCandidateReview(FrameworkModel):
    """今回の差分batchに対するSolver自身の意味判断。"""

    graph_request_ids: tuple[str, ...]
    reviewed_link_ids: tuple[str, ...]
    frontier_decisions: tuple[GraphFrontierDecision, ...]
    reason: str = Field(min_length=1, max_length=2000)
    reviewed_cycle: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def require_unique_ids(self) -> "GraphCandidateReview":
        if len(self.graph_request_ids) != len(set(self.graph_request_ids)):
            raise ValueError("graph review request IDs must be unique")
        if len(self.reviewed_link_ids) != len(set(self.reviewed_link_ids)):
            raise ValueError("graph review Link IDs must be unique")
        frontier_ids = tuple(
            item.frontier_item_id for item in self.frontier_decisions
        )
        if len(frontier_ids) != len(set(frontier_ids)):
            raise ValueError("graph review Frontier decisions must be unique")
        return self

    @property
    def selected_article_ids(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                item.article_id
                for item in self.frontier_decisions
                if item.action == "select"
            )
        )


class ToolRequest(FrameworkModel):
    request_id: str = Field(min_length=1, max_length=160)
    work_item_id: str = Field(min_length=1, max_length=160)
    tool_name: str = Field(min_length=1, max_length=160)
    arguments: dict[str, Any] = Field(default_factory=dict)
    purpose: str = Field(min_length=1, max_length=1000)
    hypothesis_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def require_unique_hypotheses(self) -> ToolRequest:
        if len(self.hypothesis_ids) != len(set(self.hypothesis_ids)):
            raise ValueError("tool request hypothesis IDs must be unique")
        return self


class ToolResult(FrameworkModel):
    request_id: str = Field(min_length=1, max_length=160)
    status: ToolStatus
    evidence_ids: tuple[str, ...] = ()
    error_code: str | None = Field(default=None, max_length=160)
    elapsed_ms: int = Field(default=0, ge=0)
    cycle_no: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_result(self) -> ToolResult:
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("tool result evidence IDs must be unique")
        if self.status == "succeeded" and self.error_code is not None:
            raise ValueError("successful tool result cannot have an error code")
        if self.status != "succeeded" and not self.error_code:
            raise ValueError("failed tool result requires an error code")
        return self


class FinalAnswer(FrameworkModel):
    text: str = Field(min_length=1)
    citation_ids: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    unresolved_work_item_ids: tuple[str, ...] = ()
    unresolved_hypothesis_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def require_unique_unresolved_ids(self) -> FinalAnswer:
        if len(self.unresolved_work_item_ids) != len(
            set(self.unresolved_work_item_ids)
        ):
            raise ValueError("unresolved work item IDs must be unique")
        if len(self.unresolved_hypothesis_ids) != len(
            set(self.unresolved_hypothesis_ids)
        ):
            raise ValueError("unresolved hypothesis IDs must be unique")
        return self


class ReviewFinding(FrameworkModel):
    description: str = Field(min_length=1, max_length=1000)
    work_item_id: str | None = Field(default=None, max_length=160)
    hypothesis_id: str | None = Field(default=None, max_length=160)


class ReviewResult(FrameworkModel):
    verdict: ReviewVerdict
    findings: tuple[ReviewFinding, ...] = ()

    @model_validator(mode="after")
    def validate_findings(self) -> ReviewResult:
        if self.verdict == "accept" and self.findings:
            raise ValueError("accepted review cannot contain findings")
        if self.verdict == "revise" and not self.findings:
            raise ValueError("revision requires at least one finding")
        return self


class CaseState(FrameworkModel):
    case_id: str = Field(min_length=1, max_length=160)
    question: str = Field(min_length=1)
    run_status: RunStatus = "running"
    research_cycle_count: int = Field(default=0, ge=0)
    work_items: tuple[WorkItem, ...] = ()
    hypotheses: tuple[Hypothesis, ...] = ()
    tool_requests: tuple[ToolRequest, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    dependency_decisions: tuple[DependencyDecision, ...] = ()
    graph_candidate_reviews: tuple[GraphCandidateReview, ...] = ()
    frontier_re_adoptions: tuple[FrontierReAdoption, ...] = ()
    deferred_frontier_resolutions: tuple[DeferredFrontierResolution, ...] = ()
    unreviewed_graph_resolutions: tuple[UnreviewedGraphResolution, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()
    focus_work_item_ids: tuple[str, ...] = ()
    retained_evidence_ids: tuple[str, ...] = ()
    final_answer: FinalAnswer | None = None
    review: ReviewResult | None = None
    stop_reason: str | None = Field(default=None, max_length=160)
    cycle_step_timeout: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
