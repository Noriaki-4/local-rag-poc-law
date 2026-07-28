"""Requirement内のArticle候補を並べ替えるCross-Encoder適用。

計画書 §9.4(Cross-Encoder)、§11.2(ペア数上限)、§12(フォールバック)に対応する。

Cross-Encoderは必須条件ではなくソフトスコアとして使う。質問全文と単一の具体化条文を
比較する単位の問題を避けるため、queryはRequirement専用クエリにし、同じRequirement内の
並べ替えだけに限定する。構造上mandatoryなArticleは低スコアでも候補から削除しない。
"""

from dataclasses import dataclass
from typing import Any

from .retrieval_budget import COMPONENT_RERANK, BudgetTracker, RerankBudget


@dataclass(frozen=True)
class RequirementRerankResult:
    candidates: tuple[dict[str, Any], ...]
    used: bool
    pairs: int = 0
    error: str | None = None
    protected_article_ids: tuple[str, ...] = ()

    def as_trace(self) -> dict[str, Any]:
        return {
            "used": self.used,
            "pairs": self.pairs,
            "error": self.error,
            "protectedArticleIds": list(self.protected_article_ids),
            "orderedArticleIds": [
                str(candidate.get("articleId")) for candidate in self.candidates
            ],
        }


@dataclass(frozen=True)
class RequirementRerankInput:
    requirement_id: str
    query: str
    candidates: tuple[dict[str, Any], ...]
    protected_article_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RequirementBatchRerankResult:
    results: dict[str, RequirementRerankResult]
    pairs: int = 0
    invoked: bool = False


def rerank_requirement_batch(
    reranker_client: Any,
    entries: list[RequirementRerankInput],
    *,
    budget: RerankBudget | None = None,
    tracker: BudgetTracker | None = None,
    used_pairs: int = 0,
    timeout_sec: float | None = None,
) -> RequirementBatchRerankResult:
    """複数Requirementを1回の外部Cross-Encoder batchへまとめる。"""
    rerank_budget = budget or RerankBudget()
    if not entries:
        return RequirementBatchRerankResult({})
    if tracker is not None and not tracker.can_invoke(
        COMPONENT_RERANK,
        max_invocations=rerank_budget.max_calls_total,
    ):
        return RequirementBatchRerankResult(
            {
                entry.requirement_id: RequirementRerankResult(
                    entry.candidates,
                    False,
                    error="rerank_call_budget_exhausted",
                    protected_article_ids=entry.protected_article_ids,
                )
                for entry in entries
            }
        )
    pair_limit = rerank_budget.allowed_pairs(
        sum(len(entry.candidates) for entry in entries),
        used_pairs=used_pairs,
    )
    if pair_limit < 2 or timeout_sec is not None and timeout_sec <= 0:
        reason = (
            "rerank_timeout_budget_exhausted"
            if timeout_sec is not None and timeout_sec <= 0
            else "rerank_pair_budget_exhausted"
        )
        return RequirementBatchRerankResult(
            {
                entry.requirement_id: RequirementRerankResult(
                    entry.candidates,
                    False,
                    error=reason,
                    protected_article_ids=entry.protected_article_ids,
                )
                for entry in entries
            }
        )

    ordered_by_id = {
        entry.requirement_id: _ordered_candidates(
            list(entry.candidates),
            entry.protected_article_ids,
        )
        for entry in entries
    }
    counts = _fair_pair_counts(entries, pair_limit)
    scoped_entries = [entry for entry in entries if counts.get(entry.requirement_id, 0) >= 2]
    requests_batch: list[tuple[str, list[dict[str, Any]]]] = []
    for entry in scoped_entries:
        scoped = ordered_by_id[entry.requirement_id][: counts[entry.requirement_id]]
        requests_batch.append(
            (
                entry.query,
                [
                    {
                        "document": {
                            **(candidate.get("chunks") or [{}])[0],
                            # Cross-EncoderへはArticle内の候補chunkを連結して渡す。
                            "text": _article_text(candidate),
                        }
                    }
                    for candidate in scoped
                ],
            )
        )
    if not requests_batch or not hasattr(reranker_client, "rerank_batch"):
        return RequirementBatchRerankResult(
            {
                entry.requirement_id: RequirementRerankResult(
                    tuple(ordered_by_id[entry.requirement_id]),
                    False,
                    error="batch_reranker_unavailable",
                    protected_article_ids=entry.protected_article_ids,
                )
                for entry in entries
            }
        )

    external_results = reranker_client.rerank_batch(
        requests_batch,
        timeout_sec=int(timeout_sec) if timeout_sec else None,
    )
    pair_count = sum(len(items) for _, items in requests_batch)
    if tracker is not None:
        tracker.record(
            COMPONENT_RERANK,
            items=pair_count,
            elapsed_ms=max(
                (int(result.latency_ms or 0) for result in external_results),
                default=0,
            ),
        )
    output: dict[str, RequirementRerankResult] = {}
    external_by_id = dict(
        zip(
            (entry.requirement_id for entry in scoped_entries),
            external_results,
            strict=False,
        )
    )
    for entry in entries:
        ordered = ordered_by_id[entry.requirement_id]
        count = counts.get(entry.requirement_id, 0)
        external = external_by_id.get(entry.requirement_id)
        if external is None:
            output[entry.requirement_id] = RequirementRerankResult(
                tuple(ordered),
                False,
                error="rerank_call_batch_capacity_exhausted",
                protected_article_ids=entry.protected_article_ids,
            )
            continue
        scores = external.scores
        scoped = ordered[:count]
        remainder = ordered[count:]
        reranked = sorted(
            scoped,
            key=lambda candidate: (
                0
                if str(candidate.get("articleId"))
                in entry.protected_article_ids
                else 1,
                -scores.get(_content_unit_id(candidate), 0.0),
                candidate.get("authorityRank", 0),
            ),
        )
        output[entry.requirement_id] = RequirementRerankResult(
            (*reranked, *remainder),
            bool(external.used),
            pairs=count,
            error=external.error,
            protected_article_ids=entry.protected_article_ids,
        )
    return RequirementBatchRerankResult(output, pairs=pair_count, invoked=True)


