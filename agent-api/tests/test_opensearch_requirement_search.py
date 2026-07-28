"""Requirement別のレイヤー指定検索とArticle集約のテスト (計画書 §5.2, §9.1-9.3, §11.7)。"""

import json
from typing import Any

import pytest

from app import opensearch_client as opensearch_module
from app.legal_ontology import (
    AUTHORITY_CABINET_OFFICE_ORDINANCE,
    AUTHORITY_MINISTERIAL_ORDINANCE,
    AUTHORITY_ORDINANCE_UNSPECIFIED,
)
from app.opensearch_client import OpenSearchClient, RequirementSearchSpec


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


def _hit(content_unit_id: str, article_id: str, score: float, authority_type: str | None) -> dict:
    return {
        "_score": score,
        "_source": {
            "contentUnitId": content_unit_id,
            "articleContentUnitId": article_id,
            "documentId": article_id.split("-article-")[0],
            "authorityType": authority_type,
            "heading": "第10条",
            "text": "公告事項を定める。",
        },
    }


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> tuple[OpenSearchClient, dict[str, Any]]:
    recorder: dict[str, Any] = {}
    monkeypatch.setattr(opensearch_module.settings, "agent_use_bm25", True)
    monkeypatch.setattr(opensearch_module.settings, "agent_use_vector", False)

    def fake_post(url: str, **kwargs: Any) -> _FakeResponse:
        recorder["url"] = url
        recorder["timeout"] = kwargs.get("timeout")
        body = kwargs.get("data")
        if body is not None:
            lines = body.decode("utf-8").strip().splitlines()
            recorder["bodies"] = [json.loads(line) for line in lines[1::2]]
        return _FakeResponse(recorder.get("payload", {"responses": []}))

    monkeypatch.setattr(opensearch_module.requests, "post", fake_post)
    return OpenSearchClient(), recorder


class TestBatchedRequirementSearch:
    def test_all_specs_go_into_a_single_msearch(
        self, client: tuple[OpenSearchClient, dict[str, Any]]
    ) -> None:
        os_client, recorder = client
        recorder["payload"] = {
            "responses": [
                {"hits": {"hits": [_hit("c1", "law-a-article-10", 5.0, "act")]}},
                {"hits": {"hits": []}},
            ]
        }
        specs = [
            RequirementSearchSpec(requirement_id="req-1", query="公開買付開始公告"),
            RequirementSearchSpec(requirement_id="req-2", query="株券等所有割合"),
        ]
        results = os_client.search_requirement_specs(specs, user_clearance_level=2)
        assert recorder["url"].endswith("/_msearch")
        assert len(recorder["bodies"]) == 2
        assert list(results) == ["req-1", "req-2"]

    def test_empty_specs_do_not_call_opensearch(
        self, client: tuple[OpenSearchClient, dict[str, Any]]
    ) -> None:
        os_client, recorder = client
        assert os_client.search_requirement_specs([], user_clearance_level=2) == {}
        assert recorder == {}

    def test_timeout_override_is_passed(
        self, client: tuple[OpenSearchClient, dict[str, Any]]
    ) -> None:
        os_client, recorder = client
        recorder["payload"] = {"responses": [{"hits": {"hits": []}}]}
        os_client.search_requirement_specs(
            [RequirementSearchSpec(requirement_id="req-1", query="x")],
            user_clearance_level=2,
            timeout_sec=3.5,
        )
        assert recorder["timeout"] == 3.5


