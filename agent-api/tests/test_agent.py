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
            questionPolarity="select_entailed",
            choiceAssessments={
                "A": {"verdict": "entailed", "citationIds": ["law-test-article-3"], "reason": "第三条に合致"},
                "B": {"verdict": "contradicted", "citationIds": ["law-test-article-2"], "reason": "条文と矛盾"},
            },
        )


class TypeSeparatedOpenSearch:
    def __init__(self):
        self.calls = []

    def search(self, query, doc_type, top_k, clearance, use_bm25=True, use_vector=True):
        self.calls.append((query, doc_type, top_k))
        if doc_type == "law":
            return [{"document": _document("law-test-article-1", "法律の根拠"), "score": 0.01}]
        if doc_type == "guideline":
            document = _document("guidance-test-page-1-chunk-1", "ガイドラインの根拠")
            document["docType"] = "guideline"
            return [{"document": document, "score": 0.02}]
        return []


def test_evidence_search_keeps_guideline_candidate_pool_separate(monkeypatch):
    from app import agent as agent_module

    monkeypatch.setattr(agent_module.settings, "agent_guidance_candidate_top_k", 7)
    os_client = TypeSeparatedOpenSearch()
    service = AgentService(os_client, FakeGraph(), FakeLLM())

    results, counts = service._search_evidence("ガイドラインの要件", 20, 2)

    assert counts == {"law": 1, "guideline": 1}
    assert [call[1] for call in os_client.calls] == ["law", "guideline"]
    assert os_client.calls[1][2] == 7
    assert [item["document"]["contentUnitId"] for item in results] == [
        "guidance-test-page-1-chunk-1",
        "law-test-article-1",
    ]


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
    assert response.trace["choiceEvidence"]["A"] == ["law-test-article-3"]
    assert response.citations[0].contentUnitId == "law-test-article-3"
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


def test_extract_article_suffixes_handles_arabic_kanji_and_branches():
    from app.agent import _extract_article_suffixes

    assert _extract_article_suffixes("金融商品取引法第185条の22により正しいものはどれか。") == ["185_22"]
    assert _extract_article_suffixes("第百八十五条の二十二の規定") == ["185_22"]
    assert _extract_article_suffixes("施行令第２条の１２に定める") == ["2_12"]
    assert _extract_article_suffixes("第8条と第25条を参照") == ["8", "25"]
    assert _extract_article_suffixes("存続期間は何年ですか") == []


def test_matched_law_ids_excludes_parent_only_inside_child_name():
    from app.agent import _matched_law_ids

    titles = {
        "law-a": "金融商品取引法",
        "law-b": "金融商品取引法施行令",
        "law-c": "借地借家法",
    }
    # 施行令のフル名しか出ていない場合、親法は名前の一部として現れただけなので除外する
    assert _matched_law_ids("金融商品取引法施行令第2条の12に定める取得勧誘", titles) == ["law-b"]
    # 親法単独でも言及されていれば両方対象
    both = _matched_law_ids("金融商品取引法第24条及び金融商品取引法施行令第3条", titles)
    assert set(both) == {"law-a", "law-b"}
    assert _matched_law_ids("無関係な質問", titles) == []


def test_law_article_references_keeps_law_and_article_pairs():
    from app.agent import _law_article_references

    titles = {
        "law-act": "金融商品取引法",
        "law-order": "金融商品取引法施行令",
    }
    text = "金融商品取引法第24条及び金融商品取引法施行令第3条による。"

    assert _law_article_references(text, titles) == {
        "law-act": ["24"],
        "law-order": ["3"],
    }


def test_pin_ranked_evidence_keeps_direct_reference():
    from app.agent import _pin_ranked_evidence

    ordinary = {"document": _document("law-test-article-1", "類似条文"), "score": 2.0}
    pinned = {
        "document": _document("law-test-article-2", "明示された条文"),
        "score": 0.0,
        "mustInclude": True,
    }

    ranked = _pin_ranked_evidence([ordinary, pinned], 1)

    assert ranked[0]["document"]["contentUnitId"] == "law-test-article-2"


class ArticleRefOpenSearch:
    """条番号直接解決の検証用: 検索は無関係な条文しか返さないが、直接引きは正解条文を返す。"""

    def __init__(self):
        self.noise = _document("law-test-article-99", "無関係な条文。")
        self.target = _document("law-test-article-3", "第三条 存続期間は三十年とする。")
        self.target["articleContentUnitId"] = "law-test-article-3"

    def search(self, query, doc_type, top_k, clearance, use_bm25=True, use_vector=True):
        return [{"document": self.noise, "score": 1.0, "bm25Score": 2.0, "vectorScore": 0.8}]

    def law_titles(self):
        return {"law-test": "検証法"}

    def get_by_article_ids(self, article_ids, clearance, max_chunks=30):
        return [self.target] if "law-test-article-3" in article_ids else []

    def get_by_content_unit_ids(self, content_unit_ids, clearance):
        return []


