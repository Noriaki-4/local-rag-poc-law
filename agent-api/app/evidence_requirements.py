"""論点(LegalIssue)と必要根拠スロット(EvidenceRequirement)の状態モデル。

計画書 §7(論点と必要根拠スロット)、§8.6(重複排除)、§8.8(上限到達時の扱い)、
§11.6(最終配分の優先度)に対応する。

Articleは論点ではない。1つの論点が複数Articleを必要とし、1つのArticleが複数論点を
支えるため、`LegalIssue` → `EvidenceRequirement` → Article候補 の多対多を明示的に持つ。

状態は「検索としてどこまで到達したか(retrieval_status)」と「回答コンテキストへ入れられたか
(context_status)」を分けて保存する。resolvedは回答根拠に含まれたことを意味しない。
"""

from dataclasses import dataclass, replace
from hashlib import sha1
from typing import Any

from .legal_ontology import ROLE_FAMILIES, normalize_role_subtypes

# --------------------------------------------------------------------------------------
# 状態値 (§7.1)
# --------------------------------------------------------------------------------------

RETRIEVAL_STATUS_UNRESOLVED = "unresolved"
RETRIEVAL_STATUS_SEARCHING = "searching"
RETRIEVAL_STATUS_CANDIDATE_FOUND = "candidate_found"
RETRIEVAL_STATUS_RESOLVED = "resolved"
RETRIEVAL_STATUS_AMBIGUOUS = "ambiguous"
RETRIEVAL_STATUS_EXHAUSTED = "exhausted"
RETRIEVAL_STATUS_NOT_APPLICABLE = "not_applicable"

RETRIEVAL_STATUSES: tuple[str, ...] = (
    RETRIEVAL_STATUS_UNRESOLVED,
    RETRIEVAL_STATUS_SEARCHING,
    RETRIEVAL_STATUS_CANDIDATE_FOUND,
    RETRIEVAL_STATUS_RESOLVED,
    RETRIEVAL_STATUS_AMBIGUOUS,
    RETRIEVAL_STATUS_EXHAUSTED,
    RETRIEVAL_STATUS_NOT_APPLICABLE,
)

CONTEXT_STATUS_PENDING = "pending"
CONTEXT_STATUS_INCLUDED = "included"
CONTEXT_STATUS_SHARED_COVERAGE = "shared_coverage"
CONTEXT_STATUS_OMITTED_BUDGET = "omitted_context_budget"

CONTEXT_STATUSES: tuple[str, ...] = (
    CONTEXT_STATUS_PENDING,
    CONTEXT_STATUS_INCLUDED,
    CONTEXT_STATUS_SHARED_COVERAGE,
    CONTEXT_STATUS_OMITTED_BUDGET,
)

# Requirementの生成元。plannerが全役割を予測する前提を置かないため区別する(§7.5)。
ORIGIN_PLANNER = "planner"
ORIGIN_RULE = "rule"
ORIGIN_ARTICLE_TEXT = "article_text"
ORIGIN_GRAPH = "graph"
ORIGIN_GUIDANCE = "guidance"
ORIGIN_REPLAN = "replan"

DIRECT_ORIGINS: tuple[str, ...] = (ORIGIN_PLANNER, ORIGIN_RULE, ORIGIN_REPLAN)

# どの関係でこのRequirementへ入ったか。`delegated_detail`をroleにしない代わりの軸(§5.3)。
ENTERED_BY_ISSUE = "issue"
ENTERED_BY_EXPLICIT_REFERENCE = "explicit_reference"

REASON_REQUIREMENT_LIMIT = "requirement_limit_exhausted"

# 役割の束ね方 (§7.6 conclusionGroup生成)
CONCLUSION_FAMILIES: tuple[str, ...] = ("normative_rule", "consequence")
QUALIFIER_FAMILIES: tuple[str, ...] = ("qualification", "meaning_scope")
PROCEDURE_FAMILIES: tuple[str, ...] = ("procedure", "temporal")
AUXILIARY_FAMILIES: tuple[str, ...] = ("interpretive", "linkage")

PRIORITIES: tuple[str, ...] = ("P0", "P1", "P2", "P3", "P4", "P5")


