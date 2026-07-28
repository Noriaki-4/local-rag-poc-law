def test_query_embedding_is_cached_for_law_and_guideline_searches(monkeypatch):
    from app import opensearch_client as module

    calls = []

    def fake_embed_text(query, dimension):
        calls.append((query, dimension))
        return [0.25] * dimension

    monkeypatch.setattr(module, "embed_text", fake_embed_text)
    client = module.OpenSearchClient()

    first = client._query_embedding("同じ検索語", 4)
    second = client._query_embedding("同じ検索語", 4)
    third = client._query_embedding("別の検索語", 4)

    assert first == second == (0.25, 0.25, 0.25, 0.25)
    assert third == first
    assert calls == [("同じ検索語", 4), ("別の検索語", 4)]


def test_vector_hits_are_reused_across_document_type_filters(monkeypatch):
    from app import opensearch_client as module

    post_calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "hits": {
                    "hits": [
                        {
                            "_score": 1.0,
                            "_source": {
                                "contentUnitId": "law-1",
                                "docType": "law",
                                "publishStatus": "published",
                                "isLatest": True,
                                "clearanceLevel": 1,
                            },
                        },
                        {
                            "_score": 0.9,
                            "_source": {
                                "contentUnitId": "guidance-1",
                                "docType": "guideline",
                                "publishStatus": "published",
                                "isLatest": True,
                                "clearanceLevel": 1,
                            },
                        },
                    ]
                }
            }

    def fake_post(url, json, timeout):
        post_calls.append((url, json, timeout))
        return FakeResponse()

    monkeypatch.setattr(module, "embed_text", lambda query, dimension: [0.1] * dimension)
    monkeypatch.setattr(module.requests, "post", fake_post)
    monkeypatch.setattr(module.settings, "embedding_dimension", 4)
    monkeypatch.setattr(module.settings, "agent_candidate_top_k", 20)
    client = module.OpenSearchClient()

    law = client._vector_search("同じ検索語", "law", 20, 2)
    guidance = client._vector_search("同じ検索語", "guideline", 10, 2)

    assert [item["_source"]["contentUnitId"] for item in law] == ["law-1"]
    assert [item["_source"]["contentUnitId"] for item in guidance] == ["guidance-1"]
    assert len(post_calls) == 1
