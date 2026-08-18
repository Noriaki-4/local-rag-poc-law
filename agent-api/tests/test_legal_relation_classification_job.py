import json
from types import SimpleNamespace

import pytest
from app import legal_relation_classification_job as module
from app.domains.legal.graph_schema import ProposedPredicate
from app.domains.legal.relation_classification import PredicateFindings
from app.legal_relation_classification_job import (
    LegalRelationClassificationJob,
    audit_classification_materialization,
    candidates_from_graph_and_sources,
    parse_relation_classification_decision,
    parse_relation_meaning_response,
    relation_classification_prompt,
    relation_classification_repair_prompt,
    relation_classification_schema,
    relation_grounding_prompt,
    relation_grounding_schema,
)

SNAPSHOT_ID = "snapshot-v9"
SOURCE_ARTICLE_ID = "law-order-article-2"
TARGET_ARTICLE_ID = "law-act-article-1"


def _rows():
    citation = "法第一条に基づき対象を定める。"
    return [
        {
            "basis": {
                "graphEdgeId": "edge-reference-1",
                "sourceContentUnitId": SOURCE_ARTICLE_ID,
                "sourceSnapshotId": SNAPSHOT_ID,
                "graphSchemaVersion": 9,
                "citationText": citation,
                "citationTexts": [citation],
                "sourceSpanStarts": [0],
                "sourceSpanEnds": [len(citation)],
            },
            "referenceSourceArticle": {
                "graphNodeId": SOURCE_ARTICLE_ID,
                "documentId": "law-order",
                "contentHash": "hash-order-2",
                "sourceSnapshotId": SNAPSHOT_ID,
                "graphSchemaVersion": 9,
                "authorityType": "cabinet_order",
            },
            "referenceTargetArticle": {
                "graphNodeId": TARGET_ARTICLE_ID,
                "documentId": "law-act",
                "contentHash": "hash-act-1",
                "sourceSnapshotId": SNAPSHOT_ID,
                "graphSchemaVersion": 9,
                "authorityType": "act",
            },
        }
    ]


def _sources():
    return [
        {
            "contentUnitId": SOURCE_ARTICLE_ID,
            "articleContentUnitId": SOURCE_ARTICLE_ID,
            "text": "法第一条に基づき対象を定める。",
            "sourceSnapshotId": SNAPSHOT_ID,
            "articleContentHash": "hash-order-2",
        },
        {
            "contentUnitId": TARGET_ARTICLE_ID,
            "articleContentUnitId": TARGET_ARTICLE_ID,
            "text": "対象は政令で定める。",
            "sourceSnapshotId": SNAPSHOT_ID,
            "articleContentHash": "hash-act-1",
        },
    ]


def _candidates():
    return candidates_from_graph_and_sources(
        _rows(),
        _sources(),
        source_snapshot_id=SNAPSHOT_ID,
        graph_schema_version=9,
        provider="ollama",
        model="gemma4:e4b",
        reviewer_model="gemma4:e4b",
    )


def _meaning_payload(candidate, predicate, finding="not_established"):
    first = finding
    second = finding
    if finding == "uncertain":
        second = "established"
    condition_fields = {
        ProposedPredicate.IMPLEMENTS: (
            "explicitDelegation",
            "sameMatterImplementation",
        ),
        ProposedPredicate.INCORPORATES: (
            "explicitApplicationLanguage",
            "targetRuleApplied",
        ),
        ProposedPredicate.USES_DEFINITION: (
            "targetDefinesTerm",
            "sourceUsesSameTerm",
        ),
        ProposedPredicate.EXCEPTION_TO: (
            "targetContainsAffectedRule",
            "citationDirectlyLimitsTargetRule",
        ),
        ProposedPredicate.OVERRIDES: (
            "explicitPriorityOverTarget",
            "targetApplicationModified",
        ),
    }
    first_field, second_field = condition_fields[predicate]
    return {
        "predicate": predicate.value,
        first_field: first,
        second_field: second,
        "finding": finding,
    }


