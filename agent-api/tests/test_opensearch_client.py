import json


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


def test_complete_article_lookup_pages_until_exact_total(monkeypatch):
    from app import opensearch_client as module

    bodies = []

    class FakeResponse:
        def __init__(self, source):
            self.source = source

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "hits": {
                    "total": {"value": 2, "relation": "eq"},
                    "hits": [{"_source": self.source}],
                }
            }

    def fake_post(url, json, timeout):
        bodies.append(json)
        suffix = len(bodies)
        return FakeResponse(
            {
                "contentUnitId": f"law-a-article-1-paragraph-{suffix}",
                "articleContentUnitId": "law-a-article-1",
                "text": f"第{suffix}項",
            }
        )

    monkeypatch.setattr(module.requests, "post", fake_post)
    sources = module.OpenSearchClient().get_complete_articles_by_ids(
        ["law-a-article-1"],
        3,
        page_size=1,
    )

    assert len(sources) == 2
    assert [body["from"] for body in bodies] == [0, 1]
    assert all(body["track_total_hits"] is True for body in bodies)


def test_article_navigation_contexts_use_one_bounded_query_per_article(monkeypatch):
    from app import opensearch_client as module

    requests_seen = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "responses": [
                    {
                        "hits": {
                            "hits": [
                                {
                                    "_source": {
                                        "contentUnitId": f"{article_id}-paragraph-1"
                                    }
                                }
                            ]
                        }
                    }
                    for article_id in ("law-a-article-1", "law-a-article-2")
                ]
            }

    def fake_post(url, data, headers, timeout):
        requests_seen.append((url, data.decode("utf-8"), headers, timeout))
        return FakeResponse()

    monkeypatch.setattr(module.requests, "post", fake_post)
    contexts = module.OpenSearchClient().get_article_navigation_contexts(
        ["law-a-article-1", "law-a-article-2"],
        3,
        max_chunks_per_article=3,
        timeout_sec=4.5,
    )

    assert list(contexts) == ["law-a-article-1", "law-a-article-2"]
    assert len(requests_seen) == 1
    url, payload, headers, timeout = requests_seen[0]
    assert url.endswith("/_msearch")
    assert headers == {"Content-Type": "application/x-ndjson"}
    assert timeout == 4.5
    lines = [json.loads(line) for line in payload.splitlines()]
    assert len(lines) == 4
    assert lines[1]["size"] == 3
    assert lines[3]["size"] == 3
    assert lines[1]["sort"][0] == {
        "paragraphNumber": {"order": "asc", "missing": "_first"}
    }
