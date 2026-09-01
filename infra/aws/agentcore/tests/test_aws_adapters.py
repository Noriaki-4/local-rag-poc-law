import io
import json
import sys
from types import SimpleNamespace

import pytest

import aws_adapters


def test_bedrock_converse_falls_back_when_compiled_grammar_is_too_large():
    requests = []

    class ValidationException(Exception):
        response = {
            "Error": {
                "Code": "ValidationException",
                "Message": "The compiled grammar is too large, simplify the schema",
            }
        }

    class Runtime:
        def converse(self, **request):
            requests.append(request)
            if len(requests) == 1:
                raise ValidationException()
            return {"output": {"message": {"content": [{"text": "{}"}]}}}

    result = aws_adapters._bedrock_converse_with_schema_fallback(
        Runtime(),
        {
            "modelId": "haiku",
            "messages": [],
            "outputConfig": {
                "textFormat": {
                    "type": "json_schema",
                    "structure": {
                        "jsonSchema": {"schema": '{"type":"object"}'}
                    },
                }
            },
        },
    )

    assert result["output"]["message"]["content"][0]["text"] == "{}"
    assert "outputConfig" in requests[0]
    assert "outputConfig" not in requests[1]
    assert requests[1]["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"] == {
        "json": {"type": "object"}
    }


def test_bedrock_converse_does_not_hide_other_validation_errors():
    class ValidationException(Exception):
        response = {
            "Error": {"Code": "ValidationException", "Message": "invalid model"}
        }

    class Runtime:
        def converse(self, **_request):
            raise ValidationException()

    with pytest.raises(ValidationException):
        aws_adapters._bedrock_converse_with_schema_fallback(
            Runtime(), {"modelId": "haiku"}
        )


def test_bedrock_response_prefers_tool_input_as_json():
    response = {
        "output": {
            "message": {
                "content": [
                    {
                        "toolUse": {
                            "name": "legal_agent_response",
                            "input": {"decision": "search"},
                        }
                    }
                ]
            }
        }
    }

    assert json.loads(aws_adapters._bedrock_response_json_text(response)) == {
        "decision": "search"
    }


def test_neptune_session_adapts_execute_query_results():
    class Client:
        def execute_query(self, **request):
            assert request["graphIdentifier"] == "g-123"
            assert request["parameters"] == {"nodeId": "n-1"}
            return {
                "payload": io.BytesIO(
                    json.dumps({"results": [{"value": 1}]}).encode()
                )
            }

    result = aws_adapters._NeptuneSession(Client(), "g-123").run(
        "MATCH (n {id: $nodeId}) RETURN n", nodeId="n-1"
    )

    assert list(result) == [{"value": 1}]
    assert result.single() == {"value": 1}


def test_neptune_session_rewrites_neo4j_predicates_and_passes_timeout():
    requests = []

    class Client:
        def execute_query(self, **request):
            requests.append(request)
            return {"payload": io.BytesIO(b'{"results": []}')}

    query = """
    MATCH path = (start)-[*1..2]->(target)
    WHERE all(node IN nodes(path) WHERE coalesce(node.clearanceLevel, 3) <= $userClearanceLevel)
    AND any(articleId IN $articleIds WHERE
      from.graphNodeId = articleId
      OR from.graphNodeId STARTS WITH articleId + '-'
    )
    AND any(articleId IN $articleIds WHERE
      to.graphNodeId = articleId
      OR to.graphNodeId STARTS WITH articleId + '-'
    )
    RETURN path
    """
    session = aws_adapters._NeptuneSession(Client(), "g-123")
    with session.begin_transaction(timeout=1.25) as transaction:
        transaction.run(query, articleIds=["a-1"], userClearanceLevel=3)

    request = requests[0]
    assert request["queryTimeoutMilliseconds"] == 1250
    assert "all(" not in request["queryString"]
    assert "any(" not in request["queryString"]
    assert "size(nodes(path))" in request["queryString"]
    assert request["queryString"].count("]) > 0") == 2


def test_titan_embedding_normalizes_and_truncates_input(monkeypatch):
    requests = []

    class Client:
        def invoke_model(self, **request):
            requests.append(request)
            return {
                "body": io.BytesIO(
                    json.dumps({"embedding": [0.0, 1.0]}).encode()
                )
            }

    monkeypatch.setitem(
        sys.modules,
        "boto3",
        SimpleNamespace(client=lambda *_args, **_kwargs: Client()),
    )
    monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
    monkeypatch.setenv("EMBEDDING_MODEL", "amazon.titan-embed-text-v2:0")
    monkeypatch.setenv("EMBEDDING_MAX_CHARS", "5")

    assert aws_adapters._bedrock_embeddings(["  ab   cdef  "], 2) == [[0.0, 1.0]]
    assert json.loads(requests[0]["body"])["inputText"] == "ab cd"


def test_serverless_msearch_uses_supported_get_operation(monkeypatch):
    adapter = object.__new__(aws_adapters._SignedRequests)
    calls = []
    monkeypatch.setattr(
        adapter,
        "request",
        lambda method, url, **kwargs: calls.append((method, url, kwargs)),
    )

    adapter.post("https://collection.example/legal-index/_msearch", data=b"{}\n")

    assert calls == [
        (
            "GET",
            "https://collection.example/legal-index/_msearch",
            {"data": b"{}\n"},
        )
    ]


def test_serverless_signing_includes_payload_hash(monkeypatch):
    class AWSRequest:
        def __init__(self, *, method, url, data, headers):
            self.method = method
            self.url = url
            self.data = data
            self.headers = headers

    class SigV4Auth:
        def __init__(self, credentials, service, region):
            assert credentials.access_key == "access-key"
            assert service == "aoss"
            assert region == "ap-northeast-1"

        def add_auth(self, request):
            request.headers["Authorization"] = "signed"
            request.headers["X-Amz-Security-Token"] = "session-token"

    monkeypatch.setitem(sys.modules, "botocore", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules, "botocore.auth", SimpleNamespace(SigV4Auth=SigV4Auth)
    )
    monkeypatch.setitem(
        sys.modules, "botocore.awsrequest", SimpleNamespace(AWSRequest=AWSRequest)
    )

    class Credentials:
        def get_frozen_credentials(self):
            return SimpleNamespace(
                access_key="access-key",
                secret_key="secret-key",
                token="session-token",
            )

    adapter = object.__new__(aws_adapters._SignedRequests)
    adapter.session = SimpleNamespace(get_credentials=lambda: Credentials())
    captured = {}

    def request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(aws_adapters.http_requests, "request", request)
    monkeypatch.setenv("AWS_REGION", "ap-northeast-1")

    adapter.post("https://collection.example/index/_search", json={"size": 1})

    assert captured["headers"]["X-Amz-Content-SHA256"] == (
        "ce9ae3ceabbd877466092a7ed74e24f1c8eea7672f9cfc6dc5aa259fff248e5e"
    )
    assert "X-Amz-Security-Token" in captured["headers"]
