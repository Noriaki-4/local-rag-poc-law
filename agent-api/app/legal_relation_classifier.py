"""RelationAssertionを法令本文で分類するオフライン処理の純粋ロジック。

候補抽出は決定的処理、関係の意味判断はLLM、ID・本文引用・件数・ハッシュの検査は
プログラムという境界を保つ。分類結果は正式Graphエッジではなく派生データとして保存する。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from .config import settings
from .legal_ontology import (
    RELATION_STATUS_LLM_IMPLEMENTS,
    RELATION_STATUS_LLM_REFERENCE_ONLY,
    RELATION_STATUS_LLM_UNCERTAIN,
)

RELATION_CLASSIFIER_PROMPT_VERSION = "legal-relation-classifier-v8"
RelationVerdict = Literal["implements", "reference_only", "uncertain"]
EVIDENCE_SPAN_MAX_CHARS = 400


@dataclass(frozen=True)
class ArticleText:
    article_id: str
    text: str

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RelationClassificationItem:
    assertion: dict[str, Any]
    from_article: ArticleText
    to_article: ArticleText


def relation_classification_json_schema(
    assertion_ids: list[str],
) -> dict[str, Any]:
    decision_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "verdict",
            "delegationFinding",
            "implementationFinding",
            "fromSupportingSpanId",
            "toSupportingSpanId",
            "reason",
        ],
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["implements", "reference_only", "uncertain"],
            },
            "delegationFinding": {
                "type": "string",
                "enum": [
                    "explicit_same_matter",
                    "not_explicit_same_matter",
                    "uncertain",
                ],
            },
            "implementationFinding": {
                "type": "string",
                "enum": [
                    "fulfills_delegation",
                    "does_not_fulfill_delegation",
                    "uncertain",
                ],
            },
            "fromSupportingSpanId": {"type": "string", "maxLength": 300},
            "toSupportingSpanId": {"type": "string", "maxLength": 300},
            "reason": {"type": "string", "maxLength": 800},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["decisions"],
        "properties": {
            "decisions": {
                "type": "object",
                "additionalProperties": False,
                "required": assertion_ids,
                "properties": {
                    assertion_id: decision_schema for assertion_id in assertion_ids
                },
            }
        },
    }


def build_relation_classification_prompt(
    items: list[RelationClassificationItem],
    *,
    reviewer: bool = False,
    primary_decisions: dict[str, dict[str, Any]] | None = None,
) -> str:
    candidates = []
    for item in items:
        assertion_id = str(item.assertion["assertionId"])
        raw_occurrences = _reference_occurrence_texts(item.assertion)
        from_spans = article_evidence_spans(item.from_article)
        to_spans = article_evidence_spans(item.to_article)
        reference_occurrences = []
        for occurrence in raw_occurrences:
            occurrence_article_id = "unknown"
            matching_from_span_ids = matching_evidence_span_ids(
                occurrence, from_spans
            )
            matching_to_span_ids = matching_evidence_span_ids(occurrence, to_spans)
            in_from = bool(matching_from_span_ids)
            in_to = bool(matching_to_span_ids)
            if in_from != in_to:
                occurrence_article_id = (
                    item.from_article.article_id
                    if in_from
                    else item.to_article.article_id
                )
            reference_occurrences.append(
                {
                    "text": occurrence,
                    "articleId": occurrence_article_id,
                    "matchingFromSpanIds": matching_from_span_ids,
                    "matchingToSpanIds": matching_to_span_ids,
                }
            )
        candidates.append(
            {
                "decisionKey": assertion_id,
                "referenceOccurrences": reference_occurrences,
                "fromArticle": {
                    "articleId": item.from_article.article_id,
                    "spans": from_spans,
                },
                "toArticle": {
                    "articleId": item.to_article.article_id,
                    "spans": to_spans,
                },
                **(
                    {
                        "primaryDecision": (primary_decisions or {}).get(assertion_id)
                    }
                    if reviewer
                    else {}
                ),
            }
        )
    role = (
        "あなたは法令関係分類のReviewerです。一次判断がuncertainだった候補を独立に再検討します。"
        if reviewer
        else "あなたは法令関係を分類する担当者です。"
    )
    return f"""{role}
