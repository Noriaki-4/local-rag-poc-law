"""CaseStateから意味選別なしでSolver入力を組み立てる。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import Field

from .contracts import SolverDecision
from .profiles import AgentLimits
from .state import (
    CaseState,
    DeferredFrontierResolutionAction,
    DependencyDecision,
    Evidence,
    FrameworkModel,
    FrontierReviewStatus,
    Hypothesis,
    ReviewFinding,
    ToolRequest,
    ToolResult,
    ToolStatus,
    WorkItem,
)


class WorkTreeItem(FrameworkModel):
    work_item_id: str
    parent_work_item_id: str | None
    question: str
    state: str
    resolution: str | None
    basis_hypothesis_ids: tuple[str, ...]
    replaces_work_item_id: str | None
    hypothesis_ids: tuple[str, ...]
    evidence_count: int = Field(ge=0)


class EvidenceManifestItem(FrameworkModel):
    evidence_id: str
    source_ref: str
    title: str | None
    content_chars: int = Field(ge=0)
    created_cycle: int = Field(ge=1)
    material_included: bool


GraphCandidateContentStatus = Literal[
    "not_requested",
    "pending",
    "succeeded",
    "failed",
    "timeout",
]


class GraphCandidateArticle(FrameworkModel):
    """Graph端点ArticleのSolver向け正規化投影。"""

    article_id: str
    document_id: str | None
    title: str | None
    heading: str | None
    content_status: GraphCandidateContentStatus


class GraphCandidateLink(FrameworkModel):
    """Articleの重複なしに全発見経路を保持する投影。"""

    link_id: str
    seed_article_id: str
    candidate_article_id: str
    work_item_ids: tuple[str, ...]
    hypothesis_ids: tuple[str, ...]
    relations: tuple[dict[str, Any], ...]
    graph_request_ids: tuple[str, ...]


class GraphCandidateCatalog(FrameworkModel):
    articles: tuple[GraphCandidateArticle, ...] = ()
    links: tuple[GraphCandidateLink, ...] = ()


class GraphReviewCandidate(FrameworkModel):
    frontier_item_id: str
    article_id: str
    document_id: str | None
    title: str | None
    heading: str | None
    work_item_id: str
    hypothesis_id: str | None
    review_trigger: Literal["new_frontier", "re_adopted", "new_link"]
    prior_review_status: FrontierReviewStatus | None
    content_status: GraphCandidateContentStatus
    links: tuple[GraphCandidateLink, ...]


class GraphReviewLedgerItem(FrameworkModel):
    frontier_item_id: str
    article_id: str
    title: str | None
    heading: str | None
    work_item_id: str
    hypothesis_id: str | None
    review_status: Literal["selected", "relevant_deferred", "rejected"]
    reason: str
    content_status: GraphCandidateContentStatus
    last_reviewed_cycle: int | None
    deferred_resolution_action: DeferredFrontierResolutionAction | None = None
    deferred_resolution_reason: str | None = None


class GraphReviewBatch(FrameworkModel):
    candidates: tuple[GraphReviewCandidate, ...] = ()
    remaining_unreviewed_count: int = Field(default=0, ge=0)


class SolverToolResult(FrameworkModel):
    """CaseStateのToolResultからLLMに必要な実行状態だけを投影する。"""

    request_id: str
    status: ToolStatus
    evidence_ids: tuple[str, ...]
    evidence_count: int = Field(ge=0)
    graph_projection_updated: bool
    error_code: str | None
    elapsed_ms: int = Field(ge=0)
    cycle_no: int = Field(ge=1)


class SearchCandidateArticle(FrameworkModel):
    """OpenSearch候補と、その発見要求を意味選別なしで対応付ける。"""

    article_id: str
    document_id: str | None
    title: str | None
    headings: tuple[str, ...]
    discovery_work_item_ids: tuple[str, ...]
    discovery_hypothesis_ids: tuple[str, ...]
    search_request_ids: tuple[str, ...]
    navigation_evidence_ids: tuple[str, ...]


class SolverContractFeedback(FrameworkModel):
    violation: str
    previous_decision: SolverDecision


class SolverContext(FrameworkModel):
    case_id: str
    question: str
    research_cycle_count: int
    remaining_research_cycles: int
    remaining_wall_time_sec: float
    min_next_cycle_budget_sec: float
    can_start_next_cycle: bool
    max_tool_requests_per_step: int
    max_fetched_resources_per_cycle: int
    fetched_resource_ids_this_cycle: tuple[str, ...]
    remaining_fetch_capacity: int = Field(ge=0)
    max_selected_frontier_per_step: int
    cycle_budget_reached: bool
    cycle_close_required: bool
    cycle_step_timeout: bool
    max_retained_evidence: int
    max_material_evidence_chars: int
    max_solver_input_chars: int
    finalize_only: bool
    grounding_evidence_ids: tuple[str, ...]
    navigation_evidence_ids: tuple[str, ...]
    fetchable_article_ids: tuple[str, ...]
    search_candidates: tuple[SearchCandidateArticle, ...] = ()
    work_tree: tuple[WorkTreeItem, ...]
    hypotheses: tuple[Hypothesis, ...]
    focus_work_items: tuple[WorkItem, ...]
    affected_work_items: tuple[WorkItem, ...]
    used_tool_request_ids: tuple[str, ...] = Field(default=(), exclude=True)
    recent_tool_requests: tuple[ToolRequest, ...]
    recent_tool_results: tuple[SolverToolResult, ...]
    evidence_manifest: tuple[EvidenceManifestItem, ...]
    graph_review_batch: GraphReviewBatch
    graph_review_ledger: tuple[GraphReviewLedgerItem, ...]
    required_graph_review_request_ids: tuple[str, ...] = ()
    required_search_review_request_ids: tuple[str, ...] = ()
    material_evidence: tuple[Evidence, ...]
    omitted_evidence_ids: tuple[str, ...]
    required_dependency_kind: str | None = None
    required_dependency_work_item_ids: tuple[str, ...] = ()
    dependency_decisions: tuple[DependencyDecision, ...] = ()
    reviewer_findings: tuple[ReviewFinding, ...] = ()
    contract_feedback: SolverContractFeedback | None = None

    @property
    def material_evidence_ids(self) -> frozenset[str]:
        return frozenset(item.evidence_id for item in self.material_evidence)


def build_solver_context(
    state: CaseState,
    limits: AgentLimits,
    *,
    remaining_wall_time_sec: float,
    finalize_only: bool,
    reviewer_findings: tuple[ReviewFinding, ...] = (),
    contract_feedback: SolverContractFeedback | None = None,
    required_dependency_kind: str | None = None,
    required_dependency_work_item_ids: tuple[str, ...] = (),
) -> SolverContext:
    hypotheses_by_work: dict[str, list[Hypothesis]] = {}
    for hypothesis in state.hypotheses:
        hypotheses_by_work.setdefault(hypothesis.work_item_id, []).append(hypothesis)

    evidence_by_id = {item.evidence_id: item for item in state.evidence}
    contradicted_ids = {
        item.hypothesis_id
        for item in state.hypotheses
        if item.judgment == "contradicted"
    }
    directly_affected_ids = {
        item.work_item_id
        for item in state.work_items
        if item.state == "open"
        and contradicted_ids.intersection(item.basis_hypothesis_ids)
    }
    affected_ids = set(directly_affected_ids)
    frontier = list(directly_affected_ids)
    while frontier:
        parent_id = frontier.pop()
        child_ids = {
            item.work_item_id
            for item in state.work_items
            if item.parent_work_item_id == parent_id
        }
        unseen_ids = child_ids - affected_ids
        affected_ids.update(unseen_ids)
        frontier.extend(unseen_ids)
    focus_ids = set(state.focus_work_item_ids)

    work_tree = tuple(
        WorkTreeItem(
            work_item_id=item.work_item_id,
            parent_work_item_id=item.parent_work_item_id,
            question=item.question,
            state=item.state,
            resolution=item.resolution,
            basis_hypothesis_ids=item.basis_hypothesis_ids,
            replaces_work_item_id=item.replaces_work_item_id,
            hypothesis_ids=tuple(
                hypothesis.hypothesis_id
                for hypothesis in hypotheses_by_work.get(item.work_item_id, ())
            ),
            evidence_count=len(
                {
                    evidence_id
                    for hypothesis in hypotheses_by_work.get(item.work_item_id, ())
                    for evidence_id in hypothesis.evidence_ids
                }
            ),
        )
        for item in state.work_items
    )

    recent_results = tuple(
        item
        for item in state.tool_results
        if item.cycle_no == state.research_cycle_count
    )
    recent_request_ids = {item.request_id for item in recent_results}
    recent_requests = tuple(
        item for item in state.tool_requests if item.request_id in recent_request_ids
    )
    recent_requests_by_id = {item.request_id: item for item in recent_requests}
    new_evidence_ids = _round_robin_result_evidence_ids(recent_results)
    declared_basis_evidence_ids = tuple(
        evidence_id
        for work_item in state.work_items
        if work_item.state != "dropped"
        for hypothesis in hypotheses_by_work.get(work_item.work_item_id, ())
        if hypothesis.hypothesis_id in work_item.basis_hypothesis_ids
        for evidence_id in hypothesis.evidence_ids
    )
    reviewer_basis_evidence_ids = tuple(
        evidence_id
        for finding in reviewer_findings
        for evidence_id in finding.basis_evidence_ids
    )
    material_ids = tuple(
        dict.fromkeys(
            [
                *new_evidence_ids,
                *state.retained_evidence_ids,
                *declared_basis_evidence_ids,
                *reviewer_basis_evidence_ids,
            ]
        )
    )
    material_items: list[Evidence] = []
    material_chars = 0
    for evidence_id in material_ids:
        evidence = evidence_by_id.get(evidence_id)
        if evidence is None:
            continue
        if evidence.metadata.get("docType") == "graph_navigation":
            # 同じ機械情報を本文枠へ重複掲載せず、探索Graphの正本から差分投影する。
            continue
        evidence_chars = len(evidence.content)
        if evidence_chars > limits.max_material_evidence_chars:
            raise ContextCapacityExceeded(
                "context_capacity_exceeded: single Evidence exceeds "
                "max_material_evidence_chars"
            )
        if material_chars + evidence_chars > limits.max_material_evidence_chars:
            continue
        material_items.append(evidence)
        material_chars += evidence_chars
    material = tuple(material_items)
    included_ids = {item.evidence_id for item in material}
    graph_navigation_ids = frozenset(
        item.evidence_id
        for item in state.evidence
        if item.metadata.get("docType") == "graph_navigation"
    )
    fetched_resource_ids_this_cycle = _fetched_article_ids_for_cycle(
        state,
        max(1, state.research_cycle_count),
    )
    remaining_fetch_capacity = max(
        0,
        limits.max_fetched_resources_per_cycle
        - len(fetched_resource_ids_this_cycle),
    )
    time_requires_cycle_close = (
        not finalize_only
        and remaining_wall_time_sec
        <= limits.finalization_reserve_sec + limits.cycle_close_reserve_sec
    )
    graph_candidate_catalog = _graph_candidate_catalog(state)
    graph_review_batch, graph_review_ledger = _graph_review_projection(
        state,
        graph_candidate_catalog,
        max_candidates=limits.max_graph_candidates_per_review_batch,
    )
    if (
        (remaining_fetch_capacity == 0 or time_requires_cycle_close)
    ):
        graph_review_batch = GraphReviewBatch(
            remaining_unreviewed_count=(
                graph_review_batch.remaining_unreviewed_count
                + len(graph_review_batch.candidates)
            )
        )
    required_graph_review_request_ids = (
        ()
        if finalize_only
        else tuple(
            dict.fromkeys(
                request_id
                for candidate in graph_review_batch.candidates
                for link in candidate.links
                for request_id in link.graph_request_ids
            )
        )
    )
    grounding_ids = tuple(
        item.evidence_id
        for item in material
        if item.metadata.get("citationEligible") is not False
    )
    navigation_ids = tuple(
        dict.fromkeys(
            [
                *(
                    item.evidence_id
                    for item in material
                    if item.metadata.get("citationEligible") is False
                ),
            ]
        )
    )
    graph_fetchable_article_ids = tuple(
        dict.fromkeys(
            [
                *(
                    item.article_id
                    for item in graph_review_batch.candidates
                    if item.content_status in {"not_requested", "failed", "timeout"}
                ),
                *(
                    item.article_id
                    for item in graph_review_ledger
                    if (
                        item.review_status == "relevant_deferred"
                        and item.content_status
                        in {"not_requested", "failed", "timeout"}
                        and item.deferred_resolution_action != "no_longer_needed"
                    )
                    or (
                        item.review_status == "selected"
                        and item.content_status in {"failed", "timeout"}
                    )
                ),
            ]
        )
    )
    successfully_fetched_article_ids = set(
        _fetched_article_ids_for_cycle(state, None)
    )
    fetchable_article_ids = tuple(
        dict.fromkeys(
            [
                *(
                    article_id
                    for item in material
                    if item.metadata.get("citationEligible") is False
                    for article_id in _evidence_article_ids(item)
                ),
                *graph_fetchable_article_ids,
            ]
        )
    )
    fetchable_article_ids = tuple(
        item
        for item in fetchable_article_ids
        if item not in successfully_fetched_article_ids
    )
    search_candidates = _search_candidate_projection(
        recent_results=recent_results,
        recent_requests_by_id=recent_requests_by_id,
        evidence_by_id=evidence_by_id,
        fetchable_article_ids=fetchable_article_ids,
    )
    search_request_ids = tuple(
        dict.fromkeys(
            request_id
            for candidate in search_candidates
            for request_id in candidate.search_request_ids
        )
    )
    latest_request = (
        recent_requests_by_id.get(recent_results[-1].request_id)
        if recent_results
        else None
    )
    reviewed_search_request_sets = {
        frozenset(review.search_request_ids)
        for review in state.search_candidate_reviews
    }
    required_search_review_request_ids = (
        search_request_ids
        if not finalize_only
        and latest_request is not None
        and latest_request.tool_name == "legal_search"
        and frozenset(search_request_ids) not in reviewed_search_request_sets
        else ()
    )
    manifest = tuple(
        EvidenceManifestItem(
            evidence_id=item.evidence_id,
            source_ref=item.source_ref,
            title=item.title,
            content_chars=len(item.content),
            created_cycle=item.created_cycle,
            material_included=item.evidence_id in included_ids,
        )
        for item in state.evidence
        if item.evidence_id not in graph_navigation_ids
    )
    solver_recent_results = tuple(
        _solver_tool_result(
            item,
            request=recent_requests_by_id.get(item.request_id),
            graph_navigation_ids=graph_navigation_ids,
        )
        for item in recent_results
    )

    return SolverContext(
        case_id=state.case_id,
        question=state.question,
        research_cycle_count=state.research_cycle_count,
        remaining_research_cycles=max(
            0, limits.max_research_cycles - state.research_cycle_count
        ),
        remaining_wall_time_sec=max(0.0, remaining_wall_time_sec),
        min_next_cycle_budget_sec=limits.min_next_cycle_budget_sec,
        can_start_next_cycle=(
            not finalize_only
            and state.research_cycle_count < limits.max_research_cycles
            and remaining_wall_time_sec
            > limits.finalization_reserve_sec + limits.min_next_cycle_budget_sec
        ),
        max_tool_requests_per_step=limits.max_tool_requests_per_step,
        max_fetched_resources_per_cycle=limits.max_fetched_resources_per_cycle,
        fetched_resource_ids_this_cycle=fetched_resource_ids_this_cycle,
        remaining_fetch_capacity=remaining_fetch_capacity,
        max_selected_frontier_per_step=limits.max_selected_frontier_per_step,
        cycle_budget_reached=remaining_fetch_capacity == 0,
        cycle_close_required=(
            remaining_fetch_capacity == 0
            or time_requires_cycle_close
            or state.cycle_step_timeout
        ),
        cycle_step_timeout=state.cycle_step_timeout,
        max_retained_evidence=limits.max_retained_evidence,
        max_material_evidence_chars=limits.max_material_evidence_chars,
        max_solver_input_chars=limits.max_solver_input_chars,
        finalize_only=finalize_only,
        grounding_evidence_ids=grounding_ids,
        navigation_evidence_ids=navigation_ids,
        fetchable_article_ids=fetchable_article_ids,
        search_candidates=search_candidates,
        work_tree=work_tree,
        hypotheses=state.hypotheses,
        focus_work_items=tuple(
            item for item in state.work_items if item.work_item_id in focus_ids
        ),
        affected_work_items=tuple(
            item for item in state.work_items if item.work_item_id in affected_ids
        ),
        used_tool_request_ids=tuple(item.request_id for item in state.tool_requests),
        recent_tool_requests=recent_requests,
        recent_tool_results=solver_recent_results,
        evidence_manifest=manifest,
        graph_review_batch=graph_review_batch,
        graph_review_ledger=graph_review_ledger,
        required_graph_review_request_ids=required_graph_review_request_ids,
        required_search_review_request_ids=required_search_review_request_ids,
        material_evidence=material,
        omitted_evidence_ids=tuple(
            item.evidence_id
            for item in state.evidence
            if item.evidence_id not in included_ids
            and item.evidence_id not in graph_navigation_ids
        ),
        required_dependency_kind=required_dependency_kind,
        required_dependency_work_item_ids=required_dependency_work_item_ids,
        dependency_decisions=state.dependency_decisions,
        reviewer_findings=reviewer_findings,
        contract_feedback=contract_feedback,
    )


class ContextCapacityExceeded(ValueError):
    """候補を欠落させずにSolver入力へ収められない。"""


def _search_candidate_projection(
    *,
    recent_results: tuple[ToolResult, ...],
    recent_requests_by_id: dict[str, ToolRequest],
    evidence_by_id: dict[str, Evidence],
    fetchable_article_ids: tuple[str, ...],
) -> tuple[SearchCandidateArticle, ...]:
    """検索要求と候補Articleの既存参照だけをArticle単位にまとめる。"""

    fetchable_ids = set(fetchable_article_ids)
    candidates: dict[str, dict[str, Any]] = {}
    for result in recent_results:
        request = recent_requests_by_id.get(result.request_id)
        if request is None or request.tool_name != "legal_search":
            continue
        for evidence_id in result.evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                continue
            for article_id in _evidence_article_ids(evidence):
                if article_id not in fetchable_ids:
                    continue
                candidate = candidates.setdefault(
                    article_id,
                    {
                        "document_id": _nonempty_string(
                            evidence.metadata.get("documentId")
                        ),
                        "title": evidence.title,
                        "headings": [],
                        "discovery_work_item_ids": [],
                        "discovery_hypothesis_ids": [],
                        "search_request_ids": [],
                        "navigation_evidence_ids": [],
                    },
                )
                heading = _nonempty_string(evidence.metadata.get("heading"))
                if heading is not None:
                    _extend_unique(candidate["headings"], (heading,))
                _extend_unique(
                    candidate["discovery_work_item_ids"],
                    (request.work_item_id,),
                )
                _extend_unique(
                    candidate["discovery_hypothesis_ids"],
                    request.hypothesis_ids,
                )
                _extend_unique(
                    candidate["search_request_ids"],
                    (request.request_id,),
                )
                _extend_unique(
                    candidate["navigation_evidence_ids"],
                    (evidence_id,),
                )

    return tuple(
        SearchCandidateArticle(
            article_id=article_id,
            document_id=item["document_id"],
            title=item["title"],
            headings=tuple(item["headings"]),
            discovery_work_item_ids=tuple(item["discovery_work_item_ids"]),
            discovery_hypothesis_ids=tuple(
                item["discovery_hypothesis_ids"]
            ),
            search_request_ids=tuple(item["search_request_ids"]),
            navigation_evidence_ids=tuple(item["navigation_evidence_ids"]),
        )
        for article_id, item in candidates.items()
    )


_GRAPH_RELATION_FIELDS = (
    "kind",
    "edgeType",
    "direction",
    "status",
    "referenceKind",
    "basisEdgeId",
    "classificationRunId",
    "subjectArticleId",
    "objectArticleId",
    "subjectSupportingSpanId",
    "objectSupportingSpanId",
    "subjectSupportingQuote",
    "objectSupportingQuote",
    "relationExplanation",
)


def _graph_candidate_catalog(
    state: CaseState,
) -> GraphCandidateCatalog:
    requests_by_id = {item.request_id: item for item in state.tool_requests}
    hypothesis_work_item_ids = {
        item.hypothesis_id: item.work_item_id for item in state.hypotheses
    }
    work_ids_by_evidence: dict[str, list[str]] = {}
    hypothesis_ids_by_evidence: dict[str, list[str]] = {}
    request_ids_by_evidence: dict[str, list[str]] = {}
    for result in state.tool_results:
        request = requests_by_id.get(result.request_id)
        if request is None or request.tool_name != "legal_graph_neighbors":
            continue
        for evidence_id in result.evidence_ids:
            request_ids = request_ids_by_evidence.setdefault(evidence_id, [])
            if request.request_id not in request_ids:
                request_ids.append(request.request_id)
            work_ids = work_ids_by_evidence.setdefault(evidence_id, [])
            if request.work_item_id not in work_ids:
                work_ids.append(request.work_item_id)
            hypothesis_ids = hypothesis_ids_by_evidence.setdefault(evidence_id, [])
            for hypothesis_id in request.hypothesis_ids:
                if hypothesis_id not in hypothesis_ids:
                    hypothesis_ids.append(hypothesis_id)
                hypothesis_work_item_id = hypothesis_work_item_ids.get(hypothesis_id)
                if (
                    hypothesis_work_item_id is not None
                    and hypothesis_work_item_id not in work_ids
                ):
                    work_ids.append(hypothesis_work_item_id)

    content_status_by_article = _article_content_statuses(state)
    articles: dict[str, dict[str, Any]] = {}
    links: dict[tuple[str, str, str], dict[str, Any]] = {}
    for evidence in state.evidence:
        if evidence.metadata.get("docType") != "graph_navigation":
            continue
        payload = _graph_navigation_payload(evidence)
        seed_article_id = _nonempty_string(
            payload.get("seedArticleId")
            or evidence.metadata.get("seedArticleId")
            or evidence.metadata.get("fromArticleId")
        )
        neighbor_article_id = _nonempty_string(
            payload.get("neighborArticleId")
            or evidence.metadata.get("neighborArticleId")
            or evidence.metadata.get("toArticleId")
        )
        if seed_article_id is None or neighbor_article_id is None:
            continue
        _merge_graph_article(
            articles,
            article_id=seed_article_id,
            document_id=_nonempty_string(
                payload.get("seedDocumentId")
                or evidence.metadata.get("seedDocumentId")
            ),
            title=_nonempty_string(
                payload.get("seedTitle") or evidence.metadata.get("seedTitle")
            ),
            heading=_nonempty_string(
                payload.get("seedHeading") or evidence.metadata.get("seedHeading")
            ),
            content_status=content_status_by_article.get(
                seed_article_id,
                "not_requested",
            ),
        )
        _merge_graph_article(
            articles,
            article_id=neighbor_article_id,
            document_id=_nonempty_string(
                payload.get("neighborDocumentId")
                or evidence.metadata.get("neighborDocumentId")
            ),
            title=_nonempty_string(
                payload.get("neighborTitle")
                or evidence.metadata.get("neighborTitle")
            ),
            heading=_nonempty_string(
                payload.get("neighborHeading")
                or evidence.metadata.get("neighborHeading")
            ),
            content_status=content_status_by_article.get(
                neighbor_article_id,
                "not_requested",
            ),
        )
        raw_relations = payload.get("relations")
        projected_relations = tuple(
            {
                key: relation[key]
                for key in _GRAPH_RELATION_FIELDS
                if key in relation and relation[key] is not None
            }
            for relation in (
                raw_relations if isinstance(raw_relations, list) else []
            )
            if isinstance(relation, dict)
        )
        link_key = (evidence.evidence_id, seed_article_id, neighbor_article_id)
        link = links.setdefault(
            link_key,
            {
                "link_id": _stable_id(
                    "graph-link",
                    evidence.evidence_id,
                    seed_article_id,
                    neighbor_article_id,
                ),
                "work_item_ids": [],
                "hypothesis_ids": [],
                "relations": [],
                "graph_request_ids": [],
            },
        )
        _extend_unique(
            link["work_item_ids"],
            work_ids_by_evidence.get(evidence.evidence_id, ()),
        )
        _extend_unique(
            link["hypothesis_ids"],
            hypothesis_ids_by_evidence.get(evidence.evidence_id, ()),
        )
        _extend_unique(
            link["graph_request_ids"],
            request_ids_by_evidence.get(evidence.evidence_id, ()),
        )
        for relation in projected_relations:
            if relation not in link["relations"]:
                link["relations"].append(relation)

    return GraphCandidateCatalog(
        articles=tuple(
            GraphCandidateArticle(
                article_id=article_id,
                document_id=item["document_id"],
                title=item["title"],
                heading=item["heading"],
                content_status=item["content_status"],
            )
            for article_id, item in articles.items()
        ),
        links=tuple(
            GraphCandidateLink(
                link_id=item["link_id"],
                seed_article_id=seed_article_id,
                candidate_article_id=candidate_article_id,
                work_item_ids=tuple(item["work_item_ids"]),
                hypothesis_ids=tuple(item["hypothesis_ids"]),
                relations=tuple(item["relations"]),
                graph_request_ids=tuple(item["graph_request_ids"]),
            )
            for (_, seed_article_id, candidate_article_id), item in links.items()
        ),
    )


def _graph_review_projection(
    state: CaseState,
    catalog: GraphCandidateCatalog,
    *,
    max_candidates: int,
) -> tuple[GraphReviewBatch, tuple[GraphReviewLedgerItem, ...]]:
    """全履歴から、意味選別せず差分batchと短い最新台帳を作る。"""

    articles_by_id = {item.article_id: item for item in catalog.articles}
    hypothesis_work_ids = {
        item.hypothesis_id: item.work_item_id for item in state.hypotheses
    }
    open_work_ids = {
        item.work_item_id for item in state.work_items if item.state == "open"
    }
    latest_decision_by_frontier = {}
    latest_cycle_by_frontier: dict[str, int | None] = {}
    reviewed_link_ids_by_frontier: dict[str, set[str]] = {}
    for review in state.graph_candidate_reviews:
        for decision in review.frontier_decisions:
            latest_decision_by_frontier[decision.frontier_item_id] = decision
            latest_cycle_by_frontier[decision.frontier_item_id] = review.reviewed_cycle
            reviewed_link_ids_by_frontier.setdefault(
                decision.frontier_item_id,
                set(),
            ).update(review.reviewed_link_ids)

    deferred_resolution_by_frontier = {
        item.frontier_item_id: item
        for item in state.deferred_frontier_resolutions
    }

    re_adoption_keys = {
        (item.article_id, item.work_item_id, item.hypothesis_id)
        for item in state.frontier_re_adoptions
    }
    frontier_records: dict[str, dict[str, Any]] = {}
    for link in catalog.links:
        pairs: list[tuple[str, str | None]] = []
        if link.hypothesis_ids:
            for hypothesis_id in link.hypothesis_ids:
                work_item_id = hypothesis_work_ids.get(hypothesis_id)
                if work_item_id in open_work_ids:
                    pairs.append((work_item_id, hypothesis_id))
        if not pairs:
            pairs.extend(
                (work_item_id, None)
                for work_item_id in link.work_item_ids
                if work_item_id in open_work_ids
            )
        for work_item_id, hypothesis_id in dict.fromkeys(pairs):
            frontier_id = _frontier_id(
                link.candidate_article_id,
                work_item_id,
                hypothesis_id,
            )
            record = frontier_records.setdefault(
                frontier_id,
                {
                    "article_id": link.candidate_article_id,
                    "work_item_id": work_item_id,
                    "hypothesis_id": hypothesis_id,
                    "links": [],
                },
            )
            if link not in record["links"]:
                record["links"].append(link)

    for article_id, work_item_id, hypothesis_id in re_adoption_keys:
        if work_item_id not in open_work_ids:
            continue
        frontier_id = _frontier_id(article_id, work_item_id, hypothesis_id)
        record = frontier_records.setdefault(
            frontier_id,
            {
                "article_id": article_id,
                "work_item_id": work_item_id,
                "hypothesis_id": hypothesis_id,
                "links": [],
            },
        )
        for link in catalog.links:
            if link.candidate_article_id == article_id and link not in record["links"]:
                record["links"].append(link)

    pending: list[GraphReviewCandidate] = []
    # Stable hash ID is identity only. Paging by that hash would make an
    # unrelated digest determine which discovered candidate the Solver sees.
    # Preserve the Tool/catalog discovery order without adding semantic ranking.
    for frontier_id, record in frontier_records.items():
        article = articles_by_id.get(record["article_id"])
        if article is None:
            continue
        prior = latest_decision_by_frontier.get(frontier_id)
        current_link_ids = {item.link_id for item in record["links"]}
        reviewed_link_ids = reviewed_link_ids_by_frontier.get(frontier_id, set())
        if prior is None:
            trigger: Literal["new_frontier", "re_adopted", "new_link"] = (
                "re_adopted"
                if (
                    record["article_id"],
                    record["work_item_id"],
                    record["hypothesis_id"],
                )
                in re_adoption_keys
                else "new_frontier"
            )
        elif current_link_ids - reviewed_link_ids:
            trigger = "new_link"
        else:
            continue
        pending.append(
            GraphReviewCandidate(
                frontier_item_id=frontier_id,
                article_id=article.article_id,
                document_id=article.document_id,
                title=article.title,
                heading=article.heading,
                work_item_id=record["work_item_id"],
                hypothesis_id=record["hypothesis_id"],
                review_trigger=trigger,
                prior_review_status=(
                    _frontier_status(prior.action) if prior is not None else None
                ),
                content_status=article.content_status,
                links=tuple(record["links"]),
            )
        )

    batch_candidates = tuple(pending[:max_candidates])
    ledger = tuple(
        GraphReviewLedgerItem(
            frontier_item_id=frontier_id,
            article_id=decision.article_id,
            title=(
                articles_by_id[decision.article_id].title
                if decision.article_id in articles_by_id
                else None
            ),
            heading=(
                articles_by_id[decision.article_id].heading
                if decision.article_id in articles_by_id
                else None
            ),
            work_item_id=decision.work_item_id,
            hypothesis_id=decision.hypothesis_id,
            review_status=_frontier_status(decision.action),
            reason=_short_text(decision.reason, 240),
            content_status=(
                articles_by_id[decision.article_id].content_status
                if decision.article_id in articles_by_id
                else "not_requested"
            ),
            last_reviewed_cycle=latest_cycle_by_frontier.get(frontier_id),
            deferred_resolution_action=(
                deferred_resolution_by_frontier[frontier_id].action
                if frontier_id in deferred_resolution_by_frontier
                else None
            ),
            deferred_resolution_reason=(
                _short_text(
                    deferred_resolution_by_frontier[frontier_id].reason,
                    240,
                )
                if frontier_id in deferred_resolution_by_frontier
                else None
            ),
        )
        for frontier_id, decision in sorted(latest_decision_by_frontier.items())
    )
    return (
        GraphReviewBatch(
            candidates=batch_candidates,
            remaining_unreviewed_count=max(0, len(pending) - len(batch_candidates)),
        ),
        ledger,
    )


def _frontier_status(action: str) -> Literal[
    "selected", "relevant_deferred", "rejected"
]:
    return {
        "select": "selected",
        "defer": "relevant_deferred",
        "reject": "rejected",
    }[action]


def _frontier_id(
    article_id: str,
    work_item_id: str,
    hypothesis_id: str | None,
) -> str:
    return _stable_id(
        "graph-frontier",
        article_id,
        work_item_id,
        hypothesis_id or "no-hypothesis",
    )


def _stable_id(prefix: str, *parts: str) -> str:
    payload = "\x1f".join(parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def _short_text(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return f"{value[: max_chars - 1]}…"


def _fetched_article_ids_for_cycle(
    state: CaseState,
    cycle_no: int | None,
) -> tuple[str, ...]:
    requests_by_id = {item.request_id: item for item in state.tool_requests}
    article_ids: list[str] = []
    for result in state.tool_results:
        if (
            (cycle_no is not None and result.cycle_no != cycle_no)
            or result.status != "succeeded"
        ):
            continue
        request = requests_by_id.get(result.request_id)
        if request is None or request.tool_name != "fetch_articles":
            continue
        raw_ids = request.arguments.get("article_ids")
        if not isinstance(raw_ids, (list, tuple)):
            continue
        for raw_id in raw_ids:
            article_id = _nonempty_string(raw_id)
            if article_id is not None and article_id not in article_ids:
                article_ids.append(article_id)
    return tuple(article_ids)


def _merge_graph_article(
    articles: dict[str, dict[str, Any]],
    *,
    article_id: str,
    document_id: str | None,
    title: str | None,
    heading: str | None,
    content_status: GraphCandidateContentStatus,
) -> None:
    item = articles.setdefault(
        article_id,
        {
            "document_id": None,
            "title": None,
            "heading": None,
            "content_status": "not_requested",
        },
    )
    for key, value in (
        ("document_id", document_id),
        ("title", title),
        ("heading", heading),
    ):
        if item[key] is None and value is not None:
            item[key] = value
    if content_status != "not_requested":
        item["content_status"] = content_status


def _extend_unique(target: list[str], values: tuple[str, ...] | list[str]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def _solver_tool_result(
    result: ToolResult,
    *,
    request: ToolRequest | None,
    graph_navigation_ids: frozenset[str],
) -> SolverToolResult:
    graph_projected = (
        request is not None and request.tool_name == "legal_graph_neighbors"
    ) or bool(graph_navigation_ids.intersection(result.evidence_ids))
    return SolverToolResult(
        request_id=result.request_id,
        status=result.status,
        evidence_ids=tuple(
            evidence_id
            for evidence_id in result.evidence_ids
            if evidence_id not in graph_navigation_ids
        ),
        evidence_count=len(result.evidence_ids),
        graph_projection_updated=graph_projected,
        error_code=result.error_code,
        elapsed_ms=result.elapsed_ms,
        cycle_no=result.cycle_no,
    )


def _graph_navigation_payload(evidence: Evidence) -> dict[str, Any]:
    try:
        payload = json.loads(evidence.content)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _article_content_statuses(
    state: CaseState,
) -> dict[str, GraphCandidateContentStatus]:
    results_by_request_id = {
        item.request_id: item for item in state.tool_results
    }
    statuses: dict[str, GraphCandidateContentStatus] = {}
    for request in state.tool_requests:
        if request.tool_name != "fetch_articles":
            continue
        article_ids = request.arguments.get("article_ids")
        if not isinstance(article_ids, (list, tuple)):
            continue
        result = results_by_request_id.get(request.request_id)
        status: GraphCandidateContentStatus = (
            "pending" if result is None else result.status
        )
        for article_id in article_ids:
            normalized = _nonempty_string(article_id)
            if normalized is not None:
                statuses[normalized] = status

    for evidence in state.evidence:
        if evidence.metadata.get("citationEligible") is False:
            continue
        article_id = _nonempty_string(evidence.metadata.get("articleId"))
        if article_id is not None:
            statuses[article_id] = "succeeded"
    return statuses


def _nonempty_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _evidence_article_ids(evidence: Evidence) -> tuple[str, ...]:
    metadata = evidence.metadata
    return tuple(
        dict.fromkeys(
            value
            for key in ("articleId", "fromArticleId", "toArticleId")
            if isinstance((value := metadata.get(key)), str) and value
        )
    )


def _round_robin_result_evidence_ids(
    results: tuple[ToolResult, ...],
) -> tuple[str, ...]:
    """並列Tool結果を呼出順で偏らせず、各結果から機械的に交互採用する。"""
    ordered: list[str] = []
    seen: set[str] = set()
    for index in range(max((len(item.evidence_ids) for item in results), default=0)):
        for result in results:
            if index >= len(result.evidence_ids):
                continue
            evidence_id = result.evidence_ids[index]
            if evidence_id not in seen:
                seen.add(evidence_id)
                ordered.append(evidence_id)
    return tuple(ordered)
