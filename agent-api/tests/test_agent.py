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

    def generate_answer(
        self,
        request,
        route,
        citations,
        timeout_sec=None,
        evidence_by_choice=None,
        answer_scope=None,
    ):
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


class NamedLawCoverageOpenSearch(TypeSeparatedOpenSearch):
    def __init__(self):
        super().__init__()
        self.scoped_calls = []

    def law_titles(self):
        return {
            "law-civil": "民法",
            "law-land-lease": "借地借家法",
        }

    def search_by_document_id(self, query, document_id, top_k, clearance):
        self.scoped_calls.append(document_id)
        article = "604" if document_id == "law-civil" else "3"
        title = "民法" if document_id == "law-civil" else "借地借家法"
        document = _document(f"{document_id}-article-{article}", "存続期間の定め")
        document["documentId"] = document_id
        document["title"] = title
        return [{"document": document, "score": 0.02}]


def test_named_law_query_keeps_one_candidate_from_that_law(monkeypatch):
    from app import agent as agent_module

    monkeypatch.setattr(agent_module.settings, "agent_use_bm25", True)
    service = AgentService(NamedLawCoverageOpenSearch(), FakeGraph(), FakeLLM())

    results, counts = service._search_evidence("民法 賃貸借の存続期間 上限", 20, 2)

    assert counts["named_law"] == 1
    assert any(item["document"]["documentId"] == "law-civil" for item in results)


def test_named_law_candidates_are_not_pinned_into_the_final_ranking(monkeypatch):
    """法令名を書いただけの候補を必須根拠にすると、無関係な条文が上位枠を奪う。

    mustInclude は明示された条番号を直接取得できた場合だけに限る。
    """
    from app import agent as agent_module
    from app.agent import _merge_search_results

    monkeypatch.setattr(agent_module.settings, "agent_use_bm25", True)
    service = AgentService(NamedLawCoverageOpenSearch(), FakeGraph(), FakeLLM())

    results, _ = service._search_evidence("民法 賃貸借の存続期間 上限", 20, 2)
    evidence = {}
    _merge_search_results(evidence, results, "民法 賃貸借の存続期間 上限", "initial_search")

    assert not evidence["law-civil-article-604"].get("mustInclude")
    assert evidence["law-civil-article-604"]["queryRanks"][
        "民法 賃貸借の存続期間 上限"
    ] >= 1


def test_named_law_search_covers_every_law_named_in_the_query(monkeypatch):
    """複数の法令名を挙げた質問でも、各法令から候補を確保する。"""
    from app import agent as agent_module

    monkeypatch.setattr(agent_module.settings, "agent_use_bm25", True)
    os_client = NamedLawCoverageOpenSearch()
    service = AgentService(os_client, FakeGraph(), FakeLLM())

    service._search_evidence("民法と借地借家法の契約期間の違い", 20, 2)

    assert sorted(os_client.scoped_calls) == ["law-civil", "law-land-lease"]


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


class ImplementationGraph:
    def __init__(self):
        self.calls = []

    def paths_from_many(self, start_ids, edge_type=None, max_depth=2, limit=20, user_clearance_level=2):
        self.calls.append(edge_type)
        if edge_type != "IMPLEMENTS" or "law-test-article-5" not in start_ids:
            return []
        return [
            {
                "nodes": [
                    {"graphNodeId": "law-test-article-5", "contentUnitId": "law-test-article-5"},
                    {"graphNodeId": "law-order-article-2_13", "contentUnitId": "law-order-article-2_13"},
                ],
                "edges": [
                    {
                        "graphEdgeId": "edge-law-test-article-5-implements-law-order-article-2_13",
                        "edgeType": "IMPLEMENTS",
                    }
                ],
            }
        ]


class ImplementationOpenSearch:
    def __init__(self):
        self.parent = _document("law-test-article-5", "政令で定める有価証券を除く。")
        self.child = _document("law-order-article-2_13", "法第五条に規定する有価証券を定める。")
        self.child["documentId"] = "law-order"

    def search(self, query, doc_type, top_k, clearance, use_bm25=True, use_vector=True):
        return (
            [{"document": self.parent, "score": 1.0, "bm25Score": 2.0, "vectorScore": 0.8}]
            if doc_type == "law"
            else []
        )

    def get_by_content_unit_ids(self, content_unit_ids, clearance):
        return [self.child] if self.child["contentUnitId"] in content_unit_ids else []


def test_graph_expansion_traverses_reverse_implementation_relation(monkeypatch):
    from app import agent as agent_module

    monkeypatch.setattr(agent_module.settings, "agent_use_llm_planner", False)
    graph = ImplementationGraph()
    service = AgentService(ImplementationOpenSearch(), graph, FakeLLM())
    request = AnswerRequest(
        question="除外される有価証券として正しいものはどれか。",
        choices={"A": "政令で定める有価証券", "B": "全ての有価証券"},
        pattern="pattern_2_rule_based_agentic_rag",
        topK=2,
    )

    response = service.answer(request)

    assert "IMPLEMENTS" in graph.calls
    assert "law-order-article-2_13" in {citation.contentUnitId for citation in response.citations}
    assert response.trace["retrievedGraphEdgeIds"] == [
        "edge-law-test-article-5-implements-law-order-article-2_13"
    ]