def _meaning_assessments(
    candidate,
    *,
    established=ProposedPredicate.IMPLEMENTS,
    uncertain=None,
):
    responses = []
    for predicate in ProposedPredicate:
        finding = "not_established"
        if predicate is established:
            finding = "established"
        elif predicate is uncertain:
            finding = "uncertain"
        responses.append(
            parse_relation_meaning_response(
                candidate,
                predicate,
                _meaning_payload(candidate, predicate, finding),
            )
        )
    return tuple(responses)


def _findings(assessments):
    by_predicate = {item.predicate: item.finding for item in assessments}
    return PredicateFindings(
        implements=by_predicate[ProposedPredicate.IMPLEMENTS],
        incorporates=by_predicate[ProposedPredicate.INCORPORATES],
        uses_definition=by_predicate[ProposedPredicate.USES_DEFINITION],
        exception_to=by_predicate[ProposedPredicate.EXCEPTION_TO],
        overrides=by_predicate[ProposedPredicate.OVERRIDES],
    )


def _grounding_payload(candidate):
    occurrence = candidate.reference_occurrences[0]
    return {
        "assertions": [
            {
                "proposedPredicate": "IMPLEMENTS",
                "referenceOccurrenceHash": occurrence.occurrence_hash,
                # REFERENCESは下位→親だが、IMPLEMENTSは親→下位になる。
                "subjectArticleId": candidate.reference_target.article_id,
                "objectArticleId": candidate.reference_source.article_id,
                "referenceSourceSupportingSpanId": candidate.reference_source.spans[
                    0
                ].span_id,
                "referenceTargetSupportingSpanId": candidate.reference_target.spans[
                    0
                ].span_id,
            }
        ],
    }


def test_candidate_keeps_reference_direction_separate_from_semantic_direction():
    candidate = _candidates()[0]

    decision = parse_relation_classification_decision(
        candidate,
        _meaning_assessments(candidate),
        _grounding_payload(candidate),
    )

    assert candidate.reference_source.article_id == SOURCE_ARTICLE_ID
    assert candidate.reference_target.article_id == TARGET_ARTICLE_ID
    assert decision.assertions[0].subject_article_id == TARGET_ARTICLE_ID
    assert decision.assertions[0].object_article_id == SOURCE_ARTICLE_ID


def test_candidate_uses_content_unit_offsets_for_repeated_citation() -> None:
    parent_id = f"{SOURCE_ARTICLE_ID}-paragraph-1"
    item_id = f"{parent_id}-item-9"
    parent_text = "1 第九十八条に規定する手続を行う。"
    repeated_parent = "第九十八条に規定する手続を行う。"
    item_own_text = "九 第九十八条の規定にかかわらず変更する。"
    item_text = f"{repeated_parent}\n{item_own_text}"
    citation = "第九十八条"
    own_start = item_text.rindex(citation)
    rows = _rows()
    rows[0]["basis"].update(
        {
            "sourceContentUnitId": item_id,
            "citationText": citation,
            "citationTexts": [citation],
            "sourceSpanStarts": [own_start],
            "sourceSpanEnds": [own_start + len(citation)],
        }
    )
    sources = [
        {
            "contentUnitId": parent_id,
            "articleContentUnitId": SOURCE_ARTICLE_ID,
            "parentContentUnitId": SOURCE_ARTICLE_ID,
            "text": parent_text,
            "sourceSnapshotId": SNAPSHOT_ID,
            "articleContentHash": "hash-order-2",
        },
        {
            "contentUnitId": item_id,
            "articleContentUnitId": SOURCE_ARTICLE_ID,
            "parentContentUnitId": parent_id,
            "text": item_text,
            "sourceSnapshotId": SNAPSHOT_ID,
            "articleContentHash": "hash-order-2",
        },
        _sources()[1],
    ]

    candidate = candidates_from_graph_and_sources(
        rows,
        sources,
        source_snapshot_id=SNAPSHOT_ID,
        graph_schema_version=9,
        provider="ollama",
        model="gemma4:e4b",
        reviewer_model="gemma4:e4b",
    )[0]

    assert candidate.reference_occurrences[0].source_span_ids == (
        f"{SOURCE_ARTICLE_ID}::span-2",
    )
    assert candidate.reference_occurrences[0].source_start == own_start
    assert candidate.reference_occurrences[0].source_end == own_start + len(citation)
    assert candidate.reference_occurrences[0].source_prefix == "九"
    assert candidate.reference_occurrences[0].source_suffix.startswith(
        "の規定にかかわらず"
    )


