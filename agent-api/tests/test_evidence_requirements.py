"""EvidenceRequirement状態モデルとconclusionGroupの単体テスト (計画書 §7, §8.6, §16.2)。"""

import pytest

from app.evidence_requirements import (
    CONTEXT_STATUS_OMITTED_BUDGET,
    CONTEXT_STATUS_PENDING,
    ENTERED_BY_ISSUE,
    ORIGIN_ARTICLE_TEXT,
    ORIGIN_GRAPH,
    ORIGIN_PLANNER,
    RETRIEVAL_STATUS_RESOLVED,
    RETRIEVAL_STATUS_UNRESOLVED,
    ConclusionGroup,
    EvidenceRequirement,
    LegalIssue,
    RequirementStore,
    assign_conclusion_groups,
    child_requirement,
    initial_requirements,
    requirement_priority,
)


def _issue(issue_id: str = "issue-1", families: tuple[str, ...] = ("normative_rule",)) -> LegalIssue:
    return LegalIssue(
        issue_id=issue_id,
        label="公開買付けの適用要件",
        question_span="手続が必要になるのはどのような場合",
        key_terms=("市場外買付け", "株券等所有割合"),
        requested_role_families=families,
        explicit_references=(),
        confidence=0.9,
    )


def _requirement(**overrides: object) -> EvidenceRequirement:
    values: dict[str, object] = {
        "requirement_id": "req-1",
        "issue_id": "issue-1",
        "role_family": "normative_rule",
        "role_subtypes": ("general_rule",),
        "origin": ORIGIN_PLANNER,
        "entered_by": ENTERED_BY_ISSUE,
        "mandatory": True,
    }
    values.update(overrides)
    return EvidenceRequirement(**values)  # type: ignore[arg-type]


class TestRequirementModel:
    def test_defaults_are_unresolved_and_pending(self) -> None:
        requirement = _requirement()
        assert requirement.retrieval_status == RETRIEVAL_STATUS_UNRESOLVED
        assert requirement.context_status == CONTEXT_STATUS_PENDING
        assert requirement.attempts == 0

    def test_is_immutable(self) -> None:
        requirement = _requirement()
        with pytest.raises(Exception):
            requirement.retrieval_status = RETRIEVAL_STATUS_RESOLVED  # type: ignore[misc]

    def test_updates_return_new_instances(self) -> None:
        requirement = _requirement()
        updated = requirement.with_status(RETRIEVAL_STATUS_RESOLVED)
        assert requirement.retrieval_status == RETRIEVAL_STATUS_UNRESOLVED
        assert updated.retrieval_status == RETRIEVAL_STATUS_RESOLVED

    def test_dedupe_key_covers_plan_fields(self) -> None:
        """(issueId, roleFamily, roleSubtypes, authorityType, parentArticleId) が一意キー (§8.6)。"""
        base = _requirement()
        same = _requirement(requirement_id="req-2")
        other_layer = _requirement(requirement_id="req-3", authority_type="cabinet_order")
        assert base.dedupe_key() == same.dedupe_key()
        assert base.dedupe_key() != other_layer.dedupe_key()

    def test_context_status_is_separate_from_retrieval_status(self) -> None:
        requirement = _requirement().with_status(RETRIEVAL_STATUS_RESOLVED)
        omitted = requirement.with_context_status(CONTEXT_STATUS_OMITTED_BUDGET)
        assert omitted.retrieval_status == RETRIEVAL_STATUS_RESOLVED
        assert omitted.context_status == CONTEXT_STATUS_OMITTED_BUDGET
        assert omitted.unresolved_for_answer is True

    def test_rejects_unknown_role_family(self) -> None:
        with pytest.raises(ValueError):
            _requirement(role_family="not_a_family")

    def test_unknown_role_subtypes_are_dropped(self) -> None:
        requirement = _requirement(role_subtypes=("general_rule", "delegated_detail"))
        assert requirement.role_subtypes == ("general_rule",)


