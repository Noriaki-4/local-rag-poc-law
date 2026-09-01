"""AWS data servicesを現行Legal Agentのローカル境界へ接続するRuntime adapter。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from time import perf_counter
from typing import Any, Iterator

import requests as http_requests


def _region() -> str:
    value = os.environ.get("AWS_REGION", "").strip()
    if not value:
        raise ValueError("AWS_REGION is required for AgentCore AWS adapters")
    return value


def _bedrock_converse_with_schema_fallback(
    runtime: Any, request: dict[str, Any]
) -> dict[str, Any]:
    try:
        return runtime.converse(**request)
    except Exception as error:
        response = getattr(error, "response", {})
        details = response.get("Error", {}) if isinstance(response, dict) else {}
        message = str(details.get("Message") or error)
        if details.get("Code") != "ValidationException" or "compiled grammar is too large" not in message:
            raise
        fallback = dict(request)
        output_config = fallback.pop("outputConfig", {})
        schema_text = (
            output_config.get("textFormat", {})
            .get("structure", {})
            .get("jsonSchema", {})
            .get("schema")
        )
        if not isinstance(schema_text, str):
            raise
        # Non-strict tool use avoids the native compiled-grammar size limit.
        # The existing LLM client still validates tool input against the same
        # application schema before accepting the decision.
        fallback["toolConfig"] = {
            "tools": [
                {
                    "toolSpec": {
                        "name": "legal_agent_response",
                        "description": "Return the Legal Agent structured response",
                        "inputSchema": {"json": json.loads(schema_text)},
                    }
                }
            ],
            "toolChoice": {"tool": {"name": "legal_agent_response"}},
        }
        return runtime.converse(**fallback)


def _bedrock_response_json_text(response: dict[str, Any]) -> str:
    blocks = response.get("output", {}).get("message", {}).get("content", [])
    for block in blocks:
        tool_input = block.get("toolUse", {}).get("input")
        if isinstance(tool_input, dict):
            return json.dumps(tool_input, ensure_ascii=False)
    return "".join(str(block.get("text") or "") for block in blocks).strip()


def _neptune_rows(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [dict(value) for value in payload if isinstance(value, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("results", "data", "records"):
        values = payload.get(key)
        if isinstance(values, list):
            return [dict(value) for value in values if isinstance(value, dict)]
    return []


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.rows)

    def consume(self) -> None:
        return None

    def single(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None


def _rewrite_neptune_query(query: str) -> str:
    """Neo4j固有のlist predicateをNeptune openCypher互換表現へ置き換える。"""

    query = re.sub(
        r"all\(node IN nodes\(path\) WHERE "
        r"coalesce\(node\.clearanceLevel, 3\) <= \$userClearanceLevel\)",
        "size([node IN nodes(path) WHERE "
        "coalesce(node.clearanceLevel, 3) <= $userClearanceLevel]) = "
        "size(nodes(path))",
        query,
    )
    for variable in ("from", "to"):
        query = re.sub(
            rf"any\(articleId IN \$articleIds WHERE\s*"
            rf"{variable}\.graphNodeId = articleId\s*"
            rf"OR {variable}\.graphNodeId STARTS WITH articleId \+ '-'\s*\)",
            f"size([articleId IN $articleIds WHERE "
            f"{variable}.graphNodeId = articleId "
            f"OR {variable}.graphNodeId STARTS WITH articleId + '-']) > 0",
            query,
        )
    return query


class _NeptuneSession:
    def __init__(
        self, client: Any, graph_id: str, query_timeout_ms: int | None = None
    ) -> None:
        self.client = client
        self.graph_id = graph_id
        self.query_timeout_ms = query_timeout_ms

    def __enter__(self) -> "_NeptuneSession":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def run(self, query: str, **parameters: Any) -> _Result:
        request = {
            "graphIdentifier": self.graph_id,
            "queryString": _rewrite_neptune_query(query),
            "language": "OPEN_CYPHER",
            "parameters": parameters,
        }
        if self.query_timeout_ms is not None:
            request["queryTimeoutMilliseconds"] = self.query_timeout_ms
        response = self.client.execute_query(**request)
        raw = response["payload"].read()
        payload = json.loads(raw) if raw else None
        return _Result(_neptune_rows(payload))

    def begin_transaction(self, **kwargs: Any) -> "_NeptuneSession":
        # Neptune Analytics execute_queryはquery単位で完結する。既存のread transaction
        # contextをquery timeout付きsessionへ写像し、複数queryのatomicityは提供しない。
        timeout_sec = float(kwargs.get("timeout", 120.0))
        timeout_ms = max(1, int(timeout_sec * 1000))
        return _NeptuneSession(self.client, self.graph_id, timeout_ms)

    def execute_read(self, callback: Any, *args: Any, **kwargs: Any) -> Any:
        return callback(self, *args, **kwargs)

    def execute_write(self, callback: Any, *args: Any, **kwargs: Any) -> Any:
        return callback(self, *args, **kwargs)


class _NeptuneDriver:
    def __init__(self, client: Any, graph_id: str) -> None:
        self.client = client
        self.graph_id = graph_id

    def session(self, **_kwargs: Any) -> _NeptuneSession:
        return _NeptuneSession(self.client, self.graph_id)

    def close(self) -> None:
        return None


class _GraphDatabaseAdapter:
    @staticmethod
    def driver(_uri: str, **_kwargs: Any) -> _NeptuneDriver:
        import boto3
        from botocore.config import Config

        graph_id = os.environ.get("NEPTUNE_GRAPH_ID", "").strip()
        if not graph_id:
            raise ValueError("NEPTUNE_GRAPH_ID is required")
        client = boto3.client(
            "neptune-graph",
            region_name=_region(),
            config=Config(
                read_timeout=None,
                retries={"mode": "standard", "total_max_attempts": 1},
            ),
        )
        return _NeptuneDriver(client, graph_id)


class _SignedRequests:
    RequestException = http_requests.RequestException
    ConnectionError = http_requests.ConnectionError
    Timeout = http_requests.Timeout

    def __init__(self) -> None:
        import boto3

        self.session = boto3.Session(region_name=_region())

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        body = kwargs.pop("data", None)
        json_body = kwargs.pop("json", None)
        headers = dict(kwargs.pop("headers", {}) or {})
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        payload = body.encode("utf-8") if isinstance(body, str) else body
        headers.setdefault(
            "X-Amz-Content-SHA256",
            hashlib.sha256(payload or b"").hexdigest(),
        )
        credentials = self.session.get_credentials().get_frozen_credentials()
        request = AWSRequest(method=method, url=url, data=payload, headers=headers)
        SigV4Auth(credentials, "aoss", _region()).add_auth(request)
        return http_requests.request(
            method,
            url,
            data=payload,
            headers=dict(request.headers),
            **kwargs,
        )

    def get(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        if url.rstrip("/").endswith("/_msearch"):
            return self.request("GET", url, **kwargs)
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> Any:
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> Any:
        return self.request("DELETE", url, **kwargs)


def _bedrock_embeddings(texts: list[str], expected_dimension: int) -> list[list[float]]:
    import boto3

    if not texts:
        return []
    client = boto3.client("bedrock-runtime", region_name=_region())
    model_id = os.environ.get("EMBEDDING_MODEL", "amazon.titan-embed-text-v2:0")
    max_chars = max(1, int(os.environ.get("EMBEDDING_MAX_CHARS", "1000")))
    vectors: list[list[float]] = []
    for text in texts:
        normalized = " ".join(text.split()) or "empty"
        response = client.invoke_model(
            modelId=model_id,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(
                {
                    "inputText": normalized[:max_chars],
                    "dimensions": expected_dimension,
                    "normalize": True,
                }
            ),
        )
        value = json.loads(response["body"].read()).get("embedding")
        if not isinstance(value, list) or len(value) != expected_dimension:
            raise ValueError("Titan embedding dimension mismatch")
        vector = [float(item) for item in value]
        if not all(math.isfinite(item) for item in vector):
            raise ValueError("Titan embedding contains non-finite values")
        vectors.append(vector)
    return vectors


def _bedrock_embed_text(
    text: str, dimension: int | None = None, *, timeout_sec: float | None = None
) -> list[float]:
    del timeout_sec
    expected = dimension or int(os.environ.get("EMBEDDING_DIMENSION", "1024"))
    return _bedrock_embeddings([text], expected)[0]


def _bedrock_embed_texts(
    texts: list[str], dimension: int | None = None, *, timeout_sec: float | None = None
) -> list[list[float]]:
    del timeout_sec
    expected = dimension or int(os.environ.get("EMBEDDING_DIMENSION", "1024"))
    return _bedrock_embeddings(texts, expected)


def _bedrock_llm_class(llm_module: Any) -> type:
    class BedrockLLMClient(llm_module.LLMClient):
        def __init__(self, **kwargs: Any) -> None:
            import boto3

            super().__init__(provider="bedrock", **{k: v for k, v in kwargs.items() if k != "provider"})
            self._runtime = boto3.client("bedrock-runtime", region_name=_region())

        def health(self) -> dict[str, Any]:
            return {
                "provider": "bedrock",
                "ok": True,
                "model": os.environ.get("BEDROCK_MODEL_ID"),
            }

        def _json_transport(
            self,
            prompt: str,
            schema: dict[str, Any],
            model: str,
            max_tokens: int,
            timeout_sec: int,
            effort: str | None = None,
        ) -> tuple[str, int, int | None, int | None, str | None]:
            del timeout_sec, effort
            started = perf_counter()
            model_id = os.environ.get("BEDROCK_MODEL_ID", model)
            converse_request = {
                "modelId": model_id,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "text": prompt
                                + "\n\nReturn only the JSON object required by the response contract."
                            }
                        ],
                    }
                ],
                "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0},
                "outputConfig": {
                    "textFormat": {
                        "type": "json_schema",
                        "structure": {
                            "jsonSchema": {
                                "schema": json.dumps(
                                    llm_module._to_anthropic_schema(schema),
                                    ensure_ascii=False,
                                ),
                                "name": "legal_agent_response",
                                "description": "Legal Agent structured response",
                            }
                        },
                    }
                },
            }
            response = _bedrock_converse_with_schema_fallback(
                self._runtime, converse_request
            )
            raw_text = _bedrock_response_json_text(response)
            usage = response.get("usage", {})
            return (
                raw_text,
                int((perf_counter() - started) * 1000),
                usage.get("inputTokens"),
                usage.get("outputTokens"),
                response.get("stopReason"),
            )

        def _generate_once(
            self,
            request: Any,
            prompt: str,
            timeout_sec: int | None,
            citations: list[Any],
            max_tokens: int | None = None,
        ) -> Any:
            started = perf_counter()
            shown = llm_module._shown_citations_for_prompt(citations)
            raw_text, _, input_tokens, output_tokens, stop_reason = self._json_transport(
                prompt,
                llm_module._answer_json_schema(request, shown),
                os.environ["BEDROCK_MODEL_ID"],
                max_tokens or llm_module.settings.anthropic_max_tokens,
                timeout_sec or llm_module.settings.llm_timeout_sec,
            )
            answer, predicted, judgements, assessments, polarity, validation_error = (
                llm_module._parse_answer_payload(
                    raw_text,
                    request.choices,
                    llm_module._citation_ids(shown),
                    request.topK,
                )
            )
            status, citation_ids, missing, decisions = llm_module._final_decision_fields(raw_text)
            return llm_module.LLMResult(
                text=answer,
                provider="bedrock",
                model=os.environ["BEDROCK_MODEL_ID"],
                latencyMs=int((perf_counter() - started) * 1000),
                inputTokens=input_tokens,
                outputTokens=output_tokens,
                estimatedCost=0,
                answer=answer,
                predictedAnswer=predicted,
                choiceJudgements=judgements,
                validationError=validation_error,
                stopReason=stop_reason,
                contentBlockTypes=["text"] if raw_text else [],
                outputChars=len(raw_text),
                questionPolarity=polarity,
                choiceAssessments=assessments,
                answerStatus=status,
                answerCitationIds=citation_ids,
                missing=missing,
                answerIssueDecisions=decisions,
            )

    return BedrockLLMClient


def _opensearch_health(client: Any, signed_requests: _SignedRequests) -> bool:
    try:
        response = signed_requests.get(f"{client.base_url}/{client.index}", timeout=3)
        return bool(response.ok)
    except http_requests.RequestException:
        return False


_INSTALLED = False


def install() -> None:
    """app.main import前にAWS adapterを一度だけ差し込む。"""

    global _INSTALLED
    if _INSTALLED:
        return
    if os.environ.get("LLM_PROVIDER") != "bedrock":
        raise ValueError("AgentCore requires LLM_PROVIDER=bedrock")
    if os.environ.get("EMBEDDING_PROVIDER") != "bedrock":
        raise ValueError("AgentCore requires EMBEDDING_PROVIDER=bedrock")
    if os.environ.get("GRAPH_PROVIDER") != "neptune-analytics":
        raise ValueError("AgentCore requires GRAPH_PROVIDER=neptune-analytics")

    from app import embeddings, graph_client, llm, opensearch_client

    embeddings.embed_text = _bedrock_embed_text
    embeddings.embed_texts = _bedrock_embed_texts
    opensearch_client.embed_text = _bedrock_embed_text
    opensearch_client.embed_texts = _bedrock_embed_texts
    signed_requests = _SignedRequests()
    opensearch_client.requests = signed_requests
    opensearch_client.OpenSearchClient.health = lambda client: _opensearch_health(
        client, signed_requests
    )
    graph_client.GraphDatabase = _GraphDatabaseAdapter
    llm.LLMClient = _bedrock_llm_class(llm)
    _INSTALLED = True
