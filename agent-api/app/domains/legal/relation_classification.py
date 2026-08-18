"""非同期法令関係分類の入出力と決定的な構造検証。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from .graph_schema import ClassificationRunPhase, ProposedPredicate


class LegalGraphModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class ArticleSpan(LegalGraphModel):
    span_id: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1)


class ClassificationArticle(LegalGraphModel):
    article_id: str = Field(min_length=1, max_length=500)
    document_id: str = Field(min_length=1, max_length=500)
    content_hash: str = Field(min_length=1, max_length=128)
    source_revision_id: str | None = Field(default=None, max_length=500)
    authority_type: str | None = Field(default=None, max_length=160)
    law_family_id: str | None = Field(default=None, max_length=500)
    spans: tuple[ArticleSpan, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_spans(self) -> ClassificationArticle:
        span_ids = [span.span_id for span in self.spans]
        if len(span_ids) != len(set(span_ids)):
            raise ValueError("article span IDs must be unique")
        prefix = f"{self.article_id}::"
        if any(not span_id.startswith(prefix) for span_id in span_ids):
            raise ValueError("article span ID must be namespaced by article ID")
        return self

    def span(self, span_id: str) -> ArticleSpan | None:
        return next((span for span in self.spans if span.span_id == span_id), None)


class ReferenceOccurrence(LegalGraphModel):
    occurrence_hash: str = Field(min_length=1, max_length=128)
    citation_text: str = Field(min_length=1)
    source_content_unit_id: str = Field(min_length=1, max_length=500)
    source_span_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_span_ids(self) -> ReferenceOccurrence:
        if len(self.source_span_ids) != len(set(self.source_span_ids)):
            raise ValueError("reference source span IDs must be unique")
        return self


class RelationClassificationCandidate(LegalGraphModel):
    """1組のArticle端点と1本の原文Relationを分類する入力。"""

    source_snapshot_id: str = Field(min_length=1, max_length=500)
    graph_schema_version: int = Field(ge=1)
    prompt_version: str = Field(min_length=1, max_length=160)
    model: str = Field(min_length=1, max_length=300)
    basis_edge_id: str = Field(min_length=1, max_length=500)
    subject: ClassificationArticle
    object: ClassificationArticle
    reference_occurrences: tuple[ReferenceOccurrence, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_candidate(self) -> RelationClassificationCandidate:
        if self.subject.article_id == self.object.article_id:
            raise ValueError("classification endpoints must be different Articles")
        hashes = [item.occurrence_hash for item in self.reference_occurrences]
        if len(hashes) != len(set(hashes)):
            raise ValueError("reference occurrence hashes must be unique")
        subject_span_ids = {span.span_id for span in self.subject.spans}
        object_span_ids = {span.span_id for span in self.object.spans}
        for occurrence in self.reference_occurrences:
            source_span_ids = set(occurrence.source_span_ids)
            if not (
                source_span_ids.issubset(subject_span_ids)
                or source_span_ids.issubset(object_span_ids)
            ):
                raise ValueError(
                    "reference occurrence spans must belong to one endpoint Article"
                )
        return self

    @property
    def candidate_key(self) -> str:
        return stable_hash(
            {
                "sourceSnapshotId": self.source_snapshot_id,
                "graphSchemaVersion": self.graph_schema_version,
                "promptVersion": self.prompt_version,
                "model": self.model,
                "basisEdgeId": self.basis_edge_id,
                "subjectArticleId": self.subject.article_id,
                "subjectContentHash": self.subject.content_hash,
                "objectArticleId": self.object.article_id,
                "objectContentHash": self.object.content_hash,
                "referenceOccurrenceHashes": sorted(
                    item.occurrence_hash for item in self.reference_occurrences
                ),
            }
        )


class ProposedRelationAssertion(LegalGraphModel):
    proposed_predicate: ProposedPredicate
    reference_occurrence_hash: str = Field(min_length=1, max_length=128)
    subject_supporting_span_id: str = Field(min_length=1, max_length=500)
    object_supporting_span_id: str = Field(min_length=1, max_length=500)


class RelationClassificationDecision(LegalGraphModel):
    candidate_key: str = Field(min_length=1, max_length=128)
    outcome: Literal["classified", "reference_only", "uncertain"]
    assertions: tuple[ProposedRelationAssertion, ...] = ()

    @model_validator(mode="after")
    def validate_outcome_shape(self) -> RelationClassificationDecision:
        if self.outcome == "classified" and not self.assertions:
            raise ValueError("classified outcome requires at least one assertion")
        if self.outcome != "classified" and self.assertions:
            raise ValueError("only classified outcome may contain assertions")
        predicates = [item.proposed_predicate for item in self.assertions]
        if len(predicates) != len(set(predicates)):
            raise ValueError("a candidate may contain each predicate only once")
        return self


class RelationAssertionRecord(LegalGraphModel):
    assertion_id: str = Field(min_length=1, max_length=500)
    candidate_key: str = Field(min_length=1, max_length=128)
    assertion_dedupe_key: str = Field(min_length=1, max_length=128)
    proposed_predicate: ProposedPredicate
    basis_edge_id: str = Field(min_length=1, max_length=500)
    source_content_unit_id: str = Field(min_length=1, max_length=500)
    subject_article_id: str = Field(min_length=1, max_length=500)
    object_article_id: str = Field(min_length=1, max_length=500)
    subject_supporting_span_id: str = Field(min_length=1, max_length=500)
    object_supporting_span_id: str = Field(min_length=1, max_length=500)
    subject_supporting_quote: str = Field(min_length=1)
    object_supporting_quote: str = Field(min_length=1)
    reference_occurrence_hash: str = Field(min_length=1, max_length=128)
    source_snapshot_id: str = Field(min_length=1, max_length=500)
    source_revision_id: str | None = Field(default=None, max_length=500)
    classification_run_id: str = Field(min_length=1, max_length=500)
    classified_at: datetime
    graph_schema_version: int = Field(ge=1)


class ClassificationRunRecord(LegalGraphModel):
    classification_run_id: str = Field(min_length=1, max_length=500)
    phase: ClassificationRunPhase
    source_snapshot_id: str = Field(min_length=1, max_length=500)
    graph_schema_version: int = Field(ge=1)
    model: str = Field(min_length=1, max_length=300)
    prompt_version: str = Field(min_length=1, max_length=160)
    candidates_per_model_call: int = Field(ge=1)
    input_count: int = Field(ge=0)
    processed_count: int = Field(ge=0)
    assertion_count: int = Field(ge=0)
    reference_only_count: int = Field(ge=0)
    uncertain_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    scope_hash: str = Field(min_length=1, max_length=128)
    published_at: datetime | None = None

    @model_validator(mode="after")
    def validate_phase_and_counts(self) -> ClassificationRunRecord:
        if self.processed_count > self.input_count:
            raise ValueError("processed count cannot exceed input count")
        if self.phase == ClassificationRunPhase.PUBLISHED:
            if self.processed_count != self.input_count:
                raise ValueError("published run must process its complete input scope")
            if self.published_at is None:
                raise ValueError("published run requires publishedAt")
        elif self.published_at is not None:
            raise ValueError("only published run may have publishedAt")
        return self


def stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def assertion_dedupe_key(
    classification_run_id: str,
    candidate_key: str,
    proposed_predicate: ProposedPredicate,
) -> str:
    return stable_hash(
        {
            "classificationRunId": classification_run_id,
            "candidateKey": candidate_key,
            "proposedPredicate": proposed_predicate.value,
        }
    )


def validate_classification_decision(
    candidate: RelationClassificationCandidate,
    decision: RelationClassificationDecision,
) -> None:
    """既知IDと端点だけを検査し、predicateの意味は補正しない。"""

    if decision.candidate_key != candidate.candidate_key:
        raise ValueError("classification decision references an unknown candidate key")
    known_occurrences = {
        occurrence.occurrence_hash for occurrence in candidate.reference_occurrences
    }
    for assertion in decision.assertions:
        if assertion.reference_occurrence_hash not in known_occurrences:
            raise ValueError("assertion references an unknown reference occurrence")
        if candidate.subject.span(assertion.subject_supporting_span_id) is None:
            raise ValueError("subject supporting span does not belong to subject Article")
        if candidate.object.span(assertion.object_supporting_span_id) is None:
            raise ValueError("object supporting span does not belong to object Article")


def build_assertion_records(
    candidate: RelationClassificationCandidate,
    decision: RelationClassificationDecision,
    *,
    classification_run_id: str,
    classified_at: datetime,
) -> tuple[RelationAssertionRecord, ...]:
    """検証済みLLM出力を保存形へ写す。predicateの選択・補正はしない。"""

    validate_classification_decision(candidate, decision)
    occurrences = {
        item.occurrence_hash: item for item in candidate.reference_occurrences
    }
    records: list[RelationAssertionRecord] = []
    for proposed in decision.assertions:
        occurrence = occurrences[proposed.reference_occurrence_hash]
        source_article = _source_article_for_occurrence(candidate, occurrence)
        subject_span = candidate.subject.span(proposed.subject_supporting_span_id)
        object_span = candidate.object.span(proposed.object_supporting_span_id)
        if subject_span is None or object_span is None:
            raise ValueError("validated supporting span is unavailable")
        dedupe_key = assertion_dedupe_key(
            classification_run_id,
            candidate.candidate_key,
            proposed.proposed_predicate,
        )
        records.append(
            RelationAssertionRecord(
                assertion_id=f"relation-assertion-{dedupe_key}",
                candidate_key=candidate.candidate_key,
                assertion_dedupe_key=dedupe_key,
                proposed_predicate=proposed.proposed_predicate,
                basis_edge_id=candidate.basis_edge_id,
                source_content_unit_id=occurrence.source_content_unit_id,
                subject_article_id=candidate.subject.article_id,
                object_article_id=candidate.object.article_id,
                subject_supporting_span_id=proposed.subject_supporting_span_id,
                object_supporting_span_id=proposed.object_supporting_span_id,
                subject_supporting_quote=subject_span.text,
                object_supporting_quote=object_span.text,
                reference_occurrence_hash=occurrence.occurrence_hash,
                source_snapshot_id=candidate.source_snapshot_id,
                source_revision_id=source_article.source_revision_id,
                classification_run_id=classification_run_id,
                classified_at=classified_at,
                graph_schema_version=candidate.graph_schema_version,
            )
        )
    return tuple(records)


def _source_article_for_occurrence(
    candidate: RelationClassificationCandidate,
    occurrence: ReferenceOccurrence,
) -> ClassificationArticle:
    source_ids = set(occurrence.source_span_ids)
    for article in (candidate.subject, candidate.object):
        article_span_ids = {span.span_id for span in article.spans}
        if source_ids.issubset(article_span_ids):
            return article
    raise ValueError("reference occurrence spans must belong to one endpoint Article")


__all__ = [
    "ArticleSpan",
    "ClassificationArticle",
    "ClassificationRunRecord",
    "LegalGraphModel",
    "ProposedRelationAssertion",
    "ReferenceOccurrence",
    "RelationAssertionRecord",
    "RelationClassificationCandidate",
    "RelationClassificationDecision",
    "assertion_dedupe_key",
    "build_assertion_records",
    "stable_hash",
    "validate_classification_decision",
]
