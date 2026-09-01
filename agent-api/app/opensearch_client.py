import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import requests

from .config import settings
from .embeddings import embed_text, embed_texts
from .legal_titles import law_title_names
from .legal_ontology import authority_type_rank, search_authority_types

# 附則(施行期日・経過措置・改正沿革)が根拠になりうる質問の手がかり。
SUPPLEMENTARY_PROVISION_CUES = (
    "附則",
    "経過措置",
    "施行日",
    "施行期日",
    "いつから",
    "改正前",
    "改正後",
    "旧法",
    "適用関係",
    "みなし規定",
)

_LEGAL_NUMBER = r"[一二三四五六七八九十百千〇零\d]+"
LEGAL_REFERENCE_PHRASE_PATTERN = re.compile(
    rf"(?:(?:当該|同|本)?(?:内閣府令|主務省令|省令|政令|法律|法|令))?"
    rf"第{_LEGAL_NUMBER}条(?:の{_LEGAL_NUMBER})*"
    rf"(?:第{_LEGAL_NUMBER}項)?(?:第{_LEGAL_NUMBER}号)?"
)
_ARTICLE_REFERENCE_PATTERN = re.compile(
    rf"第({_LEGAL_NUMBER})条((?:の{_LEGAL_NUMBER})*)"
)
_JAPANESE_DIGITS = {
    "〇": 0,
    "零": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_JAPANESE_UNITS = {"十": 10, "百": 100, "千": 1000}
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


@dataclass(frozen=True)
class RequirementSearchSpec:
    """1つのEvidenceRequirementに対する検索範囲(計画書 §9.1, §9.2)。

    元の質問全文ではなく、論点・法的役割・レイヤー・親条文から作った専用クエリを使う。
    """

    requirement_id: str
    query: str
    authority_type: str | None = None
    document_ids: tuple[str, ...] = ()
    article_ids: tuple[str, ...] = ()
    top_k: int = 10
    key_terms: tuple[str, ...] = ()
    # 同一法令系統への絞り込み。document_idsと違い、レイヤー(authorityType)の
    # 絞り込みは維持する(§6.3-7, §9.1)。
    family_document_ids: tuple[str, ...] = ()
    # 法令レーンとガイドレーンは同じ候補枠で競争させない(§10)。同じmulti-searchへ
    # まとめても、doc_typeで結果を別レーンとして扱う。
    doc_type: str = "law"


@dataclass(frozen=True)
class ArticleCandidate:
    """Article単位に集約した候補。項・号はArticle確定後に選ぶ(§9.3, §11.8)。"""

    article_id: str
    document_id: str
    requirement_id: str
    score: float = 0.0
    authority_type: str | None = None
    authority_rank: int = 0
    heading: str = ""
    text: str = ""
    chunks: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def _articles_from_hits(
    hits: list[dict[str, Any]],
    spec: RequirementSearchSpec,
) -> list[dict[str, Any]]:
    """chunkヒットをArticle単位へ集約する。

    同じArticleの項・号chunksが候補枠を消費する問題(§2-4)を避けるため、候補管理は
    Article単位で行い、chunkは付随情報として保持する。
    """
    by_article: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for hit in hits:
        source = hit.get("_source") or {}
        article_id = str(
            source.get("articleContentUnitId")
            or source.get("parentContentUnitId")
            or source.get("contentUnitId")
            or ""
        ).split("-paragraph-", 1)[0]
        if not article_id:
            continue
        score = float(hit.get("_score") or 0.0)
        if article_id not in by_article:
            order.append(article_id)
            by_article[article_id] = {
                "articleId": article_id,
                "documentId": source.get("documentId"),
                "requirementId": spec.requirement_id,
                "score": score,
                "authorityType": source.get("authorityType"),
                "authorityRank": authority_type_rank(
                    spec.authority_type, source.get("authorityType")
                ),
                "heading": source.get("heading") or "",
                "text": source.get("text") or "",
                "chunks": [source],
                "retrievalSources": list(hit.get("_retrieval_sources") or []),
                "directMatch": bool(hit.get("_direct_match")),
            }
            continue
        candidate = by_article[article_id]
        candidate["score"] = max(candidate["score"], score)
        candidate["chunks"].append(source)
        candidate["retrievalSources"] = list(
            dict.fromkeys(
                [
                    *(candidate.get("retrievalSources") or []),
                    *(hit.get("_retrieval_sources") or []),
                ]
            )
        )
        candidate["directMatch"] = bool(
            candidate.get("directMatch") or hit.get("_direct_match")
        )
    ordered = [by_article[article_id] for article_id in order]
    # 完全一致するauthorityTypeを順位上は優先し、未判別候補も落とさない(§5.2)。
    ordered.sort(
        key=lambda candidate: (
            not candidate.get("directMatch"),
            candidate["authorityRank"],
            -candidate["score"],
        )
    )
    return ordered[: spec.top_k]


def _chunks_from_hits(
    hits: list[dict[str, Any]],
    spec: RequirementSearchSpec,
) -> list[dict[str, Any]]:
    """ガイド等、条文単位へ集約しない資料のヒットをchunkのまま返す。"""
    return [
        {
            "contentUnitId": str((hit.get("_source") or {}).get("contentUnitId") or ""),
            "documentId": str((hit.get("_source") or {}).get("documentId") or ""),
            "requirementId": spec.requirement_id,
            "score": float(hit.get("_score") or 0.0),
            "docType": (hit.get("_source") or {}).get("docType"),
            "authorityType": (hit.get("_source") or {}).get("authorityType"),
            "text": (hit.get("_source") or {}).get("text") or "",
            "source": hit.get("_source") or {},
        }
        for hit in hits
    ][: spec.top_k]


def _merge_requirement_hits(
    scored: dict[str, dict[str, Any]],
    hits: list[dict[str, Any]],
    source_kind: str,
) -> None:
    """BM25・vector・直接取得をchunk単位でRRF融合する。"""
    # 一方の検索方式だけで上位枠を占有しないよう、両レーンを同じ重みで融合する。
    weights = {"bm25": 0.5, "vector": 0.5}
    for rank, hit in enumerate(hits, start=1):
        source = hit.get("_source") or {}
        content_unit_id = str(source.get("contentUnitId") or "")
        if not content_unit_id:
            continue
        entry = scored.setdefault(
            content_unit_id,
            {
                "_source": source,
                "_score": 0.0,
                "_retrieval_sources": [],
                "_direct_match": False,
            },
        )
        if source_kind == "direct":
            # 明示条文・高信頼Graph接続先は検索語の順位に依存させない。
            entry["_score"] = max(float(entry["_score"]), 1000.0 - rank)
            entry["_direct_match"] = True
        else:
            entry["_score"] = float(entry["_score"]) + (
                weights[source_kind] / (settings.agent_rrf_k + rank)
            )
        if source_kind not in entry["_retrieval_sources"]:
            entry["_retrieval_sources"].append(source_kind)


class OpenSearchClient:
    def __init__(self) -> None:
        self.base_url = settings.opensearch_url.rstrip("/")
        self.index = settings.opensearch_index
        self._law_titles_cache: dict[str, str] | None = None

    @lru_cache(maxsize=256)
    def _query_embedding(self, query: str, dimension: int) -> tuple[float, ...]:
        """同じ検索語を資料種別ごとに検索しても、Ollamaへの埋め込み要求は1回にする。"""
        return tuple(embed_text(query, dimension))

    def health(self) -> bool:
        try:
            response = requests.get(self.base_url, timeout=3)
            return response.ok
        except requests.RequestException:
            return False

    def recreate_index(self, mapping: dict[str, Any]) -> None:
        requests.delete(f"{self.base_url}/{self.index}", timeout=10)
        body = {key: value for key, value in mapping.items() if key in {"settings", "mappings", "aliases"}}
        response = requests.put(f"{self.base_url}/{self.index}", json=body, timeout=30)
        response.raise_for_status()
        self._law_titles_cache = None
        self._query_embedding.cache_clear()
        self._raw_vector_hits.cache_clear()

    def index_document(self, document: dict[str, Any]) -> None:
        doc_id = document["contentUnitId"]
        response = requests.put(f"{self.base_url}/{self.index}/_doc/{doc_id}", json=document, timeout=15)
        response.raise_for_status()

    def refresh(self) -> None:
        requests.post(f"{self.base_url}/{self.index}/_refresh", timeout=10).raise_for_status()

    def get_by_content_unit_id(self, content_unit_id: str) -> dict[str, Any] | None:
        body = {"query": {"term": {"contentUnitId": content_unit_id}}, "size": 1}
        response = requests.post(f"{self.base_url}/{self.index}/_search", json=body, timeout=10)
        response.raise_for_status()
        hits = response.json()["hits"]["hits"]
        return hits[0]["_source"] if hits else None

    def law_titles(self) -> dict[str, str]:
        """seed済み法令の documentId -> title 対応表を返す(条番号直接解決用)。"""
        if self._law_titles_cache is not None:
            return dict(self._law_titles_cache)
        body = {
            "size": 0,
            "aggs": {
                "docs": {
                    "terms": {"field": "documentId", "size": 100},
                    "aggs": {"sample": {"top_hits": {"size": 1, "_source": ["title"]}}},
                }
            },
        }
        response = requests.post(f"{self.base_url}/{self.index}/_search", json=body, timeout=10)
        response.raise_for_status()
        titles: dict[str, str] = {}
        for bucket in response.json()["aggregations"]["docs"]["buckets"]:
            hits = bucket["sample"]["hits"]["hits"]
            if hits:
                titles[bucket["key"]] = str(hits[0]["_source"].get("title") or "")
        self._law_titles_cache = titles
        return dict(titles)

    def explicit_article_ids(self, query: str) -> tuple[str, ...]:
        """検索語に明示された「法令名＋条番号」をArticle IDへ解決する。

        条番号は意味検索の順位に委ねない。法令名から次の法令名までに書かれた
        条番号だけをその法令へ対応付けるため、異なる法令の同じ条番号を混同しない。
        """
        return _explicit_article_ids(query, self.law_titles())

    def search_by_document_id(
        self,
        query: str,
        document_id: str,
        top_k: int,
        user_clearance_level: int,
    ) -> list[dict[str, Any]]:
        """明示された法令内をBM25検索し、巨大な法令群の中で候補が埋もれるのを防ぐ。

        この補助検索に限り、既定では本則へ絞る(附則は改正沿革が多く、法令名だけで
        引くと本則の候補を押し出すため)。附則が根拠になりうる語がある場合は絞らない。
        通常のlaw検索は附則も対象なので、ここで絞っても附則が引けなくなるわけではない。
        """
        section_filters = []
        if not any(cue in query for cue in SUPPLEMENTARY_PROVISION_CUES):
            section_filters.append({"term": {"sectionKey": "main"}})
        body = {
            "size": top_k,
            "query": {
                "bool": {
                    "filter": [
                        *self._filters("law", user_clearance_level),
                        {"term": {"documentId": document_id}},
                        *section_filters,
                    ],
                    "must": [
                        {
                            "multi_match": {
                                "query": query,
                                # 法令はdocumentIdで確定済みなので、法令名の反復より条見出しを優先する。
                                "fields": ["heading^8", "text^2", "sectionPath"],
                                "type": "best_fields",
                            }
                        }
                    ],
                    "should": _legal_reference_should_clauses(query),
                }
            },
        }
        response = requests.post(f"{self.base_url}/{self.index}/_search", json=body, timeout=15)
        response.raise_for_status()
        return [
            {
                "document": hit["_source"],
                "score": 1.0 / (settings.agent_rrf_k + rank),
                "bm25Score": float(hit["_score"] or 0.0),
                "vectorScore": 0.0,
            }
            for rank, hit in enumerate(response.json()["hits"]["hits"], start=1)
        ]

    def get_by_article_ids(
        self,
        article_content_unit_ids: list[str],
        user_clearance_level: int,
        max_chunks: int = 30,
    ) -> list[dict[str, Any]]:
        """条ID(articleContentUnitId)で条全体のチャンク群を取得する。
        項・号分割された条は article ID そのものが contentUnitId に存在しないため、
        articleContentUnitId フィールド経由で子チャンクも拾う。"""
        if not article_content_unit_ids:
            return []
        body = {
            "size": max_chunks,
            "query": {
                "bool": {
                    "filter": self._filters(None, user_clearance_level),
                    "should": [
                        {"terms": {"contentUnitId": article_content_unit_ids}},
                        {"terms": {"articleContentUnitId": article_content_unit_ids}},
                    ],
                    "minimum_should_match": 1,
                }
            },
        }
        response = requests.post(f"{self.base_url}/{self.index}/_search", json=body, timeout=10)
        response.raise_for_status()
        return [hit["_source"] for hit in response.json()["hits"]["hits"]]

    def get_article_navigation_contexts(
        self,
        article_content_unit_ids: list[str],
        user_clearance_level: int,
        *,
        max_chunks_per_article: int = 3,
        timeout_sec: float | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Article候補の先頭側chunkを、候補ごとに同じ上限で一括取得する。"""
        article_ids = list(dict.fromkeys(article_content_unit_ids))
        if not article_ids:
            return {}

        lines: list[str] = []
        for article_id in article_ids:
            lines.append(json.dumps({"index": self.index}))
            lines.append(
                json.dumps(
                    {
                        "size": max(1, max_chunks_per_article),
                        "sort": [
                            {
                                "paragraphNumber": {
                                    "order": "asc",
                                    "missing": "_first",
                                }
                            },
                            {"itemNumber": {"order": "asc", "missing": "_first"}},
                            {"contentUnitId": {"order": "asc"}},
                        ],
                        "query": {
                            "bool": {
                                "filter": [
                                    *self._filters("law", user_clearance_level),
                                    {
                                        "bool": {
                                            "should": [
                                                {"term": {"contentUnitId": article_id}},
                                                {
                                                    "term": {
                                                        "articleContentUnitId": article_id
                                                    }
                                                },
                                            ],
                                            "minimum_should_match": 1,
                                        }
                                    },
                                ]
                            }
                        },
                    },
                    ensure_ascii=False,
                )
            )

        response = requests.post(
            f"{self.base_url}/{self.index}/_msearch",
            data=("\n".join(lines) + "\n").encode("utf-8"),
            headers={"Content-Type": "application/x-ndjson"},
            timeout=timeout_sec or 10,
        )
        response.raise_for_status()
        responses = response.json().get("responses", [])
        return {
            article_id: [
                hit["_source"]
                for hit in ((single.get("hits") or {}).get("hits") or [])
            ]
            for article_id, single in zip(article_ids, responses, strict=False)
        }

    def get_complete_articles_by_ids(
        self,
        article_content_unit_ids: list[str],
        user_clearance_level: int,
        *,
        request_batch_size: int = 100,
        page_size: int = 1000,
    ) -> list[dict[str, Any]]:
        """オフライン分類用に、指定Articleの全チャンクを欠落なく取得する。

        通常検索の件数上限を流用すると長いArticleが黙って欠けるため、ID群を分割して
        total件数までページングする。OpenSearchの既定result windowを超える入力は
        不完全な本文で分類せず明示的に失敗させる。
        """
        ids = list(dict.fromkeys(article_content_unit_ids))
        if not ids:
            return []
        output: list[dict[str, Any]] = []
        for offset in range(0, len(ids), request_batch_size):
            batch = ids[offset : offset + request_batch_size]
            start = 0
            total: int | None = None
            while total is None or start < total:
                if start + page_size > 10000:
                    raise RuntimeError(
                        "offline relation classification article batch exceeds "
                        "OpenSearch max_result_window"
                    )
                body = {
                    "from": start,
                    "size": page_size,
                    "track_total_hits": True,
                    "sort": [{"contentUnitId": "asc"}],
                    "query": {
                        "bool": {
                            "filter": self._filters(None, user_clearance_level),
                            "should": [
                                {"terms": {"contentUnitId": batch}},
                                {"terms": {"articleContentUnitId": batch}},
                            ],
                            "minimum_should_match": 1,
                        }
                    },
                }
                response = requests.post(
                    f"{self.base_url}/{self.index}/_search",
                    json=body,
                    timeout=30,
                )
                response.raise_for_status()
                hits_block = response.json().get("hits") or {}
                total_value = hits_block.get("total", 0)
                total = int(
                    total_value.get("value", 0)
                    if isinstance(total_value, dict)
                    else total_value
                )
                hits = list(hits_block.get("hits") or [])
                output.extend(
                    hit["_source"] for hit in hits if isinstance(hit.get("_source"), dict)
                )
                if not hits:
                    break
                start += len(hits)
            if total is not None and start < total:
                raise RuntimeError(
                    "OpenSearch returned an incomplete Article set for relation classification"
                )
        return output

    def get_by_content_unit_ids(
        self,
        content_unit_ids: list[str],
        user_clearance_level: int,
    ) -> list[dict[str, Any]]:
        if not content_unit_ids:
            return []
        body = {
            "size": min(max(len(content_unit_ids) * 5, len(content_unit_ids)), 100),
            "query": {
                "bool": {
                    "filter": [
                        *self._filters(None, user_clearance_level),
                    ],
                    "must": [
                        {
                            "bool": {
                                "should": [
                                    {"terms": {"contentUnitId": content_unit_ids}},
                                    {"terms": {"parentContentUnitId": content_unit_ids}},
                                    {"terms": {"articleContentUnitId": content_unit_ids}},
                                ],
                                "minimum_should_match": 1,
                            }
                        }
                    ],
                }
            },
        }
        response = requests.post(f"{self.base_url}/{self.index}/_search", json=body, timeout=10)
        response.raise_for_status()
        return [hit["_source"] for hit in response.json()["hits"]["hits"]]

    def search(
        self,
        query: str,
        doc_type: str | None,
        top_k: int,
        user_clearance_level: int,
        use_bm25: bool = True,
        use_vector: bool = True,
    ) -> list[dict[str, Any]]:
        scored: dict[str, dict[str, Any]] = {}

        bm25_hits = self._bm25_search(query, doc_type, top_k, user_clearance_level) if use_bm25 else []
        vector_hits = self._vector_search(query, doc_type, top_k, user_clearance_level) if use_vector else []
        self._merge_rrf_hits(scored, bm25_hits, "bm25Score", 0.4)
        self._merge_rrf_hits(scored, vector_hits, "vectorScore", 0.6)

        results = list(scored.values())
        results.sort(key=lambda item: item["score"], reverse=True)
        return results[:top_k]

    def search_requirement_specs(
        self,
        specs: list["RequirementSearchSpec"],
        *,
        user_clearance_level: int,
        timeout_sec: float | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """複数Requirementの法令内検索を1回のmulti-searchへまとめる(計画書 §11.7)。

        論理クエリ数と外部API呼び出し数を分離し、Requirementごとの逐次HTTP呼び出しへ
        戻さないための入口。戻り値は requirementId -> Article単位の候補リスト。
        """
        if not specs:
            return {}
        embeddings_by_query: dict[str, list[float]] = {}
        if settings.agent_use_vector:
            unique_queries = list(dict.fromkeys(spec.query for spec in specs if spec.query))
            try:
                vectors = embed_texts(
                    unique_queries,
                    settings.embedding_dimension,
                    timeout_sec=timeout_sec,
                )
                embeddings_by_query = dict(zip(unique_queries, vectors, strict=True))
            except Exception:
                # 埋め込み障害時もBM25・直接取得を継続する。
                embeddings_by_query = {}

        lines: list[str] = []
        subqueries: list[tuple[RequirementSearchSpec, str]] = []
        for spec in specs:
            for article_id in spec.article_ids:
                lines.append(json.dumps({"index": self.index}))
                lines.append(
                    json.dumps(
                        self._direct_article_query(
                            spec,
                            article_id,
                            user_clearance_level,
                        )
                    )
                )
                subqueries.append((spec, "direct"))
            if settings.agent_use_bm25 or not embeddings_by_query:
                lines.append(json.dumps({"index": self.index}))
                lines.append(json.dumps(self._requirement_query(spec, user_clearance_level)))
                subqueries.append((spec, "bm25"))
            vector = embeddings_by_query.get(spec.query)
            if settings.agent_use_vector and vector:
                lines.append(json.dumps({"index": self.index}))
                lines.append(
                    json.dumps(
                        self._requirement_vector_query(
                            spec,
                            vector,
                            user_clearance_level,
                        )
                    )
                )
                subqueries.append((spec, "vector"))
        payload = "\n".join(lines) + "\n"
        response = requests.post(
            f"{self.base_url}/{self.index}/_msearch",
            data=payload.encode("utf-8"),
            headers={"Content-Type": "application/x-ndjson"},
            timeout=timeout_sec or 15,
        )
        response.raise_for_status()
        responses = response.json().get("responses", [])
        hits_by_requirement: dict[str, dict[str, dict[str, Any]]] = {
            spec.requirement_id: {} for spec in specs
        }
        for (spec, source_kind), single in zip(subqueries, responses, strict=False):
            hits = (single.get("hits") or {}).get("hits") or []
            _merge_requirement_hits(
                hits_by_requirement[spec.requirement_id],
                hits,
                source_kind,
            )
        results: dict[str, list[dict[str, Any]]] = {}
        for spec in specs:
            hits = sorted(
                hits_by_requirement[spec.requirement_id].values(),
                key=lambda hit: -float(hit.get("_score") or 0.0),
            )
            results[spec.requirement_id] = (
                _articles_from_hits(hits, spec)
                if spec.doc_type == "law"
                else _chunks_from_hits(hits, spec)
            )
        return results

    def _direct_article_query(
        self,
        spec: "RequirementSearchSpec",
        article_id: str,
        user_clearance_level: int,
    ) -> dict[str, Any]:
        filters = self._requirement_filters(spec, user_clearance_level)
        return {
            "size": max(1, settings.layered_max_chunks_per_article),
            "query": {
                "bool": {
                    "filter": [
                        *filters,
                        {
                            "bool": {
                                "should": [
                                    {"term": {"contentUnitId": article_id}},
                                    {"term": {"articleContentUnitId": article_id}},
                                ],
                                "minimum_should_match": 1,
                            }
                        },
                    ]
                }
            },
        }

    def _requirement_query(
        self,
        spec: "RequirementSearchSpec",
        user_clearance_level: int,
    ) -> dict[str, Any]:
        filters = self._requirement_filters(spec, user_clearance_level)
        # 出口はArticle単位のtop_k。項・号chunkが上位を占めても必要なArticleを
        # 集約前に落とさないよう、chunk取得段階だけを過取得する。
        chunk_top_k = min(100, max(spec.top_k, spec.top_k * 3))
        return {
            "size": chunk_top_k,
            "query": {
                "bool": {
                    "filter": filters,
                    "must": [
                        {
                            "multi_match": {
                                "query": spec.query,
                                "fields": ["heading^8", "text^2", "title", "sectionPath"],
                                "type": "best_fields",
                            }
                        }
                    ],
                    "should": _legal_reference_should_clauses(spec.query),
                }
            },
        }

    def _requirement_vector_query(
        self,
        spec: "RequirementSearchSpec",
        vector: list[float],
        user_clearance_level: int,
    ) -> dict[str, Any]:
        filters = self._requirement_filters(spec, user_clearance_level)
        chunk_top_k = min(100, max(spec.top_k, spec.top_k * 3))
        return {
            "size": chunk_top_k,
            "query": {
                "knn": {
                    "embedding": {
                        "vector": vector,
                        "k": max(chunk_top_k, 1),
                        "filter": {"bool": {"filter": filters}},
                    }
                }
            },
        }

    def _requirement_filters(
        self,
        spec: "RequirementSearchSpec",
        user_clearance_level: int,
    ) -> list[dict[str, Any]]:
        filters: list[dict[str, Any]] = [
            *self._filters(spec.doc_type, user_clearance_level),
        ]
        if spec.document_ids:
            filters.append({"terms": {"documentId": list(spec.document_ids)}})
        elif spec.family_document_ids:
            filters.append({"terms": {"documentId": list(spec.family_document_ids)}})
        authority_types = search_authority_types(spec.authority_type)
        if authority_types and not spec.document_ids and spec.doc_type == "law":
            # 未判別(ordinance_unspecified / unknown)を構造的に落とさない(§5.2)。
            filters.append(
                {
                    "bool": {
                        "should": [
                            {"terms": {"authorityType": list(authority_types)}},
                            {"bool": {"must_not": {"exists": {"field": "authorityType"}}}},
                        ],
                        "minimum_should_match": 1,
                    }
                }
            )
        return filters

    def _filters(self, doc_type: str | None, user_clearance_level: int) -> list[dict[str, Any]]:
        filters: list[dict[str, Any]] = [
            {"term": {"publishStatus": "published"}},
            {"term": {"isLatest": True}},
            {"range": {"clearanceLevel": {"lte": user_clearance_level}}},
        ]
        if doc_type:
            filters.append({"term": {"docType": doc_type}})
        return filters

    def _bm25_search(
        self, query: str, doc_type: str | None, top_k: int, user_clearance_level: int
    ) -> list[dict[str, Any]]:
        body = {
            "size": top_k,
            "query": {
                "bool": {
                    "filter": self._filters(doc_type, user_clearance_level),
                    "must": [
                        {
                            "multi_match": {
                                "query": query,
                                "fields": ["text^3", "heading^2", "title", "sectionPath"],
                                "type": "best_fields",
                            }
                        }
                    ],
                    "should": _legal_reference_should_clauses(query),
                }
            },
        }
        response = requests.post(f"{self.base_url}/{self.index}/_search", json=body, timeout=15)
        response.raise_for_status()
        return [{"_source": hit["_source"], "_score": hit["_score"]} for hit in response.json()["hits"]["hits"]]

    def _vector_search(
        self, query: str, doc_type: str | None, top_k: int, user_clearance_level: int
    ) -> list[dict[str, Any]]:
        vector_k = max(settings.agent_candidate_top_k * 3, top_k * 3, 10)
        raw_hits = self._raw_vector_hits(query, vector_k)
        hits = []
        for hit in raw_hits:
            source = hit["_source"]
            if source.get("publishStatus") != "published" or not source.get("isLatest"):
                continue
            if int(source.get("clearanceLevel", 3)) > user_clearance_level:
                continue
            if doc_type and source.get("docType") != doc_type:
                continue
            hits.append({"_source": source, "_score": hit["_score"]})
        return hits[:top_k]

    @lru_cache(maxsize=256)
    def _raw_vector_hits(
        self,
        query: str,
        vector_k: int,
    ) -> tuple[dict[str, Any], ...]:
        """同一語の法令・ガイドライン検索で、同じKNN検索を重複実行しない。"""
        body = {
            "size": vector_k,
            "query": {
                "knn": {
                    "embedding": {
                        "vector": self._query_embedding(
                            query,
                            settings.embedding_dimension,
                        ),
                        "k": vector_k,
                    }
                }
            },
        }
        response = requests.post(f"{self.base_url}/{self.index}/_search", json=body, timeout=20)
        response.raise_for_status()
        return tuple(
            {
                "_source": hit["_source"],
                "_score": hit["_score"],
            }
            for hit in response.json()["hits"]["hits"]
        )

    def _merge_rrf_hits(
        self,
        scored: dict[str, dict[str, Any]],
        hits: list[dict[str, Any]],
        score_key: str,
        weight: float,
    ) -> None:
        for rank, hit in enumerate(hits, start=1):
            source = hit["_source"]
            content_unit_id = source["contentUnitId"]
            raw_score = float(hit["_score"] or 0.0)
            if content_unit_id not in scored:
                scored[content_unit_id] = {
                    "score": 0.0,
                    "document": source,
                    "bm25Score": 0.0,
                    "vectorScore": 0.0,
                }
            scored[content_unit_id][score_key] = raw_score
            scored[content_unit_id]["score"] += weight / (settings.agent_rrf_k + rank)


def _legal_reference_phrases(query: str) -> tuple[str, ...]:
    """検索語に明示された法令の条・項・号表現を順序を保って抽出する。"""
    return tuple(
        dict.fromkeys(
            re.sub(r"\s+", "", match.group(0))
            for match in LEGAL_REFERENCE_PHRASE_PATTERN.finditer(query or "")
        )
    )


def _legal_reference_should_clauses(query: str) -> list[dict[str, Any]]:
    """明示条番号の完全一致を加点する。候補の採否や必須条件にはしない。"""
    return [
        {
            "multi_match": {
                "query": phrase,
                "fields": ["heading^16", "text^12", "sectionPath^4"],
                "type": "phrase",
                "boost": 8,
            }
        }
        for phrase in _legal_reference_phrases(query)
    ]


def _explicit_article_ids(query: str, titles: dict[str, str]) -> tuple[str, ...]:
    normalized = (query or "").translate(_FULLWIDTH_DIGITS)
    mentions: list[tuple[int, int, str]] = []
    for document_id, title in titles.items():
        if not title:
            continue
        for name in law_title_names(title):
            start = 0
            while (index := normalized.find(name, start)) >= 0:
                mentions.append((index, index + len(name), document_id))
                start = index + 1

    # 正式名称が別の正式名称の部分文字列なら、同じ位置では長い名称だけを採用する。
    selected: list[tuple[int, int, str]] = []
    for mention in sorted(mentions, key=lambda item: (item[0], -(item[1] - item[0]))):
        start, end, _ = mention
        if any(
            start < other_end and other_start < end
            for other_start, other_end, _ in selected
        ):
            continue
        selected.append(mention)
    selected.sort(key=lambda item: item[0])

    article_ids: list[str] = []
    for index, (_, end, document_id) in enumerate(selected):
        segment_end = selected[index + 1][0] if index + 1 < len(selected) else len(normalized)
        for match in _ARTICLE_REFERENCE_PATTERN.finditer(normalized[end:segment_end]):
            parts = [match.group(1), *match.group(2).removeprefix("の").split("の")]
            numbers = [_japanese_number_to_int(part) for part in parts if part]
            if not numbers or any(number is None for number in numbers):
                continue
            suffix = "_".join(str(number) for number in numbers)
            article_id = f"{document_id}-article-{suffix}"
            if article_id not in article_ids:
                article_ids.append(article_id)
    return tuple(article_ids)


def _japanese_number_to_int(value: str) -> int | None:
    normalized = value.translate(_FULLWIDTH_DIGITS)
    if normalized.isdigit():
        return int(normalized)
    if not normalized or any(
        char not in _JAPANESE_DIGITS and char not in _JAPANESE_UNITS
        for char in normalized
    ):
        return None
    total = 0
    current = 0
    for char in normalized:
        if char in _JAPANESE_DIGITS:
            current = _JAPANESE_DIGITS[char]
        else:
            total += (current or 1) * _JAPANESE_UNITS[char]
            current = 0
    return total + current
