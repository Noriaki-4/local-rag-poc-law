from app.agent import AgentService
from app.llm import EvidenceEvaluationResult, LLMResult, SearchPlanResult
from app.models import AnswerRequest


def _document(content_unit_id: str, text: str) -> dict:
    return {
        "documentId": "law-test",
        "contentUnitId": content_unit_id,
        "parentContentUnitId": None,
        "title": "検証法",
        "heading": content_unit_id,
        "sourceObjectUri": "minio://test/source.xml",
        "sourcePage": None,
        "text": text,
    }


class FakeOpenSearch:
    def __init__(self):
        self.search_calls = []
        self.source = _document("law-test-article-2", "第二条 第三条を参照する。")
        self.target = _document("law-test-article-3", "第三条 正しい要件を定める。")

    def search(self, query, doc_type, top_k, clearance, use_bm25=True, use_vector=True):
        self.search_calls.append(query)
        if len(self.search_calls) == 1:
            return [{"document": self.source, "score": 1.0, "bm25Score": 2.0, "vectorScore": 0.8}]
        return []

    def get_by_content_unit_ids(self, content_unit_ids, clearance):
        return [self.target] if self.target["contentUnitId"] in content_unit_ids else []


class FakeGraph:
    def paths_from_many(self, start_ids, edge_type=None, max_depth=2, limit=20, user_clearance_level=2):
        return [
            {
                "nodes": [
                    {"graphNodeId": "law-test-article-2", "contentUnitId": "law-test-article-2"},
                    {"graphNodeId": "law-test-article-3", "contentUnitId": "law-test-article-3"},
                ],
                "edges": [{"graphEdgeId": "edge-2-3", "edgeType": "REFERENCES"}],
            }
        ]


class FakeLLM:
    provider = "fake"

    def plan_search(self, request, max_queries, timeout_sec=None):
        return SearchPlanResult(
            queries=["検証法 第二条 要件"],
            graphRequired=True,
            provider="fake",
            model="fake-planner",
            latencyMs=1,
            inputTokens=10,
            outputTokens=5,
        )

    def evaluate_evidence(self, request, citations, max_queries=2, timeout_sec=None):
        return EvidenceEvaluationResult(
            choiceCoverage={"A": "sufficient", "B": "missing"},
            followUpQueries=["検証法 第三条"],
            graphRequired=True,
            stop=False,
            provider="fake",
            model="fake-evaluator",
            latencyMs=1,
            inputTokens=10,
            outputTokens=5,
        )

    def generate_answer(self, request, route, citations, timeout_sec=None, evidence_by_choice=None):
        return LLMResult(
            text="第三条を根拠にAと判断します。",
            provider="fake",
            model="fake-answer",
            latencyMs=1,
            inputTokens=10,
            outputTokens=5,
            estimatedCost=0,
            answer="第三条を根拠にAと判断します。",
            predictedAnswer="A",
            choiceJudgements={"A": "supported", "B": "not_supported"},
        )


def test_deepsearch_decomposes_expands_and_merges(monkeypatch):
    from app import agent as agent_module

    monkeypatch.setattr(agent_module.settings, "agent_use_llm_planner", True)
    os_client = FakeOpenSearch()
    service = AgentService(os_client, FakeGraph(), FakeLLM())
    request = AnswerRequest(
        question="正しいものはどれか。",
        choices={"A": "第三条の要件を満たす。", "B": "要件は存在しない。"},
        pattern="pattern_4_deepsearch",
        topK=2,
    )

    response = service.answer(request)

    assert response.predictedAnswer == "A"
    assert "query_decomposition" in response.route
    assert "graph_search_tool" in response.route
    assert "follow_up_search" in response.route
    assert {citation.contentUnitId for citation in response.citations} == {
        "law-test-article-2",
        "law-test-article-3",
    }
    assert response.trace["toolCallCount"] <= response.trace["limits"]["maxTotalToolCalls"]
    assert response.trace["llmCallCount"] == 3
    assert response.trace["evaluator"]["used"] is True
    assert set(response.trace["choiceEvidence"]) == {"A", "B"}
    assert response.trace["graphExpandedContentUnitIds"] == ["law-test-article-3"]
    assert response.trace["retrievedGraphEdgeIds"] == ["edge-2-3"]


