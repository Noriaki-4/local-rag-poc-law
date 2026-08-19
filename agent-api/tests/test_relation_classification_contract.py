from datetime import UTC, datetime

import pytest
from app.domains.legal.graph_schema import ClassificationRunPhase, ProposedPredicate
from app.domains.legal.relation_classification import (
    AdjudicationPredicateAssessment,
    AdjudicationPredicateAssessments,
    AdjudicationShardManifest,
    ArticleSpan,
    ClassificationArticle,
    ClassificationRunRecord,
    PredicateFindings,
    PredicateReviewCheck,
    PredicateReviewChecks,
    ProposedRelationAssertion,
    ReferenceOccurrence,
    RelationAdjudicationExecutionProfile,
    RelationAdjudicationManifest,
    RelationClassificationCandidate,
    RelationClassificationDecision,
    ReviewerRecord,
    ReviewIssue,
    WorkerAdjudicationRecord,
    assertion_dedupe_key,
    build_assertion_records,
    validate_classification_decision,
    validate_reviewer_record,
    validate_worker_adjudication,
    stable_hash,
)
from pydantic import ValidationError


def _findings(
    *,
    established: ProposedPredicate | None = ProposedPredicate.IMPLEMENTS,
    uncertain: ProposedPredicate | None = None,
) -> PredicateFindings:
    values = {
        "implements": "not_established",
        "incorporates": "not_established",
        "usesDefinition": "not_established",
        "exceptionTo": "not_established",
        "overrides": "not_established",
    }
    field_by_predicate = {
        ProposedPredicate.IMPLEMENTS: "implements",
        ProposedPredicate.INCORPORATES: "incorporates",
        ProposedPredicate.USES_DEFINITION: "usesDefinition",
        ProposedPredicate.EXCEPTION_TO: "exceptionTo",
        ProposedPredicate.OVERRIDES: "overrides",
    }
    if established is not None:
        values[field_by_predicate[established]] = "established"
    if uncertain is not None:
        values[field_by_predicate[uncertain]] = "uncertain"
    return PredicateFindings.model_validate(values)


def _article(article_id: str, text: str) -> ClassificationArticle:
    return ClassificationArticle(
        article_id=article_id,
        document_id=article_id.split("-article-")[0],
        content_hash=f"hash-{article_id}",
        spans=(ArticleSpan(span_id=f"{article_id}::span-1", text=text),),
    )


def _candidate(
    *, occurrence_order: tuple[str, ...] = ("occ-1", "occ-2")
) -> RelationClassificationCandidate:
    subject = _article("law-a-article-1", "第一条 内閣府令で定める。")
    object_ = _article("law-b-article-2", "第二条 法第一条の事項を定める。")
    return RelationClassificationCandidate(
        source_snapshot_id="snapshot-1",
        graph_schema_version=9,
        prompt_version="legal-relation-v1",
        provider="ollama",
        model="gemma4:e4b",
        basis_edge_id="reference-1",
        reference_source=object_,
        reference_target=subject,
        reference_occurrences=tuple(
            ReferenceOccurrence(
                occurrence_hash=occurrence_hash,
                citation_text="法第一条",
                source_content_unit_id=object_.article_id,
                source_start=0,
                source_end=4,
                source_prefix="",
                source_suffix="の事項を定める。",
                source_span_ids=(object_.spans[0].span_id,),
            )
            for occurrence_hash in occurrence_order
        ),
    )


def _assessment(finding: str = "not_established") -> AdjudicationPredicateAssessment:
    return AdjudicationPredicateAssessment(
        first_condition=finding,
        second_condition=finding,
        finding=finding,
    )


def _adjudication_assessments(
    *, implements: str = "not_established"
) -> AdjudicationPredicateAssessments:
    return AdjudicationPredicateAssessments.model_validate(
        {
            "IMPLEMENTS": _assessment(implements),
            "INCORPORATES": _assessment(),
            "USES_DEFINITION": _assessment(),
            "EXCEPTION_TO": _assessment(),
            "OVERRIDES": _assessment(),
        }
    )


