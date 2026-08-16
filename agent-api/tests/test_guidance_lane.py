"""ガイドレーンのテスト (計画書 §10, §16.4)。"""

from typing import Any

from app.evidence_requirements import ORIGIN_PLANNER, LegalIssue
from app.guidance_lane import EVIDENCE_LANE_GUIDANCE, GuidanceLane
from app.layered_context_assembler import ANSWER_STATUS_COMPLETE, assemble_context
from app.layered_retriever import LayeredRetriever
from app.layered_shadow import guidance_chunk_candidates
from app.legal_issue_planner import IssuePlan
from app.retrieval_budget import BudgetTracker, TimeProfile


def _issue(issue_id: str = "issue-1") -> LegalIssue:
    return LegalIssue(
        issue_id=issue_id,
        label="原状回復の費用負担",
        question_span="退去時の原状回復費用は誰が負担しますか",
        key_terms=("原状回復",),
        requested_role_families=("normative_rule",),
        confidence=0.9,
        source=ORIGIN_PLANNER,
    )


def _tracker(budget_sec: float = 60.0) -> BudgetTracker:
    return BudgetTracker(
        profile=TimeProfile("test", 600, 200, 90), exploration_budget_sec=budget_sec
    )


class FakeOpenSearch:
    def __init__(self, guidance_hits: list[dict[str, Any]] | None = None) -> None:
        self.guidance_hits = guidance_hits if guidance_hits is not None else [_guidance_chunk()]
        self.law_hits: dict[str, dict[str, Any]] = {}
        self.calls: list[list[Any]] = []

    def search_requirement_specs(
        self, specs: list[Any], *, user_clearance_level: int, timeout_sec: float | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        self.calls.append(list(specs))
        results: dict[str, list[dict[str, Any]]] = {}
        for spec in specs:
            if spec.doc_type == "guideline":
                results[spec.requirement_id] = list(self.guidance_hits)
                continue
            results[spec.requirement_id] = [
                dict(self.law_hits[article_id])
                for article_id in spec.article_ids
                if article_id in self.law_hits
            ]
        return results


class FakeGraph:
    def __init__(
        self,
        guidance_paths: list[dict[str, Any]] | None = None,
        assertions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.guidance_paths = guidance_paths or []
        self.assertions = assertions or []
        self.calls: list[dict[str, Any]] = []
        self.assertion_calls: list[list[str]] = []

    def paths_from_many(self, start_ids: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append({"startIds": list(start_ids), **kwargs})
        edge_types = kwargs.get("edge_types") or []
        if "EXPLAINS" in edge_types:
            return self.guidance_paths
        return []

    def relation_assertions_from(
        self,
        article_ids: list[str],
        limit: int = 20,
        user_clearance_level: int = 3,
        timeout_sec: float | None = None,
    ) -> list[dict[str, Any]]:
        self.assertion_calls.append(list(article_ids))
        return self.assertions


def _guidance_chunk(content_unit_id: str = "guidance-mlit-restoration-page-3-chunk-1") -> dict:
    return {
        "contentUnitId": content_unit_id,
        "documentId": "guidance-mlit-restoration",
        "requirementId": "issue-1",
        "score": 4.2,
        "docType": "guideline",
        "authorityType": "guidance",
        "text": "通常損耗の原状回復費用は賃貸人が負担するのが原則である。",
        "source": {
            "contentUnitId": content_unit_id,
            "documentId": "guidance-mlit-restoration",
            "docType": "guideline",
            "title": "原状回復をめぐるトラブルとガイドライン",
            "sectionPath": "国土交通省 > 原状回復をめぐるトラブルとガイドライン > p.3",
            "text": "通常損耗の原状回復費用は賃貸人が負担するのが原則である。",
        },
    }


def _path(edge_type: str, article_id: str, confidence: float = 0.9) -> dict[str, Any]:
    return {
        "nodes": [
            {"graphNodeId": "guidance-mlit-restoration", "documentId": "guidance-mlit-restoration"},
            {"graphNodeId": article_id, "contentUnitId": article_id},
        ],
        "edges": [
            {
                "edgeType": edge_type,
                "fromGraphNodeId": "guidance-mlit-restoration",
                "toGraphNodeId": article_id,
                "relationSource": (
                    "guidance_article_annotation"
                    if edge_type == "EXPLAINS"
                    else "guidance_mention_rule"
                ),
                "relationConfidence": confidence,
            }
        ],
    }


def _law_candidate(article_id: str, text: str = "第六百六条 賃貸人は修繕義務を負う。") -> dict:
    return {
        "articleId": article_id,
        "documentId": article_id.split("-article-")[0],
        "score": 3.0,
        "authorityType": "act",
        "authorityRank": 0,
        "heading": "第606条",
        "text": text,
        "chunks": [{"contentUnitId": article_id, "docType": "law", "text": text}],
    }


class TestGuidanceExploration:
    def test_explains_articles_become_direct_fetch_targets(self) -> None:
        """EXPLAINSから法令本文を取得する (§16.4)。"""
        lane = GuidanceLane(FakeOpenSearch(), FakeGraph([_path("EXPLAINS", "law-civil-article-606")]))
        result = lane.explore((_issue(),), tracker=_tracker())
        assert result.used is True
        assert result.explained_article_ids_by_issue["issue-1"] == ("law-civil-article-606",)

    def test_mentions_only_widens_search_scope(self) -> None:
        """MENTIONSだけでは条文を確実投入しない (§16.4)。"""
        lane = GuidanceLane(FakeOpenSearch(), FakeGraph([_path("MENTIONS", "law-civil-article-621")]))
        result = lane.explore((_issue(),), tracker=_tracker())
        assert result.explained_article_ids_by_issue == {}
        assert result.candidate_document_ids_by_issue["issue-1"] == ("law-civil",)

    def test_low_confidence_explains_is_treated_as_mention(self) -> None:
        lane = GuidanceLane(
            FakeOpenSearch(), FakeGraph([_path("EXPLAINS", "law-civil-article-606", confidence=0.5)])
        )
        result = lane.explore((_issue(),), tracker=_tracker())
        assert result.explained_article_ids_by_issue == {}

    def test_unverified_assertions_are_candidate_expansion_only(self) -> None:
        graph = FakeGraph(
            [_path("EXPLAINS", "law-civil-article-606")],
            assertions=[
                {
                    "assertionId": "assertion-1",
                    "fromArticleId": "law-civil-article-606",
                    "toArticleId": "law-order-article-7",
                    "suggestedType": "IMPLEMENTS",
                    "status": "unverified",
                    "confidence": 0.5,
                }
            ],
        )
        result = GuidanceLane(FakeOpenSearch(), graph).explore((_issue(),), tracker=_tracker())
        assert result.assertions[0]["usage"] == "candidate_expansion_only"
        assert result.assertions[0]["status"] == "unverified"
        # 未確認関係は条文の確実投入には使わない。検索範囲の拡張だけ。
        assert "law-order-article-7" not in result.explained_article_ids_by_issue["issue-1"]
        assert "law-order" in result.candidate_document_ids_by_issue["issue-1"]

    def test_verified_assertions_are_not_returned_as_unverified_candidates(self) -> None:
        graph = FakeGraph(
            [_path("EXPLAINS", "law-civil-article-606")],
            assertions=[{"assertionId": "a", "fromArticleId": "law-civil-article-606", "status": "law_text_verified"}],
        )
        result = GuidanceLane(FakeOpenSearch(), graph).explore((_issue(),), tracker=_tracker())
        assert result.assertions == ()

    def test_derived_articles_are_capped(self) -> None:
        paths = [_path("EXPLAINS", f"law-civil-article-{index}") for index in range(20)]
        result = GuidanceLane(FakeOpenSearch(), FakeGraph(paths)).explore((_issue(),), tracker=_tracker())
        assert len(result.explained_article_ids_by_issue["issue-1"]) <= 6

    def test_timeliness_is_reported_as_unknown_when_absent(self) -> None:
        """ガイドの法令時点が不明な場合は unknown として残す (§10 末尾)。"""
        result = GuidanceLane(FakeOpenSearch(), FakeGraph()).explore((_issue(),), tracker=_tracker())
        timeliness = result.trace["timeliness"][0]
        assert timeliness["publishedAt"] == "unknown"
        assert timeliness["legalAsOfKnown"] is False
        assert timeliness["authority"] == "国土交通省"

    def test_search_failure_is_recorded_and_does_not_raise(self) -> None:
        class BrokenSearch(FakeOpenSearch):
            def search_requirement_specs(self, specs: list[Any], **kwargs: Any) -> dict:
                raise RuntimeError("opensearch down")

        result = GuidanceLane(BrokenSearch(), FakeGraph()).explore((_issue(),), tracker=_tracker())
        assert result.used is False
        assert "searchError" in result.trace

    def test_exhausted_time_budget_skips_the_lane(self) -> None:
        result = GuidanceLane(FakeOpenSearch(), FakeGraph()).explore(
            (_issue(),), tracker=_tracker(budget_sec=0.0)
        )
        assert result.used is False


class TestGuidanceInRetrieval:
    def test_guidance_explained_article_is_fetched_from_law_lane(self) -> None:
        os_client = FakeOpenSearch()
        os_client.law_hits["law-civil-article-606"] = _law_candidate("law-civil-article-606")
        retriever = LayeredRetriever(
            os_client, FakeGraph([_path("EXPLAINS", "law-civil-article-606")]), None
        )
        result = retriever.retrieve(IssuePlan(issues=(_issue(),)), tracker=_tracker())
        assert "law-civil-article-606" in result.accepted_article_ids
        assert result.trace["guidanceLane"]["used"] is True

    def test_guidance_alone_does_not_resolve_a_law_requirement(self) -> None:
        """ガイドだけでは法令Requirementをresolvedにしない (§16.4)。"""
        os_client = FakeOpenSearch()  # 法令ヒットなし
        retriever = LayeredRetriever(
            os_client, FakeGraph([_path("EXPLAINS", "law-civil-article-606")]), None
        )
        result = retriever.retrieve(IssuePlan(issues=(_issue(),)), tracker=_tracker())
        assert result.requirements[0].retrieval_status == "exhausted"
        assert result.accepted_article_ids == ()

    def test_guidance_lane_uses_its_own_search_call(self) -> None:
        os_client = FakeOpenSearch()
        os_client.law_hits["law-civil-article-606"] = _law_candidate("law-civil-article-606")
        retriever = LayeredRetriever(
            os_client, FakeGraph([_path("EXPLAINS", "law-civil-article-606")]), None
        )
        retriever.retrieve(IssuePlan(issues=(_issue(),)), tracker=_tracker())
        doc_types = [{spec.doc_type for spec in call} for call in os_client.calls]
        assert doc_types[0] == {"guideline"}
        assert doc_types[1] == {"law"}


class TestGuidanceInContext:
    def test_guidance_fills_auxiliary_slots_only(self) -> None:
        os_client = FakeOpenSearch(
            guidance_hits=[
                _guidance_chunk("guidance-mlit-restoration-page-3-chunk-1"),
                _guidance_chunk("guidance-mlit-restoration-page-4-chunk-1"),
                _guidance_chunk("guidance-mlit-restoration-page-5-chunk-1"),
            ]
        )
        os_client.law_hits["law-civil-article-606"] = _law_candidate("law-civil-article-606")
        retriever = LayeredRetriever(
            os_client, FakeGraph([_path("EXPLAINS", "law-civil-article-606")]), None
        )
        result = retriever.retrieve(IssuePlan(issues=(_issue(),)), tracker=_tracker())

        from app.layered_shadow import chunk_candidates

        candidates = (*chunk_candidates(result), *guidance_chunk_candidates(result))
        assembly = assemble_context(
            candidates, result.requirements, result.groups, max_chunks=16, max_auxiliary_chunks=2
        )
        assert assembly.answer_status == ANSWER_STATUS_COMPLETE
        guidance_items = [
            candidate for candidate in assembly.selected if candidate.is_guidance
        ]
        assert len(guidance_items) == 2
        assert guidance_items[0].item["evidenceLane"] == EVIDENCE_LANE_GUIDANCE

    def test_guidance_is_dropped_when_no_primary_group_is_covered(self) -> None:
        os_client = FakeOpenSearch()  # 法令ヒットなし
        retriever = LayeredRetriever(
            os_client, FakeGraph([_path("EXPLAINS", "law-civil-article-606")]), None
        )
        result = retriever.retrieve(IssuePlan(issues=(_issue(),)), tracker=_tracker())

        from app.layered_shadow import chunk_candidates

        candidates = (*chunk_candidates(result), *guidance_chunk_candidates(result))
        assembly = assemble_context(candidates, result.requirements, result.groups)
        assert assembly.selected == ()