@dataclass(frozen=True)
class LegalIssue:
    """質問から抽出した主論点。Articleとは別概念として扱う。"""

    issue_id: str
    label: str
    question_span: str
    key_terms: tuple[str, ...] = ()
    requested_role_families: tuple[str, ...] = ()
    explicit_references: tuple[str, ...] = ()
    confidence: float = 0.0
    source: str = ORIGIN_PLANNER

    def __post_init__(self) -> None:
        families = tuple(
            dict.fromkeys(
                family for family in self.requested_role_families if family in ROLE_FAMILIES
            )
        )
        object.__setattr__(self, "requested_role_families", families)
        object.__setattr__(self, "key_terms", tuple(dict.fromkeys(self.key_terms)))
        object.__setattr__(self, "explicit_references", tuple(dict.fromkeys(self.explicit_references)))


@dataclass(frozen=True)
class EvidenceRequirement:
    """1論点を支えるために必要な根拠スロット1件。"""

    requirement_id: str
    issue_id: str
    role_family: str
    role_subtypes: tuple[str, ...] = ()
    conclusion_group_ids: tuple[str, ...] = ()
    authority_type: str | None = None
    document_id: str | None = None
    article_id: str | None = None
    parent_article_id: str | None = None
    # 親条文が属する法令系統。委任先の検索を同一系統へ絞るために持つ(§6.3-7, §9.1)。
    family_root: str | None = None
    entered_by: str = ENTERED_BY_ISSUE
    origin: str = ORIGIN_PLANNER
    mandatory: bool = True
    user_explicit: bool = False
    depth: int = 0
    key_terms: tuple[str, ...] = ()
    query_hint: str = ""
    retrieval_status: str = RETRIEVAL_STATUS_UNRESOLVED
    context_status: str = CONTEXT_STATUS_PENDING
    attempts: int = 0
    candidate_article_ids: tuple[str, ...] = ()
    accepted_article_ids: tuple[str, ...] = ()
    unresolved_reference_ids: tuple[str, ...] = ()
    unresolved_reason: str | None = None
    over_budget: bool = False

    def __post_init__(self) -> None:
        if self.role_family not in ROLE_FAMILIES:
            raise ValueError(f"Unknown roleFamily: {self.role_family}")
        if self.retrieval_status not in RETRIEVAL_STATUSES:
            raise ValueError(f"Unknown retrievalStatus: {self.retrieval_status}")
        if self.context_status not in CONTEXT_STATUSES:
            raise ValueError(f"Unknown contextStatus: {self.context_status}")
        object.__setattr__(
            self, "role_subtypes", normalize_role_subtypes(self.role_family, list(self.role_subtypes))
        )
        object.__setattr__(self, "key_terms", tuple(dict.fromkeys(self.key_terms)))

    def dedupe_key(self) -> tuple[Any, ...]:
        """§8.6 の一意キー。同じスロットを二重に作らない。"""
        return (
            self.issue_id,
            self.role_family,
            self.role_subtypes,
            self.authority_type,
            self.parent_article_id,
        )

    @property
    def priority(self) -> str:
        return requirement_priority(self)

    @property
    def unresolved_for_answer(self) -> bool:
        """回答が断定してはならないRequirementか(§11.6)。

        根拠chunkを回答コンテキストへ渡せていれば、そのRequirementについては断定できる。
        検索上resolvedでも枠に入らなかった場合と、そもそも未解決の場合の両方を含める。
        """
        if self.context_status in {CONTEXT_STATUS_INCLUDED, CONTEXT_STATUS_SHARED_COVERAGE}:
            return False
        if self.context_status == CONTEXT_STATUS_OMITTED_BUDGET:
            return True
        return self.retrieval_status not in {
            RETRIEVAL_STATUS_RESOLVED,
            RETRIEVAL_STATUS_NOT_APPLICABLE,
        }

    def with_status(self, status: str, *, reason: str | None = None) -> "EvidenceRequirement":
        return replace(self, retrieval_status=status, unresolved_reason=reason)

    def with_context_status(self, status: str) -> "EvidenceRequirement":
        return replace(self, context_status=status)

    def with_attempt(self) -> "EvidenceRequirement":
        return replace(self, attempts=self.attempts + 1)

    def with_candidates(self, article_ids: tuple[str, ...] | list[str]) -> "EvidenceRequirement":
        merged = tuple(dict.fromkeys([*self.candidate_article_ids, *article_ids]))
        return replace(self, candidate_article_ids=merged)

    def with_accepted(self, article_ids: tuple[str, ...] | list[str]) -> "EvidenceRequirement":
        merged = tuple(dict.fromkeys([*self.accepted_article_ids, *article_ids]))
        return replace(self, accepted_article_ids=merged)

    def with_groups(self, group_ids: tuple[str, ...] | list[str]) -> "EvidenceRequirement":
        merged = tuple(dict.fromkeys([*self.conclusion_group_ids, *group_ids]))
        return replace(self, conclusion_group_ids=merged)

    def as_trace(self) -> dict[str, Any]:
        return {
            "requirementId": self.requirement_id,
            "issueId": self.issue_id,
            "roleFamily": self.role_family,
            "roleSubtypes": list(self.role_subtypes),
            "conclusionGroupIds": list(self.conclusion_group_ids),
            "authorityType": self.authority_type,
            "documentId": self.document_id,
            "articleId": self.article_id,
            "parentArticleId": self.parent_article_id,
            "familyRoot": self.family_root,
            "enteredBy": self.entered_by,
            "origin": self.origin,
            "mandatory": self.mandatory,
            "userExplicit": self.user_explicit,
            "priority": self.priority,
            "depth": self.depth,
            "retrievalStatus": self.retrieval_status,
            "contextStatus": self.context_status,
            "attempts": self.attempts,
            "candidateArticleIds": list(self.candidate_article_ids),
            "acceptedArticleIds": list(self.accepted_article_ids),
            "unresolvedReferenceIds": list(self.unresolved_reference_ids),
            "unresolvedReason": self.unresolved_reason,
            "overBudget": self.over_budget,
        }


