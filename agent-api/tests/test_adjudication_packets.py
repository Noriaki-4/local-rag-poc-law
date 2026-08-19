import hashlib
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.domains.legal.adjudication_packets import (
    canonical_packet_jsonl,
    exclude_completed_packet_records,
    packet_records_from_candidates,
    plan_adjudication_shards,
)
from app.domains.legal.adjudication_import import (
    build_adjudication_import_batch,
    classification_run_from_adjudication_manifest,
)
from app.domains.legal.adjudication_workflow import (
    merge_once_revised_adjudications,
    prepare_adjudication_revisions,
)
from app.domains.legal.relation_classification import (
    AdjudicationPredicateAssessment,
    AdjudicationPredicateAssessments,
    ApprovedAdjudicationRecord,
    ArticleSpan,
    ClassificationArticle,
    PredicateReviewCheck,
    PredicateReviewChecks,
    ReferenceOccurrence,
    ProposedRelationAssertion,
    RelationAdjudicationCandidatePacket,
    RelationClassificationCandidate,
    ReviewerRecord,
    ReviewIssue,
    WorkerAdjudicationRecord,
)
from app.legal_adjudication_importer import LegalAdjudicationImporter


def _candidate(index: int) -> RelationClassificationCandidate:
    source_id = f"law-order-article-{index}"
    target_id = f"law-act-article-{index}"
    citation = f"法第{index}条"
    return RelationClassificationCandidate(
        source_snapshot_id="snapshot-1",
        graph_schema_version=9,
        prompt_version="legal-relation-meaning-grounding-v3",
        provider="codex_subscription",
        model="gpt-5.6-luna",
        reviewer_model="gpt-5.6-luna",
        basis_edge_ids=(f"edge-{index:03d}",),
        reference_source=ClassificationArticle(
            article_id=source_id,
            document_id="law-order",
            content_hash=f"source-hash-{index}",
            authority_type="cabinet_order",
            spans=(
                ArticleSpan(
                    span_id=f"{source_id}::span-1",
                    text=f"{citation}に基づき定める。",
                ),
            ),
        ),
        reference_target=ClassificationArticle(
            article_id=target_id,
            document_id="law-act",
            content_hash=f"target-hash-{index}",
            authority_type="act",
            spans=(
                ArticleSpan(
                    span_id=f"{target_id}::span-1",
                    text="必要な事項は政令で定める。",
                ),
            ),
        ),
        reference_occurrences=(
            ReferenceOccurrence(
                occurrence_hash=f"occurrence-{index}",
                basis_edge_id=f"edge-{index:03d}",
                reference_kind="parent_law_reference",
                citation_text=citation,
                source_content_unit_id=source_id,
                source_start=0,
                source_end=len(citation),
                source_prefix="",
                source_suffix="に基づき定める。",
                source_span_ids=(f"{source_id}::span-1",),
            ),
        ),
    )


def _rows(count: int) -> list[dict]:
    return [
        {
            "basis": {
                "graphEdgeId": f"edge-{index:03d}",
                "referenceKind": "parent_law_reference",
            }
        }
        for index in range(count)
    ]


def _worker(candidate: RelationClassificationCandidate) -> WorkerAdjudicationRecord:
    negative = AdjudicationPredicateAssessment(
        first_condition="not_established",
        second_condition="not_established",
        finding="not_established",
    )
    return WorkerAdjudicationRecord(
        candidate_key=candidate.candidate_key,
        adjudication_status="accepted",
        predicate_assessments=AdjudicationPredicateAssessments(
            implements=negative,
            incorporates=negative,
            uses_definition=negative,
            exception_to=negative,
            overrides=negative,
        ),
    )


