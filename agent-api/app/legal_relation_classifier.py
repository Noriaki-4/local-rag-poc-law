"""RelationAssertionを法令本文で分類するオフライン処理の純粋ロジック。

候補抽出は決定的処理、関係の意味判断はLLM、ID・本文引用・件数・ハッシュの検査は
プログラムという境界を保つ。分類結果は正式Graphエッジではなく派生データとして保存する。
"""

from __future__ import annotations

import hashlib
import json
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

RELATION_CLASSIFIER_PROMPT_VERSION = "legal-relation-classifier-v1"
RelationVerdict = Literal["implements", "reference_only", "uncertain"]


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
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["decisions"],
        "properties": {
            "decisions": {
                "type": "array",
                "minItems": len(assertion_ids),
                "maxItems": len(assertion_ids),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "assertionId",
                        "verdict",
                        "fromSupportingQuote",
                        "toSupportingQuote",
                        "reason",
                    ],
                    "properties": {
                        "assertionId": {"type": "string", "enum": assertion_ids},
                        "verdict": {
                            "type": "string",
                            "enum": ["implements", "reference_only", "uncertain"],
                        },
                        "fromSupportingQuote": {"type": "string", "maxLength": 500},
                        "toSupportingQuote": {"type": "string", "maxLength": 500},
                        "reason": {"type": "string", "maxLength": 800},
                    },
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
    articles = {
        article.article_id: article.text
        for item in items
        for article in (item.from_article, item.to_article)
    }
    candidates = [
        {
            "assertionId": str(item.assertion["assertionId"]),
            "suggestedType": str(item.assertion.get("suggestedType") or ""),
            "candidateSource": str(item.assertion.get("assertionSource") or ""),
            "candidateSourceText": str(item.assertion.get("sourceText") or ""),
            "fromArticleId": item.from_article.article_id,
            "toArticleId": item.to_article.article_id,
            **(
                {
                    "primaryDecision": (primary_decisions or {}).get(
                        str(item.assertion["assertionId"])
                    )
                }
                if reviewer
                else {}
            ),
        }
        for item in items
    ]
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
- reference_only: 参照・関連はあるが、提示本文から上記の具体化関係までは確認できない。
- uncertain: 本文不足、複数の読みが成り立つ、又は提示本文だけでは安全に区別できない。
- 候補生成元、suggestedType、ガイド文だけを根拠にimplementsへしない。
- 学習済み知識や質問文を根拠に補わず、提示されたArticle本文だけで判断する。
- implements/reference_onlyでは、判断を支える短い原文引用を両Articleから一つずつ返す。
- uncertainでは引用を空文字にできる。
- assertionIdを変更・追加・省略せず、JSONだけを返す。

候補:
{json.dumps(candidates, ensure_ascii=False)}

Article本文（候補のfromArticleId/toArticleIdで参照する）:
{json.dumps(articles, ensure_ascii=False)}
"""


def batch_relation_items(
    items: list[RelationClassificationItem],
    *,
    max_items: int,
    max_chars: int,
) -> list[list[RelationClassificationItem]]:
    """同じ親Articleを近接させつつ、件数と文字数の上限だけを決定的に守る。"""
    ordered = sorted(
        items,
        key=lambda item: (
            item.from_article.article_id,
            str(item.assertion.get("assertionId") or ""),
        ),
    )
    batches: list[list[RelationClassificationItem]] = []
    current: list[RelationClassificationItem] = []
    current_chars = 0
    current_articles: set[str] = set()
    for item in ordered:
        item_articles = (item.from_article, item.to_article)
        item_chars = 1000 + sum(
            len(article.text)
            for article in item_articles
            if article.article_id not in current_articles
        )
        if current and (
            len(current) >= max_items or current_chars + item_chars > max_chars
        ):
            batches.append(current)
            current = []
            current_chars = 0
            current_articles = set()
            item_chars = 1000 + sum(len(article.text) for article in item_articles)
        current.append(item)
        current_chars += item_chars
        current_articles.update(article.article_id for article in item_articles)
    if current:
        batches.append(current)
    return batches


def validate_relation_decisions(
    items: list[RelationClassificationItem],
    payload: dict[str, Any] | None,
) -> dict[str, dict[str, str]]:
    """LLMの意味判断は変更せず、既知ID・一意性・原文引用の存在だけを検査する。"""
    by_id = {str(item.assertion["assertionId"]): item for item in items}
    raw_decisions = payload.get("decisions") if isinstance(payload, dict) else None
    if not isinstance(raw_decisions, list):
        raw_decisions = []
    seen: set[str] = set()
    valid: dict[str, dict[str, str]] = {}
    for raw in raw_decisions:
        if not isinstance(raw, dict):
            continue
        assertion_id = str(raw.get("assertionId") or "")
        if assertion_id not in by_id or assertion_id in seen:
            continue
        seen.add(assertion_id)
        verdict = str(raw.get("verdict") or "")
        from_quote = str(raw.get("fromSupportingQuote") or "").strip()
        to_quote = str(raw.get("toSupportingQuote") or "").strip()
        reason = str(raw.get("reason") or "").strip()
        item = by_id[assertion_id]
        if verdict not in {"implements", "reference_only", "uncertain"}:
            verdict = "uncertain"
        if verdict != "uncertain" and (
            not from_quote
            or not to_quote
            or from_quote not in item.from_article.text
            or to_quote not in item.to_article.text
        ):
            verdict = "uncertain"
            reason = "LLMが返した根拠引用を提示本文内で確認できなかった"
            from_quote = ""
            to_quote = ""
        valid[assertion_id] = {
            "verdict": verdict,
            "fromSupportingQuote": from_quote,
            "toSupportingQuote": to_quote,
            "reason": reason,
        }
    for assertion_id in by_id:
        valid.setdefault(
            assertion_id,
            {
                "verdict": "uncertain",
                "fromSupportingQuote": "",
                "toSupportingQuote": "",
                "reason": "LLM応答に既知のassertionIdが一意に含まれなかった",
            },
        )
    return valid


def article_texts_from_sources(
    article_ids: list[str], sources: list[dict[str, Any]]
) -> dict[str, ArticleText]:
    """OpenSearchの条チャンクを安定順で連結し、Article単位の分類入力へする。"""
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
        parts = [
            str(chunk.get("text") or "").strip()
            for _, chunk in sorted(
                unique_chunks.items(),
                key=lambda pair: _natural_id_key(pair[0]),
            )
            if str(chunk.get("text") or "").strip()
        ]
        if parts:
            output[article_id] = ArticleText(article_id, "\n".join(parts))
    return output


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
        result = self.llm.generate_structured_json(
            prompt=prompt,
            schema=relation_classification_json_schema(ids),
            model=model,
            max_tokens=settings.relation_classifier_max_tokens,
            timeout_sec=settings.relation_classifier_timeout_sec,
        )
        return (
            validate_relation_decisions(items, result.payload),
            hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        )