class TestAuthorityTypeFilter:
    def _filters(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        return body["query"]["bool"]["filter"]

    def test_ministerial_requirement_includes_undetermined_layers(
        self, client: tuple[OpenSearchClient, dict[str, Any]]
    ) -> None:
        os_client, recorder = client
        recorder["payload"] = {"responses": [{"hits": {"hits": []}}]}
        os_client.search_requirement_specs(
            [
                RequirementSearchSpec(
                    requirement_id="req-1",
                    query="公告事項",
                    authority_type=AUTHORITY_MINISTERIAL_ORDINANCE,
                )
            ],
            user_clearance_level=2,
        )
        terms = [
            clause
            for clause in self._filters(recorder["bodies"][0])
            if "bool" in clause
        ]
        values = terms[0]["bool"]["should"][0]["terms"]["authorityType"]
        assert AUTHORITY_ORDINANCE_UNSPECIFIED in values
        assert "unknown" in values

    def test_document_id_scope_skips_layer_filter(
        self, client: tuple[OpenSearchClient, dict[str, Any]]
    ) -> None:
        """documentId確定時はauthorityType未判別でも法令内を直接検索する (§9.1)。"""
        os_client, recorder = client
        recorder["payload"] = {"responses": [{"hits": {"hits": []}}]}
        os_client.search_requirement_specs(
            [
                RequirementSearchSpec(
                    requirement_id="req-1",
                    query="公告事項",
                    authority_type=AUTHORITY_CABINET_OFFICE_ORDINANCE,
                    document_ids=("law-402M50000040038",),
                )
            ],
            user_clearance_level=2,
        )
        filters = self._filters(recorder["bodies"][0])
        assert any("terms" in clause and "documentId" in clause["terms"] for clause in filters)
        assert not any("bool" in clause for clause in filters)

    def test_explicit_article_ids_are_fetched_without_query_match(
        self, client: tuple[OpenSearchClient, dict[str, Any]]
    ) -> None:
        os_client, recorder = client
        recorder["payload"] = {
            "responses": [
                {
                    "hits": {
                        "hits": [
                            _hit(
                                "law-a-article-27_3",
                                "law-a-article-27_3",
                                0.0,
                                "act",
                            )
                        ]
                    }
                },
                {"hits": {"hits": []}},
            ]
        }
        os_client.search_requirement_specs(
            [
                RequirementSearchSpec(
                    requirement_id="req-1",
                    query="公告事項",
                    article_ids=("law-a-article-27_3",),
                )
            ],
            user_clearance_level=2,
        )
        direct_filter = recorder["bodies"][0]["query"]["bool"]["filter"][-1]["bool"][
            "should"
        ]
        assert any(
            "term" in clause and "articleContentUnitId" in clause["term"]
            for clause in direct_filter
        )
        assert len(recorder["bodies"]) == 2

    def test_vector_and_bm25_are_fused_in_one_msearch(
        self,
        client: tuple[OpenSearchClient, dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        os_client, recorder = client
        monkeypatch.setattr(opensearch_module.settings, "agent_use_vector", True)
        monkeypatch.setattr(
            opensearch_module,
            "embed_texts",
            lambda texts, dimension, timeout_sec=None: [[0.1] * dimension for _ in texts],
        )
        recorder["payload"] = {
            "responses": [
                {"hits": {"hits": [_hit("c1", "law-a-article-1", 4.0, "act")]}},
                {"hits": {"hits": [_hit("c2", "law-b-article-2", 0.8, "act")]}},
            ]
        }
        results = os_client.search_requirement_specs(
            [RequirementSearchSpec(requirement_id="req-1", query="公開買付け")],
            user_clearance_level=2,
        )
        assert len(recorder["bodies"]) == 2
        assert "multi_match" in recorder["bodies"][0]["query"]["bool"]["must"][0]
        assert "knn" in recorder["bodies"][1]["query"]
        assert {candidate["articleId"] for candidate in results["req-1"]} == {
            "law-a-article-1",
            "law-b-article-2",
        }


class TestArticleAggregation:
    def test_chunks_of_the_same_article_are_merged(
        self, client: tuple[OpenSearchClient, dict[str, Any]]
    ) -> None:
        os_client, recorder = client
        recorder["payload"] = {
            "responses": [
                {
                    "hits": {
                        "hits": [
                            _hit("c1", "law-a-article-10", 5.0, "cabinet_office_ordinance"),
                            _hit("c2", "law-a-article-10", 7.0, "cabinet_office_ordinance"),
                            _hit("c3", "law-a-article-11", 1.0, "cabinet_office_ordinance"),
                        ]
                    }
                }
            ]
        }
        results = os_client.search_requirement_specs(
            [RequirementSearchSpec(requirement_id="req-1", query="公告")],
            user_clearance_level=2,
        )
        candidates = results["req-1"]
        assert [candidate["articleId"] for candidate in candidates] == [
            "law-a-article-10",
            "law-a-article-11",
        ]
        assert candidates[0]["score"] > candidates[1]["score"]
        assert candidates[0]["retrievalSources"] == ["bm25"]
        assert len(candidates[0]["chunks"]) == 2

    def test_exact_authority_type_ranks_before_undetermined(
        self, client: tuple[OpenSearchClient, dict[str, Any]]
    ) -> None:
        os_client, recorder = client
        recorder["payload"] = {
            "responses": [
                {
                    "hits": {
                        "hits": [
                            _hit("c1", "law-x-article-1", 9.0, AUTHORITY_ORDINANCE_UNSPECIFIED),
                            _hit("c2", "law-y-article-1", 3.0, AUTHORITY_CABINET_OFFICE_ORDINANCE),
                        ]
                    }
                }
            ]
        }
        results = os_client.search_requirement_specs(
            [
                RequirementSearchSpec(
                    requirement_id="req-1",
                    query="公告",
                    authority_type=AUTHORITY_CABINET_OFFICE_ORDINANCE,
                )
            ],
            user_clearance_level=2,
        )
        assert [candidate["articleId"] for candidate in results["req-1"]] == [
            "law-y-article-1",
            "law-x-article-1",
        ]
