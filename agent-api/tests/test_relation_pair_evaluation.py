import importlib.util
import json
import sys
from pathlib import Path

import pytest

from app.domains.legal.graph_schema import ProposedPredicate
from app.domains.legal.relation_classification import (
    AdjudicationPredicateAssessment,
    AdjudicationPredicateAssessments,
    ArticleSpan,
    ClassificationArticle,
    EvaluationGrounding,
    PredicateGroundingAllowance,
    PredicateRecallAllowance,
    ProposedRelationAssertion,
    ReferenceOccurrence,
    RelationAdjudicationCandidatePacket,
    RelationClassificationCandidate,
    WorkerAdjudicationRecord,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str):
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILD = _load_script("build_relation_pair_gold")
SCORE = _load_script("score_relation_pair_output")


def _packet() -> RelationAdjudicationCandidatePacket:
    source_id = "law-order-article-2"
    target_id = "law-act-article-1"
    candidate = RelationClassificationCandidate(
        source_snapshot_id="snapshot-1",
        graph_schema_version=9,
        prompt_version="prompt-v1",
        provider="codex_subscription",
        model="gpt-5.6-luna",
        reviewer_model="gpt-5.6-luna",
        basis_edge_ids=("edge-1",),
        reference_source=ClassificationArticle(
            article_id=source_id,
            document_id="law-order",
            content_hash="source-hash",
            spans=(ArticleSpan(span_id=f"{source_id}::span-1", text="法第一条に基づく。"),),
        ),
        reference_target=ClassificationArticle(
            article_id=target_id,
            document_id="law-act",
            content_hash="target-hash",
            spans=(
                ArticleSpan(span_id=f"{target_id}::span-1", text="政令で定める。"),
                ArticleSpan(span_id=f"{target_id}::span-2", text="必要事項を委任する。"),
            ),
        ),
        reference_occurrences=(
            ReferenceOccurrence(
                occurrence_hash="occurrence-1",
                basis_edge_id="edge-1",
                reference_kind="parent_law_reference",
                citation_text="法第一条",
                source_content_unit_id=source_id,
                source_start=0,
                source_end=4,
                source_prefix="",
                source_suffix="に基づく。",
                source_span_ids=(f"{source_id}::span-1",),
            ),
        ),
    )
    return RelationAdjudicationCandidatePacket.from_candidate(candidate)


def _worker(
    packet: RelationAdjudicationCandidatePacket,
    *,
    target_span: str,
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
        candidate_key=packet.candidate_key,
        adjudication_status="accepted",
        predicate_assessments=AdjudicationPredicateAssessments(
            IMPLEMENTS=positive,
            INCORPORATES=negative,
            USES_DEFINITION=negative,
            EXCEPTION_TO=negative,
            OVERRIDES=negative,
        ),
        assertions=(
            ProposedRelationAssertion(
                proposed_predicate="IMPLEMENTS",
                reference_occurrence_hash="occurrence-1",
                subject_article_id=packet.reference_target_article.article_id,
                object_article_id=packet.reference_source_article.article_id,
                reference_source_supporting_span_id=(
                    packet.reference_source_article.spans[0].span_id
                ),
                reference_target_supporting_span_id=target_span,
            ),
        ),
    )


