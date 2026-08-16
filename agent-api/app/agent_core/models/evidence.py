"""Action結果のObservationと再利用可能なArtifact。"""

import hashlib
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from .common import CoreModel, Metadata, RecordRevision, StableId, utc_now

ObservationStatus = Literal["succeeded", "failed", "timeout", "cancelled"]
ArtifactSourceKind = Literal["observation", "external"]


class Observation(CoreModel):
    observation_id: StableId
    case_id: StableId
    action_id: StableId
    attempt: int = Field(ge=1)
    status: ObservationStatus
    summary: str = Field(min_length=1)
    result_count: int | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)
    error_code: str | None = None
    metadata: Metadata = Field(default_factory=dict)
    record_revision: RecordRevision = 1
    created_at: datetime = Field(default_factory=utc_now)


class Artifact(CoreModel):
    artifact_id: StableId
    case_id: StableId
    artifact_type: str = Field(min_length=1)
    content: str | None = None
    content_ref: str | None = None
    content_hash: str = Field(min_length=1)
    source_kind: ArtifactSourceKind
    source_ref: str = Field(min_length=1)
    summary: str | None = None
    metadata: Metadata = Field(default_factory=dict)
    record_revision: RecordRevision = 1
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def require_content_or_reference(self):
        if self.content is None and self.content_ref is None:
            raise ValueError("artifact requires content or content_ref")
        if len(self.content_hash) != 64 or any(
            character not in "0123456789abcdef"
            for character in self.content_hash.lower()
        ):
            raise ValueError("artifact content_hash must be a SHA-256 hex digest")
        if self.content is not None:
            actual_hash = hashlib.sha256(self.content.encode()).hexdigest()
            if actual_hash != self.content_hash.lower():
                raise ValueError("artifact content does not match content_hash")
        return self


class ObservationArtifact(CoreModel):
    observation_artifact_id: StableId
    case_id: StableId
    observation_id: StableId
    artifact_id: StableId
    role: Literal["produced", "discovered", "updated"] = "produced"
    created_at: datetime = Field(default_factory=utc_now)
