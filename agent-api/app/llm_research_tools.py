"""LLM主導法令調査から、投入済み検索基盤だけを呼び出すtool gateway。"""

from dataclasses import dataclass
from time import perf_counter
from typing import Any

from .config import settings
from .graph_client import GraphClient
from .legal_ontology import RELATION_STATUS_LLM_IMPLEMENTS, is_trusted_relation
from .llm_directed_research import (
    TOOL_EXPAND_GRAPH,
    TOOL_FETCH_ARTICLES,
    TOOL_SEARCH_CORPUS,
    EvidenceCatalog,
    ResearchAction,
    compact_graph_relations,
)
from .opensearch_client import OpenSearchClient, RequirementSearchSpec

GRAPH_PATHS_PER_ARTICLE = 50
GRAPH_RELATIONS_PER_TOOL_RESULT = 50


@dataclass(frozen=True)
class ResearchToolExecution:
    tool: str
    result_count: int
    new_evidence_count: int
    new_article_count: int
    elapsed_ms: int
    error: str | None = None
    new_content_unit_ids: tuple[str, ...] = ()
    returned_content_unit_ids: tuple[str, ...] = ()
    new_article_ids: tuple[str, ...] = ()
    auto_graph_path_count: int = 0
    auto_graph_article_ids: tuple[str, ...] = ()
    graph_relations: tuple[dict[str, Any], ...] = ()
    relation_assertions: tuple[dict[str, Any], ...] = ()
    auto_graph_error: str | None = None

    def as_trace(self, action: ResearchAction) -> dict[str, Any]:
        trace = {
            "tool": self.tool,
            "query": action.query,
            "articleIds": action.articleIds,
            "documentIds": action.documentIds,
            "docTypes": action.docTypes,
            "edgeTypes": action.edgeTypes,
            "hypothesisIds": action.hypothesisIds,
            "reason": action.reason,
            "resultCount": self.result_count,
            "newEvidenceCount": self.new_evidence_count,
            "newArticleCount": self.new_article_count,
            "elapsedMs": self.elapsed_ms,
            "error": self.error,
        }
        # 検索結果の全IDを履歴へ複製すると最終ターンの入力を圧迫する。
        # 直前取得の優先表示とGraph由来候補の監査に必要な場合だけ記録する。
        if self.tool in {TOOL_FETCH_ARTICLES, TOOL_EXPAND_GRAPH}:
            trace["newArticleIds"] = list(self.new_article_ids)
        if self.tool == TOOL_FETCH_ARTICLES:
            trace["newContentUnitIds"] = list(self.new_content_unit_ids)
            trace["returnedContentUnitIds"] = list(
                self.returned_content_unit_ids
            )
            trace["autoGraphPathCount"] = self.auto_graph_path_count
            trace["autoGraphArticleIds"] = list(self.auto_graph_article_ids)
            trace["autoGraphError"] = self.auto_graph_error
        if self.tool in {TOOL_FETCH_ARTICLES, TOOL_EXPAND_GRAPH}:
            trace["graphRelations"] = list(self.graph_relations)
            trace["relationAssertions"] = list(self.relation_assertions)
        return trace