def _review_checks(*, implements: str = "confirmed") -> PredicateReviewChecks:
    return PredicateReviewChecks.model_validate(
        {
            predicate.value: PredicateReviewCheck(
                worker_finding=(
                    "established"
                    if predicate is ProposedPredicate.IMPLEMENTS
                    else "not_established"
                ),
                review_conclusion=(
                    implements
                    if predicate is ProposedPredicate.IMPLEMENTS
                    else "confirmed"
                ),
                note=f"{predicate.value}を二条件で確認した。",
            )
            for predicate in ProposedPredicate
        }
    )


def test_candidate_key_is_stable_when_occurrence_order_changes() -> None:
    assert (
        _candidate().candidate_key
        == _candidate(occurrence_order=("occ-2", "occ-1")).candidate_key
    )


def test_candidate_key_changes_with_snapshot_provider_model_or_content() -> None:
    candidate = _candidate()
    assert (
        candidate.model_copy(update={"source_snapshot_id": "snapshot-2"}).candidate_key
        != candidate.candidate_key
    )
    assert (
        candidate.model_copy(update={"provider": "anthropic"}).candidate_key
        != candidate.candidate_key
    )
    assert (
        candidate.model_copy(update={"model": "another-model"}).candidate_key
        != candidate.candidate_key
    )
    changed_source = candidate.reference_source.model_copy(
        update={"content_hash": "changed"}
    )
    assert (
        candidate.model_copy(update={"reference_source": changed_source}).candidate_key
        != candidate.candidate_key
    )


def test_decision_validation_accepts_known_endpoint_spans() -> None:
    candidate = _candidate()
    decision = RelationClassificationDecision(
        candidate_key=candidate.candidate_key,
        outcome="classified",
        predicate_findings=_findings(),
        assertions=(
            ProposedRelationAssertion(
                proposed_predicate=ProposedPredicate.IMPLEMENTS,
                reference_occurrence_hash="occ-1",
                subject_article_id=candidate.reference_target.article_id,
                object_article_id=candidate.reference_source.article_id,
                reference_source_supporting_span_id=candidate.reference_source.spans[
                    0
                ].span_id,
                reference_target_supporting_span_id=candidate.reference_target.spans[
                    0
                ].span_id,
            ),
        ),
    )
    validate_classification_decision(candidate, decision)


def test_decision_validation_keeps_support_spans_in_physical_reference_direction() -> (
    None
):
    candidate = _candidate()
    decision = RelationClassificationDecision(
        candidate_key=candidate.candidate_key,
        outcome="classified",
        predicate_findings=_findings(),
        assertions=(
            ProposedRelationAssertion(
                proposed_predicate=ProposedPredicate.IMPLEMENTS,
                reference_occurrence_hash="occ-1",
                subject_article_id=candidate.reference_target.article_id,
                object_article_id=candidate.reference_source.article_id,
                reference_source_supporting_span_id=candidate.reference_target.spans[
                    0
                ].span_id,
                reference_target_supporting_span_id=candidate.reference_source.spans[
                    0
                ].span_id,
            ),
        ),
    )
    with pytest.raises(ValueError, match="reference source supporting span"):
        validate_classification_decision(candidate, decision)


def test_non_classified_outcome_cannot_smuggle_an_assertion() -> None:
    candidate = _candidate()
    with pytest.raises(ValidationError, match="only classified outcome"):
        RelationClassificationDecision(
            candidate_key=candidate.candidate_key,
            outcome="reference_only",
            predicate_findings=_findings(established=None),
            assertions=(
                ProposedRelationAssertion(
                    proposed_predicate=ProposedPredicate.IMPLEMENTS,
                    reference_occurrence_hash="occ-1",
                    subject_article_id=candidate.reference_target.article_id,
                    object_article_id=candidate.reference_source.article_id,
                    reference_source_supporting_span_id=candidate.reference_source.spans[
                        0
                    ].span_id,
                    reference_target_supporting_span_id=candidate.reference_target.spans[
                        0
                    ].span_id,
                ),
            ),
        )


