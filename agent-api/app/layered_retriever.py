"""Requirement別のレイヤー指定検索と、ラウンド単位の反復探索ループ。

計画書 §8(反復探索ループ)、§9(レイヤー・役割別検索)、§11.1(上限)、§11.7(バッチ化)に対応する。

全体を最初から何度も実行せず、未解決Requirementだけをラウンド単位でbatch処理する。
round 0で全初期論点の起点Articleを取得してから、最大`MAX_EXPANSION_ROUNDS`回だけ
法律→政令→府令の展開を行う。停止条件に候補件数は含めない(§8.8)。
"""

from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from .config import settings
from .evidence_requirements import (
    RETRIEVAL_STATUS_CANDIDATE_FOUND,
    RETRIEVAL_STATUS_EXHAUSTED,
    RETRIEVAL_STATUS_RESOLVED,
    ConclusionGroup,
    EvidenceRequirement,
    LegalIssue,
    RequirementStore,
    assign_conclusion_groups,
    initial_requirements,
    priority_rank,
)
from .guidance_lane import GuidanceLane, GuidanceLaneResult
from .law_family import family_document_ids
from .legal_issue_planner import IssuePlan
from .legal_ontology import expandable_edge_types
from .legal_relation_resolver import (
    child_requirements_from_article_text,
    child_requirements_from_graph,
)
from .opensearch_client import RequirementSearchSpec
from .requirement_reranker import (
    RequirementRerankInput,
    rerank_requirement_batch,
)
from .requirement_satisfaction import assess_candidate
from .retrieval_budget import (
    COMPONENT_GRAPH,
    COMPONENT_SEARCH,
    BudgetTracker,
    RerankBudget,
)

STOP_REASON_COMPLETE = "all_mandatory_resolved"
STOP_REASON_ROUNDS = "max_expansion_rounds"
STOP_REASON_TIME = "time_budget_exhausted"
STOP_REASON_ARTICLE_BUDGET = "article_candidate_budget_exhausted"
STOP_REASON_REQUIREMENT_BUDGET = "requirement_limit_exhausted"
STOP_REASON_SEARCH_CALLS = "search_call_budget_exhausted"

# 役割ごとの検索語補強。質問全文ではなくRequirement専用クエリを作る(§9.2)。
ROLE_QUERY_HINTS: dict[str, str] = {
    "normative_rule": "",
    "qualification": "要件 条件 ただし 除く",
    "meaning_scope": "定義 とは 範囲",
    "procedure": "手続 届出 公告 提出 様式",
    "consequence": "効力 責任 罰則",
    "linkage": "準用 読替え",
    "temporal": "施行 経過措置",
    "interpretive": "取扱い 考え方",
}
ROLE_SUBTYPE_QUERY_HINTS: dict[str, str] = {
    "definition": "定義",
    "exception": "ただし 除く 適用しない",
    "exclusion": "除外",
    "publication": "公告 公表",
    "filing": "届出 提出",
    "deadline": "期限 以内",
    "form": "様式 別表",
    "penalty": "罰則",
    "application": "準用",
}


@dataclass(frozen=True)
class LayeredRetrievalResult:
    issues: tuple[LegalIssue, ...] = ()
    requirements: tuple[EvidenceRequirement, ...] = ()
    groups: tuple[ConclusionGroup, ...] = ()
    article_candidates: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    guidance: GuidanceLaneResult = field(default_factory=GuidanceLaneResult)
    expansion_rounds: int = 0
    stop_reason: str = STOP_REASON_COMPLETE
    incomplete: bool = False
    trace: dict[str, Any] = field(default_factory=dict)

    @property
    def accepted_article_ids(self) -> tuple[str, ...]:
        ordered: list[str] = []
        for requirement in self.requirements:
            for article_id in requirement.accepted_article_ids:
                if article_id not in ordered:
                    ordered.append(article_id)
        return tuple(ordered)