def test_graph_expansion_pins_paragraph_returned_for_trusted_article_target():
    class ParagraphTargetOpenSearch(ImplementationOpenSearch):
        def __init__(self):
            super().__init__()
            self.child["contentUnitId"] = "law-order-article-2_13-paragraph-1"
            self.child["articleContentUnitId"] = "law-order-article-2_13"

        def get_by_content_unit_ids(self, content_unit_ids, clearance):
            return [self.child] if "law-order-article-2_13" in content_unit_ids else []

    service = AgentService(ParagraphTargetOpenSearch(), ImplementationGraph(), FakeLLM())
    request = AnswerRequest(
        question="政令で定める対象はどれか。",
        choices={"A": "対象", "B": "対象外"},
        pattern="pattern_2_rule_based_agentic_rag",
        topK=2,
    )
    parent = _document("law-test-article-5", "政令で定める。")
    evidence = {
        parent["contentUnitId"]: {
            "document": parent,
            "score": 1.0,
            "sources": ["search"],
            "queries": [],
        }
    }

    _, new_count = service._expand_graph(request, evidence, rerank_top_k=2, candidate_top_k=10)

    child = evidence["law-order-article-2_13-paragraph-1"]
    assert new_count == 1
    assert child["mustInclude"] is True


def test_fair_graph_paths_does_not_let_first_start_node_consume_global_limit():
    class PerStartGraph:
        def paths_from_many(
            self,
            start_ids,
            edge_type=None,
            max_depth=2,
            limit=20,
            user_clearance_level=2,
        ):
            start_id = start_ids[0]
            return [
                {
                    "nodes": [
                        {"graphNodeId": start_id, "contentUnitId": start_id},
                        {
                            "graphNodeId": f"{start_id}-target-{index}",
                            "contentUnitId": f"{start_id}-target-{index}",
                        },
                    ],
                    "edges": [
                        {
                            "graphEdgeId": f"edge-{start_id}-{index}",
                            "edgeType": edge_type,
                        }
                    ],
                }
                for index in range(2)
            ]

    service = AgentService(ImplementationOpenSearch(), PerStartGraph(), FakeLLM())

    paths = service._fair_graph_paths(["first", "second"], "REFERENCES", 2)

    targets = [path["nodes"][-1]["contentUnitId"] for path in paths]
    assert "first-target-0" in targets
    assert "second-target-0" in targets


def test_fair_graph_paths_keeps_targets_from_different_subordinate_laws():
    class SameStartGraph:
        def paths_from_many(
            self,
            start_ids,
            edge_type=None,
            max_depth=2,
            limit=20,
            user_clearance_level=2,
        ):
            start_id = start_ids[0]
            targets = [
                ("order-1", "law-order"),
                ("order-2", "law-order"),
                ("ordinance-10", "law-ordinance"),
            ]
            return [
                {
                    "nodes": [
                        {
                            "graphNodeId": start_id,
                            "contentUnitId": start_id,
                            "documentId": "law-parent",
                        },
                        {
                            "graphNodeId": target_id,
                            "contentUnitId": target_id,
                            "documentId": document_id,
                        },
                    ],
                    "edges": [
                        {
                            "graphEdgeId": f"edge-{start_id}-{target_id}",
                            "edgeType": edge_type,
                        }
                    ],
                }
                for target_id, document_id in targets
            ][:limit]

    service = AgentService(ImplementationOpenSearch(), SameStartGraph(), FakeLLM())

    paths = service._fair_graph_paths(["parent"], "IMPLEMENTS", 2)

    assert [path["nodes"][-1]["contentUnitId"] for path in paths] == [
        "order-1",
        "ordinance-10",
    ]


def test_diverse_graph_paths_prefers_target_heading_relevant_to_question():
    from app.agent import _diverse_target_document_paths

    paths = [
        {
            "nodes": [
                {"graphNodeId": "parent"},
                {
                    "graphNodeId": "order-8",
                    "documentId": "law-order",
                    "heading": "買付け等の期間",
                },
            ]
        },
        {
            "nodes": [
                {"graphNodeId": "parent"},
                {
                    "graphNodeId": "disclosure-19",
                    "documentId": "law-disclosure",
                    "heading": "届出書の様式",
                },
            ]
        },
        {
            "nodes": [
                {"graphNodeId": "parent"},
                {
                    "graphNodeId": "ordinance-10",
                    "documentId": "law-tob-ordinance",
                    "heading": "公開買付開始公告",
                },
            ]
        },
    ]

    selected = _diverse_target_document_paths(
        paths,
        2,
        query_text="公開買付開始公告と公開買付届出書の手続",
    )

    assert selected[0]["nodes"][-1]["graphNodeId"] == "ordinance-10"


def test_trusted_graph_documents_use_one_chunk_per_article_and_relation_slots():
    from app.agent import _select_trusted_graph_documents

    documents = [
        {
            **_document("law-parent-article-2-paragraph-4", "一般的な定義"),
            "articleContentUnitId": "law-parent-article-2",
        },
        {
            **_document("law-parent-article-2-paragraph-24", "別の一般的な定義"),
            "articleContentUnitId": "law-parent-article-2",
        },
        {
            **_document(
                "law-rule-article-116-paragraph-1-item-2",
                "外国為替リスクを減殺するための勧誘",
            ),
            "articleContentUnitId": "law-rule-article-116",
        },
    ]
    selected = _select_trusted_graph_documents(
        documents,
        {
            "law-parent-article-2": {"REFERENCES"},
            "law-rule-article-116": {"IMPLEMENTS"},
        },
        "外国為替リスクを減殺するための勧誘",
        limit=4,
    )

    assert "law-rule-article-116-paragraph-1-item-2" in selected
    assert len([item for item in selected if "law-parent-article-2-" in item]) == 1
    assert selected["law-rule-article-116-paragraph-1-item-2"] == {"IMPLEMENTS"}


