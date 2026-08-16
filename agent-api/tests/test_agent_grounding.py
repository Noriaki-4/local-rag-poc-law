from time import perf_counter

from app.agent import AgentService
from app.llm import GroundingReviewResult, LLMResult
from app.models import AnswerRequest, Citation


class GroundedAnswerLLM:
    provider = "fake"

    def generate_answer(self, request, route, citations, **kwargs):
        revised = kwargs.get("review_feedback") is not None
        answer = (
            "例外の場合は適用対象から除かれます "
            "[law-test-article-2]。専門家確認が必要です。"
            if revised
            else "例外の場合も適用されます。"
        )
        return LLMResult(
            text=answer,
            provider="fake",
            model="fake-answer",
            latencyMs=1,
            inputTokens=10,
            outputTokens=5,
            estimatedCost=0,
            answer=answer,
            predictedAnswer=None,
            choiceJudgements=None,
            answerStatus="ready",
            answerCitationIds=["law-test-article-2"],
            missing=[],
        )

    def review_answer_grounding(self, request, answer, citations, **kwargs):
        return GroundingReviewResult(
            verdict=("supported" if "除かれます" in answer else "needs_revision"),
            issues=[] if "除かれます" in answer else ["例外の向きを修正"],
            provider="fake",
            model="fake-review",
            latencyMs=1,
            inputTokens=10,
            outputTokens=5,
        )


def test_incomplete_research_is_labeled_and_uses_reviewed_answer() -> None:
    service = AgentService(object(), object(), GroundedAnswerLLM())
    citation = Citation(
        documentId="law-test",
        contentUnitId="law-test-article-2",
        text="ただし、例外の場合を除く。",
    )
    trace: dict = {}

    answer, _, _, citation_ids = service._compose_answer(
        AnswerRequest(question="例外の場合も対象ですか"),
        ["llm_directed_legal_research"],
        [citation],
        trace,
        perf_counter() + 10,
        None,
        research_context={
            "status": "continue",
            "stopReason": "iterative_cycles_complete",
            "incomplete": True,
        },
    )

    assert answer.startswith("【調査未完了】")
    assert "適用対象から除かれます" in answer
    assert citation_ids == ["law-test-article-2"]
    assert trace["groundingReview"]["verdict"] == "supported"
    assert trace["groundingReview"]["attemptCount"] == 2
    assert trace["llm"]["attemptCount"] == 2
    assert trace["partialAnswer"] == {
        "incomplete": True,
        "stopReason": "iterative_cycles_complete",
    }


class InsufficientReviewLLM(GroundedAnswerLLM):
    def review_answer_grounding(self, request, answer, citations, **kwargs):
        return GroundingReviewResult(
            verdict="insufficient",
            issues=["質問全体の根拠が不足"],
            provider="fake",
            model="fake-review",
            latencyMs=1,
            inputTokens=10,
            outputTokens=5,
        )


def test_insufficient_grounding_is_explicitly_labeled() -> None:
    service = AgentService(object(), object(), InsufficientReviewLLM())

    answer, _, _, _ = service._compose_answer(
        AnswerRequest(question="全要件を教えてください"),
        ["llm_directed_legal_research"],
        [
            Citation(
                documentId="law-test",
                contentUnitId="law-test-article-2",
                text="一部の要件",
            )
        ],
        {},
        perf_counter() + 10,
        None,
        research_context={"status": "ready", "incomplete": False},
    )

    assert answer.startswith("【根拠不十分】")
    assert "30%なら必須" not in answer


