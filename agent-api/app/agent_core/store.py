"""Persistence Adapterが満たすUnit of WorkとCaseStore契約。"""

from typing import Protocol, Self

from pydantic import Field

from .event_journal import CaseEvent
from .models import (
    Action,
    ActionHypothesis,
    AgentIteration,
    AgentRun,
    Artifact,
    Case,
    Checkpoint,
    CoreModel,
    Decision,
    DecisionReference,
    Hypothesis,
    Observation,
    ObservationArtifact,
    WorkItem,
    WorkItemDependency,
)
from .repositories import (
    ActionRepository,
    ArtifactRepository,
    CaseRepository,
    CheckpointRepository,
    EventJournal,
    RunRepository,
)


class StoreError(RuntimeError):
    """Persistence契約上の基底エラー。"""


class StoreNotFoundError(StoreError):
    """指定された安定IDが存在しない。"""


class StoreConflictError(StoreError):
    """versionまたはrecordRevisionが競合した。"""


class StoreIntegrityError(StoreError):
    """参照・一意性・状態遷移の制約違反。"""


class StoreCapabilities(CoreModel):
    transactions: bool = True
    optimistic_locking: bool = True
    leases: bool = False
    keyset_pagination: bool = False
    inline_artifacts: bool = True


class StoreHealth(CoreModel):
    ok: bool
    backend: str
    detail_code: str | None = None


class CommitResult(CoreModel):
    case_id: str
    case_version: int = Field(ge=1)
    event_id: str


class CaseSnapshot(CoreModel):
    case: Case
    work_items: tuple[WorkItem, ...] = ()
    dependencies: tuple[WorkItemDependency, ...] = ()
    hypotheses: tuple[Hypothesis, ...] = ()
    actions: tuple[Action, ...] = ()
    action_hypotheses: tuple[ActionHypothesis, ...] = ()
    observations: tuple[Observation, ...] = ()
    artifacts: tuple[Artifact, ...] = ()
    observation_artifacts: tuple[ObservationArtifact, ...] = ()
    decisions: tuple[Decision, ...] = ()
    decision_references: tuple[DecisionReference, ...] = ()
    runs: tuple[AgentRun, ...] = ()
    iterations: tuple[AgentIteration, ...] = ()
    checkpoints: tuple[Checkpoint, ...] = ()
    events: tuple[CaseEvent, ...] = ()


class CaseUnitOfWork(Protocol):
    cases: CaseRepository
    runs: RunRepository
    actions: ActionRepository
    artifacts: ArtifactRepository
    events: EventJournal
    checkpoints: CheckpointRepository

    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type, exc_value, traceback) -> None: ...

    def commit(
        self,
        *,
        event_type: str,
        entity_refs: tuple[str, ...] = (),
        summary: str | None = None,
    ) -> CommitResult: ...

    def rollback(self) -> None: ...


class CaseStore(Protocol):
    def capabilities(self) -> StoreCapabilities: ...

    def health(self) -> StoreHealth: ...

    def create_case(
        self,
        goal: str,
        *,
        case_id: str | None = None,
        root_work_item_id: str | None = None,
        acceptance_criteria: tuple[str, ...] = (),
    ) -> CaseSnapshot: ...

    def begin(
        self,
        case_id: str,
        *,
        expected_case_version: int | None = None,
    ) -> CaseUnitOfWork: ...

    def snapshot(self, case_id: str) -> CaseSnapshot: ...

    def save_checkpoint(self, checkpoint: Checkpoint) -> None: ...
