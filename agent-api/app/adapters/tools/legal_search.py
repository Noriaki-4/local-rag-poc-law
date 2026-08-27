"""既存OpenSearchを法令固有のread-only Toolへ変換する。"""

from __future__ import annotations

import hashlib
import json
from time import perf_counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, ValidationError

from app.agent_framework.ports.tool import ToolDefinition, ToolExecution
from app.agent_framework.state import Evidence, ToolRequest, ToolResult
from app.agent_framework.tool_contracts import model_input_schema
from app.config import settings
from app.domains.legal.graph_schema import (
    GraphDirection,
    GraphSearchMode,
    ProposedPredicate,
)
from app.graph_client import GraphClient
from app.opensearch_client import OpenSearchClient, RequirementSearchSpec

_SEARCH_NAVIGATION_TEXT_LIMIT = 400
_DETAILED_ARTICLE_NAVIGATION_TEXT_LIMIT = 5000
_MAX_ARTICLES_PER_FETCH = 5
_MAX_GRAPH_ROOT_ARTICLES = 4


class _LegalSearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        min_length=1,
        max_length=1000,
        description=(
            "検索欄へ入力する短い法令用語・法令表現の組合せ。"
            "確認内容を説明する文章ではない。"
        ),
    )
    doc_types: tuple[str, ...] = Field(
        default=("law", "guideline"),
        description=(
            "検索対象。法令本文はlaw、行政解釈やガイドはguideline。"
            "現在の検索に必要な値を配列で返す。"
        ),
    )
    document_ids: tuple[str, ...] = Field(
        default=(),
        max_length=12,
        description=(
            "検索対象文書を限定する既知documentId。"
            "限定しない場合は空配列。"
        ),
    )


class _FetchArticlesArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    article_ids: tuple[str, ...] = Field(
        min_length=1,
        max_length=_MAX_ARTICLES_PER_FETCH,
        description=(
            "本文を取得する既知Article ID。SolverContext.fetchable_article_idsの"
            "完全一致だけを指定する。"
        ),
    )


class _GraphNeighborsArgumentsBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    article_ids: tuple[str, ...] = Field(
        min_length=1,
        max_length=_MAX_GRAPH_ROOT_ARTICLES,
        description="1ホップの起点にする既知Article ID。",
    )
    max_relations: int = Field(
        default=50,
        ge=1,
        le=50,
        description="起点Articleごとに返すnavigation関係数の上限。",
    )


class _SemanticGraphNeighborsArguments(_GraphNeighborsArgumentsBase):
    mode: Literal[GraphSearchMode.SEMANTIC_ASSERTION] = Field(
        description="意味分類済みRelationAssertionを探索する。",
    )
    predicate: ProposedPredicate = Field(
        description="現在の仮説に合う1つの意味関係。",
    )
    direction: GraphDirection = Field(
        description=(
            "起点ArticleをSUBJECTまたはOBJECTのどちらとして探索するか。"
        ),
    )


class _ExplicitReferenceGraphNeighborsArguments(_GraphNeighborsArgumentsBase):
    mode: Literal[GraphSearchMode.EXPLICIT_REFERENCE] = Field(
        description="本文に明示されたREFERENCESを探索する。",
    )
    predicate: None = Field(
        default=None,
        description="物理参照の探索ではnull。",
    )
    direction: Literal["outgoing", "incoming"] = Field(
        description=(
            "outgoingは起点本文が明示参照する先を探す。incomingは起点を"
            "明示参照する条文を探し、Article IDが不明な下位規範を逆引きする。"
        ),
    )


class _ExplainsGraphNeighborsArguments(_GraphNeighborsArgumentsBase):
    mode: Literal[GraphSearchMode.EXPLAINS] = Field(
        description="ガイドからArticleへのEXPLAINSを探索する。",
    )
    predicate: None = Field(
        default=None,
        description="EXPLAINSの探索ではnull。",
    )
    direction: Literal["outgoing", "incoming"] = Field(
        description="起点を解説元または解説先のどちらとして探索するか。",
    )


