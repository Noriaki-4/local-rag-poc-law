"""最新状態と同じcommitで追記する小さい監査Event。"""

from datetime import datetime

from pydantic import Field

from .models.common import CaseVersion, CoreModel, StableId, utc_now


class CaseEvent(CoreModel):
    event_id: StableId
    case_id: StableId
    case_version: CaseVersion
    event_type: str = Field(min_length=1)
    entity_refs: tuple[StableId, ...] = ()
    summary: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
