"""最終コンテキスト配分のテスト (計画書 §11.6, §16.5)。"""

from app.evidence_requirements import (
    CONTEXT_STATUS_INCLUDED,
    CONTEXT_STATUS_OMITTED_BUDGET,
    CONTEXT_STATUS_SHARED_COVERAGE,
    ORIGIN_PLANNER,
    ConclusionGroup,
    EvidenceRequirement,
)
from app.layered_context_assembler import (
    ANSWER_STATUS_COMPLETE,
    ANSWER_STATUS_INSUFFICIENT_PRIMARY,
    ANSWER_STATUS_PARTIAL_PRIMARY,
    ChunkCandidate,
    assemble_context,
)


def _requirement(requirement_id: str, *, mandatory: bool = True, role_family: str = "normative_rule") -> EvidenceRequirement:
    return EvidenceRequirement(
        requirement_id=requirement_id,
        issue_id="issue-1",
        role_family=role_family,
        origin=ORIGIN_PLANNER,
        mandatory=mandatory,
    )


def _group(
    group_id: str,
    mandatory_ids: tuple[str, ...],
    *,
    is_primary: bool = True,
    priority: str = "P1",
) -> ConclusionGroup:
    return ConclusionGroup(
        group_id=group_id,
        issue_id="issue-1",
        is_primary=is_primary,
        member_requirement_ids=mandatory_ids,
        mandatory_requirement_ids=mandatory_ids,
        priority=priority,
    )


def _chunk(
    content_unit_id: str,
    requirement_ids: tuple[str, ...],
    *,
    article_id: str | None = None,
    rank: int = 0,
    is_guidance: bool = False,
    user_explicit: bool = False,
) -> ChunkCandidate:
    return ChunkCandidate(
        content_unit_id=content_unit_id,
        article_id=article_id or content_unit_id.split("-paragraph-")[0],
        requirement_ids=requirement_ids,
        is_law=not is_guidance,
        is_guidance=is_guidance,
        user_explicit=user_explicit,
        rank=rank,
        item={"document": {"contentUnitId": content_unit_id}},
    )


