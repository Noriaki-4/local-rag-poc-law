"""用途別modelと実行上限を、AgentLoopから分離して解決する。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, Field, model_validator

from .state import FrameworkModel


class ModelCallProfile(FrameworkModel):
    model: str = Field(min_length=1, max_length=200)
    max_output_tokens: int = Field(default=4096, ge=256)
    timeout_sec: float = Field(default=30.0, gt=0)
    system_prompt: str = Field(min_length=1)
    followup_system_prompt: str | None = None
    completion_check_prompt: str | None = None
    followup_completion_check_prompt: str | None = None
    dependency_system_prompt: str | None = None
    dependency_completion_check_prompt: str | None = None
    dependency_action_system_prompt: str | None = None
    dependency_action_completion_check_prompt: str | None = None
    final_answer_check_system_prompt: str | None = None
    final_answer_check_completion_prompt: str | None = None
    context_projection: Literal[
        "full",
        "integration",
        "initial_research",
        "research_decomposition",
        "research_hypothesis",
        "research_search",
        "hypothesis_revision",
        "graph_review",
        "observation_integration",
        "cycle_close",
        "finalization",
    ] = "full"
    available_tool_names: tuple[str, ...] | None = None


class ReviewerProfile(ModelCallProfile):
    enabled: bool = False
    max_revisions: int = Field(default=1, ge=0, le=3)


class AutomaticToolProfile(FrameworkModel):
    """Solverが選んだ引数を別のread-only Toolへ機械的に転記する。"""

    trigger_tool_name: str = Field(min_length=1, max_length=160)
    tool_name: str = Field(min_length=1, max_length=160)
    copied_argument_names: tuple[str, ...]
    fixed_arguments: dict[str, Any] = Field(default_factory=dict)
    deduplicate_list_argument: str | None = Field(default=None, max_length=160)
    one_hop_candidate_metadata_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
    )
    independent_root_metadata_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
    )
    independent_root_evidence_role: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
    )
    purpose: str = Field(min_length=1, max_length=1000)
    solver_may_request: bool = False

    @model_validator(mode="after")
    def validate_mapping(self) -> AutomaticToolProfile:
        if self.trigger_tool_name == self.tool_name:
            raise ValueError("automatic tool must differ from its trigger")
        if not self.copied_argument_names:
            raise ValueError("automatic tool requires copied arguments")
        if len(self.copied_argument_names) != len(set(self.copied_argument_names)):
            raise ValueError("automatic tool copied arguments must be unique")
        overlap = set(self.copied_argument_names).intersection(self.fixed_arguments)
        if overlap:
            raise ValueError(
                f"automatic tool arguments cannot be copied and fixed: {sorted(overlap)}"
            )
        if (
            self.deduplicate_list_argument is not None
            and self.deduplicate_list_argument not in self.copied_argument_names
        ):
            raise ValueError("deduplicated argument must be copied from the trigger")
        if (
            self.one_hop_candidate_metadata_key is not None
            and self.deduplicate_list_argument is None
        ):
            raise ValueError(
                "one-hop automatic tool requires a deduplicated list argument"
            )
        if (self.independent_root_metadata_key is None) != (
            self.independent_root_evidence_role is None
        ):
            raise ValueError(
                "independent root metadata key and evidence role must be configured together"
            )
        if (
            self.independent_root_metadata_key is not None
            and self.one_hop_candidate_metadata_key is None
        ):
            raise ValueError(
                "independent roots require one-hop candidate deduplication"
            )
        return self


class ToolListArgumentLimit(FrameworkModel):
    """Tool引数の件数だけを決定的に制限する。"""

    tool_name: str = Field(min_length=1, max_length=160)
    argument_name: str = Field(min_length=1, max_length=160)
    max_items: int = Field(ge=1, le=1000)


class AgentLimits(FrameworkModel):
    max_research_cycles: int = Field(default=5, ge=1, le=5)
    max_tool_requests_per_step: int = Field(
        default=4,
        ge=1,
        le=16,
        validation_alias=AliasChoices(
            "max_tool_requests_per_step",
            "max_tool_requests_per_cycle",
        ),
    )
    max_fetched_resources_per_cycle: int = Field(
        default=4,
        ge=1,
        le=32,
        description="1 WorkItemが1 Cycleで本文取得できるArticle数上限。",
    )
    max_selected_frontier_per_step: int = Field(default=3, ge=1, le=16)
    max_graph_articles_per_hypothesis_per_cycle: int = Field(
        default=3,
        ge=1,
        le=16,
    )
    max_graph_candidates_per_review_batch: int = Field(default=20, ge=1, le=200)
    max_parallel_tools: int = Field(default=4, ge=1, le=16)
    max_parallel_work_items: int = Field(default=4, ge=1, le=16)
    max_retained_evidence: int = Field(default=12, ge=0, le=60)
    max_material_evidence_chars: int = Field(default=50000, ge=1000, le=200000)
    max_solver_input_chars: int = Field(default=240000, ge=2000, le=1000000)
    cycle_close_reserve_sec: float = Field(default=15.0, gt=0)
    min_next_cycle_budget_sec: float = Field(default=25.0, gt=0)
    finalization_reserve_sec: float = Field(
        default=35.0,
        gt=0,
        validation_alias=AliasChoices(
            "finalization_reserve_sec",
            "next_solver_call_reserve_sec",
        ),
    )
    max_wall_time_sec: float = Field(default=180.0, gt=0)

    @model_validator(mode="after")
    def reserve_must_fit_wall_time(self) -> AgentLimits:
        if self.finalization_reserve_sec >= self.max_wall_time_sec:
            raise ValueError("finalization reserve must fit within wall time")
        if self.cycle_close_reserve_sec >= self.max_wall_time_sec:
            raise ValueError("cycle close reserve must fit within wall time")
        if self.min_next_cycle_budget_sec >= self.max_wall_time_sec:
            raise ValueError("next cycle budget must fit within wall time")
        if self.max_material_evidence_chars >= self.max_solver_input_chars:
            raise ValueError(
                "material evidence limit must leave room for solver context"
            )
        return self

    @property
    def max_tool_requests_per_cycle(self) -> int:
        """旧参照名。制約の実体は1 Solver Decision（1 step）の上限。"""

        return self.max_tool_requests_per_step

    @property
    def next_solver_call_reserve_sec(self) -> float:
        """旧参照名。新規コードは用途別reserveを使う。"""

        return self.finalization_reserve_sec


class AgentProfile(FrameworkModel):
    name: str = Field(min_length=1, max_length=160)
    version: str = Field(default="1", min_length=1, max_length=80)
    provider: str = Field(min_length=1, max_length=80)
    solver_research: ModelCallProfile
    solver_hypothesis_generation: ModelCallProfile | None = None
    solver_hypothesis_revision: ModelCallProfile | None = None
    solver_search_planning: ModelCallProfile | None = None
    solver_integration: ModelCallProfile = Field(
        validation_alias=AliasChoices("solver_integration", "solver_finalize")
    )
    solver_observation_integration: ModelCallProfile | None = None
    solver_cycle_close: ModelCallProfile | None = None
    solver_finalization: ModelCallProfile | None = None
    solver_reviewer_revision: ModelCallProfile | None = None
    solver_search_review: ModelCallProfile | None = None
    solver_graph_review: ModelCallProfile | None = None
    reviewer: ReviewerProfile
    required_dependency_kind: str | None = Field(default=None, max_length=160)
    graph_review_fetch_tool_name: str | None = Field(default=None, max_length=160)
    automatic_tools: tuple[AutomaticToolProfile, ...] = ()
    tool_list_argument_limits: tuple[ToolListArgumentLimit, ...] = ()
    limits: AgentLimits = Field(default_factory=AgentLimits)

    @model_validator(mode="after")
    def validate_tool_list_argument_limits(self) -> AgentProfile:
        keys = [
            (item.tool_name, item.argument_name)
            for item in self.tool_list_argument_limits
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("tool list argument limits must be unique")
        return self


class ProfileRegistry:
    def __init__(self, profiles: tuple[AgentProfile, ...]):
        self._profiles = {profile.name: profile for profile in profiles}
        if len(self._profiles) != len(profiles):
            raise ValueError("profile names must be unique")

    def resolve(self, name: str) -> AgentProfile:
        try:
            return self._profiles[name]
        except KeyError as exc:
            raise KeyError(f"unknown agent profile: {name}") from exc