def test_trusted_graph_documents_reserve_targets_from_different_laws():
    from app.agent import _select_trusted_graph_documents

    documents = []
    relations = {}
    for index, document_id in enumerate(
        ["law-order", "law-order", "law-disclosure-ordinance", "law-tob-ordinance"],
        start=1,
    ):
        article_id = f"{document_id}-article-{index}"
        document = _document(
            f"{article_id}-paragraph-1",
            "公開買付け又は届出の具体的な手続",
        )
        document["documentId"] = document_id
        document["articleContentUnitId"] = article_id
        documents.append(document)
        relations[article_id] = {"IMPLEMENTS"}

    selected = _select_trusted_graph_documents(
        documents,
        relations,
        "公開買付けの届出手続",
        limit=3,
    )

    selected_document_ids = {
        next(
            document["documentId"]
            for document in documents
            if document["contentUnitId"] == content_unit_id
        )
        for content_unit_id in selected
    }
    assert selected_document_ids == {
        "law-order",
        "law-disclosure-ordinance",
        "law-tob-ordinance",
    }


def test_trusted_graph_document_is_not_cut_off_before_evidence_merge():
    from app.agent import _prioritize_trusted_graph_documents

    documents = [
        {
            **_document(f"law-noise-article-{index}", "一般的な関連条文"),
            "documentId": "law-noise",
        }
        for index in range(25)
    ]
    trusted = {
        **_document("law-ordinance-article-10", "公開買付開始公告の掲載事項"),
        "documentId": "law-ordinance",
    }
    documents.append(trusted)

    ranked = _prioritize_trusted_graph_documents(
        documents,
        {"law-ordinance-article-10"},
        "公開買付けに必要な手続",
    )

    assert ranked[0]["contentUnitId"] == "law-ordinance-article-10"
    assert trusted in ranked[:20]


def test_graph_closure_prefers_implements_over_higher_ranked_parent_reference():
    from app.agent import _graph_closure_citation_ids

    ranked = [
        {
            "document": _document("law-parent-article-2", "定義"),
            "citationClosure": True,
            "graphRelationTypes": ["REFERENCES"],
        },
        {
            "document": _document("law-rule-article-116", "禁止行為の例外"),
            "citationClosure": True,
            "graphRelationTypes": ["IMPLEMENTS"],
        },
    ]

    assert _graph_closure_citation_ids(ranked, limit=1) == [
        "law-rule-article-116"
    ]


def test_graph_closure_prefers_target_reached_from_the_higher_ranked_source():
    """関係種別だけで選ぶと、無関係なIMPLEMENTSが直接根拠のREFERENCESを押し出す。"""
    from app.agent import _graph_closure_citation_ids

    ranked = [
        {
            "document": _document("law-rule-article-175_2", "販売業者の遵守体制"),
        },
        {
            "document": _document("law-parent-article-23", "別業態の体制"),
        },
        {
            "document": _document("law-parent-article-40", "準用"),
            "citationClosure": True,
            "graphRelationTypes": ["REFERENCES"],
            "graphSourceArticleIds": ["law-rule-article-175_2"],
        },
        {
            "document": _document("law-rule-article-114_53", "別業態の施行規則"),
            "citationClosure": True,
            "graphRelationTypes": ["IMPLEMENTS"],
            "graphSourceArticleIds": ["law-parent-article-23"],
        },
    ]

    assert _graph_closure_citation_ids(ranked, limit=1) == [
        "law-parent-article-40"
    ]


def test_free_text_does_not_force_a_graph_source_into_final_citations():
    from app.agent import _graph_closure_citations_for_request
    from app.models import AnswerRequest

    item = {
        "document": _document("law-unrelated-article-36", "無関係な登録要件"),
        "citationClosure": True,
        "graphRelationTypes": ["REFERENCES"],
    }
    request = AnswerRequest(question="賃貸住宅を退去するときの条件は何ですか。")

    assert (
        _graph_closure_citations_for_request(
            request,
            [item],
            {"law-unrelated-article-36"},
            1,
        )
        == []
    )


def test_choice_graph_closure_requires_the_raw_reranker_top_set():
    from app.agent import _graph_closure_citations_for_request
    from app.models import AnswerRequest

    item = {
        "document": _document("law-rule-article-116", "禁止行為の例外"),
        "citationClosure": True,
        "graphRelationTypes": ["IMPLEMENTS"],
    }
    request = AnswerRequest(
        question="誤っているものはどれですか。",
        choices={"A": "選択肢A", "B": "選択肢B"},
    )

    assert _graph_closure_citations_for_request(request, [item], set(), 1) == []
    assert _graph_closure_citations_for_request(
        request,
        [item],
        {"law-rule-article-116"},
        1,
    ) == ["law-rule-article-116"]


def test_pin_ranked_evidence_keeps_existing_top_k_pins_and_replaces_only_non_pins():
    from app.agent import _pin_ranked_evidence

    ordered = []
    for index in range(1, 21):
        item = {
            "document": _document(f"law-test-article-{index}", f"第{index}条"),
            "score": 1 / index,
        }
        if index in {16, 18, 19}:
            item["mustInclude"] = True
        ordered.append(item)

    ranked = _pin_ranked_evidence(ordered, top_k=16)
    ranked_ids = {item["document"]["contentUnitId"] for item in ranked}

    assert "law-test-article-16" in ranked_ids
    assert "law-test-article-18" in ranked_ids
    assert "law-test-article-19" in ranked_ids
    assert len(ranked) == 16


class AppliedByGraph:
    def __init__(self):
        self.calls = []

    def paths_from_many(self, start_ids, edge_type=None, max_depth=2, limit=20, user_clearance_level=2):
        self.calls.append(edge_type)
        if edge_type != "APPLIED_BY" or "law-test-article-1" not in start_ids:
            return []
        return [
            {
                "nodes": [
                    {"graphNodeId": "law-test-article-1", "contentUnitId": "law-test-article-1"},
                    {"graphNodeId": "law-test-article-2", "contentUnitId": "law-test-article-2"},
                ],
                "edges": [
                    {
                        "graphEdgeId": "edge-law-test-article-1-applied-by-law-test-article-2",
                        "edgeType": "APPLIED_BY",
                    }
                ],
            }
        ]