class TestGroupAtomicCoverage:
    def test_principle_and_exception_are_added_together(self) -> None:
        requirements = [_requirement("req-principle"), _requirement("req-exception")]
        groups = [_group("group-1", ("req-principle", "req-exception"))]
        candidates = [
            _chunk("law-a-article-1", ("req-principle",)),
            _chunk("law-b-article-7", ("req-exception",)),
        ]
        result = assemble_context(candidates, requirements, groups, max_chunks=16)
        assert len(result.selected) == 2
        assert result.answer_status == ANSWER_STATUS_COMPLETE
        assert all(
            requirement.context_status == CONTEXT_STATUS_INCLUDED
            for requirement in result.requirements
        )

    def test_partial_group_is_never_added(self) -> None:
        """groupの一部だけをコンテキストへ入れない (§11.6-8)。"""
        requirements = [_requirement("req-principle"), _requirement("req-exception")]
        groups = [_group("group-1", ("req-principle", "req-exception"))]
        candidates = [
            _chunk("law-a-article-1", ("req-principle",)),
            _chunk("law-b-article-7", ("req-exception",)),
        ]
        result = assemble_context(candidates, requirements, groups, max_chunks=1)
        assert result.selected == ()
        assert result.omitted_group_ids == ("group-1",)
        assert result.additional_chunks_needed_by_group["group-1"] == 1
        assert all(
            requirement.context_status == CONTEXT_STATUS_OMITTED_BUDGET
            for requirement in result.requirements
        )

    def test_group_without_any_candidate_is_omitted(self) -> None:
        requirements = [_requirement("req-1")]
        groups = [_group("group-1", ("req-1",))]
        result = assemble_context([], requirements, groups)
        assert result.selected == ()
        assert result.answer_status == ANSWER_STATUS_INSUFFICIENT_PRIMARY
        assert result.unresolved_for_answer_requirement_ids == ("req-1",)

    def test_next_completable_group_takes_the_remaining_slots(self) -> None:
        """大きいgroupを丸ごと除外し、次順位の完結可能なgroupへ枠を回す (§16.5)。"""
        requirements = [
            _requirement("req-a1"),
            _requirement("req-a2"),
            _requirement("req-a3"),
            _requirement("req-b1"),
        ]
        groups = [
            _group("group-big", ("req-a1", "req-a2", "req-a3"), priority="P1"),
            _group("group-small", ("req-b1",), priority="P1"),
        ]
        candidates = [
            _chunk("law-a-article-1", ("req-a1",)),
            _chunk("law-a-article-2", ("req-a2",)),
            _chunk("law-a-article-3", ("req-a3",)),
            _chunk("law-b-article-1", ("req-b1",)),
        ]
        result = assemble_context(candidates, requirements, groups, max_chunks=2)
        assert result.included_group_ids == ("group-small",)
        assert result.omitted_group_ids == ("group-big",)
        assert result.answer_status == ANSWER_STATUS_PARTIAL_PRIMARY

    def test_shared_chunk_covers_multiple_requirements(self) -> None:
        requirements = [_requirement("req-1"), _requirement("req-2")]
        groups = [_group("group-1", ("req-1",)), _group("group-2", ("req-2",))]
        candidates = [_chunk("law-a-article-1", ("req-1", "req-2"))]
        result = assemble_context(candidates, requirements, groups, max_chunks=16)
        assert len(result.selected) == 1
        assert result.shared_coverage["law-a-article-1"] == ["req-1", "req-2"]
        assert all(
            requirement.context_status == CONTEXT_STATUS_SHARED_COVERAGE
            for requirement in result.requirements
        )

    def test_minimum_bundle_prefers_fewest_chunks(self) -> None:
        requirements = [_requirement("req-1"), _requirement("req-2")]
        groups = [_group("group-1", ("req-1", "req-2"))]
        candidates = [
            _chunk("law-a-article-1", ("req-1",), rank=0),
            _chunk("law-a-article-2", ("req-2",), rank=1),
            _chunk("law-a-article-3", ("req-1", "req-2"), rank=2),
        ]
        result = assemble_context(candidates, requirements, groups, max_chunks=16)
        assert [candidate.content_unit_id for candidate in result.selected] == ["law-a-article-3"]

    def test_user_explicit_chunk_is_preferred_as_representative(self) -> None:
        requirements = [_requirement("req-1")]
        groups = [_group("group-1", ("req-1",))]
        candidates = [
            _chunk("law-a-article-1", ("req-1",), rank=0),
            _chunk("law-a-article-2", ("req-1",), rank=5, user_explicit=True),
        ]
        result = assemble_context(candidates, requirements, groups, max_chunks=16)
        assert result.selected[0].content_unit_id == "law-a-article-2"

    def test_selection_is_deterministic(self) -> None:
        requirements = [_requirement("req-1"), _requirement("req-2")]
        groups = [_group("group-1", ("req-1",)), _group("group-2", ("req-2",))]
        candidates = [
            _chunk("law-a-article-1", ("req-1",)),
            _chunk("law-b-article-1", ("req-2",)),
        ]
        first = assemble_context(candidates, requirements, groups, max_chunks=16)
        second = assemble_context(list(reversed(candidates)), requirements, groups, max_chunks=16)
        assert [candidate.content_unit_id for candidate in first.selected] == [
            candidate.content_unit_id for candidate in second.selected
        ]


class TestAnswerStatus:
    def test_all_primary_groups_covered_is_complete(self) -> None:
        requirements = [_requirement("req-1")]
        groups = [_group("group-1", ("req-1",))]
        result = assemble_context([_chunk("law-a-article-1", ("req-1",))], requirements, groups)
        assert result.answer_status == ANSWER_STATUS_COMPLETE

    def test_some_primary_groups_omitted_is_partial(self) -> None:
        requirements = [_requirement("req-1"), _requirement("req-2")]
        groups = [_group("group-1", ("req-1",)), _group("group-2", ("req-2",))]
        candidates = [_chunk("law-a-article-1", ("req-1",))]
        result = assemble_context(candidates, requirements, groups, max_chunks=16)
        assert result.answer_status == ANSWER_STATUS_PARTIAL_PRIMARY
        assert result.omitted_primary_group_ids == ("group-2",)

    def test_no_primary_group_is_insufficient(self) -> None:
        requirements = [_requirement("req-1")]
        groups = [_group("group-1", ("req-1",))]
        result = assemble_context([], requirements, groups)
        assert result.answer_status == ANSWER_STATUS_INSUFFICIENT_PRIMARY