def _worker_with_uses_definition(
    packet: RelationAdjudicationCandidatePacket,
    *,
    established: bool,
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
    assertions = (
        (
            ProposedRelationAssertion(
                proposed_predicate="USES_DEFINITION",
                reference_occurrence_hash="occurrence-1",
                subject_article_id=packet.reference_source_article.article_id,
                object_article_id=packet.reference_target_article.article_id,
                reference_source_supporting_span_id=(
                    packet.reference_source_article.spans[0].span_id
                ),
                reference_target_supporting_span_id=(
                    packet.reference_target_article.spans[0].span_id
                ),
            ),
        )
        if established
        else ()
    )
    return WorkerAdjudicationRecord(
        candidate_key=packet.candidate_key,
        adjudication_status="accepted",
        predicate_assessments=AdjudicationPredicateAssessments(
            IMPLEMENTS=negative,
            INCORPORATES=negative,
            USES_DEFINITION=positive if established else negative,
            EXCEPTION_TO=negative,
            OVERRIDES=negative,
        ),
        assertions=assertions,
    )


def _write_jsonl(path: Path, records: list[object]) -> None:
    path.write_text(
        "".join(
            json.dumps(
                record.model_dump(by_alias=True, mode="json"),
                ensure_ascii=False,
            )
            + "\n"
            for record in records
        ),
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_single_edge_reviewed_override_takes_precedence_without_legacy_gold() -> None:
    packet = _packet()
    expected = _worker(
        packet,
        target_span=packet.reference_target_article.spans[0].span_id,
    )
    override = {
        "candidateKey": packet.candidate_key,
        "adjudicationStatus": "accepted",
        "predicateAssessments": {
            predicate: [
                assessment.first_condition.value,
                assessment.second_condition.value,
                assessment.finding.value,
            ]
            for predicate, assessment in (
                expected.predicate_assessments.by_predicate().items()
            )
        },
        "assertions": [
            assertion.model_dump(by_alias=True, mode="json")
            for assertion in expected.assertions
        ],
        "auditNote": "Article全文を確認した人手訂正。",
    }

    records, stats = BUILD.build_pair_gold_records([packet], [], [override])

    assert records == (expected,)
    assert stats == {
        "candidateCount": 1,
        "singletonMigratedCount": 0,
        "pairOverrideCount": 1,
        "singletonOverrideCount": 1,
        "multiEdgeOverrideCount": 0,
    }


def test_grounding_allowance_accepts_only_human_listed_known_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    packet = _packet()
    canonical = _worker(
        packet,
        target_span=packet.reference_target_article.spans[0].span_id,
    )
    alternative = _worker(
        packet,
        target_span=packet.reference_target_article.spans[1].span_id,
    )
    source_span = packet.reference_source_article.spans[0].span_id
    allowance = PredicateGroundingAllowance(
        candidate_key=packet.candidate_key,
        predicate="IMPLEMENTS",
        allowed_groundings=(
            EvaluationGrounding(
                reference_occurrence_hash="occurrence-1",
                reference_source_supporting_span_id=source_span,
                reference_target_supporting_span_id=(
                    packet.reference_target_article.spans[0].span_id
                ),
            ),
            EvaluationGrounding(
                reference_occurrence_hash="occurrence-1",
                reference_source_supporting_span_id=source_span,
                reference_target_supporting_span_id=(
                    packet.reference_target_article.spans[1].span_id
                ),
            ),
        ),
        audit_note="両spanが同じ委任事項を直接支えることを人が確認した。",
    )
    packet_path = tmp_path / "packet.jsonl"
    gold_path = tmp_path / "gold.jsonl"
    actual_path = tmp_path / "actual.jsonl"
    allowance_path = tmp_path / "allowances.jsonl"
    _write_jsonl(packet_path, [packet])
    _write_jsonl(gold_path, [canonical])
    _write_jsonl(actual_path, [alternative])
    _write_jsonl(allowance_path, [allowance])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "score_relation_pair_output.py",
            "--packet",
            str(packet_path),
            "--gold",
            str(gold_path),
            "--actual",
            str(actual_path),
            "--grounding-allowances",
            str(allowance_path),
        ],
    )

    assert SCORE.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["exactCorrectCount"] == 1
    assert report["groundingAllowanceCount"] == 1
    assert report["groundingAlternativeCount"] == 1


def test_grounding_allowance_rejects_unknown_target_span() -> None:
    packet = _packet()
    canonical = _worker(
        packet,
        target_span=packet.reference_target_article.spans[0].span_id,
    )
    allowance = PredicateGroundingAllowance(
        candidate_key=packet.candidate_key,
        predicate="IMPLEMENTS",
        allowed_groundings=(
            EvaluationGrounding(
                reference_occurrence_hash="occurrence-1",
                reference_source_supporting_span_id=(
                    packet.reference_source_article.spans[0].span_id
                ),
                reference_target_supporting_span_id="unknown-span",
            ),
        ),
        audit_note="不正IDを拒否するfixture。",
    )

    with pytest.raises(ValueError, match="unknown target span"):
        SCORE._grounding_allowances(
            [allowance],
            packets={packet.candidate_key: packet},
            gold={packet.candidate_key: canonical},
        )


def test_predicate_recall_allowance_accepts_only_an_explicit_gold_omission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    packet = _packet()
    canonical = _worker_with_uses_definition(packet, established=True)
    omitted = _worker_with_uses_definition(packet, established=False)
    allowance = PredicateRecallAllowance(
        candidate_key=packet.candidate_key,
        predicate="USES_DEFINITION",
        audit_note=(
            "意味上は妥当だが、長いscope連鎖を網羅的に再現することは必須にしない。"
        ),
    )
    packet_path = tmp_path / "packet.jsonl"
    gold_path = tmp_path / "gold.jsonl"
    actual_path = tmp_path / "actual.jsonl"
    allowance_path = tmp_path / "recall-allowances.jsonl"
    _write_jsonl(packet_path, [packet])
    _write_jsonl(gold_path, [canonical])
    _write_jsonl(actual_path, [omitted])
    _write_jsonl(allowance_path, [allowance])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "score_relation_pair_output.py",
            "--packet",
            str(packet_path),
            "--gold",
            str(gold_path),
            "--actual",
            str(actual_path),
            "--predicate-recall-allowances",
            str(allowance_path),
        ],
    )

    assert SCORE.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["exactCorrectCount"] == 1
    assert report["rawExactCorrectCount"] == 0
    assert report["predicateRecallAllowanceCount"] == 1
    assert report["optionalPredicateOmissionCount"] == 1