def _established_worker(
    candidate: RelationClassificationCandidate,
) -> WorkerAdjudicationRecord:
    positive = AdjudicationPredicateAssessment(
        first_condition="established",
        second_condition="established",
        finding="established",
    )
    negative = AdjudicationPredicateAssessment(
        first_condition="not_established",
        second_condition="not_established",
        finding="not_established",
    )
    return WorkerAdjudicationRecord(
        candidate_key=candidate.candidate_key,
        adjudication_status="accepted",
        predicate_assessments=AdjudicationPredicateAssessments(
            implements=positive,
            incorporates=negative,
            uses_definition=negative,
            exception_to=negative,
            overrides=negative,
        ),
        assertions=(
            ProposedRelationAssertion(
                proposed_predicate="IMPLEMENTS",
                reference_occurrence_hash=candidate.reference_occurrences[
                    0
                ].occurrence_hash,
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


def _review(
    candidate: RelationClassificationCandidate,
    worker: WorkerAdjudicationRecord,
    *,
    request_change: bool,
) -> ReviewerRecord:
    assessments = worker.predicate_assessments

    def confirmed(finding) -> PredicateReviewCheck:
        return PredicateReviewCheck(
            worker_finding=finding,
            review_conclusion="confirmed",
            note="Workerの必要条件と根拠を確認した。",
        )

    changed = PredicateReviewCheck(
        worker_finding=assessments.implements.finding,
        review_conclusion="change_required",
        note="IMPLEMENTSの委任文言を再確認する必要がある。",
    )
    return ReviewerRecord(
        candidate_key=candidate.candidate_key,
        review_status="request_change" if request_change else "approve",
        predicate_checks=PredicateReviewChecks(
            implements=(
                changed
                if request_change
                else confirmed(assessments.implements.finding)
            ),
            incorporates=confirmed(assessments.incorporates.finding),
            uses_definition=confirmed(assessments.uses_definition.finding),
            exception_to=confirmed(assessments.exception_to.finding),
            overrides=confirmed(assessments.overrides.finding),
        ),
        issues=(
            (
                ReviewIssue(
                    predicate="IMPLEMENTS",
                    problem_type="condition",
                    critique="親規定の委任条件の検討が不足している。",
                    recommended_action="両Articleの委任事項を再照合する。",
                    supporting_span_ids=(
                        candidate.reference_target.spans[0].span_id,
                    ),
                ),
            )
            if request_change
            else ()
        ),
    )


def _approved(
    candidate: RelationClassificationCandidate,
    packet: RelationAdjudicationCandidatePacket,
    worker: WorkerAdjudicationRecord,
) -> ApprovedAdjudicationRecord:
    return ApprovedAdjudicationRecord(
        candidate_key=candidate.candidate_key,
        original_candidate=packet,
        worker_decision=worker,
        approval_review=_review(candidate, worker, request_change=False),
        revision_round=0,
    )


def test_packet_candidate_key_is_recomputed_from_complete_input() -> None:
    record = RelationAdjudicationCandidatePacket.from_candidate(_candidate(1))
    payload = record.model_dump(by_alias=True, mode="json")
    payload["candidateKey"] = "0" * 64

    with pytest.raises(ValidationError, match="candidate key does not match"):
        RelationAdjudicationCandidatePacket.model_validate(payload)


def test_packet_export_is_sorted_and_resume_rejects_another_scope() -> None:
    candidates = [_candidate(2), _candidate(0), _candidate(1)]
    records = packet_records_from_candidates(_rows(3), candidates)
    repeated = packet_records_from_candidates(
        reversed(_rows(3)), reversed(candidates)
    )

    assert [item.candidate_key for item in records] == sorted(
        item.candidate_key for item in records
    )
    assert canonical_packet_jsonl(records) == canonical_packet_jsonl(repeated)
    assert b"expectedPredicates" not in canonical_packet_jsonl(records)
    remaining = exclude_completed_packet_records(
        records, {records[1].candidate_key}
    )
    assert [item.candidate_key for item in remaining] == [
        records[0].candidate_key,
        records[2].candidate_key,
    ]
    with pytest.raises(ValueError, match="outside packet scope"):
        exclude_completed_packet_records(records, {"unknown-candidate"})


def test_shard_plan_is_deterministic_and_covers_each_candidate_once(tmp_path) -> None:
    records = packet_records_from_candidates(
        _rows(11), [_candidate(index) for index in reversed(range(11))]
    )
    packet_bytes = canonical_packet_jsonl(records)
    manifest, shard_bytes = plan_adjudication_shards(
        reversed(records),
        source_packet=tmp_path / "packet.jsonl",
        source_packet_bytes=packet_bytes,
    )

    assert [item.candidate_count for item in manifest.shards] == [5, 5, 1]
    assert manifest.execution_profile.max_active_sessions == 3
    assert manifest.execution_profile.reasoning_effort == "high"
    assert manifest.source_packet_sha256 == hashlib.sha256(packet_bytes).hexdigest()
    assert {
        key for shard in manifest.shards for key in shard.candidate_keys
    } == {record.candidate_key for record in records}
    for shard in manifest.shards:
        assert hashlib.sha256(shard_bytes[shard.file]).hexdigest() == shard.sha256

    second_manifest, second_bytes = plan_adjudication_shards(
        records,
        source_packet=tmp_path / "packet.jsonl",
        source_packet_bytes=packet_bytes,
    )
    assert second_manifest == manifest
    assert second_bytes == shard_bytes


def test_review_routes_only_request_change_candidate_to_one_revision() -> None:
    candidates = [_candidate(index) for index in range(5)]
    packets = packet_records_from_candidates(_rows(5), candidates)
    candidates_by_key = {item.candidate_key: item for item in candidates}
    workers = [_worker(candidates_by_key[item.candidate_key]) for item in packets]
    reviews = [
        _review(
            candidates_by_key[item.candidate_key],
            worker,
            request_change=index == 2,
        )
        for index, (item, worker) in enumerate(zip(packets, workers, strict=True))
    ]

    approved, revisions = prepare_adjudication_revisions(
        reversed(packets), reversed(workers), reversed(reviews)
    )

    assert len(approved) == 4
    assert len(revisions) == 1
    assert revisions[0].previous_decision == workers[2]
    single_approved, single_revisions = prepare_adjudication_revisions(
        (packets[2],), (workers[2],), (reviews[2],)
    )
    assert single_approved == ()
    assert single_revisions == revisions


def test_final_review_approves_revision_or_separates_unresolved() -> None:
    candidate = _candidate(1)
    packet = RelationAdjudicationCandidatePacket.from_candidate(candidate)
    initial_worker = _worker(candidate)
    initial_review = _review(candidate, initial_worker, request_change=True)
    revised_worker = _worker(candidate)

    approved, unresolved = merge_once_revised_adjudications(
        (packet,),
        (initial_worker,),
        (initial_review,),
        (revised_worker,),
        (_review(candidate, revised_worker, request_change=False),),
    )
    assert len(approved) == 1
    assert approved[0].worker_decision == revised_worker
    assert approved[0].revision_round == 1
    assert unresolved == ()

    approved, unresolved = merge_once_revised_adjudications(
        (packet,),
        (initial_worker,),
        (initial_review,),
        (revised_worker,),
        (_review(candidate, revised_worker, request_change=True),),
    )
    assert approved == ()
    assert len(unresolved) == 1
    assert unresolved[0].reason == "request_change_after_single_revision"


def test_revision_sets_must_exactly_match_reviewer_requests() -> None:
    candidate = _candidate(1)
    packet = RelationAdjudicationCandidatePacket.from_candidate(candidate)
    worker = _worker(candidate)
    review = _review(candidate, worker, request_change=True)

    with pytest.raises(ValueError, match="exactly match requests"):
        merge_once_revised_adjudications(
            (packet,), (worker,), (review,), (), ()
        )


def test_manifest_builds_luna_classification_run_and_import_records(tmp_path) -> None:
    candidates = [_candidate(0), _candidate(1)]
    packets = packet_records_from_candidates(_rows(2), candidates)
    packet_bytes = canonical_packet_jsonl(packets)
    manifest, _ = plan_adjudication_shards(
        packets,
        source_packet=tmp_path / "packet.jsonl",
        source_packet_bytes=packet_bytes,
    )
    run = classification_run_from_adjudication_manifest(manifest, packets)

    assert run.provider == "codex_subscription"
    assert run.model == "gpt-5.6-luna"
    assert run.reviewer_model == "gpt-5.6-luna"
    assert run.skill_version == "legal-relation-adjudicator-2026-08-19-pair-v4"
    assert run.reasoning_effort == "high"
    assert run.candidates_per_model_call == 5
    assert run.input_count == 2

    candidate_by_key = {item.candidate_key: item for item in candidates}
    first_candidate = candidate_by_key[packets[0].candidate_key]
    second_candidate = candidate_by_key[packets[1].candidate_key]
    first_worker = _established_worker(first_candidate)
    second_initial = _worker(second_candidate)
    second_review = _review(second_candidate, second_initial, request_change=True)
    _, unresolved = merge_once_revised_adjudications(
        (packets[1],),
        (second_initial,),
        (second_review,),
        (_worker(second_candidate),),
        (_review(second_candidate, second_initial, request_change=True),),
    )
    imported_at = datetime.now(UTC)
    batch = build_adjudication_import_batch(
        packets,
        (_approved(first_candidate, packets[0], first_worker),),
        unresolved,
        classification_run_id=run.classification_run_id,
        processed_at=imported_at,
    )

    assert [item.outcome.value for item in batch.checkpoints] == [
        "classified",
        "uncertain",
    ]
    assert len(batch.assertions_by_candidate[first_worker.candidate_key]) == 1
    assert batch.assertions_by_candidate[unresolved[0].candidate_key] == ()
    assert "unresolved_after_revision" in batch.checkpoints[1].decision_payload_json


def test_import_rejects_manifest_with_different_reasoning_effort(tmp_path) -> None:
    candidate = _candidate(0)
    packet = RelationAdjudicationCandidatePacket.from_candidate(candidate)
    data = canonical_packet_jsonl((packet,))
    manifest, _ = plan_adjudication_shards(
        (packet,),
        source_packet=tmp_path / "packet.jsonl",
        source_packet_bytes=data,
        reasoning_effort="medium",
    )

    with pytest.raises(ValueError, match="requires high reasoning"):
        classification_run_from_adjudication_manifest(manifest, (packet,))


class _ImportGraph:
    def __init__(self) -> None:
        self.run = None
        self.checkpoints = {}
        self.assertions = []

    def create_or_resume_classification_run(self, run):
        if self.run is None:
            self.run = dict(run)
        return dict(self.run)

    def save_classification_checkpoint(self, *, checkpoint, assertions):
        existing = self.checkpoints.get(checkpoint["candidateKey"])
        value = (dict(checkpoint), list(assertions))
        if existing is None:
            self.checkpoints[checkpoint["candidateKey"]] = value
            field = {
                "classified": "classifiedCandidateCount",
                "reference_only": "referenceOnlyCount",
                "uncertain": "uncertainCount",
                "failed": "failedCount",
            }[checkpoint["outcome"]]
            self.run["processedCount"] += 1
            self.run[field] += 1
            self.run["assertionCount"] += len(assertions)
            self.assertions.extend(assertions)
            return True
        if existing[0]["decisionPayloadHash"] != checkpoint["decisionPayloadHash"]:
            raise RuntimeError("classification checkpoint payload conflicts")
        return False

    def classification_run_materialization(self, run_id):
        return {
            "run": dict(self.run),
            "checkpoints": [value[0] for value in self.checkpoints.values()],
            "assertions": [
                {
                    "assertion": assertion,
                    "subjects": [assertion["subjectArticleId"]],
                    "objects": [assertion["objectArticleId"]],
                    "runs": [assertion["classificationRunId"]],
                    "basisCount": 1,
                }
                for assertion in self.assertions
            ],
        }

    def publish_classification_run(self, run_id, *, published_at):
        self.run["phase"] = "published"
        self.run["publishedAt"] = published_at
        return dict(self.run)

    def fail_classification_run(self, run_id, *, error_code):
        self.run["phase"] = "failed"
        self.run["errorCode"] = error_code
        return dict(self.run)


def test_importer_dry_run_is_read_only_and_reimport_is_idempotent(tmp_path) -> None:
    candidate = _candidate(0)
    packet = RelationAdjudicationCandidatePacket.from_candidate(candidate)
    packet_bytes = canonical_packet_jsonl((packet,))
    manifest, _ = plan_adjudication_shards(
        (packet,),
        source_packet=tmp_path / "packet.jsonl",
        source_packet_bytes=packet_bytes,
    )
    graph = _ImportGraph()
    importer = LegalAdjudicationImporter(graph)

    dry_run = importer.run(
        manifest=manifest,
        packets=(packet,),
        approved_records=(
            _approved(candidate, packet, _established_worker(candidate)),
        ),
        unresolved_records=(),
    )
    assert dry_run["dryRun"] is True
    assert graph.run is None
    assert graph.checkpoints == {}

    first = importer.run(
        manifest=manifest,
        packets=(packet,),
        approved_records=(
            _approved(candidate, packet, _established_worker(candidate)),
        ),
        unresolved_records=(),
        apply=True,
    )
    second = importer.run(
        manifest=manifest,
        packets=(packet,),
        approved_records=(
            _approved(candidate, packet, _established_worker(candidate)),
        ),
        unresolved_records=(),
        apply=True,
    )
    assert first["savedCount"] == 1
    assert second["savedCount"] == 0
    assert second["skippedCount"] == 1

    with pytest.raises(RuntimeError, match="payload conflicts"):
        importer.run(
            manifest=manifest,
            packets=(packet,),
            approved_records=(_approved(candidate, packet, _worker(candidate)),),
            unresolved_records=(),
            apply=True,
        )
    assert graph.run["phase"] == "failed"
    assert graph.run["errorCode"] == "adjudication_import_conflict"

    # The conflict failure is terminal. Publish is tested with a clean run below.
    graph = _ImportGraph()
    importer = LegalAdjudicationImporter(graph)

    published = importer.run(
        manifest=manifest,
        packets=(packet,),
        approved_records=(
            _approved(candidate, packet, _established_worker(candidate)),
        ),
        unresolved_records=(),
        apply=True,
        publish=True,
    )
    assert published["published"] is True
    assert graph.run["phase"] == "published"