def test_classified_assertions_must_match_established_findings() -> None:
    candidate = _candidate()
    with pytest.raises(ValidationError, match="exactly match established"):
        RelationClassificationDecision(
            candidate_key=candidate.candidate_key,
            outcome="classified",
            predicate_findings=_findings(established=ProposedPredicate.INCORPORATES),
            assertions=(
                ProposedRelationAssertion(
                    proposed_predicate=ProposedPredicate.IMPLEMENTS,
                    reference_occurrence_hash="occ-1",
                    subject_article_id=candidate.reference_target.article_id,
                    object_article_id=candidate.reference_source.article_id,
                    reference_source_supporting_span_id=candidate.reference_source.spans[
                        0
                    ].span_id,
                    reference_target_supporting_span_id=candidate.reference_target.spans[
                        0
                    ].span_id,
                ),
            ),
        )


def test_reference_only_requires_all_predicates_not_established() -> None:
    candidate = _candidate()
    with pytest.raises(ValidationError, match="every predicate"):
        RelationClassificationDecision(
            candidate_key=candidate.candidate_key,
            outcome="reference_only",
            predicate_findings=_findings(
                established=None,
                uncertain=ProposedPredicate.USES_DEFINITION,
            ),
        )


def test_uncertain_requires_an_uncertain_predicate_finding() -> None:
    candidate = _candidate()
    decision = RelationClassificationDecision(
        candidate_key=candidate.candidate_key,
        outcome="uncertain",
        predicate_findings=_findings(
            established=None,
            uncertain=ProposedPredicate.EXCEPTION_TO,
        ),
    )

    assert decision.outcome == "uncertain"


def test_assertion_dedupe_key_distinguishes_predicate_and_run() -> None:
    candidate_key = _candidate().candidate_key
    key = assertion_dedupe_key("run-1", candidate_key, ProposedPredicate.IMPLEMENTS)
    assert key == assertion_dedupe_key(
        "run-1", candidate_key, ProposedPredicate.IMPLEMENTS
    )
    assert key != assertion_dedupe_key(
        "run-1", candidate_key, ProposedPredicate.EXCEPTION_TO
    )
    assert key != assertion_dedupe_key(
        "run-2", candidate_key, ProposedPredicate.IMPLEMENTS
    )


def test_build_assertion_records_copies_only_validated_llm_choices() -> None:
    candidate = _candidate()
    decision = RelationClassificationDecision(
        candidate_key=candidate.candidate_key,
        outcome="classified",
        predicate_findings=_findings(),
        assertions=(
            ProposedRelationAssertion(
                proposed_predicate=ProposedPredicate.IMPLEMENTS,
                reference_occurrence_hash="occ-1",
                subject_article_id=candidate.reference_target.article_id,
                object_article_id=candidate.reference_source.article_id,
                reference_source_supporting_span_id=candidate.reference_source.spans[
                    0
                ].span_id,
                reference_target_supporting_span_id=candidate.reference_target.spans[
                    0
                ].span_id,
            ),
        ),
    )
    records = build_assertion_records(
        candidate,
        decision,
        classification_run_id="run-1",
        classified_at=datetime(2026, 8, 18, tzinfo=UTC),
    )
    assert len(records) == 1
    assert records[0].proposed_predicate is ProposedPredicate.IMPLEMENTS
    assert records[0].reference_occurrence_hash == "occ-1"
    assert (
        records[0].subject_supporting_quote == candidate.reference_target.spans[0].text
    )
    assert (
        records[0].object_supporting_quote == candidate.reference_source.spans[0].text
    )


def test_unknown_occurrence_is_rejected_without_program_fallback() -> None:
    candidate = _candidate()
    decision = RelationClassificationDecision(
        candidate_key=candidate.candidate_key,
        outcome="classified",
        predicate_findings=_findings(),
        assertions=(
            ProposedRelationAssertion(
                proposed_predicate=ProposedPredicate.IMPLEMENTS,
                reference_occurrence_hash="unknown",
                subject_article_id=candidate.reference_target.article_id,
                object_article_id=candidate.reference_source.article_id,
                reference_source_supporting_span_id=candidate.reference_source.spans[
                    0
                ].span_id,
                reference_target_supporting_span_id=candidate.reference_target.spans[
                    0
                ].span_id,
            ),
        ),
    )
    with pytest.raises(ValueError, match="unknown reference occurrence"):
        validate_classification_decision(candidate, decision)


