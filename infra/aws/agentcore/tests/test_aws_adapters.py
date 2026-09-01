import io
import json
import sys
from types import SimpleNamespace

import aws_adapters


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