class _GraphNeighborsArguments(
    RootModel[
        _SemanticGraphNeighborsArguments
        | _ExplicitReferenceGraphNeighborsArguments
        | _ExplainsGraphNeighborsArguments
    ]
):
    @property
    def article_ids(self) -> tuple[str, ...]:
        return self.root.article_ids

    @property
    def mode(self) -> GraphSearchMode:
        return GraphSearchMode(self.root.mode)

    @property
    def predicate(self) -> ProposedPredicate | None:
        return self.root.predicate

    @property
    def direction(self) -> str:
        value = self.root.direction
        return value.value if isinstance(value, GraphDirection) else value

    @property
    def max_relations(self) -> int:
        return self.root.max_relations


class LegalSearchTool:
    definition = ToolDefinition(
        name="legal_search",
        description=(
            "Article IDがまだ分からないとき、OpenSearchで法令またはガイドの候補を探す。"
            "質問をそのまま繰り返さず、短い法令用語・法令表現を組み合わせて使う。"
            "返す検索抜粋は候補選択用であり、回答やHypothesisの根拠にはしない。"
        ),
        input_schema=model_input_schema(_LegalSearchArguments),
        result_description=(
            "候補Articleの所在と短い検索抜粋をnavigation Evidenceとして返す。"
            "Article本文のgrounding Evidenceは返さない。"
        ),
        read_only=True,
        parallel_safe=True,
    )

    def __init__(
        self,
        client: OpenSearchClient,
        *,
        user_clearance_level: int,
        top_k: int | None = None,
    ) -> None:
        self._client = client
        self._user_clearance_level = user_clearance_level
        self._top_k = top_k or settings.llm_research_search_top_k

    def execute(
        self,
        request: ToolRequest,
        *,
        cycle_no: int,
        timeout_sec: float,
    ) -> ToolExecution:
        started = perf_counter()
        arguments = _parse_arguments(_LegalSearchArguments, request.arguments)
        raw_results = self._article_aware_search(arguments, timeout_sec)
        evidence = _evidence_from_results(
            raw_results,
            cycle_no,
            search_candidates=True,
        )
        return _successful_execution(request, evidence, cycle_no, started)

    def _article_aware_search(
        self,
        arguments: _LegalSearchArguments,
        timeout_sec: float,
    ) -> list[dict[str, Any]]:
        doc_types = tuple(dict.fromkeys(arguments.doc_types))
        unsupported = set(doc_types) - {"law", "guideline"}
        if unsupported:
            raise ValueError(f"unsupported doc_type: {sorted(unsupported)}")

        batched_search = getattr(self._client, "search_requirement_specs", None)
        if not callable(batched_search):
            results: list[dict[str, Any]] = []
            for doc_type in doc_types:
                results.extend(
                    self._client.search(
                        arguments.query,
                        doc_type,
                        self._top_k,
                        self._user_clearance_level,
                        settings.agent_use_bm25,
                        settings.agent_use_vector,
                    )
                )
            return results

        specs = [
            RequirementSearchSpec(
                requirement_id=f"framework-search-{index}",
                query=arguments.query,
                document_ids=arguments.document_ids,
                top_k=(self._top_k if doc_type == "law" else min(2, self._top_k)),
                doc_type=doc_type,
            )
            for index, doc_type in enumerate(doc_types)
        ]
        batches = batched_search(
            specs,
            user_clearance_level=self._user_clearance_level,
            timeout_sec=max(0.1, timeout_sec),
        )
        results = []
        for spec in specs:
            for candidate_index, candidate in enumerate(
                batches.get(spec.requirement_id, [])
            ):
                if spec.doc_type == "law":
                    chunks = candidate.get("chunks") or []
                    representative = (
                        _law_navigation_candidate(
                            chunks,
                            detail_chunk_limit=(
                                len(chunks) if candidate_index == 0 else 1
                            ),
                        )
                        if chunks
                        else _bounded_navigation_document(
                            candidate.get("source") or candidate
                        )
                    )
                else:
                    representative = _bounded_navigation_document(
                        candidate.get("source") or candidate
                    )
                if representative:
                    results.append({"document": representative})
        return results