class PartialMainLLM(GroundedAnswerLLM):
    def generate_answer(self, request, route, citations, **kwargs):
        return LLMResult(
            text="確認できたのは第1の要件だけです [law-test-article-2]。",
            provider="fake",
            model="fake-answer",
            latencyMs=1,
            inputTokens=10,
            outputTokens=5,
            estimatedCost=0,
            answer="確認できたのは第1の要件だけです [law-test-article-2]。",
            predictedAnswer=None,
            choiceJudgements=None,
            answerStatus="partial",
            answerCitationIds=["law-test-article-2"],
            missing=["第2の要件"],
        )

    def review_answer_grounding(self, request, answer, citations, **kwargs):
        return GroundingReviewResult(
            verdict="supported",
            issues=[],
            provider="fake",
            model="fake-review",
            latencyMs=1,
            inputTokens=10,
            outputTokens=5,
        )


def test_main_agent_partial_decision_is_preserved() -> None:
    service = AgentService(object(), object(), PartialMainLLM())

    answer, _, _, _ = service._compose_answer(
        AnswerRequest(question="全要件を教えてください"),
        ["llm_directed_legal_research"],
        [
            Citation(
                documentId="law-test",
                contentUnitId="law-test-article-2",
                text="一部の要件",
            )
        ],
        {},
        perf_counter() + 10,
        None,
        research_context={
            "status": "ready",
            "incomplete": False,
        },
    )

    assert answer.startswith("【一部のみ回答】")
    assert "第1の要件" in answer


class ReviewerDirectedResearchLLM(GroundedAnswerLLM):
    def __init__(self):
        self.research_contexts: list[dict | None] = []

    def generate_answer(self, request, route, citations, **kwargs):
        self.research_contexts.append(kwargs.get("research_context"))
        revised = kwargs.get("review_feedback") is not None
        citation_id = (
            "law-test-article-3"
            if revised
            else "law-test-article-2"
        )
        answer = (
            f"追加取得した本文で要件を確認しました [{citation_id}]。"
            if revised
            else f"当初の本文だけで回答します [{citation_id}]。"
        )
        return LLMResult(
            text=answer,
            provider="fake",
            model="fake-answer",
            latencyMs=1,
            inputTokens=10,
            outputTokens=5,
            estimatedCost=0,
            answer=answer,
            predictedAnswer=None,
            choiceJudgements=None,
            answerStatus="ready",
            answerCitationIds=[citation_id],
            missing=[],
        )

    def review_answer_grounding(self, request, answer, citations, **kwargs):
        researched = any(
            citation.contentUnitId == "law-test-article-3"
            for citation in citations
        )
        return GroundingReviewResult(
            verdict="supported" if researched else "needs_research",
            issues=[] if researched else ["第3条の本文が必要"],
            researchQueries=[] if researched else ["テスト法 第3条 要件"],
            provider="fake",
            model="fake-review",
            latencyMs=1,
            inputTokens=10,
            outputTokens=5,
        )


class ReviewerResearchOpenSearch:
    def law_titles(self):
        return {"law-test": "テスト法"}

    def search(self, query, doc_type, top_k, clearance, use_bm25, use_vector):
        if doc_type != "law":
            return []
        return [
            {
                "documentId": "law-test",
                "contentUnitId": "law-test-article-3",
                "articleId": "law-test-article-3",
                "docType": "law",
                "title": "テスト法",
                "heading": "第三条",
                "text": "追加の要件を定める。",
            }
        ]


def test_reviewer_can_route_missing_evidence_back_to_search() -> None:
    llm = ReviewerDirectedResearchLLM()
    service = AgentService(
        ReviewerResearchOpenSearch(),
        object(),
        llm,
    )
    citations = [
        Citation(
            documentId="law-test",
            contentUnitId="law-test-article-2",
            text="当初の本文。",
        )
    ]
    trace: dict = {}

    answer, _, _, citation_ids = service._compose_answer(
        AnswerRequest(question="要件を教えてください"),
        ["llm_directed_legal_research"],
        citations,
        trace,
        perf_counter() + 10,
        None,
        research_context={"status": "ready", "incomplete": False},
    )

    assert "追加取得した本文" in answer
    assert citation_ids == ["law-test-article-3"]
    assert citations[0].contentUnitId == "law-test-article-3"
    assert trace["groundingReview"]["verdict"] == "supported"
    assert trace["groundingResearch"]["requestedQueries"] == [
        "テスト法 第3条 要件"
    ]
    assert trace["groundingResearch"]["semanticSelection"] == "main_llm"
    assert llm.research_contexts[0].get("reviewerFollowUp") is None
    assert llm.research_contexts[1]["reviewerFollowUp"] == {
        "performed": True,
        "queries": ["テスト法 第3条 要件"],
        "newEvidenceContentUnitIds": ["law-test-article-3"],
    }


