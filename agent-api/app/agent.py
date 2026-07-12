from typing import Any

from .config import settings
from .llm import LLMClient
from .models import AnswerRequest, AnswerResponse, Citation
from .opensearch_client import OpenSearchClient


class AgentService:
    def __init__(self, os_client: OpenSearchClient, llm_client: LLMClient) -> None:
        self.os_client = os_client
        self.llm_client = llm_client

    def answer(self, request: AnswerRequest) -> AnswerResponse:
        route = self._route(request)
        trace: dict[str, Any] = {"rounds": [], "inputType": self._input_type(request)}
        citations: list[Citation] = []

        law_results: list[dict[str, Any]] = []

        if "instruction_rag" in route:
            trace["instruction"] = "Instruction RAG slot executed. Add real manuals under docType=reasoning_manual."

        if "law_search_tool" in route:
            if not law_results:
                law_results.extend(
                    self.os_client.search(
                        _search_query(request),
                        "law",
                        request.topK,
                        request.userClearanceLevel,
                        use_bm25=settings.agent_use_bm25,
                        use_vector=settings.agent_use_vector,
                    )
                )
            trace["rounds"].append(
                {
                    "tool": "law_search_tool",
                    "resultCount": len(law_results),
                    "useBm25": settings.agent_use_bm25,
                    "useVector": settings.agent_use_vector,
                }
            )
            citations.extend(_citations_from_results(law_results))

        citations = _dedupe_citations(citations)
        answer_text, predicted_answer, judgements = self._compose_answer(request, route, citations, trace)

        return AnswerResponse(
            pattern=request.pattern,
            route=route,
            answer=answer_text,
            predictedAnswer=predicted_answer,
            choiceJudgements=judgements,
            citations=citations,
            graphPaths=[],
            trace=trace,
        )

    def _route(self, request: AnswerRequest) -> list[str]:
        route: list[str] = []
        if request.pattern == "pattern_4_deepsearch_partial":
            route.append("instruction_rag")

        if request.pattern == "pattern_1_baseline_rag":
            route.append("law_search_tool")
            return route

        route.append("law_search_tool")
        return route

    def _input_type(self, request: AnswerRequest) -> str:
        if request.choices:
            return "multiple_choice_legal_qa"
        return "legal_qa"

    def _compose_answer(
        self,
        request: AnswerRequest,
        route: list[str],
        citations: list[Citation],
        trace: dict[str, Any],
    ) -> tuple[str, str | None, dict[str, str] | None]:
        if citations:
            try:
                llm_result = self.llm_client.generate_answer(request, route, citations)
            except Exception as exc:
                trace["llm"] = {
                    "provider": self.llm_client.provider,
                    "used": False,
                    "error": str(exc),
                    "fallback": "no_choice_judgement",
                }
            else:
                if llm_result:
                    trace["llm"] = {
                        "provider": llm_result.provider,
                        "model": llm_result.model,
                        "used": True,
                        "latencyMs": llm_result.latencyMs,
                        "inputTokens": llm_result.inputTokens,
                        "outputTokens": llm_result.outputTokens,
                        "estimatedCost": llm_result.estimatedCost,
                        "validationError": llm_result.validationError,
                    }
                    answer_text = llm_result.text or "十分な引用根拠を取得できなかったため、断定回答は行いません。"
                    return answer_text, llm_result.predictedAnswer, llm_result.choiceJudgements

        if request.choices:
            return (
                "LLM 未使用のため選択肢判定は行いません。"
                " 評価時は predictedAnswer を null として扱ってください。",
                None,
                None,
            )
        if citations:
            return (
                "検索された根拠候補に基づく回答です。法的判断は引用元を確認し、必要に応じて専門家確認を行ってください。",
                None,
                None,
            )
        return "十分な引用根拠を取得できなかったため、断定回答は行いません。", None, None


def _search_query(request: AnswerRequest) -> str:
    """lawqa_jp選択式は問題文が汎用文のことが多く、実質的な検索手掛かりは選択肢側にある。"""
    if not request.choices:
        return request.question
    choice_texts = [text for _, text in sorted(request.choices.items())]
    return " ".join([request.question, *choice_texts])


def _citations_from_results(results: list[dict[str, Any]]) -> list[Citation]:
    citations = []
    for item in results:
        document = item["document"]
        citations.append(
            Citation(
                documentId=document.get("documentId"),
                contentUnitId=document.get("contentUnitId"),
                title=document.get("title"),
                heading=document.get("heading"),
                sourceObjectUri=document.get("sourceObjectUri"),
                sourcePage=document.get("sourcePage"),
                text=document.get("text"),
            )
        )
    return citations


def _dedupe_citations(citations: list[Citation]) -> list[Citation]:
    seen: set[str] = set()
    deduped: list[Citation] = []
    for citation in citations:
        key = f"{citation.documentId}:{citation.contentUnitId}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(citation)
    return deduped