def requirement_priority(requirement: EvidenceRequirement) -> str:
    """§11.6 の Requirement優先度 P0〜P5 を決定的に求める。"""
    if requirement.user_explicit:
        return "P0"
    if requirement.role_family in QUALIFIER_FAMILIES:
        # 結論を変え得る定義・適用範囲・例外・除外・条件。
        return "P2"
    if requirement.origin in DIRECT_ORIGINS:
        if requirement.role_family in CONCLUSION_FAMILIES:
            return "P1"
        if requirement.role_family in PROCEDURE_FAMILIES:
            return "P4"
    if requirement.role_family in AUXILIARY_FAMILIES:
        return "P5"
    if requirement.origin in (ORIGIN_GRAPH, ORIGIN_ARTICLE_TEXT):
        return "P3"
    return "P5"


def priority_rank(priority: str) -> int:
    try:
        return PRIORITIES.index(priority)
    except ValueError:
        return len(PRIORITIES)


# --------------------------------------------------------------------------------------
# Requirementの生成
# --------------------------------------------------------------------------------------


def initial_requirements(issues: tuple[LegalIssue, ...] | list[LegalIssue]) -> tuple[EvidenceRequirement, ...]:
    """初期plannerとルール補正の結果から、起点となるRequirementを作る。

    plannerが必要役割を完全に予測できる前提は置かない。ここで作るのは仮説であり、
    取得Article本文とGraph関係から子Requirementを追加していく(§7.5)。
    """
    requirements: list[EvidenceRequirement] = []
    for issue in issues:
        for index, reference in enumerate(issue.explicit_references):
            requirements.append(
                EvidenceRequirement(
                    requirement_id=f"{issue.issue_id}-explicit-{index}",
                    issue_id=issue.issue_id,
                    role_family=_explicit_role_family(issue),
                    role_subtypes=(),
                    article_id=reference,
                    entered_by=ENTERED_BY_EXPLICIT_REFERENCE,
                    origin=ORIGIN_RULE,
                    mandatory=True,
                    user_explicit=True,
                    key_terms=issue.key_terms,
                    query_hint=issue.question_span or issue.label,
                )
            )
        for family in issue.requested_role_families:
            requirements.append(
                EvidenceRequirement(
                    requirement_id=f"{issue.issue_id}-{family}",
                    issue_id=issue.issue_id,
                    role_family=family,
                    role_subtypes=(),
                    entered_by=ENTERED_BY_ISSUE,
                    origin=issue.source if issue.source in DIRECT_ORIGINS else ORIGIN_PLANNER,
                    mandatory=family not in AUXILIARY_FAMILIES,
                    key_terms=issue.key_terms,
                    query_hint=issue.question_span or issue.label,
                )
            )
    return tuple(requirements)


