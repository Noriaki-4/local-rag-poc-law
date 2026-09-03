from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException

from .config import settings
from .domains.legal.question_readiness import (
    QuestionReadinessModelProtocolError,
    QuestionReadinessService,
)
from .domains.legal.model_routing import legal_model_for, legal_model_routing
from .framework_agent import LegalFrameworkAgentService
from .framework_audit import (
    AuditContextCapacityError,
    AuditModelProtocolError,
    AuditSnapshotInvalidError,
    AuditSnapshotNotFoundError,
    FrameworkPostRunAuditService,
    PostRunAuditDisabledError,
)
from .graph_client import GraphClient
from .legal_ontology import GRAPH_SCHEMA_VERSION
from .llm import LLMClient
from .models import (
    AnswerRequest,
    FrameworkAuditRequest,
    GraphPathRequest,
    QuestionReadinessRequest,
    SearchRequest,
)
from .opensearch_client import OpenSearchClient
from .retrieval_budget import current_profile
from .seed import seed_all

os_client = OpenSearchClient()
graph_client = GraphClient()
llm_client = LLMClient()
framework_agent_service = LegalFrameworkAgentService(
    os_client,
    graph_client,
    llm_client,
)
framework_audit_service = FrameworkPostRunAuditService(llm_client)
question_readiness_service = QuestionReadinessService(llm_client)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    graph_client.close()


app = FastAPI(title="Local Agentic RAG POC", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, Any]:
    profile = current_profile()
    opensearch_health = os_client.health()
    neo4j_health = graph_client.health()
    llm_health = llm_client.health()
    components_ok = all(
        _health_component_ok(component)
        for component in (
            opensearch_health,
            neo4j_health,
            llm_health,
        )
    )
    return {
        "status": "ok" if components_ok else "degraded",
        "opensearch": opensearch_health,
        "neo4j": neo4j_health,
        "llm": llm_health,
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
        "agentFramework": {
            "available": True,
            "algorithm": "shared_boundary_iterative_v1",
            "active": True,
            "reviewerEnabled": settings.agent_framework_reviewer_enabled,
            "diagnosticsMode": settings.agent_framework_diagnostics_mode,
            "postRunAudit": settings.agent_framework_post_run_audit,
            "modelTiers": settings.agent_framework_model_tiers,
            "modelRouting": legal_model_routing(),
            "researchModel": legal_model_for("question_decomposition"),
            "integrationModel": legal_model_for("integration"),
            # 旧クライアントとの互換性のため当面残す。
            "finalizeModel": legal_model_for("finalization"),
            "reviewerModel": legal_model_for("reviewer"),
            "maxResearchCycles": settings.agent_framework_max_research_cycles,
            "maxWallTimeSec": settings.agent_framework_max_wall_time_sec,
        },
    }


@app.post("/admin/seed")
def admin_seed() -> dict[str, Any]:
    try:
        result = seed_all(os_client, graph_client)
        return {"status": "seeded", **result}
    except Exception as exc:
        raise _internal_http_error(
            "seed_failed", "データ投入処理に失敗しました。", exc
        ) from exc


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
        raise _internal_http_error(
            "search_failed", "検索処理に失敗しました。", exc
        ) from exc


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
        raise _internal_http_error(
            "graph_path_failed", "グラフ探索処理に失敗しました。", exc
        ) from exc


@app.post("/answer")
def answer(request: AnswerRequest) -> dict[str, Any]:
    try:
        return framework_agent_service.answer(request).model_dump()
    except Exception as exc:
        raise _internal_http_error(
            "answer_failed", "回答処理に失敗しました。", exc
        ) from exc


@app.post("/question/readiness")
def question_readiness(request: QuestionReadinessRequest) -> dict[str, Any]:
    """検索を開始せず、質問が原文のまま調査可能かだけを確認する。"""

    try:
        return question_readiness_service.check(request).model_dump(mode="json")
    except QuestionReadinessModelProtocolError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "question_readiness_model_protocol_error",
                "message": "質問確認モデルの応答を検証できませんでした。",
            },
        ) from exc
    except Exception as exc:
        raise _internal_http_error(
            "question_readiness_failed",
            "質問確認処理に失敗しました。",
            exc,
        ) from exc


@app.post("/answer/framework")
def framework_answer(request: AnswerRequest) -> dict[str, Any]:
    """Feature Flag切替前に新Frameworkを明示検証する。"""
    try:
        return framework_agent_service.answer(request).model_dump()
    except Exception as exc:
        raise _internal_http_error(
            "framework_answer_failed",
            "新Agent Frameworkの回答処理に失敗しました。",
            exc,
        ) from exc


@app.post("/answer/framework/audit")
def framework_audit(request: FrameworkAuditRequest) -> dict[str, Any]:
    """保存済みSnapshotを使い、指定した適用済み判断を読み取り専用で説明する。"""

    try:
        return framework_audit_service.audit(request).model_dump()
    except PostRunAuditDisabledError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "framework_post_run_audit_disabled",
                "message": "事後監査は無効です。",
            },
        ) from exc
    except AuditSnapshotNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "framework_audit_snapshot_not_found",
                "message": "指定された診断Snapshotが見つかりません。",
            },
        ) from exc
    except (AuditSnapshotInvalidError, AuditContextCapacityError) as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "framework_audit_snapshot_invalid",
                "message": "診断Snapshotを事後監査に使用できません。",
            },
        ) from exc
    except AuditModelProtocolError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "framework_audit_model_protocol_error",
                "message": "事後監査モデルの応答を検証できませんでした。",
            },
        ) from exc
    except Exception as exc:
        raise _internal_http_error(
            "framework_audit_failed",
            "事後監査処理に失敗しました。",
            exc,
        ) from exc


def _health_component_ok(component: Any) -> bool:
    if isinstance(component, bool):
        return component
    if isinstance(component, dict):
        return bool(component.get("ok", False))
    return False


def _internal_http_error(
    code: str,
    message: str,
    cause: Exception,
) -> HTTPException:
    del cause
    return HTTPException(
        status_code=500,
        detail={"code": code, "message": message},
    )