class LegalFetchArticlesTool:
    definition = ToolDefinition(
        name="fetch_articles",
        description=(
            "発見済みArticleの本文をOpenSearchから取得する。"
            "fetchable_article_idsにある既知IDから、質問とHypothesisに必要なものを選んで使う。"
            "検索やGraph展開は行わず、未知IDや取得済みEvidence IDは受け付けない。"
        ),
        input_schema=model_input_schema(_FetchArticlesArguments),
        result_description=(
            "指定Articleに属するArticle・Paragraph・Itemの本文をgrounding Evidenceとして返す。"
        ),
        read_only=True,
        parallel_safe=True,
    )

    def __init__(
        self,
        client: OpenSearchClient,
        *,
        user_clearance_level: int,
    ) -> None:
        self._client = client
        self._user_clearance_level = user_clearance_level

    def execute(
        self,
        request: ToolRequest,
        *,
        cycle_no: int,
        timeout_sec: float,
    ) -> ToolExecution:
        del timeout_sec
        started = perf_counter()
        arguments = _parse_arguments(_FetchArticlesArguments, request.arguments)
        raw_results: list[dict[str, Any]] = []
        for article_id in dict.fromkeys(arguments.article_ids):
            raw_results.extend(
                self._client.get_by_article_ids(
                    [article_id],
                    self._user_clearance_level,
                    max_chunks=settings.llm_research_max_chunks_per_article,
                )
            )
        evidence = _evidence_from_results(raw_results, cycle_no)
        return _successful_execution(request, evidence, cycle_no, started)


class LegalGraphNeighborsTool:
    definition = ToolDefinition(
        name="legal_graph_neighbors",
        description=(
            "既知Articleを起点に、仮説に合う法令関係を1ホップだけ探索する。"
            "関係のmode、意味predicate、向きを説明できる場合に使う。"
            "返す候補は本文ではなくnavigation情報であり、関連性と次の取得対象はSolverが判断する。"
        ),
        input_schema=model_input_schema(_GraphNeighborsArguments),
        result_description=(
            "起点と隣接Articleの関係、向き、根拠所在をGraph navigation Evidenceとして返す。"
            "隣接Articleの本文は返さない。"
        ),
        read_only=True,
        parallel_safe=True,
    )

    def __init__(
        self,
        client: GraphClient,
        *,
        user_clearance_level: int,
    ) -> None:
        self._client = client
        self._user_clearance_level = user_clearance_level

    def execute(
        self,
        request: ToolRequest,
        *,
        cycle_no: int,
        timeout_sec: float,
    ) -> ToolExecution:
        started = perf_counter()
        arguments = _parse_arguments(_GraphNeighborsArguments, request.arguments)
        # Scan a larger mechanical pool because several paragraph/item edges may
        # collapse into one Article-pair candidate before max_relations applies.
        lookup_limit = min(max(arguments.max_relations * 10, 100), 500)
        formal_lookup = getattr(self._client, "article_relations_touching", None)
        assertion_lookup = getattr(self._client, "relation_assertions_touching", None)
        evidence_groups: list[tuple[Evidence, ...]] = []

        # Neo4j applies LIMIT before the tool can diversify candidates. Querying
        # multiple high-degree Articles together therefore lets the first seed
        # exhaust the shared window and hides every relation of later seeds.
        # Keep an independent, mechanical window per seed; the Solver still
        # decides which returned endpoint is legally relevant.
        for article_id in dict.fromkeys(arguments.article_ids):
            remaining = timeout_sec - (perf_counter() - started)
            if remaining <= 0.1:
                break
            formal: list[dict[str, Any]] = []
            assertions: list[dict[str, Any]] = []
            if arguments.mode is GraphSearchMode.SEMANTIC_ASSERTION:
                if callable(assertion_lookup):
                    assertions = assertion_lookup(
                        [article_id],
                        proposed_predicate=arguments.predicate.value,
                        direction=arguments.direction,
                        classification_run_id=(
                            settings.legal_relation_classification_run_id
                        ),
                        user_clearance_level=self._user_clearance_level,
                        limit=lookup_limit,
                        timeout_sec=max(0.1, remaining),
                    )
            elif callable(formal_lookup):
                edge_type = (
                    "REFERENCES"
                    if arguments.mode is GraphSearchMode.EXPLICIT_REFERENCE
                    else "EXPLAINS"
                )
                formal = formal_lookup(
                    [article_id],
                    edge_types=[edge_type],
                    direction=arguments.direction,
                    user_clearance_level=self._user_clearance_level,
                    limit=lookup_limit,
                    timeout_sec=max(0.1, remaining),
                )
            evidence_groups.append(
                _graph_navigation_evidence(
                    formal,
                    assertions,
                    cycle_no,
                    seed_article_ids=(article_id,),
                    max_items=arguments.max_relations,
                )
            )

        evidence = _round_robin_evidence(evidence_groups)
        return _successful_execution(request, evidence, cycle_no, started)