def _explicit_role_family(issue: LegalIssue) -> str:
    for family in issue.requested_role_families:
        if family in CONCLUSION_FAMILIES:
            return family
    return issue.requested_role_families[0] if issue.requested_role_families else "normative_rule"


def child_requirement(
    parent: EvidenceRequirement,
    *,
    role_family: str,
    role_subtypes: tuple[str, ...] | list[str] = (),
    entered_by: str,
    origin: str,
    authority_type: str | None = None,
    document_id: str | None = None,
    article_id: str | None = None,
    parent_article_id: str | None = None,
    family_root: str | None = None,
    key_terms: tuple[str, ...] | list[str] = (),
    query_hint: str = "",
    mandatory: bool | None = None,
) -> EvidenceRequirement:
    """親Requirementから委任先・準用先・定義などの子Requirementを作る。

    親を完成させるために必要な根拠なので、`conclusionGroup`は親から継承する(§7.6-3)。
    """
    key = "|".join(
        [
            parent.requirement_id,
            entered_by,
            role_family,
            ",".join(sorted(role_subtypes)),
            authority_type or "-",
            parent_article_id or "-",
            article_id or "-",
        ]
    )
    digest = sha1(key.encode("utf-8")).hexdigest()[:10]  # noqa: S324 - IDの安定生成のみ
    resolved_mandatory = parent.mandatory if mandatory is None else mandatory
    if role_family in AUXILIARY_FAMILIES:
        resolved_mandatory = False
    return EvidenceRequirement(
        requirement_id=f"req-{digest}",
        issue_id=parent.issue_id,
        role_family=role_family,
        role_subtypes=tuple(role_subtypes),
        conclusion_group_ids=parent.conclusion_group_ids,
        authority_type=authority_type,
        document_id=document_id,
        article_id=article_id,
        parent_article_id=parent_article_id,
        family_root=family_root or parent.family_root,
        entered_by=entered_by,
        origin=origin,
        mandatory=resolved_mandatory,
        depth=parent.depth + 1,
        key_terms=tuple(key_terms) or parent.key_terms,
        query_hint=query_hint or parent.query_hint,
    )


# --------------------------------------------------------------------------------------
# conclusionGroup (§7.6)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ConclusionGroup:
    """一体でなければ結論を支えられないRequirementの束。"""

    group_id: str
    issue_id: str
    is_primary: bool
    member_requirement_ids: tuple[str, ...] = ()
    mandatory_requirement_ids: tuple[str, ...] = ()
    priority: str = "P5"

    def as_trace(self) -> dict[str, Any]:
        return {
            "groupId": self.group_id,
            "issueId": self.issue_id,
            "isPrimary": self.is_primary,
            "memberRequirementIds": list(self.member_requirement_ids),
            "mandatoryRequirementIds": list(self.mandatory_requirement_ids),
            "priority": self.priority,
        }


def assign_conclusion_groups(
    requirements: tuple[EvidenceRequirement, ...] | list[EvidenceRequirement],
    issues: tuple[LegalIssue, ...] | list[LegalIssue],
) -> tuple[tuple[EvidenceRequirement, ...], tuple[ConclusionGroup, ...]]:
    """Requirementの依存関係からgroupを決定的に生成する。

    plannerに最終group構造を断定させない。既にgroupを持つRequirement(子Requirement等)は
    黙って分割・再割当てせず、所属だけを集計する(§7.6-6)。
    """
    issue_order = [issue.issue_id for issue in issues]
    by_issue: dict[str, list[EvidenceRequirement]] = {}
    for requirement in requirements:
        by_issue.setdefault(requirement.issue_id, []).append(requirement)
    for issue_id in by_issue:
        if issue_id not in issue_order:
            issue_order.append(issue_id)

    assigned: dict[str, tuple[str, ...]] = {}
    group_order: list[str] = []
    for issue_id in issue_order:
        scoped = by_issue.get(issue_id, [])
        for requirement, group_ids in _issue_group_assignment(issue_id, scoped).items():
            assigned[requirement] = group_ids
            for group_id in group_ids:
                if group_id not in group_order:
                    group_order.append(group_id)

    updated = tuple(
        requirement.with_groups(assigned.get(requirement.requirement_id, ()))
        for requirement in requirements
    )
    for requirement in updated:
        for group_id in requirement.conclusion_group_ids:
            if group_id not in group_order:
                group_order.append(group_id)

    groups = tuple(_build_group(group_id, updated) for group_id in group_order)
    return updated, groups


