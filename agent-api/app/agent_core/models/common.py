"""Coreモデルで共有する型と生成関数。"""

from datetime import datetime, timezone
from typing import Annotated, Any, TypeAlias
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

StableId: TypeAlias = Annotated[str, Field(min_length=1, max_length=160)]
CaseVersion: TypeAlias = Annotated[int, Field(ge=0)]
RecordRevision: TypeAlias = Annotated[int, Field(ge=1)]
SchemaVersion: TypeAlias = Annotated[int, Field(ge=1)]
Metadata: TypeAlias = dict[str, Any]


class CoreModel(BaseModel):
    """未知フィールドを黙って永続化しないCore DTOの基底。"""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_stable_id(prefix: str) -> str:
    """DBの自動採番へ依存しない安定IDを生成する。"""

    if not prefix or not prefix.replace("_", "").isalnum():
        raise ValueError("ID prefix must be alphanumeric or underscore")
    return f"{prefix}_{uuid4().hex}"
