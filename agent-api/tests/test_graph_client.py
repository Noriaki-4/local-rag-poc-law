"""GraphClientの複数edge type batchとtimeout override のテスト (計画書 §8.5, §16.3)。"""

from typing import Any

import pytest

from app import graph_client as graph_client_module
from app.graph_client import GraphClient


class _FakeResult(list):
    def consume(self) -> None:
        return None


class _FakeTransaction:
    def __init__(self, recorder: dict[str, Any], timeout: float | None) -> None:
        self.recorder = recorder
        self.recorder["timeout"] = timeout

    def __enter__(self) -> "_FakeTransaction":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def run(self, query: str, **kwargs: Any) -> _FakeResult:
        self.recorder["query"] = query
        self.recorder["kwargs"] = kwargs
        return _FakeResult()


class _FakeSession:
    def __init__(self, recorder: dict[str, Any]) -> None:
        self.recorder = recorder

    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def begin_transaction(self, timeout: float | None = None) -> _FakeTransaction:
        return _FakeTransaction(self.recorder, timeout)

    def run(self, query: str, **kwargs: Any) -> _FakeResult:
        self.recorder["query"] = query
        self.recorder["kwargs"] = kwargs
        return _FakeResult()


class _FakeDriver:
    def __init__(self, recorder: dict[str, Any]) -> None:
        self.recorder = recorder

    def session(self) -> _FakeSession:
        return _FakeSession(self.recorder)


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> tuple[GraphClient, dict[str, Any]]:
    recorder: dict[str, Any] = {}
    monkeypatch.setattr(
        graph_client_module.GraphDatabase,
        "driver",
        lambda *args, **kwargs: _FakeDriver(recorder),
    )
    return GraphClient(), recorder


class TestBatchTraversal:
    def test_ensure_legal_graph_schema_runs_all_statements(
        self, client: tuple[GraphClient, dict[str, Any]]
    ) -> None:
        graph, recorder = client
        graph.ensure_legal_graph_schema()
        assert "classification_run_snapshot_id" in recorder["query"]

    def test_seed_nodes_uses_unwind_batch(
        self, client: tuple[GraphClient, dict[str, Any]]
    ) -> None:
        graph, recorder = client
        graph.seed_nodes(
            [
                {"graphNodeId": "law-a-article-1", "nodeType": "Article"},
                {"graphNodeId": "law-a-article-2", "nodeType": "Article"},
            ]
        )
        assert "UNWIND $rows AS row" in recorder["query"]
        assert ":GraphNode:Article" in recorder["query"]
        assert len(recorder["kwargs"]["rows"]) == 2

    def test_seed_edges_uses_unwind_batch(
        self, client: tuple[GraphClient, dict[str, Any]]
    ) -> None:
        graph, recorder = client
        graph.seed_edges(
            [
                {
                    "graphEdgeId": "edge-1",
                    "edgeType": "REFERENCES",
                    "fromGraphNodeId": "law-a-article-1",
                    "toGraphNodeId": "law-a-article-2",
                },
                {
                    "graphEdgeId": "edge-2",
                    "edgeType": "REFERENCES",
                    "fromGraphNodeId": "law-a-article-2",
                    "toGraphNodeId": "law-a-article-1",
                },
            ]
        )
        assert "UNWIND $rows AS row" in recorder["query"]
        assert "[r:REFERENCES" in recorder["query"]
        assert "MATCH (from:GraphNode" in recorder["query"]
        assert "MATCH (to:GraphNode" in recorder["query"]
        assert len(recorder["kwargs"]["rows"]) == 2

    def test_seed_edges_rejects_invalid_type_before_writing(
        self, client: tuple[GraphClient, dict[str, Any]]
    ) -> None:
        graph, recorder = client
        with pytest.raises(ValueError, match="Invalid edgeType"):
            graph.seed_edges(
                [
                    {
                        "graphEdgeId": "edge-1",
                        "edgeType": "REFERENCES] DELETE n",
                        "fromGraphNodeId": "law-a-article-1",
                        "toGraphNodeId": "law-a-article-2",
                    }
                ]
            )
        assert recorder == {}

    def test_multiple_edge_types_use_relationship_union(
        self, client: tuple[GraphClient, dict[str, Any]]
    ) -> None:
        graph, recorder = client
        graph.paths_from_many(
            ["law-a-article-1"],
            edge_types=["IMPLEMENTS", "APPLIED_BY"],
            max_depth=1,
        )
        assert ":IMPLEMENTS|APPLIED_BY" in recorder["query"]

    def test_unregistered_edge_type_is_rejected(
        self, client: tuple[GraphClient, dict[str, Any]]
    ) -> None:
        graph, _ = client
        with pytest.raises(ValueError):
            graph.paths_from_many(["law-a-article-1"], edge_types=["DROP_TABLE"])

    def test_unimplemented_edge_type_is_rejected(
        self, client: tuple[GraphClient, dict[str, Any]]
    ) -> None:
        """未実装エッジを前提にした探索は拒否する (§6.1)。"""
        graph, _ = client
        with pytest.raises(ValueError):
            graph.paths_from_many(["law-a-article-1"], edge_types=["EXCEPTION_TO"])

    def test_injection_attempt_is_rejected(
        self, client: tuple[GraphClient, dict[str, Any]]
    ) -> None:
        graph, _ = client
        with pytest.raises(ValueError):
            graph.paths_from_many(["law-a-article-1"], edge_types=["IMPLEMENTS]->() DELETE n //"])

    def test_legacy_single_edge_type_still_works(
        self, client: tuple[GraphClient, dict[str, Any]]
    ) -> None:
        graph, recorder = client
        graph.paths_from_many(["law-a-article-1"], edge_type="REFERENCES")
        assert ":REFERENCES" in recorder["query"]

    def test_duplicate_start_ids_are_collapsed(
        self, client: tuple[GraphClient, dict[str, Any]]
    ) -> None:
        graph, recorder = client
        graph.paths_from_many(["law-a-article-1", "law-a-article-1"])
        assert recorder["kwargs"]["fromGraphNodeIds"] == ["law-a-article-1"]


