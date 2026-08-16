"""新Agent Coreの意味モデルとInMemory永続化契約。"""

import hashlib

import pytest
from pydantic import ValidationError

from app.adapters.persistence import InMemoryCaseStore
from app.agent_core.models import (
    Action,
    ActionHypothesis,
    AgentRun,
    Artifact,
    BudgetProfile,
    Checkpoint,
    Decision,
    DecisionReference,
    Hypothesis,
    Observation,
    WorkItem,
    WorkItemDependency,
)
from app.agent_core.store import StoreConflictError, StoreIntegrityError
from app.agent_core.transactions import (
    ActionCompletion,
    DecisionDelta,
    PlanDelta,
    apply_decision,
    apply_plan_delta,
    claim_action,
    complete_action,
    register_external_artifact,
)


def _store() -> InMemoryCaseStore:
    store = InMemoryCaseStore()
    store.create_case(
        "取得済み根拠を失わず調査する",
        case_id="case_test",
        root_work_item_id="work_root",
    )
    return store


def _work(work_item_id: str, *, parent: str = "work_root") -> WorkItem:
    return WorkItem(
        work_item_id=work_item_id,
        case_id="case_test",
        parent_work_item_id=parent,
        goal=f"{work_item_id}を確認する",
    )


def _hypothesis(
    hypothesis_id: str = "hypothesis_1",
    work_item_id: str = "work_issue",
) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hypothesis_id,
        case_id="case_test",
        work_item_id=work_item_id,
        statement="取得対象に判断材料が存在する",
    )


def _action(
    action_id: str = "action_1",
    work_item_id: str = "work_issue",
) -> Action:
    return Action(
        action_id=action_id,
        case_id="case_test",
        work_item_id=work_item_id,
        tool_name="fetch_resource",
        purpose="判断材料の本文を取得する",
        arguments={"resource_ids": ["resource_1"]},
        client_operation_id=f"operation_{action_id}",
    )


def _artifact(
    artifact_id: str = "artifact_1",
    *,
    observation_id: str = "observation_1",
) -> Artifact:
    content = "確認済みの本文"
    return Artifact(
        artifact_id=artifact_id,
        case_id="case_test",
        artifact_type="document_text",
        content=content,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        source_kind="observation",
        source_ref=observation_id,
    )


def _prepare_running_action(
    store: InMemoryCaseStore,
) -> tuple[Action, Hypothesis]:
    work = _work("work_issue").model_copy(update={"status": "ready"})
    hypothesis = _hypothesis()
    action = _action()
    relation = ActionHypothesis(
        action_hypothesis_id="action_hypothesis_1",
        case_id="case_test",
        action_id=action.action_id,
        hypothesis_id=hypothesis.hypothesis_id,
        purpose="仮説の本文根拠を確認する",
    )
    apply_plan_delta(
        store,
        "case_test",
        PlanDelta(
            work_items=(work,),
            hypotheses=(hypothesis,),
            actions=(action,),
            action_hypotheses=(relation,),
        ),
    )
    claim_action(
        store,
        "case_test",
        action.action_id,
        expected_record_revision=1,
    )
    running = next(
        item
        for item in store.snapshot("case_test").actions
        if item.action_id == action.action_id
    )
    return running, hypothesis


def _complete_successfully(store: InMemoryCaseStore, running: Action) -> Artifact:
    observation = Observation(
        observation_id="observation_1",
        case_id="case_test",
        action_id=running.action_id,
        attempt=running.attempt,
        status="succeeded",
        summary="本文を1件取得した",
        result_count=1,
    )
    artifact = _artifact(observation_id=observation.observation_id)
    complete_action(
        store,
        "case_test",
        ActionCompletion(
            action_id=running.action_id,
            expected_record_revision=running.record_revision,
            expected_attempt=running.attempt,
            status="succeeded",
            observation=observation,
            artifacts=(artifact,),
        ),
    )
    return artifact


