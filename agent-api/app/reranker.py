from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

import requests

from .config import settings


@dataclass
class RerankResult:
    items: list[dict[str, Any]]
    used: bool
    provider: str
    model: str | None = None
    latency_ms: int | None = None
    error: str | None = None
    scores: dict[str, float] = field(default_factory=dict)


class RerankerClient:
    def status(self) -> dict[str, Any]:
        if settings.rerank_provider == "none":
            return {"enabled": False, "provider": "none", "ok": True}
        try:
            response = requests.get(
                f"{settings.rerank_base_url.rstrip('/')}/health",
                timeout=min(settings.rerank_timeout_sec, 5),
            )
            response.raise_for_status()
            payload = response.json()
            return {
                "enabled": True,
                "provider": settings.rerank_provider,
                "ok": payload.get("status") == "ok",
                "model": payload.get("model") or settings.rerank_model,
            }
        except Exception as exc:
            return {
                "enabled": True,
                "provider": settings.rerank_provider,
                "ok": False,
                "model": settings.rerank_model,
                "error": str(exc),
            }

    def rerank(
        self,
        query: str,
        items: list[dict[str, Any]],
        timeout_sec: int | None = None,
    ) -> RerankResult:
        if settings.rerank_provider == "none" or len(items) < 2:
            return RerankResult(items=items, used=False, provider=settings.rerank_provider)
        if settings.rerank_provider != "local_http":
            return RerankResult(
                items=items,
                used=False,
                provider=settings.rerank_provider,
                error=f"unsupported rerank provider: {settings.rerank_provider}",
            )

        documents = [_document_text(item["document"]) for item in items]
        started = perf_counter()
        try:
            response = requests.post(
                f"{settings.rerank_base_url.rstrip('/')}/rerank",
                json={"query": query, "documents": documents, "top_n": len(documents)},
                timeout=timeout_sec or settings.rerank_timeout_sec,
            )
            response.raise_for_status()
            payload = response.json()
            ranked_items: list[dict[str, Any]] = []
            seen: set[int] = set()
            scores: dict[str, float] = {}
            for result in payload.get("results", []):
                index = result.get("index")
                if not isinstance(index, int) or index < 0 or index >= len(items) or index in seen:
                    continue
                score = float(result.get("relevance_score", 0.0))
                item = dict(items[index])
                item["rerankScore"] = score
                ranked_items.append(item)
                seen.add(index)
                scores[item["document"]["contentUnitId"]] = score
            if not ranked_items:
                raise ValueError("reranker returned no valid results")
            ranked_items.extend(item for index, item in enumerate(items) if index not in seen)
            return RerankResult(
                items=ranked_items,
                used=True,
                provider=settings.rerank_provider,
                model=payload.get("model") or settings.rerank_model,
                latency_ms=int((perf_counter() - started) * 1000),
                scores=scores,
            )
        except Exception as exc:
            return RerankResult(
                items=items,
                used=False,
                provider=settings.rerank_provider,
                model=settings.rerank_model,
                latency_ms=int((perf_counter() - started) * 1000),
                error=str(exc),
            )

    def rerank_batch(
        self,
        requests_batch: list[tuple[str, list[dict[str, Any]]]],
        timeout_sec: int | None = None,
    ) -> list[RerankResult]:
        """複数queryの候補を1回のHTTP・CrossEncoder推論へまとめる。"""
        if not requests_batch:
            return []
        if settings.rerank_provider == "none":
            return [
                RerankResult(items=items, used=False, provider=settings.rerank_provider)
                for _, items in requests_batch
            ]
        if settings.rerank_provider != "local_http":
            return [
                RerankResult(
                    items=items,
                    used=False,
                    provider=settings.rerank_provider,
                    error=f"unsupported rerank provider: {settings.rerank_provider}",
                )
                for _, items in requests_batch
            ]

        documents_by_request = [
            [_document_text(item["document"]) for item in items]
            for _, items in requests_batch
        ]
        started = perf_counter()
        try:
            response = requests.post(
                f"{settings.rerank_base_url.rstrip('/')}/rerank/batch",
                json={
                    "items": [
                        {"query": query, "documents": documents}
                        for (query, _), documents in zip(
                            requests_batch,
                            documents_by_request,
                            strict=True,
                        )
                    ]
                },
                timeout=timeout_sec or settings.rerank_timeout_sec,
            )
            response.raise_for_status()
            payload = response.json()
            latency_ms = int((perf_counter() - started) * 1000)
            raw_items = payload.get("items") or []
            if len(raw_items) != len(requests_batch):
                raise ValueError("reranker batch response count mismatch")
            return [
                _rerank_result_from_rows(
                    items,
                    raw.get("results") or [],
                    provider=settings.rerank_provider,
                    model=payload.get("model") or settings.rerank_model,
                    latency_ms=latency_ms,
                )
                for (_, items), raw in zip(requests_batch, raw_items, strict=True)
            ]
        except Exception as exc:
            latency_ms = int((perf_counter() - started) * 1000)
            return [
                RerankResult(
                    items=items,
                    used=False,
                    provider=settings.rerank_provider,
                    model=settings.rerank_model,
                    latency_ms=latency_ms,
                    error=str(exc),
                )
                for _, items in requests_batch
            ]


def _document_text(document: dict[str, Any]) -> str:
    fields = [
        f"資料名: {document.get('title') or ''}",
        f"見出し: {document.get('heading') or ''}",
        f"本文: {document.get('text') or ''}",
    ]
    return "\n".join(fields)[: settings.rerank_max_chars]


def _rerank_result_from_rows(
    items: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    provider: str,
    model: str,
    latency_ms: int,
) -> RerankResult:
    ranked_items: list[dict[str, Any]] = []
    seen: set[int] = set()
    scores: dict[str, float] = {}
    for row in rows:
        index = row.get("index")
        if not isinstance(index, int) or index < 0 or index >= len(items) or index in seen:
            continue
        score = float(row.get("relevance_score", 0.0))
        item = dict(items[index])
        item["rerankScore"] = score
        ranked_items.append(item)
        seen.add(index)
        scores[str(item["document"].get("contentUnitId") or "")] = score
    if not ranked_items:
        return RerankResult(
            items=items,
            used=False,
            provider=provider,
            model=model,
            latency_ms=latency_ms,
            error="reranker returned no valid results",
        )
    ranked_items.extend(item for index, item in enumerate(items) if index not in seen)
    return RerankResult(
        items=ranked_items,
        used=True,
        provider=provider,
        model=model,
        latency_ms=latency_ms,
        scores=scores,
    )
