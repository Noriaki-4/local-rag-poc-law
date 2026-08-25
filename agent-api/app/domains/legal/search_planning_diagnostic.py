"""本番の検索要求作成Promptだけを1回呼ぶ実モデル診断。"""

from __future__ import annotations

from collections.abc import Iterable

from app.adapters.models.structured_json import render_solver_model_call
from app.adapters.tools.legal_search import LegalSearchTool
from app.agent_framework.context import SolverContext, build_solver_context
from app.agent_framework.model_call_artifacts import RenderedModelCall
from app.agent_framework.state import CaseState, Hypothesis, WorkItem
from app.domains.legal.profiles import legal_agent_profile
from app.domains.legal.staged_research_diagnostic import (
    StagedResearchDiagnosticRun,
    StructuredJSONClient,
    run_staged_research_diagnostic,
)


def build_search_planning_context(
    question: str,
    work_items: Iterable[WorkItem | dict[str, object]],
    hypotheses: Iterable[Hypothesis | dict[str, object]],
    *,
    non_work_item_requirements: Iterable[str] = (),
) -> SolverContext:
    """確定済みWorkItem・Hypothesisから本番と同じ検索入力Viewを作る。"""

    normalized_question = question.strip()
    if not normalized_question:
        raise ValueError("question must not be empty")
    normalized_work_items = tuple(
        item if isinstance(item, WorkItem) else WorkItem.model_validate(item)
        for item in work_items
    )
    normalized_hypotheses = tuple(
        item if isinstance(item, Hypothesis) else Hypothesis.model_validate(item)
        for item in hypotheses
    )
    if not normalized_work_items:
        raise ValueError("at least one WorkItem is required")
    if not normalized_hypotheses:
        raise ValueError("at least one Hypothesis is required")

    work_item_ids = [item.work_item_id for item in normalized_work_items]
    hypothesis_ids = [item.hypothesis_id for item in normalized_hypotheses]
    if len(work_item_ids) != len(set(work_item_ids)):
        raise ValueError("WorkItem IDs must be unique")
    if len(hypothesis_ids) != len(set(hypothesis_ids)):
        raise ValueError("Hypothesis IDs must be unique")
    known_work_item_ids = set(work_item_ids)
    unknown_work_item_ids = {
        item.work_item_id
        for item in normalized_hypotheses
        if item.work_item_id not in known_work_item_ids
    }
    if unknown_work_item_ids:
        raise ValueError(
            "Hypotheses reference unknown WorkItem IDs: "
            f"{sorted(unknown_work_item_ids)}"
        )
    if any(item.state != "open" for item in normalized_work_items):
        raise ValueError("search planning requires open WorkItems")
    if any(item.judgment != "unresolved" for item in normalized_hypotheses):
        raise ValueError("search planning requires unresolved Hypotheses")

    profile = legal_agent_profile()
    return build_solver_context(
        CaseState(
            case_id="search-planning-diagnostic",
            question=normalized_question,
            work_items=normalized_work_items,
            hypotheses=normalized_hypotheses,
            non_work_item_requirements=tuple(non_work_item_requirements),
        ),
        profile.limits,
        remaining_wall_time_sec=profile.limits.max_wall_time_sec,
        finalize_only=False,
        available_tools=(LegalSearchTool.definition,),
    )


def render_search_planning_call(
    question: str,
    work_items: Iterable[WorkItem | dict[str, object]],
    hypotheses: Iterable[Hypothesis | dict[str, object]],
    *,
    non_work_item_requirements: Iterable[str] = (),
    provider: str,
    model: str,
) -> tuple[RenderedModelCall, SolverContext]:
    """本番Profileの検索Prompt・入力・schemaを完成形にする。"""

    agent_profile = legal_agent_profile()
    profile = agent_profile.solver_search_planning
    if profile is None:
        raise ValueError("search planning profile is unavailable")
    context = build_search_planning_context(
        question,
        work_items,
        hypotheses,
        non_work_item_requirements=non_work_item_requirements,
    )
    rendered = render_solver_model_call(
        context,
        profile.model_copy(update={"model": model}),
        provider=provider,
        stage="search_planning_diagnostic",
    )
    return rendered, context


def run_search_planning_diagnostic(
    question: str,
    work_items: Iterable[WorkItem | dict[str, object]],
    hypotheses: Iterable[Hypothesis | dict[str, object]],
    *,
    non_work_item_requirements: Iterable[str] = (),
    provider: str,
    model: str,
    max_tokens: int,
    timeout_sec: int,
    client: StructuredJSONClient,
) -> StagedResearchDiagnosticRun:
    """検索や修復を起動せず、検索要求作成の初回応答だけを観測する。"""

    rendered, context = render_search_planning_call(
        question,
        work_items,
        hypotheses,
        non_work_item_requirements=non_work_item_requirements,
        provider=provider,
        model=model,
    )
    return run_staged_research_diagnostic(
        rendered,
        context,
        projection="research_search",
        model=model,
        max_tokens=max_tokens,
        timeout_sec=timeout_sec,
        client=client,
    )