class TestAuxiliaryBudget:
    def test_guidance_is_capped(self) -> None:
        requirements = [_requirement("req-1"), _requirement("req-aux", mandatory=False, role_family="interpretive")]
        groups = [_group("group-1", ("req-1",))]
        candidates = [
            _chunk("law-a-article-1", ("req-1",)),
            _chunk("guidance-1-page-1-chunk-1", ("req-aux",), rank=1, is_guidance=True),
            _chunk("guidance-1-page-2-chunk-1", ("req-aux",), rank=2, is_guidance=True),
            _chunk("guidance-1-page-3-chunk-1", ("req-aux",), rank=3, is_guidance=True),
        ]
        result = assemble_context(
            candidates, requirements, groups, max_chunks=16, max_auxiliary_chunks=2
        )
        assert sum(1 for candidate in result.selected if candidate.is_guidance) == 2

    def test_no_auxiliary_when_no_primary_group_is_covered(self) -> None:
        """primary groupが0件の場合、optional・ガイドだけで枠を埋めない (§11.6-11)。"""
        requirements = [_requirement("req-1"), _requirement("req-aux", mandatory=False, role_family="interpretive")]
        groups = [_group("group-1", ("req-1",))]
        candidates = [_chunk("guidance-1-page-1-chunk-1", ("req-aux",), is_guidance=True)]
        result = assemble_context(candidates, requirements, groups, max_chunks=16)
        assert result.selected == ()
        assert result.answer_status == ANSWER_STATUS_INSUFFICIENT_PRIMARY

    def test_slots_are_not_filled_just_to_reach_the_limit(self) -> None:
        requirements = [_requirement("req-1")]
        groups = [_group("group-1", ("req-1",))]
        candidates = [_chunk("law-a-article-1", ("req-1",))]
        result = assemble_context(candidates, requirements, groups, max_chunks=16)
        assert len(result.selected) == 1

    def test_same_article_paragraphs_are_capped(self) -> None:
        requirements = [_requirement("req-1")]
        groups = [_group("group-1", ("req-1",))]
        candidates = [
            _chunk("law-a-article-1-paragraph-1", ("req-1",), article_id="law-a-article-1", rank=0),
            _chunk("law-a-article-1-paragraph-2", (), article_id="law-a-article-1", rank=1),
            _chunk("law-a-article-1-paragraph-3", (), article_id="law-a-article-1", rank=2),
            _chunk("law-a-article-1-paragraph-4", (), article_id="law-a-article-1", rank=3),
        ]
        result = assemble_context(
            candidates, requirements, groups, max_chunks=16, max_chunks_per_article=3
        )
        assert len(result.selected) == 3


class TestTrace:
    def test_trace_reports_group_and_requirement_state(self) -> None:
        requirements = [_requirement("req-1"), _requirement("req-2")]
        groups = [_group("group-1", ("req-1",)), _group("group-2", ("req-2",))]
        candidates = [_chunk("law-a-article-1", ("req-1",))]
        trace = assemble_context(candidates, requirements, groups, max_chunks=16).as_trace()
        assert trace["answerStatus"] == ANSWER_STATUS_PARTIAL_PRIMARY
        assert trace["includedRequirementIds"] == ["req-1"]
        assert trace["omittedRequirementIds"] == ["req-2"]
        assert trace["unresolvedForAnswerRequirementIds"] == ["req-2"]
        assert trace["selectedContentUnitIds"] == ["law-a-article-1"]


class TestUnevidencedMandatoryMembers:
    """候補が無いmandatory Requirementを黙って完全被覆扱いにしない。"""

    def test_group_is_not_covered_when_one_mandatory_member_has_no_evidence(self) -> None:
        requirements = [
            _requirement("req-principle"),
            _requirement("req-hypothetical-application"),
        ]
        groups = [_group("group-1", ("req-principle", "req-hypothetical-application"))]
        # 準用先の仮説Requirementは検索で候補ゼロ(exhausted)だった。
        candidates = [_chunk("law-a-article-1", ("req-principle",))]
        result = assemble_context(candidates, requirements, groups, max_chunks=16)
        assert result.selected == ()
        assert result.answer_status == ANSWER_STATUS_INSUFFICIENT_PRIMARY
        assert result.unresolved_for_answer_requirement_ids == (
            "req-principle",
            "req-hypothetical-application",
        )
        statuses = {r.requirement_id: r.context_status for r in result.requirements}
        assert statuses["req-principle"] == CONTEXT_STATUS_OMITTED_BUDGET
        assert statuses["req-hypothetical-application"] == CONTEXT_STATUS_OMITTED_BUDGET

    def test_group_without_any_evidence_is_still_omitted(self) -> None:
        requirements = [_requirement("req-1"), _requirement("req-2")]
        groups = [_group("group-1", ("req-1", "req-2"))]
        result = assemble_context([], requirements, groups)
        assert result.selected == ()
        assert result.answer_status == ANSWER_STATUS_INSUFFICIENT_PRIMARY

    def test_evidenced_members_are_still_added_atomically(self) -> None:
        """根拠がある例外は、原則と一体でなければ枠を使わない(§11.6-8)。"""
        requirements = [_requirement("req-principle"), _requirement("req-exception")]
        groups = [_group("group-1", ("req-principle", "req-exception"))]
        candidates = [
            _chunk("law-a-article-1", ("req-principle",)),
            _chunk("law-b-article-7", ("req-exception",)),
        ]
        result = assemble_context(candidates, requirements, groups, max_chunks=1)
        assert result.selected == ()
        assert result.omitted_group_ids == ("group-1",)
