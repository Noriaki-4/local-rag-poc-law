"""本番のHypothesis生成Promptだけを1回呼ぶ実モデル診断。"""

from __future__ import annotations

from collections.abc import Iterable

from app.adapters.models.structured_json import (
    _project_next_hypothesis_work_item,
    render_solver_model_call,
)
from app.agent_framework.context import SolverContext, build_solver_context
from app.agent_framework.model_call_artifacts import RenderedModelCall
from app.agent_framework.state import CaseState, WorkItem
from app.domains.legal.profiles import legal_agent_profile
from app.domains.legal.staged_research_diagnostic import (
    StagedResearchDiagnosticRun,
    StructuredJSONClient,
    run_staged_research_diagnostic,
)


def build_hypothesis_generation_context(
    question: str,
    work_items: Iterable[WorkItem | dict[str, object]],
    *,
    non_work_item_requirements: Iterable[str] = (),
) -> SolverContext:
    """確定済みWorkItemを持つ状態から、本番と同じ入力Viewを作る。"""

    normalized_question = question.strip()
    if not normalized_question:
        raise ValueError("question must not be empty")
    normalized_work_items = tuple(
        item if isinstance(item, WorkItem) else WorkItem.model_validate(item)
        for item in work_items
    )
    if not normalized_work_items:
        raise ValueError("at least one WorkItem is required")
    work_item_ids = [item.work_item_id for item in normalized_work_items]
    if len(work_item_ids) != len(set(work_item_ids)):
        raise ValueError("WorkItem IDs must be unique")
    if any(item.state != "open" for item in normalized_work_items):
        raise ValueError("hypothesis generation requires open WorkItems")
    profile = legal_agent_profile()
    return build_solver_context(
        CaseState(
            case_id="hypothesis-generation-diagnostic",
            question=normalized_question,
            work_items=normalized_work_items,
            non_work_item_requirements=tuple(non_work_item_requirements),
        ),
        profile.limits,
        remaining_wall_time_sec=profile.limits.max_wall_time_sec,
        finalize_only=False,
    )


def render_hypothesis_generation_call(
    question: str,
    work_items: Iterable[WorkItem | dict[str, object]],
    *,
    non_work_item_requirements: Iterable[str] = (),
    provider: str,
    model: str,
) -> tuple[RenderedModelCall, SolverContext]:
    """本番ProfileのHypothesis生成Prompt・入力・schemaを完成形にする。"""

    agent_profile = legal_agent_profile()
    profile = agent_profile.solver_hypothesis_generation
    if profile is None:
        raise ValueError("hypothesis generation profile is unavailable")
    context = build_hypothesis_generation_context(
        question,
        work_items,
        non_work_item_requirements=non_work_item_requirements,
    )
    context = _project_next_hypothesis_work_item(context)
    rendered = render_solver_model_call(
        context,
        profile.model_copy(update={"model": model}),
        provider=provider,
        stage="hypothesis_generation_diagnostic",
    )
    return rendered, context


def run_hypothesis_generation_diagnostic(
    question: str,
    work_items: Iterable[WorkItem | dict[str, object]],
    *,
    non_work_item_requirements: Iterable[str] = (),
    provider: str,
    model: str,
    max_tokens: int,
    timeout_sec: int,
    client: StructuredJSONClient,
) -> StagedResearchDiagnosticRun:
    """検索や修復を起動せず、Hypothesis生成の初回応答だけを観測する。"""

    rendered, context = render_hypothesis_generation_call(
        question,
        work_items,
        non_work_item_requirements=non_work_item_requirements,
        provider=provider,
        model=model,
    )
    return run_staged_research_diagnostic(
        rendered,
        context,
        projection="research_hypothesis",
        model=model,
        max_tokens=max_tokens,
        timeout_sec=timeout_sec,
        client=client,
    )
