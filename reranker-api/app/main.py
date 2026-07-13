import os
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sentence_transformers import CrossEncoder


MODEL_ID = os.getenv("RERANK_MODEL", "hotchpotch/japanese-reranker-base-v2")
MAX_LENGTH = max(128, min(int(os.getenv("RERANK_MAX_LENGTH", "512")), 8192))
BATCH_SIZE = max(1, min(int(os.getenv("RERANK_BATCH_SIZE", "8")), 64))

model: CrossEncoder | None = None


class RerankRequest(BaseModel):
    query: str = Field(min_length=1)
    documents: list[str] = Field(min_length=1, max_length=100)
    top_n: int | None = Field(default=None, ge=1, le=100)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global model
    model = CrossEncoder(MODEL_ID, max_length=MAX_LENGTH, device="cpu")
    yield
    model = None


app = FastAPI(title="Local Japanese Reranker", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok" if model is not None else "loading",
        "model": MODEL_ID,
        "device": "cpu",
        "maxLength": MAX_LENGTH,
    }


@app.post("/rerank")
def rerank(request: RerankRequest) -> dict[str, Any]:
    if model is None:
        raise HTTPException(status_code=503, detail="reranker model is still loading")
    started = perf_counter()
    scores = model.predict(
        [(request.query, document) for document in request.documents],
        batch_size=BATCH_SIZE,
        show_progress_bar=False,
    )
    ranked = sorted(
        (
            {"index": index, "relevance_score": float(score)}
            for index, score in enumerate(scores)
        ),
        key=lambda item: item["relevance_score"],
        reverse=True,
    )
    top_n = min(request.top_n or len(ranked), len(ranked))
    return {
        "model": MODEL_ID,
        "results": ranked[:top_n],
        "latencyMs": int((perf_counter() - started) * 1000),
    }