def test_candidate_rejects_reference_from_removed_parent_context() -> None:
    parent_id = f"{SOURCE_ARTICLE_ID}-paragraph-1"
    item_id = f"{parent_id}-item-9"
    parent_text = "1 第九十八条に規定する手続を行う。"
    repeated_parent = "第九十八条に規定する手続を行う。"
    item_text = f"{repeated_parent}\n九 独自の規定。"
    citation = "第九十八条"
    rows = _rows()
    rows[0]["basis"].update(
        {
            "sourceContentUnitId": item_id,
            "citationText": citation,
            "citationTexts": [citation],
            "sourceSpanStarts": [0],
            "sourceSpanEnds": [len(citation)],
        }
    )
    sources = [
        {
            "contentUnitId": parent_id,
            "articleContentUnitId": SOURCE_ARTICLE_ID,
            "parentContentUnitId": SOURCE_ARTICLE_ID,
            "text": parent_text,
            "sourceSnapshotId": SNAPSHOT_ID,
            "articleContentHash": "hash-order-2",
        },
        {
            "contentUnitId": item_id,
            "articleContentUnitId": SOURCE_ARTICLE_ID,
            "parentContentUnitId": parent_id,
            "text": item_text,
            "sourceSnapshotId": SNAPSHOT_ID,
            "articleContentHash": "hash-order-2",
        },
        _sources()[1],
    ]

    with pytest.raises(ValueError, match="cannot be mapped"):
        candidates_from_graph_and_sources(
            rows,
            sources,
            source_snapshot_id=SNAPSHOT_ID,
            graph_schema_version=9,
            provider="ollama",
            model="gemma4:e4b",
            reviewer_model="gemma4:e4b",
        )


def test_candidate_recovers_all_repeated_occurrences_from_legacy_edge() -> None:
    citation = "法第一条に基づき対象を定める。"
    source_text = f"前段。{citation}中段。{citation}後段。"
    rows = _rows()
    rows[0]["basis"].pop("sourceSpanStarts")
    rows[0]["basis"].pop("sourceSpanEnds")
    sources = _sources()
    sources[0]["text"] = source_text

    candidate = candidates_from_graph_and_sources(
        rows,
        sources,
        source_snapshot_id=SNAPSHOT_ID,
        graph_schema_version=9,
        provider="ollama",
        model="gemma4:e4b",
        reviewer_model="gemma4:e4b",
    )[0]

    assert len(candidate.reference_occurrences) == 2
    assert [
        occurrence.source_start for occurrence in candidate.reference_occurrences
    ] == [source_text.index(citation), source_text.rindex(citation)]
    assert candidate.reference_occurrences[0].source_prefix == "前段。"
    assert candidate.reference_occurrences[1].source_prefix.endswith("中段。")


def test_unknown_semantic_endpoint_is_rejected_without_program_correction():
    candidate = _candidates()[0]
    payload = _grounding_payload(candidate)
    payload["assertions"][0]["subjectArticleId"] = "unknown-article"

    with pytest.raises(ValueError, match="candidate Articles"):
        parse_relation_classification_decision(
            candidate,
            _meaning_assessments(candidate),
            payload,
        )


def test_all_negative_assessments_project_to_reference_only_without_assertions():
    candidate = _candidates()[0]
    decision = parse_relation_classification_decision(
        candidate, _meaning_assessments(candidate, established=None)
    )

    assert decision.outcome == "reference_only"
    assert decision.assertions == ()


def test_uncertain_assessment_projects_to_uncertain_without_assertions():
    candidate = _candidates()[0]
    decision = parse_relation_classification_decision(
        candidate,
        _meaning_assessments(
            candidate,
            established=None,
            uncertain=ProposedPredicate.USES_DEFINITION,
        ),
    )

    assert decision.outcome == "uncertain"
    assert decision.assertions == ()