class AppliedByOpenSearch:
    def __init__(self):
        self.target = _document("law-test-article-1", "用語の定義を定める。")
        self.source = _document("law-test-article-2", "第一条の定義を準用する。")

    def search(self, query, doc_type, top_k, clearance, use_bm25=True, use_vector=True):
        return (
            [{"document": self.target, "score": 1.0, "bm25Score": 2.0, "vectorScore": 0.8}]
            if doc_type == "law"
            else []
        )

    def get_by_content_unit_ids(self, content_unit_ids, clearance):
        return [self.source] if self.source["contentUnitId"] in content_unit_ids else []


def test_graph_expansion_traverses_reverse_incorporation_relation(monkeypatch):
    from app import agent as agent_module

    monkeypatch.setattr(agent_module.settings, "agent_use_llm_planner", False)
    graph = AppliedByGraph()
    service = AgentService(AppliedByOpenSearch(), graph, FakeLLM())
    request = AnswerRequest(
        question="検証法第1条の定義が準用される場面として正しいものはどれか。",
        choices={"A": "第2条の場面", "B": "準用される場面はない"},
        pattern="pattern_2_rule_based_agentic_rag",
        topK=2,
    )

    response = service.answer(request)

    assert "APPLIED_BY" in graph.calls
    assert "law-test-article-2" in {citation.contentUnitId for citation in response.citations}
    assert response.trace["retrievedGraphEdgeIds"] == [
        "edge-law-test-article-1-applied-by-law-test-article-2"
    ]


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


class ProvisionRefOpenSearch(ArticleRefOpenSearch):
    def __init__(self):
        super().__init__()
        self.exact = _document(
            "law-test-article-2-paragraph-1-item-1",
            "第1号 具体的な要件を定める。",
        )
        self.exact["articleContentUnitId"] = "law-test-article-2"
        self.exact["parentContentUnitId"] = "law-test-article-2-paragraph-1"
        self.content_lookup_calls = []

    def get_by_article_ids(self, article_ids, clearance, max_chunks=30):
        return []

    def get_by_content_unit_ids(self, content_unit_ids, clearance):
        self.content_lookup_calls.append(list(content_unit_ids))
        return [self.exact] if self.exact["contentUnitId"] in content_unit_ids else []


def test_provision_direct_lookup_resolves_explicit_paragraph_and_item(monkeypatch):
    from app import agent as agent_module
    from app.agent import _extract_provision_references

    monkeypatch.setattr(agent_module.settings, "agent_use_llm_planner", False)
    os_client = ProvisionRefOpenSearch()
    service = AgentService(os_client, FakeGraph(), FakeLLM())
    request = AnswerRequest(
        question="検証法第2条第1項第1号が定める要件はどれか。",
        choices={"A": "具体的な要件である。", "B": "要件は存在しない。"},
        pattern="pattern_2_rule_based_agentic_rag",
        topK=2,
    )

    response = service.answer(request)

    assert _extract_provision_references("第２条第１項第１号") == [
        {"articleSuffix": "2", "contentSuffix": "2-paragraph-1-item-1"}
    ]
    assert os_client.content_lookup_calls == [["law-test-article-2-paragraph-1-item-1"]]
    assert "law-test-article-2-paragraph-1-item-1" in {
        citation.contentUnitId for citation in response.citations
    }
    lookup = next(r for r in response.trace["rounds"] if r.get("tool") == "article_direct_lookup")
    assert lookup["contentUnitReferences"] == ["law-test-article-2-paragraph-1-item-1"]


def test_direct_lookup_keeps_all_explicit_items_beyond_normal_per_article_limit():
    from app.agent import _select_direct_documents

    request = AnswerRequest(
        question="検証法第2条第1項の各号を比較する。",
        choices={label: f"第{index}号" for index, label in enumerate("ABCD", start=1)},
        pattern="pattern_2_rule_based_agentic_rag",
        topK=2,
    )
    documents = []
    preferred = set()
    for index in range(1, 5):
        content_unit_id = f"law-test-article-2-paragraph-1-item-{index}"
        document = _document(content_unit_id, f"第{index}号の要件")
        document["articleContentUnitId"] = "law-test-article-2"
        documents.append(document)
        preferred.add(content_unit_id)

    selected = _select_direct_documents(
        request,
        documents,
        per_article=3,
        preferred_content_ids=preferred,
    )

    assert {item["contentUnitId"] for item in selected} == preferred


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


def _citation(content_unit_id: str):
    from app.models import Citation

    return Citation(
        documentId="law-test",
        contentUnitId=content_unit_id,
        title="検証法",
        heading=content_unit_id,
        text="本文",
        sourceObjectUri="minio://test/source.xml",
        sourcePage=None,
    )


def test_final_citations_include_every_article_the_answer_cited():
    """自由入力の回答は本文にcontentUnitIdを書くため、それらは必ず引用一覧へ載せる。

    載らないと、回答が挙げたIDが利用者にはどの条文か分からなくなる。
    """
    from app.agent import _select_final_citations

    candidates = [_citation(f"law-test-article-{n}") for n in range(1, 9)]
    answer = (
        "原則として負担させられません(law-test-article-7)。"
        "敷金の扱いも同様です(law-test-article-8)。"
    )

    citations = _select_final_citations(candidates, [], 3, answer_text=answer)

    content_unit_ids = [citation.contentUnitId for citation in citations]
    assert content_unit_ids[:2] == ["law-test-article-7", "law-test-article-8"]
    assert len(citations) == 3