def test_nested_work_items_are_reconstructed_from_flat_records() -> None:
    store = _store()
    apply_plan_delta(
        store,
        "case_test",
        PlanDelta(
            work_items=(
                _work("work_level_1"),
                _work("work_level_2", parent="work_level_1"),
                _work("work_level_3", parent="work_level_2"),
            )
        ),
    )

    snapshot = store.snapshot("case_test")
    by_id = {item.work_item_id: item for item in snapshot.work_items}

    assert by_id["work_level_3"].parent_work_item_id == "work_level_2"
    assert by_id["work_level_2"].parent_work_item_id == "work_level_1"
    assert by_id["work_level_1"].parent_work_item_id == "work_root"
    assert snapshot.case.case_version == 2


@pytest.mark.parametrize("relationship", ["parent", "dependency"])
def test_relationship_cycles_are_rejected_atomically(relationship: str) -> None:
    store = _store()

    if relationship == "parent":
        delta = PlanDelta(
            work_items=(
                _work("work_a", parent="work_b"),
                _work("work_b", parent="work_a"),
            )
        )
    else:
        apply_plan_delta(
            store,
            "case_test",
            PlanDelta(work_items=(_work("work_a"), _work("work_b"))),
        )
        delta = PlanDelta(
            dependencies=(
                WorkItemDependency(
                    dependency_id="dependency_ab",
                    case_id="case_test",
                    dependent_work_item_id="work_a",
                    prerequisite_work_item_id="work_b",
                    reason="Bを先に確認する",
                ),
                WorkItemDependency(
                    dependency_id="dependency_ba",
                    case_id="case_test",
                    dependent_work_item_id="work_b",
                    prerequisite_work_item_id="work_a",
                    reason="Aを先に確認する",
                ),
            )
        )

    version_before = store.snapshot("case_test").case.case_version
    with pytest.raises(StoreIntegrityError, match="cycle"):
        apply_plan_delta(store, "case_test", delta)

    snapshot = store.snapshot("case_test")
    assert snapshot.case.case_version == version_before
    assert not snapshot.dependencies
    if relationship == "parent":
        assert {item.work_item_id for item in snapshot.work_items} == {"work_root"}


def test_unit_of_work_rolls_back_all_records_on_exception() -> None:
    store = _store()
    version_before = store.snapshot("case_test").case.case_version

    with (
        pytest.raises(RuntimeError, match="integration failed"),
        store.begin("case_test") as uow,
    ):
        uow.cases.add_work_item(_work("work_uncommitted"))
        raise RuntimeError("integration failed")

    snapshot = store.snapshot("case_test")
    assert snapshot.case.case_version == version_before
    assert "work_uncommitted" not in {item.work_item_id for item in snapshot.work_items}


def test_complete_action_survives_later_integration_failure() -> None:
    store = _store()
    running, _ = _prepare_running_action(store)
    artifact = _complete_successfully(store, running)

    with pytest.raises(TimeoutError):
        raise TimeoutError("later model integration timed out")

    snapshot = store.snapshot("case_test")
    assert snapshot.case.case_version == 4
    assert [item.observation_id for item in snapshot.observations] == ["observation_1"]
    assert [item.artifact_id for item in snapshot.artifacts] == [artifact.artifact_id]
    assert snapshot.actions[0].status == "succeeded"
    assert [event.case_version for event in snapshot.events] == [1, 2, 3, 4]


def test_failed_completion_does_not_partially_save_observation() -> None:
    store = _store()
    running, _ = _prepare_running_action(store)
    observation = Observation(
        observation_id="observation_bad",
        case_id="case_test",
        action_id=running.action_id,
        attempt=running.attempt,
        status="succeeded",
        summary="取得した",
    )
    invalid_artifact = _artifact(
        artifact_id="artifact_bad",
        observation_id="another_observation",
    )

    with pytest.raises(StoreIntegrityError, match="source_ref"):
        complete_action(
            store,
            "case_test",
            ActionCompletion(
                action_id=running.action_id,
                expected_record_revision=running.record_revision,
                expected_attempt=running.attempt,
                status="succeeded",
                observation=observation,
                artifacts=(invalid_artifact,),
            ),
        )

    snapshot = store.snapshot("case_test")
    assert snapshot.case.case_version == 3
    assert not snapshot.observations
    assert not snapshot.artifacts
    assert snapshot.actions[0].status == "running"