def test_finding_must_match_llm_evaluated_necessary_conditions():
    candidate = _candidates()[0]
    payload = _meaning_payload(candidate, ProposedPredicate.IMPLEMENTS, "established")
    payload["explicitDelegation"] = "not_established"

    with pytest.raises(ValueError, match="must match"):
        parse_relation_meaning_response(
            candidate, ProposedPredicate.IMPLEMENTS, payload
        )


def test_prompt_defines_all_predicates_and_physical_direction_boundary():
    for predicate in ProposedPredicate:
        prompt = relation_classification_prompt(_candidates()[0], predicate)
        assert f"判定対象: {predicate.value}" in prompt
        assert "物理方向は、意味関係のSUBJECT / OBJECT方向を意味しません" in prompt
        assert '"referenceSourceArticle"' in prompt
        assert '"referenceTargetArticle"' in prompt


def test_prompt_prevents_observed_predicate_overclassification():
    incorporates = relation_classification_prompt(
        _candidates()[0], ProposedPredicate.INCORPORATES
    )
    uses_definition = relation_classification_prompt(
        _candidates()[0], ProposedPredicate.USES_DEFINITION
    )
    implements = relation_classification_prompt(
        _candidates()[0], ProposedPredicate.IMPLEMENTS
    )

    assert "準用する" in incorporates
    assert "前条に定める" in incorporates
    assert "前条の規定を準用する" in incorporates
    assert "sourceSpanIdsの本文全体" in incorporates
    assert "X（…をいう）" in uses_definition
    assert "同一法令内の前条参照" in implements
    assert "別のpredicateを同じ応答で検討しません" in implements


def test_provider_schema_restricts_decision_and_endpoint_ids_to_the_candidate():
    candidate = _candidates()[0]
    schema = relation_classification_schema(candidate, ProposedPredicate.IMPLEMENTS)
    assert "candidateKey" not in schema["properties"]
    assert schema["properties"]["predicate"]["enum"] == ["IMPLEMENTS"]
    assert set(schema["required"]) == {
        "predicate",
        "explicitDelegation",
        "sameMatterImplementation",
        "finding",
    }

    findings = _findings(_meaning_assessments(candidate))
    grounding_schema = relation_grounding_schema(candidate, findings)
    relation_schema = grounding_schema["$defs"]["ProposedRelationAssertion"]
    relation = relation_schema["properties"]
    assert set(relation["subjectArticleId"]["enum"]) == {
        SOURCE_ARTICLE_ID,
        TARGET_ARTICLE_ID,
    }
    assert relation["referenceOccurrenceHash"]["enum"] == [
        candidate.reference_occurrences[0].occurrence_hash
    ]
    assert set(relation_schema["required"]) == {
        "proposedPredicate",
        "referenceOccurrenceHash",
        "subjectArticleId",
        "objectArticleId",
        "referenceSourceSupportingSpanId",
        "referenceTargetSupportingSpanId",
    }
    assert relation["proposedPredicate"]["enum"] == ["IMPLEMENTS"]
    assert relation["referenceSourceSupportingSpanId"]["enum"] == [
        candidate.reference_source.spans[0].span_id
    ]
    assert relation["referenceTargetSupportingSpanId"]["enum"] == [
        candidate.reference_target.spans[0].span_id
    ]


def test_grounding_prompt_only_accepts_established_predicates():
    candidate = _candidates()[0]
    findings = _findings(_meaning_assessments(candidate))
    prompt = relation_grounding_prompt(candidate, findings)

    assert '"establishedPredicates":["IMPLEMENTS"]' in prompt
    assert "意味分類を再判断せず" in prompt


def test_repair_prompt_returns_cross_field_validation_to_the_llm():
    candidate = _candidates()[0]
    prompt = relation_classification_repair_prompt(
        candidate,
        ProposedPredicate.IMPLEMENTS,
        invalid_payload={"outcome": "classified", "assertions": []},
        validation_error=ValueError("cross-field mismatch"),
    )

    assert "Programはpredicateを選びません" in prompt
    assert "cross-field mismatch" in prompt
    assert '"assertions":[]' in prompt


