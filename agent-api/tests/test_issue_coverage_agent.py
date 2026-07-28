from time import perf_counter

import pytest

from app import agent as agent_module
from app.agent import AgentService, _aspect_phase_budget_seconds
from app.evidence_selector import AspectEvidence, AspectEvidenceMatrix
from app.models import AnswerRequest
from app.reranker import RerankResult


def _item(content_unit_id: str) -> dict:
    document_id = content_unit_id.split("-article-", 1)[0]
    return {
        "document": {
            "documentId": document_id,
            "contentUnitId": content_unit_id,
            "articleContentUnitId": content_unit_id.split("-paragraph-", 1)[0],
            "docType": "law",
            "heading": content_unit_id,
            "text": content_unit_id,
        },
        "score": 0.1,
        "sources": ["initial_search"],
        "queries": [],
        "introducedBy": "initial_search",
    }


class _Unused:
    provider = "fake"


class RecordingReranker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], int | None]] = []

    def rerank(self, query, items, timeout_sec=None):
        item_ids = [item["document"]["contentUnitId"] for item in items]
        self.calls.append((query, item_ids, timeout_sec))
        ordered = list(reversed(items))
        return RerankResult(
            items=ordered,
            used=True,
            provider="fake",
            model="fake-reranker",
            scores={
                item["document"]["contentUnitId"]: float(index)
                for index, item in enumerate(ordered, start=1)
            },
        )


def test_answer_reserve_is_independent_from_llm_timeout():
    available, budget = _aspect_phase_budget_seconds(
        deadline=110.0,
        now=30.0,
        answer_reserve_sec=60,
        rerank_timeout_sec=30,
    )

    assert available == 20.0
    assert budget == 20.0


def test_aspect_phase_has_one_aggregate_reranker_cap():
    available, budget = _aspect_phase_budget_seconds(
        deadline=280.0,
        now=70.0,
        answer_reserve_sec=60,
        rerank_timeout_sec=30,
    )

    assert available == 150.0
    assert budget == 30.0


def test_final_aspect_rerank_includes_graph_inherited_candidate_without_query_rank():
    reranker = RecordingReranker()
    service = AgentService(_Unused(), _Unused(), _Unused(), reranker)
    initial_id = "law-test-article-1"
    graph_id = "law-order-article-5"
    matrix = AspectEvidenceMatrix([
        AspectEvidence(
            query="委任された具体的要件",
            searched_content_ids=[initial_id],
            ordered_content_ids=[initial_id],
            used=True,
        )
    ])

    final_matrix, _ = service._rerank_final_aspects(
        [_item(initial_id), _item(graph_id)],
        matrix,
        {graph_id: {"委任された具体的要件"}},
        perf_counter() + 10,
    )

    aspect = final_matrix.aspects[0]
    assert aspect.used is True
    assert set(aspect.searched_content_ids) == {initial_id, graph_id}
    assert aspect.inherited_content_ids == {graph_id}
    assert reranker.calls[0][1] == [initial_id, graph_id]


def test_final_aspect_rerank_skips_without_phase_budget():
    reranker = RecordingReranker()
    service = AgentService(_Unused(), _Unused(), _Unused(), reranker)
    matrix = AspectEvidenceMatrix([
        AspectEvidence(
            query="時間切れ論点",
            searched_content_ids=["law-test-article-1", "law-test-article-2"],
            ordered_content_ids=["law-test-article-1", "law-test-article-2"],
            used=True,
        )
    ])

    final_matrix, _ = service._rerank_final_aspects(
        [_item("law-test-article-1"), _item("law-test-article-2")],
        matrix,
        {},
        perf_counter(),
    )

    assert final_matrix.aspects[0].used is False
    assert final_matrix.aspects[0].skipped_reason == "aspect_phase_budget_exhausted"
    assert reranker.calls == []