class TestTimeout:
    def test_timeout_is_passed_to_driver(
        self, client: tuple[GraphClient, dict[str, Any]]
    ) -> None:
        graph, recorder = client
        graph.paths_from_many(["law-a-article-1"], timeout_sec=1.5)
        assert recorder["timeout"] == 1.5

    def test_no_timeout_keeps_previous_behaviour(
        self, client: tuple[GraphClient, dict[str, Any]]
    ) -> None:
        graph, recorder = client
        graph.paths_from_many(["law-a-article-1"])
        assert "timeout" not in recorder

    def test_exhausted_budget_skips_the_query(
        self, client: tuple[GraphClient, dict[str, Any]]
    ) -> None:
        graph, recorder = client
        assert graph.paths_from_many(["law-a-article-1"], timeout_sec=0) == []
        assert recorder == {}


class TestAssertionsAndInventory:
    def test_relation_assertions_query_uses_from_article_ids(
        self, client: tuple[GraphClient, dict[str, Any]]
    ) -> None:
        graph, recorder = client
        graph.relation_assertions_from(["law-a-article-27_2"])
        assert "RelationAssertion" in recorder["query"]
        assert "assertion.status IN $visibleStatuses" in recorder["query"]
        assert recorder["kwargs"]["visibleStatuses"] == [
            "unverified",
            "llm_classified_uncertain",
            "llm_classified_implements",
        ]
        assert "coalesce(to.clearanceLevel, 3)" in recorder["query"]
        assert recorder["kwargs"]["fromArticleIds"] == ["law-a-article-27_2"]

    def test_empty_inputs_short_circuit(
        self, client: tuple[GraphClient, dict[str, Any]]
    ) -> None:
        graph, recorder = client
        assert graph.relation_assertions_from([]) == []
        assert graph.paths_from_many([]) == []
        assert recorder == {}

    def test_touching_assertions_query_checks_both_endpoints_and_clearance(
        self, client: tuple[GraphClient, dict[str, Any]]
    ) -> None:
        graph, recorder = client
        graph.relation_assertions_touching(
            ["law-a-article-27_2"],
            suggested_types=["IMPLEMENTS"],
            user_clearance_level=2,
            timeout_sec=1.5,
        )
        assert "assertion.fromArticleId IN $articleIds" in recorder["query"]
        assert "assertion.toArticleId IN $articleIds" in recorder["query"]
        assert "assertion.status IN $visibleStatuses" in recorder["query"]
        assert recorder["kwargs"]["visibleStatuses"] == [
            "unverified",
            "llm_classified_uncertain",
            "llm_classified_implements",
        ]
        assert "coalesce(from.clearanceLevel, 3)" in recorder["query"]
        assert "coalesce(to.clearanceLevel, 3)" in recorder["query"]
        assert recorder["kwargs"]["suggestedTypes"] == ["IMPLEMENTS"]
        assert recorder["kwargs"]["userClearanceLevel"] == 2
        assert recorder["timeout"] == 1.5

    def test_touching_formal_relations_query_checks_both_directions(
        self, client: tuple[GraphClient, dict[str, Any]]
    ) -> None:
        graph, recorder = client
        graph.article_relations_touching(
            ["law-a-article-27_2"],
            edge_types=["REFERENCES", "IMPLEMENTS"],
            user_clearance_level=2,
            timeout_sec=1.5,
        )
        assert "from.graphNodeId = articleId" in recorder["query"]
        assert "to.graphNodeId = articleId" in recorder["query"]
        assert "from.graphNodeId STARTS WITH articleId + '-'" in recorder["query"]
        assert "to.graphNodeId STARTS WITH articleId + '-'" in recorder["query"]
        assert "type(relation) IN $edgeTypes" in recorder["query"]
        assert recorder["kwargs"]["edgeTypes"] == ["REFERENCES", "IMPLEMENTS"]
        assert recorder["kwargs"]["userClearanceLevel"] == 2
        assert recorder["timeout"] == 1.5

    def test_touching_formal_relations_rejects_unimplemented_type(
        self, client: tuple[GraphClient, dict[str, Any]]
    ) -> None:
        graph, _ = client
        with pytest.raises(ValueError):
            graph.article_relations_touching(
                ["law-a-article-1"],
                edge_types=["EXCEPTION_TO"],
            )

    def test_touching_assertions_rejects_unimplemented_type(
        self, client: tuple[GraphClient, dict[str, Any]]
    ) -> None:
        graph, _ = client
        with pytest.raises(ValueError):
            graph.relation_assertions_touching(
                ["law-a-article-1"],
                suggested_types=["EXCEPTION_TO"],
            )

    def test_classification_updates_only_assertion_properties(
        self, client: tuple[GraphClient, dict[str, Any]]
    ) -> None:
        graph, recorder = client
        graph.update_relation_classifications(
            [{"assertionId": "assertion-1", "status": "llm_classified_implements"}]
        )
        assert "MATCH (assertion:RelationAssertion" in recorder["query"]
        assert "SET assertion += item" in recorder["query"]
        assert "MERGE" not in recorder["query"]
