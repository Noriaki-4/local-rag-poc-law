from copy import deepcopy
import io
import json

import pytest

import bootstrap_aws_data


def _definition(engine="lucene", dimensions=1024):
    return {
        "settings": {"index": {"knn": True}},
        "mappings": {
            "properties": {
                "embedding": {
                    "type": "knn_vector",
                    "dimension": dimensions,
                    "method": {
                        "name": "hnsw",
                        "engine": engine,
                        "space_type": "cosinesimil",
                        "parameters": {},
                    },
                }
            }
        },
    }


def test_serverless_definition_converts_lucene_to_faiss_without_mutating_artifact():
    source = _definition()
    original = deepcopy(source)

    result = bootstrap_aws_data._serverless_index_definition(source, 1024)

    assert result["mappings"]["properties"]["embedding"]["method"]["engine"] == "faiss"
    assert source == original


def test_serverless_definition_rejects_dimension_mismatch():
    with pytest.raises(ValueError, match="dimension"):
        bootstrap_aws_data._serverless_index_definition(_definition(dimensions=768), 1024)


def test_serverless_definition_rejects_unknown_engine():
    with pytest.raises(ValueError, match="unsupported"):
        bootstrap_aws_data._serverless_index_definition(_definition(engine="nmslib"), 1024)


def test_wait_for_index_retries_serverless_visibility_delay():
    class Client:
        def __init__(self):
            self.responses = [{"status": 404}, {"legal-rag-content-ja-v2": {}}]

        def request(self, method, index, allowed):
            assert (method, index, allowed) == (
                "GET",
                "legal-rag-content-ja-v2",
                (200, 404),
            )
            return self.responses.pop(0)

    client = Client()
    bootstrap_aws_data._wait_for_index(
        client, "legal-rag-content-ja-v2", attempts=2, delay_seconds=0
    )
    assert client.responses == []


def test_serverless_bulk_action_uses_generated_id():
    action = bootstrap_aws_data._serverless_bulk_action("legal-rag-content-ja-v2")

    assert action == {"index": {"_index": "legal-rag-content-ja-v2"}}
    assert "_id" not in action["index"]


def test_embedding_workers_are_bounded(monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_EMBEDDING_WORKERS", "2")
    assert bootstrap_aws_data._embedding_workers() == 2

    monkeypatch.setenv("BOOTSTRAP_EMBEDDING_WORKERS", "33")
    with pytest.raises(ValueError, match="between 1 and 32"):
        bootstrap_aws_data._embedding_workers()


def test_titan_embedding_retries_throttling(monkeypatch):
    sleeps = []

    class Throttled(Exception):
        response = {"Error": {"Code": "ThrottlingException"}}

    class Client:
        def __init__(self):
            self.calls = 0

        def invoke_model(self, **request):
            self.calls += 1
            assert json.loads(request["body"])["dimensions"] == 2
            if self.calls < 3:
                raise Throttled()
            return {"body": io.BytesIO(b'{"embedding":[0.0,1.0]}')}

    client = Client()
    monkeypatch.setattr(bootstrap_aws_data.time, "sleep", sleeps.append)

    assert bootstrap_aws_data._embed(client, "titan", "text", 2, 100) == [
        0.0,
        1.0,
    ]
    assert sleeps == [1, 2]
