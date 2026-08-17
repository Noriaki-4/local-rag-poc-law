"""法令Domainの用途別Profileを環境設定から解決する。"""

from pathlib import Path

from app.agent_framework.profiles import (
    AgentLimits,
    AgentProfile,
    AutomaticToolProfile,
    ModelCallProfile,
    ReviewerProfile,
    ToolListArgumentLimit,
)
from app.config import settings

_PROMPT_DIR = Path(__file__).with_name("prompts")


def legal_agent_profile() -> AgentProfile:
    common_solver_prompt = _read_prompt("solver_common.md")
    return AgentProfile(
        name="legal-default",
        version="49",
        provider=settings.llm_provider,
        solver_research=ModelCallProfile(
            model=settings.agent_framework_research_model,
            max_output_tokens=settings.agent_framework_research_max_tokens,
            timeout_sec=settings.agent_framework_model_timeout_sec,
            system_prompt=_join_prompts(
                common_solver_prompt,
                _read_prompt("solver_research.md"),
            ),
        ),
        solver_integration=ModelCallProfile(
            model=settings.agent_framework_integration_model,
            max_output_tokens=settings.agent_framework_integration_max_tokens,
            timeout_sec=settings.agent_framework_model_timeout_sec,
            system_prompt=_join_prompts(
                common_solver_prompt,
                _read_prompt("solver_integration.md"),
            ),
        ),
        solver_graph_review=ModelCallProfile(
            model=settings.agent_framework_integration_model,
            max_output_tokens=min(
                settings.agent_framework_integration_max_tokens,
                4096,
            ),
            timeout_sec=settings.agent_framework_model_timeout_sec,
            system_prompt=_read_prompt("solver_graph_review.md"),
        ),
        reviewer=ReviewerProfile(
            enabled=settings.agent_framework_reviewer_enabled,
            max_revisions=settings.agent_framework_reviewer_max_revisions,
            model=settings.agent_framework_reviewer_model,
            max_output_tokens=settings.agent_framework_reviewer_max_tokens,
            timeout_sec=settings.agent_framework_model_timeout_sec,
            system_prompt=_read_prompt("reviewer.md"),
        ),
        automatic_tools=(
            AutomaticToolProfile(
                trigger_tool_name="fetch_articles",
                tool_name="legal_graph_neighbors",
                copied_argument_names=("article_ids",),
                fixed_arguments={
                    "edge_types": ["REFERENCES", "IMPLEMENTS", "APPLIED_BY"],
                    "max_relations": 50,
                },
                deduplicate_list_argument="article_ids",
                one_hop_candidate_metadata_key="neighborArticleId",
                independent_root_metadata_key="articleId",
                independent_root_evidence_role="search_navigation",
                purpose="本文取得対象Articleの1ホップ関係を取得する",
            ),
        ),
        tool_list_argument_limits=(
            ToolListArgumentLimit(
                tool_name="fetch_articles",
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