def rerank_requirement_candidates(
    reranker_client: Any,
    *,
    query: str,
    candidates: list[dict[str, Any]],
    protected_article_ids: tuple[str, ...] = (),
    budget: RerankBudget | None = None,
    tracker: BudgetTracker | None = None,
    used_pairs: int = 0,
    timeout_sec: float | None = None,
) -> RequirementRerankResult:
    """Requirement内候補を並べ替える。呼び出せない場合は元の順序を維持する。"""
    rerank_budget = budget or RerankBudget()
    protected = tuple(dict.fromkeys(protected_article_ids))
    if len(candidates) < 2:
        return RequirementRerankResult(tuple(candidates), False, protected_article_ids=protected)

    allowed = rerank_budget.allowed_pairs(len(candidates), used_pairs=used_pairs)
    if allowed < 2:
        return RequirementRerankResult(
            tuple(candidates), False, error="rerank_pair_budget_exhausted", protected_article_ids=protected
        )
    if tracker is not None and not tracker.can_invoke(
        COMPONENT_RERANK, max_invocations=rerank_budget.max_calls_total
    ):
        return RequirementRerankResult(
            tuple(candidates), False, error="rerank_call_budget_exhausted", protected_article_ids=protected
        )
    if timeout_sec is not None and timeout_sec <= 0:
        return RequirementRerankResult(
            tuple(candidates), False, error="rerank_timeout_budget_exhausted", protected_article_ids=protected
        )

    # mandatory・明示・高信頼Graph由来の候補を優先してペア枠へ入れる(§11.2)。
    ordered_input = _ordered_candidates(candidates, protected)
    scoped = ordered_input[:allowed]
    remainder = ordered_input[allowed:]

    items = [
        {"document": {**(candidate.get("chunks") or [{}])[0], "text": candidate.get("text") or ""}}
        for candidate in scoped
    ]
    result = reranker_client.rerank(query, items, timeout_sec=int(timeout_sec) if timeout_sec else None)
    if tracker is not None:
        tracker.record(COMPONENT_RERANK, items=len(scoped), elapsed_ms=result.latency_ms or 0)
    if not result.used:
        return RequirementRerankResult(
            tuple(ordered_input),
            False,
            pairs=len(scoped),
            error=result.error or "rerank_unavailable",
            protected_article_ids=protected,
        )

    scores = {
        str(item["document"].get("contentUnitId") or ""): float(item.get("rerankScore") or 0.0)
        for item in result.items
    }
    reranked = sorted(
        scoped,
        key=lambda candidate: (
            # 構造上mandatoryなArticleはスコアに関わらず先頭側へ残す(§9.4)。
            0 if str(candidate.get("articleId")) in protected else 1,
            -scores.get(_content_unit_id(candidate), 0.0),
            candidate.get("authorityRank", 0),
        ),
    )
    return RequirementRerankResult(
        (*reranked, *remainder),
        True,
        pairs=len(scoped),
        protected_article_ids=protected,
    )


def _content_unit_id(candidate: dict[str, Any]) -> str:
    chunks = candidate.get("chunks") or []
    if chunks:
        return str(chunks[0].get("contentUnitId") or "")
    return str(candidate.get("articleId") or "")


def _ordered_candidates(
    candidates: list[dict[str, Any]],
    protected_article_ids: tuple[str, ...],
) -> list[dict[str, Any]]:
    return sorted(
        candidates,
        key=lambda candidate: (
            0 if str(candidate.get("articleId")) in protected_article_ids else 1,
            candidate.get("authorityRank", 0),
            -float(candidate.get("score") or 0.0),
        ),
    )


def _fair_pair_counts(
    entries: list[RequirementRerankInput],
    pair_limit: int,
) -> dict[str, int]:
    """各Requirementへまず2ペア、その後をround-robinで配る。"""
    counts: dict[str, int] = {}
    remaining = pair_limit
    for entry in entries:
        if len(entry.candidates) < 2 or remaining < 2:
            continue
        counts[entry.requirement_id] = 2
        remaining -= 2
    while remaining > 0:
        progressed = False
        for entry in entries:
            current = counts.get(entry.requirement_id, 0)
            if current <= 0 or current >= len(entry.candidates):
                continue
            counts[entry.requirement_id] = current + 1
            remaining -= 1
            progressed = True
            if remaining <= 0:
                break
        if not progressed:
            break
    return counts


def _article_text(candidate: dict[str, Any]) -> str:
    chunks = candidate.get("chunks") or []
    text = "\n".join(str(chunk.get("text") or "") for chunk in chunks)
    return text or str(candidate.get("text") or "")