class RecordingGraph:
    def __init__(self):
        self.calls = []

    def paths_from_many(self, start_ids, edge_type=None, max_depth=2, limit=20, user_clearance_level=2):
        self.calls.append({"edge_type": edge_type, "start_ids": list(start_ids), "max_depth": max_depth})
        if edge_type == "HAS_CONTENT_UNIT":
            return [
                {
                    "nodes": [
                        {"graphNodeId": "law-test-article-9", "contentUnitId": "law-test-article-9"},
                        {"graphNodeId": "law-test-article-9-paragraph-1", "contentUnitId": "law-test-article-9-paragraph-1"},
                    ],
                    "edges": [{"graphEdgeId": "edge-9-has-content-unit-paragraph-1", "edgeType": "HAS_CONTENT_UNIT"}],
                }
            ]
        return []


class SiblingOpenSearch:
    def __init__(self):
        self.doc = _document("law-test-article-9-paragraph-2", "前項に規定する要件を準用する。")
        self.doc["parentContentUnitId"] = "law-test-article-9"
        self.doc["articleContentUnitId"] = "law-test-article-9"
        self.sibling = _document("law-test-article-9-paragraph-1", "第一項 基本要件を定める。")

    def search(self, query, doc_type, top_k, clearance, use_bm25=True, use_vector=True):
        return [{"document": self.doc, "score": 1.0, "bm25Score": 2.0, "vectorScore": 0.8}]

    def get_by_content_unit_ids(self, content_unit_ids, clearance):
        return [self.sibling] if self.sibling["contentUnitId"] in content_unit_ids else []


def test_sibling_expansion_traverses_hierarchy_for_paragraph_reference(monkeypatch):
    from app import agent as agent_module

    monkeypatch.setattr(agent_module.settings, "agent_use_llm_planner", False)
    graph = RecordingGraph()
    service = AgentService(SiblingOpenSearch(), graph, FakeLLM())
    request = AnswerRequest(
        question="前項の要件について正しいものはどれか。",
        choices={"A": "基本要件を満たす。", "B": "要件は存在しない。"},
        pattern="pattern_3_controlled_agentic_rag",
        topK=2,
    )

    response = service.answer(request)

    hierarchy_calls = [call for call in graph.calls if call["edge_type"] == "HAS_CONTENT_UNIT"]
    assert hierarchy_calls, "HAS_CONTENT_UNIT traversal should fire for 前項 reference"
    assert "law-test-article-9" in hierarchy_calls[0]["start_ids"]
    assert "law-test-article-9-paragraph-1" in {c.contentUnitId for c in response.citations}


def test_needs_sibling_expansion_detects_cues():
    from app.agent import _needs_sibling_expansion

    request = AnswerRequest(question="前項の定める要件はどれか。", choices={"A": "a", "B": "b"})
    assert _needs_sibling_expansion(request, []) is True

    plain = AnswerRequest(question="有価証券の定義はどれか。", choices={"A": "a", "B": "b"})
    assert _needs_sibling_expansion(plain, []) is False
    evidence = [{"document": {"text": "同号に掲げる書類を除く。"}}]
    assert _needs_sibling_expansion(plain, evidence) is True


def test_baseline_uses_one_search_without_graph(monkeypatch):
    from app import agent as agent_module

    monkeypatch.setattr(agent_module.settings, "agent_use_llm_planner", True)
    os_client = FakeOpenSearch()
    service = AgentService(os_client, FakeGraph(), FakeLLM())
    request = AnswerRequest(
        question="検証法の要件は何か。",
        pattern="pattern_1_baseline_rag",
        topK=1,
    )

    response = service.answer(request)

    assert response.trace["toolCallCount"] == 1
    assert response.trace["planner"]["used"] is False
    assert "query_decomposition" not in response.route
    assert "graph_search_tool" not in response.route
    assert response.graphPaths == []
