"""Article候補から最終回答コンテキスト(既定16chunks)を組み立てる。

計画書 §11.5(文字予算)、§11.6(最終16chunksの配分)、§11.8(chunk化のタイミング)に対応する。

配分はArticle候補数やRequirementの処理順ではなく、mandatoryな`conclusionGroup`の完全被覆を
優先する。原則だけを残して例外を落とすなど、結論を歪める選抜を避けるため、groupは原子的に
扱い、一部だけが最終コンテキストへ入る状態を作らない。

既存の論点被覆方式(`evidence_selector.py`)は旧経路が使用中のため、この新方式は同居させず
独立したモジュールとして持つ(§14)。
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .evidence_requirements import (
    CONTEXT_STATUS_INCLUDED,
    CONTEXT_STATUS_OMITTED_BUDGET,
    CONTEXT_STATUS_SHARED_COVERAGE,
    ConclusionGroup,
    EvidenceRequirement,
    priority_rank,
)

ANSWER_STATUS_COMPLETE = "complete"
ANSWER_STATUS_PARTIAL_PRIMARY = "partial_primary_evidence"
ANSWER_STATUS_INSUFFICIENT_PRIMARY = "insufficient_primary_evidence"

REASON_CONTEXT_BUDGET_EXHAUSTED = "context_budget_exhausted"
REASON_NO_CANDIDATE = "no_candidate_chunk"


@dataclass(frozen=True)
class ChunkCandidate:
    """最終コンテキストの候補1件(項・号単位)。"""

    content_unit_id: str
    article_id: str
    requirement_ids: tuple[str, ...] = ()
    is_law: bool = True
    is_guidance: bool = False
    user_explicit: bool = False
    rank: int = 0
    chars: int = 0
    truncated: bool = False
    item: dict[str, Any] = field(default_factory=dict)

    def covers(self, requirement_ids: set[str]) -> set[str]:
        return set(self.requirement_ids) & requirement_ids


@dataclass(frozen=True)
class ContextAssembly:
    """組み立て結果。回答制御に必要な状態を併せて返す。"""

    selected: tuple[ChunkCandidate, ...] = ()
    requirements: tuple[EvidenceRequirement, ...] = ()
    answer_status: str = ANSWER_STATUS_INSUFFICIENT_PRIMARY
    included_group_ids: tuple[str, ...] = ()
    omitted_group_ids: tuple[str, ...] = ()
    included_primary_group_ids: tuple[str, ...] = ()
    omitted_primary_group_ids: tuple[str, ...] = ()
    shared_coverage: dict[str, list[str]] = field(default_factory=dict)
    additional_chunks_needed_by_group: dict[str, int] = field(default_factory=dict)
    unresolved_for_answer_requirement_ids: tuple[str, ...] = ()

    @property
    def items(self) -> list[dict[str, Any]]:
        """既存経路と同じ形(検索結果item)のリストを返す。"""
        return [candidate.item for candidate in self.selected if candidate.item]

    def as_trace(self) -> dict[str, Any]:
        return {
            "answerStatus": self.answer_status,
            "primaryConclusionGroupIds": list(
                dict.fromkeys([*self.included_primary_group_ids, *self.omitted_primary_group_ids])
            ),
            "includedPrimaryConclusionGroupIds": list(self.included_primary_group_ids),
            "omittedPrimaryConclusionGroupIds": list(self.omitted_primary_group_ids),
            "includedConclusionGroupIds": list(self.included_group_ids),
            "omittedConclusionGroupIds": list(self.omitted_group_ids),
            "includedRequirementIds": [
                requirement.requirement_id
                for requirement in self.requirements
                if requirement.context_status
                in {CONTEXT_STATUS_INCLUDED, CONTEXT_STATUS_SHARED_COVERAGE}
            ],
            "sharedCoverage": {key: list(value) for key, value in self.shared_coverage.items()},
            "omittedRequirementIds": [
                requirement.requirement_id
                for requirement in self.requirements
                if requirement.context_status == CONTEXT_STATUS_OMITTED_BUDGET
            ],
            "additionalChunksNeeded": sum(self.additional_chunks_needed_by_group.values()),
            "additionalChunksNeededByGroup": dict(self.additional_chunks_needed_by_group),
            "unresolvedForAnswerRequirementIds": list(self.unresolved_for_answer_requirement_ids),
            "selectedContentUnitIds": [candidate.content_unit_id for candidate in self.selected],
        }


def assemble_context(
    candidates: Sequence[ChunkCandidate],
    requirements: Sequence[EvidenceRequirement],
    groups: Sequence[ConclusionGroup],
    *,
    max_chunks: int = 16,
    max_chunks_per_article: int = 3,
    max_auxiliary_chunks: int = 2,
) -> ContextAssembly:
    """§11.6 の手順で最終コンテキストを決定的に選ぶ。"""
    requirement_by_id = {requirement.requirement_id: requirement for requirement in requirements}
    selected: list[ChunkCandidate] = []
    selected_ids: set[str] = set()
    included_groups: list[str] = []
    omitted_groups: list[str] = []
    additional_needed: dict[str, int] = {}

    bundles = {
        group.group_id: _minimum_bundle(group, candidates, requirement_by_id)
        for group in groups
    }

    remaining_groups = [group for group in groups if group.mandatory_requirement_ids]
    while remaining_groups and len(selected) < max_chunks:
        eligible: list[tuple[tuple[Any, ...], ConclusionGroup, list[ChunkCandidate]]] = []
        for group in remaining_groups:
            bundle = bundles[group.group_id]
            if bundle is None:
                continue
            increment = [
                candidate for candidate in bundle if candidate.content_unit_id not in selected_ids
            ]
            if len(selected) + len(increment) > max_chunks:
                additional_needed[group.group_id] = (
                    len(selected) + len(increment) - max_chunks
                )
                continue
            eligible.append((_group_sort_key(group, increment), group, increment))
        if not eligible:
            break
        eligible.sort(key=lambda entry: entry[0])
        _, group, increment = eligible[0]
        # groupの一部だけを先に追加しない(§11.6-6)。
        for candidate in increment:
            selected.append(candidate)
            selected_ids.add(candidate.content_unit_id)
        included_groups.append(group.group_id)
        remaining_groups = [item for item in remaining_groups if item.group_id != group.group_id]

    for group in remaining_groups:
        omitted_groups.append(group.group_id)
        bundle = bundles[group.group_id]
        if bundle is None:
            additional_needed.setdefault(group.group_id, 0)
        else:
            increment = [
                candidate for candidate in bundle if candidate.content_unit_id not in selected_ids
            ]
            additional_needed.setdefault(
                group.group_id, max(0, len(selected) + len(increment) - max_chunks)
            )

    primary_groups = [group for group in groups if group.is_primary and group.mandatory_requirement_ids]
    included_primary = [
        group.group_id for group in primary_groups if group.group_id in included_groups
    ]
    omitted_primary = [
        group.group_id for group in primary_groups if group.group_id not in included_groups
    ]
    answer_status = _answer_status(primary_groups, included_primary)

    if answer_status != ANSWER_STATUS_INSUFFICIENT_PRIMARY:
        # 主論点の根拠を確保できた場合だけ、補完項号・optional・ガイドを足す(§11.6-9)。
        selected = _fill_remaining(
            selected,
            selected_ids,
            candidates,
            requirement_by_id,
            max_chunks=max_chunks,
            max_chunks_per_article=max_chunks_per_article,
            max_auxiliary_chunks=max_auxiliary_chunks,
        )

    # 実際に最終コンテキストへ入ったchunkから被覆状況を求める。検索上のresolvedと
    # 回答コンテキストへ渡せた状態を混同しない(§11.6)。
    covered_requirements: dict[str, list[ChunkCandidate]] = {}
    for candidate in selected:
        for requirement_id in candidate.requirement_ids:
            covered_requirements.setdefault(requirement_id, []).append(candidate)
    updated_requirements = tuple(
        _with_context_status(requirement, covered_requirements)
        for requirement in requirements
    )
    # 1chunkで複数Requirementを被覆した枠を記録する(§11.6-7)。
    shared_coverage = {
        candidate.content_unit_id: sorted(set(candidate.requirement_ids))
        for candidate in selected
        if len(set(candidate.requirement_ids)) > 1
    }
    return ContextAssembly(
        selected=tuple(selected),
        requirements=updated_requirements,
        answer_status=answer_status,
        included_group_ids=tuple(included_groups),
        omitted_group_ids=tuple(omitted_groups),
        included_primary_group_ids=tuple(included_primary),
        omitted_primary_group_ids=tuple(omitted_primary),
        shared_coverage=shared_coverage,
        additional_chunks_needed_by_group=additional_needed,
        unresolved_for_answer_requirement_ids=tuple(
            requirement.requirement_id
            for requirement in updated_requirements
            if requirement.mandatory and requirement.unresolved_for_answer
        ),
    )


def _minimum_bundle(
    group: ConclusionGroup,
    candidates: Sequence[ChunkCandidate],
    requirement_by_id: dict[str, EvidenceRequirement],
) -> list[ChunkCandidate] | None:
    """groupの全mandatory memberを被覆する最小chunk集合をset coverで求める。

    mandatory memberを全て被覆する。1件でも被覆できない場合はgroupを完成できないため
    Noneを返し、部分bundleに枠を使わない。候補ゼロの仮説をgroupから外すには、
    探索側で明示的に`not_applicable`へ確定してmandatory memberから除く必要がある。
    """
    coverable = {
        requirement_id
        for candidate in candidates
        if candidate.is_law
        for requirement_id in candidate.requirement_ids
    }
    targets = {
        requirement_id
        for requirement_id in group.mandatory_requirement_ids
        if requirement_id in requirement_by_id
    }
    if not targets or not targets.issubset(coverable):
        return None
    bundle: list[ChunkCandidate] = []
    uncovered = set(targets)
    available = [
        candidate for candidate in candidates if candidate.is_law and candidate.covers(targets)
    ]
    while uncovered:
        best = min(
            (
                candidate
                for candidate in available
                if candidate.covers(uncovered) and candidate not in bundle
            ),
            key=lambda candidate: (
                -len(candidate.covers(uncovered)),
                not candidate.user_explicit,
                candidate.rank,
                candidate.content_unit_id,
            ),
            default=None,
        )
        if best is None:
            return None
        bundle.append(best)
        uncovered -= best.covers(uncovered)
    return bundle


def _group_sort_key(group: ConclusionGroup, increment: list[ChunkCandidate]) -> tuple[Any, ...]:
    """group優先度、被覆mandatory数、増分の少なさ、順位、切り詰めの少なさで決定的に並べる。"""
    return (
        0 if group.is_primary else 1,
        priority_rank(group.priority),
        -len(group.mandatory_requirement_ids),
        len(increment),
        min((candidate.rank for candidate in increment), default=0),
        sum(1 for candidate in increment if candidate.truncated),
        group.group_id,
    )


def _answer_status(primary_groups: list[ConclusionGroup], included_primary: list[str]) -> str:
    if not primary_groups:
        return ANSWER_STATUS_INSUFFICIENT_PRIMARY
    if len(included_primary) == len(primary_groups):
        return ANSWER_STATUS_COMPLETE
    if included_primary:
        return ANSWER_STATUS_PARTIAL_PRIMARY
    return ANSWER_STATUS_INSUFFICIENT_PRIMARY


def _fill_remaining(
    selected: list[ChunkCandidate],
    selected_ids: set[str],
    candidates: Sequence[ChunkCandidate],
    requirement_by_id: dict[str, EvidenceRequirement],
    *,
    max_chunks: int,
    max_chunks_per_article: int,
    max_auxiliary_chunks: int,
) -> list[ChunkCandidate]:
    """残枠へ、同一Articleの補完項号 → optional Requirement → ガイド補足の順で足す。

    枠を上限まで埋めること自体は目標にしない(§11.6-10)。
    """
    result = list(selected)
    counts_by_article: dict[str, int] = {}
    for candidate in result:
        counts_by_article[candidate.article_id] = counts_by_article.get(candidate.article_id, 0) + 1

    def can_add(candidate: ChunkCandidate) -> bool:
        if len(result) >= max_chunks or candidate.content_unit_id in selected_ids:
            return False
        return counts_by_article.get(candidate.article_id, 0) < max_chunks_per_article

    def add(candidate: ChunkCandidate) -> None:
        result.append(candidate)
        selected_ids.add(candidate.content_unit_id)
        counts_by_article[candidate.article_id] = counts_by_article.get(candidate.article_id, 0) + 1

    selected_articles = {candidate.article_id for candidate in result}
    for candidate in _ordered(candidates):
        if candidate.is_guidance or not candidate.is_law:
            continue
        if candidate.article_id in selected_articles and can_add(candidate):
            add(candidate)

    auxiliary = 0
    for candidate in _ordered(candidates):
        if auxiliary >= max_auxiliary_chunks or len(result) >= max_chunks:
            break
        if candidate.content_unit_id in selected_ids:
            continue
        is_optional_law = candidate.is_law and not any(
            requirement_by_id[requirement_id].mandatory
            for requirement_id in candidate.requirement_ids
            if requirement_id in requirement_by_id
        )
        if not (candidate.is_guidance or is_optional_law):
            continue
        if candidate.is_law and not can_add(candidate):
            continue
        add(candidate)
        auxiliary += 1
    return result


def _ordered(candidates: Sequence[ChunkCandidate]) -> list[ChunkCandidate]:
    return sorted(
        candidates,
        key=lambda candidate: (
            not candidate.user_explicit,
            candidate.rank,
            candidate.content_unit_id,
        ),
    )


def _with_context_status(
    requirement: EvidenceRequirement,
    covered_requirements: dict[str, list[ChunkCandidate]],
) -> EvidenceRequirement:
    covering = covered_requirements.get(requirement.requirement_id)
    if not covering:
        if not requirement.mandatory:
            return requirement
        return requirement.with_context_status(CONTEXT_STATUS_OMITTED_BUDGET)
    status = (
        CONTEXT_STATUS_SHARED_COVERAGE
        if any(len(set(candidate.requirement_ids)) > 1 for candidate in covering)
        else CONTEXT_STATUS_INCLUDED
    )
    return requirement.with_context_status(status)
