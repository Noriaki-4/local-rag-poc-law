from typing import Any, Literal

from pydantic import BaseModel, Field


Pattern = Literal[
    "pattern_1_baseline_rag",
    "pattern_2_rule_based_agentic_rag",
    "pattern_3_controlled_agentic_rag",
    "pattern_4_deepsearch_partial",
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
    choices: dict[str, str] | None = None
    pattern: Pattern = "pattern_2_rule_based_agentic_rag"
    userClearanceLevel: int = Field(default=2, ge=1, le=3)
    topK: int = Field(default=5, ge=1, le=20)


class GraphPathRequest(BaseModel):
    fromGraphNodeId: str
    edgeType: str | None = None
    maxDepth: int = Field(default=2, ge=1, le=3)


class Citation(BaseModel):
    documentId: str
    contentUnitId: str | None = None
    title: str | None = None
    heading: str | None = None
    sourceObjectUri: str | None = None
    sourcePage: int | None = None
    text: str | None = None


class AnswerResponse(BaseModel):
    pattern: str
    route: list[str]
    answer: str
    predictedAnswer: str | None = None
    choiceJudgements: dict[str, str | None] | None = None
    citations: list[Citation]
    graphPaths: list[dict[str, Any]]
    trace: dict[str, Any]