class TestPriority:
    def test_user_explicit_is_p0(self) -> None:
        assert requirement_priority(_requirement(user_explicit=True, role_family="procedure")) == "P0"

    def test_direct_conclusion_is_p1(self) -> None:
        assert requirement_priority(_requirement()) == "P1"

    def test_exception_and_definition_are_p2(self) -> None:
        assert requirement_priority(_requirement(role_family="qualification", role_subtypes=("exception",))) == "P2"
        assert requirement_priority(_requirement(role_family="meaning_scope", role_subtypes=("definition",))) == "P2"

    def test_graph_derived_specification_is_p3(self) -> None:
        requirement = _requirement(
            role_family="normative_rule",
            origin=ORIGIN_GRAPH,
            entered_by="IMPLEMENTS",
        )
        assert requirement_priority(requirement) == "P3"

    def test_directly_asked_procedure_is_p4(self) -> None:
        assert requirement_priority(_requirement(role_family="procedure", role_subtypes=("publication",))) == "P4"

    def test_interpretive_is_p5(self) -> None:
        assert requirement_priority(_requirement(role_family="interpretive", mandatory=False)) == "P5"


class TestInitialRequirements:
    def test_issue_role_families_become_requirements(self) -> None:
        issues = (_issue(families=("normative_rule", "qualification")),)
        requirements = initial_requirements(issues)
        assert [r.role_family for r in requirements] == ["normative_rule", "qualification"]
        assert all(r.origin == ORIGIN_PLANNER for r in requirements)
        assert all(r.mandatory for r in requirements)

    def test_explicit_references_create_p0_requirements(self) -> None:
        issue = LegalIssue(
            issue_id="issue-1",
            label="金商法27条の2",
            question_span="金商法27条の2",
            key_terms=(),
            requested_role_families=("normative_rule",),
            explicit_references=("law-323AC0000000025-article-27_2",),
            confidence=1.0,
        )
        requirements = initial_requirements((issue,))
        explicit = [r for r in requirements if r.user_explicit]
        assert len(explicit) == 1
        assert explicit[0].article_id == "law-323AC0000000025-article-27_2"
        assert requirement_priority(explicit[0]) == "P0"

    def test_requirement_ids_are_stable_and_unique(self) -> None:
        requirements = initial_requirements((_issue(families=("normative_rule", "procedure")),))
        again = initial_requirements((_issue(families=("normative_rule", "procedure")),))
        assert [r.requirement_id for r in requirements] == [r.requirement_id for r in again]
        assert len({r.requirement_id for r in requirements}) == len(requirements)

    def test_interpretive_requirements_are_optional(self) -> None:
        requirements = initial_requirements((_issue(families=("interpretive",)),))
        assert requirements[0].mandatory is False


class TestConclusionGroups:
    def test_principle_and_exception_share_a_group(self) -> None:
        issue = _issue(families=("normative_rule", "qualification"))
        requirements, groups = assign_conclusion_groups(initial_requirements((issue,)), (issue,))
        by_family = {r.role_family: r for r in requirements}
        assert set(by_family["normative_rule"].conclusion_group_ids) & set(
            by_family["qualification"].conclusion_group_ids
        )
        assert all(isinstance(group, ConclusionGroup) for group in groups)

    def test_independent_conclusions_are_not_merged(self) -> None:
        issues = (_issue("issue-1"), _issue("issue-2"))
        requirements, groups = assign_conclusion_groups(initial_requirements(issues), issues)
        group_ids_1 = {
            gid for r in requirements if r.issue_id == "issue-1" for gid in r.conclusion_group_ids
        }
        group_ids_2 = {
            gid for r in requirements if r.issue_id == "issue-2" for gid in r.conclusion_group_ids
        }
        assert not (group_ids_1 & group_ids_2)
        assert len(groups) == 2

    def test_shared_definition_belongs_to_multiple_groups(self) -> None:
        issue = _issue(families=("normative_rule", "consequence", "meaning_scope"))
        requirements, _ = assign_conclusion_groups(initial_requirements((issue,)), (issue,))
        definition = next(r for r in requirements if r.role_family == "meaning_scope")
        assert len(definition.conclusion_group_ids) == 2

    def test_directly_asked_procedure_group_is_primary(self) -> None:
        issue = _issue(families=("procedure",))
        _, groups = assign_conclusion_groups(initial_requirements((issue,)), (issue,))
        assert [group.is_primary for group in groups] == [True]

    def test_interpretive_group_is_not_primary(self) -> None:
        issue = _issue(families=("normative_rule", "interpretive"))
        _, groups = assign_conclusion_groups(initial_requirements((issue,)), (issue,))
        interpretive = [group for group in groups if not group.is_primary]
        assert len(interpretive) == 1
        assert interpretive[0].mandatory_requirement_ids == ()

    def test_singleton_group_for_isolated_mandatory_requirement(self) -> None:
        issue = _issue(families=("meaning_scope",))
        requirements, groups = assign_conclusion_groups(initial_requirements((issue,)), (issue,))
        assert len(groups) == 1
        assert groups[0].is_primary is True
        assert requirements[0].conclusion_group_ids == (groups[0].group_id,)

    def test_child_requirement_inherits_group(self) -> None:
        issue = _issue(families=("normative_rule",))
        requirements, _ = assign_conclusion_groups(initial_requirements((issue,)), (issue,))
        parent = requirements[0]
        child = child_requirement(
            parent,
            role_family="qualification",
            role_subtypes=("exception",),
            authority_type="cabinet_order",
            parent_article_id="law-x-article-27_2",
            entered_by="IMPLEMENTS",
            origin=ORIGIN_ARTICLE_TEXT,
        )
        assert child.conclusion_group_ids == parent.conclusion_group_ids
        assert child.mandatory is True
        assert child.depth == parent.depth + 1


