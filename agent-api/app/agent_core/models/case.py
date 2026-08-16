"""Case、WorkItem、実行依存のモデル。"""

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from .common import (
    CaseVersion,
    CoreModel,
    Metadata,
    RecordRevision,
    SchemaVersion,
    StableId,
    utc_now,
)

CaseStatus = Literal["active", "finished", "cancelled"]
WorkItemStatus = Literal[
    "proposed",
    "ready",
    "in_progress",
    "resolved",
    "insufficient",
    "blocked",
    "cancelled",
]


class Case(CoreModel):
    case_id: StableId
    goal: str = Field(min_length=1)
    status: CaseStatus = "active"
    case_version: CaseVersion = 1
    schema_version: SchemaVersion = 1
    metadata: Metadata = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class WorkItem(CoreModel):
    work_item_id: StableId
    case_id: StableId
    goal: str = Field(min_length=1)
    acceptance_criteria: tuple[str, ...] = ()
    parent_work_item_id: StableId | None = None
    required: bool = True
    status: WorkItemStatus = "proposed"
    priority: int = 0
    record_revision: RecordRevision = 1
    metadata: Metadata = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def reject_self_parent(self):
        if self.parent_work_item_id == self.work_item_id:
            raise ValueError("work item cannot be its own parent")
        return self


class WorkItemDependency(CoreModel):
    dependency_id: StableId
    case_id: StableId
    dependent_work_item_id: StableId
    prerequisite_work_item_id: StableId
    condition: Literal["resolved"] = "resolved"
    reason: str = Field(min_length=1)
    record_revision: RecordRevision = 1
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def reject_self_dependency(self):
        if self.dependent_work_item_id == self.prerequisite_work_item_id:
            raise ValueError("work item cannot depend on itself")
        return self