def test_final_citations_keep_choice_assessment_priority():
    """選択式では従来どおり選択肢判定の根拠を優先し、上限も維持する。"""
    from app.agent import _select_final_citations

    candidates = [_citation(f"law-test-article-{n}") for n in range(1, 6)]

    citations = _select_final_citations(candidates, ["law-test-article-4"], 2, answer_text="A が正しい。")

    assert [citation.contentUnitId for citation in citations] == [
        "law-test-article-4",
        "law-test-article-1",
    ]


def test_final_citations_reserve_one_structural_graph_source_within_choice_cap():
    """LLMの引用指定が枠を埋めても、法令間の接続根拠を1件だけ残す。"""
    from app.agent import _select_final_citations

    candidates = [_citation(f"law-test-article-{n}") for n in range(1, 7)]

    citations = _select_final_citations(
        candidates,
        [f"law-test-article-{n}" for n in range(1, 6)],
        5,
        answer_text="Bが正しい。",
        expand_answer_citations=False,
        structural_citation_ids=["law-test-article-6"],
    )

    assert [citation.contentUnitId for citation in citations] == [
        "law-test-article-6",
        "law-test-article-1",
        "law-test-article-2",
        "law-test-article-3",
        "law-test-article-4",
    ]


def test_final_citations_ignore_ids_the_answer_invented():
    """回答が候補にないIDを書いても、引用一覧には出さない。"""
    from app.agent import _select_final_citations

    candidates = [_citation("law-test-article-1")]

    citations = _select_final_citations(
        candidates, [], 5, answer_text="law-test-article-999 を参照。"
    )

    assert [citation.contentUnitId for citation in citations] == ["law-test-article-1"]


def test_final_citations_resolve_article_level_mention_to_its_paragraph():
    """条文が項単位で投入されている場合、回答が条レベルのIDで引用することがある。

    そのままでは引用一覧に出ず、本文のIDが何を指すか分からなくなるため項の引用へ解決する。
    """
    from app.agent import _select_final_citations

    candidates = [
        _citation("law-test-article-3"),
        _citation("law-test-article-27_2-paragraph-1"),
        _citation("law-test-article-27_2-paragraph-2"),
    ]

    citations = _select_final_citations(
        candidates, [], 1, answer_text="公開買付けが必要です(law-test-article-27_2)。"
    )

    assert citations[0].contentUnitId == "law-test-article-27_2-paragraph-1"


def test_final_citations_do_not_resolve_prefix_of_a_different_article():
    """条番号の前方一致だけで拾うと、第2条が第27条を巻き込むため区切りまで一致させる。"""
    from app.agent import _select_final_citations

    candidates = [
        _citation("law-test-article-27-paragraph-1"),
        _citation("law-test-article-2-paragraph-1"),
    ]

    citations = _select_final_citations(
        candidates, [], 1, answer_text="law-test-article-2 を参照。"
    )

    assert [c.contentUnitId for c in citations] == ["law-test-article-2-paragraph-1"]


def test_article_level_mention_adds_only_one_paragraph_of_that_article():
    """条レベルの引用で同じ条の全項を積むと、引用一覧が膨れて読めなくなる。"""
    from app.agent import _select_final_citations

    candidates = [_citation(f"law-test-article-27_2-paragraph-{n}") for n in range(1, 9)]

    citations = _select_final_citations(
        candidates, [], 2, answer_text="law-test-article-27_2 を参照。"
    )

    assert [c.contentUnitId for c in citations] == [
        "law-test-article-27_2-paragraph-1",
        "law-test-article-27_2-paragraph-2",
    ]


def test_choice_questions_keep_the_citation_cap_exactly():
    """選択式は評価の採点対象。引用件数が増えるとcitationHitが甘くなるため上限を守る。"""
    from app.agent import _select_final_citations

    candidates = [_citation(f"law-test-article-{n}") for n in range(1, 9)]
    answer = "Aが正しい(law-test-article-6、law-test-article-7、law-test-article-8)。"

    citations = _select_final_citations(
        candidates,
        ["law-test-article-5"],
        2,
        answer_text=answer,
        expand_answer_citations=False,
    )

    assert [c.contentUnitId for c in citations] == ["law-test-article-5", "law-test-article-1"]


def test_choice_questions_cap_even_when_assessments_exceed_top_k():
    """選択肢判定の根拠が上限より多くても、選択式では上限で切る。

    変更前は選択後に一律で上限まで切っていた。ここを緩めると採点対象の
    citations が増え、citationHit が実力以上に当たりやすくなる。
    """
    from app.agent import _select_final_citations

    candidates = [_citation(f"law-test-article-{n}") for n in range(1, 9)]
    assessment_ids = [f"law-test-article-{n}" for n in range(1, 7)]

    citations = _select_final_citations(
        candidates,
        assessment_ids,
        5,
        answer_text="Aが正しい。",
        expand_answer_citations=False,
    )

    assert len(citations) == 5


def test_reviewed_free_text_citations_do_not_add_unused_candidates():
    from app.agent import _select_final_citations

    candidates = [_citation(f"law-test-article-{n}") for n in range(1, 6)]

    citations = _select_final_citations(
        candidates,
        ["law-test-article-4"],
        5,
        answer_text="第4条だけを使う。",
        expand_answer_citations=False,
        fill_remaining=False,
    )

    assert [citation.contentUnitId for citation in citations] == [
        "law-test-article-4"
    ]


def test_named_law_candidates_never_outrank_the_global_search(monkeypatch):
    """法令内検索のスコアは、その法令の中だけの相対値。

    通常検索のスコアと直接比較すると、法令名を書いただけで無関係な条文が上位に並ぶ。
    候補としては加えるが、順位は通常検索の後ろに置く。
    """
    from app import agent as agent_module

    monkeypatch.setattr(agent_module.settings, "agent_use_bm25", True)
    os_client = NamedLawCoverageOpenSearch()
    service = AgentService(os_client, FakeGraph(), FakeLLM())

    results, _ = service._search_evidence("民法 医薬品 広告", 20, 2)

    ids = [item["document"]["contentUnitId"] for item in results]
    global_ids = ["guidance-test-page-1-chunk-1", "law-test-article-1"]
    assert ids[: len(global_ids)] == global_ids
    assert "law-civil-article-604" in ids