質問への関連性ではなく、提示された二つのArticle本文の法的関係だけを分類してください。

判定基準:
- implements: fromArticleが下位法令へ事項を委任し、
  toArticleがその委任事項を具体化している。
- implementsには、fromArticleに「政令で定める」「内閣府令で定める」
  「必要な技術的読替えは政令で定める」等の委任があり、その同じ事項を
  toArticleが定めることを必要とする。
- reference_only: 参照・関連はあるが、提示本文から上記の具体化関係までは確認できない。
- toArticleが用語定義、適用対象の列挙、認可済みであることの条件、権限委任の
  対象範囲としてfromArticleを使うだけならreference_onlyとする。
- uncertain: 本文不足、複数の読みが成り立つ、又は提示本文だけでは安全に区別できない。
- 候補生成元、suggestedType、ガイド文だけを根拠にimplementsへしない。
- 分類対象はArticleペアに存在し得る任意の関係ではなく、各候補の
  referenceOccurrencesが表す明示参照である。複数ある場合も、同じArticleペアの
  一つの候補に紐づく参照箇所群として全件を評価する。Article全文は、それらの
  参照箇所の意味を確認する文脈として使い、同じArticle内の別の参照・委任へ
  判断対象を移さない。
- 複数のreferenceOccurrencesのうち少なくとも一つについて、同じ事項の明示的委任と
  その具体化を両本文から確認できればimplementsとする。見出しや定義的な参照が別に
  含まれていても、それだけを理由にreference_onlyへしない。どの参照にも二条件が
  揃わなければreference_only、提示本文だけでは区別できなければuncertainとする。
- referenceOccurrencesが断片又は見出しでも、同じ事項についてfromArticleが委任し、
  toArticleが具体化していることを両本文から確認する。別の事項の委任は根拠にしない。
- matchingToSpanIdsがある場合、toSupportingSpanIdはそのいずれかにする。
  指定参照と別のspanにある委任・定義・列挙を根拠に使わない。
- delegationFindingは、fromSupportingSpanIdが指す文言が、指定参照と同じ事項を
  下位法令へ明示的に委任するかの判断である。
- implementationFindingは、toSupportingSpanIdが指す参照箇所が、その同じ委任事項を
  実際に具体化するかの判断である。
- implementsはdelegationFinding=explicit_same_matterかつ
  implementationFinding=fulfills_delegationの場合だけとする。
- reference_onlyは参照を確認できるが、上記2条件の少なくとも一方が成立しない場合とする。
- 学習済み知識や質問文を根拠に補わず、提示されたArticle本文だけで判断する。
- Article本文は各候補のfromArticle/toArticle内に閉じて示す。他候補のArticleや判断を
  当該候補へ流用しない。
- spanIdはArticle IDを含む一意なIDである。末尾のspan番号だけを返さない。
- implements/reference_onlyでは、判断を支えるspanIdを両Articleから一つずつ返す。
  fromSupportingSpanIdにはfromArticle、toSupportingSpanIdにはtoArticleのspanIdを使う。
- uncertainでは両SupportingSpanIdを空文字にできる。spanのtextを書き写さない。
- decisionsはdecisionKeyをそのままキーにしたobjectとする。キーを変更・追加・省略せず、
  各valueに判定を返す。JSONだけを返す。