class TwoRevisionLLM(GroundedAnswerLLM):
    def __init__(self):
        self.main_calls = 0
        self.revision_kwargs: list[dict] = []

    def generate_answer(self, request, route, citations, **kwargs):
        self.main_calls += 1
        if kwargs.get("review_feedback") is not None:
            self.revision_kwargs.append(kwargs)
        answer = f"第{self.main_calls}稿"
        return LLMResult(
            text=answer,
            provider="fake",
            model="fake-answer",
            latencyMs=1,
            inputTokens=10,
            outputTokens=5,
            estimatedCost=0,
            answer=answer,
            predictedAnswer=None,
            choiceJudgements=None,
            answerStatus="ready",
            answerCitationIds=["law-test-article-2"],
            missing=[],
        )

    def review_answer_grounding(self, request, answer, citations, **kwargs):
        supported = answer == "第3稿"
        return GroundingReviewResult(
            verdict="supported" if supported else "needs_revision",
            issues=[] if supported else ["同じ引用で表現を限定する必要がある"],
            provider="fake",
            model="fake-review",
            latencyMs=1,
            inputTokens=10,
            outputTokens=5,
        )


def test_reviewer_can_request_two_bounded_revisions() -> None:
    llm = TwoRevisionLLM()
    service = AgentService(object(), object(), llm)
    trace: dict = {}

    answer, _, _, citation_ids = service._compose_answer(
        AnswerRequest(question="適用範囲を説明してください"),
        ["llm_directed_legal_research"],
        [
            Citation(
                documentId="law-test",
                contentUnitId="law-test-article-2",
                text="確認済み本文",
            )
        ],
        trace,
        perf_counter() + 10,
        None,
        research_context={"status": "ready", "incomplete": False},
    )

    assert answer == "第3稿"
    assert citation_ids == ["law-test-article-2"]
    assert llm.main_calls == 3
    assert trace["groundingReview"]["verdict"] == "supported"
    assert len(trace["groundingReview"]["remediationRounds"]) == 2
    assert llm.revision_kwargs[0]["review_verdict"] == "needs_revision"
    assert llm.revision_kwargs[0]["previous_answer"] == "第1稿"
    assert llm.revision_kwargs[0]["previous_answer_status"] == "ready"
    assert llm.revision_kwargs[0]["previous_citation_ids"] == [
        "law-test-article-2"
    ]
    assert llm.revision_kwargs[0]["previous_missing"] == []


class InvalidMainContractLLM(GroundedAnswerLLM):
    def generate_answer(self, request, route, citations, **kwargs):
        result = super().generate_answer(request, route, citations, **kwargs)
        result.validationError = "validation_error: citation mismatch"
        return result


def test_invalid_main_contract_fails_closed_without_candidate_citations() -> None:
    service = AgentService(object(), object(), InvalidMainContractLLM())
    trace: dict = {}

    answer, predicted, judgements, citation_ids = service._compose_answer(
        AnswerRequest(question="全要件を教えてください"),
        ["llm_directed_legal_research"],
        [
            Citation(
                documentId="law-test",
                contentUnitId="law-test-article-2",
                text="候補本文",
            )
        ],
        trace,
        perf_counter() + 10,
        None,
        research_context={"status": "ready", "incomplete": False},
    )

    assert "構造化最終判断を検証できなかった" in answer
    assert predicted is None
    assert judgements is None
    assert citation_ids == []
    assert trace["llm"]["errorCode"] == "answer_contract_invalid"