def _evidence_item(content_unit_id: str, document_id: str, score: float) -> dict:
    document = _document(content_unit_id, "本文")
    document["documentId"] = document_id
    return {"document": document, "score": score}


def test_aspect_coverage_marks_one_search_top_candidate_per_focused_query():
    from app.agent import _mark_aspect_representatives

    evidence = {
        "law-lease-article-28": {
            **_evidence_item("law-lease-article-28", "law-lease", 0.9),
            "queryRanks": {"大家の更新拒絶": 1},
        },
        "law-civil-article-621": {
            **_evidence_item("law-civil-article-621", "law-civil", 0.2),
            "queryRanks": {"通常損耗の原状回復": 2},
        },
        "law-civil-article-999": {
            **_evidence_item("law-civil-article-999", "law-civil", 0.1),
            "queryRanks": {"通常損耗の原状回復": 8},
        },
    }

    selected = _mark_aspect_representatives(
        evidence,
        ["大家の更新拒絶", "通常損耗の原状回復"],
        limit=3,
        max_query_rank=5,
    )

    assert selected == ["law-lease-article-28", "law-civil-article-621"]
    assert evidence["law-civil-article-621"]["aspectInclude"] is True
    assert not evidence["law-civil-article-999"].get("aspectInclude")


def test_aspect_coverage_uses_query_specific_reranker_order():
    from app.agent import _mark_aspect_representatives

    query = "医薬品 製造販売業許可 基準 品質管理 安全管理"
    evidence = {
        "law-device-article-23_2": {
            **_evidence_item("law-device-article-23_2", "law-device", 0.9),
            "queryRanks": {query: 1},
        },
        "law-drug-article-12_2": {
            **_evidence_item("law-drug-article-12_2", "law-drug", 0.2),
            "queryRanks": {query: 5},
        },
    }

    selected = _mark_aspect_representatives(
        evidence,
        [query],
        limit=1,
        max_query_rank=12,
        query_orders={
            query: ["law-drug-article-12_2", "law-device-article-23_2"],
        },
    )

    assert selected == ["law-drug-article-12_2"]


def test_aspect_coverage_round_robins_multiple_articles_per_query():
    from app.agent import _mark_aspect_representatives

    first_query = "借地借家法 存続期間"
    second_query = "民法 賃貸借 存続期間"
    evidence = {
        content_unit_id: {
            **_evidence_item(content_unit_id, document_id, score),
            "queryRanks": query_ranks,
        }
        for content_unit_id, document_id, score, query_ranks in [
            ("law-lease-article-1", "law-lease", 0.9, {first_query: 1}),
            ("law-lease-article-23", "law-lease", 0.8, {first_query: 2}),
            ("law-lease-article-3", "law-lease", 0.7, {first_query: 3}),
            ("law-civil-article-601", "law-civil", 0.9, {second_query: 1}),
            ("law-civil-article-604", "law-civil", 0.8, {second_query: 2}),
            ("law-civil-article-605", "law-civil", 0.7, {second_query: 3}),
        ]
    }

    selected = _mark_aspect_representatives(
        evidence,
        [first_query, second_query],
        limit=5,
        max_query_rank=5,
        query_orders={
            first_query: [
                "law-lease-article-1",
                "law-lease-article-23",
                "law-lease-article-3",
            ],
            second_query: [
                "law-civil-article-601",
                "law-civil-article-604",
                "law-civil-article-605",
            ],
        },
        per_query=3,
    )

    assert selected == [
        "law-lease-article-1",
        "law-civil-article-601",
        "law-lease-article-23",
        "law-civil-article-604",
        "law-lease-article-3",
    ]


def test_follow_up_aspect_coverage_can_keep_multiple_units_from_one_article():
    from app.agent import _mark_aspect_representatives

    query = "金融商品取引法 第二十七条の二 株券等所有割合"
    evidence = {
        content_unit_id: {
            **_evidence_item(content_unit_id, "law-financial", score),
            "queryRanks": {query: rank},
        }
        for rank, (content_unit_id, score) in enumerate(
            [
                ("law-financial-article-27_2-paragraph-1-item-2", 0.9),
                ("law-financial-article-27_2-paragraph-1-item-1", 0.8),
            ],
            start=1,
        )
    }

    selected = _mark_aspect_representatives(
        evidence,
        [query],
        limit=2,
        max_query_rank=5,
        query_orders={
            query: [
                "law-financial-article-27_2-paragraph-1-item-2",
                "law-financial-article-27_2-paragraph-1-item-1",
            ]
        },
        per_query=2,
        dedupe_articles=False,
    )

    assert selected == [
        "law-financial-article-27_2-paragraph-1-item-2",
        "law-financial-article-27_2-paragraph-1-item-1",
    ]


def test_fusion_keeps_aspect_representative_for_a_lower_scoring_subquestion():
    from app.agent import _fusion_ranked_evidence

    evidence = {
        f"law-major-article-{n}": _evidence_item(
            f"law-major-article-{n}",
            "law-major",
            1.0 - n * 0.01,
        )
        for n in range(1, 10)
    }
    evidence["law-civil-article-621"] = _evidence_item(
        "law-civil-article-621",
        "law-civil",
        0.01,
    )
    evidence["law-civil-article-621"]["aspectInclude"] = True

    ranked = _fusion_ranked_evidence(evidence, 4)

    assert "law-civil-article-621" in {
        item["document"]["contentUnitId"] for item in ranked
    }