def _issue_group_assignment(
    issue_id: str,
    requirements: list[EvidenceRequirement],
) -> dict[str, tuple[str, ...]]:
    """1論点内のRequirementへgroup IDを割り当てる(既に持つものは触らない)。"""
    pending = [requirement for requirement in requirements if not requirement.conclusion_group_ids]
    if not pending:
        return {}

    conclusion_groups = tuple(
        dict.fromkeys(
            f"group-{issue_id}-{requirement.role_family}"
            for requirement in requirements
            if requirement.role_family in CONCLUSION_FAMILIES
        )
    )
    assignment: dict[str, tuple[str, ...]] = {}
    for requirement in pending:
        family = requirement.role_family
        if requirement.user_explicit:
            # 利用者の明示条項は、その論点の結論groupの代表候補として扱う(§11.6-2)。
            assignment[requirement.requirement_id] = conclusion_groups or (
                f"group-{issue_id}-explicit",
            )
        elif family in CONCLUSION_FAMILIES:
            assignment[requirement.requirement_id] = (f"group-{issue_id}-{family}",)
        elif family in QUALIFIER_FAMILIES:
            # 原則を変え得る例外・定義は、その結論と一体で扱う。結論groupが無い場合
            # (定義そのものを問う質問など)は自身だけのgroupにする。
            assignment[requirement.requirement_id] = conclusion_groups or (
                f"group-{issue_id}-{family}",
            )
        elif family in PROCEDURE_FAMILIES:
            # 別個に回答できる手続・期限は独立group(§7.6-4)。
            assignment[requirement.requirement_id] = (f"group-{issue_id}-procedure",)
        else:
            assignment[requirement.requirement_id] = (f"group-{issue_id}-interpretive",)
    return assignment


def _build_group(group_id: str, requirements: tuple[EvidenceRequirement, ...]) -> ConclusionGroup:
    members = [
        requirement for requirement in requirements if group_id in requirement.conclusion_group_ids
    ]
    mandatory = [requirement for requirement in members if requirement.mandatory]
    ranked = mandatory or members
    priority = min(
        (requirement.priority for requirement in ranked),
        key=priority_rank,
        default="P5",
    )
    is_primary = any(
        requirement.priority in ("P0", "P1")
        or (requirement.mandatory and requirement.origin in DIRECT_ORIGINS)
        for requirement in members
    )
    return ConclusionGroup(
        group_id=group_id,
        issue_id=members[0].issue_id if members else "",
        is_primary=is_primary,
        member_requirement_ids=tuple(requirement.requirement_id for requirement in members),
        mandatory_requirement_ids=tuple(requirement.requirement_id for requirement in mandatory),
        priority=priority,
    )