def test_decision_references_trace_back_to_saved_artifact() -> None:
    store = _store()
    running, hypothesis = _prepare_running_action(store)
    artifact = _complete_successfully(store, running)
    current = store.snapshot("case_test")
    work = next(
        item for item in current.work_items if item.work_item_id == "work_issue"
    )
    stored_hypothesis = next(
        item
        for item in current.hypotheses
        if item.hypothesis_id == hypothesis.hypothesis_id
    )
    decision = Decision(
        decision_id="decision_1",
        case_id="case_test",
        work_item_id=work.work_item_id,
        hypothesis_id=hypothesis.hypothesis_id,
        outcome="supported",
        statement="取得本文が仮説を支持する",
    )
    reference = DecisionReference(
        decision_reference_id="decision_reference_1",
        case_id="case_test",
        decision_id=decision.decision_id,
        artifact_id=artifact.artifact_id,
        target_record_revision=artifact.record_revision,
        role="supports",
    )

    apply_decision(
        store,
        "case_test",
        DecisionDelta(
            decision=decision,
            references=(reference,),
            expected_work_item_revision=work.record_revision,
            work_item_status="resolved",
            expected_hypothesis_revision=stored_hypothesis.record_revision,
            hypothesis_status="supported",
        ),
    )

    snapshot = store.snapshot("case_test")
    assert snapshot.case.case_version == 5
    assert snapshot.decisions[0].decision_id == decision.decision_id
    assert snapshot.decision_references[0].artifact_id == artifact.artifact_id
    assert snapshot.hypotheses[0].status == "supported"
    assert (
        next(
            item for item in snapshot.work_items if item.work_item_id == "work_issue"
        ).status
        == "resolved"
    )


def test_supported_decision_without_reference_is_rejected() -> None:
    store = _store()
    running, hypothesis = _prepare_running_action(store)
    _complete_successfully(store, running)
    snapshot = store.snapshot("case_test")
    work = next(
        item for item in snapshot.work_items if item.work_item_id == "work_issue"
    )
    stored_hypothesis = snapshot.hypotheses[0]

    with pytest.raises(StoreIntegrityError, match="requires at least one"):
        apply_decision(
            store,
            "case_test",
            DecisionDelta(
                decision=Decision(
                    decision_id="decision_without_reference",
                    case_id="case_test",
                    work_item_id=work.work_item_id,
                    hypothesis_id=hypothesis.hypothesis_id,
                    outcome="supported",
                    statement="根拠なしで支持する",
                ),
                expected_work_item_revision=work.record_revision,
                work_item_status="resolved",
                expected_hypothesis_revision=stored_hypothesis.record_revision,
                hypothesis_status="supported",
            ),
        )

    assert not store.snapshot("case_test").decisions


def test_decision_rejects_stale_artifact_revision() -> None:
    store = _store()
    running, hypothesis = _prepare_running_action(store)
    artifact = _complete_successfully(store, running)
    snapshot = store.snapshot("case_test")
    work = next(
        item for item in snapshot.work_items if item.work_item_id == "work_issue"
    )
    stored_hypothesis = snapshot.hypotheses[0]
    decision = Decision(
        decision_id="decision_stale_reference",
        case_id="case_test",
        work_item_id=work.work_item_id,
        hypothesis_id=hypothesis.hypothesis_id,
        outcome="supported",
        statement="古い表示内容から判断した",
    )

    with pytest.raises(StoreConflictError, match="artifact reference"):
        apply_decision(
            store,
            "case_test",
            DecisionDelta(
                decision=decision,
                references=(
                    DecisionReference(
                        decision_reference_id="decision_reference_stale",
                        case_id="case_test",
                        decision_id=decision.decision_id,
                        artifact_id=artifact.artifact_id,
                        target_record_revision=artifact.record_revision + 1,
                        role="supports",
                    ),
                ),
                expected_work_item_revision=work.record_revision,
                work_item_status="resolved",
                expected_hypothesis_revision=stored_hypothesis.record_revision,
                hypothesis_status="supported",
            ),
        )

    assert not store.snapshot("case_test").decisions


