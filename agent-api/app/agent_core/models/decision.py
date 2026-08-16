"""仮説、意味判断、根拠参照のモデル。"""

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from .common import CoreModel, Metadata, RecordRevision, StableId, utc_now

HypothesisStatus = Literal[
    "proposed",
    "testing",
    "supported",
    "partially_supported",
    "refuted",
    "inconclusive",
    "superseded",
]
DecisionOutcome = Literal[
    "supported",
    "partially_supported",
    "refuted",
    "inconclusive",
    "accepted",
    "rejected",
    "deferred",
]
ReferenceRole = Literal["supports", "contradicts", "qualifies", "based_on"]


class Hypothesis(CoreModel):
    hypothesis_id: StableId
    case_id: StableId
    work_item_id: StableId
    statement: str = Field(min_length=1)
    status: HypothesisStatus = "proposed"
    record_revision: RecordRevision = 1
    metadata: Metadata = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Decision(CoreModel):
    decision_id: StableId
    case_id: StableId
    work_item_id: StableId
    hypothesis_id: StableId | None = None
    outcome: DecisionOutcome
    statement: str = Field(min_length=1)
    record_revision: RecordRevision = 1
    metadata: Metadata = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class DecisionReference(CoreModel):
    decision_reference_id: StableId
    case_id: StableId
    decision_id: StableId
    role: ReferenceRole
    artifact_id: StableId | None = None
    observation_id: StableId | None = None
    target_record_revision: RecordRevision
    metadata: Metadata = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def require_exactly_one_target(self):
        targets = (self.artifact_id, self.observation_id)
        if sum(target is not None for target in targets) != 1:
            raise ValueError(
                "decision reference must target exactly one artifact or observation"
            )
        return self