class TestRequirementStore:
    def test_add_deduplicates_by_key(self) -> None:
        store = RequirementStore.empty(max_total=10)
        store = store.add(_requirement())
        store = store.add(_requirement(requirement_id="req-2"))
        assert len(store.requirements) == 1

    def test_over_limit_requirements_are_recorded_not_deleted(self) -> None:
        store = RequirementStore.empty(max_total=1)
        store = store.add(_requirement())
        store = store.add(
            _requirement(requirement_id="req-2", role_family="procedure", role_subtypes=("filing",))
        )
        assert len(store.requirements) == 2
        overflow = store.get("req-2")
        assert overflow.over_budget is True
        assert overflow.unresolved_reason == "requirement_limit_exhausted"
        assert overflow.requirement_id not in [r.requirement_id for r in store.pending()]

    def test_priority_batch_limits_active_issues(self) -> None:
        store = RequirementStore.empty(max_total=50)
        for index in range(6):
            store = store.add(
                _requirement(
                    requirement_id=f"req-{index}",
                    issue_id=f"issue-{index}",
                )
            )
        batch, rest = store.pop_priority_batch(max_active_issues=4)
        assert len({r.issue_id for r in batch}) == 4
        assert len(rest.pending()) == 2

    def test_priority_batch_prefers_p0_then_mandatory(self) -> None:
        store = RequirementStore.empty(max_total=50)
        store = store.add(
            _requirement(
                requirement_id="req-aux",
                issue_id="issue-9",
                role_family="interpretive",
                mandatory=False,
            )
        )
        store = store.add(
            _requirement(
                requirement_id="req-explicit",
                issue_id="issue-1",
                user_explicit=True,
                article_id="law-x-article-1",
            )
        )
        batch, _ = store.pop_priority_batch(max_active_issues=1)
        assert batch[0].requirement_id == "req-explicit"

    def test_update_replaces_in_place_by_id(self) -> None:
        store = RequirementStore.empty(max_total=10).add(_requirement())
        store = store.update(store.get("req-1").with_status(RETRIEVAL_STATUS_RESOLVED))
        assert store.get("req-1").retrieval_status == RETRIEVAL_STATUS_RESOLVED
        assert len(store.requirements) == 1

    def test_transitions_are_recorded_for_trace(self) -> None:
        store = RequirementStore.empty(max_total=10).add(_requirement())
        store = store.update(
            store.get("req-1").with_status(RETRIEVAL_STATUS_RESOLVED, reason="article_text_fetched")
        )
        assert store.transitions[-1]["requirementId"] == "req-1"
        assert store.transitions[-1]["to"] == RETRIEVAL_STATUS_RESOLVED
        assert store.transitions[-1]["reason"] == "article_text_fetched"

    def test_mark_remaining_unresolved(self) -> None:
        store = RequirementStore.empty(max_total=10).add(_requirement())
        store = store.mark_pending_unresolved("expansion_budget_exhausted")
        assert store.get("req-1").unresolved_reason == "expansion_budget_exhausted"
        assert store.pending() == ()
