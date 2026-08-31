from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

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