class LayeredRetriever:
    """Requirement単位の探索を、外部呼び出し回数を抑えたラウンド処理で行う。"""

    def __init__(
        self,
        os_client: Any,
        graph_client: Any,
        reranker_client: Any = None,
    ) -> None:
        self.os_client = os_client
        self.graph_client = graph_client
        self.reranker_client = reranker_client
        self.guidance_lane = GuidanceLane(os_client, graph_client)

    def retrieve(
        self,
        plan: IssuePlan,
        *,
        tracker: BudgetTracker,
        user_clearance_level: int = 2,
        authority_types_by_article: dict[str, str] | None = None,
    ) -> LayeredRetrievalResult:
        requirements, groups = assign_conclusion_groups(
            initial_requirements(plan.issues), plan.issues
        )
        store = RequirementStore.empty(
            max_total=settings.layered_max_requirements_total
        ).add_all(requirements)
        state = _RetrievalState(
            tracker=tracker,
            user_clearance_level=user_clearance_level,
            authority_types_by_article=authority_types_by_article,
        )

        # ガイドレーンを先に走らせ、自然言語と法令用語を橋渡しする条文候補を得る(§10)。
        # ここで得るのは候補であって根拠ではない。法令本文の取得は法令レーンが行う。
        state.guidance = self.guidance_lane.explore(
            plan.issues,
            tracker=tracker,
            user_clearance_level=user_clearance_level,
        )

        # round 0: 全初期Requirementを、時間・件数予算内で枯渇するまで処理する(§8.2)。
        frontier = self._run_round(store, state, round_index=0)
        store, next_frontier = frontier

        expansion_rounds = 0
        for round_index in range(1, settings.layered_max_expansion_rounds + 1):
            if not next_frontier or state.stopped:
                break
            expansion_rounds = round_index
            store = store.add_all(next_frontier)
            store, next_frontier = self._run_round(store, state, round_index=round_index)
        if next_frontier and not state.stopped:
            # 未処理の子Requirementが残ったままラウンド上限に達した(§8.8)。
            state.stop_reason = STOP_REASON_ROUNDS

        if state.stopped:
            store = store.mark_pending_unresolved(state.stop_reason)

        requirements_out, groups = assign_conclusion_groups(store.requirements, plan.issues)
        return LayeredRetrievalResult(
            issues=tuple(plan.issues),
            requirements=requirements_out,
            groups=groups,
            article_candidates=dict(state.candidates_by_requirement),
            guidance=state.guidance,
            expansion_rounds=expansion_rounds,
            stop_reason=state.stop_reason,
            incomplete=state.stopped,
            trace={
                "searchesByRequirement": dict(state.searches_by_requirement),
                "rerankByRequirement": dict(state.rerank_by_requirement),
                "satisfactionByRequirement": dict(state.satisfaction_by_requirement),
                "graphEdgesConsidered": list(state.graph_edges_considered),
                "graphEdgesAccepted": list(state.graph_edges_accepted),
                "graphEdgesRejected": list(state.graph_edges_rejected),
                "requirementTransitions": list(store.transitions),
                "evidenceRequirements": store.as_trace(),
                "conclusionGroups": [group.as_trace() for group in groups],
                "articleCandidateCount": state.article_candidate_count,
                "articleCandidateBudget": {
                    "limitReached": state.article_budget_reached,
                    "evictedCandidateIds": list(state.evicted_candidate_ids),
                    "exhaustedRequirementIds": list(state.exhausted_requirement_ids),
                },
                "rerankPairCount": state.rerank_pairs,
                "expansionRounds": expansion_rounds,
                "requirementLimitReached": any(
                    requirement.over_budget for requirement in store.requirements
                ),
                "stopReason": state.stop_reason,
                "guidanceLane": dict(state.guidance.trace),
                "guidanceRelationAssertions": [
                    dict(assertion) for assertion in state.guidance.assertions
                ],
                "fallbacks": list(state.fallbacks),
            },
        )

    # ------------------------------------------------------------------ ラウンド処理

    def _run_round(
        self,
        store: RequirementStore,
        state: "_RetrievalState",
        *,
        round_index: int,
    ) -> tuple[RequirementStore, tuple[EvidenceRequirement, ...]]:
        children: list[EvidenceRequirement] = []
        state.begin_round()
        while store.pending():
            if not state.can_continue():
                return store, tuple(children)
            batch, store = store.pop_priority_batch(
                max_active_issues=settings.layered_active_issue_batch_size
            )
            if not batch:
                break
            store, batch_children = self._process_batch(batch, store, state, round_index)
            # 同じラウンドの探索キューへ割り込ませず、次ラウンドで処理する(§8.2)。
            children.extend(batch_children)
        return store, tuple(children)

    def _process_batch(
        self,
        batch: tuple[EvidenceRequirement, ...],
        store: RequirementStore,
        state: "_RetrievalState",
        round_index: int,
    ) -> tuple[RequirementStore, tuple[EvidenceRequirement, ...]]:
        specs = [self._search_spec(requirement, state) for requirement in batch]
        results = self._search(specs, state)
        direct_targets = {
            spec.requirement_id: set(spec.article_ids)
            for spec in specs
        }
        for requirement_id, candidates in results.items():
            targets = direct_targets.get(requirement_id, set())
            for candidate in candidates:
                if str(candidate.get("articleId") or "") in targets:
                    candidate["directMatch"] = True
        ordered_by_requirement = self._rerank_batch(batch, results, state)
        children: list[EvidenceRequirement] = []

        for requirement in batch:
            candidates = results.get(requirement.requirement_id, [])
            state.searches_by_requirement[requirement.requirement_id] = {
                "query": next(
                    spec.query for spec in specs if spec.requirement_id == requirement.requirement_id
                ),
                "authorityType": requirement.authority_type,
                "documentIds": list(requirement.document_id and [requirement.document_id] or []),
                "articleIds": list(requirement.article_id and [requirement.article_id] or []),
                "roundIndex": round_index,
                "candidateCount": len(candidates),
            }
            if not candidates:
                store = store.update(
                    requirement.with_status(
                        RETRIEVAL_STATUS_EXHAUSTED, reason="no_candidate_article"
                    ),
                    reason="no_candidate_article",
                )
                continue

            ordered = ordered_by_requirement.get(requirement.requirement_id, candidates)
            assessments = {
                str(candidate.get("articleId") or ""): assess_candidate(
                    requirement, candidate
                )
                for candidate in ordered
            }
            state.satisfaction_by_requirement[requirement.requirement_id] = {
                article_id: assessment.as_trace()
                for article_id, assessment in assessments.items()
            }
            satisfying_ids = {
                article_id
                for article_id, assessment in assessments.items()
                if assessment.satisfied
            }
            pooled, accepted = state.allocate_candidates(
                requirement,
                ordered,
                satisfying_article_ids=satisfying_ids,
            )
            if not accepted:
                reason = (
                    STOP_REASON_ARTICLE_BUDGET
                    if not pooled and state.article_budget_reached
                    else "no_satisfying_article"
                )
                store = store.update(
                    requirement.with_candidates(
                        [str(candidate["articleId"]) for candidate in pooled]
                    ).with_status(RETRIEVAL_STATUS_CANDIDATE_FOUND, reason=reason),
                    reason=reason,
                )
                continue

            accepted_ids = tuple(str(candidate["articleId"]) for candidate in accepted)
            updated = (
                requirement.with_candidates(
                    [str(candidate["articleId"]) for candidate in pooled]
                )
                .with_accepted(accepted_ids)
                .with_status(RETRIEVAL_STATUS_RESOLVED)
            )
            store = store.update(updated, reason="article_text_fetched")
            children.extend(self._children_from_text(updated, accepted))

        children.extend(self._children_from_graph(batch, store, state))
        return store, tuple(children)

    # ------------------------------------------------------------------ 外部呼び出し

    def _search(
        self,
        specs: list[RequirementSearchSpec],
        state: "_RetrievalState",
    ) -> dict[str, list[dict[str, Any]]]:
        tracker = state.tracker
        if not specs:
            return {}
        if state.search_calls_this_round >= settings.layered_max_search_batch_calls_per_round:
            return {}
        if not tracker.can_invoke(
            COMPONENT_SEARCH, max_invocations=settings.layered_max_search_batch_calls_total
        ):
            # 時間ではなく呼び出し回数の上限。停止理由を取り違えない(§13)。
            state.stop(
                STOP_REASON_TIME if not tracker.can_continue() else STOP_REASON_SEARCH_CALLS
            )
            return {}
        timeout = tracker.effective_timeout(COMPONENT_SEARCH)
        if timeout <= 0:
            state.stop(STOP_REASON_TIME)
            return {}
        started = perf_counter()
        try:
            results = self.os_client.search_requirement_specs(
                specs,
                user_clearance_level=state.user_clearance_level,
                timeout_sec=timeout,
            )
        except Exception as exc:  # 検索障害で新方式全体を落とさない(§12)
            state.fallbacks.append({"component": COMPONENT_SEARCH, "error": str(exc)})
            results = {}
        tracker.record(
            COMPONENT_SEARCH,
            items=len(specs),
            elapsed_ms=int((perf_counter() - started) * 1000),
        )
        state.search_calls_this_round += 1
        return results

    def _rerank_batch(
        self,
        requirements: tuple[EvidenceRequirement, ...],
        candidates_by_requirement: dict[str, list[dict[str, Any]]],
        state: "_RetrievalState",
    ) -> dict[str, list[dict[str, Any]]]:
        ordered = {
            requirement.requirement_id: list(
                candidates_by_requirement.get(requirement.requirement_id, [])
            )
            for requirement in requirements
        }
        if self.reranker_client is None:
            return ordered
        eligible = [
            requirement
            for requirement in requirements
            if len(ordered[requirement.requirement_id]) >= 2
        ]
        # 1呼び出しで各Requirementへ最低2ペアを割り当てる。
        per_call_requirements = max(
            1,
            settings.layered_max_rerank_pairs_per_call // 2,
        )
        while eligible:
            if (
                state.rerank_calls_this_round
                >= settings.layered_max_rerank_calls_per_round
            ):
                for requirement in eligible:
                    state.rerank_by_requirement[requirement.requirement_id] = {
                        "used": False,
                        "pairs": 0,
                        "error": "rerank_round_call_budget_exhausted",
                        "orderedArticleIds": [
                            str(candidate.get("articleId") or "")
                            for candidate in ordered[requirement.requirement_id]
                        ],
                    }
                break
            current = eligible[:per_call_requirements]
            eligible = eligible[per_call_requirements:]
            entries = [
                RequirementRerankInput(
                    requirement_id=requirement.requirement_id,
                    query=_requirement_query(requirement),
                    candidates=tuple(ordered[requirement.requirement_id]),
                    protected_article_ids=tuple(
                        article_id
                        for article_id in (requirement.article_id,)
                        if article_id
                    ),
                )
                for requirement in current
            ]
            result = rerank_requirement_batch(
                self.reranker_client,
                entries,
                budget=RerankBudget(),
                tracker=state.tracker,
                used_pairs=state.rerank_pairs,
                timeout_sec=state.tracker.effective_timeout("rerank"),
            )
            state.rerank_pairs += result.pairs
            if result.invoked:
                state.rerank_calls_this_round += 1
            for requirement in current:
                item = result.results.get(requirement.requirement_id)
                if item is None:
                    continue
                ordered[requirement.requirement_id] = list(item.candidates)
                state.rerank_by_requirement[requirement.requirement_id] = (
                    item.as_trace()
                )
        for requirement in requirements:
            if requirement.requirement_id not in state.rerank_by_requirement:
                state.rerank_by_requirement[requirement.requirement_id] = {
                    "used": False,
                    "pairs": 0,
                    "error": None,
                    "orderedArticleIds": [
                        str(candidate.get("articleId") or "")
                        for candidate in ordered[requirement.requirement_id]
                    ],
                }
        return ordered

    def _children_from_text(
        self,
        requirement: EvidenceRequirement,
        accepted: list[dict[str, Any]],
    ) -> list[EvidenceRequirement]:
        children: list[EvidenceRequirement] = []
        for candidate in accepted:
            text = " ".join(
                str(chunk.get("text") or "") for chunk in (candidate.get("chunks") or [])
            )
            children.extend(
                child_requirements_from_article_text(
                    requirement,
                    article_id=str(candidate["articleId"]),
                    text=text,
                )
            )
        return children

    def _children_from_graph(
        self,
        batch: tuple[EvidenceRequirement, ...],
        store: RequirementStore,
        state: "_RetrievalState",
    ) -> list[EvidenceRequirement]:
        tracker = state.tracker
        start_ids: list[str] = []
        for requirement in batch:
            for article_id in store.get(requirement.requirement_id).accepted_article_ids:
                if article_id not in start_ids:
                    start_ids.append(article_id)
        if not start_ids or self.graph_client is None:
            return []
        if state.graph_calls_this_round >= settings.layered_max_graph_batch_calls_per_round:
            return []
        if not tracker.can_invoke(
            COMPONENT_GRAPH, max_invocations=settings.layered_max_graph_batch_calls_total
        ):
            return []
        timeout = tracker.effective_timeout(COMPONENT_GRAPH)
        if timeout <= 0:
            state.stop(STOP_REASON_TIME)
            return []
        started = perf_counter()
        try:
            # 1 query = 1 hop を基本とし、論理ホップはラウンドで数える(§8.5)。
            paths = self.graph_client.paths_from_many(
                start_ids,
                edge_types=list(expandable_edge_types()),
                max_depth=1,
                limit=settings.agent_max_graph_paths,
                user_clearance_level=state.user_clearance_level,
                timeout_sec=timeout,
            )
        except Exception as exc:  # Graph障害時は明示参照・法令内検索を継続する(§12)
            state.fallbacks.append({"component": COMPONENT_GRAPH, "error": str(exc)})
            paths = []
        tracker.record(
            COMPONENT_GRAPH,
            items=len(start_ids),
            elapsed_ms=int((perf_counter() - started) * 1000),
        )
        state.graph_calls_this_round += 1

        edges_by_article: dict[str, list[dict[str, Any]]] = {}
        for path in paths:
            for edge in path.get("edges") or []:
                state.graph_edges_considered.append(dict(edge))
                edges_by_article.setdefault(str(edge.get("fromGraphNodeId") or ""), []).append(edge)

        children: list[EvidenceRequirement] = []
        for requirement in batch:
            current = store.get(requirement.requirement_id)
            edges = [
                edge
                for article_id in current.accepted_article_ids
                for edge in edges_by_article.get(article_id, [])
            ]
            if not edges:
                continue
            generated = child_requirements_from_graph(
                current,
                edges,
                authority_types_by_article=state.authority_types_by_article,
                max_children=settings.layered_max_child_relations_per_article,
            )
            accepted_targets = {child.article_id for child in generated}
            for edge in edges:
                bucket = (
                    state.graph_edges_accepted
                    if str(edge.get("toGraphNodeId")) in accepted_targets
                    else state.graph_edges_rejected
                )
                bucket.append(dict(edge))
            children.extend(generated)
        return children

    def _search_spec(
        self,
        requirement: EvidenceRequirement,
        state: "_RetrievalState",
    ) -> RequirementSearchSpec:
        article_ids = [requirement.article_id] if requirement.article_id else []
        document_ids = [requirement.document_id] if requirement.document_id else []
        if requirement.depth == 0 and not requirement.article_id:
            # EXPLAINSで明示的に解説対象とされた条文は、索引として直接取得してよい(§10-2)。
            article_ids.extend(
                state.guidance.explained_article_ids_by_issue.get(requirement.issue_id, ())
            )
            # MENTIONS・未確認assertion由来は条文を確実投入せず、検索範囲の拡張だけに使う。
            if not document_ids:
                document_ids.extend(
                    state.guidance.candidate_document_ids_by_issue.get(requirement.issue_id, ())
                )
        return RequirementSearchSpec(
            requirement_id=requirement.requirement_id,
            query=_requirement_query(requirement),
            authority_type=requirement.authority_type,
            document_ids=tuple(dict.fromkeys(document_ids)),
            article_ids=tuple(dict.fromkeys(article_ids)),
            # 委任先・準用先は親条文と同じ法令系統から探す(§6.3-7)。
            family_document_ids=family_document_ids(requirement.family_root),
            top_k=settings.layered_max_articles_per_requirement * 3,
            key_terms=requirement.key_terms,
        )