class _OpenSearch:
    def get_complete_articles_by_ids(self, article_ids, user_clearance_level):
        assert set(article_ids) == {SOURCE_ARTICLE_ID, TARGET_ARTICLE_ID}
        assert user_clearance_level == 3
        return _sources()


class _LLM:
    provider = "ollama"

    def __init__(self):
        self.call_count = 0

    def generate_structured_json(self, *, prompt, schema, model, **kwargs):
        self.call_count += 1
        if "根拠付与候補:\n" not in prompt:
            candidate_payload = json.loads(prompt.rsplit("分類候補:\n", 1)[1])
            predicate = ProposedPredicate(candidate_payload["predicate"])
            finding = (
                "established"
                if predicate is ProposedPredicate.IMPLEMENTS
                else "not_established"
            )
            payload = _meaning_payload(_candidates()[0], predicate, finding)
            return SimpleNamespace(payload=payload)
        candidate_payload = json.loads(prompt.rsplit("根拠付与候補:\n", 1)[1])
        source = candidate_payload["referenceSourceArticle"]
        target = candidate_payload["referenceTargetArticle"]
        occurrence = candidate_payload["referenceOccurrences"][0]
        grounding = {
            "referenceOccurrenceHash": occurrence["occurrenceHash"],
            "subjectArticleId": target["articleId"],
            "objectArticleId": source["articleId"],
            "referenceSourceSupportingSpanId": source["spans"][0]["spanId"],
            "referenceTargetSupportingSpanId": target["spans"][0]["spanId"],
        }
        return SimpleNamespace(
            payload={
                "assertions": [
                    {
                        "proposedPredicate": "IMPLEMENTS",
                        **grounding,
                    }
                ],
            }
        )


class _RepairingLLM:
    provider = "ollama"

    def __init__(self):
        self.call_count = 0

    def generate_structured_json(self, *, prompt, schema, model, **kwargs):
        self.call_count += 1
        if "根拠付与候補:\n" in prompt:
            candidate_payload = json.loads(
                prompt.split("根拠付与候補:\n", 1)[1].split("\n\n前回", 1)[0]
            )
            valid = _grounding_payload(_candidates()[0])
            return SimpleNamespace(payload=valid)
        candidate_payload = json.loads(
            prompt.split("分類候補:\n", 1)[1].split("\n\n前回", 1)[0]
        )
        predicate = ProposedPredicate(candidate_payload["predicate"])
        finding = (
            "established"
            if predicate is ProposedPredicate.IMPLEMENTS
            else "not_established"
        )
        valid = _meaning_payload(_candidates()[0], predicate, finding)
        if self.call_count == 1:
            invalid = json.loads(json.dumps(valid))
            invalid["explicitDelegation"] = "not_established"
            return SimpleNamespace(payload=invalid)
        if self.call_count == 2:
            assert "構造契約違反" in prompt
        return SimpleNamespace(payload=valid)


class _GroundingFailsOnceLLM(_LLM):
    def __init__(self):
        super().__init__()
        self.grounding_calls = 0

    def generate_structured_json(self, *, prompt, schema, model, **kwargs):
        if "根拠付与候補:\n" not in prompt:
            return super().generate_structured_json(
                prompt=prompt,
                schema=schema,
                model=model,
                **kwargs,
            )
        self.call_count += 1
        self.grounding_calls += 1
        candidate_payload = json.loads(
            prompt.split("根拠付与候補:\n", 1)[1].split("\n\n前回", 1)[0]
        )
        source = candidate_payload["referenceSourceArticle"]
        target = candidate_payload["referenceTargetArticle"]
        occurrence = candidate_payload["referenceOccurrences"][0]
        payload = {
            "assertions": [
                {
                    "proposedPredicate": "IMPLEMENTS",
                    "referenceOccurrenceHash": occurrence["occurrenceHash"],
                    "subjectArticleId": target["articleId"],
                    "objectArticleId": source["articleId"],
                    "referenceSourceSupportingSpanId": source["spans"][0]["spanId"],
                    "referenceTargetSupportingSpanId": target["spans"][0]["spanId"],
                }
            ],
        }
        if self.grounding_calls <= 2:
            payload["assertions"][0]["referenceSourceSupportingSpanId"] = (
                _candidates()[0].reference_target.spans[0].span_id
            )
        return SimpleNamespace(payload=payload)


