"""LLM判断・Tool実行を分けて確定する小さいtransaction。"""

from collections.abc import Mapping
from typing import Literal

from pydantic import model_validator

from .models import (
    Action,
    ActionHypothesis,
    Artifact,
    CoreModel,
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
from .relationships import RelationshipError, validate_work_item_relationships
from .store import (
    CaseStore,
    CommitResult,
    StoreConflictError,
    StoreIntegrityError,
)


class PlanDelta(CoreModel):
    work_items: tuple[WorkItem, ...] = ()
    dependencies: tuple[WorkItemDependency, ...] = ()
    hypotheses: tuple[Hypothesis, ...] = ()
    actions: tuple[Action, ...] = ()
    action_hypotheses: tuple[ActionHypothesis, ...] = ()

    @model_validator(mode="after")
    def require_change(self):
        if not any(
            (
                self.work_items,
                self.dependencies,
                self.hypotheses,
                self.actions,
                self.action_hypotheses,
            )
        ):
            raise ValueError("plan delta must contain at least one change")
        return self


class ActionCompletion(CoreModel):
    action_id: str
    expected_record_revision: int
    expected_attempt: int
    status: Literal["succeeded", "failed", "timeout", "cancelled"]
    observation: Observation
    artifacts: tuple[Artifact, ...] = ()


class DecisionDelta(CoreModel):
    decision: Decision
    references: tuple[DecisionReference, ...] = ()
    expected_work_item_revision: int
    work_item_status: Literal[
        "proposed",
        "ready",
        "in_progress",
        "resolved",
        "insufficient",
        "blocked",
        "cancelled",
    ]
    expected_hypothesis_revision: int | None = None
    hypothesis_status: (
        Literal[
            "proposed",
            "testing",
            "supported",
            "partially_supported",
            "refuted",
            "inconclusive",
            "superseded",
        ]
        | None
    ) = None


def apply_plan_delta(
    store: CaseStore,
    case_id: str,
    delta: PlanDelta,
    *,
    expected_case_version: int | None = None,
) -> CommitResult:
    """作業分解・仮説・次Actionを1回のcommitで追加する。"""

    with store.begin(case_id, expected_case_version=expected_case_version) as uow:
        existing_items = {
            item.work_item_id: item for item in uow.cases.list_work_items(case_id)
        }
        existing_dependencies = list(uow.cases.list_dependencies(case_id))
        existing_hypotheses = {
            item.hypothesis_id: item for item in uow.cases.list_hypotheses(case_id)
        }
        existing_actions = {
            item.action_id: item for item in uow.actions.list_actions(case_id)
        }
        existing_action_hypotheses = uow.actions.list_action_hypotheses(case_id)

        _require_case_ids(case_id, delta)
        _reject_duplicate_ids(
            existing_items,
            delta.work_items,
            "work_item_id",
            "work item",
        )
        _reject_duplicate_ids(
            {item.dependency_id: item for item in existing_dependencies},
            delta.dependencies,
            "dependency_id",
            "dependency",
        )
        _reject_duplicate_ids(
            existing_hypotheses,
            delta.hypotheses,
            "hypothesis_id",
            "hypothesis",
        )
        _reject_duplicate_ids(
            existing_actions,
            delta.actions,
            "action_id",
            "action",
        )
        _reject_relation_duplicates(
            existing_action_hypotheses + delta.action_hypotheses
        )
        _require_initial_record_revisions(delta)
        _reject_duplicate_client_operations(
            tuple(existing_actions.values()) + delta.actions
        )

        all_items = existing_items | {
            item.work_item_id: item for item in delta.work_items
        }
        all_dependencies = existing_dependencies + list(delta.dependencies)
        try:
            validate_work_item_relationships(all_items, all_dependencies)
        except RelationshipError as exc:
            raise StoreIntegrityError(str(exc)) from exc

        all_hypotheses = existing_hypotheses | {
            item.hypothesis_id: item for item in delta.hypotheses
        }
        all_actions = existing_actions | {
            item.action_id: item for item in delta.actions
        }
        for hypothesis in delta.hypotheses:
            _require_work_item(all_items, hypothesis.work_item_id, case_id)
        for action in delta.actions:
            _require_work_item(all_items, action.work_item_id, case_id)
            if action.status not in {"proposed", "queued"}:
                raise StoreIntegrityError(
                    "new action must have proposed or queued status"
                )
            if action.attempt != 0:
                raise StoreIntegrityError("new action attempt must be zero")
        for relation in delta.action_hypotheses:
            relation_action = all_actions.get(relation.action_id)
            relation_hypothesis = all_hypotheses.get(relation.hypothesis_id)
            if relation_action is None or relation_hypothesis is None:
                raise StoreIntegrityError(
                    "action hypothesis references an unknown record"
                )
            if (
                relation_action.case_id != case_id
                or relation_hypothesis.case_id != case_id
                or relation.case_id != case_id
            ):
                raise StoreIntegrityError("action hypothesis crosses case boundary")
            if relation_action.work_item_id != relation_hypothesis.work_item_id:
                raise StoreIntegrityError(
                    "action and hypothesis must belong to the same work item"
                )

        for item in delta.work_items:
            uow.cases.add_work_item(item)
        for dependency in delta.dependencies:
            uow.cases.add_dependency(dependency)
        for hypothesis in delta.hypotheses:
            uow.cases.add_hypothesis(hypothesis)
        for action in delta.actions:
            uow.actions.add_action(action)
        for relation in delta.action_hypotheses:
            uow.actions.add_action_hypothesis(relation)

        refs = tuple(
            [item.work_item_id for item in delta.work_items]
            + [item.dependency_id for item in delta.dependencies]
            + [item.hypothesis_id for item in delta.hypotheses]
            + [item.action_id for item in delta.actions]
            + [item.action_hypothesis_id for item in delta.action_hypotheses]
        )
        return uow.commit(event_type="plan.delta_applied", entity_refs=refs)


def claim_action(
    store: CaseStore,
    case_id: str,
    action_id: str,
    *,
    expected_record_revision: int,
) -> CommitResult:
    """外部I/O開始前にActionのattemptを確定する。"""

    with store.begin(case_id) as uow:
        action = uow.actions.get_action(action_id)
        _ensure_record_revision(
            action.record_revision,
            expected_record_revision,
            "action",
        )
        if action.case_id != case_id:
            raise StoreIntegrityError("action belongs to another case")
        if action.status not in {"proposed", "queued"}:
            raise StoreIntegrityError("only proposed or queued action can be claimed")
        updated = action.model_copy(
            update={
                "status": "running",
                "attempt": action.attempt + 1,
                "record_revision": action.record_revision + 1,
                "updated_at": utc_now(),
            }
        )
        uow.actions.update_action(updated)
        return uow.commit(event_type="action.claimed", entity_refs=(action_id,))


def complete_action(
    store: CaseStore,
    case_id: str,
    completion: ActionCompletion,
) -> CommitResult:
    """Observation・Artifact・Action終端状態を同時に確定する。"""

    with store.begin(case_id) as uow:
        action = uow.actions.get_action(completion.action_id)
        _ensure_record_revision(
            action.record_revision,
            completion.expected_record_revision,
            "action",
        )
        if action.case_id != case_id:
            raise StoreIntegrityError("action belongs to another case")
        if action.status != "running":
            raise StoreIntegrityError("only running action can be completed")
        if action.attempt != completion.expected_attempt:
            raise StoreConflictError("action attempt does not match")

        observation = completion.observation
        if (
            observation.case_id != case_id
            or observation.action_id != action.action_id
            or observation.attempt != action.attempt
        ):
            raise StoreIntegrityError(
                "observation does not match action, case, and attempt"
            )
        if observation.status != completion.status:
            raise StoreIntegrityError(
                "observation status must match action completion status"
            )
        existing_observation_ids = {
            item.observation_id for item in uow.actions.list_observations(case_id)
        }
        if observation.observation_id in existing_observation_ids:
            raise StoreIntegrityError(
                f"duplicate observation ID: {observation.observation_id}"
            )

        existing_artifact_ids = {
            item.artifact_id for item in uow.artifacts.list_artifacts(case_id)
        }
        new_artifact_ids: set[str] = set()
        for artifact in completion.artifacts:
            if artifact.case_id != case_id:
                raise StoreIntegrityError("artifact belongs to another case")
            if artifact.source_kind != "observation":
                raise StoreIntegrityError(
                    "tool result artifact must use observation source"
                )
            if artifact.source_ref != observation.observation_id:
                raise StoreIntegrityError(
                    "artifact source_ref must match completion observation"
                )
            if (
                artifact.artifact_id in existing_artifact_ids
                or artifact.artifact_id in new_artifact_ids
            ):
                raise StoreIntegrityError(
                    f"duplicate artifact ID: {artifact.artifact_id}"
                )
            new_artifact_ids.add(artifact.artifact_id)

        uow.actions.add_observation(observation)
        relation_ids: list[str] = []
        for artifact in completion.artifacts:
            uow.artifacts.add_artifact(artifact)
            relation = ObservationArtifact(
                observation_artifact_id=new_stable_id("obs_art"),
                case_id=case_id,
                observation_id=observation.observation_id,
                artifact_id=artifact.artifact_id,
                role="produced",
            )
            uow.artifacts.add_observation_artifact(relation)
            relation_ids.append(relation.observation_artifact_id)
        updated = action.model_copy(
            update={
                "status": completion.status,
                "record_revision": action.record_revision + 1,
                "updated_at": utc_now(),
            }
        )
        uow.actions.update_action(updated)
        return uow.commit(
            event_type="action.completed",
            entity_refs=(
                action.action_id,
                observation.observation_id,
                *(item.artifact_id for item in completion.artifacts),
                *relation_ids,
            ),
        )


def register_external_artifact(
    store: CaseStore,
    case_id: str,
    artifact: Artifact,
) -> CommitResult:
    """Action外のArtifactを明示的な外部出所付きで登録する。"""

    if artifact.case_id != case_id:
        raise StoreIntegrityError("artifact belongs to another case")
    if artifact.source_kind != "external":
        raise StoreIntegrityError(
            "artifact without observation must declare an external source"
        )
    with store.begin(case_id) as uow:
        uow.artifacts.add_artifact(artifact)
        return uow.commit(
            event_type="artifact.external_registered",
            entity_refs=(artifact.artifact_id,),
        )


def apply_decision(
    store: CaseStore,
    case_id: str,
    delta: DecisionDelta,
) -> CommitResult:
    """意味判断と対象状態を参照整合性を保って確定する。"""

    with store.begin(case_id) as uow:
        decision = delta.decision
        if decision.case_id != case_id:
            raise StoreIntegrityError("decision belongs to another case")
        work_item = uow.cases.get_work_item(decision.work_item_id)
        _ensure_record_revision(
            work_item.record_revision,
            delta.expected_work_item_revision,
            "work item",
        )
        _validate_work_item_transition(work_item.status, delta.work_item_status)

        hypothesis = None
        if decision.hypothesis_id is not None:
            hypothesis = uow.cases.get_hypothesis(decision.hypothesis_id)
            if hypothesis.work_item_id != work_item.work_item_id:
                raise StoreIntegrityError(
                    "decision hypothesis belongs to another work item"
                )
            if (
                delta.expected_hypothesis_revision is None
                or delta.hypothesis_status is None
            ):
                raise StoreIntegrityError(
                    "hypothesis decision requires revision and next status"
                )
            _ensure_record_revision(
                hypothesis.record_revision,
                delta.expected_hypothesis_revision,
                "hypothesis",
            )
            _validate_hypothesis_transition(hypothesis.status, delta.hypothesis_status)
        elif (
            delta.expected_hypothesis_revision is not None
            or delta.hypothesis_status is not None
        ):
            raise StoreIntegrityError(
                "hypothesis update requires decision.hypothesis_id"
            )

        existing_decision_ids = {
            item.decision_id for item in uow.cases.list_decisions(case_id)
        }
        if decision.decision_id in existing_decision_ids:
            raise StoreIntegrityError(f"duplicate decision ID: {decision.decision_id}")
        _validate_decision_references(uow, case_id, decision, delta.references)
        if (
            decision.outcome in {"supported", "refuted"}
            or delta.hypothesis_status in {"supported", "refuted"}
        ) and not delta.references:
            raise StoreIntegrityError(
                "supported or refuted decision requires at least one reference"
            )

        if delta.work_item_status == "resolved":
            required_children = [
                item
                for item in uow.cases.list_work_items(case_id)
                if item.parent_work_item_id == work_item.work_item_id and item.required
            ]
            if any(item.status != "resolved" for item in required_children):
                raise StoreIntegrityError(
                    "required child work items must be resolved first"
                )

        uow.cases.add_decision(decision)
        for reference in delta.references:
            uow.cases.add_decision_reference(reference)
        uow.cases.update_work_item(
            work_item.model_copy(
                update={
                    "status": delta.work_item_status,
                    "record_revision": work_item.record_revision + 1,
                    "updated_at": utc_now(),
                }
            )
        )
        if hypothesis is not None:
            uow.cases.update_hypothesis(
                hypothesis.model_copy(
                    update={
                        "status": delta.hypothesis_status,
                        "record_revision": hypothesis.record_revision + 1,
                        "updated_at": utc_now(),
                    }
                )
            )
        return uow.commit(
            event_type="decision.applied",
            entity_refs=(
                decision.decision_id,
                work_item.work_item_id,
                *(item.decision_reference_id for item in delta.references),
            ),
        )


def _require_case_ids(case_id: str, delta: PlanDelta) -> None:
    records = (
        *delta.work_items,
        *delta.dependencies,
        *delta.hypotheses,
        *delta.actions,
        *delta.action_hypotheses,
    )
    if any(getattr(record, "case_id", None) != case_id for record in records):
        raise StoreIntegrityError("plan delta crosses case boundary")


def _reject_duplicate_ids(
    existing: Mapping[str, object],
    new_records: tuple,
    id_field: str,
    label: str,
) -> None:
    seen = set(existing)
    for record in new_records:
        stable_id = getattr(record, id_field)
        if stable_id in seen:
            raise StoreIntegrityError(f"duplicate {label} ID: {stable_id}")
        seen.add(stable_id)


def _reject_relation_duplicates(
    relations: tuple[ActionHypothesis, ...],
) -> None:
    ids: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    for relation in relations:
        if relation.action_hypothesis_id in ids:
            raise StoreIntegrityError("duplicate action hypothesis ID")
        pair = (relation.action_id, relation.hypothesis_id)
        if pair in pairs:
            raise StoreIntegrityError("duplicate action hypothesis relation")
        ids.add(relation.action_hypothesis_id)
        pairs.add(pair)


def _require_initial_record_revisions(delta: PlanDelta) -> None:
    versioned_records = (
        *delta.work_items,
        *delta.dependencies,
        *delta.hypotheses,
        *delta.actions,
    )
    if any(
        getattr(record, "record_revision", None) != 1 for record in versioned_records
    ):
        raise StoreIntegrityError("new records must start at record revision 1")


def _reject_duplicate_client_operations(actions: tuple[Action, ...]) -> None:
    seen: dict[str, str] = {}
    for action in actions:
        operation_id = action.client_operation_id
        if operation_id is None:
            continue
        existing_action_id = seen.get(operation_id)
        if existing_action_id is not None and existing_action_id != action.action_id:
            raise StoreIntegrityError(
                "client_operation_id must be unique within a case"
            )
        seen[operation_id] = action.action_id


def _require_work_item(
    items: dict[str, WorkItem], work_item_id: str, case_id: str
) -> WorkItem:
    item = items.get(work_item_id)
    if item is None:
        raise StoreIntegrityError(f"unknown work item: {work_item_id}")
    if item.case_id != case_id:
        raise StoreIntegrityError("work item belongs to another case")
    return item


def _ensure_record_revision(actual: int, expected: int, label: str) -> None:
    if actual != expected:
        raise StoreConflictError(f"{label} record revision does not match")


def _validate_work_item_transition(current: str, target: str) -> None:
    allowed = {
        "proposed": {"proposed", "ready", "in_progress", "cancelled", "blocked"},
        "ready": {"ready", "in_progress", "resolved", "cancelled", "blocked"},
        "in_progress": {
            "in_progress",
            "resolved",
            "insufficient",
            "blocked",
            "cancelled",
        },
        "resolved": {"resolved"},
        "insufficient": {"insufficient"},
        "blocked": {"blocked", "ready"},
        "cancelled": {"cancelled"},
    }
    if target not in allowed[current]:
        raise StoreIntegrityError(
            f"invalid work item transition: {current} -> {target}"
        )


def _validate_hypothesis_transition(current: str, target: str) -> None:
    terminal = {
        "supported",
        "partially_supported",
        "refuted",
        "inconclusive",
        "superseded",
    }
    if current in terminal and target not in {current, "superseded"}:
        raise StoreIntegrityError(
            f"invalid hypothesis transition: {current} -> {target}"
        )
    if current == "proposed" and target == "proposed":
        return
    if current == "testing" and target == "proposed":
        raise StoreIntegrityError(
            f"invalid hypothesis transition: {current} -> {target}"
        )


def _validate_decision_references(
    uow,
    case_id: str,
    decision: Decision,
    references: tuple[DecisionReference, ...],
) -> None:
    seen_ids: set[str] = set()
    seen_targets: set[tuple[str, str]] = set()
    for reference in references:
        if (
            reference.case_id != case_id
            or reference.decision_id != decision.decision_id
        ):
            raise StoreIntegrityError(
                "decision reference does not match decision and case"
            )
        if reference.decision_reference_id in seen_ids:
            raise StoreIntegrityError("duplicate decision reference ID")
        if reference.artifact_id is not None:
            artifact = uow.artifacts.get_artifact(reference.artifact_id)
            if artifact.case_id != case_id:
                raise StoreIntegrityError("artifact belongs to another case")
            _ensure_record_revision(
                artifact.record_revision,
                reference.target_record_revision,
                "decision artifact reference",
            )
            target = ("artifact", reference.artifact_id)
        else:
            observation_id = reference.observation_id
            if observation_id is None:
                raise StoreIntegrityError(
                    "decision reference requires an observation ID"
                )
            observation = uow.actions.get_observation(observation_id)
            if observation.case_id != case_id:
                raise StoreIntegrityError("observation belongs to another case")
            _ensure_record_revision(
                observation.record_revision,
                reference.target_record_revision,
                "decision observation reference",
            )
            target = ("observation", observation_id)
        if target in seen_targets:
            raise StoreIntegrityError("duplicate target in decision references")
        seen_ids.add(reference.decision_reference_id)
        seen_targets.add(target)
