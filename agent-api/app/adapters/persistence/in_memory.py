"""単一プロセス向けの原子的なInMemory CaseStore。"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from threading import RLock
from typing import Self

from app.agent_core.event_journal import CaseEvent
from app.agent_core.models import (
    Action,
    ActionHypothesis,
    AgentIteration,
    AgentRun,
    Artifact,
    Case,
    Checkpoint,
    Decision,
    DecisionReference,
    Hypothesis,
    Observation,
    ObservationArtifact,
    WorkItem,
    WorkItemDependency,
    new_stable_id,
    utc_now,
)
from app.agent_core.store import (
    CaseSnapshot,
    CommitResult,
    StoreCapabilities,
    StoreConflictError,
    StoreHealth,
    StoreIntegrityError,
    StoreNotFoundError,
)


@dataclass
class _State:
    cases: dict[str, Case] = field(default_factory=dict)
    work_items: dict[str, WorkItem] = field(default_factory=dict)
    dependencies: dict[str, WorkItemDependency] = field(default_factory=dict)
    hypotheses: dict[str, Hypothesis] = field(default_factory=dict)
    decisions: dict[str, Decision] = field(default_factory=dict)
    decision_references: dict[str, DecisionReference] = field(default_factory=dict)
    actions: dict[str, Action] = field(default_factory=dict)
    action_hypotheses: dict[str, ActionHypothesis] = field(default_factory=dict)
    observations: dict[str, Observation] = field(default_factory=dict)
    artifacts: dict[str, Artifact] = field(default_factory=dict)
    observation_artifacts: dict[str, ObservationArtifact] = field(default_factory=dict)
    runs: dict[str, AgentRun] = field(default_factory=dict)
    iterations: dict[str, AgentIteration] = field(default_factory=dict)
    checkpoints: dict[str, Checkpoint] = field(default_factory=dict)
    events: dict[str, CaseEvent] = field(default_factory=dict)


def _copy(value):
    return deepcopy(value)


def _for_case(values: dict, case_id: str) -> tuple:
    selected = [_copy(value) for value in values.values() if value.case_id == case_id]
    selected.sort(key=lambda value: tuple(_sort_value(value)))
    return tuple(selected)


def _sort_value(value) -> tuple[str, str]:
    created_at = getattr(value, "created_at", None)
    stable_id = next(
        (
            getattr(value, name)
            for name in (
                "work_item_id",
                "dependency_id",
                "hypothesis_id",
                "decision_id",
                "decision_reference_id",
                "action_id",
                "action_hypothesis_id",
                "observation_id",
                "artifact_id",
                "observation_artifact_id",
                "run_id",
                "iteration_id",
                "checkpoint_id",
                "event_id",
            )
            if hasattr(value, name)
        ),
        "",
    )
    return (created_at.isoformat() if created_at else "", stable_id)


class _CaseRepository:
    def __init__(self, state: _State, mark_dirty: Callable[[], None]):
        self._state = state
        self._mark_dirty = mark_dirty

    def get_case(self, case_id: str) -> Case:
        return _get(self._state.cases, case_id, "case")

    def get_work_item(self, work_item_id: str) -> WorkItem:
        return _get(self._state.work_items, work_item_id, "work item")

    def list_work_items(self, case_id: str) -> tuple[WorkItem, ...]:
        return _for_case(self._state.work_items, case_id)

    def list_dependencies(self, case_id: str) -> tuple[WorkItemDependency, ...]:
        return _for_case(self._state.dependencies, case_id)

    def get_hypothesis(self, hypothesis_id: str) -> Hypothesis:
        return _get(self._state.hypotheses, hypothesis_id, "hypothesis")

    def list_hypotheses(self, case_id: str) -> tuple[Hypothesis, ...]:
        return _for_case(self._state.hypotheses, case_id)

    def list_decisions(self, case_id: str) -> tuple[Decision, ...]:
        return _for_case(self._state.decisions, case_id)

    def list_decision_references(self, case_id: str) -> tuple[DecisionReference, ...]:
        return _for_case(self._state.decision_references, case_id)

    def add_work_item(self, item: WorkItem) -> None:
        _add(self._state.work_items, item.work_item_id, item, "work item")
        self._mark_dirty()

    def update_work_item(self, item: WorkItem) -> None:
        _update(self._state.work_items, item.work_item_id, item, "work item")
        self._mark_dirty()

    def add_dependency(self, dependency: WorkItemDependency) -> None:
        _add(
            self._state.dependencies,
            dependency.dependency_id,
            dependency,
            "dependency",
        )
        self._mark_dirty()

    def add_hypothesis(self, hypothesis: Hypothesis) -> None:
        _add(
            self._state.hypotheses,
            hypothesis.hypothesis_id,
            hypothesis,
            "hypothesis",
        )
        self._mark_dirty()

    def update_hypothesis(self, hypothesis: Hypothesis) -> None:
        _update(
            self._state.hypotheses,
            hypothesis.hypothesis_id,
            hypothesis,
            "hypothesis",
        )
        self._mark_dirty()

    def add_decision(self, decision: Decision) -> None:
        _add(
            self._state.decisions,
            decision.decision_id,
            decision,
            "decision",
        )
        self._mark_dirty()

    def add_decision_reference(self, reference: DecisionReference) -> None:
        _add(
            self._state.decision_references,
            reference.decision_reference_id,
            reference,
            "decision reference",
        )
        self._mark_dirty()


class _ActionRepository:
    def __init__(self, state: _State, mark_dirty: Callable[[], None]):
        self._state = state
        self._mark_dirty = mark_dirty

    def get_action(self, action_id: str) -> Action:
        return _get(self._state.actions, action_id, "action")

    def list_actions(self, case_id: str) -> tuple[Action, ...]:
        return _for_case(self._state.actions, case_id)

    def list_action_hypotheses(self, case_id: str) -> tuple[ActionHypothesis, ...]:
        return _for_case(self._state.action_hypotheses, case_id)

    def get_observation(self, observation_id: str) -> Observation:
        return _get(self._state.observations, observation_id, "observation")

    def list_observations(self, case_id: str) -> tuple[Observation, ...]:
        return _for_case(self._state.observations, case_id)

    def add_action(self, action: Action) -> None:
        _add(self._state.actions, action.action_id, action, "action")
        self._mark_dirty()

    def update_action(self, action: Action) -> None:
        _update(self._state.actions, action.action_id, action, "action")
        self._mark_dirty()

    def add_action_hypothesis(self, relation: ActionHypothesis) -> None:
        _add(
            self._state.action_hypotheses,
            relation.action_hypothesis_id,
            relation,
            "action hypothesis",
        )
        self._mark_dirty()

    def add_observation(self, observation: Observation) -> None:
        _add(
            self._state.observations,
            observation.observation_id,
            observation,
            "observation",
        )
        self._mark_dirty()


class _ArtifactRepository:
    def __init__(self, state: _State, mark_dirty: Callable[[], None]):
        self._state = state
        self._mark_dirty = mark_dirty

    def get_artifact(self, artifact_id: str) -> Artifact:
        return _get(self._state.artifacts, artifact_id, "artifact")

    def list_artifacts(self, case_id: str) -> tuple[Artifact, ...]:
        return _for_case(self._state.artifacts, case_id)

    def list_observation_artifacts(
        self, case_id: str
    ) -> tuple[ObservationArtifact, ...]:
        return _for_case(self._state.observation_artifacts, case_id)

    def add_artifact(self, artifact: Artifact) -> None:
        _add(self._state.artifacts, artifact.artifact_id, artifact, "artifact")
        self._mark_dirty()

    def add_observation_artifact(self, relation: ObservationArtifact) -> None:
        _add(
            self._state.observation_artifacts,
            relation.observation_artifact_id,
            relation,
            "observation artifact",
        )
        self._mark_dirty()


class _RunRepository:
    def __init__(self, state: _State, mark_dirty: Callable[[], None]):
        self._state = state
        self._mark_dirty = mark_dirty

    def list_runs(self, case_id: str) -> tuple[AgentRun, ...]:
        return _for_case(self._state.runs, case_id)

    def list_iterations(self, case_id: str) -> tuple[AgentIteration, ...]:
        return _for_case(self._state.iterations, case_id)

    def add_run(self, run: AgentRun) -> None:
        _add(self._state.runs, run.run_id, run, "run")
        self._mark_dirty()

    def add_iteration(self, iteration: AgentIteration) -> None:
        _add(
            self._state.iterations,
            iteration.iteration_id,
            iteration,
            "iteration",
        )
        self._mark_dirty()


class _EventJournal:
    def __init__(self, state: _State):
        self._state = state

    def list_events(self, case_id: str) -> tuple[CaseEvent, ...]:
        return _for_case(self._state.events, case_id)


class _CheckpointRepository:
    def __init__(self, state: _State):
        self._state = state

    def latest(self, case_id: str) -> Checkpoint | None:
        checkpoints = self.list_checkpoints(case_id)
        return checkpoints[-1] if checkpoints else None

    def list_checkpoints(self, case_id: str) -> tuple[Checkpoint, ...]:
        return _for_case(self._state.checkpoints, case_id)


class InMemoryCaseUnitOfWork:
    """外部I/Oを含まない短いtransaction中だけStore lockを保持する。"""

    def __init__(
        self,
        store: InMemoryCaseStore,
        case_id: str,
        expected_case_version: int | None,
    ):
        self._store = store
        self._case_id = case_id
        self._expected_case_version = expected_case_version
        self._working: _State | None = None
        self._active = False
        self._dirty = False

    def __enter__(self) -> Self:
        self._store._lock.acquire()
        try:
            case = self._store._state.cases.get(self._case_id)
            if case is None:
                raise StoreNotFoundError(f"case not found: {self._case_id}")
            if (
                self._expected_case_version is not None
                and case.case_version != self._expected_case_version
            ):
                raise StoreConflictError(
                    "case version does not match expected_case_version"
                )
            self._working = _copy(self._store._state)
            self._active = True
            self.cases = _CaseRepository(self._working, self._mark_dirty)
            self.runs = _RunRepository(self._working, self._mark_dirty)
            self.actions = _ActionRepository(self._working, self._mark_dirty)
            self.artifacts = _ArtifactRepository(self._working, self._mark_dirty)
            self.events = _EventJournal(self._working)
            self.checkpoints = _CheckpointRepository(self._working)
            return self
        except Exception:
            self._store._lock.release()
            raise

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._active:
            self.rollback()

    def _mark_dirty(self) -> None:
        self._dirty = True

    def commit(
        self,
        *,
        event_type: str,
        entity_refs: tuple[str, ...] = (),
        summary: str | None = None,
    ) -> CommitResult:
        working = self._require_active()
        if not self._dirty:
            raise StoreIntegrityError("cannot commit an empty transaction")
        case = working.cases[self._case_id]
        next_version = case.case_version + 1
        now = utc_now()
        working.cases[self._case_id] = case.model_copy(
            update={"case_version": next_version, "updated_at": now}
        )
        event = CaseEvent(
            event_id=new_stable_id("evt"),
            case_id=self._case_id,
            case_version=next_version,
            event_type=event_type,
            entity_refs=entity_refs,
            summary=summary,
            created_at=now,
        )
        working.events[event.event_id] = event
        self._store._state = working
        self._close()
        return CommitResult(
            case_id=self._case_id,
            case_version=next_version,
            event_id=event.event_id,
        )

    def rollback(self) -> None:
        self._require_active()
        self._close()

    def _require_active(self) -> _State:
        if not self._active or self._working is None:
            raise StoreIntegrityError("unit of work is not active")
        return self._working

    def _close(self) -> None:
        self._working = None
        self._active = False
        self._store._lock.release()


class InMemoryCaseStore:
    def __init__(self):
        self._state = _State()
        self._lock = RLock()

    def capabilities(self) -> StoreCapabilities:
        return StoreCapabilities()

    def health(self) -> StoreHealth:
        return StoreHealth(ok=True, backend="memory")

    def create_case(
        self,
        goal: str,
        *,
        case_id: str | None = None,
        root_work_item_id: str | None = None,
        acceptance_criteria: tuple[str, ...] = (),
    ) -> CaseSnapshot:
        case_id = case_id or new_stable_id("case")
        root_work_item_id = root_work_item_id or new_stable_id("work")
        now = utc_now()
        case = Case(
            case_id=case_id,
            goal=goal,
            case_version=1,
            created_at=now,
            updated_at=now,
        )
        root = WorkItem(
            work_item_id=root_work_item_id,
            case_id=case_id,
            goal=goal,
            acceptance_criteria=acceptance_criteria,
            status="ready",
            created_at=now,
            updated_at=now,
        )
        event = CaseEvent(
            event_id=new_stable_id("evt"),
            case_id=case_id,
            case_version=1,
            event_type="case.created",
            entity_refs=(case_id, root_work_item_id),
            created_at=now,
        )
        with self._lock:
            if case_id in self._state.cases:
                raise StoreIntegrityError(f"duplicate case ID: {case_id}")
            if root_work_item_id in self._state.work_items:
                raise StoreIntegrityError(
                    f"duplicate work item ID: {root_work_item_id}"
                )
            working = _copy(self._state)
            working.cases[case_id] = case
            working.work_items[root_work_item_id] = root
            working.events[event.event_id] = event
            self._state = working
        return self.snapshot(case_id)

    def begin(
        self,
        case_id: str,
        *,
        expected_case_version: int | None = None,
    ) -> InMemoryCaseUnitOfWork:
        return InMemoryCaseUnitOfWork(self, case_id, expected_case_version)

    def snapshot(self, case_id: str) -> CaseSnapshot:
        with self._lock:
            if case_id not in self._state.cases:
                raise StoreNotFoundError(f"case not found: {case_id}")
            state = _copy(self._state)
        return _snapshot_from_state(state, case_id)

    def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        with self._lock:
            case = self._state.cases.get(checkpoint.case_id)
            if case is None:
                raise StoreNotFoundError(f"case not found: {checkpoint.case_id}")
            if checkpoint.case_version != case.case_version:
                raise StoreConflictError(
                    "checkpoint must reference the latest committed case version"
                )
            if checkpoint.checkpoint_id in self._state.checkpoints:
                raise StoreIntegrityError(
                    f"duplicate checkpoint ID: {checkpoint.checkpoint_id}"
                )
            working = _copy(self._state)
            working.checkpoints[checkpoint.checkpoint_id] = _copy(checkpoint)
            self._state = working


def _get(values: dict, stable_id: str, label: str):
    value = values.get(stable_id)
    if value is None:
        raise StoreNotFoundError(f"{label} not found: {stable_id}")
    return _copy(value)


def _add(values: dict, stable_id: str, value, label: str) -> None:
    if stable_id in values:
        raise StoreIntegrityError(f"duplicate {label} ID: {stable_id}")
    values[stable_id] = _copy(value)


def _update(values: dict, stable_id: str, value, label: str) -> None:
    if stable_id not in values:
        raise StoreNotFoundError(f"{label} not found: {stable_id}")
    values[stable_id] = _copy(value)


def _snapshot_from_state(state: _State, case_id: str) -> CaseSnapshot:
    return CaseSnapshot(
        case=_copy(state.cases[case_id]),
        work_items=_for_case(state.work_items, case_id),
        dependencies=_for_case(state.dependencies, case_id),
        hypotheses=_for_case(state.hypotheses, case_id),
        actions=_for_case(state.actions, case_id),
        action_hypotheses=_for_case(state.action_hypotheses, case_id),
        observations=_for_case(state.observations, case_id),
        artifacts=_for_case(state.artifacts, case_id),
        observation_artifacts=_for_case(state.observation_artifacts, case_id),
        decisions=_for_case(state.decisions, case_id),
        decision_references=_for_case(state.decision_references, case_id),
        runs=_for_case(state.runs, case_id),
        iterations=_for_case(state.iterations, case_id),
        checkpoints=_for_case(state.checkpoints, case_id),
        events=_for_case(state.events, case_id),
    )
