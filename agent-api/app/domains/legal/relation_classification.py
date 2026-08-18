"""非同期法令関係分類の入出力と決定的な構造検証。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from .graph_schema import (
    ClassificationCheckpointOutcome,
    ClassificationRunPhase,
    PredicateFinding,
    ProposedPredicate,
    RelationClassificationOutcome,
)


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
    """1組のArticle端点と1本の原文Relationを分類する入力。

    `reference_source / reference_target`は原文REFERENCESの物理方向であり、
    法的意味上のSUBJECT / OBJECTではない。意味方向はLLM出力で選択する。
    """

    source_snapshot_id: str = Field(min_length=1, max_length=500)
    graph_schema_version: int = Field(ge=1)
    prompt_version: str = Field(min_length=1, max_length=160)
    provider: str = Field(min_length=1, max_length=160)
    model: str = Field(min_length=1, max_length=300)
    reviewer_model: str | None = Field(default=None, max_length=300)
    basis_edge_id: str = Field(min_length=1, max_length=500)
    reference_source: ClassificationArticle
    reference_target: ClassificationArticle
    reference_occurrences: tuple[ReferenceOccurrence, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_candidate(self) -> RelationClassificationCandidate:
        if self.reference_source.article_id == self.reference_target.article_id:
            raise ValueError("classification endpoints must be different Articles")
        hashes = [item.occurrence_hash for item in self.reference_occurrences]
        if len(hashes) != len(set(hashes)):
            raise ValueError("reference occurrence hashes must be unique")
        source_span_ids = {span.span_id for span in self.reference_source.spans}
        for occurrence in self.reference_occurrences:
            occurrence_span_ids = set(occurrence.source_span_ids)
            if not occurrence_span_ids.issubset(source_span_ids):
                raise ValueError(
                    "reference occurrence spans must belong to reference source Article"
                )
        return self

    @property
    def candidate_key(self) -> str:
        return stable_hash(
            {
                "sourceSnapshotId": self.source_snapshot_id,
                "graphSchemaVersion": self.graph_schema_version,
                "promptVersion": self.prompt_version,
                "provider": self.provider,
                "model": self.model,
                "reviewerModel": self.reviewer_model,
                "basisEdgeId": self.basis_edge_id,
                "referenceSourceArticleId": self.reference_source.article_id,
                "referenceSourceContentHash": self.reference_source.content_hash,
                "referenceTargetArticleId": self.reference_target.article_id,
                "referenceTargetContentHash": self.reference_target.content_hash,
                "referenceOccurrenceHashes": sorted(
                    item.occurrence_hash for item in self.reference_occurrences
                ),
            }
        )


class ProposedRelationAssertion(LegalGraphModel):
    proposed_predicate: ProposedPredicate
    reference_occurrence_hash: str = Field(min_length=1, max_length=128)
    subject_article_id: str = Field(min_length=1, max_length=500)
    object_article_id: str = Field(min_length=1, max_length=500)
    subject_supporting_span_id: str = Field(min_length=1, max_length=500)
    object_supporting_span_id: str = Field(min_length=1, max_length=500)


class PredicateFindings(LegalGraphModel):
    """5 predicateを独立に評価したLLM判断。"""

    implements: PredicateFinding = Field(
        description=(
            "親規定の明示的な下位法令委任と、下位規定による同一事項の具体化を"
            "両本文で確認できる場合だけestablished"
        )
    )
    incorporates: PredicateFinding = Field(
        description=(
            "SUBJECTがOBJECTの規律を準用、読替え又は参照によって自らの規律へ"
            "実際に取り込む場合だけestablished"
        )
    )
    uses_definition: PredicateFinding = Field(
        description=(
            "OBJECTが語の意味又は範囲を定義し、SUBJECTがその同じ定義語を使う場合だけ"
            "established。OBJECTが権利、義務、要件又は手続を定めるだけならnot_established"
        )
    )
    exception_to: PredicateFinding = Field(
        description=(
            "OBJECT自体が一般規定で、SUBJECTがOBJECTの適用に対する例外又は適用除外を"
            "定める場合だけestablished。OBJECTを例外対象の定義元として引用するだけなら"
            "not_established"
        )
    )
    overrides: PredicateFinding = Field(
        description=(
            "SUBJECTがOBJECTより優先してOBJECTの適用内容を排除又は修正する場合だけ"
            "established"
        )
    )

    def established_predicates(self) -> set[ProposedPredicate]:
        return {
            predicate
            for predicate, finding in self._by_predicate().items()
            if finding is PredicateFinding.ESTABLISHED
        }

    def uncertain_predicates(self) -> set[ProposedPredicate]:
        return {
            predicate
            for predicate, finding in self._by_predicate().items()
            if finding is PredicateFinding.UNCERTAIN
        }

    def _by_predicate(self) -> dict[ProposedPredicate, PredicateFinding]:
        return {
            ProposedPredicate.IMPLEMENTS: self.implements,
            ProposedPredicate.INCORPORATES: self.incorporates,
            ProposedPredicate.USES_DEFINITION: self.uses_definition,
            ProposedPredicate.EXCEPTION_TO: self.exception_to,
            ProposedPredicate.OVERRIDES: self.overrides,
        }


def _validate_two_conditions(
    finding: PredicateFinding,
    first: PredicateFinding,
    second: PredicateFinding,
) -> None:
    conditions = (first, second)
    if all(value is PredicateFinding.ESTABLISHED for value in conditions):
        expected = PredicateFinding.ESTABLISHED
    elif any(value is PredicateFinding.NOT_ESTABLISHED for value in conditions):
        expected = PredicateFinding.NOT_ESTABLISHED
    else:
        expected = PredicateFinding.UNCERTAIN
    if finding is not expected:
        raise ValueError(
            "predicate finding must match its two LLM-evaluated necessary conditions"
        )


class RelationClassificationResponse(LegalGraphModel):
    """predicate固有Provider応答を正規化した保存用の意味判断。"""

    candidate_key: str = Field(min_length=1, max_length=128)
    predicate: ProposedPredicate
    first_condition_name: str = Field(min_length=1, max_length=160)
    first_condition: PredicateFinding
    second_condition_name: str = Field(min_length=1, max_length=160)
    second_condition: PredicateFinding
    finding: PredicateFinding

    @model_validator(mode="after")
    def validate_conditions(self) -> RelationClassificationResponse:
        _validate_two_conditions(
            self.finding,
            self.first_condition,
            self.second_condition,
        )
        return self


class ImplementsClassificationResponse(LegalGraphModel):
    candidate_key: str = Field(min_length=1, max_length=128)
    predicate: ProposedPredicate
    explicit_delegation: PredicateFinding
    same_matter_implementation: PredicateFinding
    finding: PredicateFinding

    @model_validator(mode="after")
    def validate_conditions(self) -> ImplementsClassificationResponse:
        _validate_two_conditions(
            self.finding,
            self.explicit_delegation,
            self.same_matter_implementation,
        )
        return self


class IncorporatesClassificationResponse(LegalGraphModel):
    candidate_key: str = Field(min_length=1, max_length=128)
    predicate: ProposedPredicate
    explicit_application_language: PredicateFinding
    target_rule_applied: PredicateFinding
    finding: PredicateFinding

    @model_validator(mode="after")
    def validate_conditions(self) -> IncorporatesClassificationResponse:
        _validate_two_conditions(
            self.finding,
            self.explicit_application_language,
            self.target_rule_applied,
        )
        return self


class UsesDefinitionClassificationResponse(LegalGraphModel):
    candidate_key: str = Field(min_length=1, max_length=128)
    predicate: ProposedPredicate
    target_defines_term: PredicateFinding
    source_uses_same_term: PredicateFinding
    finding: PredicateFinding

    @model_validator(mode="after")
    def validate_conditions(self) -> UsesDefinitionClassificationResponse:
        _validate_two_conditions(
            self.finding,
            self.target_defines_term,
            self.source_uses_same_term,
        )
        return self


class ExceptionToClassificationResponse(LegalGraphModel):
    candidate_key: str = Field(min_length=1, max_length=128)
    predicate: ProposedPredicate
    target_contains_affected_rule: PredicateFinding
    citation_directly_limits_target_rule: PredicateFinding
    finding: PredicateFinding

    @model_validator(mode="after")
    def validate_conditions(self) -> ExceptionToClassificationResponse:
        _validate_two_conditions(
            self.finding,
            self.target_contains_affected_rule,
            self.citation_directly_limits_target_rule,
        )
        return self


class OverridesClassificationResponse(LegalGraphModel):
    candidate_key: str = Field(min_length=1, max_length=128)
    predicate: ProposedPredicate
    explicit_priority_over_target: PredicateFinding
    target_application_modified: PredicateFinding
    finding: PredicateFinding

    @model_validator(mode="after")
    def validate_conditions(self) -> OverridesClassificationResponse:
        _validate_two_conditions(
            self.finding,
            self.explicit_priority_over_target,
            self.target_application_modified,
        )
        return self


class RelationGroundingResponse(LegalGraphModel):
    """第二段階で成立関係へ既知IDを割り当てるLLM応答。"""

    candidate_key: str = Field(min_length=1, max_length=128)
    assertions: tuple[ProposedRelationAssertion, ...] = Field()

    @model_validator(mode="after")
    def validate_assertions(self) -> RelationGroundingResponse:
        predicates = [item.proposed_predicate for item in self.assertions]
        if len(predicates) != len(set(predicates)):
            raise ValueError("each established predicate requires one grounding")
        return self


class RelationClassificationDecision(LegalGraphModel):
    candidate_key: str = Field(min_length=1, max_length=128)
    outcome: RelationClassificationOutcome = Field(
        description=(
            "一つ以上establishedならclassified、全てnot_establishedならreference_only、"
            "establishedがなく一つ以上uncertainならuncertain"
        )
    )
    predicate_findings: PredicateFindings = Field(
        description="提示された二つのArticle間について5 predicateを個別に評価した結果"
    )
    meaning_assessments: tuple[RelationClassificationResponse, ...] = ()
    assertions: tuple[ProposedRelationAssertion, ...] = ()

    @model_validator(mode="after")
    def validate_outcome_shape(self) -> RelationClassificationDecision:
        assessed_predicates = [item.predicate for item in self.meaning_assessments]
        if len(assessed_predicates) != len(set(assessed_predicates)):
            raise ValueError("each predicate may have only one meaning assessment")
        if self.meaning_assessments:
            if set(assessed_predicates) != set(ProposedPredicate):
                raise ValueError("meaning assessments must cover all predicates")
            if any(
                item.candidate_key != self.candidate_key
                for item in self.meaning_assessments
            ):
                raise ValueError("meaning assessment candidate key mismatch")
            assessed_findings = {
                item.predicate: item.finding for item in self.meaning_assessments
            }
            if assessed_findings != self.predicate_findings._by_predicate():
                raise ValueError("meaning assessments must match predicate findings")
        if (
            self.outcome is RelationClassificationOutcome.CLASSIFIED
            and not self.assertions
        ):
            raise ValueError("classified outcome requires at least one assertion")
        if (
            self.outcome is not RelationClassificationOutcome.CLASSIFIED
            and self.assertions
        ):
            raise ValueError("only classified outcome may contain assertions")
        predicates = [item.proposed_predicate for item in self.assertions]
        if len(predicates) != len(set(predicates)):
            raise ValueError("a candidate may contain each predicate only once")
        asserted = set(predicates)
        established = self.predicate_findings.established_predicates()
        uncertain = self.predicate_findings.uncertain_predicates()
        if self.outcome is RelationClassificationOutcome.CLASSIFIED:
            if asserted != established:
                raise ValueError(
                    "classified assertions must exactly match established predicate findings"
                )
        elif self.outcome is RelationClassificationOutcome.REFERENCE_ONLY:
            if established or uncertain:
                raise ValueError(
                    "reference_only requires every predicate to be not_established"
                )
        elif established or not uncertain:
            raise ValueError(
                "uncertain requires no established predicate and at least one uncertain finding"
            )
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
    provider: str = Field(min_length=1, max_length=160)
    model: str = Field(min_length=1, max_length=300)
    reviewer_model: str | None = Field(default=None, max_length=300)
    prompt_version: str = Field(min_length=1, max_length=160)
    candidates_per_model_call: int = Field(ge=1)
    input_count: int = Field(ge=0)
    processed_count: int = Field(ge=0)
    classified_candidate_count: int = Field(ge=0)
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
        if self.processed_count != (
            self.classified_candidate_count
            + self.reference_only_count
            + self.uncertain_count
            + self.failed_count
        ):
            raise ValueError("processed count must equal candidate outcome counts")
        if self.phase == ClassificationRunPhase.PUBLISHED:
            if self.processed_count != self.input_count:
                raise ValueError("published run must process its complete input scope")
            if self.published_at is None:
                raise ValueError("published run requires publishedAt")
        elif self.published_at is not None:
            raise ValueError("only published run may have publishedAt")
        return self


class ClassificationCheckpointRecord(LegalGraphModel):
    """1候補の保存済み結果。中断再開の正本であり、法的意味Edgeではない。"""

    checkpoint_id: str = Field(min_length=1, max_length=500)
    classification_run_id: str = Field(min_length=1, max_length=500)
    candidate_key: str = Field(min_length=1, max_length=128)
    outcome: ClassificationCheckpointOutcome
    decision_payload_hash: str = Field(min_length=1, max_length=128)
    decision_payload_json: str = Field(min_length=2)
    assertion_count: int = Field(ge=0)
    error_code: str | None = Field(default=None, max_length=160)
    processed_at: datetime
    source_snapshot_id: str = Field(min_length=1, max_length=500)
    graph_schema_version: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_outcome(self) -> ClassificationCheckpointRecord:
        if (
            self.outcome is ClassificationCheckpointOutcome.CLASSIFIED
            and self.assertion_count < 1
        ):
            raise ValueError("classified checkpoint requires an assertion")
        if (
            self.outcome is not ClassificationCheckpointOutcome.CLASSIFIED
            and self.assertion_count != 0
        ):
            raise ValueError("only classified checkpoint may count assertions")
        if (
            self.outcome is ClassificationCheckpointOutcome.FAILED
            and not self.error_code
        ):
            raise ValueError("failed checkpoint requires errorCode")
        if (
            self.outcome is not ClassificationCheckpointOutcome.FAILED
            and self.error_code is not None
        ):
            raise ValueError("only failed checkpoint may contain errorCode")
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
        occurrence.occurrence_hash: occurrence
        for occurrence in candidate.reference_occurrences
    }
    articles = {
        candidate.reference_source.article_id: candidate.reference_source,
        candidate.reference_target.article_id: candidate.reference_target,
    }
    for assertion in decision.assertions:
        occurrence = known_occurrences.get(assertion.reference_occurrence_hash)
        if occurrence is None:
            raise ValueError("assertion references an unknown reference occurrence")
        if assertion.subject_article_id == assertion.object_article_id:
            raise ValueError("assertion subject and object must be different Articles")
        if {
            assertion.subject_article_id,
            assertion.object_article_id,
        } != set(articles):
            raise ValueError("assertion endpoints must match the candidate Articles")
        subject = articles[assertion.subject_article_id]
        object_ = articles[assertion.object_article_id]
        if subject.span(assertion.subject_supporting_span_id) is None:
            raise ValueError(
                "subject supporting span does not belong to subject Article"
            )
        if object_.span(assertion.object_supporting_span_id) is None:
            raise ValueError("object supporting span does not belong to object Article")
        reference_source_span_id = (
            assertion.subject_supporting_span_id
            if assertion.subject_article_id == candidate.reference_source.article_id
            else assertion.object_supporting_span_id
        )
        if reference_source_span_id not in occurrence.source_span_ids:
            raise ValueError(
                "reference source supporting span does not belong to the selected occurrence"
            )


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
        articles = {
            candidate.reference_source.article_id: candidate.reference_source,
            candidate.reference_target.article_id: candidate.reference_target,
        }
        source_article = candidate.reference_source
        subject_span = articles[proposed.subject_article_id].span(
            proposed.subject_supporting_span_id
        )
        object_span = articles[proposed.object_article_id].span(
            proposed.object_supporting_span_id
        )
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
                subject_article_id=proposed.subject_article_id,
                object_article_id=proposed.object_article_id,
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


__all__ = [
    "ArticleSpan",
    "ClassificationArticle",
    "ClassificationCheckpointRecord",
    "ClassificationRunRecord",
    "ExceptionToClassificationResponse",
    "ImplementsClassificationResponse",
    "IncorporatesClassificationResponse",
    "LegalGraphModel",
    "OverridesClassificationResponse",
    "PredicateFindings",
    "ProposedRelationAssertion",
    "ReferenceOccurrence",
    "RelationAssertionRecord",
    "RelationClassificationCandidate",
    "RelationClassificationDecision",
    "RelationClassificationResponse",
    "RelationGroundingResponse",
    "UsesDefinitionClassificationResponse",
    "assertion_dedupe_key",
    "build_assertion_records",
    "stable_hash",
    "validate_classification_decision",
]
