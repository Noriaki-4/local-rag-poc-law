"""新Agent Frameworkを法令検索へ接続する薄いAPI Application Service。"""

from __future__ import annotations

from uuid import uuid4

from app.adapters.models import StructuredJSONModelAdapter
from app.adapters.persistence.simple_in_memory import InMemoryCaseStore
from app.agent_framework.loop import AgentLoop
from app.agent_framework.state import CaseState, Evidence
from app.domains.legal import legal_agent_profile, legal_tool_registry
from app.graph_client import GraphClient
from app.llm import LLMClient
from app.models import AnswerRequest, AnswerResponse, Citation
from app.opensearch_client import OpenSearchClient


class LegalFrameworkAgentService:
    def __init__(
        self,
        os_client: OpenSearchClient,
        graph_client: GraphClient,
        llm_client: LLMClient,
    ) -> None:
        self._os_client = os_client
        self._graph_client = graph_client
        self._llm_client = llm_client

    def answer(self, request: AnswerRequest) -> AnswerResponse:
        if request.choices:
            raise ValueError(
                "new agent framework currently supports natural-language questions only"
            )

        profile = legal_agent_profile()
        store = InMemoryCaseStore()
        initial = CaseState(
            case_id=f"legal-{uuid4().hex}",
            question=request.question,
        )
        store.create(initial)
        loop = AgentLoop(
            store=store,
            model=StructuredJSONModelAdapter(self._llm_client),
            tools=legal_tool_registry(
                self._os_client,
                self._graph_client,
                user_clearance_level=request.userClearanceLevel,
            ),
            profile=profile,
        )
        result = loop.run(initial.case_id)
        final_answer = result.state.final_answer
        if result.state.run_status == "completed" and final_answer is not None:
            answer_text = final_answer.text
            citation_ids = final_answer.citation_ids
        else:
            answer_text = (
                "新Agent Frameworkは根拠付き回答を完了できませんでした。"
                f"停止理由: {result.state.stop_reason or 'unknown'}"
            )
            citation_ids = ()

        evidence_by_id = {item.evidence_id: item for item in result.state.evidence}
        citations = [
            _citation_from_evidence(evidence_by_id[evidence_id])
            for evidence_id in citation_ids
            if evidence_id in evidence_by_id
        ]
        return AnswerResponse(
            pattern="agent_framework_v1",
            route=["agent_framework", "legal_domain"],
            answer=answer_text,
            citations=citations,
            graphPaths=[],
            trace={
                "agentFramework": {
                    "algorithm": "differential_graph_review_v2",
                    "profile": profile.name,
                    "profileVersion": profile.version,
                    "provider": profile.provider,
                    "reviewerEnabled": profile.reviewer.enabled,
                    "runStatus": result.state.run_status,
                    "researchCycleCount": result.state.research_cycle_count,
                    "stopReason": result.state.stop_reason,
                    "failureCode": result.trace.failure_code,
                    "modelCalls": [
                        item.model_dump(mode="json")
                        for item in result.trace.model_calls
                    ],
                    "toolCalls": [
                        item.model_dump(mode="json") for item in result.trace.tool_calls
                    ],
                    "workItems": [
                        {
                            "workItemId": item.work_item_id,
                            "parentWorkItemId": item.parent_work_item_id,
                            "state": item.state,
                            "basisHypothesisIds": list(item.basis_hypothesis_ids),
                        }
                        for item in result.state.work_items
                    ],
                    "hypotheses": [
                        {
                            "hypothesisId": item.hypothesis_id,
                            "workItemId": item.work_item_id,
                            "judgment": item.judgment,
                            "evidenceIds": list(item.evidence_ids),
                            "gaps": list(item.gaps),
                        }
                        for item in result.state.hypotheses
                    ],
                    "dependencyDecisions": [
                        item.model_dump(mode="json")
                        for item in result.state.dependency_decisions
                    ],
                    "graphCandidateReviews": [
                        item.model_dump(mode="json")
                        for item in result.state.graph_candidate_reviews
                    ],
                    "frontierReAdoptions": [
                        item.model_dump(mode="json")
                        for item in result.state.frontier_re_adoptions
                    ],
                    "limitations": (
                        list(final_answer.limitations) if final_answer else []
                    ),
                }
            },
        )


def _citation_from_evidence(evidence: Evidence) -> Citation:
    metadata = evidence.metadata
    source_page = metadata.get("sourcePage")
    return Citation(
        documentId=str(metadata.get("documentId") or "unknown"),
        contentUnitId=evidence.evidence_id,
        title=evidence.title,
        heading=str(metadata.get("heading") or "") or None,
        sourceObjectUri=(
            str(metadata.get("sourceObjectUri"))
            if metadata.get("sourceObjectUri") is not None
            else None
        ),
        sourcePage=(int(source_page) if isinstance(source_page, int) else None),
        text=evidence.content,
        evidenceLane=str(metadata.get("docType") or "") or None,
        evidenceRole="solver_selected",
    )
