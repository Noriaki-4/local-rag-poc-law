"""検索対象に依存しない、最小の反復型エージェント基盤。"""

from .contracts import (
    CaseUpdate,
    HypothesisUpdate,
    SolverDecision,
    WorkItemImpactDecision,
    WorkItemUpdate,
)
from .loop import AgentLoop
from .profiles import (
    AgentLimits,
    AgentProfile,
    ModelCallProfile,
    ProfileRegistry,
    ReviewerProfile,
)
from .state import (
    CaseState,
    DeferredFrontierResolution,
    Evidence,
    FinalAnswer,
    FrontierReAdoption,
    GraphCandidateReview,
    GraphFrontierDecision,
    Hypothesis,
    ReviewFinding,
    ReviewFindingResolution,
    ReviewResult,
    ToolRequest,
    ToolResult,
    UnreviewedGraphResolution,
    WorkItem,
)
from .store import CaseStore

__all__ = [
    "AgentLimits",
    "AgentLoop",
    "AgentProfile",
    "CaseState",
    "CaseStore",
    "CaseUpdate",
    "DeferredFrontierResolution",
    "Evidence",
    "FinalAnswer",
    "FrontierReAdoption",
    "GraphCandidateReview",
    "GraphFrontierDecision",
    "Hypothesis",
    "HypothesisUpdate",
    "ModelCallProfile",
    "ProfileRegistry",
    "ReviewFinding",
    "ReviewFindingResolution",
    "ReviewResult",
    "ReviewerProfile",
    "SolverDecision",
    "ToolRequest",
    "ToolResult",
    "UnreviewedGraphResolution",
    "WorkItem",
    "WorkItemImpactDecision",
    "WorkItemUpdate",
]
