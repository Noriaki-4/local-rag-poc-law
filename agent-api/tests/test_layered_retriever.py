"""反復探索ループのテスト (計画書 §8, §9, §11.7, §16.3)。"""

from typing import Any

import pytest

from app.evidence_requirements import (
    RETRIEVAL_STATUS_RESOLVED,
    EvidenceRequirement,
    LegalIssue,
    ORIGIN_PLANNER,
)
from app.layered_context_assembler import ANSWER_STATUS_COMPLETE, ChunkCandidate, assemble_context
from app.layered_retriever import (
    STOP_REASON_TIME,
    LayeredRetriever,
    _RetrievalState,
    _requirement_query,
)
from app.layered_shadow import chunk_candidates
from app.legal_issue_planner import IssuePlan
from app.legal_ontology import (
    AUTHORITY_CABINET_OFFICE_ORDINANCE,
    AUTHORITY_CABINET_ORDER,
    IMPLEMENTS_CONFIDENCE_EXPLICIT_DELEGATION,
)
from app.retrieval_budget import BudgetTracker, TimeProfile
from app.reranker import RerankResult


class FakeOpenSearch:
    """Requirement別の検索結果を、指定Article・レイヤーに応じて返す。"""

    def __init__(
        self,
        results_by_layer: dict[Any, list[dict[str, Any]]] | None = None,
        results_by_article: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.results_by_layer = results_by_layer or {}
        self.results_by_article = results_by_article or {}
        self.calls: list[list[Any]] = []

    def search_requirement_specs(
        self,
        specs: list[Any],
        *,
        user_clearance_level: int,
        timeout_sec: float | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        self.calls.append(list(specs))
        results: dict[str, list[dict[str, Any]]] = {}
        for spec in specs:
            # 明示条文・Graph接続先は検索語ではなくIDで直接取得する(§9.1)。
            direct = [
                dict(self.results_by_article[article_id])
                for article_id in spec.article_ids
                if article_id in self.results_by_article
            ]
            results[spec.requirement_id] = direct or [
                dict(candidate)
                for candidate in self.results_by_layer.get(spec.authority_type, [])
            ]
        return results


class FakeGraph:
    def __init__(self, paths: list[dict[str, Any]] | None = None) -> None:
        self.paths = paths or []
        self.calls: list[dict[str, Any]] = []

    def paths_from_many(self, start_ids: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append({"startIds": list(start_ids), **kwargs})
        return self.paths


def _candidate(article_id: str, text: str, authority_type: str | None = None, score: float = 5.0) -> dict:
    return {
        "articleId": article_id,
        "documentId": article_id.split("-article-")[0],
        "requirementId": "",
        "score": score,
        "authorityType": authority_type,
        "authorityRank": 0,
        "heading": "見出し",
        "text": text,
        "chunks": [{"contentUnitId": article_id, "text": text}],
    }


def _plan(*issues: LegalIssue) -> IssuePlan:
    return IssuePlan(issues=tuple(issues), graph_potentially_required=True)


def _issue(issue_id: str, families: tuple[str, ...] = ("normative_rule",), **overrides: Any) -> LegalIssue:
    values: dict[str, Any] = {
        "issue_id": issue_id,
        "label": "公開買付けの適用要件",
        "question_span": "公開買付けの要件",
        "key_terms": ("公開買付け",),
        "requested_role_families": families,
        "confidence": 0.9,
        "source": ORIGIN_PLANNER,
    }
    values.update(overrides)
    return LegalIssue(**values)


@pytest.fixture(autouse=True)
def _local_law_registry(monkeypatch: pytest.MonkeyPatch):
    """テスト実行時は、コンテナ内パスではなくリポジトリのlaw_registry.jsonを読む。"""
    from pathlib import Path

    from app import law_family
    from app.config import settings

    monkeypatch.setattr(
        settings, "samples_dir", Path(__file__).resolve().parents[2] / "docs" / "requirements" / "samples"
    )
    law_family.clear_cache()
    yield
    law_family.clear_cache()


def _law_calls(os_client: FakeOpenSearch) -> list[list[Any]]:
    """法令レーンの検索呼び出しだけを取り出す(ガイドレーンは別レーン: §10)。"""
    return [
        call for call in os_client.calls if all(spec.doc_type == "law" for spec in call)
    ]


def _law_graph_calls(graph: FakeGraph) -> list[dict[str, Any]]:
    return [call for call in graph.calls if "IMPLEMENTS" in (call.get("edge_types") or [])]


def _tracker(budget_sec: float = 60.0) -> BudgetTracker:
    return BudgetTracker(
        profile=TimeProfile("test", 600, 200, 90), exploration_budget_sec=budget_sec
    )


class TestRequirementQuery:
    def test_query_uses_role_and_key_terms_not_whole_question(self) -> None:
        from app.evidence_requirements import EvidenceRequirement

        requirement = EvidenceRequirement(
            requirement_id="req-1",
            issue_id="issue-1",
            role_family="procedure",
            role_subtypes=("publication",),
            key_terms=("公開買付開始公告",),
            query_hint="公告の方法",
        )
        query = _requirement_query(requirement)
        assert "公開買付開始公告" in query
        assert "公告" in query
        assert len(query) <= 200


class TestRoundZero:
    def test_all_initial_issues_are_processed_before_expansion(self) -> None:
        """5〜8主論点のround 0を複数batchで全件処理してから子展開を始める (§8.2)。"""
        os_client = FakeOpenSearch(
            {None: [_candidate("law-a-article-1", "公開買付けの原則を定める。")]}
        )
        retriever = LayeredRetriever(os_client, FakeGraph(), None)
        issues = tuple(_issue(f"issue-{index}") for index in range(6))
        result = retriever.retrieve(_plan(*issues), tracker=_tracker())
        assert len(result.requirements) == 6
        assert all(
            requirement.retrieval_status == RETRIEVAL_STATUS_RESOLVED
            for requirement in result.requirements
        )
        # 論点6件を ACTIVE_ISSUE_BATCH_SIZE=4 で分けるため、round 0は2 batch。
        assert len(_law_calls(os_client)) == 2

    def test_requirements_are_batched_into_one_search_call(self) -> None:
        os_client = FakeOpenSearch({None: [_candidate("law-a-article-1", "原則。")]})
        retriever = LayeredRetriever(os_client, FakeGraph(), None)
        result = retriever.retrieve(
            _plan(_issue("issue-1", ("normative_rule", "qualification", "meaning_scope"))),
            tracker=_tracker(),
        )
        assert len(_law_calls(os_client)) == 1
        assert len(_law_calls(os_client)[0]) == 3
        assert len(result.requirements) == 3

    def test_no_candidate_marks_requirement_exhausted(self) -> None:
        retriever = LayeredRetriever(FakeOpenSearch({}), FakeGraph(), None)
        result = retriever.retrieve(_plan(_issue("issue-1")), tracker=_tracker())
        assert result.requirements[0].retrieval_status == "exhausted"
        assert result.requirements[0].unresolved_reason == "no_candidate_article"


class TestExpansionRounds:
    def test_act_to_cabinet_order_to_ordinance_chain(self) -> None:
        """法律→政令→府令の連鎖を、質問別の固定条番号なしで到達する (§16.3)。"""
        os_client = FakeOpenSearch(
            {
                None: [
                    _candidate(
                        "law-act-article-27_2",
                        "公開買付けについて政令で定めるものを除く。",
                    )
                ],
                AUTHORITY_CABINET_ORDER: [
                    _candidate(
                        "law-order-article-7",
                        "公開買付けについて内閣府令で定める事項とする。",
                        AUTHORITY_CABINET_ORDER,
                    )
                ],
                AUTHORITY_CABINET_OFFICE_ORDINANCE: [
                    _candidate(
                        "law-ordinance-article-10",
                        "公開買付けの公告事項は次のとおりとする。",
                        AUTHORITY_CABINET_OFFICE_ORDINANCE,
                    )
                ],
            }
        )
        retriever = LayeredRetriever(os_client, FakeGraph(), None)
        result = retriever.retrieve(_plan(_issue("issue-1")), tracker=_tracker())
        assert "law-act-article-27_2" in result.accepted_article_ids
        assert "law-order-article-7" in result.accepted_article_ids
        assert "law-ordinance-article-10" in result.accepted_article_ids
        assert result.expansion_rounds >= 2

    def test_children_do_not_run_in_the_same_round(self) -> None:
        os_client = FakeOpenSearch(
            {
                None: [_candidate("law-act-article-1", "公開買付けについて政令で定める。")],
                AUTHORITY_CABINET_ORDER: [
                    _candidate(
                        "law-order-article-1",
                        "公開買付けについて定める。",
                        AUTHORITY_CABINET_ORDER,
                    )
                ],
            }
        )
        retriever = LayeredRetriever(os_client, FakeGraph(), None)
        retriever.retrieve(_plan(_issue("issue-1")), tracker=_tracker())
        # round 0 で親、round 1 で子。1回の検索へ混ぜない。
        assert [len(call) for call in _law_calls(os_client)] == [1, 1]

    def test_law_only_question_creates_no_lower_layer(self) -> None:
        os_client = FakeOpenSearch(
            {None: [_candidate("law-a-article-1", "公開買付けの原則を定める。")]}
        )
        retriever = LayeredRetriever(os_client, FakeGraph(), None)
        result = retriever.retrieve(_plan(_issue("issue-1")), tracker=_tracker())
        assert len(result.requirements) == 1
        assert result.expansion_rounds == 0

    def test_duplicate_child_requirements_are_not_created_twice(self) -> None:
        os_client = FakeOpenSearch(
            {
                None: [
                    _candidate(
                        "law-a-article-1",
                        "公開買付けについて政令で定める。政令で定める場合とする。",
                    )
                ],
                AUTHORITY_CABINET_ORDER: [
                    _candidate(
                        "law-order-article-1",
                        "公開買付けについて定める。",
                        AUTHORITY_CABINET_ORDER,
                    )
                ],
            }
        )
        retriever = LayeredRetriever(os_client, FakeGraph(), None)
        result = retriever.retrieve(_plan(_issue("issue-1")), tracker=_tracker())
        cabinet_order = [
            requirement
            for requirement in result.requirements
            if requirement.authority_type == AUTHORITY_CABINET_ORDER
        ]
        assert len(cabinet_order) == 1


class TestGraphExpansion:
    def test_trusted_edges_create_children_and_are_traced(self) -> None:
        os_client = FakeOpenSearch(
            {
                None: [_candidate("law-a-article-1", "公開買付けの原則を定める。")],
                AUTHORITY_CABINET_ORDER: [
                    _candidate(
                        "law-b-article-7",
                        "公開買付けの詳細を定める。",
                        AUTHORITY_CABINET_ORDER,
                    )
                ],
            }
        )
        graph = FakeGraph(
            [
                {
                    "nodes": [],
                    "edges": [
                        {
                            "edgeType": "IMPLEMENTS",
                            "fromGraphNodeId": "law-a-article-1",
                            "toGraphNodeId": "law-b-article-7",
                            "relationSource": "subordinate_law_parent_reference",
                            "relationConfidence": IMPLEMENTS_CONFIDENCE_EXPLICIT_DELEGATION,
                            "derivedFromEdgeId": "edge-ref",
                            "delegationWordingDetected": True,
                        }
                    ],
                }
            ]
        )
        os_client.results_by_article["law-b-article-7"] = _candidate(
            "law-b-article-7", "公開買付けの詳細を定める。", AUTHORITY_CABINET_ORDER
        )
        retriever = LayeredRetriever(os_client, graph, None)
        result = retriever.retrieve(_plan(_issue("issue-1")), tracker=_tracker())
        law_graph_call = _law_graph_calls(graph)[0]
        assert law_graph_call["max_depth"] == 1
        assert "IMPLEMENTS" in law_graph_call["edge_types"]
        assert result.trace["graphEdgesAccepted"]
        assert "law-b-article-7" in result.accepted_article_ids

    def test_low_confidence_edges_are_rejected_in_trace(self) -> None:
        os_client = FakeOpenSearch(
            {None: [_candidate("law-a-article-1", "公開買付けの原則を定める。")]}
        )
        graph = FakeGraph(
            [
                {
                    "edges": [
                        {
                            "edgeType": "IMPLEMENTS",
                            "fromGraphNodeId": "law-a-article-1",
                            "toGraphNodeId": "law-b-article-7",
                            "relationSource": "subordinate_law_parent_reference",
                            "relationConfidence": 0.7,
                        }
                    ]
                }
            ]
        )
        retriever = LayeredRetriever(os_client, graph, None)
        result = retriever.retrieve(_plan(_issue("issue-1")), tracker=_tracker())
        assert result.trace["graphEdgesRejected"]
        assert "law-b-article-7" not in result.accepted_article_ids

    def test_graph_failure_does_not_stop_retrieval(self) -> None:
        class BrokenGraph:
            def paths_from_many(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
                raise RuntimeError("neo4j unavailable")

        os_client = FakeOpenSearch(
            {None: [_candidate("law-a-article-1", "公開買付けの原則を定める。")]}
        )
        retriever = LayeredRetriever(os_client, BrokenGraph(), None)
        result = retriever.retrieve(_plan(_issue("issue-1")), tracker=_tracker())
        assert result.requirements[0].retrieval_status == RETRIEVAL_STATUS_RESOLVED


class TestBudgets:
    def test_time_budget_exhaustion_stops_and_records_reason(self) -> None:
        os_client = FakeOpenSearch({None: [_candidate("law-a-article-1", "政令で定める。")]})
        retriever = LayeredRetriever(os_client, FakeGraph(), None)
        result = retriever.retrieve(
            _plan(_issue("issue-1")), tracker=_tracker(budget_sec=0.0)
        )
        assert result.incomplete is True
        assert result.stop_reason == STOP_REASON_TIME
        assert result.requirements[0].unresolved_reason == STOP_REASON_TIME

    def test_search_failure_is_recorded_as_fallback(self) -> None:
        class BrokenSearch:
            def search_requirement_specs(self, specs: list[Any], **kwargs: Any) -> dict:
                raise RuntimeError("opensearch down")

        retriever = LayeredRetriever(BrokenSearch(), FakeGraph(), None)
        result = retriever.retrieve(_plan(_issue("issue-1")), tracker=_tracker())
        assert result.requirements[0].retrieval_status == "exhausted"

    def test_trace_reports_requirements_and_candidates(self) -> None:
        os_client = FakeOpenSearch({None: [_candidate("law-a-article-1", "原則。")]})
        retriever = LayeredRetriever(os_client, FakeGraph(), None)
        trace = retriever.retrieve(_plan(_issue("issue-1")), tracker=_tracker()).trace
        assert trace["evidenceRequirements"]
        assert trace["conclusionGroups"]
        assert trace["articleCandidateCount"] == 1
        assert trace["searchesByRequirement"]

    def test_article_budget_counts_candidate_pool_and_really_evicts_optional(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "layered_max_article_candidates_total", 2)
        monkeypatch.setattr(settings, "layered_max_articles_per_requirement", 3)
        monkeypatch.setattr(settings, "layered_max_accepted_articles_per_requirement", 1)
        state = _RetrievalState(tracker=_tracker(), user_clearance_level=2)
        optional = EvidenceRequirement(
            requirement_id="optional",
            issue_id="issue-1",
            role_family="interpretive",
            mandatory=False,
            key_terms=("公開買付け",),
        )
        optional_candidates = [
            _candidate("law-a-article-1", "公開買付けの取扱い。"),
            _candidate("law-a-article-2", "公開買付けの考え方。"),
        ]
        state.allocate_candidates(
            optional,
            optional_candidates,
            satisfying_article_ids={
                "law-a-article-1",
                "law-a-article-2",
            },
        )
        mandatory = EvidenceRequirement(
            requirement_id="mandatory",
            issue_id="issue-2",
            role_family="normative_rule",
            mandatory=True,
            key_terms=("公開買付け",),
        )
        mandatory_candidate = _candidate(
            "law-b-article-1", "公開買付けをしなければならない。"
        )

        pooled, accepted = state.allocate_candidates(
            mandatory,
            [mandatory_candidate],
            satisfying_article_ids={"law-b-article-1"},
        )

        assert [item["articleId"] for item in pooled] == ["law-b-article-1"]
        assert [item["articleId"] for item in accepted] == ["law-b-article-1"]
        assert state.article_candidate_count == 2
        assert state.evicted_candidate_ids == ["law-a-article-2"]
        assert [
            item["articleId"]
            for item in state.candidates_by_requirement["optional"]
        ] == ["law-a-article-1"]

    def test_mandatory_low_rank_surplus_can_be_evicted_for_later_mandatory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "layered_max_article_candidates_total", 2)
        monkeypatch.setattr(settings, "layered_max_articles_per_requirement", 2)
        monkeypatch.setattr(settings, "layered_max_accepted_articles_per_requirement", 1)
        state = _RetrievalState(tracker=_tracker(), user_clearance_level=2)
        first = EvidenceRequirement(
            requirement_id="first",
            issue_id="issue-1",
            role_family="normative_rule",
            key_terms=("公開買付け",),
        )
        state.allocate_candidates(
            first,
            [
                _candidate("law-a-article-1", "公開買付けをしなければならない。"),
                _candidate("law-a-article-2", "公開買付けの補足規定。"),
            ],
            satisfying_article_ids={"law-a-article-1"},
        )
        later = EvidenceRequirement(
            requirement_id="later",
            issue_id="issue-2",
            role_family="qualification",
            key_terms=("公開買付け",),
        )
        pooled, accepted = state.allocate_candidates(
            later,
            [_candidate("law-b-article-1", "公開買付けをしないことができる場合。")],
            satisfying_article_ids={"law-b-article-1"},
        )

        assert [item["articleId"] for item in accepted] == ["law-b-article-1"]
        assert [item["articleId"] for item in pooled] == ["law-b-article-1"]
        assert state.evicted_candidate_ids == ["law-a-article-2"]
        assert [
            item["articleId"] for item in state.candidates_by_requirement["first"]
        ] == ["law-a-article-1"]

    def test_reranker_batches_all_requirements_instead_of_only_the_first_two(
        self,
    ) -> None:
        class FakeBatchReranker:
            def __init__(self) -> None:
                self.calls: list[list[tuple[str, list[dict[str, Any]]]]] = []

            def rerank_batch(self, requests_batch, timeout_sec=None):
                self.calls.append(requests_batch)
                return [
                    RerankResult(
                        items=list(reversed(items)),
                        used=True,
                        provider="fake",
                        latency_ms=1,
                        scores={
                            item["document"]["contentUnitId"]: float(index)
                            for index, item in enumerate(items, start=1)
                        },
                    )
                    for _, items in requests_batch
                ]

        candidates = [
            _candidate("law-a-article-1", "公開買付けの原則を定める。", score=3.0),
            _candidate("law-a-article-2", "公開買付けの要件を定める。", score=2.0),
        ]
        os_client = FakeOpenSearch({None: candidates})
        reranker = FakeBatchReranker()
        issues = tuple(_issue(f"issue-{index}") for index in range(3))
        result = LayeredRetriever(
            os_client,
            FakeGraph(),
            reranker,
        ).retrieve(_plan(*issues), tracker=_tracker())
        assert len(reranker.calls) == 1
        assert len(reranker.calls[0]) == 3
        assert all(
            item["used"] is True
            for item in result.trace["rerankByRequirement"].values()
        )


class TestEndToEndContext:
    def test_retrieved_articles_can_be_assembled_into_context(self) -> None:
        os_client = FakeOpenSearch(
            {None: [_candidate("law-a-article-1", "公開買付けの原則を定める。")]}
        )
        retriever = LayeredRetriever(os_client, FakeGraph(), None)
        result = retriever.retrieve(_plan(_issue("issue-1")), tracker=_tracker())
        candidates = [
            ChunkCandidate(
                content_unit_id=article_id,
                article_id=article_id,
                requirement_ids=tuple(
                    requirement.requirement_id
                    for requirement in result.requirements
                    if article_id in requirement.accepted_article_ids
                ),
            )
            for article_id in result.accepted_article_ids
        ]
        assembly = assemble_context(candidates, result.requirements, result.groups)
        assert assembly.answer_status == ANSWER_STATUS_COMPLETE

    def test_chunk_selection_prefers_requirement_relevance_over_source_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "layered_max_chunks_per_article", 1)
        result = LayeredRetriever(
            FakeOpenSearch(
                {
                    None: [
                        {
                            **_candidate(
                                "law-a-article-1",
                                "公開買付けをしなければならない。",
                            ),
                            "chunks": [
                                {
                                    "contentUnitId": "law-a-article-1-paragraph-1",
                                    "text": "雑則を定める。",
                                },
                                {
                                    "contentUnitId": "law-a-article-1-paragraph-2",
                                    "text": "公開買付けをしなければならない。",
                                },
                            ],
                        }
                    ]
                }
            ),
            FakeGraph(),
            None,
        ).retrieve(_plan(_issue("issue-1")), tracker=_tracker())

        selected = chunk_candidates(result)

        assert [item.content_unit_id for item in selected] == [
            "law-a-article-1-paragraph-2"
        ]


class TestLawFamilyScope:
    def test_child_search_is_scoped_to_the_parent_law_family(self) -> None:
        """薬機法の委任先を探すとき、別法令系統の施行令へ届かせない (§6.3-7)。"""
        os_client = FakeOpenSearch(
            {
                None: [
                    _candidate(
                        "law-335AC0000000145-article-18_2",
                        "公開買付けについて厚生労働省令で定める。",
                    )
                ],
                "ministerial_ordinance": [
                    _candidate(
                        "law-336M50000100001-article-96",
                        "公開買付けの責任者を置く。",
                        "ministerial_ordinance",
                    )
                ],
            }
        )
        retriever = LayeredRetriever(os_client, FakeGraph(), None)
        retriever.retrieve(_plan(_issue("issue-1")), tracker=_tracker())
        child_specs = [
            spec
            for call in _law_calls(os_client)
            for spec in call
            if spec.authority_type == "ministerial_ordinance"
        ]
        assert child_specs
        assert "law-336M50000100001" in child_specs[0].family_document_ids
        assert "law-340CO0000000321" not in child_specs[0].family_document_ids