class _Graph:
    def __init__(self):
        self.run = None
        self.checkpoints = []
        self.assertions = []
        self.failed_error = None

    def classification_source_state(self):
        return {
            "sourceSnapshotId": SNAPSHOT_ID,
            "graphSchemaVersion": 9,
            "articleCount": 2,
            "referenceCount": 1,
        }

    def reference_candidates_for_classification(self, **kwargs):
        return _rows()

    def create_or_resume_classification_run(self, record):
        if self.run is None:
            self.run = dict(record)
        return dict(self.run)

    def classification_checkpoints(self, run_id):
        return [dict(value) for value in self.checkpoints]

    def save_classification_checkpoint(self, *, checkpoint, assertions):
        existing_index = next(
            (
                index
                for index, value in enumerate(self.checkpoints)
                if value["checkpointId"] == checkpoint["checkpointId"]
            ),
            None,
        )
        replacing_failed = (
            existing_index is not None
            and self.checkpoints[existing_index]["outcome"] == "failed"
        )
        if replacing_failed:
            self.checkpoints[existing_index] = dict(checkpoint)
        elif existing_index is not None:
            return False
        else:
            self.checkpoints.append(dict(checkpoint))
        self.assertions.extend(dict(assertion) for assertion in assertions)
        field = {
            "classified": "classifiedCandidateCount",
            "reference_only": "referenceOnlyCount",
            "uncertain": "uncertainCount",
            "failed": "failedCount",
        }[checkpoint["outcome"]]
        if replacing_failed:
            if checkpoint["outcome"] != "failed":
                self.run["failedCount"] -= 1
                self.run[field] += 1
        else:
            self.run["processedCount"] += 1
            self.run[field] += 1
        self.run["assertionCount"] += len(assertions)
        return True

    def classification_run_materialization(self, run_id):
        rows = [
            {
                "assertion": dict(assertion),
                "subjects": [assertion["subjectArticleId"]],
                "objects": [assertion["objectArticleId"]],
                "runs": [assertion["classificationRunId"]],
                "basisCount": 1,
            }
            for assertion in self.assertions
        ]
        return {
            "run": dict(self.run),
            "checkpoints": [dict(value) for value in self.checkpoints],
            "assertions": rows,
        }

    def publish_classification_run(self, run_id, *, published_at):
        self.run["phase"] = "published"
        self.run["publishedAt"] = published_at
        return dict(self.run)

    def fail_classification_run(self, run_id, *, error_code):
        self.run["phase"] = "failed"
        self.run["errorCode"] = error_code
        self.failed_error = error_code


def test_job_resumes_checkpoint_without_repeating_llm_and_publishes(monkeypatch):
    monkeypatch.setattr(module.settings, "relation_classifier_model", "gemma4:e4b")
    monkeypatch.setattr(
        module.settings, "relation_classifier_reviewer_model", "gemma4:e4b"
    )
    graph = _Graph()
    llm = _LLM()
    job = LegalRelationClassificationJob(graph, _OpenSearch(), llm)

    building = job.run(apply=True)
    published = job.run(apply=True, publish=True)

    assert building["phase"] == "building"
    assert published["phase"] == "published"
    assert published["processedCount"] == 1
    assert published["classifiedCandidateCount"] == 1
    assert published["assertionCount"] == 1
    assert published["skippedCheckpointCount"] == 1
    assert llm.call_count == 6
    assert graph.failed_error is None
    saved_decision = json.loads(graph.checkpoints[0]["decisionPayloadJson"])
    assert saved_decision["predicateFindings"]["implements"] == "established"
    assert len(saved_decision["meaningAssessments"]) == 5
    assert saved_decision["meaningAssessments"][0]["firstConditionName"]