def test_external_artifact_requires_explicit_external_source() -> None:
    store = _store()
    observation_artifact = _artifact()
    with pytest.raises(StoreIntegrityError, match="external source"):
        register_external_artifact(store, "case_test", observation_artifact)

    content = "利用者が添付した資料"
    external = Artifact(
        artifact_id="artifact_external",
        case_id="case_test",
        artifact_type="user_document",
        content=content,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        source_kind="external",
        source_ref="upload_123",
    )
    register_external_artifact(store, "case_test", external)

    assert store.snapshot("case_test").artifacts == (external,)


def test_artifact_rejects_content_hash_mismatch() -> None:
    with pytest.raises(ValidationError, match="does not match content_hash"):
        Artifact(
            artifact_id="artifact_tampered",
            case_id="case_test",
            artifact_type="document_text",
            content="保存する本文",
            content_hash=hashlib.sha256("別の本文".encode()).hexdigest(),
            source_kind="external",
            source_ref="upload_456",
        )


def test_read_and_checkpoint_do_not_increment_case_version() -> None:
    store = _store()
    before = store.snapshot("case_test")
    checkpoint = Checkpoint(
        checkpoint_id="checkpoint_1",
        case_id="case_test",
        case_version=before.case.case_version,
        focus_work_item_ids=("work_root",),
        reason="iteration_end",
    )

    store.save_checkpoint(checkpoint)
    first_read = store.snapshot("case_test")
    second_read = store.snapshot("case_test")

    assert first_read.case.case_version == before.case.case_version
    assert second_read.case.case_version == before.case.case_version
    assert second_read.checkpoints == (checkpoint,)
    assert len(second_read.events) == 1


def test_expected_case_version_rejects_stale_plan() -> None:
    store = _store()
    apply_plan_delta(
        store,
        "case_test",
        PlanDelta(work_items=(_work("work_new"),)),
        expected_case_version=1,
    )

    with pytest.raises(StoreConflictError, match="case version"):
        apply_plan_delta(
            store,
            "case_test",
            PlanDelta(work_items=(_work("work_stale"),)),
            expected_case_version=1,
        )

    assert store.snapshot("case_test").case.case_version == 2


def test_stale_action_revision_cannot_complete_new_attempt() -> None:
    store = _store()
    running, _ = _prepare_running_action(store)
    observation = Observation(
        observation_id="observation_stale",
        case_id="case_test",
        action_id=running.action_id,
        attempt=running.attempt,
        status="timeout",
        summary="期限内に完了しなかった",
    )

    with pytest.raises(StoreConflictError, match="record revision"):
        complete_action(
            store,
            "case_test",
            ActionCompletion(
                action_id=running.action_id,
                expected_record_revision=1,
                expected_attempt=running.attempt,
                status="timeout",
                observation=observation,
            ),
        )


def test_snapshot_changes_do_not_mutate_store() -> None:
    store = _store()
    detached = store.snapshot("case_test")
    detached.case.goal = "呼出し側で書き換えた値"
    detached.work_items[0].goal = "呼出し側で書き換えた作業"

    persisted = store.snapshot("case_test")
    assert persisted.case.goal == "取得済み根拠を失わず調査する"
    assert persisted.work_items[0].goal == "取得済み根拠を失わず調査する"


def test_run_status_and_stop_reason_contract_is_validated() -> None:
    budget = BudgetProfile(
        max_iterations=3,
        max_llm_calls_total=6,
        max_domain_actions_total=10,
        max_wall_time_sec=120,
        finalization_reserve_sec=15,
    )
    valid = AgentRun(
        run_id="run_1",
        case_id="case_test",
        status="paused",
        stop_reason="model_timeout",
        budget_profile=budget,
        correlation_id="correlation_1",
    )
    assert valid.stop_reason == "model_timeout"

    with pytest.raises(ValidationError, match="status and stop_reason"):
        AgentRun(
            run_id="run_2",
            case_id="case_test",
            status="finished",
            stop_reason="wall_timeout",
            budget_profile=budget,
            correlation_id="correlation_2",
        )
