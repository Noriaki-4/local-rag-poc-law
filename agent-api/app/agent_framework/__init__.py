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
    Evidence,
    FinalAnswer,
    FrontierReAdoption,
    GraphCandidateReview,
    GraphFrontierDecision,
    Hypothesis,
    ReviewFinding,
    ReviewResult,
    ToolRequest,
    ToolResult,
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
    "ReviewResult",
    "ReviewerProfile",
    "SolverDecision",
    "ToolRequest",
    "ToolResult",
    "WorkItem",
    "WorkItemImpactDecision",
    "WorkItemUpdate",
]
