from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException

from .agent import AgentService
from .config import settings
from .graph_client import GraphClient
from .legal_ontology import GRAPH_SCHEMA_VERSION
from .llm import LLMClient
from .models import AnswerRequest, GraphPathRequest, SearchRequest
from .opensearch_client import OpenSearchClient
from .reranker import RerankerClient
from .retrieval_budget import current_profile
from .seed import seed_all

os_client = OpenSearchClient()
graph_client = GraphClient()
llm_client = LLMClient()
reranker_client = RerankerClient()
agent_service = AgentService(os_client, graph_client, llm_client, reranker_client)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    graph_client.close()


app = FastAPI(title="Local Agentic RAG POC", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, Any]:
    profile = current_profile()
    return {
        "status": "ok",
        "opensearch": os_client.health(),
        "neo4j": graph_client.health(),
        "llm": llm_client.health(),
        "reranker": reranker_client.status(),
        # クライアント(eval-runner等)が自身のrequest timeoutと突き合わせるために公開する
        # (layered_legal_evidence_retrieval_plan.md §11.2)。
        "timeBudget": {
            "profileName": profile.name,
            "agentMaxWallTimeSec": profile.wall_time_sec,
            "minimumAnswerReserveSec": profile.minimum_answer_reserve_sec,
            "llmTimeoutSec": profile.llm_timeout_sec,
            "fullAnswerSafeReserveSec": profile.full_answer_safe_reserve_sec,
            "fullAnswerSafeExplorationBudgetSec": profile.full_answer_safe_exploration_budget_sec,
            "warnings": list(profile.warnings()),
        },
        "layeredLegalRetrieval": {
            "active": settings.agent_layered_legal_retrieval,
            "shadow": settings.agent_layered_legal_retrieval_shadow,
            "graphSchemaVersion": GRAPH_SCHEMA_VERSION,
        },
    }


@app.post("/admin/seed")
def admin_seed() -> dict[str, Any]:
    try:
        result = seed_all(os_client, graph_client)
        return {"status": "seeded", **result}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/search")
def search(request: SearchRequest) -> dict[str, Any]:
    try:
        results = os_client.search(
            request.query,
            request.docType,
            request.topK,
            request.userClearanceLevel,
            request.useBm25,
            request.useVector,
        )
        return {"results": results}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/graph/path")
def graph_path(request: GraphPathRequest) -> dict[str, Any]:
    try:
        return {
            "paths": graph_client.paths_from(
                request.fromGraphNodeId,
                request.edgeType,
                request.maxDepth,
                request.userClearanceLevel,
            )
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/answer")
def answer(request: AnswerRequest) -> dict[str, Any]:
    try:
        return agent_service.answer(request).model_dump()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
