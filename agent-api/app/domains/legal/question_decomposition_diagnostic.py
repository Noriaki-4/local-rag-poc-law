"""本番の質問分解Promptだけを1回呼ぶ実モデル診断。"""

from __future__ import annotations

from app.adapters.models.structured_json import (
    render_solver_model_call,
)
from app.agent_framework.context import SolverContext, build_solver_context
from app.agent_framework.model_call_artifacts import RenderedModelCall
from app.agent_framework.state import CaseState
from app.domains.legal.profiles import legal_agent_profile
from app.domains.legal.staged_research_diagnostic import (
    StagedResearchDiagnosticRun,
    StructuredJSONClient,
    run_staged_research_diagnostic,
)

QuestionDecompositionDiagnosticRun = StagedResearchDiagnosticRun


def build_question_decomposition_context(question: str) -> SolverContext:
    """質問だけを持つ初期状態から、本番と同じSolverContextを投影する。"""

    normalized_question = question.strip()
    if not normalized_question:
        raise ValueError("question must not be empty")
    profile = legal_agent_profile()
    return build_solver_context(
        CaseState(
            case_id="question-decomposition-diagnostic",
            question=normalized_question,
        ),
        profile.limits,
        remaining_wall_time_sec=profile.limits.max_wall_time_sec,
        finalize_only=False,
    )


def render_question_decomposition_call(
    question: str,
    *,
    provider: str,
    model: str,
) -> tuple[RenderedModelCall, SolverContext]:
    """本番Profileの質問分解Prompt・入力投影・schemaを完成形にする。"""

    agent_profile = legal_agent_profile()
    profile = agent_profile.solver_research.model_copy(update={"model": model})
    context = build_question_decomposition_context(question)
    rendered = render_solver_model_call(
        context,
        profile,
        provider=provider,
        stage="question_decomposition_diagnostic",
    )
    return rendered, context


def run_question_decomposition_diagnostic(
    question: str,
    *,
    provider: str,
    model: str,
    max_tokens: int,
    timeout_sec: int,
    client: StructuredJSONClient,
) -> QuestionDecompositionDiagnosticRun:
    """修復や後続Stepを起動せず、質問分解の初回応答だけを観測する。"""

    rendered, context = render_question_decomposition_call(
        question,
        provider=provider,
        model=model,
    )
    return run_staged_research_diagnostic(
        rendered,
        context,
        projection="research_decomposition",
        model=model,
        max_tokens=max_tokens,
        timeout_sec=timeout_sec,
        client=client,
    )