def _parse_arguments(model_type, arguments: dict[str, Any]):
    try:
        return model_type.model_validate(arguments)
    except ValidationError as exc:
        raise ValueError("tool arguments violate schema") from exc


def _successful_execution(
    request: ToolRequest,
    evidence: tuple[Evidence, ...],
    cycle_no: int,
    started: float,
) -> ToolExecution:
    return ToolExecution(
        result=ToolResult(
            request_id=request.request_id,
            status="succeeded",
            evidence_ids=tuple(item.evidence_id for item in evidence),
            elapsed_ms=max(0, round((perf_counter() - started) * 1000)),
            cycle_no=cycle_no,
        ),
        evidence=evidence,
    )


def _evidence_from_results(
    results: list[dict[str, Any]],
    cycle_no: int,
    *,
    search_candidates: bool = False,
) -> tuple[Evidence, ...]:
    evidence_by_id: dict[str, Evidence] = {}
    for raw in results:
        source = raw.get("document") or raw.get("source") or raw
        if not isinstance(source, dict):
            continue
        source_content_unit_id = str(source.get("contentUnitId") or "")
        content = str(source.get("text") or "").strip()
        if not source_content_unit_id or not content:
            continue
        is_search_candidate = search_candidates
        evidence_id = (
            _search_navigation_evidence_id(
                str(source.get("searchNavigationKey") or source_content_unit_id)
            )
            if is_search_candidate
            else source_content_unit_id
        )
        article_id = (
            str(
                source.get("articleContentUnitId")
                or source.get("parentContentUnitId")
                or source_content_unit_id.split("-paragraph-", 1)[0]
            )
            if source.get("docType") == "law"
            else None
        )
        evidence_by_id.setdefault(
            evidence_id,
            Evidence(
                evidence_id=evidence_id,
                source_ref=f"opensearch:{source_content_unit_id}",
                title=str(source.get("title") or "") or None,
                content=content,
                created_cycle=cycle_no,
                metadata={
                    "articleId": article_id,
                    "sourceContentUnitId": source_content_unit_id,
                    "documentId": source.get("documentId"),
                    "docType": source.get("docType"),
                    "citationEligible": not is_search_candidate,
                    "evidenceRole": (
                        "search_navigation" if is_search_candidate else "retrieved_text"
                    ),
                    "heading": source.get("heading"),
                    "sourceObjectUri": source.get("sourceObjectUri"),
                    "sourcePage": source.get("sourcePage"),
                    "matchedChunkCount": source.get("matchedChunkCount"),
                    "navigationTextTruncated": bool(
                        source.get("navigationTextTruncated")
                    ),
                },
            ),
        )
    return tuple(evidence_by_id.values())


