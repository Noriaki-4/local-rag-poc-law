"""Graph schema v9向けの再開可能な非同期法令関係分類job。"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .config import settings
from .domains.legal.graph_schema import (
    ClassificationRunPhase,
    PredicateFinding,
    ProposedPredicate,
    RelationClassificationOutcome,
)
from .domains.legal.relation_classification import (
    ArticleSpan,
    ClassificationArticle,
    ClassificationCheckpointRecord,
    ClassificationRunRecord,
    ExceptionToClassificationResponse,
    ImplementsClassificationResponse,
    IncorporatesClassificationResponse,
    OverridesClassificationResponse,
    PredicateFindings,
    ProposedRelationAssertion,
    ReferenceOccurrence,
    RelationAssertionRecord,
    RelationClassificationCandidate,
    RelationClassificationDecision,
    RelationClassificationResponse,
    RelationGroundingResponse,
    UsesDefinitionClassificationResponse,
    assertion_dedupe_key,
    build_assertion_records,
    stable_hash,
    validate_classification_decision,
)
from .legal_ontology import GRAPH_SCHEMA_VERSION
from .legal_relation_classifier import (
    article_evidence_spans,
    article_texts_from_sources,
    matching_evidence_span_ids_at_source_offsets,
    relation_classification_timeout,
    without_repeated_parent_context_with_offset,
)

RELATION_CLASSIFICATION_PROMPT_VERSION = "legal-relation-5predicate-v19"
logger = logging.getLogger(__name__)


class RelationClassificationStageError(RuntimeError):
    """候補単位の失敗を、法的意味へ変換せず実行段階付きで伝える。"""

    def __init__(
        self,
        *,
        stage: str,
        error: Exception,
        predicate: ProposedPredicate | None = None,
    ) -> None:
        super().__init__(str(error))
        self.stage = stage
        self.predicate = predicate
        self.original_error = error


PREDICATE_PROMPT_CONTRACTS = {
    ProposedPredicate.IMPLEMENTS: """判定対象: IMPLEMENTS
今回の参照について、referenceTargetArticleを委任する親規定、referenceSourceArticleを具体化する下位規定として検査します。
成立条件1 explicitDelegation: referenceTargetArticleが、政令・府省令等の下位法令へ対象事項の具体化を明示的に委ねている。
成立条件2 sameMatterImplementation: referenceSourceArticleが、委任された同じ事項を具体化している。
両方establishedの場合だけfinding=establishedです。
同一法令内の前条参照、準用、用語定義、要件の引用、効果の追加だけならnot_establishedです。""",
    ProposedPredicate.INCORPORATES: """判定対象: INCORPORATES
今回の参照について、referenceSourceArticleをSUBJECT、referenceTargetArticleをOBJECTとして検査します。
成立条件1 explicitApplicationLanguage: referenceSourceArticleに「準用する」「読み替えて適用する」等、referenceTargetArticleの規律を適用する明示文言がある。
成立条件2 targetRuleApplied: その文言によりreferenceTargetArticleの規律自体がreferenceSourceArticleへ適用される。
両方establishedの場合だけfinding=establishedです。
「前条の規定を準用する」は典型的な成立例です。citationTextが「前条」だけでも、対応するsourceSpanIdsの本文全体に「準用する」があれば明示文言として扱います。
「準用する」「読み替えて適用する」等を伴わず、「前条の場合」「前条に定める」「第X条に規定する」「第X条の規定による」と参照するだけならnot_establishedです。""",
    ProposedPredicate.USES_DEFINITION: """判定対象: USES_DEFINITION
今回の参照について、referenceSourceArticleをSUBJECT、referenceTargetArticleをOBJECTとして検査します。
成立条件1 targetDefinesTerm: referenceTargetArticleが、参照された語を「Xとは」「Xをいう」又は括弧書きの「X（…をいう）」で定義している。
成立条件2 sourceUsesSameTerm: referenceSourceArticleが「第X条に規定するX」等の形でその同じ定義語を利用している。
両方establishedの場合だけfinding=establishedです。
OBJECTが権利、義務、要件又は手続を定めるだけならnot_establishedです。""",
    ProposedPredicate.EXCEPTION_TO: """判定対象: EXCEPTION_TO
今回の参照について、referenceSourceArticleをSUBJECT、referenceTargetArticleをOBJECTとして検査します。
成立条件1 targetContainsAffectedRule: referenceTargetArticleに、referenceSourceArticleが打ち消し又は制限しているものと同一の規律・法的効果が書かれている。
成立条件2 citationDirectlyLimitsTargetRule: referenceSourceArticleの例外・適用除外文言が、そのreferenceTargetArticleの規律・法的効果を直接の対象として適用範囲を狭める。
両方establishedの場合だけfinding=establishedです。
referenceSourceArticleに「ただし」「この限りでない」があっても、その対象が同Articleの直前の規律であり、referenceTargetArticleを定義語の出典として引用するだけならnot_establishedです。
例えばsourceが「担保は消滅する。ただし、targetに規定する敷金はこの限りでない」とし、targetが敷金を定義して返還義務を定める場合、打ち消される「担保は消滅する」はtargetの規律ではないためnot_establishedです。
「前条に定める」「前条を準用する」だけでもnot_establishedです。""",
    ProposedPredicate.OVERRIDES: """判定対象: OVERRIDES
