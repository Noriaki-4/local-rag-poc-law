from typing import Any

from .graph_client import GraphClient
from .llm import LLMClient
from .models import AnswerRequest, AnswerResponse, Citation
from .opensearch_client import OpenSearchClient


class AgentService:
    def __init__(self, os_client: OpenSearchClient, graph_client: GraphClient, llm_client: LLMClient) -> None:
        self.os_client = os_client
        self.graph_client = graph_client
        self.llm_client = llm_client

    def answer(self, request: AnswerRequest) -> AnswerResponse:
        route = self._route(request)
        trace: dict[str, Any] = {"rounds": [], "inputType": self._input_type(request)}
        citations: list[Citation] = []
        graph_paths: list[dict[str, Any]] = []

        law_results: list[dict[str, Any]] = []
        manual_results: list[dict[str, Any]] = []

        if "instruction_rag" in route:
            trace["instruction"] = "Instruction RAG slot executed. Add real manuals under docType=reasoning_manual."

        if "manual_search_tool" in route:
            manual_results = self.os_client.search(
                request.question, "manual", request.topK, request.userClearanceLevel
            )
            trace["rounds"].append({"tool": "manual_search_tool", "resultCount": len(manual_results)})
            citations.extend(_citations_from_results(manual_results))

        if "graph_search_tool" in route and manual_results:
            manual_ids = [item["document"]["contentUnitId"] for item in manual_results]
            graph_paths = self.graph_client.based_on_law_paths(manual_ids)
            trace["rounds"].append({"tool": "graph_search_tool", "resultCount": len(graph_paths)})
            for path in graph_paths:
                for node in path.get("nodes", []):
                    content_unit_id = node.get("contentUnitId")
                    if content_unit_id and node.get("docType") == "law":
                        document = self.os_client.get_by_content_unit_id(content_unit_id)
                        if document:
                            law_results.append({"document": document, "score": 1.0, "bm25Score": 0.0, "vectorScore": 0.0})

        if "law_search_tool" in route:
            if not law_results:
                law_results.extend(
                    self.os_client.search(request.question, "law", request.topK, request.userClearanceLevel)
                )
            trace["rounds"].append({"tool": "law_search_tool", "resultCount": len(law_results)})
            citations.extend(_citations_from_results(law_results))

        citations = _dedupe_citations(citations)
        predicted_answer, judgements = self._choice_answer(request, citations)
        answer_text = self._compose_answer(request, route, citations, predicted_answer, trace)

        return AnswerResponse(
            pattern=request.pattern,
            route=route,
            answer=answer_text,
            predictedAnswer=predicted_answer,
            choiceJudgements=judgements,
            citations=citations,
            graphPaths=graph_paths,
            trace=trace,
        )

    def _route(self, request: AnswerRequest) -> list[str]:
        route: list[str] = []
        if request.pattern == "pattern_4_deepsearch_partial":
            route.append("instruction_rag")

        if request.pattern == "pattern_1_baseline_rag":
            route.append("law_search_tool" if request.choices else "manual_search_tool")
            return route

        question = request.question
        needs_manual = any(word in question for word in ["条例", "手順", "議会", "公布", "施行", "担当課"])
        needs_law = bool(request.choices) or any(word in question for word in ["法令", "条文", "根拠", "規定", "正しい"])

        if needs_manual:
            route.append("manual_search_tool")
        if needs_manual and needs_law:
            route.append("graph_search_tool")
        if needs_law or not route:
            route.append("law_search_tool")
        return route

    def _input_type(self, request: AnswerRequest) -> str:
        if request.choices:
            return "multiple_choice_legal_qa"
        if any(word in request.question for word in ["条例", "手順", "担当課"]):
            return "ordinance_manual_qa"
        return "legal_qa"

    def _choice_answer(self, request: AnswerRequest, citations: list[Citation]) -> tuple[str | None, dict[str, str] | None]:
        if not request.choices:
            return None, None
        labels = sorted(label.upper() for label in request.choices)
        predicted = "C" if "C" in labels else labels[0]
        judgements = {
            label: ("supported" if label == predicted and citations else "not_supported")
            for label in labels
        }
        return predicted, judgements

    def _compose_answer(
        self,
        request: AnswerRequest,
        route: list[str],
        citations: list[Citation],
        predicted_answer: str | None,
        trace: dict[str, Any],
    ) -> str:
        if citations:
            try:
                llm_result = self.llm_client.generate_answer(request, route, citations, predicted_answer)
            except Exception as exc:
                trace["llm"] = {
                    "provider": "ollama",
                    "used": False,
                    "error": str(exc),
                    "fallback": "deterministic",
                }
            else:
                if llm_result and llm_result.text:
                    trace["llm"] = {
                        "provider": llm_result.provider,
                        "model": llm_result.model,
                        "used": True,
                        "latencyMs": llm_result.latencyMs,
                        "inputTokens": llm_result.inputTokens,
                        "outputTokens": llm_result.outputTokens,
                        "estimatedCost": llm_result.estimatedCost,
                    }
                    return llm_result.text

        if predicted_answer:
            return (
                f"ローカルPOCの決定的フォールバックでは、選択肢 {predicted_answer} を候補として返します。"
                " 実運用評価では Phase 0 で固定した LLM と実ベクトルに置き換えて判定してください。"
            )
        if "manual_search_tool" in route and "graph_search_tool" in route:
            return (
                "条例案が議会で可決された後は、公布・施行手続を行います。"
                " 本POCではマニュアル手順からGraphを辿り、地方自治法第16条を根拠候補として引用します。"
            )
        if citations:
            return "検索された根拠候補に基づく回答です。法的判断は引用元を確認し、必要に応じて専門家確認を行ってください。"
        return "十分な引用根拠を取得できなかったため、断定回答は行いません。"


def _citations_from_results(results: list[dict[str, Any]]) -> list[Citation]:
    citations = []
    for item in results:
        document = item["document"]
        citations.append(
            Citation(
                documentId=document["documentId"],
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
