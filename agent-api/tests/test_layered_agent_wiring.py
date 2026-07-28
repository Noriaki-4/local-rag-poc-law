"""agent.pyへのvNext配線テスト (計画書 §19 互換性, §12 フォールバック)。"""

from typing import Any

import pytest

from app import agent as agent_module
from app.agent import AgentService


class StubOpenSearch:
    def law_titles(self) -> dict[str, str]:
        return {"law-test": "検証法"}

    def search_requirement_specs(
        self, specs: list[Any], *, user_clearance_level: int, timeout_sec: float | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        return {
            spec.requirement_id: [
                {
                    "articleId": "law-test-article-2",
                    "documentId": "law-test",
                    "score": 3.0,
                    "authorityType": "act",
                    "authorityRank": 0,
                    "heading": "第2条",
                    "text": "第二条 要件を定める。",
                    "chunks": [
                        {
                            "contentUnitId": "law-test-article-2",
                            "documentId": "law-test",
                            "docType": "law",
                            "text": "第二条 要件を定める。",
                        }
                    ],
                }
            ]
            for spec in specs
        }


class StubGraph:
    def paths_from_many(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []


class StubLLM:
    provider = "fake"


def _service() -> AgentService:
    return AgentService(StubOpenSearch(), StubGraph(), StubLLM())


def _request() -> Any:
    from app.models import AnswerRequest

    return AnswerRequest(
        question="検証法第2条の要件はどのような場合に適用されますか",
        pattern="pattern_4_deepsearch",
        userClearanceLevel=2,
        topK=5,
    )


def _legacy_context() -> list[dict[str, Any]]:
    return [{"document": {"contentUnitId": "legacy-1", "text": "旧経路の根拠"}}]


class TestFeatureFlags:
    def test_disabled_by_default_leaves_context_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(agent_module.settings, "agent_layered_legal_retrieval", False)
        monkeypatch.setattr(agent_module.settings, "agent_layered_legal_retrieval_shadow", False)
        trace: dict[str, Any] = {}
        legacy = _legacy_context()
        result = _service()._apply_layered_legal_retrieval(
            _request(), legacy, deadline=_deadline(), trace=trace, route=[]
        )
        assert result == legacy
        assert "layeredLegalRetrieval" not in trace

    def test_shadow_records_trace_without_changing_context(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(agent_module.settings, "agent_layered_legal_retrieval", False)
        monkeypatch.setattr(agent_module.settings, "agent_layered_legal_retrieval_shadow", True)
        trace: dict[str, Any] = {}
        legacy = _legacy_context()
        route: list[str] = []
        result = _service()._apply_layered_legal_retrieval(
            _request(), legacy, deadline=_deadline(), trace=trace, route=route
        )
        assert result == legacy
        assert trace["layeredLegalRetrieval"]["mode"] == "shadow"
        assert trace["layeredLegalRetrieval"]["contextCoverage"]["answerStatus"]
        assert "layered_legal_retrieval" not in route

    def test_active_uses_new_context_when_primary_groups_are_covered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(agent_module.settings, "agent_layered_legal_retrieval", True)
        trace: dict[str, Any] = {}
        route: list[str] = []
        result = _service()._apply_layered_legal_retrieval(
            _request(), _legacy_context(), deadline=_deadline(), trace=trace, route=route
        )
        assert [item["document"]["contentUnitId"] for item in result] == ["law-test-article-2"]
        assert "layered_legal_retrieval" in route
        assert trace["layeredLegalRetrieval"]["mode"] == "active"

    def test_active_returns_no_context_when_no_primary_group_is_covered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class EmptySearch(StubOpenSearch):
            def search_requirement_specs(self, specs: list[Any], **kwargs: Any) -> dict:
                return {spec.requirement_id: [] for spec in specs}

        monkeypatch.setattr(agent_module.settings, "agent_layered_legal_retrieval", True)
        service = AgentService(EmptySearch(), StubGraph(), StubLLM())
        trace: dict[str, Any] = {}
        legacy = _legacy_context()
        result = service._apply_layered_legal_retrieval(
            _request(), legacy, deadline=_deadline(), trace=trace, route=[]
        )
        assert result == []
        assert trace["layeredLegalRetrieval"]["contextCoverage"]["answerStatus"] == (
            "insufficient_primary_evidence"
        )
        assert "fallback" not in trace["layeredLegalRetrieval"]

    def test_insufficient_primary_evidence_suppresses_normal_answer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(agent_module.settings, "agent_layered_legal_retrieval", True)
        trace = {
            "layeredLegalRetrieval": {
                "mode": "active",
                "contextCoverage": {
                    "answerStatus": "insufficient_primary_evidence",
                },
                "answerControl": {
                    "answerStatus": "insufficient_primary_evidence",
                    "omittedPrimaryIssueLabels": ["公開買付けの要件"],
                },
            }
        }
        answer, predicted, judgements, citation_ids = _service()._compose_answer(
            _request(), [], [], trace, _deadline(), None
        )
        assert "根拠付きで回答できません" in answer
        assert predicted is None
        assert judgements is None
        assert citation_ids == []


class TestFailureIsolation:
    def test_internal_error_does_not_break_the_current_answer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class BrokenSearch(StubOpenSearch):
            def search_requirement_specs(self, specs: list[Any], **kwargs: Any) -> dict:
                raise RuntimeError("boom")

        monkeypatch.setattr(agent_module.settings, "agent_layered_legal_retrieval", True)
        service = AgentService(BrokenSearch(), StubGraph(), StubLLM())
        trace: dict[str, Any] = {}
        legacy = _legacy_context()
        result = service._apply_layered_legal_retrieval(
            _request(), legacy, deadline=_deadline(), trace=trace, route=[]
        )
        assert result == legacy
        assert trace["layeredLegalRetrieval"]["fallback"] == "legacy_retrieval"


class TestExplicitReferences:
    def test_question_article_numbers_become_explicit_references(self) -> None:
        references = _service()._layered_explicit_references(_request())
        assert references[0]["articleContentUnitId"] == "law-test-article-2"
        assert references[0]["documentId"] == "law-test"

    def test_law_titles_failure_is_tolerated(self) -> None:
        class BrokenTitles(StubOpenSearch):
            def law_titles(self) -> dict[str, str]:
                raise RuntimeError("opensearch down")

        service = AgentService(BrokenTitles(), StubGraph(), StubLLM())
        assert service._layered_explicit_references(_request()) == []


def _deadline() -> float:
    from time import perf_counter

    return perf_counter() + 600
