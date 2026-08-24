"""Provider非依存のSolver・Reviewer model契約。"""

from __future__ import annotations

from typing import Protocol

from pydantic import Field

from ..context import SolverContext
from ..contracts import SolverDecision
from ..profiles import ModelCallProfile, ReviewerProfile
from ..state import (
    DependencyDecision,
    Evidence,
    FinalAnswer,
    FrameworkModel,
    Hypothesis,
    ReviewResult,
    WorkItem,
)


class ModelProtocolError(ValueError):
    """Model応答が構造化契約を満たさない。"""


class SolverCheckpointTimeout(TimeoutError):
    """完了済みの意味更新を保持したまま、後続Model処理が時間切れになった。"""

    def __init__(
        self,
        message: str,
        *,
        partial_decision: SolverDecision,
        completed_stage: str,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        super().__init__(message)
        self.partial_decision = partial_decision
        self.completed_stage = completed_stage
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class SolverCallResult(FrameworkModel):
    decision: SolverDecision
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    attempt_count: int = Field(default=1, ge=1)


class ReviewerView(FrameworkModel):
    case_id: str
    question: str
    answer: FinalAnswer
    work_items: tuple[WorkItem, ...]
    hypotheses: tuple[Hypothesis, ...]
    dependency_decisions: tuple[DependencyDecision, ...]
    evidence: tuple[Evidence, ...]


# 移行中の呼出し側との互換名。新規コードはReviewerViewを使う。
ReviewContext = ReviewerView


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
        context: ReviewerView,
        profile: ReviewerProfile,
    ) -> ReviewCallResult: ...