def test_article_direct_lookup_injects_explicitly_referenced_article(monkeypatch):
    from app import agent as agent_module

    monkeypatch.setattr(agent_module.settings, "agent_use_llm_planner", False)
    service = AgentService(ArticleRefOpenSearch(), FakeGraph(), FakeLLM())
    request = AnswerRequest(
        question="検証法第3条により正しいものはどれか。",
        choices={"A": "存続期間は30年である。", "B": "存続期間は10年である。"},
        pattern="pattern_2_rule_based_agentic_rag",
        topK=2,
    )

    response = service.answer(request)

    assert "article_direct_lookup" in response.route
    cited = {citation.contentUnitId for citation in response.citations}
    assert "law-test-article-3" in cited
    lookup_rounds = [r for r in response.trace["rounds"] if r.get("tool") == "article_direct_lookup"]
    assert lookup_rounds and lookup_rounds[0]["references"] == {"law-test": ["3"]}
    assert lookup_rounds[0]["selectedCount"] == 1
    assert response.trace["fusionTopContentUnitIds"][0] == "law-test-article-3"


def _guideline_document(
    content_unit_id: str,
    text: str = "ガイドライン解説文。",
    document_id: str = "guidance-test",
) -> dict:
    document = _document(content_unit_id, text)
    document["docType"] = "guideline"
    document["documentId"] = document_id
    return document


class GuidanceExplainsGraph:
    """ガイドライン文書 -EXPLAINS-> 条文 のグラフ羅針盤を模す。
    documentId -> 条文IDリスト の対応で、EXPLAINSクエリにのみパスを返す。"""

    def __init__(self, articles_by_document: dict[str, list[str]]):
        self.articles_by_document = articles_by_document
        self.calls: list[tuple[list[str], str | None]] = []

    def paths_from_many(self, start_ids, edge_type=None, max_depth=2, limit=20, user_clearance_level=2):
        self.calls.append((list(start_ids), edge_type))
        if edge_type != "EXPLAINS":
            return []
        paths = []
        for document_id in start_ids:
            for article_id in self.articles_by_document.get(document_id, []):
                paths.append(
                    {
                        "nodes": [
                            {"graphNodeId": document_id, "documentId": document_id},
                            {"graphNodeId": article_id, "contentUnitId": article_id},
                        ],
                        "edges": [
                            {"graphEdgeId": f"edge-{document_id}-explains-{article_id}", "edgeType": "EXPLAINS"}
                        ],
                    }
                )
        return paths


class GuidanceExplainsOpenSearch:
    """ガイドライン解説からの条文救済の検証用: 検索はガイドラインチャンクのみ返し、
    法令本体条文は get_by_article_ids 経由でのみ取得できる。"""

    def __init__(self):
        self.guidance = _guideline_document("guidance-test-page-1-chunk-1")
        self.target = _document("law-test-article-18_2", "薬機法第十八条の二 体制を整備すること。")
        self.target["articleContentUnitId"] = "law-test-article-18_2"
        self.get_by_article_ids_calls = []

    def search(self, query, doc_type, top_k, clearance, use_bm25=True, use_vector=True):
        return [{"document": self.guidance, "score": 1.0, "bm25Score": 2.0, "vectorScore": 0.8}]

    def law_titles(self):
        return {"law-test": "検証法"}

    def get_by_article_ids(self, article_ids, clearance, max_chunks=30):
        self.get_by_article_ids_calls.append(list(article_ids))
        return [self.target] if "law-test-article-18_2" in article_ids else []

    def get_by_content_unit_ids(self, content_unit_ids, clearance):
        return []


def _guidance_graph() -> GuidanceExplainsGraph:
    return GuidanceExplainsGraph({"guidance-test": ["law-test-article-18_2"]})


def test_guidance_explains_lookup_injects_related_article(monkeypatch):
    from app import agent as agent_module

    monkeypatch.setattr(agent_module.settings, "agent_use_llm_planner", False)
    service = AgentService(GuidanceExplainsOpenSearch(), _guidance_graph(), FakeLLM())
    request = AnswerRequest(
        question="役職員が遵守すべき法令遵守体制として正しいものはどれか。",
        choices={"A": "体制を整備している。", "B": "特に何もしていない。"},
        pattern="pattern_2_rule_based_agentic_rag",
        topK=2,
    )

    response = service.answer(request)

    assert "guidance_explains_lookup" in response.route
    cited = {citation.contentUnitId for citation in response.citations}
    assert "law-test-article-18_2" in cited
    lookup_rounds = [r for r in response.trace["rounds"] if r.get("tool") == "guidance_explains_lookup"]
    assert lookup_rounds and lookup_rounds[0]["articleIds"] == ["law-test-article-18_2"]
    assert lookup_rounds[0]["guidanceDocumentIds"] == ["guidance-test"]
    assert lookup_rounds[0]["selectedCount"] == 1
    assert lookup_rounds[0]["newContentUnitCount"] == 1


