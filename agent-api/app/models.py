from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Pattern = Literal[
    "pattern_1_baseline_rag",
    "pattern_2_rule_based_agentic_rag",
    "pattern_3_controlled_agentic_rag",
    "pattern_4_deepsearch_partial",
    "pattern_4_deepsearch",
]


class SearchRequest(BaseModel):
    query: str
    docType: str | None = None
    topK: int = Field(default=5, ge=1, le=20)
    userClearanceLevel: int = Field(default=2, ge=1, le=3)
    useBm25: bool = True
    useVector: bool = True


class AnswerRequest(BaseModel):
    question: str
    choices: dict[str, str] | None = Field(
        default=None,
        description=(
            "任意の回答候補。キーは候補ID、値は未確認の候補本文。"
            "正解や採点情報は含めない。"
        ),
    )
    pattern: Pattern = "pattern_2_rule_based_agentic_rag"
    userClearanceLevel: int = Field(default=2, ge=1, le=3)
    topK: int = Field(default=5, ge=1, le=20)
    candidateTopK: int | None = Field(default=None, ge=5, le=100)
    rerankTopK: int | None = Field(default=None, ge=1, le=50)

    @model_validator(mode="after")
    def validate_top_k_order(self):
        if self.choices is not None:
            normalized = {
                option_id.strip(): text.strip()
                for option_id, text in self.choices.items()
            }
            if not normalized or any(not key or not value for key, value in normalized.items()):
                raise ValueError("choices must contain non-empty option IDs and text")
            self.choices = normalized
        if self.candidateTopK is not None and self.candidateTopK < self.topK:
            raise ValueError("candidateTopK must be greater than or equal to topK")
        if self.rerankTopK is not None and self.rerankTopK < self.topK:
            raise ValueError("rerankTopK must be greater than or equal to topK")
        if (
            self.candidateTopK is not None
            and self.rerankTopK is not None
            and self.rerankTopK > self.candidateTopK
        ):
            raise ValueError("rerankTopK must be less than or equal to candidateTopK")
        return self


class GraphPathRequest(BaseModel):
    fromGraphNodeId: str
    edgeType: str | None = None
    maxDepth: int = Field(default=2, ge=1, le=3)
    userClearanceLevel: int = Field(default=2, ge=1, le=3)


class Citation(BaseModel):
    documentId: str
    contentUnitId: str | None = None
    title: str | None = None
    heading: str | None = None
    sourceObjectUri: str | None = None
    sourcePage: int | None = None
    text: str | None = None
    # 法令本文かガイド(行政解釈)かを回答・UIで区別するためのレーン表示
    # (docs/layered_legal_evidence_retrieval_plan.md §10)。
    evidenceLane: str | None = None
    evidenceRole: str | None = None


class AnswerResponse(BaseModel):
    pattern: str
    route: list[str]
    answer: str
    predictedAnswer: str | None = None
    choiceJudgements: dict[str, str | None] | None = None
    citations: list[Citation]
    graphPaths: list[dict[str, Any]]
    trace: dict[str, Any]


class QuestionReadinessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(
        min_length=1,
        max_length=4000,
        description=(
            "利用者が入力した確認前の質問。質問に書かれていない事実を補わず、"
            "この本文だけから調査開始可能性を判断する。"
        ),
    )


class QuestionReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: Literal["ready", "clarification_recommended"] = Field(
        description=(
            "readyは一つの主たる主体と、検索対象を特定できる行為を原文から"
            "読み取って調査を開始でき、検索向けの質問文を提案できることを示す。"
            "clarification_recommendedは、"
            "主体、行為若しくは行為対象の欠落・曖昧さ、又は独立した検索単位の"
            "混在により、質問文の明確化が検索精度の向上に役立つことを示す。"
        )
    )
    reason: str = Field(
        min_length=1,
        max_length=1000,
        description=(
            "判断理由。clarification_recommendedでは、何が不足又は混在し、検索対象を"
            "どう変え得るかを法的結論を断定せず説明する。通常UIには表示せず、"
            "診断又はtraceで確認する。"
        ),
    )
    recommendation: str = Field(
        min_length=1,
        max_length=4000,
        description=(
            "readyでは、原文の意味と確認事項を保ち、検索対象を明確にした質問文案。"
            "clarification_recommendedでは、質問文で不足又は曖昧な最も重要な一点を"
            "示す短い指摘文。どちらも利用者への質問にはしない。"
        ),
    )


class FrameworkAuditRequest(BaseModel):
    caseId: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    inquiry: str = Field(
        default="この判断を選んだ理由を、記録された根拠に基づいて説明してください。",
        min_length=1,
        max_length=1000,
    )
    decisionSequence: int | None = Field(default=None, ge=1)


class FrameworkAuditResponse(BaseModel):
    caseId: str
    decisionSequence: int
    recordedDecisionReason: str
    explanation: str
    recordedFacts: list[str]
    inferences: list[str]
    sourceDecisionSequences: list[int]
    limitations: list[str]
    model: str
    inputTokens: int | None = None
    outputTokens: int | None = None
