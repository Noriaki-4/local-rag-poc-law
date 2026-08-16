"""Provider・保存先・法令Domainに依存しない反復型Agent Core。"""

from .store import CaseSnapshot, CaseStore
from .projection_policy import ProjectionPolicy
from .projector import MaterialItem, MaterialProjection, Projector
from .transactions import (
    ActionCompletion,
    DecisionDelta,
    PlanDelta,
    apply_decision,
    apply_plan_delta,
    claim_action,
    complete_action,
    register_external_artifact,
)

__all__ = [
    "ActionCompletion",
    "CaseSnapshot",
    "CaseStore",
    "DecisionDelta",
    "PlanDelta",
    "ProjectionPolicy",
    "Projector",
    "MaterialItem",
    "MaterialProjection",
    "apply_decision",
    "apply_plan_delta",
    "claim_action",
    "complete_action",
    "register_external_artifact",
]
