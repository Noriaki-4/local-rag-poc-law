"""法令Domainの用途別Profileを環境設定から解決する。"""

from pathlib import Path
from typing import Literal

from app.agent_framework.profiles import (
    AgentLimits,
    AgentProfile,
    ModelCallProfile,
    ReviewerProfile,
    ToolListArgumentLimit,
)
from app.config import settings

_PROMPT_DIR = Path(__file__).with_name("prompts")


def legal_agent_profile() -> AgentProfile:
    solver_identity_prompt = _read_prompt("solver_identity.md")
    common_solver_prompt = _read_prompt("solver_common.md")
    tool_prompt = _read_prompt("solver_tools.md")
    completion_prompt = _read_prompt("solver_completion.md")
    integration_model = settings.agent_framework_integration_model
    integration_max_tokens = settings.agent_framework_integration_max_tokens
    timeout_sec = settings.agent_framework_model_timeout_sec
    return AgentProfile(
        name="legal-default",
        version="380",
        provider=settings.llm_provider,
        solver_research=_model_profile(
            model=settings.agent_framework_research_model,
            max_tokens=settings.agent_framework_research_max_tokens,
            timeout_sec=timeout_sec,
            context_projection="research_decomposition",
            prompts=(
                _read_prompt("solver_question_decomposition.md"),
            ),
            completion_check_prompt=_read_prompt(
                "solver_question_decomposition_check.md"
            ),
            available_tool_names=(),
        ),
        solver_hypothesis_generation=_model_profile(
            model=settings.agent_framework_research_model,
            max_tokens=settings.agent_framework_research_max_tokens,
            timeout_sec=timeout_sec,
            context_projection="research_hypothesis",
            prompts=(
                _read_prompt("solver_hypothesis_generation.md"),
            ),
            completion_check_prompt=_read_prompt(
                "solver_hypothesis_generation_check.md"
            ),
            available_tool_names=(),
        ),
        solver_search_planning=_model_profile(
            model=settings.agent_framework_research_model,
            max_tokens=settings.agent_framework_research_max_tokens,
            timeout_sec=timeout_sec,
            context_projection="research_search",
            prompts=(
                _read_prompt("solver_search_planning.md"),
            ),
            completion_check_prompt=_read_prompt(
                "solver_search_planning_check.md"
            ),
            available_tool_names=("legal_search",),
        ),
        solver_integration=_model_profile(
            model=integration_model,
            max_tokens=integration_max_tokens,
            timeout_sec=timeout_sec,
            prompts=(
                solver_identity_prompt,
                _read_prompt("solver_integration.md"),
                common_solver_prompt,
                tool_prompt,
                completion_prompt,
            ),
            completion_check_prompt=_read_prompt("solver_integration_check.md"),
            dependency_action_system_prompt=_join_prompts(
                solver_identity_prompt,
                _read_prompt("solver_dependency_action.md"),
                tool_prompt,
            ),
            dependency_action_completion_check_prompt=_read_prompt(
                "solver_dependency_action_check.md"
            ),
        ),
        solver_cycle_close=ModelCallProfile(
            model=integration_model,
            max_output_tokens=integration_max_tokens,
            timeout_sec=timeout_sec,
            system_prompt=_read_prompt("solver_evidence_integration.md"),
            followup_system_prompt=_read_prompt("solver_cycle_close.md"),
            completion_check_prompt=_read_prompt(
                "solver_evidence_integration_check.md"
            ),
            followup_completion_check_prompt=_read_prompt(
                "solver_cycle_close_check.md"
            ),
            dependency_system_prompt=_read_prompt(
                "solver_dependency_assessment.md"
            ),
            dependency_completion_check_prompt=_read_prompt(
                "solver_dependency_assessment_check.md"
            ),
            final_answer_check_system_prompt=_read_prompt(
                "solver_final_answer_check.md"
            ),
            final_answer_check_completion_prompt=_read_prompt(
                "solver_final_answer_check_completion.md"
            ),
            context_projection="cycle_close",
            available_tool_names=(),
        ),
        solver_finalization=_model_profile(
            model=integration_model,
            max_tokens=integration_max_tokens,
            timeout_sec=timeout_sec,
            context_projection="finalization",
            prompts=(
                solver_identity_prompt,
                _read_prompt("solver_finalization.md"),
            ),
            completion_check_prompt=_read_prompt("solver_finalization_check.md"),
            available_tool_names=(),
        ),
        solver_reviewer_revision=_model_profile(
            model=integration_model,
            max_tokens=integration_max_tokens,
            timeout_sec=timeout_sec,
            prompts=(
                solver_identity_prompt,
                _read_prompt("solver_reviewer_revision.md"),
                common_solver_prompt,
                tool_prompt,
                completion_prompt,
            ),
            completion_check_prompt=_read_prompt(
                "solver_reviewer_revision_check.md"
            ),
        ),
        solver_search_review=ModelCallProfile(
            model=integration_model,
            max_output_tokens=integration_max_tokens,
            timeout_sec=timeout_sec,
            system_prompt=_read_prompt("solver_search_selection.md"),
            # 分割済みfixtureの隔離診断互換。実行経路では使用しない。
            followup_system_prompt=_read_prompt(
                "solver_search_reselection.md"
            ),
            completion_check_prompt=_read_prompt(
                "solver_search_selection_check.md"
            ),
            followup_completion_check_prompt=_read_prompt(
                "solver_search_reselection_check.md"
            ),
        ),
        solver_graph_review=ModelCallProfile(
            model=integration_model,
            max_output_tokens=integration_max_tokens,
            timeout_sec=timeout_sec,
            system_prompt=_read_prompt("solver_graph_review.md"),
            completion_check_prompt=_read_prompt(
                "solver_graph_review_check.md"
            ),
            context_projection="graph_review",
        ),
        reviewer=ReviewerProfile(
            enabled=settings.agent_framework_reviewer_enabled,
            max_revisions=settings.agent_framework_reviewer_max_revisions,
            model=settings.agent_framework_reviewer_model,
            max_output_tokens=settings.agent_framework_reviewer_max_tokens,
            timeout_sec=settings.agent_framework_model_timeout_sec,
            system_prompt=_read_prompt("reviewer.md"),
        ),
        required_dependency_kind="lower_norm",
        tool_list_argument_limits=(
            ToolListArgumentLimit(
                tool_name="fetch_articles",
                argument_name="article_ids",
                max_items=5,
            ),
            ToolListArgumentLimit(
                tool_name="legal_graph_neighbors",
                argument_name="article_ids",
                max_items=4,
            ),
        ),
        graph_review_fetch_tool_name="fetch_articles",
        limits=AgentLimits(
            max_research_cycles=settings.agent_framework_max_research_cycles,
            max_tool_requests_per_step=(
                settings.agent_framework_max_tool_requests_per_step
            ),
            max_fetched_resources_per_cycle=(
                settings.agent_framework_max_fetched_resources_per_cycle
            ),
            max_selected_frontier_per_step=(
                settings.agent_framework_max_selected_frontier_per_step
            ),
            max_graph_candidates_per_review_batch=(
                settings.agent_framework_max_graph_candidates_per_review_batch
            ),
            max_parallel_tools=settings.agent_framework_max_parallel_tools,
            max_retained_evidence=settings.agent_framework_max_retained_evidence,
            max_material_evidence_chars=(
                settings.agent_framework_max_material_evidence_chars
            ),
            max_solver_input_chars=(
                settings.agent_framework_max_solver_input_chars
            ),
            cycle_close_reserve_sec=(
                settings.agent_framework_cycle_close_reserve_sec
            ),
            min_next_cycle_budget_sec=(
                settings.agent_framework_min_next_cycle_budget_sec
            ),
            finalization_reserve_sec=(
                settings.agent_framework_finalization_reserve_sec
            ),
            max_wall_time_sec=settings.agent_framework_max_wall_time_sec,
        ),
    )


def _read_prompt(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8").strip()


def _join_prompts(*prompts: str) -> str:
    return "\n\n".join(prompt.strip() for prompt in prompts if prompt.strip())


def _model_profile(
    *,
    model: str,
    max_tokens: int,
    timeout_sec: float,
    prompts: tuple[str, ...],
    completion_check_prompt: str,
    context_projection: Literal[
        "full",
        "initial_research",
        "research_decomposition",
        "research_hypothesis",
        "research_search",
        "graph_review",
        "observation_integration",
        "cycle_close",
        "finalization",
    ] = "full",
    available_tool_names: tuple[str, ...] | None = None,
    dependency_action_system_prompt: str | None = None,
    dependency_action_completion_check_prompt: str | None = None,
) -> ModelCallProfile:
    return ModelCallProfile(
        model=model,
        max_output_tokens=max_tokens,
        timeout_sec=timeout_sec,
        system_prompt=_join_prompts(*prompts),
        completion_check_prompt=completion_check_prompt,
        context_projection=context_projection,
        available_tool_names=available_tool_names,
        dependency_action_system_prompt=dependency_action_system_prompt,
        dependency_action_completion_check_prompt=(
            dependency_action_completion_check_prompt
        ),
    )