class LegalResearchToolGateway:
    """LLMの操作を既存OpenSearch / Neo4j APIへ写像する。

    queryや操作順序はLLMが決めるが、検索件数、clearance、Graph深度、検索対象は
    アプリケーション設定からのみ決める。LLMは任意URLや任意DBクエリを実行できない。
    """

    def __init__(
        self,
        os_client: OpenSearchClient,
        graph_client: GraphClient,
    ) -> None:
        self.os_client = os_client
        self.graph_client = graph_client

    def execute(
        self,
        action: ResearchAction,
        catalog: EvidenceCatalog,
        *,
        user_clearance_level: int,
        timeout_sec: float,
    ) -> ResearchToolExecution:
        started = perf_counter()
        evidence_before = set(catalog.content_unit_ids)
        articles_before = set(catalog.known_article_ids)
        auto_graph_path_count = 0
        auto_graph_article_ids: tuple[str, ...] = ()
        returned_content_unit_ids: tuple[str, ...] = ()
        graph_relations: tuple[dict[str, Any], ...] = ()
        relation_assertions: tuple[dict[str, Any], ...] = ()
        auto_graph_error: str | None = None
        try:
            unknown_documents = sorted(
                set(action.documentIds) - set(catalog.known_document_ids)
            )
            unknown_articles = sorted(
                set(action.articleIds) - set(catalog.known_article_ids)
            )
            if unknown_documents:
                raise ValueError(
                    "unknown documentIds: " + ", ".join(unknown_documents)
                )
            if unknown_articles:
                raise ValueError(
                    "unknown articleIds: " + ", ".join(unknown_articles)
                )
            if action.tool == TOOL_SEARCH_CORPUS:
                result_count = self._search(
                    action,
                    catalog,
                    user_clearance_level=user_clearance_level,
                    timeout_sec=timeout_sec,
                )
            elif action.tool == TOOL_FETCH_ARTICLES:
                # 複数Articleを1つのglobal sizeで取得すると、項号の多い最初の条が
                # 枠を独占する。Articleごとに独立した取得上限を適用し、全Articleを
                # 証拠カタログへ保存する。LLMへの提示量は別の文字予算で制御する。
                results: list[dict[str, Any]] = []
                for article_id in dict.fromkeys(action.articleIds):
                    results.extend(
                        self.os_client.get_by_article_ids(
                            [article_id],
                            user_clearance_level,
                            max_chunks=(
                                settings.llm_research_max_chunks_per_article
                            ),
                        )
                    )
                result_count = len(results)
                catalog.add_results(results)
                returned_content_unit_ids = tuple(
                    dict.fromkeys(
                        content_unit_id
                        for item in results
                        if (
                            content_unit_id := _content_unit_id(item)
                        )
                    )
                )
            elif action.tool == TOOL_EXPAND_GRAPH:
                assertion_lookup = getattr(
                    self.graph_client,
                    "relation_assertions_touching",
                    None,
                )
                assertion_items = (
                    assertion_lookup(
                        action.articleIds,
                        suggested_types=action.edgeTypes or None,
                        user_clearance_level=user_clearance_level,
                        limit=GRAPH_RELATIONS_PER_TOOL_RESULT,
                        timeout_sec=max(0.1, timeout_sec),
                    )
                    if callable(assertion_lookup)
                    else []
                )
                preclassified_items = [
                    item
                    for item in assertion_items
                    if str(item.get("status") or "")
                    == RELATION_STATUS_LLM_IMPLEMENTS
                ]
                unresolved_items = [
                    item
                    for item in assertion_items
                    if str(item.get("status") or "")
                    != RELATION_STATUS_LLM_IMPLEMENTS
                ]
                catalog.add_preclassified_relations(preclassified_items)
                catalog.add_relation_assertions(unresolved_items)
                preclassified_ids = {
                    str(item.get("assertionId") or "")
                    for item in preclassified_items
                }
                preclassified_relations = [
                    relation
                    for relation in catalog.prompt_graph_relations(
                        max_items=GRAPH_RELATIONS_PER_TOOL_RESULT * 2
                    )
                    if str(relation.get("assertionId") or "")
                    in preclassified_ids
                ]
                normalized_assertions: list[dict[str, Any]] = []
                for item in unresolved_items:
                    assertion_id = str(
                        item.get("assertionId")
                        or item.get("graphNodeId")
                        or ""
                    )
                    normalized = catalog.relation_assertion(assertion_id)
                    if normalized is not None:
                        normalized_assertions.append(normalized)
                relation_assertions = tuple(normalized_assertions)
                remaining_graph_sec = timeout_sec - (perf_counter() - started)
                paths = (
                    self._graph_paths_per_article(
                        action.articleIds,
                        edge_types=action.edgeTypes or None,
                        user_clearance_level=user_clearance_level,
                        timeout_sec=max(0.1, remaining_graph_sec),
                    )
                    if remaining_graph_sec > 0.1
                    else []
                )
                trusted_paths = _trusted_graph_paths(paths)
                result_count = (
                    len(trusted_paths)
                    + len(preclassified_relations)
                    + len(relation_assertions)
                )
                catalog.add_graph_paths(trusted_paths)
                graph_relations = tuple(
                    _diversify_graph_relations(
                        [
                            *compact_graph_relations(trusted_paths),
                            *preclassified_relations,
                        ],
                        max_items=GRAPH_RELATIONS_PER_TOOL_RESULT,
                    )
                )
            else:  # ResearchActionのschemaで通常は到達しない
                raise ValueError(f"unsupported research tool: {action.tool}")
            error = None
        except Exception as exc:  # noqa: BLE001 - 調査失敗を回答経路へ波及させない
            result_count = 0
            error = f"{type(exc).__name__}: {exc}"
        return ResearchToolExecution(
            tool=action.tool,
            result_count=result_count,
            new_evidence_count=len(set(catalog.content_unit_ids) - evidence_before),
            new_article_count=len(set(catalog.known_article_ids) - articles_before),
            elapsed_ms=int((perf_counter() - started) * 1000),
            error=error,
            new_content_unit_ids=tuple(
                content_unit_id
                for content_unit_id in catalog.content_unit_ids
                if content_unit_id not in evidence_before
            ),
            returned_content_unit_ids=returned_content_unit_ids,
            new_article_ids=tuple(
                article_id
                for article_id in catalog.known_article_ids
                if article_id not in articles_before
            ),
            auto_graph_path_count=auto_graph_path_count,
            auto_graph_article_ids=auto_graph_article_ids,
            graph_relations=graph_relations,
            relation_assertions=relation_assertions,
            auto_graph_error=auto_graph_error,
        )

    def _graph_paths_per_article(
        self,
        article_ids: list[str],
        *,
        edge_types: list[str] | None,
        user_clearance_level: int,
        timeout_sec: float,
        limit_per_article: int = GRAPH_PATHS_PER_ARTICLE,
    ) -> list[dict[str, Any]]:
        """起点ごとの上限を分け、一つのArticleがGraph候補を独占しないようにする。"""
        started = perf_counter()
        paths: list[dict[str, Any]] = []
        for article_id in dict.fromkeys(article_ids):
            remaining = timeout_sec - (perf_counter() - started)
            if remaining <= 0.1:
                break
            paths.extend(
                self.graph_client.paths_from_many(
                    [article_id],
                    edge_types=edge_types,
                    max_depth=1,
                    limit=limit_per_article,
                    user_clearance_level=user_clearance_level,
                    timeout_sec=max(0.1, remaining),
                )
            )
        return paths

    def _search(
        self,
        action: ResearchAction,
        catalog: EvidenceCatalog,
        *,
        user_clearance_level: int,
        timeout_sec: float,
    ) -> int:
        results: list[dict[str, Any]] = []
        if action.documentIds:
            # LLM主導経路も、日本語Analyzer・BM25/vector・Article集約を備えた
            # 共通multi-searchへ接続する。旧BM25専用APIへは戻さない。
            specs = [
                RequirementSearchSpec(
                    requirement_id=f"llm-research-{doc_index}-{doc_type}",
                    query=str(action.query or ""),
                    document_ids=(document_id,),
                    top_k=settings.llm_research_document_search_top_k,
                    doc_type=doc_type,
                )
                for doc_index, document_id in enumerate(action.documentIds)
                for doc_type in (action.docTypes or ["law", "guideline"])
            ]
            batches = self.os_client.search_requirement_specs(
                specs,
                user_clearance_level=user_clearance_level,
                timeout_sec=max(0.1, timeout_sec),
            )
            for spec in specs:
                for candidate in batches.get(spec.requirement_id, []):
                    if spec.doc_type == "law":
                        # search_requirement_specsの候補単位はArticleだが、chunksを
                        # 全展開すると1回の検索で数十項がカタログを占有する。
                        # 初回検索では順位最上位の代表chunkだけを提示し、LLMが
                        # 必要と判断したArticleの全項号はfetch_articlesで取得する。
                        chunks = candidate.get("chunks") or []
                        representative = (
                            chunks[0]
                            if chunks
                            else candidate.get("source") or candidate
                        )
                        if representative:
                            results.append(representative)
                    else:
                        results.append(candidate.get("source") or candidate)
            catalog.add_results(results)
            return len(results)
        # 未指定なら両レーンを検索する。ガイドだけを法令根拠扱いする判断はここではしない。
        for doc_type in action.docTypes or ["law", "guideline"]:
            results.extend(
                self.os_client.search(
                    str(action.query or ""),
                    doc_type,
                    settings.llm_research_search_top_k,
                    user_clearance_level,
                    settings.agent_use_bm25,
                    settings.agent_use_vector,
                )
            )
        catalog.add_results(results)
        return len(results)


def _trusted_graph_paths(paths: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """本文取得候補へ使える、実装済みかつ検証済みの関係だけを残す。"""
    return [
        path
        for path in paths
        if path.get("edges")
        and all(
            is_trusted_relation(edge)
            for edge in path.get("edges") or []
        )
    ]


def _diversify_graph_relations(
    relations: list[dict[str, Any]],
    *,
    max_items: int,
) -> list[dict[str, Any]]:
    """Graph関係を起点Articleごとのラウンドロビンで圧縮する。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    for relation in relations:
        key = str(relation.get("fromArticleId") or "__unknown__")
        groups.setdefault(key, []).append(relation)
    output: list[dict[str, Any]] = []
    offsets = {key: 0 for key in groups}
    while len(output) < max(0, max_items):
        added = False
        for key, items in groups.items():
            offset = offsets[key]
            if offset >= len(items):
                continue
            output.append(items[offset])
            offsets[key] = offset + 1
            added = True
            if len(output) >= max_items:
                break
        if not added:
            break
    return output


def _content_unit_id(item: dict[str, Any]) -> str:
    source = item.get("document") or item.get("source") or item
    if not isinstance(source, dict):
        return ""
    return str(source.get("contentUnitId") or "")