def _law_navigation_candidate(
    chunks: list[dict[str, Any]],
    *,
    detail_chunk_limit: int,
) -> dict[str, Any]:
    """Article候補の上位一致chunkを、意味選別せず検索順位どおり提示する。"""
    selected = chunks[: max(1, detail_chunk_limit)]
    representative = dict(selected[0])
    if len(selected) == 1:
        return _bounded_navigation_document(representative)

    content_parts: list[str] = []
    content_unit_ids: list[str] = []
    base_text = str(selected[0].get("text") or "").strip()
    repeated_prefix = base_text.lstrip("0123456789０１２３４５６７８９ ")
    truncated = False
    for chunk_index, chunk in enumerate(selected, start=1):
        content_unit_id = str(chunk.get("contentUnitId") or "")
        text = str(chunk.get("text") or "").strip()
        if not content_unit_id or not text:
            continue
        if content_parts and repeated_prefix and text.startswith(repeated_prefix):
            text = text[len(repeated_prefix) :].lstrip()
            if not text:
                continue
        content_unit_ids.append(content_unit_id)
        heading = str(chunk.get("heading") or "").strip()
        label = f"[一致箇所{chunk_index}]"
        if heading:
            label = f"{label} {heading}"
        part = f"{label}\n{text}"
        current_chars = sum(len(item) for item in content_parts) + 2 * len(
            content_parts
        )
        remaining_chars = _DETAILED_ARTICLE_NAVIGATION_TEXT_LIMIT - current_chars
        if remaining_chars <= len(label) + 1:
            truncated = True
            break
        if len(part) > remaining_chars:
            part = part[:remaining_chars]
            truncated = True
        content_parts.append(part)
        if truncated:
            break

    if content_parts:
        representative["text"] = "\n\n".join(content_parts)
        representative["matchedChunkCount"] = len(content_unit_ids)
        representative["navigationTextTruncated"] = truncated
        signature = json.dumps(
            {
                "contentUnitIds": content_unit_ids,
                "content": representative["text"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        representative["searchNavigationKey"] = (
            f"{representative.get('contentUnitId')}:"
            f"{hashlib.sha256(signature.encode('utf-8')).hexdigest()[:16]}"
        )
    return representative


def _bounded_navigation_document(source: dict[str, Any]) -> dict[str, Any]:
    bounded = dict(source)
    content = str(bounded.get("text") or "")
    if len(content) > _SEARCH_NAVIGATION_TEXT_LIMIT:
        bounded["text"] = content[:_SEARCH_NAVIGATION_TEXT_LIMIT]
        bounded["navigationTextTruncated"] = True
    return bounded


def _search_navigation_evidence_id(content_unit_id: str) -> str:
    candidate = f"search-nav-{content_unit_id}"
    if len(candidate) <= 160:
        return candidate
    digest = hashlib.sha256(content_unit_id.encode("utf-8")).hexdigest()
    return f"search-nav-{digest}"


def _round_robin_evidence(
    groups: list[tuple[Evidence, ...]],
) -> tuple[Evidence, ...]:
    evidence_by_id: dict[str, Evidence] = {}
    for index in range(max((len(group) for group in groups), default=0)):
        for group in groups:
            if index < len(group):
                item = group[index]
                evidence_by_id.setdefault(item.evidence_id, item)
    return tuple(evidence_by_id.values())


def _graph_navigation_evidence(
    formal_relations: list[dict[str, Any]],
    assertions: list[dict[str, Any]],
    cycle_no: int,
    *,
    seed_article_ids: tuple[str, ...],
    max_items: int,
) -> tuple[Evidence, ...]:
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    relation_signatures: dict[tuple[str, str], set[str]] = {}

    for kind, relations in (
        ("formal_relation", formal_relations),
        ("relation_assertion", assertions),
    ):
        for relation in relations:
            is_semantic_assertion = bool(
                kind == "relation_assertion"
                and relation.get("proposedPredicate")
                and relation.get("subjectArticleId")
                and relation.get("objectArticleId")
            )
            from_article_id = str(
                relation.get(
                    "subjectArticleId" if is_semantic_assertion else "fromArticleId"
                )
                or ""
            )
            to_article_id = str(
                relation.get(
                    "objectArticleId" if is_semantic_assertion else "toArticleId"
                )
                or ""
            )
            for seed_article_id in (
                item
                for item in seed_article_ids
                if item in (from_article_id, to_article_id)
            ):
                neighbor_article_id = (
                    to_article_id
                    if from_article_id == seed_article_id
                    else from_article_id
                )
                if not neighbor_article_id or neighbor_article_id == seed_article_id:
                    continue
                outgoing = from_article_id == seed_article_id
                key = (seed_article_id, neighbor_article_id)
                candidate = candidates.setdefault(
                    key,
                    {
                        "seedArticleId": seed_article_id,
                        "seedDocumentId": relation.get(
                            "fromDocumentId" if outgoing else "toDocumentId"
                        ),
                        "seedTitle": relation.get(
                            "fromTitle" if outgoing else "toTitle"
                        ),
                        "seedHeading": relation.get(
                            "fromHeading" if outgoing else "toHeading"
                        ),
                        "neighborArticleId": neighbor_article_id,
                        "neighborDocumentId": relation.get(
                            "toDocumentId" if outgoing else "fromDocumentId"
                        ),
                        "neighborTitle": relation.get(
                            "toTitle" if outgoing else "fromTitle"
                        ),
                        "neighborHeading": relation.get(
                            "toHeading" if outgoing else "fromHeading"
                        ),
                        "relations": [],
                    },
                )
                descriptor = {
                    key: value
                    for key, value in {
                        "kind": kind,
                        "edgeType": relation.get("edgeType")
                        or relation.get("proposedPredicate")
                        or relation.get("suggestedType"),
                        "direction": (
                            "from_subject" if outgoing else "to_subject"
                        )
                        if is_semantic_assertion
                        else ("outgoing" if outgoing else "incoming"),
                        "status": relation.get("status"),
                        "referenceKind": relation.get("referenceKind"),
                        "relationSource": relation.get("relationSource")
                        or relation.get("assertionSource"),
                        "sourceId": relation.get("graphEdgeId")
                        or relation.get("assertionId"),
                        "derivedFromEdgeId": relation.get("derivedFromEdgeId"),
                        "basisEdgeId": relation.get("basisEdgeId"),
                        "classificationRunId": relation.get("classificationRunId"),
                        "subjectArticleId": relation.get("subjectArticleId"),
                        "objectArticleId": relation.get("objectArticleId"),
                        "subjectSupportingSpanId": relation.get(
                            "subjectSupportingSpanId"
                        ),
                        "objectSupportingSpanId": relation.get(
                            "objectSupportingSpanId"
                        ),
                        "subjectSupportingQuote": relation.get(
                            "subjectSupportingQuote"
                        ),
                        "objectSupportingQuote": relation.get(
                            "objectSupportingQuote"
                        ),
                        "relationExplanation": relation.get("relationExplanation"),
                    }.items()
                    if value is not None
                }
                signature = json.dumps(
                    descriptor,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                signatures = relation_signatures.setdefault(key, set())
                if signature not in signatures:
                    signatures.add(signature)
                    candidate["relations"].append(descriptor)

    groups: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates.values():
        document_id = str(candidate.get("neighborDocumentId") or "unknown")
        groups.setdefault(document_id, []).append(candidate)

    selected: list[dict[str, Any]] = []
    while groups and len(selected) < max_items:
        exhausted: list[str] = []
        for document_id, items in groups.items():
            if items and len(selected) < max_items:
                selected.append(items.pop(0))
            if not items:
                exhausted.append(document_id)
        for document_id in exhausted:
            groups.pop(document_id)

    evidence: list[Evidence] = []
    for payload in selected:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        identifier = hashlib.sha256(
            (
                f"{payload['seedArticleId']}\0"
                f"{payload['neighborArticleId']}"
            ).encode("utf-8")
        ).hexdigest()[:24]
        graph_kinds = tuple(
            dict.fromkeys(
                str(item.get("kind") or "") for item in payload["relations"]
            )
        )
        edge_types = tuple(
            dict.fromkeys(
                str(item.get("edgeType") or "") for item in payload["relations"]
            )
        )
        evidence.append(
            Evidence(
                evidence_id=f"graph-nav-article-pair-{identifier}",
                source_ref=f"neo4j:article_pair:{identifier}",
                title="Graph navigation candidate",
                content=encoded,
                created_cycle=cycle_no,
                metadata={
                    "docType": "graph_navigation",
                    "citationEligible": False,
                    "graphKinds": graph_kinds,
                    "edgeTypes": edge_types,
                    "fromArticleId": payload["seedArticleId"],
                    "toArticleId": payload["neighborArticleId"],
                    "seedArticleId": payload["seedArticleId"],
                    "neighborArticleId": payload["neighborArticleId"],
                    "seedDocumentId": payload["seedDocumentId"],
                    "seedTitle": payload["seedTitle"],
                    "seedHeading": payload["seedHeading"],
                    "neighborDocumentId": payload["neighborDocumentId"],
                    "neighborTitle": payload["neighborTitle"],
                    "neighborHeading": payload["neighborHeading"],
                },
            )
        )
    return tuple(evidence)
