"""Provider非依存のSolver・Reviewer model契約。"""

from __future__ import annotations

from typing import Protocol

from pydantic import Field

from ..context import SolverContext
from ..contracts import SolverDecision
from ..profiles import ModelCallProfile, ReviewerProfile
from ..state import (
    Evidence,
    FinalAnswer,
    FrameworkModel,
    ReviewResult,
)


class ModelProtocolError(ValueError):
    """Model応答が構造化契約を満たさない。"""


class SolverCallResult(FrameworkModel):
    decision: SolverDecision
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    attempt_count: int = Field(default=1, ge=1)


class ReviewContext(FrameworkModel):
    question: str
    answer: FinalAnswer
    evidence: tuple[Evidence, ...]


class ReviewCallResult(FrameworkModel):
    review: ReviewResult
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    attempt_count: int = Field(default=1, ge=1)


class ModelPort(Protocol):
    def solve(
        self,
        context: SolverContext,
        profile: ModelCallProfile,
    ) -> SolverCallResult: ...

    def review(
        self,
        context: ReviewContext,
        profile: ReviewerProfile,
    ) -> ReviewCallResult: ...
