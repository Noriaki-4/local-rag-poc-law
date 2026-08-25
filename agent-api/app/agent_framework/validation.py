"""LLMの意味判断を書き換えず、参照と構造だけを検証する。"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from typing import TypeVar

from pydantic import ValidationError

from .contracts import CaseUpdate, SolverDecision, WorkItemImpactDecision
from .profiles import AgentLimits
from .state import (
    CaseState,
    FrameworkModel,
    Hypothesis,
    ToolRequest,
    WorkItem,
    utc_now,
)

ModelT = TypeVar("ModelT", bound=FrameworkModel)


class ContractViolation(ValueError):
    pass


class ActionRejected(ContractViolation):
    """意味判断を伴わず、実行前に棄却できるTool行動。"""

    def __init__(
        self,
        message: str,
        *,
        rejected_requests: tuple[ToolRequest, ...],
    ) -> None:
        super().__init__(message)
        self.code = "already_completed"
        self.rejected_requests = rejected_requests


def apply_solver_decision(
    state: CaseState,
    decision: SolverDecision,
    *,
    limits: AgentLimits,
    known_tool_names: Collection[str],
    material_evidence_ids: Collection[str],
    finalize_only: bool,
    fetchable_article_ids: Collection[str] | None = None,
    required_dependency_kind: str | None = None,
    required_dependency_work_item_ids: Collection[str] | None = None,
    require_dependency_decisions: bool = False,
    required_review_finding_ids: Collection[str] = (),
    tool_list_argument_limits: Mapping[tuple[str, str], int] | None = None,
    required_graph_review_request_ids: Collection[str] = (),
    required_search_review_request_ids: Collection[str] = (),
    search_candidate_article_ids: Collection[str] = (),
    graph_candidate_article_ids: Collection[str] = (),
    graph_known_article_ids: Collection[str] | None = None,
    graph_review_fetch_tool_name: str | None = None,
    graph_review_frontiers: Mapping[
        str, tuple[str, str, str | None]
    ] | None = None,
    graph_review_link_ids: Collection[str] = (),
    graph_selectable_frontiers: Mapping[
        str, tuple[str, str, str | None]
    ] | None = None,
    deferred_frontiers: Mapping[
        str, tuple[str, str, str | None]
    ] | None = None,
    unreviewed_graph_candidate_count: int = 0,
    remaining_fetch_capacity: int | None = None,
    cycle_close_required: bool = False,
    can_start_next_cycle: bool = True,
    allow_dependency_action_without_tool: bool = False,
) -> CaseState:
    if state.run_status != "running":
        raise ContractViolation("solver can update only a running case")
    if decision.start_next_cycle and required_graph_review_request_ids:
        raise ContractViolation(
            "Graph candidate review must continue the current research cycle"
        )
    if (
        decision.start_next_cycle
        and state.research_cycle_count >= limits.max_research_cycles
    ):
        raise ContractViolation("cannot start a research cycle beyond the profile limit")
    if decision.start_next_cycle and not can_start_next_cycle:
        raise ContractViolation("remaining time is insufficient to start another Cycle")
    if finalize_only and decision.next != "finalize":
        raise ContractViolation("this solver call must finalize")
    if (
        cycle_close_required
        and decision.next == "continue"
        and not decision.start_next_cycle
        and decision.tool_requests
    ):
        raise ContractViolation(
            "Cycle boundary requires finalize or start_next_cycle before new Tools"
        )
    if len(decision.tool_requests) > limits.max_tool_requests_per_step:
        raise ContractViolation("tool request count exceeds the step limit")
    if len(decision.retain_evidence_ids) > limits.max_retained_evidence:
        raise ContractViolation("retained evidence count exceeds the profile limit")

    _raise_preflight_contract_violations(
        state,
        decision,
        fetchable_article_ids=fetchable_article_ids,
        graph_known_article_ids=graph_known_article_ids,
        article_fetch_tool_name=graph_review_fetch_tool_name,
        unreviewed_graph_candidate_count=unreviewed_graph_candidate_count,
    )

    _require_unique_state_ids(state)
    non_work_item_requirements = state.non_work_item_requirements
    requested_non_work_item_requirements = (
        decision.update.set_non_work_item_requirements
    )
    if requested_non_work_item_requirements is not None:
        if state.work_items or state.hypotheses or state.non_work_item_requirements:
            raise ContractViolation(
                "non-WorkItem requirements may be set only during initial decomposition"
            )
        if any(not item for item in requested_non_work_item_requirements):
            raise ContractViolation("non-WorkItem requirements must not be empty")
        if len(requested_non_work_item_requirements) != len(
            set(requested_non_work_item_requirements)
        ):
            raise ContractViolation("non-WorkItem requirements must be unique")
        non_work_item_requirements = requested_non_work_item_requirements
    work_items = {item.work_item_id: item for item in state.work_items}
    hypotheses = {item.hypothesis_id: item for item in state.hypotheses}
    evidence_by_id = {item.evidence_id: item for item in state.evidence}
    evidence_ids = set(evidence_by_id)
    citable_evidence_ids = {
        evidence_id
        for evidence_id, evidence in evidence_by_id.items()
        if evidence.metadata.get("citationEligible") is not False
    }
    material_ids = set(material_evidence_ids)
    unknown_material_ids = material_ids - evidence_ids
    if unknown_material_ids:
        raise ContractViolation(
            f"material evidence is not stored: {sorted(unknown_material_ids)}"
        )
    review_resolution_ids = [
        item.finding_id for item in decision.review_finding_resolutions
    ]
    if len(review_resolution_ids) != len(set(review_resolution_ids)):
        raise ContractViolation("review finding resolutions must be unique")
    expected_review_finding_ids = set(required_review_finding_ids)
    if set(review_resolution_ids) != expected_review_finding_ids:
        missing = sorted(expected_review_finding_ids - set(review_resolution_ids))
        extra = sorted(set(review_resolution_ids) - expected_review_finding_ids)
        raise ContractViolation(
            "review finding resolutions do not match pending findings; "
            f"missing={missing}, extra={extra}"
        )
    for resolution in decision.review_finding_resolutions:
        unknown_resolution_evidence = (
            set(resolution.basis_evidence_ids) - material_ids
        )
        if unknown_resolution_evidence:
            raise ContractViolation(
                "review finding resolution references evidence not shown in full: "
                f"{sorted(unknown_resolution_evidence)}"
            )
    changed_hypothesis_ids: set[str] = set()
    added_work_item_ids = {item.work_item_id for item in decision.update.add_work_items}
    dependency_scope_ids = (
        set(required_dependency_work_item_ids)
        if required_dependency_work_item_ids is not None
        else {item.work_item_id for item in state.work_items if item.state == "open"}
    )

    _reject_duplicate_delta_ids(
        (item.work_item_id for item in decision.update.add_work_items),
        "new work item",
    )
    _reject_duplicate_delta_ids(
        (item.work_item_id for item in decision.update.update_work_items),
        "work item update",
    )
    _reject_duplicate_delta_ids(
        (item.hypothesis_id for item in decision.update.add_hypotheses),
        "new hypothesis",
    )
    _reject_duplicate_delta_ids(
        (item.hypothesis_id for item in decision.update.update_hypotheses),
        "hypothesis update",
    )
    _reject_duplicate_delta_ids(
        (item.work_item_id for item in decision.update.impact_decisions),
        "impact decision",
    )

    for new_work_item in decision.update.add_work_items:
        if new_work_item.work_item_id in work_items:
            raise ContractViolation(
                f"duplicate work item ID: {new_work_item.work_item_id}"
            )
        work_items[new_work_item.work_item_id] = new_work_item
    for new_hypothesis in decision.update.add_hypotheses:
        if new_hypothesis.hypothesis_id in hypotheses:
            raise ContractViolation(
                f"duplicate hypothesis ID: {new_hypothesis.hypothesis_id}"
            )
        hypotheses[new_hypothesis.hypothesis_id] = new_hypothesis
        changed_hypothesis_ids.add(new_hypothesis.hypothesis_id)

    newly_contradicted: set[str] = set()
    affected_source_items = dict(work_items)

    for work_update in decision.update.update_work_items:
        current_work_item = work_items.get(work_update.work_item_id)
        if current_work_item is None:
            raise ContractViolation(
                f"unknown work item update: {work_update.work_item_id}"
            )
        work_items[work_update.work_item_id] = _validated_copy(
            current_work_item,
            state=work_update.state,
            resolution=work_update.resolution,
            basis_hypothesis_ids=work_update.basis_hypothesis_ids,
        )

    for hypothesis_update in decision.update.update_hypotheses:
        current_hypothesis = hypotheses.get(hypothesis_update.hypothesis_id)
        if current_hypothesis is None:
            raise ContractViolation(
                f"unknown hypothesis update: {hypothesis_update.hypothesis_id}"
            )
        hypotheses[hypothesis_update.hypothesis_id] = _validated_copy(
            current_hypothesis,
            judgment=hypothesis_update.judgment,
            evidence_ids=hypothesis_update.evidence_ids,
            gaps=hypothesis_update.gaps,
        )
        changed_hypothesis_ids.add(hypothesis_update.hypothesis_id)
        if (
            current_hypothesis.judgment != "contradicted"
            and hypothesis_update.judgment == "contradicted"
        ):
            newly_contradicted.add(hypothesis_update.hypothesis_id)

    for new_hypothesis in decision.update.add_hypotheses:
        if new_hypothesis.judgment == "contradicted":
            newly_contradicted.add(new_hypothesis.hypothesis_id)

    affected_ids = {
        item.work_item_id
        for item in affected_source_items.values()
        if item.state == "open"
        and newly_contradicted.intersection(item.basis_hypothesis_ids)
    }
    impact_by_id = {
        item.work_item_id: item for item in decision.update.impact_decisions
    }
    if set(impact_by_id) != affected_ids:
        missing = sorted(affected_ids - set(impact_by_id))
        extra = sorted(set(impact_by_id) - affected_ids)
        raise ContractViolation(
            f"impact decisions do not match affected work items; "
            f"missing={missing}, extra={extra}"
        )

    for impact in decision.update.impact_decisions:
        _apply_impact(
            impact,
            work_items,
            hypotheses,
            newly_contradicted=newly_contradicted,
            added_work_item_ids=added_work_item_ids,
        )

    _validate_work_tree(work_items, hypotheses)
    _validate_hypotheses(
        hypotheses,
        work_items,
        known_evidence_ids=evidence_ids,
        material_evidence_ids=material_ids,
        citable_evidence_ids=citable_evidence_ids,
        changed_hypothesis_ids=changed_hypothesis_ids,
    )

    retained_ids = tuple(dict.fromkeys(decision.retain_evidence_ids))
    if len(retained_ids) != len(decision.retain_evidence_ids):
        raise ContractViolation("retained evidence IDs must be unique")
    unknown_retained = set(retained_ids) - evidence_ids
    if unknown_retained:
        raise ContractViolation(
            f"unknown retained evidence IDs: {sorted(unknown_retained)}"
        )

    request_ids = {item.request_id for item in state.tool_requests}
    completed_search_scopes = {
        _tool_request_scope(request)
        for request in state.tool_requests
        if request.tool_name == "legal_search"
        and any(
            result.request_id == request.request_id
            and result.status == "succeeded"
            for result in state.tool_results
        )
    }
    completed_graph_scopes = {
        _tool_request_scope(request)
        for request in state.tool_requests
        if request.tool_name == "legal_graph_neighbors"
        and any(
            result.request_id == request.request_id
            and result.status == "succeeded"
            for result in state.tool_results
        )
    }
    new_request_ids: set[str] = set()
    new_requests_by_id = {
        item.request_id: item for item in decision.tool_requests
    }
    duplicate_search_scopes = [
        request
        for request in decision.tool_requests
        if request.tool_name == "legal_search"
        and _tool_request_scope(request) in completed_search_scopes
    ]
    if duplicate_search_scopes:
        duplicate_details = [
            {
                "work_item_id": request.work_item_id,
                "hypothesis_ids": list(request.hypothesis_ids),
                "arguments": request.arguments,
            }
            for request in duplicate_search_scopes
        ]
        raise ActionRejected(
            "successful legal_search scope was already completed: "
            + json.dumps(
                duplicate_details,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            rejected_requests=tuple(duplicate_search_scopes),
        )
    duplicate_graph_scopes = [
        request
        for request in decision.tool_requests
        if request.tool_name == "legal_graph_neighbors"
        and _tool_request_scope(request) in completed_graph_scopes
    ]
    if duplicate_graph_scopes:
        duplicate_details = [
            {
                "work_item_id": request.work_item_id,
                "hypothesis_ids": list(request.hypothesis_ids),
                "arguments": request.arguments,
            }
            for request in duplicate_graph_scopes
        ]
        raise ActionRejected(
            "successful legal_graph_neighbors scope was already completed: "
            + json.dumps(
                duplicate_details,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            rejected_requests=tuple(duplicate_graph_scopes),
        )
    execution_scopes = [
        (
            request.tool_name,
            json.dumps(
                request.arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        for request in decision.tool_requests
        if request.tool_name == "legal_graph_neighbors"
    ]
    if len(execution_scopes) != len(set(execution_scopes)):
        raise ContractViolation(
            "identical legal_graph_neighbors arguments must be consolidated "
            "into one request"
        )
    for request in decision.tool_requests:
        if request.request_id in request_ids or request.request_id in new_request_ids:
            raise ContractViolation(f"duplicate tool request ID: {request.request_id}")
        new_request_ids.add(request.request_id)
        if request.tool_name not in known_tool_names:
            raise ContractViolation(f"unknown tool: {request.tool_name}")
        for (tool_name, argument_name), max_items in (
            tool_list_argument_limits or {}
        ).items():
            if request.tool_name != tool_name or argument_name not in request.arguments:
                continue
            value = request.arguments[argument_name]
            if not isinstance(value, list):
                raise ContractViolation(
                    f"{tool_name}.{argument_name} must be a list"
                )
            if len(value) > max_items:
                raise ContractViolation(
                    f"{tool_name}.{argument_name} exceeds the profile limit "
                    f"of {max_items} items"
                )
        target = work_items.get(request.work_item_id)
        if target is None or target.state != "open":
            raise ContractViolation(
                f"tool request requires an open work item: {request.work_item_id}"
            )
        for hypothesis_id in request.hypothesis_ids:
            if hypothesis_id not in hypotheses:
                raise ContractViolation(
                    f"tool request references unknown hypothesis: {hypothesis_id}"
                )
        requested_article_ids = request.arguments.get("article_ids")
        if requested_article_ids is not None:
            if not isinstance(requested_article_ids, list) or any(
                not isinstance(item, str) for item in requested_article_ids
            ):
                raise ContractViolation("tool article_ids must be a string list")
            allowed_article_ids = _allowed_tool_article_ids(
                state,
                request.tool_name,
                fetchable_article_ids=fetchable_article_ids,
                graph_known_article_ids=graph_known_article_ids,
                article_fetch_tool_name=graph_review_fetch_tool_name,
            )
            if (
                allowed_article_ids is not None
                and (unknown_article_ids := set(requested_article_ids) - allowed_article_ids)
            ):
                raise ContractViolation(
                    "tool request references unknown Article IDs: "
                    f"{sorted(unknown_article_ids)}"
                )

    effective_fetch_capacity = (
        limits.max_fetched_resources_per_cycle
        if decision.start_next_cycle
        else (0 if cycle_close_required else remaining_fetch_capacity)
    )
    if graph_review_fetch_tool_name is not None and effective_fetch_capacity is not None:
        requested_articles = {
            article_id
            for request in decision.tool_requests
            if request.tool_name == graph_review_fetch_tool_name
            for article_id in request.arguments.get("article_ids", ())
            if isinstance(article_id, str)
        }
        if len(requested_articles) > effective_fetch_capacity:
            raise ContractViolation(
                "Article body fetch exceeds the remaining Cycle capacity of "
                f"{effective_fetch_capacity} items"
            )

    if graph_review_fetch_tool_name is not None:
        article_fetch_requests = tuple(
            request
            for request in decision.tool_requests
            if request.tool_name == graph_review_fetch_tool_name
        )
        if len(article_fetch_requests) > 1:
            raise ContractViolation(
                "Article body fetches in one SolverDecision must be consolidated "
                "into exactly one request"
            )
        if article_fetch_requests:
            article_ids = article_fetch_requests[0].arguments.get("article_ids")
            if isinstance(article_ids, list) and len(article_ids) != len(
                set(article_ids)
            ):
                raise ContractViolation(
                    "Article body fetch request contains duplicate Article IDs"
                )

    expected_deferred = dict(deferred_frontiers or {})
    deferred_resolutions = decision.deferred_frontier_resolutions
    resolution_ids = [item.frontier_item_id for item in deferred_resolutions]
    if len(resolution_ids) != len(set(resolution_ids)):
        raise ContractViolation("deferred Frontier resolutions must be unique")
    cycle_boundary = decision.next == "finalize" or decision.start_next_cycle
    if deferred_resolutions and not cycle_boundary:
        raise ContractViolation(
            "deferred Frontier resolutions are allowed only at a Cycle boundary"
        )
    if cycle_boundary and set(resolution_ids) != set(expected_deferred):
        missing = sorted(set(expected_deferred) - set(resolution_ids))
        extra = sorted(set(resolution_ids) - set(expected_deferred))
        raise ContractViolation(
            "Cycle boundary must resolve every active deferred Frontier; "
            f"missing={missing}, extra={extra}"
        )
    fetched_article_ids = {
        article_id
        for request in decision.tool_requests
        if request.tool_name == graph_review_fetch_tool_name
        for article_id in request.arguments.get("article_ids", ())
        if isinstance(article_id, str)
    }
    next_cycle_article_ids = {
        item.article_id
        for item in deferred_resolutions
        if item.action == "fetch_next_cycle"
    }
    if len(next_cycle_article_ids) > limits.max_fetched_resources_per_cycle:
        raise ContractViolation(
            "fetch_next_cycle Article count exceeds the next Cycle capacity"
        )
    if next_cycle_article_ids and fetched_article_ids and (
        fetched_article_ids != next_cycle_article_ids
    ):
        raise ContractViolation(
            "Cycle-start Article fetch conflicts with fetch_next_cycle resolutions"
        )
    if (
        next_cycle_article_ids
        and not fetched_article_ids
        and len(decision.tool_requests) + 1 > limits.max_tool_requests_per_step
    ):
        raise ContractViolation(
            "projected Cycle-start Article fetch exceeds the step Tool limit"
        )
    for resolution in deferred_resolutions:
        expected = expected_deferred.get(resolution.frontier_item_id)
        actual = (
            resolution.article_id,
            resolution.work_item_id,
            resolution.hypothesis_id,
        )
        if actual != expected:
            raise ContractViolation(
                "deferred Frontier resolution references do not match the ledger"
            )
        if resolution.action == "fetch_next_cycle":
            if not decision.start_next_cycle:
                raise ContractViolation(
                    "fetch_next_cycle requires start_next_cycle=true"
                )
            target = work_items.get(resolution.work_item_id)
            if target is None or target.state != "open":
                raise ContractViolation(
                    "fetch_next_cycle requires an open WorkItem"
                )
        elif resolution.action == "carry_forward":
            if not decision.start_next_cycle:
                raise ContractViolation(
                    "carry_forward requires start_next_cycle=true"
                )
            target = work_items.get(resolution.work_item_id)
            if target is None or target.state != "open":
                raise ContractViolation(
                    "carry_forward requires an open WorkItem"
                )
        elif resolution.action == "unresolved_at_limit":
            if not (finalize_only or not can_start_next_cycle):
                raise ContractViolation(
                    "unresolved_at_limit is allowed only when another Cycle cannot "
                    "start"
                )
            if decision.next != "finalize" or not decision.answer.limitations:
                raise ContractViolation(
                    "unresolved_at_limit requires finalize with limitations"
                )

    unreviewed_resolution = decision.unreviewed_graph_resolution
    if unreviewed_graph_candidate_count and cycle_boundary:
        if unreviewed_resolution is None:
            raise ContractViolation(
                "Cycle boundary must state how the unreviewed Graph candidate pool "
                "will be handled"
            )
    elif unreviewed_resolution is not None:
        if not unreviewed_graph_candidate_count:
            raise ContractViolation(
                "unreviewed Graph resolution requires a non-empty candidate pool"
            )
        if not cycle_boundary:
            raise ContractViolation(
                "unreviewed Graph resolution is allowed only at a Cycle boundary"
            )
    if unreviewed_resolution is not None:
        if unreviewed_resolution.action == "review_next_cycle":
            if decision.next != "continue" or not decision.start_next_cycle:
                raise ContractViolation(
                    "review_next_cycle requires continue with start_next_cycle=true"
                )
        elif unreviewed_resolution.action == "no_longer_needed":
            if decision.next != "finalize":
                raise ContractViolation(
                    "no_longer_needed for an unreviewed Graph pool requires finalize"
                )
        elif unreviewed_resolution.action == "unresolved_at_limit":
            if not (finalize_only or not can_start_next_cycle):
                raise ContractViolation(
                    "unreviewed Graph candidates may remain unresolved only when "
                    "another Cycle cannot start"
                )
            if decision.next != "finalize" or not decision.answer.limitations:
                raise ContractViolation(
                    "unresolved unreviewed Graph candidates require finalize with "
                    "limitations"
                )

    required_graph_ids = tuple(dict.fromkeys(required_graph_review_request_ids))
    graph_review = decision.graph_candidate_review
    if required_graph_ids:
        if deferred_resolutions:
            raise ContractViolation(
                "Graph Review mode cannot resolve deferred Frontiers"
            )
        if unreviewed_resolution is not None:
            raise ContractViolation(
                "Graph Review mode cannot resolve the unreviewed Graph pool"
            )
        if decision.frontier_re_adoptions:
            raise ContractViolation(
                "Graph Review mode cannot re-adopt another Frontier"
            )
        if decision.update != CaseUpdate():
            raise ContractViolation(
                "Graph Review mode cannot update WorkItems or Hypotheses"
            )
        if decision.dependency_decisions:
            raise ContractViolation(
                "Graph Review mode cannot update dependency decisions"
            )
        if graph_review is None:
            raise ContractViolation(
                "new Graph candidates require a graph_candidate_review"
            )
        if set(graph_review.graph_request_ids) != set(required_graph_ids):
            raise ContractViolation(
                "graph review request IDs do not match the newly projected Graph "
                f"results: expected={sorted(required_graph_ids)}"
            )
        expected_frontiers = dict(graph_review_frontiers or {})
        decisions_by_frontier = {
            item.frontier_item_id: item
            for item in graph_review.frontier_decisions
        }
        missing_frontiers = set(expected_frontiers) - set(decisions_by_frontier)
        if missing_frontiers:
            raise ContractViolation(
                "graph review must decide every batch Frontier: "
                f"missing={sorted(missing_frontiers)}"
            )
        selectable_frontiers = dict(graph_selectable_frontiers or {})
        allowed_extra_frontiers = set(selectable_frontiers)
        unknown_frontiers = (
            set(decisions_by_frontier)
            - set(expected_frontiers)
            - allowed_extra_frontiers
        )
        if unknown_frontiers:
            raise ContractViolation(
                "graph review references unknown Frontier IDs: "
                f"{sorted(unknown_frontiers)}"
            )
        all_frontier_refs = {**selectable_frontiers, **expected_frontiers}
        for item in graph_review.frontier_decisions:
            expected = all_frontier_refs.get(item.frontier_item_id)
            if expected is None:
                raise ContractViolation("graph review Frontier is not selectable")
            if (
                item.frontier_item_id not in expected_frontiers
                and item.action != "select"
            ):
                raise ContractViolation("ledger-only Frontier may only be selected")
            actual = (item.article_id, item.work_item_id, item.hypothesis_id)
            if actual != expected:
                raise ContractViolation(
                    "graph review Frontier references do not match the batch"
                )
        expected_link_ids = set(graph_review_link_ids)
        if set(graph_review.reviewed_link_ids) != expected_link_ids:
            raise ContractViolation(
                "graph review Link IDs do not match the current batch"
            )
        selected_ids = tuple(graph_review.selected_article_ids)
        fetchable_selected_ids = tuple(
            dict.fromkeys(
                item.article_id
                for item in graph_review.frontier_decisions
                if item.action == "select"
                and item.frontier_item_id in selectable_frontiers
            )
        )
        max_selected = min(
            limits.max_selected_frontier_per_step,
            effective_fetch_capacity
            if effective_fetch_capacity is not None
            else limits.max_selected_frontier_per_step,
        )
        if len(fetchable_selected_ids) > max_selected:
            raise ContractViolation(
                "graph review selected Article count exceeds the remaining limit "
                f"of {max_selected} items"
            )
        has_fetchable_deferred = any(
            item.action == "defer"
            and item.frontier_item_id in selectable_frontiers
            for item in graph_review.frontier_decisions
        )
        if max_selected > 0 and not selected_ids and has_fetchable_deferred:
            raise ContractViolation(
                "graph review deferred every relevant fetchable Article despite "
                f"a remaining selection limit of {max_selected} items"
            )
        if decision.tool_requests:
            raise ContractViolation(
                "Graph candidate review returns selections only; AgentLoop executes "
                "the selected IDs deterministically"
            )
        if selected_ids:
            if decision.next != "continue":
                raise ContractViolation(
                    "graph review with selected Articles must continue"
                )
    elif graph_review is not None:
        raise ContractViolation(
            "graph_candidate_review is allowed only for newly projected Graph results"
        )

    required_search_ids = tuple(
        dict.fromkeys(required_search_review_request_ids)
    )
    search_review = decision.search_candidate_review
    if required_search_ids:
        if decision.update != CaseUpdate():
            raise ContractViolation(
                "Search Review mode cannot update WorkItems or Hypotheses"
            )
        if decision.dependency_decisions:
            raise ContractViolation(
                "Search Review mode cannot update dependency decisions"
            )
        if graph_review is not None:
            raise ContractViolation(
                "Search Review mode cannot review Graph candidates"
            )
        if search_review is None:
            raise ContractViolation(
                "new OpenSearch candidates require a search_candidate_review"
            )
        if set(search_review.search_request_ids) != set(required_search_ids):
            raise ContractViolation(
                "search review request IDs do not match the latest OpenSearch results"
            )
        expected_candidates = set(search_candidate_article_ids)
        selected_ids = tuple(search_review.selected_article_ids)
        decided_ids = {
            *selected_ids,
            *search_review.deferred_article_ids,
        }
        if decided_ids != expected_candidates:
            raise ContractViolation(
                "search review must decide every candidate Article"
            )
        if len(selected_ids) + len(search_review.deferred_article_ids) != len(
            expected_candidates
        ):
            raise ContractViolation(
                "search review candidates must be selected or deferred exactly once"
            )
        for selection in search_review.selections:
            if not selection.matched_hypothesis_ids:
                raise ContractViolation(
                    "selected search candidate requires matched Hypothesis IDs"
                )
            unknown_matched_ids = set(
                selection.matched_hypothesis_ids
            ) - set(hypotheses)
            if unknown_matched_ids:
                raise ContractViolation(
                    "search candidate references unknown Hypothesis IDs: "
                    f"{sorted(unknown_matched_ids)}"
                )
        max_selected = (
            effective_fetch_capacity
            if effective_fetch_capacity is not None
            else limits.max_selected_frontier_per_step
        )
        if len(selected_ids) > max_selected:
            raise ContractViolation(
                "search review selected Article count exceeds the remaining limit "
                f"of {max_selected} items"
            )
        if decision.tool_requests:
            raise ContractViolation(
                "Search candidate review returns selections only; AgentLoop executes "
                "the selected IDs deterministically"
            )
        if decision.next != "continue":
            raise ContractViolation("Search Review mode must continue")
    elif search_review is not None:
        raise ContractViolation(
            "search_candidate_review is allowed only for newly projected search results"
        )

    graph_known_articles = set(graph_known_article_ids or ())
    open_work_ids = {
        item.work_item_id for item in work_items.values() if item.state == "open"
    }
    re_adoption_keys = [
        (item.article_id, item.work_item_id, item.hypothesis_id)
        for item in decision.frontier_re_adoptions
    ]
    if len(re_adoption_keys) != len(set(re_adoption_keys)):
        raise ContractViolation("frontier re-adoptions must be unique")
    reviewed_frontier_keys = {
        (item.article_id, item.work_item_id, item.hypothesis_id)
        for review_item in state.graph_candidate_reviews
        for item in review_item.frontier_decisions
    }
    for re_adoption in decision.frontier_re_adoptions:
        if re_adoption.article_id not in graph_known_articles:
            raise ContractViolation(
                "frontier re-adoption references an unknown Graph Article"
            )
        if re_adoption.work_item_id not in open_work_ids:
            raise ContractViolation(
                "frontier re-adoption requires an open WorkItem"
            )
        hypothesis = hypotheses.get(re_adoption.hypothesis_id)
        if hypothesis is None or hypothesis.work_item_id != re_adoption.work_item_id:
            raise ContractViolation(
                "frontier re-adoption Hypothesis must belong to its WorkItem"
            )
        if (
            re_adoption.article_id,
            re_adoption.work_item_id,
            re_adoption.hypothesis_id,
        ) in reviewed_frontier_keys:
            raise ContractViolation(
                "frontier re-adoption must bind the Article to a new Hypothesis"
            )

    dependency_by_key = {
        (item.dependency_kind, item.work_item_id): item
        for item in state.dependency_decisions
    }
    new_dependency_keys = [
        (item.dependency_kind, item.work_item_id)
        for item in decision.dependency_decisions
    ]
    if len(new_dependency_keys) != len(set(new_dependency_keys)):
        raise ContractViolation("dependency decision keys must be unique")
    for dependency in decision.dependency_decisions:
        if dependency.work_item_id not in work_items:
            raise ContractViolation(
                f"dependency decision references unknown work item: "
                f"{dependency.work_item_id}"
            )
        if (
            not dependency.basis_evidence_ids
            and dependency.status != "needs_action"
        ):
            raise ContractViolation("dependency decision requires basis evidence")
        if len(dependency.basis_evidence_ids) != len(
            set(dependency.basis_evidence_ids)
        ):
            raise ContractViolation("dependency basis evidence IDs must be unique")
        unknown_dependency_evidence = (
            set(dependency.basis_evidence_ids) - material_ids
        )
        if unknown_dependency_evidence:
            raise ContractViolation(
                "dependency basis evidence was not shown in full: "
                f"{sorted(unknown_dependency_evidence)}"
            )
        if dependency.status == "needs_action":
            if (
                allow_dependency_action_without_tool
                and dependency.action_request_id is None
            ):
                pass
            elif decision.start_next_cycle:
                if dependency.action_request_id is not None:
                    raise ContractViolation(
                        "cycle-boundary dependency action cannot reference a "
                        "ToolRequest before the next cycle"
                    )
            elif dependency.action_request_id not in new_requests_by_id:
                raise ContractViolation(
                    "dependency action must reference a ToolRequest in the same decision"
                )
        elif dependency.action_request_id is not None:
            raise ContractViolation(
                "completed dependency decision cannot reference an action request"
            )
        if (
            dependency.status == "resolved"
            and dependency.dependency_kind == required_dependency_kind
        ):
            basis_article_ids = {
                str(evidence_by_id[evidence_id].metadata.get("articleId") or "")
                for evidence_id in dependency.basis_evidence_ids
            }
            basis_article_ids.discard("")
            if len(basis_article_ids) < 2:
                raise ContractViolation(
                    "resolved dependency for work item "
                    f"{dependency.work_item_id!r} requires full-text evidence from "
                    "at least two distinct Articles: the delegating source and the "
                    f"terminal target; actual Article IDs={sorted(basis_article_ids)}"
                )
        dependency_by_key[
            (dependency.dependency_kind, dependency.work_item_id)
        ] = dependency

    closed_dependency_work_items = sorted(
        {
            dependency.work_item_id
            for dependency in decision.dependency_decisions
            if dependency.status == "needs_action"
            and work_items[dependency.work_item_id].state != "open"
        }
    )
    if closed_dependency_work_items:
        raise ContractViolation(
            "needs_action dependency requires open WorkItem IDs: "
            f"{closed_dependency_work_items}"
        )

    if required_dependency_kind is not None and require_dependency_decisions:
        provided_scope_ids = {
            item.work_item_id
            for item in decision.dependency_decisions
            if item.dependency_kind == required_dependency_kind
        }
        if provided_scope_ids != dependency_scope_ids:
            missing = sorted(dependency_scope_ids - provided_scope_ids)
            extra = sorted(provided_scope_ids - dependency_scope_ids)
            raise ContractViolation(
                f"{required_dependency_kind} decisions do not match required work items; "
                f"missing={missing}, extra={extra}"
            )

    if decision.start_next_cycle:
        has_open_work = any(
            item.state == "open" for item in work_items.values()
        )
        has_deferred_followup = any(
            item.action in {"fetch_next_cycle", "carry_forward"}
            for item in deferred_resolutions
        )
        has_unreviewed_followup = (
            unreviewed_resolution is not None
            and unreviewed_resolution.action == "review_next_cycle"
        )
        has_dependency_followup = any(
            item.status == "needs_action"
            for item in dependency_by_key.values()
            if item.work_item_id in work_items
            and work_items[item.work_item_id].state == "open"
        )
        if not any(
            (
                has_open_work,
                has_deferred_followup,
                has_unreviewed_followup,
                has_dependency_followup,
            )
        ):
            raise ContractViolation(
                "start_next_cycle requires an unresolved WorkItem or an explicit "
                "Graph or dependency follow-up"
            )

    focus_ids = tuple(dict.fromkeys(decision.next_focus_work_item_ids))
    if len(focus_ids) != len(decision.next_focus_work_item_ids):
        raise ContractViolation("focus work item IDs must be unique")
    for work_item_id in focus_ids:
        focused_work_item = work_items.get(work_item_id)
        if focused_work_item is None or focused_work_item.state != "open":
            raise ContractViolation(
                f"focus must reference an open work item: {work_item_id}"
            )

    if decision.answer is not None:
        unknown_citations = set(decision.answer.citation_ids) - material_ids
        if unknown_citations:
            raise ContractViolation(
                f"answer cites evidence not shown in full: {sorted(unknown_citations)}"
            )
        navigation_citations = set(decision.answer.citation_ids) - citable_evidence_ids
        if navigation_citations:
            raise ContractViolation(
                f"answer cites navigation-only evidence: {sorted(navigation_citations)}"
            )
        resolved_basis_evidence_ids = {
            evidence_id
            for item in work_items.values()
            if item.state == "resolved"
            for hypothesis_id in item.basis_hypothesis_ids
            for evidence_id in hypotheses[hypothesis_id].evidence_ids
        }
        resolved_basis_evidence_ids.update(
            evidence_id
            for dependency in dependency_by_key.values()
            if work_items[dependency.work_item_id].state == "resolved"
            and dependency.status == "resolved"
            for evidence_id in dependency.basis_evidence_ids
        )
        unsupported_citations = (
            set(decision.answer.citation_ids) - resolved_basis_evidence_ids
        )
        if unsupported_citations:
            raise ContractViolation(
                "answer citations require resolved WorkItem basis: "
                f"{sorted(unsupported_citations)}"
            )
        citation_article_ids = {
            str(evidence_by_id[evidence_id].metadata.get("articleId") or "")
            for evidence_id in decision.answer.citation_ids
        }
        citation_article_ids.discard("")
        for dependency in decision.dependency_decisions:
            work_item = work_items[dependency.work_item_id]
            if dependency.status != "resolved" or work_item.state != "resolved":
                continue
            dependency_article_ids = {
                str(evidence_by_id[evidence_id].metadata.get("articleId") or "")
                for evidence_id in dependency.basis_evidence_ids
            }
            dependency_article_ids.discard("")
            missing_dependency_articles = (
                dependency_article_ids - citation_article_ids
            )
            if missing_dependency_articles:
                raise ContractViolation(
                    "final answer citations omit Articles declared as a resolved "
                    f"dependency basis for work item {dependency.work_item_id!r}: "
                    f"{sorted(missing_dependency_articles)}"
                )

        unresolved_work_item_ids = set(decision.answer.unresolved_work_item_ids)
        unresolved_hypothesis_ids = set(
            decision.answer.unresolved_hypothesis_ids
        )
        unknown_unresolved_work_items = unresolved_work_item_ids - set(work_items)
        if unknown_unresolved_work_items:
            raise ContractViolation(
                "answer names unknown unresolved WorkItems: "
                f"{sorted(unknown_unresolved_work_items)}"
            )
        unknown_unresolved_hypotheses = unresolved_hypothesis_ids - set(hypotheses)
        if unknown_unresolved_hypotheses:
            raise ContractViolation(
                "answer names unknown unresolved Hypotheses: "
                f"{sorted(unknown_unresolved_hypotheses)}"
            )
        if bool(decision.answer.limitations) != bool(unresolved_work_item_ids):
            raise ContractViolation(
                "answer limitations and unresolved_work_item_ids must either both "
                "be present or both be empty"
            )
        for hypothesis_id in unresolved_hypothesis_ids:
            hypothesis = hypotheses[hypothesis_id]
            if hypothesis.judgment != "unresolved":
                raise ContractViolation(
                    "answer unresolved Hypothesis must have judgment=unresolved: "
                    f"{hypothesis_id}"
                )
            if hypothesis.work_item_id not in unresolved_work_item_ids:
                raise ContractViolation(
                    "answer unresolved Hypothesis must belong to a named unresolved "
                    f"WorkItem: {hypothesis_id}"
                )
        unresolved_hypothesis_work_items = {
            hypotheses[hypothesis_id].work_item_id
            for hypothesis_id in unresolved_hypothesis_ids
        }
        unresolved_dependency_work_items = {
            dependency.work_item_id
            for dependency in dependency_by_key.values()
            if dependency.status == "needs_action"
        }
        missing_unresolved_hypotheses = (
            unresolved_work_item_ids
            - unresolved_hypothesis_work_items
            - unresolved_dependency_work_items
        )
        if missing_unresolved_hypotheses:
            raise ContractViolation(
                "each unresolved WorkItem requires an unresolved Hypothesis or "
                "needs_action dependency: "
                f"{sorted(missing_unresolved_hypotheses)}"
            )

    if decision.next == "finalize":
        open_work_item_ids = {
            item.work_item_id for item in work_items.values() if item.state == "open"
        }
        declared_unresolved_work_item_ids = set(
            decision.answer.unresolved_work_item_ids
        )
        if open_work_item_ids != declared_unresolved_work_item_ids:
            raise ContractViolation(
                "finalize must account for every open WorkItem as an unresolved "
                "answer scope; "
                f"open={sorted(open_work_item_ids)}, "
                f"declared={sorted(declared_unresolved_work_item_ids)}"
            )
        if open_work_item_ids and not (finalize_only or not can_start_next_cycle):
            raise ContractViolation(
                "finalize cannot leave unresolved WorkItems while another Cycle can "
                "start"
            )
        if (
            unreviewed_resolution is not None
            and unreviewed_resolution.action == "unresolved_at_limit"
            and not open_work_item_ids
        ):
            raise ContractViolation(
                "unresolved unreviewed Graph candidates require an unresolved answer "
                "scope"
            )
        unresolved_deferred_work_items = {
            item.work_item_id
            for item in deferred_resolutions
            if item.action == "unresolved_at_limit"
        }
        if not unresolved_deferred_work_items.issubset(open_work_item_ids):
            raise ContractViolation(
                "unresolved deferred Frontiers must reference unresolved answer "
                "WorkItems"
            )
        declared_basis_evidence_ids = {
            evidence_id
            for item in work_items.values()
            if item.state == "resolved"
            for hypothesis_id in item.basis_hypothesis_ids
            for evidence_id in hypotheses[hypothesis_id].evidence_ids
        }
        missing_basis_citations = declared_basis_evidence_ids - set(
            decision.answer.citation_ids
        )
        if missing_basis_citations:
            raise ContractViolation(
                "final answer citations omit Evidence declared as resolved WorkItem "
                f"basis: {sorted(missing_basis_citations)}"
            )

    deferred_resolution_by_frontier = {
        item.frontier_item_id: item
        for item in state.deferred_frontier_resolutions
    }
    if graph_review is not None:
        for item in graph_review.frontier_decisions:
            deferred_resolution_by_frontier.pop(item.frontier_item_id, None)
    for resolution in deferred_resolutions:
        deferred_resolution_by_frontier[resolution.frontier_item_id] = (
            resolution.model_copy(
                update={"decided_cycle": max(1, state.research_cycle_count)}
            )
        )

    return _validated_copy(
        state,
        non_work_item_requirements=non_work_item_requirements,
        work_items=tuple(work_items.values()),
        hypotheses=tuple(hypotheses.values()),
        tool_requests=(*state.tool_requests, *decision.tool_requests),
        focus_work_item_ids=focus_ids,
        retained_evidence_ids=retained_ids,
        review_finding_resolutions=(
            *state.review_finding_resolutions,
            *decision.review_finding_resolutions,
        ),
        dependency_decisions=tuple(dependency_by_key.values()),
        graph_candidate_reviews=(
            (
                *state.graph_candidate_reviews,
                graph_review.model_copy(
                    update={"reviewed_cycle": max(1, state.research_cycle_count)}
                ),
            )
            if graph_review is not None
            else state.graph_candidate_reviews
        ),
        search_candidate_reviews=(
            (
                *state.search_candidate_reviews,
                search_review.model_copy(
                    update={"reviewed_cycle": max(1, state.research_cycle_count)}
                ),
            )
            if search_review is not None
            else state.search_candidate_reviews
        ),
        frontier_re_adoptions=(
            *state.frontier_re_adoptions,
            *decision.frontier_re_adoptions,
        ),
        deferred_frontier_resolutions=tuple(
            deferred_resolution_by_frontier.values()
        ),
        unreviewed_graph_resolutions=(
            (
                *state.unreviewed_graph_resolutions,
                unreviewed_resolution.model_copy(
                    update={
                        "candidate_count": unreviewed_graph_candidate_count,
                        "decided_cycle": max(1, state.research_cycle_count),
                    }
                ),
            )
            if unreviewed_resolution is not None
            else state.unreviewed_graph_resolutions
        ),
        final_answer=decision.answer,
        updated_at=utc_now(),
    )


def _apply_impact(
    impact: WorkItemImpactDecision,
    work_items: dict[str, WorkItem],
    hypotheses: Mapping[str, Hypothesis],
    *,
    newly_contradicted: set[str],
    added_work_item_ids: set[str],
) -> None:
    current = work_items.get(impact.work_item_id)
    if current is None:
        raise ContractViolation(f"unknown impacted work item: {impact.work_item_id}")
    if current.state != "open":
        raise ContractViolation("impact decision requires an open work item")
    if not newly_contradicted.intersection(current.basis_hypothesis_ids):
        raise ContractViolation(
            "impact decision must address a newly contradicted basis"
        )
    unknown_basis = set(impact.new_basis_hypothesis_ids) - set(hypotheses)
    if unknown_basis:
        raise ContractViolation(
            f"impact decision has unknown basis IDs: {sorted(unknown_basis)}"
        )
    if newly_contradicted.intersection(impact.new_basis_hypothesis_ids):
        raise ContractViolation("impact decision retains a contradicted basis")

    if impact.action == "retain":
        work_items[current.work_item_id] = _validated_copy(
            current,
            basis_hypothesis_ids=impact.new_basis_hypothesis_ids,
        )
        return

    work_items[current.work_item_id] = _validated_copy(
        current,
        state="dropped",
        resolution=impact.reason,
    )
    if impact.action == "replace":
        if impact.replacement_work_item_id not in added_work_item_ids:
            raise ContractViolation(
                "replacement work item must be added in the same update"
            )
        replacement = work_items.get(impact.replacement_work_item_id or "")
        if replacement is None:
            raise ContractViolation("replacement work item does not exist")
        if replacement.replaces_work_item_id != current.work_item_id:
            raise ContractViolation(
                "replacement work item must point to the replaced work item"
            )
        if tuple(replacement.basis_hypothesis_ids) != tuple(
            impact.new_basis_hypothesis_ids
        ):
            raise ContractViolation("replacement basis must match the impact decision")
        return

    if impact.drop_subtree:
        for descendant_id in _descendant_ids(current.work_item_id, work_items):
            descendant = work_items[descendant_id]
            if descendant.state == "open":
                work_items[descendant_id] = _validated_copy(
                    descendant,
                    state="dropped",
                    resolution=impact.reason,
                )


def _validate_work_tree(
    work_items: Mapping[str, WorkItem],
    hypotheses: Mapping[str, Hypothesis],
) -> None:
    for item in work_items.values():
        if (
            item.parent_work_item_id is not None
            and item.parent_work_item_id not in work_items
        ):
            raise ContractViolation(
                f"unknown parent work item: {item.parent_work_item_id}"
            )
        if (
            item.replaces_work_item_id is not None
            and item.replaces_work_item_id not in work_items
        ):
            raise ContractViolation(
                f"unknown replaced work item: {item.replaces_work_item_id}"
            )
        unknown_basis = set(item.basis_hypothesis_ids) - set(hypotheses)
        if unknown_basis:
            raise ContractViolation(
                f"unknown basis hypothesis IDs: {sorted(unknown_basis)}"
            )
        unresolved_basis = {
            hypothesis_id
            for hypothesis_id in item.basis_hypothesis_ids
            if hypotheses[hypothesis_id].judgment == "unresolved"
        }
        if item.state == "resolved" and unresolved_basis:
            raise ContractViolation(
                "resolved work item retains unresolved basis hypotheses: "
                f"{item.work_item_id}={sorted(unresolved_basis)}"
            )
        if item.state == "open" and any(
            hypotheses[hypothesis_id].judgment == "contradicted"
            for hypothesis_id in item.basis_hypothesis_ids
        ):
            raise ContractViolation(
                f"open work item retains a contradicted basis: {item.work_item_id}"
            )

    for item in work_items.values():
        seen = {item.work_item_id}
        parent_id = item.parent_work_item_id
        while parent_id is not None:
            if parent_id in seen:
                raise ContractViolation("work item parent cycle detected")
            seen.add(parent_id)
            parent_id = work_items[parent_id].parent_work_item_id

    for item in work_items.values():
        if item.parent_work_item_id is None:
            continue
        parent = work_items[item.parent_work_item_id]
        if parent.state != "open" and item.state == "open":
            raise ContractViolation(
                f"closed parent has an open child: {item.work_item_id}"
            )


def _validate_hypotheses(
    hypotheses: Mapping[str, Hypothesis],
    work_items: Mapping[str, WorkItem],
    *,
    known_evidence_ids: set[str],
    material_evidence_ids: set[str],
    citable_evidence_ids: set[str],
    changed_hypothesis_ids: set[str],
) -> None:
    for item in hypotheses.values():
        if item.work_item_id not in work_items:
            raise ContractViolation(
                f"hypothesis has unknown work item: {item.work_item_id}"
            )
        unknown_evidence = set(item.evidence_ids) - known_evidence_ids
        if unknown_evidence:
            raise ContractViolation(
                f"hypothesis has unknown evidence IDs: {sorted(unknown_evidence)}"
            )
        if item.hypothesis_id in changed_hypothesis_ids and item.evidence_ids:
            unseen = set(item.evidence_ids) - material_evidence_ids
            if unseen:
                raise ContractViolation(
                    f"hypothesis update uses evidence not shown in full: "
                    f"{sorted(unseen)}"
                )
            navigation_only = set(item.evidence_ids) - citable_evidence_ids
            if navigation_only:
                raise ContractViolation(
                    "hypothesis update uses navigation-only evidence: "
                    f"{sorted(navigation_only)}"
                )


def _descendant_ids(
    root_id: str,
    work_items: Mapping[str, WorkItem],
) -> tuple[str, ...]:
    descendants: list[str] = []
    frontier = [root_id]
    while frontier:
        parent_id = frontier.pop()
        children = [
            item.work_item_id
            for item in work_items.values()
            if item.parent_work_item_id == parent_id
        ]
        descendants.extend(children)
        frontier.extend(children)
    return tuple(descendants)


def _raise_preflight_contract_violations(
    state: CaseState,
    decision: SolverDecision,
    *,
    fetchable_article_ids: Collection[str] | None,
    graph_known_article_ids: Collection[str] | None,
    article_fetch_tool_name: str | None,
    unreviewed_graph_candidate_count: int,
) -> None:
    """独立した主要違反を一括提示し、修復の逐次エラー化を防ぐ。"""

    violations: list[str] = []
    projected_states = {
        item.work_item_id: item.state for item in state.work_items
    }
    parent_by_id = {
        item.work_item_id: item.parent_work_item_id for item in state.work_items
    }
    for item in decision.update.add_work_items:
        projected_states[item.work_item_id] = item.state
        parent_by_id[item.work_item_id] = item.parent_work_item_id
    for item in decision.update.update_work_items:
        if item.work_item_id in projected_states:
            projected_states[item.work_item_id] = item.state
    for impact in decision.update.impact_decisions:
        if impact.action == "retain" or impact.work_item_id not in projected_states:
            continue
        projected_states[impact.work_item_id] = "dropped"
        if impact.drop_subtree:
            pending = [impact.work_item_id]
            while pending:
                parent_id = pending.pop()
                children = [
                    work_item_id
                    for work_item_id, candidate_parent_id in parent_by_id.items()
                    if candidate_parent_id == parent_id
                ]
                for child_id in children:
                    if projected_states.get(child_id) == "open":
                        projected_states[child_id] = "dropped"
                    pending.append(child_id)

    invalid_focus_ids = {
        work_item_id
        for work_item_id in decision.next_focus_work_item_ids
        if projected_states.get(work_item_id) != "open"
    }
    if invalid_focus_ids and not (
        decision.start_next_cycle
        and not any(
            work_item_state == "open"
            for work_item_state in projected_states.values()
        )
    ):
        violations.append(
            "focus must reference open WorkItem IDs: "
            f"{sorted(invalid_focus_ids)}"
        )

    invalid_tool_work_item_ids = {
        request.work_item_id
        for request in decision.tool_requests
        if projected_states.get(request.work_item_id) != "open"
    }
    if invalid_tool_work_item_ids:
        violations.append(
            "tool requests must reference open WorkItem IDs: "
            f"{sorted(invalid_tool_work_item_ids)}"
        )

    projected_hypothesis_ids = {
        item.hypothesis_id for item in state.hypotheses
    } | {
        item.hypothesis_id for item in decision.update.add_hypotheses
    }
    unknown_tool_hypothesis_ids = {
        hypothesis_id
        for request in decision.tool_requests
        for hypothesis_id in request.hypothesis_ids
        if hypothesis_id not in projected_hypothesis_ids
    }
    if unknown_tool_hypothesis_ids:
        violations.append(
            "tool requests reference unknown Hypothesis IDs: "
            f"{sorted(unknown_tool_hypothesis_ids)}"
        )

    for request in decision.tool_requests:
        requested_article_ids = {
            article_id
            for article_id in request.arguments.get("article_ids", ())
            if isinstance(article_id, str)
        }
        allowed_article_ids = _allowed_tool_article_ids(
            state,
            request.tool_name,
            fetchable_article_ids=fetchable_article_ids,
            graph_known_article_ids=graph_known_article_ids,
            article_fetch_tool_name=article_fetch_tool_name,
        )
        if (
            allowed_article_ids is not None
            and (unknown_article_ids := requested_article_ids - allowed_article_ids)
        ):
            violations.append(
                "tool request references unknown Article IDs: "
                f"{sorted(unknown_article_ids)}"
            )

    known_evidence_ids = {item.evidence_id for item in state.evidence}
    decision_evidence_ids = {
        evidence_id
        for hypothesis in (
            *decision.update.add_hypotheses,
            *decision.update.update_hypotheses,
        )
        for evidence_id in hypothesis.evidence_ids
    }
    unknown_evidence_ids = decision_evidence_ids - known_evidence_ids
    if unknown_evidence_ids:
        violations.append(
            "hypothesis has unknown evidence IDs: "
            f"{sorted(unknown_evidence_ids)}"
        )

    cycle_boundary = decision.next == "finalize" or decision.start_next_cycle
    if (
        unreviewed_graph_candidate_count
        and cycle_boundary
        and decision.unreviewed_graph_resolution is None
    ):
        violations.append(
            "Cycle boundary must state how the unreviewed Graph candidate pool "
            "will be handled"
        )

    if decision.next == "finalize" and decision.answer is not None:
        open_work_item_ids = {
            work_item_id
            for work_item_id, work_item_state in projected_states.items()
            if work_item_state == "open"
        }
        declared_unresolved_work_item_ids = set(
            decision.answer.unresolved_work_item_ids
        )
        if open_work_item_ids != declared_unresolved_work_item_ids:
            violations.append(
                "finalize must account for every open WorkItem as an unresolved "
                "answer scope; "
                f"open={sorted(open_work_item_ids)}, "
                f"declared={sorted(declared_unresolved_work_item_ids)}"
            )

    if not violations:
        return
    if len(violations) == 1:
        raise ContractViolation(violations[0])
    raise ContractViolation(
        "multiple contract violations: " + " | ".join(violations)
    )


def _require_unique_state_ids(state: CaseState) -> None:
    _reject_duplicate_delta_ids(
        (item.work_item_id for item in state.work_items),
        "stored work item",
    )
    _reject_duplicate_delta_ids(
        (item.hypothesis_id for item in state.hypotheses),
        "stored hypothesis",
    )
    _reject_duplicate_delta_ids(
        (item.evidence_id for item in state.evidence),
        "stored evidence",
    )
    _reject_duplicate_delta_ids(
        (item.request_id for item in state.tool_requests),
        "stored tool request",
    )
    dependency_keys = tuple(
        (item.dependency_kind, item.work_item_id)
        for item in state.dependency_decisions
    )
    if len(dependency_keys) != len(set(dependency_keys)):
        raise ContractViolation("stored dependency decision keys must be unique")
def _validated_copy(model: ModelT, /, **updates) -> ModelT:
    try:
        return type(model).model_validate({**model.model_dump(), **updates})
    except ValidationError as exc:
        detail = exc.errors(include_url=False, include_input=False)[0]
        location = ".".join(str(item) for item in detail.get("loc", ()))
        message = str(detail.get("msg") or "validation failed")
        raise ContractViolation(
            "updated state violates its schema"
            f" at {location or '<root>'}: {message}"
        ) from exc


def _allowed_tool_article_ids(
    state: CaseState,
    tool_name: str,
    *,
    fetchable_article_ids: Collection[str] | None,
    graph_known_article_ids: Collection[str] | None,
    article_fetch_tool_name: str | None,
) -> set[str] | None:
    """本文取得候補とGraph起点の既知Article集合を混同しない。"""

    fetch_tool_name = article_fetch_tool_name or "fetch_articles"
    if tool_name == fetch_tool_name:
        return (
            set(fetchable_article_ids)
            if fetchable_article_ids is not None
            else None
        )
    known_article_ids = set(fetchable_article_ids or ())
    known_article_ids.update(graph_known_article_ids or ())
    for evidence in state.evidence:
        for key in ("articleId", "fromArticleId", "toArticleId"):
            article_id = evidence.metadata.get(key)
            if isinstance(article_id, str) and article_id:
                known_article_ids.add(article_id)
    return known_article_ids


def _tool_request_scope(
    request: ToolRequest,
) -> tuple[str, str, tuple[str, ...], str]:
    """意味を解釈せず、同一Tool scopeの完全一致だけを比較する。"""

    return (
        request.tool_name,
        request.work_item_id,
        tuple(sorted(request.hypothesis_ids)),
        json.dumps(
            request.arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _reject_duplicate_delta_ids(values, label: str) -> None:
    values = tuple(values)
    if len(values) != len(set(values)):
        raise ContractViolation(f"{label} IDs must be unique")