def test_publish_audit_detects_checkpoint_decision_payload_tampering(monkeypatch):
    monkeypatch.setattr(module.settings, "relation_classifier_model", "gemma4:e4b")
    monkeypatch.setattr(
        module.settings, "relation_classifier_reviewer_model", "gemma4:e4b"
    )
    graph = _Graph()
    LegalRelationClassificationJob(graph, _OpenSearch(), _LLM()).run(apply=True)
    materialization = graph.classification_run_materialization(
        graph.run["classificationRunId"]
    )
    materialization["checkpoints"][0]["decisionPayloadJson"] = "{}"

    violations = audit_classification_materialization(materialization, _candidates())

    assert "checkpoint_decision_payload_hash_mismatch" in violations


def test_job_repairs_cross_field_contract_once_before_checkpoint(monkeypatch):
    monkeypatch.setattr(module.settings, "relation_classifier_model", "gemma4:e4b")
    monkeypatch.setattr(
        module.settings, "relation_classifier_reviewer_model", "gemma4:e4b"
    )
    graph = _Graph()
    llm = _RepairingLLM()

    report = LegalRelationClassificationJob(graph, _OpenSearch(), llm).run(
        apply=True,
        publish=False,
    )

    assert report["classifiedCandidateCount"] == 1
    assert report["failedCount"] == 0
    assert llm.call_count == 7


def test_job_records_failure_details_and_retries_failed_checkpoint(monkeypatch):
    monkeypatch.setattr(module.settings, "relation_classifier_model", "gemma4:e4b")
    monkeypatch.setattr(
        module.settings, "relation_classifier_reviewer_model", "gemma4:e4b"
    )
    graph = _Graph()
    llm = _GroundingFailsOnceLLM()
    job = LegalRelationClassificationJob(graph, _OpenSearch(), llm)

    failed = job.run(apply=True)

    assert failed["processedCount"] == 1
    assert failed["failedCount"] == 1
    assert graph.checkpoints[0]["errorCode"] == "ValueError"
    assert graph.checkpoints[0]["errorStage"] == "grounding"
    assert graph.checkpoints[0]["errorPredicate"] is None
    assert "selected occurrence" in graph.checkpoints[0]["errorMessage"]
    failed_payload = json.loads(graph.checkpoints[0]["decisionPayloadJson"])
    assert failed_payload["errorStage"] == "grounding"

    retried = job.run(apply=True)

    assert retried["processedCount"] == 1
    assert retried["classifiedCandidateCount"] == 1
    assert retried["failedCount"] == 0
    assert retried["skippedCheckpointCount"] == 0
    assert len(graph.checkpoints) == 1
    assert graph.checkpoints[0]["outcome"] == "classified"


def test_dry_run_does_not_call_llm_or_create_a_run(monkeypatch):
    monkeypatch.setattr(module.settings, "relation_classifier_model", "gemma4:e4b")
    monkeypatch.setattr(
        module.settings, "relation_classifier_reviewer_model", "gemma4:e4b"
    )
    graph = _Graph()
    llm = _LLM()

    report = LegalRelationClassificationJob(graph, _OpenSearch(), llm).run(limit=1)

    assert report["dryRun"] is True
    assert report["inputCount"] == 1
    assert report["eligibleCandidateCount"] == 1
    assert report["outOfScopeCandidateCount"] == 0
    assert llm.call_count == 0
    assert graph.run is None


def test_publish_audit_detects_checkpoint_scope_mismatch():
    candidate = _candidates()[0]
    materialization = {
        "run": {
            "classificationRunId": "run-1",
            "processedCount": 0,
            "classifiedCandidateCount": 0,
            "assertionCount": 0,
            "referenceOnlyCount": 0,
            "uncertainCount": 0,
            "failedCount": 0,
        },
        "checkpoints": [],
        "assertions": [],
    }

    assert "checkpoint_scope_mismatch" in audit_classification_materialization(
        materialization, (candidate,)
    )