def test_inject_guidance_explained_articles_noop_without_guideline_evidence():
    os_client = GuidanceExplainsOpenSearch()
    graph = _guidance_graph()
    service = AgentService(os_client, graph, FakeLLM())
    request = AnswerRequest(
        question="無関係な質問",
        choices={"A": "選択肢A", "B": "選択肢B"},
        pattern="pattern_2_rule_based_agentic_rag",
        topK=2,
    )
    law_only_document = _document("law-test-article-1", "第一条 通常の条文。")
    evidence = {"law-test-article-1": {"document": law_only_document, "score": 1.0}}
    trace = {"rounds": []}

    result = service._inject_guidance_explained_articles(request, evidence, trace, rerank_top_k=10, graph_paths=[])

    assert result is None
    assert "law-test-article-18_2" not in evidence
    assert graph.calls == []  # ガイドラインが上位に無ければグラフも引かない
    assert os_client.get_by_article_ids_calls == []


def test_inject_guidance_explained_articles_ignores_guideline_beyond_rerank_top_k():
    os_client = GuidanceExplainsOpenSearch()
    service = AgentService(os_client, _guidance_graph(), FakeLLM())
    request = AnswerRequest(
        question="役職員が遵守すべき法令遵守体制として正しいものはどれか。",
        choices={"A": "体制を整備している。", "B": "特に何もしていない。"},
        pattern="pattern_2_rule_based_agentic_rag",
        topK=2,
    )
    evidence = {
        "law-test-article-99": {"document": _document("law-test-article-99", "無関係な条文。"), "score": 2.0},
        "guidance-test-page-1-chunk-1": {"document": os_client.guidance, "score": 1.0},
    }
    trace = {"rounds": []}

    result = service._inject_guidance_explained_articles(request, evidence, trace, rerank_top_k=1, graph_paths=[])

    assert result is None
    assert "law-test-article-18_2" not in evidence
    assert os_client.get_by_article_ids_calls == []


def test_inject_guidance_explained_articles_dedupes_articles_from_multiple_guideline_chunks():
    os_client = GuidanceExplainsOpenSearch()
    service = AgentService(os_client, _guidance_graph(), FakeLLM())
    request = AnswerRequest(
        question="役職員が遵守すべき法令遵守体制として正しいものはどれか。",
        choices={"A": "体制を整備している。", "B": "特に何もしていない。"},
        pattern="pattern_2_rule_based_agentic_rag",
        topK=2,
    )
    # 同一ガイドライン文書の2チャンクが上位に入っても、EXPLAINSは文書単位なので条文は1件。
    second_guideline = _guideline_document("guidance-test-page-2-chunk-1")
    evidence = {
        "guidance-test-page-1-chunk-1": {"document": os_client.guidance, "score": 2.0},
        "guidance-test-page-2-chunk-1": {"document": second_guideline, "score": 1.0},
    }
    trace = {"rounds": []}
    graph_paths: list = []

    result = service._inject_guidance_explained_articles(
        request, evidence, trace, rerank_top_k=10, graph_paths=graph_paths
    )

    assert result == 1
    assert os_client.get_by_article_ids_calls == [["law-test-article-18_2"]]
    assert evidence["law-test-article-18_2"]["mustInclude"] is True
    assert graph_paths  # EXPLAINSパスがtrace用に記録される


def test_inject_guidance_explained_articles_caps_article_count():
    from app import agent as agent_module

    os_client = GuidanceExplainsOpenSearch()
    many_articles = [f"law-test-article-{n}" for n in range(1, 12)]
    graph = GuidanceExplainsGraph({"guidance-test": many_articles})
    service = AgentService(os_client, graph, FakeLLM())
    request = AnswerRequest(
        question="役職員が遵守すべき法令遵守体制として正しいものはどれか。",
        choices={"A": "体制を整備している。", "B": "特に何もしていない。"},
        pattern="pattern_2_rule_based_agentic_rag",
        topK=2,
    )
    evidence = {"guidance-test-page-1-chunk-1": {"document": os_client.guidance, "score": 1.0}}
    trace = {"rounds": []}

    service._inject_guidance_explained_articles(request, evidence, trace, rerank_top_k=10, graph_paths=[])

    requested = os_client.get_by_article_ids_calls[0]
    assert len(requested) == agent_module.GUIDANCE_EXPLAINS_MAX_ARTICLES
    assert requested == many_articles[: agent_module.GUIDANCE_EXPLAINS_MAX_ARTICLES]
