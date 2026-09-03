"""GenUから既存Legal Agentを呼び出すBedrock AgentCore Runtime。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from contract import (
    QUESTION_READINESS_OPERATION,
    AgentCoreContractError,
    encode_stream_event,
    extract_operation,
    extract_question,
    render_answer,
    unwrap_payload,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="Legal RAG AgentCore Runtime", version="0.1.0")


@app.get("/ping")
async def ping() -> dict[str, str]:
    return {"status": "Healthy"}


@app.post("/invocations")
async def invocations(request: Request):
    try:
        payload = unwrap_payload(await request.json())
        operation = extract_operation(payload)
        question = extract_question(payload)
    except (AgentCoreContractError, ValueError) as exc:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "invalid_request", "message": str(exc)}},
        )

    runtime_session_id = request.headers.get(
        "x-amzn-bedrock-agentcore-runtime-session-id"
    )
    requested_model = payload.get("model")
    logger.info(
        "Starting legal AgentCore invocation operation=%s session=%s requested_model=%s",
        operation,
        runtime_session_id,
        requested_model,
    )

    if operation == QUESTION_READINESS_OPERATION:
        stream = _stream_question_readiness(question, runtime_session_id)
    else:
        stream = _stream_legal_answer(question, runtime_session_id)

    return StreamingResponse(
        stream,
        media_type="text/event-stream",
    )


async def _stream_legal_answer(question: str, runtime_session_id: str | None):
    yield encode_stream_event({"messageStart": {"role": "assistant"}})
    yield encode_stream_event(
        {
            "contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {"reasoningContent": {"text": "法令を調査しています。"}},
            }
        }
    )
    try:
        response = await asyncio.to_thread(_invoke_legal_agent, question)
        framework_trace = response.get("frameworkTrace")
        if (
            isinstance(framework_trace, Mapping)
            and framework_trace.get("runStatus") != "completed"
        ):
            logger.warning(
                "Legal Agent did not complete session=%s case=%s stop_reason=%s "
                "failure_code=%s",
                runtime_session_id,
                framework_trace.get("caseId"),
                framework_trace.get("stopReason"),
                framework_trace.get("failureCode"),
            )
        answer = render_answer(response)
    except Exception:
        logger.exception("Legal Agent invocation failed session=%s", runtime_session_id)
        yield encode_stream_event(
            {
                "internalServerException": {
                    "message": "法令検索バックエンドで回答を生成できませんでした。"
                }
            }
        )
        yield encode_stream_event({"messageStop": {"stopReason": "end_turn"}})
        return

    yield encode_stream_event(
        {
            "contentBlockStart": {
                "contentBlockIndex": 1,
                "start": {"text": ""},
            }
        }
    )
    yield encode_stream_event(
        {
            "contentBlockDelta": {
                "contentBlockIndex": 1,
                "delta": {"text": answer},
            }
        }
    )
    yield encode_stream_event({"contentBlockStop": {"contentBlockIndex": 1}})
    yield encode_stream_event({"messageStop": {"stopReason": "end_turn"}})


def _invoke_legal_agent(question: str) -> Mapping[str, Any]:
    """Importを遅延し、health checkをdata service初期化から分離する。"""

    from aws_adapters import install

    install()
    from app.main import framework_agent_service
    from app.models import AnswerRequest

    response = framework_agent_service.answer(AnswerRequest(question=question))
    return response.model_dump(mode="json")


async def _stream_question_readiness(
    question: str, runtime_session_id: str | None
):
    yield encode_stream_event({"messageStart": {"role": "assistant"}})
    try:
        response = await asyncio.to_thread(_invoke_question_readiness, question)
        rendered = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        logger.exception(
            "Question readiness invocation failed session=%s", runtime_session_id
        )
        yield encode_stream_event(
            {
                "internalServerException": {
                    "message": "質問の整理を実行できませんでした。"
                }
            }
        )
        yield encode_stream_event({"messageStop": {"stopReason": "end_turn"}})
        return

    yield encode_stream_event(
        {
            "contentBlockStart": {
                "contentBlockIndex": 0,
                "start": {"text": ""},
            }
        }
    )
    yield encode_stream_event(
        {
            "contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {"text": rendered},
            }
        }
    )
    yield encode_stream_event({"contentBlockStop": {"contentBlockIndex": 0}})
    yield encode_stream_event({"messageStop": {"stopReason": "end_turn"}})


def _invoke_question_readiness(question: str) -> Mapping[str, Any]:
    """既存の質問確認Domain ServiceをAgentCore境界から呼び出す。"""

    from aws_adapters import install

    install()
    from app.main import question_readiness_service
    from app.models import QuestionReadinessRequest

    response = question_readiness_service.check(
        QuestionReadinessRequest(question=question)
    )
    return response.model_dump(mode="json")
