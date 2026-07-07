from typing import Any

import requests

from .config import settings
from .embeddings import embed_text


class OpenSearchClient:
    def __init__(self) -> None:
        self.base_url = settings.opensearch_url.rstrip("/")
        self.index = settings.opensearch_index

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

        if use_bm25:
            for hit in self._bm25_search(query, doc_type, top_k, user_clearance_level):
                self._merge_score(scored, hit, "bm25Score", 0.4)

        if use_vector:
            for hit in self._vector_search(query, doc_type, top_k, user_clearance_level):
                self._merge_score(scored, hit, "vectorScore", 0.6)

        results = list(scored.values())
        results.sort(key=lambda item: item["score"], reverse=True)
        return results[:top_k]

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
                }
            },
        }
        response = requests.post(f"{self.base_url}/{self.index}/_search", json=body, timeout=15)
        response.raise_for_status()
        return [{"_source": hit["_source"], "_score": hit["_score"]} for hit in response.json()["hits"]["hits"]]

    def _vector_search(
        self, query: str, doc_type: str | None, top_k: int, user_clearance_level: int
    ) -> list[dict[str, Any]]:
        body = {
            "size": max(top_k * 3, 10),
            "query": {"knn": {"embedding": {"vector": embed_text(query, settings.embedding_dimension), "k": max(top_k * 3, 10)}}},
        }
        response = requests.post(f"{self.base_url}/{self.index}/_search", json=body, timeout=20)
        response.raise_for_status()
        hits = []
        for hit in response.json()["hits"]["hits"]:
            source = hit["_source"]
            if source.get("publishStatus") != "published" or not source.get("isLatest"):
                continue
            if int(source.get("clearanceLevel", 3)) > user_clearance_level:
                continue
            if doc_type and source.get("docType") != doc_type:
                continue
            hits.append({"_source": source, "_score": hit["_score"]})
        return hits[:top_k]

    def _merge_score(self, scored: dict[str, dict[str, Any]], hit: dict[str, Any], score_key: str, weight: float) -> None:
        source = hit["_source"]
        content_unit_id = source["contentUnitId"]
        score = float(hit["_score"] or 0.0)
        if content_unit_id not in scored:
            scored[content_unit_id] = {"score": 0.0, "document": source, "bm25Score": 0.0, "vectorScore": 0.0}
        scored[content_unit_id][score_key] = score
        scored[content_unit_id]["score"] += weight * score