def test_fusion_reserves_a_slot_for_every_law_found():
    """1つの法令が上位を占有すると、横断に必要な他法令が再ランカーへ届かない。

    実測(2026-07-25)では、民法・公開買付府令が候補プールにあるのに
    再ランカー入力30件へ1件も入らなかった。関連性の判断は再ランカーに委ね、
    融合段階では各法令の代表を必ず渡す。
    """
    from app.agent import _fusion_ranked_evidence

    evidence = {
        f"law-major-article-{n}": _evidence_item(f"law-major-article-{n}", "law-major", 1.0 - n * 0.01)
        for n in range(1, 20)
    }
    evidence["law-minor-article-1"] = _evidence_item("law-minor-article-1", "law-minor", 0.05)

    ranked = _fusion_ranked_evidence(evidence, 8)

    document_ids = [item["document"]["documentId"] for item in ranked]
    assert "law-minor" in document_ids
    assert document_ids.count("law-major") == len(ranked) - 1


def test_fusion_keeps_score_order_when_only_one_law_matched():
    from app.agent import _fusion_ranked_evidence

    evidence = {
        f"law-major-article-{n}": _evidence_item(f"law-major-article-{n}", "law-major", 1.0 - n * 0.01)
        for n in range(1, 10)
    }

    ranked = _fusion_ranked_evidence(evidence, 3)

    assert [item["document"]["contentUnitId"] for item in ranked] == [
        "law-major-article-1",
        "law-major-article-2",
        "law-major-article-3",
    ]


def test_fusion_still_puts_explicit_article_lookups_first():
    from app.agent import _fusion_ranked_evidence

    evidence = {
        "law-major-article-1": _evidence_item("law-major-article-1", "law-major", 0.9),
        "law-major-article-2": _evidence_item("law-major-article-2", "law-major", 0.8),
        "law-minor-article-9": _evidence_item("law-minor-article-9", "law-minor", 0.01),
    }
    evidence["law-minor-article-9"]["mustInclude"] = True

    ranked = _fusion_ranked_evidence(evidence, 2)

    assert ranked[0]["document"]["contentUnitId"] == "law-minor-article-9"


def test_fusion_diversity_reserves_half_the_input_for_score_order():
    from app.agent import _fusion_ranked_evidence

    evidence = {
        f"law-score-article-{index}": _evidence_item(
            f"law-score-article-{index}",
            "law-score",
            1.0 - index * 0.01,
        )
        for index in range(1, 21)
    }
    for index in range(1, 13):
        content_unit_id = f"law-aspect-{index}-article-1"
        evidence[content_unit_id] = _evidence_item(
            content_unit_id,
            f"law-aspect-{index}",
            0.001,
        )
        evidence[content_unit_id]["aspectInclude"] = True

    ranked = _fusion_ranked_evidence(evidence, 30)
    selected_ids = {
        item["document"]["contentUnitId"]
        for item in ranked
    }

    assert len(ranked) == 30
    assert "law-score-article-1" in selected_ids
    assert "law-score-article-15" in selected_ids


def test_final_ranking_keeps_one_article_per_law_without_displacing_the_head():
    """各法令の代表はLLMへ渡す枠に残すが、上位は再ランク順のまま保つ。

    実測(2026-07-25)では、代表を先頭へ入れた結果、選択式の引用枠から必要な条文
    (金商業等府令73条・開示府令)が押し出され、正答率が0.95から0.85へ落ちた。
    引用は上位から取られるため、代表は上位の後ろへ差し込む。
    """
    from app.agent import _pin_ranked_evidence

    ordered = [
        _evidence_item(f"law-lease-article-{n}", "law-lease", 1.0 - n * 0.01) for n in range(1, 17)
    ]
    ordered.append(_evidence_item("law-civil-article-604", "law-civil", 0.1))

    ranked = _pin_ranked_evidence(ordered, 16)

    document_ids = [item["document"]["documentId"] for item in ranked]
    assert "law-civil" in document_ids
    assert document_ids[:8] == ["law-lease"] * 8, "上位は再ランク順を崩さない"
    assert document_ids.index("law-civil") == 15


def test_final_ranking_keeps_reranker_order_within_a_law():
    from app.agent import _pin_ranked_evidence

    ordered = [
        _evidence_item(f"law-lease-article-{n}", "law-lease", 1.0 - n * 0.01) for n in range(1, 6)
    ]

    ranked = _pin_ranked_evidence(ordered, 3)

    assert [item["document"]["contentUnitId"] for item in ranked] == [
        "law-lease-article-1",
        "law-lease-article-2",
        "law-lease-article-3",
    ]


def test_final_ranking_limits_aspect_rescues_after_reranking():
    from app.agent import _pin_ranked_evidence

    ordered = [
        _evidence_item(f"law-main-article-{index}", "law-main", 1.0 - index * 0.01)
        for index in range(1, 21)
    ]
    for item in ordered[16:]:
        item["aspectInclude"] = True

    ranked = _pin_ranked_evidence(ordered, 16)
    selected_ids = {
        item["document"]["contentUnitId"]
        for item in ranked
    }

    assert "law-main-article-17" in selected_ids
    assert "law-main-article-18" in selected_ids
    assert "law-main-article-19" not in selected_ids
    assert "law-main-article-14" in selected_ids


def _guidance_item(content_unit_id: str, document_id: str, score: float) -> dict:
    item = _evidence_item(content_unit_id, document_id, score)
    item["document"]["docType"] = "guideline"
    return item


