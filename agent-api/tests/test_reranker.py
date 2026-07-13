from app.reranker import RerankerClient


def _item(content_unit_id: str, text: str) -> dict:
    return {
        "document": {
            "contentUnitId": content_unit_id,
            "title": "検証法",
            "heading": content_unit_id,
            "text": text,
        },
        "score": 1.0,
    }


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "model": "local-test-reranker",
            "results": [
                {"index": 1, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.1},
            ],
        }


def test_local_reranker_reorders_documents(monkeypatch):
    from app import reranker as reranker_module

    monkeypatch.setattr(reranker_module.settings, "rerank_provider", "local_http")
    monkeypatch.setattr(reranker_module.settings, "rerank_base_url", "http://reranker")
    monkeypatch.setattr(reranker_module.requests, "post", lambda *args, **kwargs: FakeResponse())

    result = RerankerClient().rerank(
        "正しい要件",
        [_item("law-test-article-1", "無関係"), _item("law-test-article-2", "正しい要件")],
    )

    assert result.used is True
    assert [item["document"]["contentUnitId"] for item in result.items] == [
        "law-test-article-2",
        "law-test-article-1",
    ]
    assert result.scores["law-test-article-2"] == 0.9


def test_local_reranker_falls_back_on_error(monkeypatch):
    from app import reranker as reranker_module

    monkeypatch.setattr(reranker_module.settings, "rerank_provider", "local_http")

    def fail(*args, **kwargs):
        raise TimeoutError("timeout")

    monkeypatch.setattr(reranker_module.requests, "post", fail)
    items = [_item("law-test-article-1", "第一条"), _item("law-test-article-2", "第二条")]

    result = RerankerClient().rerank("要件", items)

    assert result.used is False
    assert result.items == items
    assert "timeout" in result.error
