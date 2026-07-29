"""LLM主導法令調査から、投入済み検索基盤だけを呼び出すtool gateway。"""

from dataclasses import dataclass
from time import perf_counter
from typing import Any

from .config import settings
from .graph_client import GraphClient
from .legal_ontology import expandable_edge_types, is_trusted_relation
from .llm_directed_research import (
    TOOL_EXPAND_GRAPH,
    TOOL_FETCH_ARTICLES,
    TOOL_SEARCH_CORPUS,
    EvidenceCatalog,
    ResearchAction,
)
from .opensearch_client import OpenSearchClient, RequirementSearchSpec

AUTO_GRAPH_TIMEOUT_SEC = 5.0


@dataclass(frozen=True)
class ResearchToolExecution:
    tool: str
    result_count: int
    new_evidence_count: int
    new_article_count: int
    elapsed_ms: int
    error: str | None = None
    new_content_unit_ids: tuple[str, ...] = ()
    new_article_ids: tuple[str, ...] = ()
    auto_graph_path_count: int = 0
    auto_graph_article_ids: tuple[str, ...] = ()
    auto_graph_error: str | None = None

    def as_trace(self, action: ResearchAction) -> dict[str, Any]:
        trace = {
            "tool": self.tool,
            "query": action.query,
            "articleIds": action.articleIds,
            "documentIds": action.documentIds,
            "docTypes": action.docTypes,
            "edgeTypes": action.edgeTypes,
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
            trace["autoGraphPathCount"] = self.auto_graph_path_count
            trace["autoGraphArticleIds"] = list(self.auto_graph_article_ids)
            trace["autoGraphError"] = self.auto_graph_error
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
                results = self.os_client.get_by_article_ids(
                    action.articleIds,
                    user_clearance_level,
                    max_chunks=max(
                        settings.llm_research_search_top_k,
                        len(action.articleIds)
                        * settings.llm_research_search_top_k,
                    ),
                )
                result_count = len(results)
                catalog.add_results(results)
                # LLMがArticleを選んだ時点で、seed時に本文根拠を検証済みの
                # 委任・準用・参照関係だけを1hop取得する。関係先を採用するか、
                # さらに本文を取得するかは次ターンのLLM判断へ委ねる。
                graph_articles_before = set(catalog.known_article_ids)
                try:
                    paths = self.graph_client.paths_from_many(
                        action.articleIds,
                        edge_types=list(expandable_edge_types()),
                        max_depth=1,
                        limit=settings.agent_max_graph_paths,
                        user_clearance_level=user_clearance_level,
                        timeout_sec=max(
                            0.1,
                            min(
                                AUTO_GRAPH_TIMEOUT_SEC,
                                timeout_sec - (perf_counter() - started),
                            ),
                        ),
                    )
                    trusted_paths = _trusted_graph_paths(paths)
                    auto_graph_path_count = len(trusted_paths)
                    catalog.add_graph_paths(trusted_paths)
                    auto_graph_article_ids = tuple(
                        article_id
                        for article_id in catalog.known_article_ids
                        if article_id not in graph_articles_before
                    )
                except Exception as exc:  # noqa: BLE001 - 本文取得は成功扱いのまま残す
                    auto_graph_error = f"{type(exc).__name__}: {exc}"
            elif action.tool == TOOL_EXPAND_GRAPH:
                paths = self.graph_client.paths_from_many(
                    action.articleIds,
                    edge_types=action.edgeTypes or None,
                    max_depth=1,
                    limit=settings.agent_max_graph_paths,
                    user_clearance_level=user_clearance_level,
                    timeout_sec=max(0.1, timeout_sec),
                )
                trusted_paths = _trusted_graph_paths(paths)
                result_count = len(trusted_paths)
                catalog.add_graph_paths(trusted_paths)
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
            new_article_ids=tuple(
                article_id
                for article_id in catalog.known_article_ids
                if article_id not in articles_before
            ),
            auto_graph_path_count=auto_graph_path_count,
            auto_graph_article_ids=auto_graph_article_ids,
            auto_graph_error=auto_graph_error,
        )

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