def test_final_ranking_does_not_promote_low_ranked_guidance():
    """代表確保は法令横断のための仕組み。関連の薄いガイドラインPDFまで前に出さない。

    実測(2026-07-25)では、土地賃貸借の質問に監督指針・原状回復ガイドラインが
    引用へ混入した。ガイドラインは再ランクのスコア順でのみ採用する。
    """
    from app.agent import _pin_ranked_evidence

    ordered = [
        _evidence_item(f"law-lease-article-{n}", "law-lease", 1.0 - n * 0.01) for n in range(1, 17)
    ]
    ordered.append(_guidance_item("guidance-unrelated-page-1-chunk-1", "guidance-unrelated", 0.2))
    ordered.append(_evidence_item("law-civil-article-604", "law-civil", 0.1))

    ranked = _pin_ranked_evidence(ordered, 16)

    document_ids = [item["document"]["documentId"] for item in ranked]
    assert "law-civil" in document_ids
    assert "guidance-unrelated" not in document_ids


def test_reranker_fallback_uses_score_order_without_promoting_guidance():
    """再ランカー入力用の資料多様化を、フォールバック順位へ流用しない。

    融合入力ではガイドライン代表を拾っても、再ランカー失敗時の最終候補は
    多様化前の融合スコア順へ戻す。
    """
    from app.agent import _fusion_ranked_evidence, _score_ranked_evidence

    evidence = {
        f"law-lease-article-{n}": _evidence_item(
            f"law-lease-article-{n}", "law-lease", 1.0 - n * 0.01
        )
        for n in range(1, 17)
    }
    evidence["guidance-unrelated"] = _guidance_item(
        "guidance-unrelated-page-1-chunk-1", "guidance-unrelated", 0.2
    )
    evidence["law-civil-article-604"] = _evidence_item(
        "law-civil-article-604", "law-civil", 0.1
    )

    reranker_input = _fusion_ranked_evidence(evidence, 18)
    fallback = _score_ranked_evidence(evidence, 8)

    assert reranker_input[1]["document"]["documentId"] == "guidance-unrelated"
    assert "guidance-unrelated" not in {
        item["document"]["documentId"] for item in fallback
    }


def test_final_ranking_does_not_promote_a_law_far_below_the_cutoff():
    """別法令という理由だけで、再ランカー下位の無関係法令を昇格させない。"""
    from app.agent import _pin_ranked_evidence

    ordered = [
        _evidence_item(f"law-main-article-{n}", "law-main", 1.0 - n * 0.01)
        for n in range(1, 21)
    ]
    ordered.append(_evidence_item("law-unrelated-article-1", "law-unrelated", 0.01))

    ranked = _pin_ranked_evidence(ordered, 16)

    assert "law-unrelated" not in {
        item["document"]["documentId"] for item in ranked
    }


def test_final_ranking_does_not_promote_a_graph_target_far_below_the_cutoff():
    from app.agent import _pin_ranked_evidence

    ordered = [
        _evidence_item(f"law-main-article-{n}", "law-main", 1.0 - n * 0.01)
        for n in range(1, 24)
    ]
    graph_target = ordered[-1]
    graph_target["mustInclude"] = True
    graph_target["citationClosure"] = True

    ranked = _pin_ranked_evidence(ordered, 16)

    assert graph_target["document"]["contentUnitId"] not in {
        item["document"]["contentUnitId"] for item in ranked
    }


def test_final_ranking_can_rescue_graph_target_reached_from_an_aspect():
    from app.agent import _pin_ranked_evidence

    ordered = [
        _evidence_item(f"law-main-article-{n}", "law-main", 1.0 - n * 0.01)
        for n in range(1, 25)
    ]
    graph_target = ordered[-1]
    graph_target["mustInclude"] = True
    graph_target["citationClosure"] = True
    graph_target["graphFromAspect"] = True

    ranked = _pin_ranked_evidence(ordered, 16)

    assert graph_target["document"]["contentUnitId"] in {
        item["document"]["contentUnitId"] for item in ranked
    }


def test_final_ranking_prioritizes_aspect_graph_target_within_shared_rescue_cap():
    from app.agent import _pin_ranked_evidence

    ordered = [
        _evidence_item(f"law-main-article-{n}", "law-main", 1.0 - n * 0.01)
        for n in range(1, 25)
    ]
    ordered[16]["aspectInclude"] = True
    ordered[17]["aspectInclude"] = True
    graph_target = ordered[-1]
    graph_target["mustInclude"] = True
    graph_target["citationClosure"] = True
    graph_target["graphFromAspect"] = True

    ranked = _pin_ranked_evidence(ordered, 16)
    selected_ids = {
        item["document"]["contentUnitId"]
        for item in ranked
    }

    assert graph_target["document"]["contentUnitId"] in selected_ids
    assert "law-main-article-17" in selected_ids
    assert "law-main-article-18" not in selected_ids
    assert "law-main-article-14" in selected_ids


def test_law_title_matching_supports_common_abbreviations():
    from app.agent import _matched_law_ids

    titles = {
        "law-fiea": "金融商品取引法",
        "law-fiea-order": "金融商品取引法施行令",
        "law-pmd": "医薬品、医療機器等の品質、有効性及び安全性の確保等に関する法律",
        "law-pmd-rule": "医薬品、医療機器等の品質、有効性及び安全性の確保等に関する法律施行規則",
    }

    assert _matched_law_ids("金商法の届出要件", titles) == ["law-fiea"]
    assert _matched_law_ids("薬機法施行規則の申請書類", titles) == ["law-pmd-rule"]


def test_law_title_matching_keeps_parent_and_child_when_both_are_named():
    from app.agent import _matched_law_ids

    titles = {
        "law-fiea": "金融商品取引法",
        "law-fiea-order": "金融商品取引法施行令",
    }

    assert _matched_law_ids("金商法と金商法施行令を確認する", titles) == [
        "law-fiea",
        "law-fiea-order",
    ]