# --------------------------------------------------------------------------------------
# Requirementストア (§8.2, §8.8)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RequirementStore:
    """Requirement集合の不変コンテナ。更新は常に新しいストアを返す。"""

    requirements: tuple[EvidenceRequirement, ...] = ()
    transitions: tuple[dict[str, Any], ...] = ()
    max_total: int = 24

    @classmethod
    def empty(cls, *, max_total: int = 24) -> "RequirementStore":
        return cls(requirements=(), transitions=(), max_total=max_total)

    def _replace_requirements(
        self,
        requirements: tuple[EvidenceRequirement, ...],
        transitions: tuple[dict[str, Any], ...] | None = None,
    ) -> "RequirementStore":
        return RequirementStore(
            requirements=requirements,
            transitions=self.transitions if transitions is None else transitions,
            max_total=self.max_total,
        )

    def get(self, requirement_id: str) -> EvidenceRequirement:
        for requirement in self.requirements:
            if requirement.requirement_id == requirement_id:
                return requirement
        raise KeyError(requirement_id)

    def has_key(self, requirement: EvidenceRequirement) -> bool:
        key = requirement.dedupe_key()
        return any(existing.dedupe_key() == key for existing in self.requirements)

    def active_count(self) -> int:
        return sum(1 for requirement in self.requirements if not requirement.over_budget)

    def add(self, requirement: EvidenceRequirement) -> "RequirementStore":
        """重複キーを弾き、総数上限を超えた分は削除せずover_budgetとして記録する(§8.8)。"""
        if self.has_key(requirement):
            return self
        if self.active_count() >= self.max_total:
            requirement = replace(
                requirement,
                over_budget=True,
                unresolved_reason=REASON_REQUIREMENT_LIMIT,
                retrieval_status=RETRIEVAL_STATUS_UNRESOLVED,
            )
        transition = {
            "requirementId": requirement.requirement_id,
            "issueId": requirement.issue_id,
            "from": None,
            "to": requirement.retrieval_status,
            "reason": requirement.unresolved_reason or f"created_by_{requirement.origin}",
            "priority": requirement.priority,
        }
        return self._replace_requirements(
            (*self.requirements, requirement),
            (*self.transitions, transition),
        )

    def add_all(
        self, requirements: tuple[EvidenceRequirement, ...] | list[EvidenceRequirement]
    ) -> "RequirementStore":
        store = self
        for requirement in requirements:
            store = store.add(requirement)
        return store

    def update(self, requirement: EvidenceRequirement, *, reason: str | None = None) -> "RequirementStore":
        updated: list[EvidenceRequirement] = []
        transitions = list(self.transitions)
        for existing in self.requirements:
            if existing.requirement_id != requirement.requirement_id:
                updated.append(existing)
                continue
            updated.append(requirement)
            if (
                existing.retrieval_status != requirement.retrieval_status
                or existing.context_status != requirement.context_status
            ):
                transitions.append(
                    {
                        "requirementId": requirement.requirement_id,
                        "issueId": requirement.issue_id,
                        "from": existing.retrieval_status,
                        "to": requirement.retrieval_status,
                        "contextStatus": requirement.context_status,
                        "reason": reason or requirement.unresolved_reason,
                        "priority": requirement.priority,
                    }
                )
        return self._replace_requirements(tuple(updated), tuple(transitions))

    def update_all(
        self, requirements: tuple[EvidenceRequirement, ...] | list[EvidenceRequirement]
    ) -> "RequirementStore":
        store = self
        for requirement in requirements:
            store = store.update(requirement)
        return store

    def pending(self) -> tuple[EvidenceRequirement, ...]:
        """まだ探索キューに残っているRequirement。"""
        return tuple(
            requirement
            for requirement in self.requirements
            if requirement.retrieval_status == RETRIEVAL_STATUS_UNRESOLVED
            and not requirement.over_budget
            and requirement.unresolved_reason is None
        )

    def mandatory_requirements(self) -> tuple[EvidenceRequirement, ...]:
        return tuple(requirement for requirement in self.requirements if requirement.mandatory)

    def unresolved_for_answer(self) -> tuple[EvidenceRequirement, ...]:
        return tuple(
            requirement
            for requirement in self.requirements
            if requirement.mandatory and requirement.unresolved_for_answer
        )

    def pop_priority_batch(
        self,
        *,
        max_active_issues: int,
    ) -> tuple[tuple[EvidenceRequirement, ...], "RequirementStore"]:
        """同じラウンドで処理するRequirementを、論点数を上限に取り出す(§8.2)。

        取り出したRequirementは`searching`へ移し、同じラウンドで再度取り出さない。
        """
        pending = sorted(self.pending(), key=_batch_sort_key)
        if not pending or max_active_issues <= 0:
            return (), self

        active_issues: list[str] = []
        for requirement in pending:
            if requirement.issue_id not in active_issues:
                if len(active_issues) >= max_active_issues:
                    continue
                active_issues.append(requirement.issue_id)
        batch = tuple(
            requirement for requirement in pending if requirement.issue_id in active_issues
        )
        store = self
        for requirement in batch:
            store = store.update(
                requirement.with_status(RETRIEVAL_STATUS_SEARCHING).with_attempt(),
                reason="batch_started",
            )
        return tuple(store.get(requirement.requirement_id) for requirement in batch), store

    def mark_pending_unresolved(self, reason: str) -> "RequirementStore":
        store = self
        for requirement in self.pending():
            store = store.update(
                requirement.with_status(RETRIEVAL_STATUS_UNRESOLVED, reason=reason),
                reason=reason,
            )
        return store

    def as_trace(self) -> list[dict[str, Any]]:
        return [requirement.as_trace() for requirement in self.requirements]


def _batch_sort_key(requirement: EvidenceRequirement) -> tuple[Any, ...]:
    return (
        priority_rank(requirement.priority),
        not requirement.mandatory,
        requirement.depth,
        requirement.issue_id,
        requirement.requirement_id,
    )
