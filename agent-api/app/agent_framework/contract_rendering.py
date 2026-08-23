"""型付き契約からLLM向けの短い用語集を決定的に生成する。"""

from __future__ import annotations

from functools import lru_cache

from pydantic import BaseModel

from .context import (
    EvidenceManifestItem,
    GraphCandidateLink,
    GraphReviewBatch,
    GraphReviewCandidate,
    GraphReviewLedgerItem,
    SearchCandidateArticle,
    SolverContext,
    SolverContractFeedback,
    SolverToolResult,
    WorkTreeItem,
)
from .contracts import (
    CaseUpdate,
    HypothesisUpdate,
    SolverDecision,
    WorkItemImpactDecision,
    WorkItemUpdate,
)
from .state import (
    DeferredFrontierResolution,
    DependencyDecision,
    Evidence,
    FinalAnswer,
    FrontierReAdoption,
    GraphCandidateReview,
    GraphFrontierDecision,
    Hypothesis,
    ReviewFinding,
    ReviewFindingResolution,
    SearchCandidateReview,
    SearchCandidateSelection,
    ToolRequest,
    UnreviewedGraphResolution,
    WorkItem,
)
from .tool_contracts import ToolDefinition


def _llm_field_names(model_type: type[BaseModel]) -> tuple[str, ...]:
    return tuple(
        name
        for name, field in model_type.model_fields.items()
        if field.exclude is not True
    )


_LLM_VISIBLE_FIELDS: tuple[tuple[str, type[BaseModel], tuple[str, ...]], ...] = (
    ("SolverContext", SolverContext, _llm_field_names(SolverContext)),
    ("SolverDecision", SolverDecision, _llm_field_names(SolverDecision)),
    ("CaseUpdate", CaseUpdate, _llm_field_names(CaseUpdate)),
    ("WorkItem", WorkItem, _llm_field_names(WorkItem)),
    ("WorkItemUpdate", WorkItemUpdate, _llm_field_names(WorkItemUpdate)),
    (
        "WorkItemImpactDecision",
        WorkItemImpactDecision,
        _llm_field_names(WorkItemImpactDecision),
    ),
    ("Hypothesis", Hypothesis, _llm_field_names(Hypothesis)),
    ("HypothesisUpdate", HypothesisUpdate, _llm_field_names(HypothesisUpdate)),
    ("ToolRequest", ToolRequest, _llm_field_names(ToolRequest)),
    ("ToolDefinition", ToolDefinition, _llm_field_names(ToolDefinition)),
    ("DependencyDecision", DependencyDecision, _llm_field_names(DependencyDecision)),
    ("WorkTreeItem", WorkTreeItem, _llm_field_names(WorkTreeItem)),
    (
        "EvidenceManifestItem",
        EvidenceManifestItem,
        _llm_field_names(EvidenceManifestItem),
    ),
    ("Evidence", Evidence, _llm_field_names(Evidence)),
    (
        "SearchCandidateArticle",
        SearchCandidateArticle,
        _llm_field_names(SearchCandidateArticle),
    ),
    (
        "SearchCandidateSelection",
        SearchCandidateSelection,
        _llm_field_names(SearchCandidateSelection),
    ),
    (
        "SearchCandidateReview",
        SearchCandidateReview,
        _llm_field_names(SearchCandidateReview),
    ),
    ("GraphCandidateLink", GraphCandidateLink, _llm_field_names(GraphCandidateLink)),
    (
        "GraphReviewCandidate",
        GraphReviewCandidate,
        _llm_field_names(GraphReviewCandidate),
    ),
    ("GraphReviewBatch", GraphReviewBatch, _llm_field_names(GraphReviewBatch)),
    (
        "GraphReviewLedgerItem",
        GraphReviewLedgerItem,
        _llm_field_names(GraphReviewLedgerItem),
    ),
    (
        "GraphFrontierDecision",
        GraphFrontierDecision,
        _llm_field_names(GraphFrontierDecision),
    ),
    (
        "GraphCandidateReview",
        GraphCandidateReview,
        _llm_field_names(GraphCandidateReview),
    ),
    (
        "FrontierReAdoption",
        FrontierReAdoption,
        _llm_field_names(FrontierReAdoption),
    ),
    (
        "DeferredFrontierResolution",
        DeferredFrontierResolution,
        _llm_field_names(DeferredFrontierResolution),
    ),
    (
        "UnreviewedGraphResolution",
        UnreviewedGraphResolution,
        _llm_field_names(UnreviewedGraphResolution),
    ),
    ("SolverToolResult", SolverToolResult, _llm_field_names(SolverToolResult)),
    ("ReviewFinding", ReviewFinding, _llm_field_names(ReviewFinding)),
    (
        "ReviewFindingResolution",
        ReviewFindingResolution,
        _llm_field_names(ReviewFindingResolution),
    ),
    (
        "SolverContractFeedback",
        SolverContractFeedback,
        _llm_field_names(SolverContractFeedback),
    ),
    ("FinalAnswer", FinalAnswer, _llm_field_names(FinalAnswer)),
)


@lru_cache(maxsize=1)
def render_solver_contract_glossary() -> str:
    """Pydantic Field.descriptionを正本に、Prompt用語集を生成する。"""

    lines = [
        "<contract_glossary>",
        "以下は正規契約の項目名と意味です。Provider輸送用の別表現ではなく、判断はこの意味で行います。",
    ]
    for model_name, model_type, field_names in _LLM_VISIBLE_FIELDS:
        lines.append(f"### {model_name}")
        for field_name in field_names:
            field = model_type.model_fields[field_name]
            description = (field.description or "").strip()
            if not description:
                raise ValueError(
                    f"LLM-visible contract field lacks description: "
                    f"{model_name}.{field_name}"
                )
            lines.append(f"- `{model_name}.{field_name}`: {description}")
    lines.append("</contract_glossary>")
    return "\n".join(lines)


def contract_field_description(
    model_type: type[BaseModel],
    field_name: str,
) -> str:
    """輸送Schemaも同じField.descriptionを利用するための入口。"""

    description = (model_type.model_fields[field_name].description or "").strip()
    if not description:
        raise ValueError(
            f"contract field lacks description: {model_type.__name__}.{field_name}"
        )
    return description
