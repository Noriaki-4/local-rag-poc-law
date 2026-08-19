"""非同期法令関係分類の入出力と決定的な構造検証。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from .graph_schema import (
    AdjudicationStatus,
    ClassificationCheckpointOutcome,
    ClassificationRunPhase,
    PredicateFinding,
    ProposedPredicate,
    RelationClassificationOutcome,
    ReviewConclusion,
    ReviewProblemType,
    ReviewStatus,
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
    basis_edge_id: str = Field(min_length=1, max_length=500)
    reference_kind: str = Field(min_length=1, max_length=160)
    citation_text: str = Field(min_length=1)
    source_content_unit_id: str = Field(min_length=1, max_length=500)
    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)
    source_prefix: str = Field(max_length=160)
    source_suffix: str = Field(max_length=160)
    source_span_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_span_ids(self) -> ReferenceOccurrence:
        if self.source_end <= self.source_start:
            raise ValueError("reference source end must be after start")
        if len(self.source_span_ids) != len(set(self.source_span_ids)):
            raise ValueError("reference source span IDs must be unique")
        return self


class RelationClassificationCandidate(LegalGraphModel):
    """1組のArticle端点と、その間の全原文Relationを分類する入力。

    `reference_source / reference_target`は原文REFERENCESの物理方向であり、
    法的意味上のSUBJECT / OBJECTではない。意味方向はLLM出力で選択する。
    """

    source_snapshot_id: str = Field(min_length=1, max_length=500)
    graph_schema_version: int = Field(ge=1)
    prompt_version: str = Field(min_length=1, max_length=160)
    provider: str = Field(min_length=1, max_length=160)
    model: str = Field(min_length=1, max_length=300)
    reviewer_model: str | None = Field(default=None, max_length=300)
    basis_edge_ids: tuple[str, ...] = Field(min_length=1)
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
        if self.basis_edge_ids != tuple(sorted(set(self.basis_edge_ids))):
            raise ValueError("candidate basis edge IDs must be sorted and unique")
        occurrence_basis_ids = {
            item.basis_edge_id for item in self.reference_occurrences
        }
        if occurrence_basis_ids != set(self.basis_edge_ids):
            raise ValueError(
                "candidate basis edge IDs must exactly match reference occurrences"
            )
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
                "basisEdgeIds": self.basis_edge_ids,
                "referenceSourceArticleId": self.reference_source.article_id,
                "referenceSourceContentHash": self.reference_source.content_hash,
                "referenceTargetArticleId": self.reference_target.article_id,
                "referenceTargetContentHash": self.reference_target.content_hash,
                "referenceOccurrenceHashes": sorted(
                    item.occurrence_hash for item in self.reference_occurrences
                ),
            }
        )


class RelationAdjudicationCandidatePacket(LegalGraphModel):
    """Codex Workerへ渡す、label-freeな1候補の完全な作業packet。"""

    candidate_key: str = Field(min_length=64, max_length=64)
    source_snapshot_id: str = Field(min_length=1, max_length=500)
    graph_schema_version: int = Field(ge=1)
    prompt_version: str = Field(min_length=1, max_length=160)
    provider: str = Field(min_length=1, max_length=160)
    model: str = Field(min_length=1, max_length=300)
    reviewer_model: str = Field(min_length=1, max_length=300)
    basis_edge_ids: tuple[str, ...] = Field(min_length=1)
    reference_source_article: ClassificationArticle
    reference_target_article: ClassificationArticle
    reference_occurrences: tuple[ReferenceOccurrence, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_candidate_identity(self) -> RelationAdjudicationCandidatePacket:
        candidate = self.to_candidate()
        if candidate.candidate_key != self.candidate_key:
            raise ValueError("packet candidate key does not match packet contents")
        return self

    def to_candidate(self) -> RelationClassificationCandidate:
        return RelationClassificationCandidate(
            source_snapshot_id=self.source_snapshot_id,
            graph_schema_version=self.graph_schema_version,
            prompt_version=self.prompt_version,
            provider=self.provider,
            model=self.model,
            reviewer_model=self.reviewer_model,
            basis_edge_ids=self.basis_edge_ids,
            reference_source=self.reference_source_article,
            reference_target=self.reference_target_article,
            reference_occurrences=self.reference_occurrences,
        )

    @classmethod
    def from_candidate(
        cls,
        candidate: RelationClassificationCandidate,
    ) -> RelationAdjudicationCandidatePacket:
        if candidate.reviewer_model is None:
            raise ValueError("adjudication packet requires a reviewer model")
        return cls(
            candidate_key=candidate.candidate_key,
            source_snapshot_id=candidate.source_snapshot_id,
            graph_schema_version=candidate.graph_schema_version,
            prompt_version=candidate.prompt_version,
            provider=candidate.provider,
            model=candidate.model,
            reviewer_model=candidate.reviewer_model,
            basis_edge_ids=candidate.basis_edge_ids,
            reference_source_article=candidate.reference_source,
            reference_target_article=candidate.reference_target,
            reference_occurrences=candidate.reference_occurrences,
        )


class ProposedRelationAssertion(LegalGraphModel):
    proposed_predicate: ProposedPredicate
    reference_occurrence_hash: str = Field(min_length=1, max_length=128)
    subject_article_id: str = Field(min_length=1, max_length=500)
    object_article_id: str = Field(min_length=1, max_length=500)
    reference_source_supporting_span_id: str = Field(min_length=1, max_length=500)
    reference_target_supporting_span_id: str = Field(min_length=1, max_length=500)


class EvaluationGrounding(LegalGraphModel):
    """人が確認した、評価時だけ使用できる物理根拠IDの組。"""

    reference_occurrence_hash: str = Field(min_length=1, max_length=128)
    reference_source_supporting_span_id: str = Field(min_length=1, max_length=500)
    reference_target_supporting_span_id: str = Field(min_length=1, max_length=500)


class PredicateGroundingAllowance(LegalGraphModel):
    """1候補・1predicateについて人が明示した妥当なgrounding集合。"""

    candidate_key: str = Field(min_length=64, max_length=64)
    predicate: ProposedPredicate
    allowed_groundings: tuple[EvaluationGrounding, ...] = Field(min_length=1)
    audit_note: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def validate_unique_groundings(self) -> PredicateGroundingAllowance:
        if len(self.allowed_groundings) != len(set(self.allowed_groundings)):
            raise ValueError("allowed groundings must be unique")
        return self


class PredicateRecallAllowance(LegalGraphModel):
    """人が確認した、評価時だけ見落としを許容する妥当なpredicate。"""

    candidate_key: str = Field(min_length=64, max_length=64)
    predicate: ProposedPredicate
    audit_note: str = Field(min_length=1, max_length=4000)


class AdjudicationPredicateAssessment(LegalGraphModel):
    """Workerが評価するpredicate固有の二条件と結論。"""

    first_condition: PredicateFinding
    second_condition: PredicateFinding
    finding: PredicateFinding

    @model_validator(mode="after")
    def validate_conditions(self) -> AdjudicationPredicateAssessment:
        _validate_two_conditions(
            self.finding,
            self.first_condition,
            self.second_condition,
        )
        return self


class AdjudicationPredicateAssessments(LegalGraphModel):
    """Worker JSONLで必須となる5 predicateの完全な評価集合。"""

    implements: AdjudicationPredicateAssessment = Field(alias="IMPLEMENTS")
    incorporates: AdjudicationPredicateAssessment = Field(alias="INCORPORATES")
    uses_definition: AdjudicationPredicateAssessment = Field(alias="USES_DEFINITION")
    exception_to: AdjudicationPredicateAssessment = Field(alias="EXCEPTION_TO")
    overrides: AdjudicationPredicateAssessment = Field(alias="OVERRIDES")

    def by_predicate(self) -> dict[ProposedPredicate, AdjudicationPredicateAssessment]:
        return {
            ProposedPredicate.IMPLEMENTS: self.implements,
            ProposedPredicate.INCORPORATES: self.incorporates,
            ProposedPredicate.USES_DEFINITION: self.uses_definition,
            ProposedPredicate.EXCEPTION_TO: self.exception_to,
            ProposedPredicate.OVERRIDES: self.overrides,
        }


class WorkerAdjudicationRecord(LegalGraphModel):
    """Codex Workerが1候補について返す完全な意味判断。"""

    candidate_key: str = Field(min_length=1, max_length=128)
    adjudication_status: AdjudicationStatus
    predicate_assessments: AdjudicationPredicateAssessments
    assertions: tuple[ProposedRelationAssertion, ...] = ()
    note: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_status_and_assertions(self) -> WorkerAdjudicationRecord:
        assessments = self.predicate_assessments.by_predicate()
        established = {
            predicate
            for predicate, assessment in assessments.items()
            if assessment.finding is PredicateFinding.ESTABLISHED
        }
        uncertain = {
            predicate
            for predicate, assessment in assessments.items()
            if assessment.finding is PredicateFinding.UNCERTAIN
        }
        assertion_predicates = [item.proposed_predicate for item in self.assertions]
        if len(assertion_predicates) != len(set(assertion_predicates)):
            raise ValueError("each established predicate requires at most one assertion")
        if set(assertion_predicates) != established:
            raise ValueError(
                "assertions must exactly match established predicate assessments"
            )

        if self.adjudication_status is AdjudicationStatus.ACCEPTED:
            if uncertain:
                raise ValueError("accepted adjudication cannot contain uncertainty")
            if self.note is not None:
                raise ValueError("accepted adjudication must omit note")
        elif self.adjudication_status is AdjudicationStatus.NEEDS_REVIEW:
            if not uncertain:
                raise ValueError("needs_review requires an uncertain predicate")
            if self.note is None:
                raise ValueError("needs_review requires a note")
        else:
            if any(
                assessment.finding is not PredicateFinding.UNCERTAIN
                for assessment in assessments.values()
            ):
                raise ValueError(
                    "needs_resolution must leave every predicate uncertain"
                )
            if self.assertions:
                raise ValueError("needs_resolution cannot contain assertions")
            if self.note is None:
                raise ValueError("needs_resolution requires a note")
        return self


class PredicateReviewCheck(LegalGraphModel):
    worker_finding: PredicateFinding
    review_conclusion: ReviewConclusion
    note: str = Field(min_length=1, max_length=2000)


class PredicateReviewChecks(LegalGraphModel):
    implements: PredicateReviewCheck = Field(alias="IMPLEMENTS")
    incorporates: PredicateReviewCheck = Field(alias="INCORPORATES")
    uses_definition: PredicateReviewCheck = Field(alias="USES_DEFINITION")
    exception_to: PredicateReviewCheck = Field(alias="EXCEPTION_TO")
    overrides: PredicateReviewCheck = Field(alias="OVERRIDES")

    def by_predicate(self) -> dict[ProposedPredicate, PredicateReviewCheck]:
        return {
            ProposedPredicate.IMPLEMENTS: self.implements,
            ProposedPredicate.INCORPORATES: self.incorporates,
            ProposedPredicate.USES_DEFINITION: self.uses_definition,
            ProposedPredicate.EXCEPTION_TO: self.exception_to,
            ProposedPredicate.OVERRIDES: self.overrides,
        }


class ReviewIssue(LegalGraphModel):
    predicate: ProposedPredicate
    problem_type: ReviewProblemType
    critique: str = Field(min_length=1, max_length=4000)
    recommended_action: str = Field(min_length=1, max_length=4000)
    supporting_span_ids: tuple[str, ...] = ()


class ReviewerRecord(LegalGraphModel):
    """ReviewerがWorker回答を見て返す確認結果。"""

    candidate_key: str = Field(min_length=1, max_length=128)
    review_status: ReviewStatus
    predicate_checks: PredicateReviewChecks
    issues: tuple[ReviewIssue, ...] = ()

    @model_validator(mode="after")
    def validate_review_shape(self) -> ReviewerRecord:
        checks = self.predicate_checks.by_predicate()
        change_required = {
            predicate
            for predicate, check in checks.items()
            if check.review_conclusion is ReviewConclusion.CHANGE_REQUIRED
        }
        issue_predicates = {issue.predicate for issue in self.issues}
        if issue_predicates != change_required:
            raise ValueError(
                "change_required predicates must exactly match issue predicates"
            )
        expected = (
            ReviewStatus.REQUEST_CHANGE if change_required else ReviewStatus.APPROVE
        )
        if self.review_status is not expected:
            raise ValueError("review status must match predicate check conclusions")
        return self


class AdjudicationRevisionPacket(LegalGraphModel):
    """Reviewerが差し戻した1候補を、同じWorkerへ一度だけ返すpacket。"""

    candidate_key: str = Field(min_length=64, max_length=64)
    original_candidate: RelationAdjudicationCandidatePacket
    previous_decision: WorkerAdjudicationRecord
    review_feedback: ReviewerRecord

    @model_validator(mode="after")
    def validate_revision_scope(self) -> AdjudicationRevisionPacket:
        candidate = self.original_candidate.to_candidate()
        if self.candidate_key != candidate.candidate_key:
            raise ValueError("revision identity must match original candidate")
        validate_reviewer_record(
            candidate,
            self.previous_decision,
            self.review_feedback,
        )
        if self.review_feedback.review_status is not ReviewStatus.REQUEST_CHANGE:
            raise ValueError("revision packet requires request_change")
        return self


class UnresolvedAdjudicationRecord(LegalGraphModel):
    """1回の差戻し後もReviewerが承認しなかった候補の監査記録。"""

    candidate_key: str = Field(min_length=64, max_length=64)
    reason: str = Field(pattern="^request_change_after_single_revision$")
    original_candidate: RelationAdjudicationCandidatePacket
    initial_worker_decision: WorkerAdjudicationRecord
    initial_review: ReviewerRecord
    revised_worker_decision: WorkerAdjudicationRecord
    final_review: ReviewerRecord

    @model_validator(mode="after")
    def validate_unresolved_scope(self) -> UnresolvedAdjudicationRecord:
        candidate = self.original_candidate.to_candidate()
        if self.candidate_key != candidate.candidate_key:
            raise ValueError("unresolved identity must match original candidate")
        validate_reviewer_record(
            candidate,
            self.initial_worker_decision,
            self.initial_review,
        )
        validate_reviewer_record(
            candidate,
            self.revised_worker_decision,
            self.final_review,
        )
        if self.initial_review.review_status is not ReviewStatus.REQUEST_CHANGE:
            raise ValueError("unresolved record requires an initial request_change")
        if self.final_review.review_status is not ReviewStatus.REQUEST_CHANGE:
            raise ValueError("unresolved record requires a final request_change")
        return self


class ApprovedAdjudicationRecord(LegalGraphModel):
    """Reviewer承認とWorker回答を切り離さずにimportする証跡。"""

    candidate_key: str = Field(min_length=64, max_length=64)
    original_candidate: RelationAdjudicationCandidatePacket
    worker_decision: WorkerAdjudicationRecord
    approval_review: ReviewerRecord
    revision_round: int = Field(ge=0, le=1)
    initial_worker_decision: WorkerAdjudicationRecord | None = None
    initial_review: ReviewerRecord | None = None

    @model_validator(mode="after")
    def validate_approval_scope(self) -> ApprovedAdjudicationRecord:
        candidate = self.original_candidate.to_candidate()
        if self.candidate_key != candidate.candidate_key:
            raise ValueError("approval identity must match original candidate")
        validate_reviewer_record(
            candidate,
            self.worker_decision,
            self.approval_review,
        )
        if self.approval_review.review_status is not ReviewStatus.APPROVE:
            raise ValueError("approved record requires Reviewer approve")
        if self.revision_round == 0:
            if self.initial_worker_decision is not None or self.initial_review is not None:
                raise ValueError("initial approval cannot contain revision history")
        else:
            if self.initial_worker_decision is None or self.initial_review is None:
                raise ValueError("revised approval requires initial review history")
            validate_reviewer_record(
                candidate,
                self.initial_worker_decision,
                self.initial_review,
            )
            if self.initial_review.review_status is not ReviewStatus.REQUEST_CHANGE:
                raise ValueError("revised approval requires initial request_change")
        return self


class RelationAdjudicationExecutionProfile(LegalGraphModel):
    """全件実行成果物へ記録するWorker / Reviewerの実行契約。"""

    skill_version: str = Field(min_length=1, max_length=160)
    worker_model: str = Field(min_length=1, max_length=300)
    reviewer_model: str = Field(min_length=1, max_length=300)
    reasoning_effort: str = Field(min_length=1, max_length=80)
    candidates_per_worker_session: int = Field(ge=1, le=5)
    candidates_per_reviewer_session: int = Field(ge=1, le=5)
    max_active_sessions: int = Field(ge=1, le=3)
    worker_reviewer_separate_contexts: bool
    max_revision_rounds: int = Field(ge=1, le=1)

    @model_validator(mode="after")
    def validate_context_boundary(self) -> RelationAdjudicationExecutionProfile:
        if not self.worker_reviewer_separate_contexts:
            raise ValueError("Worker and Reviewer must use separate contexts")
        return self


class AdjudicationShardManifest(LegalGraphModel):
    shard_id: str = Field(min_length=1, max_length=160)
    file: str = Field(min_length=1, max_length=500)
    candidate_count: int = Field(ge=1, le=5)
    input_characters: int = Field(ge=1)
    sha256: str = Field(min_length=64, max_length=64)
    candidate_keys: tuple[str, ...] = Field(min_length=1, max_length=5)
    basis_edge_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_shard_coverage(self) -> AdjudicationShardManifest:
        if len(self.candidate_keys) != self.candidate_count:
            raise ValueError("shard candidate count must match candidate keys")
        if len(set(self.candidate_keys)) != len(self.candidate_keys):
            raise ValueError("shard candidate keys must be unique")
        if len(set(self.basis_edge_ids)) != len(self.basis_edge_ids):
            raise ValueError("shard basis edge IDs must be unique")
        return self


class RelationAdjudicationManifest(LegalGraphModel):
    schema_version: Literal[2]
    source_packet: str = Field(min_length=1)
    source_packet_sha256: str = Field(min_length=64, max_length=64)
    source_snapshot_id: str = Field(min_length=1, max_length=500)
    graph_schema_version: int = Field(ge=1)
    prompt_version: str = Field(min_length=1, max_length=160)
    candidate_count: int = Field(ge=1)
    scope_hash: str = Field(min_length=64, max_length=64)
    sharding_mode: str = Field(pattern="^fixed_candidate_limit$")
    max_candidates_per_shard: int = Field(ge=1, le=5)
    shard_count: int = Field(ge=1)
    execution_profile: RelationAdjudicationExecutionProfile
    shards: tuple[AdjudicationShardManifest, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_manifest_coverage(self) -> RelationAdjudicationManifest:
        if len(self.shards) != self.shard_count:
            raise ValueError("manifest shard count must match shard records")
        if any(
            shard.candidate_count > self.max_candidates_per_shard
            for shard in self.shards
        ):
            raise ValueError("shard exceeds max candidates per shard")
        candidate_keys = [key for shard in self.shards for key in shard.candidate_keys]
        basis_edge_ids = [
            basis_id for shard in self.shards for basis_id in shard.basis_edge_ids
        ]
        if len(candidate_keys) != self.candidate_count:
            raise ValueError("manifest candidate count must match all shards")
        if len(set(candidate_keys)) != len(candidate_keys):
            raise ValueError("candidate key may appear in only one shard")
        if len(set(basis_edge_ids)) != len(basis_edge_ids):
            raise ValueError("basis edge ID may appear in only one shard")
        if stable_hash(sorted(candidate_keys)) != self.scope_hash:
            raise ValueError("manifest scope hash must match candidate keys")
        profile = self.execution_profile
        if (
            profile.candidates_per_worker_session
            != self.max_candidates_per_shard
            or profile.candidates_per_reviewer_session
            != self.max_candidates_per_shard
        ):
            raise ValueError("manifest shard limit must match execution profile")
        return self


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
    relation_explanation: str | None = Field(
        default=None,
        min_length=1,
        max_length=2000,
    )
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
    skill_version: str | None = Field(default=None, max_length=160)
    reasoning_effort: str | None = Field(default=None, max_length=80)
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
            if self.failed_count:
                raise ValueError("published run cannot contain failed checkpoints")
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
    error_stage: str | None = Field(default=None, max_length=160)
    error_message: str | None = Field(default=None, max_length=2000)
    error_predicate: ProposedPredicate | None = None
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
        if self.outcome is not ClassificationCheckpointOutcome.FAILED and any(
            value is not None
            for value in (
                self.error_code,
                self.error_stage,
                self.error_message,
                self.error_predicate,
            )
        ):
            raise ValueError("only failed checkpoint may contain error details")
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
        if (
            assertion.reference_source_supporting_span_id
            not in occurrence.source_span_ids
        ):
            raise ValueError(
                "reference source supporting span does not belong to the selected occurrence"
            )
        if (
            candidate.reference_target.span(
                assertion.reference_target_supporting_span_id
            )
            is None
        ):
            raise ValueError(
                "reference target supporting span does not belong to reference target Article"
            )


def validate_worker_adjudication(
    candidate: RelationClassificationCandidate,
    worker: WorkerAdjudicationRecord,
) -> None:
    """Worker出力の既知IDだけを検査し、意味判断は変更しない。"""

    if worker.candidate_key != candidate.candidate_key:
        raise ValueError("Worker references an unknown candidate key")
    validate_classification_decision(
        candidate,
        worker_adjudication_to_decision(worker),
    )


def worker_adjudication_to_decision(
    worker: WorkerAdjudicationRecord,
) -> RelationClassificationDecision:
    """Worker判定を保存用の共通Decisionへ値を変えずに投影する。"""

    return RelationClassificationDecision(
        candidate_key=worker.candidate_key,
        outcome=(
            RelationClassificationOutcome.CLASSIFIED
            if worker.assertions
            else (
                RelationClassificationOutcome.UNCERTAIN
                if worker.adjudication_status
                in {
                    AdjudicationStatus.NEEDS_REVIEW,
                    AdjudicationStatus.NEEDS_RESOLUTION,
                }
                else RelationClassificationOutcome.REFERENCE_ONLY
            )
        ),
        predicate_findings=PredicateFindings(
            **{
                predicate.value.lower(): assessment.finding
                for predicate, assessment in worker.predicate_assessments.by_predicate().items()
            }
        ),
        assertions=worker.assertions,
    )


def validate_reviewer_record(
    candidate: RelationClassificationCandidate,
    worker: WorkerAdjudicationRecord,
    review: ReviewerRecord,
) -> None:
    """Reviewer出力の対応関係と既知IDだけを検査する。"""

    validate_worker_adjudication(candidate, worker)
    if review.candidate_key != candidate.candidate_key:
        raise ValueError("Reviewer references an unknown candidate key")
    worker_findings = {
        predicate: assessment.finding
        for predicate, assessment in worker.predicate_assessments.by_predicate().items()
    }
    review_checks = review.predicate_checks.by_predicate()
    if any(
        review_checks[predicate].worker_finding is not finding
        for predicate, finding in worker_findings.items()
    ):
        raise ValueError("Reviewer must copy every Worker finding exactly")
    known_span_ids = {
        span.span_id
        for article in (candidate.reference_source, candidate.reference_target)
        for span in article.spans
    }
    for issue in review.issues:
        if any(span_id not in known_span_ids for span_id in issue.supporting_span_ids):
            raise ValueError("Reviewer issue references an unknown supporting span ID")


def build_assertion_records(
    candidate: RelationClassificationCandidate,
    decision: RelationClassificationDecision,
    *,
    classification_run_id: str,
    classified_at: datetime,
    relation_explanations: Mapping[ProposedPredicate, str] | None = None,
) -> tuple[RelationAssertionRecord, ...]:
    """検証済みLLM出力を保存形へ写す。predicateの選択・補正はしない。"""

    validate_classification_decision(candidate, decision)
    occurrences = {
        item.occurrence_hash: item for item in candidate.reference_occurrences
    }
    records: list[RelationAssertionRecord] = []
    for proposed in decision.assertions:
        occurrence = occurrences[proposed.reference_occurrence_hash]
        source_article = candidate.reference_source
        source_span = source_article.span(proposed.reference_source_supporting_span_id)
        target_span = candidate.reference_target.span(
            proposed.reference_target_supporting_span_id
        )
        if source_span is None or target_span is None:
            raise ValueError("validated supporting span is unavailable")
        source_is_subject = (
            proposed.subject_article_id == candidate.reference_source.article_id
        )
        subject_span = source_span if source_is_subject else target_span
        object_span = target_span if source_is_subject else source_span
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
                basis_edge_id=occurrence.basis_edge_id,
                source_content_unit_id=occurrence.source_content_unit_id,
                subject_article_id=proposed.subject_article_id,
                object_article_id=proposed.object_article_id,
                subject_supporting_span_id=subject_span.span_id,
                object_supporting_span_id=object_span.span_id,
                subject_supporting_quote=subject_span.text,
                object_supporting_quote=object_span.text,
                relation_explanation=(
                    relation_explanations.get(proposed.proposed_predicate)
                    if relation_explanations is not None
                    else None
                ),
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
    "AdjudicationPredicateAssessment",
    "AdjudicationPredicateAssessments",
    "ApprovedAdjudicationRecord",
    "AdjudicationShardManifest",
    "AdjudicationRevisionPacket",
    "ArticleSpan",
    "ClassificationArticle",
    "ClassificationCheckpointRecord",
    "ClassificationRunRecord",
    "EvaluationGrounding",
    "ExceptionToClassificationResponse",
    "ImplementsClassificationResponse",
    "IncorporatesClassificationResponse",
    "LegalGraphModel",
    "OverridesClassificationResponse",
    "PredicateFindings",
    "PredicateGroundingAllowance",
    "PredicateRecallAllowance",
    "PredicateReviewCheck",
    "PredicateReviewChecks",
    "ProposedRelationAssertion",
    "ReferenceOccurrence",
    "RelationAssertionRecord",
    "RelationAdjudicationExecutionProfile",
    "RelationAdjudicationCandidatePacket",
    "RelationAdjudicationManifest",
    "RelationClassificationCandidate",
    "RelationClassificationDecision",
    "RelationClassificationResponse",
    "RelationGroundingResponse",
    "ReviewerRecord",
    "ReviewIssue",
    "UsesDefinitionClassificationResponse",
    "UnresolvedAdjudicationRecord",
    "assertion_dedupe_key",
    "build_assertion_records",
    "stable_hash",
    "validate_classification_decision",
    "validate_reviewer_record",
    "validate_worker_adjudication",
    "worker_adjudication_to_decision",
    "WorkerAdjudicationRecord",
]