候補:
{json.dumps(candidates, ensure_ascii=False)}
"""


def batch_relation_items(
    items: list[RelationClassificationItem],
    *,
    max_items: int,
    max_chars: int,
) -> list[list[RelationClassificationItem]]:
    """同じ親Articleを近接させつつ、件数と実request長の上限だけを守る。"""
    ordered = sorted(
        items,
        key=lambda item: (
            item.from_article.article_id,
            str(item.assertion.get("assertionId") or ""),
        ),
    )
    batches: list[list[RelationClassificationItem]] = []
    current: list[RelationClassificationItem] = []
    for item in ordered:
        proposed = [*current, item]
        if current and (
            len(current) >= max_items
            or relation_classification_request_chars(proposed) > max_chars
        ):
            batches.append(current)
            current = [item]
        else:
            current = proposed
    if current:
        batches.append(current)
    return batches


def relation_classification_request_chars(
    items: list[RelationClassificationItem],
    *,
    reviewer: bool = False,
    primary_decisions: dict[str, dict[str, Any]] | None = None,
) -> int:
    """providerへ渡すpromptとstructured-output schemaの直列化後文字数。"""
    ids = [str(item.assertion["assertionId"]) for item in items]
    prompt = build_relation_classification_prompt(
        items,
        reviewer=reviewer,
        primary_decisions=primary_decisions,
    )
    schema_text = json.dumps(
        relation_classification_json_schema(ids),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return len(prompt) + len(schema_text)


def relation_classification_timeout(
    request_chars: int,
    *,
    base_timeout_sec: int,
    batch_chars: int,
) -> int:
    """通常batchに対する相対量だけで長い単件のtransport timeoutを延長する。"""
    multiplier = max(1, math.ceil(request_chars / max(1, batch_chars)))
    return min(600, base_timeout_sec * multiplier)


def validate_relation_decisions(
    items: list[RelationClassificationItem],
    payload: dict[str, Any] | None,
) -> dict[str, dict[str, str]]:
    """LLMの意味判断は変更せず、既知ID・一意性・原文引用の存在だけを検査する。"""
    by_id = {str(item.assertion["assertionId"]): item for item in items}
    raw_decisions = payload.get("decisions") if isinstance(payload, dict) else None
    if isinstance(raw_decisions, dict):
        decision_entries = list(raw_decisions.items())
    elif isinstance(raw_decisions, list):
        # v1応答を読み取る互換経路。現行schemaはobjectだけを要求する。
        decision_entries = [
            (str(raw.get("assertionId") or ""), raw)
            for raw in raw_decisions
            if isinstance(raw, dict)
        ]
    else:
        decision_entries = []
    seen: set[str] = set()
    valid: dict[str, dict[str, str]] = {}
    for assertion_id, raw in decision_entries:
        if not isinstance(raw, dict):
            continue
        if assertion_id not in by_id or assertion_id in seen:
            continue
        seen.add(assertion_id)
        verdict = str(raw.get("verdict") or "")
        delegation_finding = str(raw.get("delegationFinding") or "")
        implementation_finding = str(raw.get("implementationFinding") or "")
        from_span_id = str(raw.get("fromSupportingSpanId") or "").strip()
        to_span_id = str(raw.get("toSupportingSpanId") or "").strip()
        reason = str(raw.get("reason") or "").strip()
        item = by_id[assertion_id]
        from_spans = article_evidence_spans(item.from_article)
        to_spans = article_evidence_spans(item.to_article)
        reference_occurrences = _reference_occurrence_texts(item.assertion)
        occurrence_matches = [
            matching_evidence_span_ids(occurrence, to_spans)
            for occurrence in reference_occurrences
        ]
        occurrence_span_ids = {
            span_id for matches in occurrence_matches for span_id in matches
        }
        occurrences_mapped = bool(reference_occurrences) and all(
            occurrence_matches
        )
        if verdict not in {"implements", "reference_only", "uncertain"}:
            verdict = "uncertain"
        finding_contract_valid = (
            verdict != "implements"
            or (
                delegation_finding == "explicit_same_matter"
                and implementation_finding == "fulfills_delegation"
            )
        ) and (
            verdict != "reference_only"
            or delegation_finding == "not_explicit_same_matter"
            or implementation_finding == "does_not_fulfill_delegation"
        )
        if verdict != "uncertain" and (
            from_span_id not in from_spans
            or to_span_id not in to_spans
            or not occurrences_mapped
            or to_span_id not in occurrence_span_ids
            or not finding_contract_valid
        ):
            verdict = "uncertain"
            reason = "LLM応答の根拠span又は判定間の構造契約を確認できなかった"
            from_span_id = ""
            to_span_id = ""
        valid[assertion_id] = {
            "verdict": verdict,
            "delegationFinding": delegation_finding,
            "implementationFinding": implementation_finding,
            "fromSupportingSpanId": from_span_id,
            "toSupportingSpanId": to_span_id,
            "fromSupportingQuote": from_spans.get(from_span_id, ""),
            "toSupportingQuote": to_spans.get(to_span_id, ""),
            "reason": reason,
        }
    for assertion_id in by_id:
        valid.setdefault(
            assertion_id,
            {
                "verdict": "uncertain",
                "delegationFinding": "uncertain",
                "implementationFinding": "uncertain",
                "fromSupportingSpanId": "",
                "toSupportingSpanId": "",
                "fromSupportingQuote": "",
                "toSupportingQuote": "",
                "reason": "LLM応答に既知のassertionIdが一意に含まれなかった",
            },
        )
    return valid


def _reference_occurrence_texts(assertion: dict[str, Any]) -> list[str]:
    source_texts = assertion.get("sourceTexts")
    if not isinstance(source_texts, list):
        source_texts = []
    return list(
        dict.fromkeys(
            str(value).strip()
            for value in [assertion.get("sourceText"), *source_texts]
            if str(value or "").strip()
        )
    )


def article_evidence_spans(article: ArticleText) -> dict[str, str]:
    """Article本文をLLMが選択できる安定spanへ分ける。

    文やチャンク境界の判定は決定的に行い、関係の意味は判断しない。
    """
    parts = []
    for line in article.text.splitlines():
        for sentence in re.split(r"(?<=[。！？])", line.strip()):
            sentence = sentence.strip()
            while sentence:
                parts.append(sentence[:EVIDENCE_SPAN_MAX_CHARS])
                sentence = sentence[EVIDENCE_SPAN_MAX_CHARS:]
    return {
        f"{article.article_id}::span-{index}": text
        for index, text in enumerate(parts, start=1)
    }


def matching_evidence_span_ids(
    occurrence: str,
    spans: dict[str, str],
) -> list[str]:
    """参照文を含む連続spanを返す。

    span境界や元chunk間の改行をまたぐ参照も、空白差だけを無視して位置対応する。
    法的な関係の意味は分類しない。
    """
    normalized_occurrence = re.sub(r"\s+", "", occurrence)
    if not normalized_occurrence:
        return []
    normalized_parts: list[tuple[str, int, int]] = []
    article_text = ""
    for span_id, text in spans.items():
        normalized_text = re.sub(r"\s+", "", text)
        start = len(article_text)
        article_text += normalized_text
        normalized_parts.append((span_id, start, len(article_text)))
    matched_span_ids: list[str] = []
    search_from = 0
    while True:
        match_start = article_text.find(normalized_occurrence, search_from)
        if match_start < 0:
            break
        match_end = match_start + len(normalized_occurrence)
        for span_id, start, end in normalized_parts:
            if (
                start < match_end
                and end > match_start
                and span_id not in matched_span_ids
            ):
                matched_span_ids.append(span_id)
        search_from = match_start + 1
    return matched_span_ids


def matching_evidence_span_ids_at_source_offsets(
    occurrence: str,
    spans: dict[str, str],
    *,
    source_text: str,
    source_start: int,
    source_end: int,
) -> list[str]:
    """元Content Unit上の位置を使い、同文言の別出現を混ぜずにspanへ対応する。"""

    if not 0 <= source_start < source_end <= len(source_text):
        return []
    normalized_occurrence = re.sub(r"\s+", "", occurrence)
    normalized_source = re.sub(r"\s+", "", source_text)
    normalized_start = len(re.sub(r"\s+", "", source_text[:source_start]))
    normalized_end = len(re.sub(r"\s+", "", source_text[:source_end]))
    if (
        not normalized_occurrence
        or normalized_source[normalized_start:normalized_end]
        != normalized_occurrence
    ):
        return []

    normalized_parts: list[tuple[str, int, int]] = []
    article_text = ""
    for span_id, text in spans.items():
        normalized_text = re.sub(r"\s+", "", text)
        start = len(article_text)
        article_text += normalized_text
        normalized_parts.append((span_id, start, len(article_text)))

    occurrence_range: tuple[int, int] | None = None
    radius = max(32, len(normalized_occurrence))
    while radius <= max(len(normalized_source), 32) * 2:
        context_start = max(0, normalized_start - radius)
        context_end = min(len(normalized_source), normalized_end + radius)
        context = normalized_source[context_start:context_end]
        matches: list[int] = []
        search_from = 0
        while context:
            match_start = article_text.find(context, search_from)
            if match_start < 0:
                break
            matches.append(match_start)
            search_from = match_start + 1
        if len(matches) == 1:
            article_occurrence_start = matches[0] + normalized_start - context_start
            occurrence_range = (
                article_occurrence_start,
                article_occurrence_start + len(normalized_occurrence),
            )
            break
        if context_start == 0 and context_end == len(normalized_source):
            break
        radius *= 2

    if occurrence_range is None:
        return []
    occurrence_start, occurrence_end = occurrence_range
    return [
        span_id
        for span_id, start, end in normalized_parts
        if start < occurrence_end and end > occurrence_start
    ]


def article_texts_from_sources(
    article_ids: list[str], sources: list[dict[str, Any]]
) -> dict[str, ArticleText]:
    """OpenSearchの条チャンクから重複のないArticle全文を復元する。"""
    grouped: dict[str, list[dict[str, Any]]] = {
        article_id: [] for article_id in article_ids
    }
    for source in sources:
        article_id = str(
            source.get("articleContentUnitId")
            or str(source.get("contentUnitId") or "").split("-paragraph-", 1)[0]
        )
        if article_id in grouped:
            grouped[article_id].append(source)
    output: dict[str, ArticleText] = {}
    for article_id, chunks in grouped.items():
        unique_chunks = {
            str(chunk.get("contentUnitId") or f"__missing__-{index}"): chunk
            for index, chunk in enumerate(chunks)
        }
        parts: list[str] = []
        for _content_unit_id, chunk in sorted(
            unique_chunks.items(),
            key=lambda pair: _natural_id_key(pair[0]),
        ):
            text = str(chunk.get("text") or "").strip()
            if not text:
                continue
            parent_id = str(chunk.get("parentContentUnitId") or "")
            parent = unique_chunks.get(parent_id)
            if parent is not None:
                text = _without_repeated_parent_context(
                    text,
                    str(parent.get("text") or "").strip(),
                )
            if text:
                parts.append(text)
        if parts:
            output[article_id] = ArticleText(article_id, "\n".join(parts))
    return output


def without_repeated_parent_context_with_offset(
    text: str, parent_text: str
) -> tuple[str, int]:
    """検索用子chunkに再掲された親本文だけを構造情報に基づいて除く。

    法令中に偶然同じ文が現れる場合を消さないよう、直近親chunkの完全な先頭一致だけを
    対象にする。Paragraph chunkの表示番号だけがItem chunkでは省かれる形式にも対応する。
    """
    if not parent_text:
        return text, 0
    candidates = [parent_text]
    without_paragraph_number = re.sub(
        r"^\s*(?:[0-9０-９]+|[一二三四五六七八九十百千]+)\s*",
        "",
        parent_text,
        count=1,
    )
    if without_paragraph_number and without_paragraph_number != parent_text:
        candidates.append(without_paragraph_number)
    for context in sorted(candidates, key=len, reverse=True):
        if text == context:
            return "", len(text)
        if text.startswith(context):
            remainder = text[len(context) :]
            stripped = remainder.lstrip()
            return stripped, len(context) + len(remainder) - len(stripped)
    return text, 0


def _without_repeated_parent_context(text: str, parent_text: str) -> str:
    return without_repeated_parent_context_with_offset(text, parent_text)[0]


def _natural_id_key(value: str) -> tuple[tuple[int, int | str], ...]:
    """paragraph-10がparagraph-2より前にならない安定したcontent ID順。"""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", value)
        if part
    )


def classification_record(
    item: RelationClassificationItem,
    decision: dict[str, str],
    *,
    provider: str,
    model: str,
    reviewer_model: str | None,
    primary_prompt_hash: str,
    reviewer_prompt_hash: str | None,
) -> dict[str, Any]:
    verdict = decision["verdict"]
    status = {
        "implements": RELATION_STATUS_LLM_IMPLEMENTS,
        "reference_only": RELATION_STATUS_LLM_REFERENCE_ONLY,
        "uncertain": RELATION_STATUS_LLM_UNCERTAIN,
    }[verdict]
    return {
        "assertionId": str(item.assertion["assertionId"]),
        "status": status,
        "classificationVerdict": verdict,
        "classificationReason": decision["reason"],
        "classificationDelegationFinding": decision["delegationFinding"],
        "classificationImplementationFinding": decision[
            "implementationFinding"
        ],
        "fromSupportingSpanId": decision["fromSupportingSpanId"],
        "toSupportingSpanId": decision["toSupportingSpanId"],
        "fromSupportingQuote": decision["fromSupportingQuote"],
        "toSupportingQuote": decision["toSupportingQuote"],
        "fromArticleHash": item.from_article.content_hash,
        "toArticleHash": item.to_article.content_hash,
        "classifierProvider": provider,
        "classifierModel": model,
        "classifierReviewerModel": reviewer_model or "",
        "classifierPromptVersion": RELATION_CLASSIFIER_PROMPT_VERSION,
        "classifierPromptHash": reviewer_prompt_hash or primary_prompt_hash,
        "primaryClassifierPromptHash": primary_prompt_hash,
        "classifiedAt": datetime.now(UTC).isoformat(),
    }


class LegalRelationClassificationService:
    """seedから独立してRelationAssertionの派生分類だけを更新する。"""

    def __init__(
        self, graph_client: Any, opensearch_client: Any, llm_client: Any
    ) -> None:
        self.graph = graph_client
        self.opensearch = opensearch_client
        self.llm = llm_client

    def run(self, *, limit: int | None = None, dry_run: bool = False) -> dict[str, Any]:
        assertions = self.graph.relation_assertions_for_classification(limit=limit)
        article_ids = list(
            dict.fromkeys(
                str(assertion[key])
                for assertion in assertions
                for key in ("fromArticleId", "toArticleId")
            )
        )
        sources = self.opensearch.get_complete_articles_by_ids(
            article_ids, user_clearance_level=3
        )
        texts = article_texts_from_sources(article_ids, sources)
        items: list[RelationClassificationItem] = []
        missing_text = 0
        skipped_current = 0
        for assertion in assertions:
            from_article = texts.get(str(assertion["fromArticleId"]))
            to_article = texts.get(str(assertion["toArticleId"]))
            if from_article is None or to_article is None:
                missing_text += 1
                continue
            if (
                str(assertion.get("fromArticleHash") or "") == from_article.content_hash
                and str(assertion.get("toArticleHash") or "") == to_article.content_hash
                and str(assertion.get("classifierPromptVersion") or "")
                == RELATION_CLASSIFIER_PROMPT_VERSION
                and str(assertion.get("classifierModel") or "")
                == settings.relation_classifier_model
                and (
                    not str(assertion.get("classifierReviewerModel") or "")
                    or str(assertion.get("classifierReviewerModel") or "")
                    == settings.relation_classifier_reviewer_model
                )
                and str(assertion.get("status") or "")
                in {
                    RELATION_STATUS_LLM_IMPLEMENTS,
                    RELATION_STATUS_LLM_REFERENCE_ONLY,
                    RELATION_STATUS_LLM_UNCERTAIN,
                }
            ):
                skipped_current += 1
                continue
            items.append(
                RelationClassificationItem(assertion, from_article, to_article)
            )

        records: list[dict[str, Any]] = []
        primary_uncertain = 0
        batches = batch_relation_items(
            items,
            max_items=settings.relation_classifier_batch_size,
            max_chars=settings.relation_classifier_batch_chars,
        )
        for batch in batches:
            batch_records: list[dict[str, Any]] = []
            primary, primary_prompt_hash = self._classify_batch(
                batch, model=settings.relation_classifier_model, reviewer=False
            )
            uncertain_items = [
                item
                for item in batch
                if primary[str(item.assertion["assertionId"])]["verdict"] == "uncertain"
            ]
            primary_uncertain += len(uncertain_items)
            reviewed: dict[str, dict[str, str]] = {}
            reviewer_prompt_hash: str | None = None
            if uncertain_items:
                reviewed, reviewer_prompt_hash = self._classify_batch(
                    uncertain_items,
                    model=settings.relation_classifier_reviewer_model,
                    reviewer=True,
                    primary_decisions=primary,
                )
            for item in batch:
                assertion_id = str(item.assertion["assertionId"])
                final = reviewed.get(assertion_id, primary[assertion_id])
                record = classification_record(
                    item,
                    final,
                    provider=self.llm.provider,
                    model=settings.relation_classifier_model,
                    reviewer_model=(
                        settings.relation_classifier_reviewer_model
                        if assertion_id in reviewed
                        else None
                    ),
                    primary_prompt_hash=primary_prompt_hash,
                    reviewer_prompt_hash=(
                        reviewer_prompt_hash if assertion_id in reviewed else None
                    ),
                )
                records.append(record)
                batch_records.append(record)
            # 全件完了後の一括保存では、長時間実行の途中失敗でそれまでのLLM判断を
            # すべて失う。入力hashとprompt/modelで再開判定できるため、batch単位で
            # 保存して次回実行時に確定済みbatchを安全にskipできるようにする。
            if batch_records and not dry_run:
                self.graph.update_relation_classifications(batch_records)
        counts: dict[str, int] = {}
        for record in records:
            verdict = str(record["classificationVerdict"])
            counts[verdict] = counts.get(verdict, 0) + 1
        return {
            "assertionCount": len(assertions),
            "classifiedCount": len(records),
            "skippedCurrentCount": skipped_current,
            "missingArticleTextCount": missing_text,
            "primaryUncertainCount": primary_uncertain,
            "batchCount": len(batches),
            "verdictCounts": counts,
            "dryRun": dry_run,
        }

    def _classify_batch(
        self,
        items: list[RelationClassificationItem],
        *,
        model: str,
        reviewer: bool,
        primary_decisions: dict[str, dict[str, str]] | None = None,
    ) -> tuple[dict[str, dict[str, str]], str]:
        ids = [str(item.assertion["assertionId"]) for item in items]
        prompt = build_relation_classification_prompt(
            items,
            reviewer=reviewer,
            primary_decisions=primary_decisions,
        )
        schema = relation_classification_json_schema(ids)
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
        return (
            validate_relation_decisions(items, result.payload),
            hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        )