今回の参照について、referenceSourceArticleをSUBJECT、referenceTargetArticleをOBJECTとして検査します。
成立条件1 explicitPriorityOverTarget: 「referenceTargetArticleの規定にかかわらず」等、referenceSourceArticleがreferenceTargetArticleより優先すると明示されている。
成立条件2 targetApplicationModified: referenceSourceArticleがreferenceTargetArticleの適用内容を排除又は修正する。
両方establishedの場合だけfinding=establishedです。
単なる別の効果、追加要件、準用、定義利用、「前条に定める」だけならnot_establishedです。""",
}

PREDICATE_RESPONSE_CONTRACTS: dict[ProposedPredicate, tuple[type[Any], str, str]] = {
    ProposedPredicate.IMPLEMENTS: (
        ImplementsClassificationResponse,
        "explicit_delegation",
        "same_matter_implementation",
    ),
    ProposedPredicate.INCORPORATES: (
        IncorporatesClassificationResponse,
        "explicit_application_language",
        "target_rule_applied",
    ),
    ProposedPredicate.USES_DEFINITION: (
        UsesDefinitionClassificationResponse,
        "target_defines_term",
        "source_uses_same_term",
    ),
    ProposedPredicate.EXCEPTION_TO: (
        ExceptionToClassificationResponse,
        "target_contains_affected_rule",
        "citation_directly_limits_target_rule",
    ),
    ProposedPredicate.OVERRIDES: (
        OverridesClassificationResponse,
        "explicit_priority_over_target",
        "target_application_modified",
    ),
}


@lru_cache(maxsize=1)
def _prompt_contract() -> str:
    path = (
        Path(__file__).resolve().parent
        / "domains"
        / "legal"
        / "prompts"
        / "relation_classifier.md"
    )
    return path.read_text(encoding="utf-8").strip()


@lru_cache(maxsize=1)
def _grounding_prompt_contract() -> str:
    path = (
        Path(__file__).resolve().parent
        / "domains"
        / "legal"
        / "prompts"
        / "relation_grounder.md"
    )
    return path.read_text(encoding="utf-8").strip()


def _candidate_prompt_payload(
    candidate: RelationClassificationCandidate,
) -> dict[str, Any]:
    return {
        "basisEdgeId": candidate.basis_edge_id,
        "referenceOccurrences": [
            occurrence.model_dump(by_alias=True, mode="json")
            for occurrence in candidate.reference_occurrences
        ],
        "referenceSourceArticle": _article_prompt_payload(candidate.reference_source),
        "referenceTargetArticle": _article_prompt_payload(candidate.reference_target),
    }


def relation_classification_prompt(
    candidate: RelationClassificationCandidate,
    predicate: ProposedPredicate,
    *,
    reviewer: bool = False,
    primary_response: RelationClassificationResponse | None = None,
) -> str:
    payload = _candidate_prompt_payload(candidate)
    payload["predicate"] = predicate.value
    reviewer_instruction = ""
    if reviewer:
        reviewer_instruction = (
            "\nあなたはReviewerです。一次判断を鵜呑みにせず、同じ本文契約で再判断してください。"
            "一次判断:\n"
            + json.dumps(
                primary_response.model_dump(by_alias=True, mode="json")
                if primary_response is not None
                else None,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    return (
        f"{_prompt_contract()}\n\n{PREDICATE_PROMPT_CONTRACTS[predicate]}"
        f"{reviewer_instruction}\n\n分類候補:\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def relation_classification_schema(
    candidate: RelationClassificationCandidate,
    predicate: ProposedPredicate,
) -> dict[str, Any]:
    """第一段階の意味条件schemaへ既知candidate keyだけを加える。"""

    response_model, _, _ = PREDICATE_RESPONSE_CONTRACTS[predicate]
    schema = response_model.model_json_schema(by_alias=True)
    schema["properties"]["predicate"]["enum"] = [predicate.value]
    return schema


def relation_grounding_prompt(
    candidate: RelationClassificationCandidate,
    findings: PredicateFindings,
) -> str:
    payload = _candidate_prompt_payload(candidate)
    payload["establishedPredicates"] = sorted(
        predicate.value for predicate in findings.established_predicates()
    )
    return f"{_grounding_prompt_contract()}\n\n根拠付与候補:\n" + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    )


def relation_grounding_schema(
    candidate: RelationClassificationCandidate,
    findings: PredicateFindings,
) -> dict[str, Any]:
    """成立済みpredicateと候補内の既知IDだけを許すschema。"""

    schema = RelationGroundingResponse.model_json_schema(by_alias=True)
    article_ids = [
        candidate.reference_source.article_id,
        candidate.reference_target.article_id,
    ]
    reference_source_span_ids = list(
        dict.fromkeys(
            span_id
            for occurrence in candidate.reference_occurrences
            for span_id in occurrence.source_span_ids
        )
    )
    reference_target_span_ids = [
        span.span_id for span in candidate.reference_target.spans
    ]
    assertion = schema["$defs"]["ProposedRelationAssertion"]["properties"]
    assertion["proposedPredicate"]["enum"] = sorted(
        predicate.value for predicate in findings.established_predicates()
    )
    assertion["referenceOccurrenceHash"]["enum"] = [
        item.occurrence_hash for item in candidate.reference_occurrences
    ]
    assertion["subjectArticleId"]["enum"] = article_ids
    assertion["objectArticleId"]["enum"] = article_ids
    assertion["referenceSourceSupportingSpanId"]["enum"] = reference_source_span_ids
    assertion["referenceTargetSupportingSpanId"]["enum"] = reference_target_span_ids
    return schema


def relation_classification_repair_prompt(
    candidate: RelationClassificationCandidate,
    predicate: ProposedPredicate,
    *,
    invalid_payload: dict[str, Any] | None,
    validation_error: Exception,
) -> str:
    """意味をProgramで補正せず、相互制約違反だけをLLMへ差し戻す。"""

    return (
        relation_classification_prompt(candidate, predicate)
        + "\n\n前回の応答はJSON Schemaの個別fieldを満たしましたが、"
        "必要条件とfindingの整合契約に違反しました。"
        "Programはpredicateを選びません。あなたが5関係の必要条件を本文から再評価し、"
        "完全なJSONを一つ返し直してください。\n"
        "前回の応答:\n"
        + json.dumps(
            invalid_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n構造契約違反:\n"
        + str(validation_error)
    )


def relation_grounding_repair_prompt(
    candidate: RelationClassificationCandidate,
    findings: PredicateFindings,
    *,
    invalid_payload: dict[str, Any] | None,
    validation_error: Exception,
) -> str:
    """成立predicateを変えず、既知IDの構造違反だけを差し戻す。"""

    return (
        relation_grounding_prompt(candidate, findings)
        + "\n\n前回の応答は、成立済みpredicate、既知ID、端点又は根拠spanの"
        "構造契約に違反しました。意味分類は変更せず、完全なJSONを一つ返し直してください。\n"
        "前回の応答:\n"
        + json.dumps(
            invalid_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n構造契約違反:\n"
        + str(validation_error)
    )


def parse_relation_meaning_response(
    candidate: RelationClassificationCandidate,
    predicate: ProposedPredicate,
    payload: dict[str, Any] | None,
) -> RelationClassificationResponse:
    if not isinstance(payload, dict):
        raise TypeError("relation classifier returned no JSON object")
    response_model, first_name, second_name = PREDICATE_RESPONSE_CONTRACTS[predicate]
    provider_response = response_model.model_validate(payload)
    if provider_response.predicate is not predicate:
        raise ValueError("relation classifier returned a different predicate")
    return RelationClassificationResponse(
        candidate_key=candidate.candidate_key,
        predicate=provider_response.predicate,
        first_condition_name=first_name,
        first_condition=getattr(provider_response, first_name),
        second_condition_name=second_name,
        second_condition=getattr(provider_response, second_name),
        finding=provider_response.finding,
    )


def parse_relation_classification_decision(
    candidate: RelationClassificationCandidate,
    meaning_assessments: tuple[RelationClassificationResponse, ...],
    grounding_payload: dict[str, Any] | None = None,
) -> RelationClassificationDecision:
    by_predicate = {item.predicate: item for item in meaning_assessments}
    if len(by_predicate) != len(ProposedPredicate) or set(by_predicate) != set(
        ProposedPredicate
    ):
        raise ValueError("meaning assessments must cover each predicate exactly once")
    if any(
        item.candidate_key != candidate.candidate_key for item in meaning_assessments
    ):
        raise ValueError("meaning assessment candidate key mismatch")
    findings = PredicateFindings(
        implements=by_predicate[ProposedPredicate.IMPLEMENTS].finding,
        incorporates=by_predicate[ProposedPredicate.INCORPORATES].finding,
        uses_definition=by_predicate[ProposedPredicate.USES_DEFINITION].finding,
        exception_to=by_predicate[ProposedPredicate.EXCEPTION_TO].finding,
        overrides=by_predicate[ProposedPredicate.OVERRIDES].finding,
    )
    established = findings.established_predicates()
    assertions: tuple[ProposedRelationAssertion, ...] = ()
    if established:
        if not isinstance(grounding_payload, dict):
            raise ValueError("established predicates require a grounding response")
        grounding = RelationGroundingResponse.model_validate(grounding_payload)
        asserted = {item.proposed_predicate for item in grounding.assertions}
        if asserted != established:
            raise ValueError(
                "grounded predicates must exactly match established predicates"
            )
        for assertion in grounding.assertions:
            _validate_assessment_grounding(candidate, assertion)
        assertions = grounding.assertions
    elif grounding_payload is not None:
        raise ValueError(
            "grounding response is forbidden without established predicates"
        )
    if assertions:
        outcome = RelationClassificationOutcome.CLASSIFIED
    elif findings.uncertain_predicates():
        outcome = RelationClassificationOutcome.UNCERTAIN
    else:
        outcome = RelationClassificationOutcome.REFERENCE_ONLY
    decision = RelationClassificationDecision(
        candidate_key=candidate.candidate_key,
        outcome=outcome,
        predicate_findings=findings,
        meaning_assessments=meaning_assessments,
        assertions=assertions,
    )
    validate_classification_decision(candidate, decision)
    return decision


def _validate_assessment_grounding(
    candidate: RelationClassificationCandidate,
    assessment: ProposedRelationAssertion,
) -> None:
    """LLMが全predicateへ返した既知ID・方向・根拠だけを検査する。"""

    known_occurrences = {
        occurrence.occurrence_hash: occurrence
        for occurrence in candidate.reference_occurrences
    }
    occurrence = known_occurrences.get(assessment.reference_occurrence_hash)
    if occurrence is None:
        raise ValueError("assessment references an unknown reference occurrence")
    if assessment.subject_article_id == assessment.object_article_id:
        raise ValueError("assessment subject and object must be different Articles")
    articles = {
        candidate.reference_source.article_id: candidate.reference_source,
        candidate.reference_target.article_id: candidate.reference_target,
    }
    if {assessment.subject_article_id, assessment.object_article_id} != set(articles):
        raise ValueError("assessment endpoints must match the candidate Articles")
    if assessment.reference_source_supporting_span_id not in occurrence.source_span_ids:
        raise ValueError(
            "reference source supporting span does not belong to the selected occurrence"
        )
    if (
        candidate.reference_target.span(assessment.reference_target_supporting_span_id)
        is None
    ):
        raise ValueError(
            "reference target supporting span does not belong to reference target Article"
        )


def _article_prompt_payload(article: ClassificationArticle) -> dict[str, Any]:
    return {
        "articleId": article.article_id,
        "documentId": article.document_id,
        "authorityType": article.authority_type,
        "lawFamilyId": article.law_family_id,
        "spans": [span.model_dump(by_alias=True) for span in article.spans],
    }


def candidates_from_graph_and_sources(
    rows: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    *,
    source_snapshot_id: str,
    graph_schema_version: int,
    provider: str,
    model: str,
    reviewer_model: str | None,
) -> tuple[RelationClassificationCandidate, ...]:
    """Graphの参照事実とOpenSearch全文を同一snapshotの候補へ変換する。"""

    article_ids = list(
        dict.fromkeys(
            str(row[key].get("graphNodeId") or "")
            for row in rows
            for key in ("referenceSourceArticle", "referenceTargetArticle")
        )
    )
    texts = article_texts_from_sources(article_ids, sources)
    sources_by_article: dict[str, list[dict[str, Any]]] = {
        article_id: [] for article_id in article_ids
    }
    for source in sources:
        article_id = str(
            source.get("articleContentUnitId")
            or str(source.get("contentUnitId") or "").split("-paragraph-", 1)[0]
        )
        if article_id in sources_by_article:
            sources_by_article[article_id].append(source)

    articles: dict[str, ClassificationArticle] = {}
    graph_articles: dict[str, dict[str, Any]] = {}
    for row in rows:
        for key in ("referenceSourceArticle", "referenceTargetArticle"):
            graph_article = dict(row[key])
            article_id = str(graph_article.get("graphNodeId") or "")
            if graph_article.get("sourceSnapshotId") != source_snapshot_id:
                raise ValueError(f"Graph Article snapshot mismatch: {article_id}")
            if (
                int(graph_article.get("graphSchemaVersion") or 0)
                != graph_schema_version
            ):
                raise ValueError(f"Graph Article schema mismatch: {article_id}")
            graph_articles[article_id] = graph_article
    for article_id in article_ids:
        graph_article = graph_articles[article_id]
        article_sources = sources_by_article.get(article_id) or []
        if article_id not in texts or not article_sources:
            raise ValueError(f"complete Article text is missing: {article_id}")
        if any(
            source.get("sourceSnapshotId") != source_snapshot_id
            for source in article_sources
        ):
            raise ValueError(f"OpenSearch snapshot mismatch: {article_id}")
        content_hash = str(graph_article.get("contentHash") or "")
        if not content_hash or any(
            str(source.get("articleContentHash") or "") != content_hash
            for source in article_sources
        ):
            raise ValueError(f"Article content hash mismatch: {article_id}")
        spans = tuple(
            ArticleSpan(span_id=span_id, text=text)
            for span_id, text in article_evidence_spans(texts[article_id]).items()
        )
        articles[article_id] = ClassificationArticle(
            article_id=article_id,
            document_id=str(graph_article.get("documentId") or ""),
            content_hash=content_hash,
            source_revision_id=graph_article.get("sourceRevisionId"),
            authority_type=graph_article.get("authorityType"),
            law_family_id=graph_article.get("lawFamilyId"),
            spans=spans,
        )

    candidates: list[RelationClassificationCandidate] = []
    for row in rows:
        basis = dict(row["basis"])
        if basis.get("sourceSnapshotId") != source_snapshot_id:
            raise ValueError("REFERENCES snapshot mismatch")
        if int(basis.get("graphSchemaVersion") or 0) != graph_schema_version:
            raise ValueError("REFERENCES schema mismatch")
        source_id = str(row["referenceSourceArticle"].get("graphNodeId") or "")
        target_id = str(row["referenceTargetArticle"].get("graphNodeId") or "")
        source_article = articles[source_id]
        source_spans = {span.span_id: span.text for span in source_article.spans}
        citation_texts = basis.get("citationTexts")
        if not isinstance(citation_texts, list) or not citation_texts:
            citation_texts = [basis.get("citationText")]
        source_starts = basis.get("sourceSpanStarts")
        source_ends = basis.get("sourceSpanEnds")
        source_unit_id = str(basis.get("sourceContentUnitId") or "")
        source_units = [
            source
            for source in sources_by_article.get(source_id, [])
            if str(source.get("contentUnitId") or "") == source_unit_id
        ]
        if source_starts is None and source_ends is None and len(source_units) == 1:
            recovered = _recover_reference_occurrences(
                [str(value or "").strip() for value in citation_texts],
                str(source_units[0].get("text") or ""),
            )
            citation_texts = [item[0] for item in recovered]
            source_starts = [item[1] for item in recovered]
            source_ends = [item[2] for item in recovered]
        source_text = ""
        removed_parent_chars = 0
        if not (
            isinstance(source_starts, list)
            and isinstance(source_ends, list)
            and len(source_starts) == len(citation_texts)
            and len(source_ends) == len(citation_texts)
            and len(source_units) == 1
        ):
            raise ValueError(
                "REFERENCES source offsets are incomplete or source Content Unit "
                f"is missing: {basis.get('graphEdgeId')}"
            )
        source_text = str(source_units[0].get("text") or "")
        parent_id = str(source_units[0].get("parentContentUnitId") or "")
        if parent_id:
            parent_units = [
                source
                for source in sources_by_article.get(source_id, [])
                if str(source.get("contentUnitId") or "") == parent_id
            ]
            if len(parent_units) > 1:
                raise ValueError(
                    "REFERENCES parent Content Unit is duplicated: "
                    f"{basis.get('graphEdgeId')}"
                )
            if parent_units:
                source_text, removed_parent_chars = (
                    without_repeated_parent_context_with_offset(
                        source_text,
                        str(parent_units[0].get("text") or "").strip(),
                    )
                )
        occurrences: list[ReferenceOccurrence] = []
        for index, raw_text in enumerate(citation_texts):
            citation_text = str(raw_text or "").strip()
            if not citation_text:
                raise ValueError(
                    f"REFERENCES citation text is missing: {basis.get('graphEdgeId')}"
                )
            raw_source_start = int(source_starts[index])
            raw_source_end = int(source_ends[index])
            source_start = raw_source_start - removed_parent_chars
            source_end = raw_source_end - removed_parent_chars
            matching_span_ids = matching_evidence_span_ids_at_source_offsets(
                citation_text,
                source_spans,
                source_text=source_text,
                source_start=source_start,
                source_end=source_end,
            )
            if not matching_span_ids:
                raise ValueError(
                    "REFERENCES citation cannot be mapped to source Article spans: "
                    f"{basis.get('graphEdgeId')}"
                )
            occurrence_hash = stable_hash(
                {
                    "basisEdgeId": basis.get("graphEdgeId"),
                    "occurrenceIndex": index,
                    "citationText": citation_text,
                    "sourceContentUnitId": basis.get("sourceContentUnitId"),
                    "sourceStart": raw_source_start,
                    "sourceEnd": raw_source_end,
                }
            )
            occurrences.append(
                ReferenceOccurrence(
                    occurrence_hash=occurrence_hash,
                    citation_text=citation_text,
                    source_content_unit_id=str(basis.get("sourceContentUnitId") or ""),
                    source_start=raw_source_start,
                    source_end=raw_source_end,
                    source_prefix=source_text[
                        max(0, source_start - 120) : source_start
                    ],
                    source_suffix=source_text[source_end : source_end + 120],
                    source_span_ids=tuple(matching_span_ids),
                )
            )
        candidates.append(
            RelationClassificationCandidate(
                source_snapshot_id=source_snapshot_id,
                graph_schema_version=graph_schema_version,
                prompt_version=RELATION_CLASSIFICATION_PROMPT_VERSION,
                provider=provider,
                model=model,
                reviewer_model=reviewer_model,
                basis_edge_id=str(basis.get("graphEdgeId") or ""),
                reference_source=source_article,
                reference_target=articles[target_id],
                reference_occurrences=tuple(occurrences),
            )
        )
    return tuple(sorted(candidates, key=lambda item: item.candidate_key))


def _recover_reference_occurrences(
    citation_texts: list[str], source_text: str
) -> list[tuple[str, int, int]]:
    """旧Graphの引用文から全ての原文位置を復元する。意味は推測しない。"""

    recovered: list[tuple[str, int, int]] = []
    seen: set[tuple[int, int]] = set()
    for citation_text in citation_texts:
        if not citation_text:
            continue
        search_from = 0
        while True:
            start = source_text.find(citation_text, search_from)
            if start < 0:
                break
            end = start + len(citation_text)
            if (start, end) not in seen:
                seen.add((start, end))
                recovered.append((citation_text, start, end))
            search_from = start + 1
            if len(recovered) > 32:
                raise ValueError("REFERENCES occurrence recovery exceeded 32 matches")
    return sorted(recovered, key=lambda item: (item[1], item[2], item[0]))


def audit_classification_materialization(
    materialization: dict[str, Any],
    candidates: tuple[RelationClassificationCandidate, ...],
) -> list[str]:
    """publish前の構造監査。predicateの法的意味は再判定しない。"""

    violations: list[str] = []
    run = materialization.get("run")
    if not isinstance(run, dict):
        return ["classification_run_missing"]
    expected = {candidate.candidate_key: candidate for candidate in candidates}
    checkpoints = list(materialization.get("checkpoints") or [])
    checkpoint_keys = [str(item.get("candidateKey") or "") for item in checkpoints]
    if len(checkpoint_keys) != len(set(checkpoint_keys)):
        violations.append("duplicate_checkpoint_candidate_key")
    if set(checkpoint_keys) != set(expected):
        violations.append("checkpoint_scope_mismatch")

    outcome_counts = {
        "classified": 0,
        "reference_only": 0,
        "uncertain": 0,
        "failed": 0,
    }
    checkpoint_assertion_counts: dict[str, int] = {}
    for checkpoint in checkpoints:
        try:
            parsed = ClassificationCheckpointRecord.model_validate(
                _model_properties(ClassificationCheckpointRecord, checkpoint)
            )
        except ValidationError:
            violations.append("invalid_checkpoint_record")
            continue
        outcome_counts[parsed.outcome.value] += 1
        checkpoint_assertion_counts[parsed.candidate_key] = parsed.assertion_count
        try:
            decision_payload = json.loads(parsed.decision_payload_json)
        except json.JSONDecodeError:
            violations.append("invalid_checkpoint_decision_payload")
        else:
            if stable_hash(decision_payload) != parsed.decision_payload_hash:
                violations.append("checkpoint_decision_payload_hash_mismatch")
        if parsed.classification_run_id != run.get("classificationRunId"):
            violations.append("checkpoint_run_mismatch")
        if parsed.source_snapshot_id != run.get("sourceSnapshotId"):
            violations.append("checkpoint_snapshot_mismatch")

    assertions_by_candidate: dict[str, int] = {}
    assertion_ids: set[str] = set()
    dedupe_keys: set[str] = set()
    for row in materialization.get("assertions") or []:
        raw_assertion = dict(row.get("assertion") or {})
        try:
            assertion = RelationAssertionRecord.model_validate(
                _model_properties(RelationAssertionRecord, raw_assertion)
            )
        except ValidationError:
            violations.append("invalid_relation_assertion_record")
            continue
        candidate = expected.get(assertion.candidate_key)
        if candidate is None:
            violations.append("assertion_candidate_unknown")
            continue
        assertions_by_candidate[assertion.candidate_key] = (
            assertions_by_candidate.get(assertion.candidate_key, 0) + 1
        )
        if assertion.assertion_id in assertion_ids:
            violations.append("duplicate_assertion_id")
        assertion_ids.add(assertion.assertion_id)
        if assertion.assertion_dedupe_key in dedupe_keys:
            violations.append("duplicate_assertion_dedupe_key")
        dedupe_keys.add(assertion.assertion_dedupe_key)
        if assertion.assertion_dedupe_key != assertion_dedupe_key(
            assertion.classification_run_id,
            assertion.candidate_key,
            assertion.proposed_predicate,
        ):
            violations.append("assertion_dedupe_key_mismatch")
        if assertion.basis_edge_id != candidate.basis_edge_id:
            violations.append("assertion_basis_mismatch")
        occurrence = next(
            (
                item
                for item in candidate.reference_occurrences
                if item.occurrence_hash == assertion.reference_occurrence_hash
            ),
            None,
        )
        if occurrence is None:
            violations.append("assertion_reference_occurrence_unknown")
        elif assertion.source_content_unit_id != occurrence.source_content_unit_id:
            violations.append("assertion_source_content_unit_mismatch")
        if assertion.source_snapshot_id != candidate.source_snapshot_id:
            violations.append("assertion_snapshot_mismatch")
        endpoint_ids = {
            candidate.reference_source.article_id,
            candidate.reference_target.article_id,
        }
        if {assertion.subject_article_id, assertion.object_article_id} != endpoint_ids:
            violations.append("assertion_endpoint_mismatch")
        elif occurrence is not None:
            reference_source_span_id = (
                assertion.subject_supporting_span_id
                if assertion.subject_article_id == candidate.reference_source.article_id
                else assertion.object_supporting_span_id
            )
            if reference_source_span_id not in occurrence.source_span_ids:
                violations.append("assertion_reference_source_span_mismatch")
        article_by_id = {
            candidate.reference_source.article_id: candidate.reference_source,
            candidate.reference_target.article_id: candidate.reference_target,
        }
        subject_span = article_by_id.get(assertion.subject_article_id)
        object_span = article_by_id.get(assertion.object_article_id)
        if (
            subject_span is None
            or subject_span.span(assertion.subject_supporting_span_id) is None
            or subject_span.span(assertion.subject_supporting_span_id).text
            != assertion.subject_supporting_quote
        ):
            violations.append("assertion_subject_span_mismatch")
        if (
            object_span is None
            or object_span.span(assertion.object_supporting_span_id) is None
            or object_span.span(assertion.object_supporting_span_id).text
            != assertion.object_supporting_quote
        ):
            violations.append("assertion_object_span_mismatch")
        if row.get("subjects") != [assertion.subject_article_id]:
            violations.append("assertion_subject_relation_mismatch")
        if row.get("objects") != [assertion.object_article_id]:
            violations.append("assertion_object_relation_mismatch")
        if row.get("runs") != [assertion.classification_run_id]:
            violations.append("assertion_run_relation_mismatch")
        if int(row.get("basisCount") or 0) != 1:
            violations.append("assertion_basis_edge_missing")

    for candidate_key, expected_count in checkpoint_assertion_counts.items():
        if assertions_by_candidate.get(candidate_key, 0) != expected_count:
            violations.append("checkpoint_assertion_count_mismatch")
    expected_processed = len(checkpoints)
    if int(run.get("processedCount") or 0) != expected_processed:
        violations.append("run_processed_count_mismatch")
    if int(run.get("classifiedCandidateCount") or 0) != outcome_counts["classified"]:
        violations.append("run_classified_candidate_count_mismatch")
    if int(run.get("assertionCount") or 0) != len(assertion_ids):
        violations.append("run_assertion_count_mismatch")
    for outcome, field in (
        ("reference_only", "referenceOnlyCount"),
        ("uncertain", "uncertainCount"),
        ("failed", "failedCount"),
    ):
        if int(run.get(field) or 0) != outcome_counts[outcome]:
            violations.append(f"run_{outcome}_count_mismatch")
    return list(dict.fromkeys(violations))


def _model_properties(
    model_type: type[Any], properties: dict[str, Any]
) -> dict[str, Any]:
    aliases = {field.alias or name for name, field in model_type.model_fields.items()}
    return {key: value for key, value in properties.items() if key in aliases}


class LegalRelationClassificationJob:
    """seedとは別プロセスで実行する、checkpoint付き分類job。"""

    def __init__(
        self, graph_client: Any, opensearch_client: Any, llm_client: Any
    ) -> None:
        self.graph = graph_client
        self.opensearch = opensearch_client
        self.llm = llm_client

    def run(
        self,
        *,
        limit: int | None = None,
        run_id: str | None = None,
        apply: bool = False,
        publish: bool = False,
    ) -> dict[str, Any]:
        source = self.graph.classification_source_state()
        if int(source["graphSchemaVersion"]) != GRAPH_SCHEMA_VERSION:
            raise RuntimeError("classification graph schema version is not current")
        eligible_rows = self.graph.reference_candidates_for_classification(
            source_snapshot_id=str(source["sourceSnapshotId"]),
            limit=None,
        )
        rows = eligible_rows[:limit] if limit is not None else eligible_rows
        coverage = {
            "sourceReferenceCount": int(source["referenceCount"]),
            "eligibleCandidateCount": len(eligible_rows),
            "outOfScopeCandidateCount": len(eligible_rows) - len(rows),
            "excludedReferenceCount": max(
                0, int(source["referenceCount"]) - len(eligible_rows)
            ),
        }
        article_ids = list(
            dict.fromkeys(
                str(row[key].get("graphNodeId") or "")
                for row in rows
                for key in ("referenceSourceArticle", "referenceTargetArticle")
            )
        )
        article_sources = self.opensearch.get_complete_articles_by_ids(
            article_ids, user_clearance_level=3
        )
        reviewer_model = settings.relation_classifier_reviewer_model or None
        candidates = candidates_from_graph_and_sources(
            rows,
            article_sources,
            source_snapshot_id=str(source["sourceSnapshotId"]),
            graph_schema_version=int(source["graphSchemaVersion"]),
            provider=self.llm.provider,
            model=settings.relation_classifier_model,
            reviewer_model=reviewer_model,
        )
        scope_hash = stable_hash(
            {
                "candidateKeys": [candidate.candidate_key for candidate in candidates],
                "sourceSnapshotId": source["sourceSnapshotId"],
                "graphSchemaVersion": source["graphSchemaVersion"],
            }
        )
        classification_run_id = run_id or f"classification-run-{scope_hash[:32]}"
        if not apply:
            return {
                "classificationRunId": classification_run_id,
                "sourceSnapshotId": source["sourceSnapshotId"],
                "scopeHash": scope_hash,
                "inputCount": len(candidates),
                **coverage,
                "dryRun": True,
            }

        initial_run = ClassificationRunRecord(
            classification_run_id=classification_run_id,
            phase=ClassificationRunPhase.BUILDING,
            source_snapshot_id=str(source["sourceSnapshotId"]),
            graph_schema_version=int(source["graphSchemaVersion"]),
            provider=self.llm.provider,
            model=settings.relation_classifier_model,
            reviewer_model=reviewer_model,
            prompt_version=RELATION_CLASSIFICATION_PROMPT_VERSION,
            candidates_per_model_call=1,
            input_count=len(candidates),
            processed_count=0,
            classified_candidate_count=0,
            assertion_count=0,
            reference_only_count=0,
            uncertain_count=0,
            failed_count=0,
            scope_hash=scope_hash,
        )
        persisted_run = self.graph.create_or_resume_classification_run(
            initial_run.model_dump(by_alias=True, mode="json")
        )
        self._validate_resumed_run(initial_run, persisted_run)
        logger.info(
            "classification run=%s phase=%s input=%d snapshot=%s",
            classification_run_id,
            persisted_run.get("phase"),
            len(candidates),
            source["sourceSnapshotId"],
        )
        if persisted_run.get("phase") == ClassificationRunPhase.PUBLISHED.value:
            materialization = self.graph.classification_run_materialization(
                classification_run_id
            )
            violations = audit_classification_materialization(
                materialization, candidates
            )
            if violations:
                raise RuntimeError(
                    "published classification run audit failed: "
                    + ", ".join(violations)
                )
            ClassificationRunRecord.model_validate(
                _model_properties(ClassificationRunRecord, persisted_run)
            )
            return self._report(
                persisted_run,
                skipped_count=len(candidates),
                coverage=coverage,
            )

        checkpoint_rows = self.graph.classification_checkpoints(classification_run_id)
        checkpoints: dict[
            str, tuple[ClassificationCheckpointRecord, dict[str, Any]]
        ] = {}
        for item in checkpoint_rows:
            try:
                parsed = ClassificationCheckpointRecord.model_validate(
                    _model_properties(ClassificationCheckpointRecord, item)
                )
            except ValidationError as error:
                self.graph.fail_classification_run(
                    classification_run_id, error_code="invalid_checkpoint_contract"
                )
                raise RuntimeError(
                    "persisted checkpoint contract is invalid"
                ) from error
            if (
                parsed.classification_run_id != classification_run_id
                or parsed.source_snapshot_id != source["sourceSnapshotId"]
                or parsed.graph_schema_version != source["graphSchemaVersion"]
                or parsed.candidate_key in checkpoints
            ):
                self.graph.fail_classification_run(
                    classification_run_id, error_code="invalid_checkpoint_contract"
                )
                raise RuntimeError("persisted checkpoint contract is invalid")
            checkpoints[parsed.candidate_key] = (parsed, item)
        unknown_keys = set(checkpoints).difference(
            candidate.candidate_key for candidate in candidates
        )
        if unknown_keys:
            self.graph.fail_classification_run(
                classification_run_id, error_code="checkpoint_scope_mismatch"
            )
            raise RuntimeError("persisted checkpoint is outside the current scope")

        skipped_count = 0
        completed_count = sum(
            parsed.outcome.value != "failed" for parsed, _ in checkpoints.values()
        )
        for candidate in candidates:
            saved_checkpoint = checkpoints.get(candidate.candidate_key)
            if (
                saved_checkpoint is not None
                and saved_checkpoint[0].outcome.value != "failed"
            ):
                skipped_count += 1
                continue
            classified_at = datetime.now(UTC)
            try:
                decision = self.classify_candidate(candidate)
                try:
                    assertions = build_assertion_records(
                        candidate,
                        decision,
                        classification_run_id=classification_run_id,
                        classified_at=classified_at,
                    )
                except Exception as error:
                    raise RelationClassificationStageError(
                        stage="assertion_building",
                        error=error,
                    ) from error
                outcome = decision.outcome
                error_code = None
                error_stage = None
                error_message = None
                error_predicate = None
                decision_payload = decision.model_dump(by_alias=True, mode="json")
                payload_hash = stable_hash(decision_payload)
            except Exception as error:  # noqa: BLE001 - 候補単位でcoverageへ残して継続する
                assertions = ()
                outcome = "failed"
                if isinstance(error, RelationClassificationStageError):
                    original_error = error.original_error
                    error_stage = error.stage
                    error_predicate = error.predicate
                else:
                    original_error = error
                    error_stage = "classification"
                    error_predicate = None
                error_code = type(original_error).__name__
                error_message = str(original_error)[:2000] or error_code
                decision_payload = {
                    "candidateKey": candidate.candidate_key,
                    "errorCode": error_code,
                    "errorStage": error_stage,
                    "errorMessage": error_message,
                    "errorPredicate": (
                        error_predicate.value if error_predicate is not None else None
                    ),
                }
                payload_hash = stable_hash(decision_payload)
            decision_payload_json = json.dumps(
                decision_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            checkpoint_key = stable_hash(
                {
                    "classificationRunId": classification_run_id,
                    "candidateKey": candidate.candidate_key,
                }
            )
            checkpoint = ClassificationCheckpointRecord(
                checkpoint_id=f"classification-checkpoint-{checkpoint_key}",
                classification_run_id=classification_run_id,
                candidate_key=candidate.candidate_key,
                outcome=outcome,
                decision_payload_hash=payload_hash,
                decision_payload_json=decision_payload_json,
                assertion_count=len(assertions),
                error_code=error_code,
                error_stage=error_stage,
                error_message=error_message,
                error_predicate=error_predicate,
                processed_at=classified_at,
                source_snapshot_id=candidate.source_snapshot_id,
                graph_schema_version=candidate.graph_schema_version,
            )
            self.graph.save_classification_checkpoint(
                checkpoint=checkpoint.model_dump(by_alias=True, mode="json"),
                assertions=[
                    assertion.model_dump(by_alias=True, mode="json")
                    for assertion in assertions
                ],
            )
            completed_count += 1
            if completed_count % 10 == 0 or completed_count == len(candidates):
                logger.info(
                    "classification run=%s processed=%d/%d latestOutcome=%s",
                    classification_run_id,
                    completed_count,
                    len(candidates),
                    outcome,
                )

        materialization = self.graph.classification_run_materialization(
            classification_run_id
        )
        violations = audit_classification_materialization(materialization, candidates)
        if violations:
            self.graph.fail_classification_run(
                classification_run_id,
                error_code="publish_audit_failed",
            )
            raise RuntimeError(
                "classification publish audit failed: " + ", ".join(violations)
            )
        run_properties = dict(materialization["run"])
        if publish:
            run_properties = self.graph.publish_classification_run(
                classification_run_id, published_at=datetime.now(UTC)
            )
            ClassificationRunRecord.model_validate(
                _model_properties(ClassificationRunRecord, run_properties)
            )
        return self._report(
            run_properties,
            skipped_count=skipped_count,
            coverage=coverage,
        )

    def classify_candidate(
        self, candidate: RelationClassificationCandidate
    ) -> RelationClassificationDecision:
        """1候補を5つの独立した意味判定と、必要時の根拠付与で分類する。"""
        assessments: list[RelationClassificationResponse] = []
        grounding_model = settings.relation_classifier_model
        for predicate in ProposedPredicate:
            try:
                meaning = self._call_meaning_model(
                    candidate,
                    predicate,
                    model=settings.relation_classifier_model,
                    reviewer=False,
                )
            except Exception as error:
                raise RelationClassificationStageError(
                    stage="meaning",
                    predicate=predicate,
                    error=error,
                ) from error
            if meaning.finding is PredicateFinding.UNCERTAIN:
                try:
                    meaning = self._call_meaning_model(
                        candidate,
                        predicate,
                        model=settings.relation_classifier_reviewer_model,
                        reviewer=True,
                        primary_response=meaning,
                    )
                except Exception as error:
                    raise RelationClassificationStageError(
                        stage="review",
                        predicate=predicate,
                        error=error,
                    ) from error
                grounding_model = settings.relation_classifier_reviewer_model
            assessments.append(meaning)
        meaning_assessments = tuple(assessments)
        by_predicate = {item.predicate: item.finding for item in meaning_assessments}
        findings = PredicateFindings(
            implements=by_predicate[ProposedPredicate.IMPLEMENTS],
            incorporates=by_predicate[ProposedPredicate.INCORPORATES],
            uses_definition=by_predicate[ProposedPredicate.USES_DEFINITION],
            exception_to=by_predicate[ProposedPredicate.EXCEPTION_TO],
            overrides=by_predicate[ProposedPredicate.OVERRIDES],
        )
        if not findings.established_predicates():
            return parse_relation_classification_decision(
                candidate, meaning_assessments
            )
        try:
            grounding = self._call_grounding_model(
                candidate,
                meaning_assessments,
                findings,
                model=grounding_model,
            )
        except Exception as error:
            raise RelationClassificationStageError(
                stage="grounding",
                error=error,
            ) from error
        return parse_relation_classification_decision(
            candidate,
            meaning_assessments,
            grounding.model_dump(by_alias=True, mode="json"),
        )

    def _call_meaning_model(
        self,
        candidate: RelationClassificationCandidate,
        predicate: ProposedPredicate,
        *,
        model: str,
        reviewer: bool,
        primary_response: RelationClassificationResponse | None = None,
    ) -> RelationClassificationResponse:
        prompt = relation_classification_prompt(
            candidate,
            predicate,
            reviewer=reviewer,
            primary_response=primary_response,
        )
        schema = relation_classification_schema(candidate, predicate)
        request_chars = len(prompt) + len(
            json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        )
        timeout_sec = relation_classification_timeout(
            request_chars,
            base_timeout_sec=settings.relation_classifier_timeout_sec,
            batch_chars=settings.relation_classifier_batch_chars,
        )
        result = self.llm.generate_structured_json(
            prompt=prompt,
            schema=schema,
            model=model,
            max_tokens=settings.relation_classifier_max_tokens,
            timeout_sec=timeout_sec,
        )
        try:
            return parse_relation_meaning_response(candidate, predicate, result.payload)
        except (TypeError, ValidationError, ValueError) as error:
            repaired = self.llm.generate_structured_json(
                prompt=relation_classification_repair_prompt(
                    candidate,
                    predicate,
                    invalid_payload=result.payload,
                    validation_error=error,
                ),
                schema=schema,
                model=settings.relation_classifier_reviewer_model or model,
                max_tokens=settings.relation_classifier_max_tokens,
                timeout_sec=timeout_sec,
            )
            return parse_relation_meaning_response(
                candidate, predicate, repaired.payload
            )

    def _call_grounding_model(
        self,
        candidate: RelationClassificationCandidate,
        meaning_assessments: tuple[RelationClassificationResponse, ...],
        findings: PredicateFindings,
        *,
        model: str,
    ) -> RelationGroundingResponse:
        prompt = relation_grounding_prompt(candidate, findings)
        schema = relation_grounding_schema(candidate, findings)
        request_chars = len(prompt) + len(
            json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        )
        timeout_sec = relation_classification_timeout(
            request_chars,
            base_timeout_sec=settings.relation_classifier_timeout_sec,
            batch_chars=settings.relation_classifier_batch_chars,
        )
        result = self.llm.generate_structured_json(
            prompt=prompt,
            schema=schema,
            model=model,
            max_tokens=settings.relation_classifier_max_tokens,
            timeout_sec=timeout_sec,
        )
        try:
            return self._validate_grounding_response(
                candidate, meaning_assessments, result.payload
            )
        except (TypeError, ValidationError, ValueError) as error:
            repaired = self.llm.generate_structured_json(
                prompt=relation_grounding_repair_prompt(
                    candidate,
                    findings,
                    invalid_payload=result.payload,
                    validation_error=error,
                ),
                schema=schema,
                model=settings.relation_classifier_reviewer_model or model,
                max_tokens=settings.relation_classifier_max_tokens,
                timeout_sec=timeout_sec,
            )
            return self._validate_grounding_response(
                candidate, meaning_assessments, repaired.payload
            )

    @staticmethod
    def _validate_grounding_response(
        candidate: RelationClassificationCandidate,
        meaning_assessments: tuple[RelationClassificationResponse, ...],
        payload: dict[str, Any] | None,
    ) -> RelationGroundingResponse:
        if not isinstance(payload, dict):
            raise TypeError("relation grounder returned no JSON object")
        response = RelationGroundingResponse.model_validate(payload)
        parse_relation_classification_decision(
            candidate,
            meaning_assessments,
            response.model_dump(by_alias=True, mode="json"),
        )
        return response

    @staticmethod
    def _validate_resumed_run(
        expected: ClassificationRunRecord, persisted: dict[str, Any]
    ) -> None:
        for field in (
            "sourceSnapshotId",
            "graphSchemaVersion",
            "provider",
            "model",
            "reviewerModel",
            "promptVersion",
            "candidatesPerModelCall",
            "inputCount",
            "scopeHash",
        ):
            expected_value = expected.model_dump(by_alias=True, mode="json").get(field)
            if persisted.get(field) != expected_value:
                raise RuntimeError(f"classification run contract mismatch: {field}")
        if persisted.get("phase") == ClassificationRunPhase.FAILED.value:
            raise RuntimeError("failed classification run cannot be resumed")

    @staticmethod
    def _report(
        run: dict[str, Any],
        *,
        skipped_count: int,
        coverage: dict[str, int],
    ) -> dict[str, Any]:
        return {
            "classificationRunId": run.get("classificationRunId"),
            "phase": run.get("phase"),
            "sourceSnapshotId": run.get("sourceSnapshotId"),
            "inputCount": int(run.get("inputCount") or 0),
            "processedCount": int(run.get("processedCount") or 0),
            "classifiedCandidateCount": int(run.get("classifiedCandidateCount") or 0),
            "assertionCount": int(run.get("assertionCount") or 0),
            "referenceOnlyCount": int(run.get("referenceOnlyCount") or 0),
            "uncertainCount": int(run.get("uncertainCount") or 0),
            "failedCount": int(run.get("failedCount") or 0),
            "skippedCheckpointCount": skipped_count,
            **coverage,
            "dryRun": False,
        }


__all__ = [
    "RELATION_CLASSIFICATION_PROMPT_VERSION",
    "LegalRelationClassificationJob",
    "audit_classification_materialization",
    "candidates_from_graph_and_sources",
    "parse_relation_classification_decision",
    "relation_classification_prompt",
    "relation_classification_repair_prompt",
    "relation_classification_schema",
]