def test_reference_source_support_must_match_the_selected_occurrence() -> None:
    candidate = _candidate()
    extra_span = ArticleSpan(
        span_id=f"{candidate.reference_source.article_id}::span-2",
        text="別の参照箇所",
    )
    candidate = candidate.model_copy(
        update={
            "reference_source": candidate.reference_source.model_copy(
                update={"spans": (*candidate.reference_source.spans, extra_span)}
            )
        }
    )
    decision = RelationClassificationDecision(
        candidate_key=candidate.candidate_key,
        outcome="classified",
        predicate_findings=_findings(),
        assertions=(
            ProposedRelationAssertion(
                proposed_predicate=ProposedPredicate.IMPLEMENTS,
                reference_occurrence_hash="occ-1",
                subject_article_id=candidate.reference_target.article_id,
                object_article_id=candidate.reference_source.article_id,
                reference_source_supporting_span_id=extra_span.span_id,
                reference_target_supporting_span_id=candidate.reference_target.spans[
                    0
                ].span_id,
            ),
        ),
    )

    with pytest.raises(ValueError, match="selected occurrence"):
        validate_classification_decision(candidate, decision)


def test_only_complete_run_can_be_published() -> None:
    common = {
        "classification_run_id": "run-1",
        "source_snapshot_id": "snapshot-1",
        "graph_schema_version": 9,
        "provider": "ollama",
        "model": "gemma4:e4b",
        "prompt_version": "legal-relation-v1",
        "candidates_per_model_call": 1,
        "input_count": 2,
        "classified_candidate_count": 1,
        "assertion_count": 1,
        "reference_only_count": 1,
        "uncertain_count": 0,
        "failed_count": 0,
        "scope_hash": "scope-hash",
    }
    published = ClassificationRunRecord(
        **common,
        phase=ClassificationRunPhase.PUBLISHED,
        processed_count=2,
        published_at=datetime.now(UTC),
    )
    assert published.phase == "published"
    with pytest.raises(ValidationError, match="complete input scope"):
        ClassificationRunRecord(
            **{
                **common,
                "classified_candidate_count": 0,
            },
            phase=ClassificationRunPhase.PUBLISHED,
            processed_count=1,
            published_at=datetime.now(UTC),
        )


def test_published_run_rejects_failed_checkpoint_count() -> None:
    with pytest.raises(ValidationError, match="failed checkpoints"):
        ClassificationRunRecord(
            classification_run_id="run-failed",
            phase="published",
            source_snapshot_id="snapshot-1",
            graph_schema_version=9,
            provider="codex_subscription",
            model="gpt-5.6-luna",
            reviewer_model="gpt-5.6-luna",
            prompt_version="legal-relation-v1",
            candidates_per_model_call=5,
            input_count=1,
            processed_count=1,
            classified_candidate_count=0,
            assertion_count=0,
            reference_only_count=0,
            uncertain_count=0,
            failed_count=1,
            scope_hash="scope-hash",
            published_at=datetime.now(UTC),
        )


def test_worker_adjudication_requires_five_consistent_predicates_and_grounding() -> (
    None
):
    candidate = _candidate()
    worker = WorkerAdjudicationRecord(
        candidate_key=candidate.candidate_key,
        basis_edge_id=candidate.basis_edge_id,
        adjudication_status="accepted",
        predicate_assessments=_adjudication_assessments(implements="established"),
        assertions=(
            ProposedRelationAssertion(
                proposed_predicate="IMPLEMENTS",
                reference_occurrence_hash="occ-1",
                subject_article_id=candidate.reference_target.article_id,
                object_article_id=candidate.reference_source.article_id,
                reference_source_supporting_span_id=candidate.reference_source.spans[
                    0
                ].span_id,
                reference_target_supporting_span_id=candidate.reference_target.spans[
                    0
                ].span_id,
            ),
        ),
    )

    validate_worker_adjudication(candidate, worker)


def test_worker_adjudication_does_not_turn_uncertainty_into_accepted() -> None:
    with pytest.raises(ValidationError, match="cannot contain uncertainty"):
        WorkerAdjudicationRecord(
            candidate_key="candidate-1",
            basis_edge_id="basis-1",
            adjudication_status="accepted",
            predicate_assessments=_adjudication_assessments(
                implements="uncertain"
            ),
        )


