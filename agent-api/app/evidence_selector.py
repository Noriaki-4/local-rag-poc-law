from dataclasses import dataclass, field
from typing import Any


@dataclass
class AspectEvidence:
    query: str
    searched_content_ids: list[str] = field(default_factory=list)
    ordered_content_ids: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    inherited_content_ids: set[str] = field(default_factory=set)
    used: bool = False
    error: str | None = None
    skipped_reason: str | None = None


@dataclass
class AspectEvidenceMatrix:
    aspects: list[AspectEvidence] = field(default_factory=list)

    @property
    def orders_by_query(self) -> dict[str, list[str]]:
        return {
            aspect.query: list(aspect.ordered_content_ids)
            for aspect in self.aspects
        }

    @property
    def scores_by_query(self) -> dict[str, dict[str, float]]:
        return {
            aspect.query: dict(aspect.scores)
            for aspect in self.aspects
        }


@dataclass
class ContextSelectionResult:
    items: list[dict[str, Any]]
    explicit_protected_ids: list[str]
    aspect_protected_ids: list[str]
    global_rank_ids: list[str]
    covered_articles_by_query: dict[str, list[str]]

    @property
    def protected_ids(self) -> list[str]:
        return list(dict.fromkeys([
            *self.explicit_protected_ids,
            *self.aspect_protected_ids,
        ]))


def aspect_queries_by_article(
    matrix: AspectEvidenceMatrix,
    evidence_by_id: dict[str, dict[str, Any]],
    *,
    per_query: int = 3,
) -> dict[str, set[str]]:
    """旧aspectIncludeフラグに依存せず、論点上位Articleとqueryを対応付ける。"""
    result: dict[str, set[str]] = {}
    for aspect in matrix.aspects:
        seen_articles: set[str] = set()
        for item_id in aspect.ordered_content_ids:
            item = evidence_by_id.get(item_id)
            if item is None or not is_law_item(item):
                continue
            item_article_id = article_id(item)
            if not item_article_id or item_article_id in seen_articles:
                continue
            seen_articles.add(item_article_id)
            result.setdefault(item_article_id, set()).add(aspect.query)
            if len(seen_articles) >= per_query:
                break
    return result


def content_id(item: dict[str, Any]) -> str:
    return str(item.get("document", {}).get("contentUnitId") or "")


def article_id(item: dict[str, Any]) -> str:
    document = item.get("document", {})
    value = str(
        document.get("articleContentUnitId")
        or document.get("parentContentUnitId")
        or document.get("contentUnitId")
        or ""
    )
    return value.split("-paragraph-", 1)[0].split("-item-", 1)[0]


def is_law_item(item: dict[str, Any]) -> bool:
    document = item.get("document", {})
    doc_type = document.get("docType")
    if doc_type:
        return doc_type == "law"
    return str(document.get("documentId") or "").startswith("law-")


def is_user_explicit_reference(item: dict[str, Any]) -> bool:
    sources = {str(source) for source in item.get("sources") or []}
    return (
        item.get("introducedBy") == "article_reference"
        or "article_reference" in sources
    )


def select_issue_covered_context(
    globally_ranked: list[dict[str, Any]],
    aspect_matrix: AspectEvidenceMatrix,
    *,
    top_k: int,
    max_aspects: int = 4,
    protected_chunk_limit: int | None = None,
    explicit_chunk_limit: int | None = None,
    rounds: int = 2,
) -> ContextSelectionResult:
    """全文順位の半分を維持し、残りで明示条文と論点別法令を確保する。"""
    if top_k <= 0:
        return ContextSelectionResult([], [], [], [], {})

    protected_limit = min(
        top_k,
        protected_chunk_limit if protected_chunk_limit is not None else top_k // 2,
    )
    explicit_limit = min(
        protected_limit,
        explicit_chunk_limit if explicit_chunk_limit is not None else max(1, top_k // 4),
    )
    ranked_by_id = {
        content_id(item): item
        for item in globally_ranked
        if content_id(item)
    }
    global_index = {
        content_id(item): index
        for index, item in enumerate(globally_ranked)
        if content_id(item)
    }

    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    explicit_protected_ids: list[str] = []
    aspect_protected_ids: list[str] = []

    def add_protected(item: dict[str, Any], bucket: list[str]) -> bool:
        item_id = content_id(item)
        if not item_id or item_id in selected_ids or len(selected) >= protected_limit:
            return False
        selected.append(item)
        selected_ids.add(item_id)
        bucket.append(item_id)
        return True

    explicit_candidates = [
        item
        for item in globally_ranked
        if is_user_explicit_reference(item)
    ]
    for item in explicit_candidates[:explicit_limit]:
        add_protected(item, explicit_protected_ids)

    active_aspects = [
        aspect
        for aspect in aspect_matrix.aspects[:max_aspects]
        if aspect.used and not aspect.skipped_reason
    ]
    candidates_by_query = {
        aspect.query: _article_candidates(
            aspect,
            ranked_by_id,
            global_index,
        )
        for aspect in active_aspects
    }
    covered_articles_by_query: dict[str, list[str]] = {
        aspect.query: [] for aspect in active_aspects
    }

    for round_index in range(max(0, rounds)):
        if len(selected) >= protected_limit:
            break
        for aspect in active_aspects:
            if len(selected) >= protected_limit:
                break
            article_candidates = candidates_by_query[aspect.query]
            if round_index >= len(article_candidates):
                continue
            candidate_article, representative = article_candidates[round_index]
            if candidate_article in {
                article_id(item)
                for item in selected
            }:
                covered_articles_by_query[aspect.query].append(candidate_article)
                continue
            if add_protected(representative, aspect_protected_ids):
                covered_articles_by_query[aspect.query].append(candidate_article)

    global_rank_ids: list[str] = []
    for item in globally_ranked:
        if len(selected) >= top_k:
            break
        item_id = content_id(item)
        if not item_id or item_id in selected_ids:
            continue
        selected.append(item)
        selected_ids.add(item_id)
        global_rank_ids.append(item_id)

    return ContextSelectionResult(
        items=selected,
        explicit_protected_ids=explicit_protected_ids,
        aspect_protected_ids=aspect_protected_ids,
        global_rank_ids=global_rank_ids,
        covered_articles_by_query={
            query: list(dict.fromkeys(article_ids))
            for query, article_ids in covered_articles_by_query.items()
        },
    )


def _article_candidates(
    aspect: AspectEvidence,
    ranked_by_id: dict[str, dict[str, Any]],
    global_index: dict[str, int],
) -> list[tuple[str, dict[str, Any]]]:
    """論点内Article順を保ち、各Articleの代表chunkを選ぶ。"""
    items = [
        ranked_by_id[item_id]
        for item_id in aspect.ordered_content_ids
        if item_id in ranked_by_id and is_law_item(ranked_by_id[item_id])
    ]
    by_article: dict[str, list[dict[str, Any]]] = {}
    article_order: list[str] = []
    for item in items:
        item_article_id = article_id(item)
        if not item_article_id:
            continue
        if item_article_id not in by_article:
            by_article[item_article_id] = []
            article_order.append(item_article_id)
        by_article[item_article_id].append(item)

    result: list[tuple[str, dict[str, Any]]] = []
    for item_article_id in article_order:
        article_items = by_article[item_article_id]
        representative = min(
            article_items,
            key=lambda item: (
                not is_user_explicit_reference(item),
                aspect.ordered_content_ids.index(content_id(item)),
                global_index.get(content_id(item), len(global_index)),
            ),
        )
        result.append((item_article_id, representative))
    return result