def test_predicate_recall_allowance_rejects_a_negative_gold_predicate() -> None:
    packet = _packet()
    canonical = _worker_with_uses_definition(packet, established=False)
    allowance = PredicateRecallAllowance(
        candidate_key=packet.candidate_key,
        predicate="USES_DEFINITION",
        audit_note="goldにない関係を許容対象にしてはならない。",
    )

    with pytest.raises(ValueError, match="requires an established gold predicate"):
        SCORE._recall_allowances(
            [allowance],
            packets={packet.candidate_key: packet},
            gold={packet.candidate_key: canonical},
        )


def test_predicate_recall_allowance_does_not_hide_bad_positive_grounding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    packet = _packet()
    canonical = _worker_with_uses_definition(packet, established=True)
    wrong_grounding = canonical.model_copy(
        update={
            "assertions": (
                canonical.assertions[0].model_copy(
                    update={
                        "reference_target_supporting_span_id": (
                            packet.reference_target_article.spans[1].span_id
                        )
                    }
                ),
            )
        }
    )
    allowance = PredicateRecallAllowance(
        candidate_key=packet.candidate_key,
        predicate="USES_DEFINITION",
        audit_note="省略だけを許容し、誤った根拠は許容しない。",
    )
    packet_path = tmp_path / "packet.jsonl"
    gold_path = tmp_path / "gold.jsonl"
    actual_path = tmp_path / "actual.jsonl"
    allowance_path = tmp_path / "recall-allowances.jsonl"
    _write_jsonl(packet_path, [packet])
    _write_jsonl(gold_path, [canonical])
    _write_jsonl(actual_path, [wrong_grounding])
    _write_jsonl(allowance_path, [allowance])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "score_relation_pair_output.py",
            "--packet",
            str(packet_path),
            "--gold",
            str(gold_path),
            "--actual",
            str(actual_path),
            "--predicate-recall-allowances",
            str(allowance_path),
        ],
    )

    assert SCORE.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report["exactCorrectCount"] == 0
    assert report["mismatches"][0]["assertions"]["USES_DEFINITION"]


def test_committed_pair_gold_and_grounding_allowances_are_rebuildable() -> None:
    packet_path = (
        REPO_ROOT
        / "eval-results/relation-guidance-100-pair-v2/semantic-blind-packet.jsonl"
    )
    legacy_path = (
        REPO_ROOT
        / "docs/samples/eval/legal_relation_94_adjudication_audit.jsonl"
    )
    override_path = (
        REPO_ROOT
        / "docs/samples/eval/legal_relation_73_pair_overrides.jsonl"
    )
    gold_path = (
        REPO_ROOT
        / "eval-results/relation-guidance-100-pair-v2/semantic-pair-gold.jsonl"
    )
    allowance_path = (
        REPO_ROOT
        / "docs/samples/eval/legal_relation_73_grounding_allowances.jsonl"
    )
    recall_allowance_path = (
        REPO_ROOT
        / "docs/samples/eval/"
        "legal_relation_73_predicate_recall_allowances.jsonl"
    )
    packets = [
        RelationAdjudicationCandidatePacket.model_validate(record)
        for record in _read_jsonl(packet_path)
    ]
    rebuilt, stats = BUILD.build_pair_gold_records(
        packets,
        _read_jsonl(legacy_path),
        _read_jsonl(override_path),
    )
    committed = tuple(
        WorkerAdjudicationRecord.model_validate(record)
        for record in _read_jsonl(gold_path)
    )

    assert rebuilt == committed
    assert stats["candidateCount"] == 73
    assert stats["pairOverrideCount"] == 22
    assert stats["singletonOverrideCount"] == 7
    allowances = SCORE._grounding_allowances(
        [
            PredicateGroundingAllowance.model_validate(record)
            for record in _read_jsonl(allowance_path)
        ],
        packets={packet.candidate_key: packet for packet in packets},
        gold={record.candidate_key: record for record in committed},
    )
    assert len(allowances) == 11
    assert sum(len(values) - 1 for values in allowances.values()) == 15
    recall_allowances = SCORE._recall_allowances(
        [
            PredicateRecallAllowance.model_validate(record)
            for record in _read_jsonl(recall_allowance_path)
        ],
        packets={packet.candidate_key: packet for packet in packets},
        gold={record.candidate_key: record for record in committed},
    )
    assert recall_allowances == {
        (
            "0d39784c98080896ab0e3c7fd121f7c509fe2d7f3ac14adc36bc567e70446a18",
            ProposedPredicate.USES_DEFINITION,
        )
    }