def test_reviewer_must_copy_worker_findings_and_known_span_ids() -> None:
    candidate = _candidate()
    worker = WorkerAdjudicationRecord(
        candidate_key=candidate.candidate_key,
        basis_edge_id=candidate.basis_edge_id,
        adjudication_status="accepted",
        predicate_assessments=_adjudication_assessments(implements="established"),
        assertions=(
            ProposedRelationAssertion(
                proposed_predicate="IMPLEMENTS",
                reference_occurrence_hash="occ-1",
                subject_article_id=candidate.reference_target.article_id,
                object_article_id=candidate.reference_source.article_id,
                reference_source_supporting_span_id=candidate.reference_source.spans[
                    0
                ].span_id,
                reference_target_supporting_span_id=candidate.reference_target.spans[
                    0
                ].span_id,
            ),
        ),
    )
    review = ReviewerRecord(
        candidate_key=candidate.candidate_key,
        basis_edge_id=candidate.basis_edge_id,
        review_status="request_change",
        predicate_checks=_review_checks(implements="change_required"),
        issues=(
            ReviewIssue(
                predicate="IMPLEMENTS",
                problem_type="grounding",
                critique="根拠spanが参照箇所を支えていない。",
                recommended_action="既知spanを再確認する。",
                supporting_span_ids=("unknown-span",),
            ),
        ),
    )

    with pytest.raises(ValueError, match="unknown supporting span"):
        validate_reviewer_record(candidate, worker, review)


def test_relation_adjudication_execution_profile_enforces_session_limits() -> None:
    profile = RelationAdjudicationExecutionProfile(
        skill_version="legal-relation-adjudicator-v1",
        worker_model="gpt-5.6-luna",
        reviewer_model="gpt-5.6-luna",
        reasoning_effort="high",
        candidates_per_worker_session=5,
        candidates_per_reviewer_session=5,
        max_active_sessions=3,
        worker_reviewer_separate_contexts=True,
        max_revision_rounds=1,
    )
    assert profile.max_active_sessions == 3
    with pytest.raises(ValidationError, match="less than or equal to 5"):
        profile.model_copy(update={"candidates_per_worker_session": 6}).model_validate(
            {
                **profile.model_dump(),
                "candidates_per_worker_session": 6,
            }
        )


def test_relation_adjudication_manifest_covers_each_candidate_once() -> None:
    candidate_keys = ["candidate-1", "candidate-2"]
    profile = RelationAdjudicationExecutionProfile(
        skill_version="legal-relation-adjudicator-2026-08-19",
        worker_model="gpt-5.6-luna",
        reviewer_model="gpt-5.6-luna",
        reasoning_effort="high",
        candidates_per_worker_session=5,
        candidates_per_reviewer_session=5,
        max_active_sessions=3,
        worker_reviewer_separate_contexts=True,
        max_revision_rounds=1,
    )
    manifest = RelationAdjudicationManifest(
        schema_version=1,
        source_packet="/tmp/packet.jsonl",
        source_packet_sha256="a" * 64,
        source_snapshot_id="snapshot-1",
        graph_schema_version=9,
        prompt_version="legal-relation-v1",
        candidate_count=2,
        scope_hash=stable_hash(candidate_keys),
        sharding_mode="fixed_candidate_limit",
        max_candidates_per_shard=5,
        shard_count=1,
        execution_profile=profile,
        shards=(
            AdjudicationShardManifest(
                shard_id="shard-0000",
                file="shard-0000.jsonl",
                candidate_count=2,
                input_characters=100,
                sha256="b" * 64,
                candidate_keys=tuple(candidate_keys),
                basis_edge_ids=("basis-1", "basis-2"),
            ),
        ),
    )

    assert manifest.candidate_count == 2
    with pytest.raises(ValidationError, match="only one shard"):
        RelationAdjudicationManifest.model_validate(
            {
                **manifest.model_dump(by_alias=True),
                "candidateCount": 4,
                "shardCount": 2,
                "shards": [
                    manifest.shards[0].model_dump(by_alias=True),
                    manifest.shards[0].model_dump(by_alias=True),
                ],
            }
        )