class GraphOpenSearch:
    def __init__(self) -> None:
        self.target = _item("law-order-article-5")["document"]

    def get_by_content_unit_ids(self, content_unit_ids, clearance):
        return [self.target] if "law-order-article-5" in content_unit_ids else []


class ImplementsGraph:
    def paths_from_many(
        self,
        start_ids,
        edge_type=None,
        max_depth=2,
        limit=20,
        user_clearance_level=2,
    ):
        if edge_type != "IMPLEMENTS" or "law-test-article-1" not in start_ids:
            return []
        return [
            {
                "nodes": [
                    {
                        "graphNodeId": "law-test-article-1",
                        "contentUnitId": "law-test-article-1",
                        "documentId": "law-test",
                    },
                    {
                        "graphNodeId": "law-order-article-5",
                        "contentUnitId": "law-order-article-5",
                        "documentId": "law-order",
                    },
                ],
                "edges": [
                    {
                        "graphEdgeId": "edge-implements",
                        "edgeType": "IMPLEMENTS",
                        "relationConfidence": 0.95,
                    }
                ],
            }
        ]


def test_graph_candidate_inherits_aspect_without_legacy_aspect_flags():
    parent = _item("law-test-article-1")
    evidence = {"law-test-article-1": parent}
    matrix = AspectEvidenceMatrix([
        AspectEvidence(
            query="委任された具体的要件",
            searched_content_ids=["law-test-article-1"],
            ordered_content_ids=["law-test-article-1"],
            used=True,
        )
    ])
    inherited: dict[str, set[str]] = {}
    service = AgentService(GraphOpenSearch(), ImplementsGraph(), _Unused())

    service._expand_graph(
        AnswerRequest(question="具体的要件は何か", topK=2),
        evidence,
        rerank_top_k=2,
        candidate_top_k=10,
        aspect_matrix=matrix,
        inherited_aspects_by_content_id=inherited,
    )

    assert inherited == {
        "law-order-article-5": {"委任された具体的要件"}
    }
    assert "aspectQueries" not in evidence["law-test-article-1"]


@pytest.mark.parametrize(
    ("selection_enabled", "shadow_enabled", "target_is_returned"),
    [
        (False, True, False),
        (True, True, True),
    ],
)
def test_issue_coverage_shadow_and_active_modes(
    monkeypatch,
    selection_enabled,
    shadow_enabled,
    target_is_returned,
):
    reranker = RecordingReranker()
    service = AgentService(_Unused(), _Unused(), _Unused(), reranker)
    ranked = [_item(f"law-test-article-{index}") for index in range(1, 31)]
    target_id = "law-test-article-18"
    old_ranked = ranked[:16]
    matrix = AspectEvidenceMatrix([
        AspectEvidence(
            query="50名基準",
            searched_content_ids=["law-test-article-1", target_id],
            ordered_content_ids=["law-test-article-1", target_id],
            used=True,
        )
    ])
    monkeypatch.setattr(
        agent_module.settings,
        "agent_issue_coverage_selection",
        selection_enabled,
    )
    monkeypatch.setattr(
        agent_module.settings,
        "agent_issue_coverage_shadow",
        shadow_enabled,
    )
    monkeypatch.setattr(agent_module.settings, "agent_answer_reserve_sec", 1)
    monkeypatch.setattr(agent_module.settings, "rerank_timeout_sec", 5)
    trace = {}

    selected = service._apply_issue_coverage_selection(
        old_ranked,
        ranked,
        matrix,
        {},
        16,
        perf_counter() + 10,
        trace,
    )

    returned_ids = [item["document"]["contentUnitId"] for item in selected]
    assert (target_id in returned_ids) is target_is_returned
    assert target_id in trace["newContextContentUnitIds"]
    assert trace["oldContextContentUnitIds"] == [
        item["document"]["contentUnitId"] for item in old_ranked
    ]
    assert trace["shadowSelection"] == {
        "complete": True,
        "skippedAspectCount": 0,
    }