def _requirement_query(requirement: EvidenceRequirement) -> str:
    """論点・法的役割・親条文から、Requirement専用の検索語を作る(§9.2)。"""
    parts = [requirement.query_hint, *requirement.key_terms]
    parts.append(ROLE_QUERY_HINTS.get(requirement.role_family, ""))
    parts.extend(
        ROLE_SUBTYPE_QUERY_HINTS.get(subtype, "") for subtype in requirement.role_subtypes
    )
    return " ".join(part for part in parts if part).strip()[:200]


class _RetrievalState:
    """1リクエスト分の探索状態。Article候補枠の配分と予算消費を集約する。"""

    def __init__(
        self,
        *,
        tracker: BudgetTracker,
        user_clearance_level: int,
        authority_types_by_article: dict[str, str] | None = None,
    ) -> None:
        self.tracker = tracker
        self.user_clearance_level = user_clearance_level
        self.authority_types_by_article = authority_types_by_article or {}
        self.candidates_by_requirement: dict[str, list[dict[str, Any]]] = {}
        self.searches_by_requirement: dict[str, dict[str, Any]] = {}
        self.rerank_by_requirement: dict[str, dict[str, Any]] = {}
        self.satisfaction_by_requirement: dict[str, dict[str, Any]] = {}
        self.graph_edges_considered: list[dict[str, Any]] = []
        self.graph_edges_accepted: list[dict[str, Any]] = []
        self.graph_edges_rejected: list[dict[str, Any]] = []
        self.evicted_candidate_ids: list[str] = []
        self.exhausted_requirement_ids: list[str] = []
        self.fallbacks: list[dict[str, Any]] = []
        self.guidance = GuidanceLaneResult()
        self.article_candidate_count = 0
        self.article_budget_reached = False
        self.rerank_pairs = 0
        self.search_calls_this_round = 0
        self.graph_calls_this_round = 0
        self.rerank_calls_this_round = 0
        self.stopped = False
        self.stop_reason = STOP_REASON_COMPLETE
        self._evictable_candidates: list[tuple[str, str]] = []

    def begin_round(self) -> None:
        self.search_calls_this_round = 0
        self.graph_calls_this_round = 0
        self.rerank_calls_this_round = 0

    def can_continue(self) -> bool:
        if self.stopped:
            return False
        if not self.tracker.can_continue():
            self.stop(STOP_REASON_TIME)
            return False
        return True

    def stop(self, reason: str) -> None:
        if not self.stopped:
            self.stopped = True
            self.stop_reason = reason

    def allocate_candidates(
        self,
        requirement: EvidenceRequirement,
        ordered: list[dict[str, Any]],
        *,
        satisfying_article_ids: set[str],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Article候補総数を先着順で消費させず、mandatoryへ最低枠を確保する(§8.8)。

        1. 各mandatory Requirementへ最低1件の機会を与える。
        2. 上限に達している場合は、未採用のoptional候補を退避して枠を作る。
        3. 1 Requirementあたりは`MAX_ACCEPTED_ARTICLES_PER_REQUIREMENT`件までとする。
        """
        limit = settings.layered_max_article_candidates_total
        per_requirement = settings.layered_max_accepted_articles_per_requirement
        pooled: list[dict[str, Any]] = []
        accepted: list[dict[str, Any]] = []
        for candidate in ordered[: settings.layered_max_articles_per_requirement]:
            if self.article_candidate_count >= limit:
                self.article_budget_reached = True
                if not (requirement.mandatory and self._evict_low_priority_candidate()):
                    if requirement.requirement_id not in self.exhausted_requirement_ids:
                        self.exhausted_requirement_ids.append(requirement.requirement_id)
                    break
            self.article_candidate_count += 1
            pooled.append(candidate)
            article_id = str(candidate["articleId"])
            if (
                article_id in satisfying_article_ids
                and len(accepted) < per_requirement
            ):
                accepted.append(candidate)
            else:
                # mandatory Requirementでも、採用上限を超えた低順位候補や充足しない候補は
                # 後続の高優先度mandatoryへ枠を譲れる。採用済みArticleは登録しない。
                self._evictable_candidates.append(
                    (requirement.requirement_id, article_id)
                )
        if pooled:
            self.candidates_by_requirement[requirement.requirement_id] = pooled
        return pooled, accepted

    def _evict_low_priority_candidate(self) -> bool:
        """採用済みArticleを残し、optionalまたはRequirement内の低順位余剰を退避する。"""
        if not self._evictable_candidates:
            return False
        requirement_id, article_id = self._evictable_candidates.pop()
        candidates = self.candidates_by_requirement.get(requirement_id, [])
        self.candidates_by_requirement[requirement_id] = [
            candidate
            for candidate in candidates
            if str(candidate.get("articleId") or "") != article_id
        ]
        self.evicted_candidate_ids.append(article_id)
        self.article_candidate_count = max(0, self.article_candidate_count - 1)
        return True


def sort_requirements_for_context(
    requirements: tuple[EvidenceRequirement, ...],
) -> list[EvidenceRequirement]:
    return sorted(
        requirements,
        key=lambda requirement: (
            priority_rank(requirement.priority),
            not requirement.mandatory,
            requirement.requirement_id,
        ),
    )
