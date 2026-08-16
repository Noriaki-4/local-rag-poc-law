"""Actionと最小Run/Budget/Checkpointモデル。"""

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from .common import (
    CaseVersion,
    CoreModel,
    Metadata,
    RecordRevision,
    StableId,
    utc_now,
)

ActionStatus = Literal[
    "proposed",
    "queued",
    "running",
    "succeeded",
    "failed",
    "timeout",
    "cancelled",
]
RunStatus = Literal["created", "running", "finished", "paused", "failed", "cancelled"]
StopReason = Literal[
    "completed",
    "insufficient",
    "needs_user_input",
    "max_iterations",
    "max_llm_calls",
    "max_actions",
    "wall_timeout",
    "token_budget",
    "storage_budget",
    "blocked",
    "model_timeout",
    "provider_quota",
    "cancelled",
    "error",
]


class Action(CoreModel):
    action_id: StableId
    case_id: StableId
    work_item_id: StableId
    tool_name: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    arguments: Metadata = Field(default_factory=dict)
    status: ActionStatus = "proposed"
    attempt: int = Field(default=0, ge=0)
    client_operation_id: str | None = None
    record_revision: RecordRevision = 1
    metadata: Metadata = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ActionHypothesis(CoreModel):
    action_hypothesis_id: StableId
    case_id: StableId
    action_id: StableId
    hypothesis_id: StableId
    purpose: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)


class BudgetProfile(CoreModel):
    max_iterations: int = Field(ge=1)
    max_llm_calls_total: int = Field(ge=1)
    max_domain_actions_total: int = Field(ge=1)
    max_wall_time_sec: float = Field(gt=0)
    finalization_reserve_sec: float = Field(ge=0)

    @model_validator(mode="after")
    def reserve_must_fit_wall_time(self):
        if self.finalization_reserve_sec >= self.max_wall_time_sec:
            raise ValueError("finalization reserve must be less than wall time")
        return self


class BudgetUsage(CoreModel):
    iterations: int = Field(default=0, ge=0)
    llm_calls: int = Field(default=0, ge=0)
    domain_actions: int = Field(default=0, ge=0)
    elapsed_ms: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    artifact_bytes: int = Field(default=0, ge=0)


class AgentRun(CoreModel):
    run_id: StableId
    case_id: StableId
    status: RunStatus = "created"
    stop_reason: StopReason | None = None
    budget_profile: BudgetProfile
    budget_usage: BudgetUsage = Field(default_factory=BudgetUsage)
    model_profile_snapshot: Metadata = Field(default_factory=dict)
    correlation_id: StableId
    record_revision: RecordRevision = 1
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_status_and_stop_reason(self):
        allowed: dict[str, set[str | None]] = {
            "created": {None},
            "running": {None},
            "finished": {"completed", "insufficient"},
            "paused": {
                "needs_user_input",
                "max_iterations",
                "max_llm_calls",
                "max_actions",
                "wall_timeout",
                "token_budget",
                "storage_budget",
                "blocked",
                "model_timeout",
                "provider_quota",
            },
            "failed": {"error"},
            "cancelled": {"cancelled"},
        }
        if self.stop_reason not in allowed[self.status]:
            raise ValueError("invalid Run status and stop_reason combination")
        return self


class AgentIteration(CoreModel):
    iteration_id: StableId
    run_id: StableId
    case_id: StableId
    iteration_no: int = Field(ge=1)
    start_case_version: CaseVersion
    end_case_version: CaseVersion | None = None
    focus_work_item_ids: tuple[StableId, ...] = ()
    action_ids: tuple[StableId, ...] = ()
    decision_ids: tuple[StableId, ...] = ()
    created_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None


class Checkpoint(CoreModel):
    checkpoint_id: StableId
    case_id: StableId
    case_version: CaseVersion
    focus_work_item_ids: tuple[StableId, ...] = ()
    work_item_cursor: str | None = None
    reason: Literal["iteration_end", "timeout", "manual_pause"]
    created_at: datetime = Field(default_factory=utc_now)
