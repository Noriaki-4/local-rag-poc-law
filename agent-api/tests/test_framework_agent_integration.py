"""新Frameworkから法令Tool・Model・API応答までの薄い縦切り。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import requests
from pydantic import ValidationError

from app import main
from app.adapters.models import StructuredJSONModelAdapter
from app.adapters.models.structured_json import (
    _assign_tool_request_ids,
    _allocate_observation_fetch_capacity,
    _case_update_transport_schema,
    _derive_observation_work_item_updates,
    _dependency_work_item_contexts,
    _downgrade_unproven_dependency_confirmations,
    _include_required_finalization_citations,
    _include_required_cycle_close_citations,
    _normalize_absent_context_branches,
    _normalize_observation_integration_payload,
    _observation_work_item_contexts,
    _normalize_search_assessment_transport_payload,
    _normalize_search_reselection_transport_payload,
    _normalize_search_selection_transport_payload,
    _normalize_search_review_payload,
    _normalize_staged_research_payload,
    _normalize_solver_payload,
    _preserve_previous_answer_for_contract_repair,
    _search_reselection_prompt,
    _search_reselection_transport_schema,
    _search_review_batch_contexts,
    _search_review_prompt,
    _search_review_context_payload,
    _search_review_transport_schema,
    _solver_anthropic_transport_schema,
    _solver_anthropic_json_transport_schema,
    _solver_common_transport_schema,
    _solver_compact_transport_schema,
    _solver_prompt,
    _solver_transport_schema,
    _tool_requests_transport_schema,
    _validate_search_assessment_payload,
    _validate_search_reselection_payload,
    _validate_selected_search_assessments,
    render_cycle_close_model_call,
    render_dependency_assessment_model_call,
    render_observation_integration_model_call,
    render_search_assessment_model_call,
    render_search_reselection_model_call,
    render_search_selection_model_call,
    render_solver_model_call,
    render_solver_transport_repair_model_call,
    normalize_dependency_action_decision,
)
from app.adapters.persistence.simple_in_memory import InMemoryCaseStore
from app.adapters.tools.legal_search import (
    LegalFetchArticlesTool,
    LegalGraphNeighborsTool,
    LegalSearchTool,
)
from app.agent_framework.contract_rendering import (
    contract_field_description,
    render_solver_contract_glossary,
)
from app.agent_framework.context import (
    ContextCapacityExceeded,
    GraphCandidateArticle,
    GraphCandidateCatalog,
    GraphCandidateLink,
    GraphReviewBatch,
    GraphReviewLedgerItem,
    SearchCandidateArticle,
    SolverActionFeedback,
    SolverContext,
    SolverContractFeedback,
    _graph_review_projection,
    build_solver_context,
)
from app.agent_framework.contracts import (
    CycleCloseDecision,
    DependencyActionDecision,
    DependencyAssessmentDecision,
    HypothesisUpdate,
    ObservationIntegrationDecision,
    SolverDecision,
    WorkItemImpactDecision,
)
from app.agent_framework.diagnostics import AgentDiagnostics
from app.agent_framework.loop import (
    AgentLoop,
    _dependency_audit_scope,
    _dependency_audit_work_item_ids,
)
from app.agent_framework.ports.model import (
    ModelProtocolError,
    ReviewerView,
    SolverCheckpointTimeout,
)
from app.agent_framework.ports.tool import ToolRegistry
from app.agent_framework.profiles import (
    AgentLimits,
    ModelCallProfile,
    ReviewerProfile,
)
from app.agent_framework.state import (
    CaseState,
    DeferredFrontierResolution,
    DependencyDecision,
    Evidence,
    FinalAnswer,
    Hypothesis,
    ReviewFinding,
    ReviewFindingResolution,
    ReviewResult,
    SearchCandidateReview,
    SearchCandidateAssessmentRecord,
    SearchCandidateSelection,
    ToolRequest,
    ToolResult,
    UnreviewedGraphResolution,
    WorkItem,
)
from app.agent_framework.validation import (
    ContractViolation,
    _validated_copy,
    apply_solver_decision,
)
from app.domains.legal import profiles as legal_profiles
from app.framework_agent import LegalFrameworkAgentService
from app.llm import StructuredJSONResult
from app.models import AnswerRequest, AnswerResponse


def test_continue_accepts_dependency_decision_as_state_update() -> None:
    decision = SolverDecision(
        next="continue",
        decision_reason="下位規範の本文確認が残っている。",
        dependency_decisions=(
            DependencyDecision(
                dependency_kind="lower_norm",
                work_item_id="w1",
                status="needs_action",
                reason="末端規定の本文が未確認である。",
                basis_evidence_ids=("e1",),
            ),
        ),
    )

    assert decision.dependency_decisions[0].status == "needs_action"


def test_duplicate_load_evidence_ids_are_rejected_before_tool_execution() -> None:
    state = CaseState(
        case_id="case-load-duplicate",
        question="質問",
        research_cycle_count=1,
        work_items=(WorkItem(work_item_id="w1", question="確認事項"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="確認する命題",
            ),
        ),
        evidence=(
            Evidence(
                evidence_id="e1",
                source_ref="fixture:e1",
                content="本文",
                created_cycle=1,
            ),
        ),
    )
    decision = SolverDecision(
        next="continue",
        decision_reason="取得済み本文を再表示する。",
        tool_requests=(
            ToolRequest(
                request_id="load-1",
                work_item_id="w1",
                tool_name="load_evidence",
                arguments={"evidence_ids": ["e1", "e1"]},
                purpose="本文を再表示する。",
                hypothesis_ids=("h1",),
            ),
        ),
    )

    with pytest.raises(
        ContractViolation,
        match="load_evidence requires a non-empty unique evidence_ids list",
    ):
        apply_solver_decision(
            state,
            decision,
            limits=AgentLimits(),
            known_tool_names={"load_evidence"},
            material_evidence_ids=(),
            finalize_only=False,
        )


def test_observation_transport_deduplicates_load_evidence_ids() -> None:
    normalized = _normalize_observation_integration_payload(
        {
            "decision_reason": "本文を再表示する。",
            "update_hypotheses": [],
            "dependency_decisions": [],
            "tool_requests": [
                {
                    "request_id": "load-1",
                    "work_item_id": "w1",
                    "tool_name": "load_evidence",
                    "arguments": {"evidence_ids": ["e1", "e1", "e2"]},
                    "purpose": "本文を再表示する。",
                    "hypothesis_ids": ["h1"],
                }
            ],
        }
    )

    assert normalized["tool_requests"][0]["arguments"]["evidence_ids"] == [
        "e1",
        "e2",
    ]


def test_solver_contract_glossary_is_generated_from_field_descriptions() -> None:
    glossary = render_solver_contract_glossary()

    assert "`SolverContext.fetchable_article_ids`" in glossary
    assert "fetch_articlesに指定できるArticle ID" in glossary
    assert "`SolverDecision.retain_evidence_ids`" in glossary
    assert "同じIDは重複させず1回だけ指定" in glossary
    assert "`ToolRequest.arguments`" not in glossary
    assert "入れ子の出力項目はProvider schema" in glossary


@pytest.mark.parametrize("model_type", [SolverContext, SolverDecision])
def test_every_llm_visible_contract_field_has_a_description(model_type) -> None:
    schema = model_type.model_json_schema()
    missing: set[str] = set()

    def collect(value: object, path: str) -> None:
        if not isinstance(value, dict):
            return
        properties = value.get("properties", {})
        if isinstance(properties, dict):
            for field_name, field_schema in properties.items():
                field_path = f"{path}.{field_name}"
                if not isinstance(field_schema, dict) or not (
                    field_schema.get("description")
                    or field_schema.get("$ref")
                ):
                    missing.add(field_path)
        definitions = value.get("$defs", {})
        if isinstance(definitions, dict):
            for definition_name, definition in definitions.items():
                collect(definition, definition_name)

    collect(schema, model_type.__name__)
    assert missing <= {"SolverContext.used_tool_request_ids"}


def test_provider_update_schema_uses_the_canonical_field_descriptions() -> None:
    properties = _case_update_transport_schema()["properties"]
    work_item = properties["add_work_items"]["items"]["properties"]
    hypothesis = properties["add_hypotheses"]["items"]["properties"]
    impact = properties["impact_decisions"]["items"]["properties"]

    assert work_item["basis_hypothesis_ids"]["description"] == (
        contract_field_description(WorkItem, "basis_hypothesis_ids")
    )
    assert hypothesis["work_item_id"]["description"] == (
        contract_field_description(Hypothesis, "work_item_id")
    )
    assert impact["action"]["description"] == contract_field_description(
        WorkItemImpactDecision,
        "action",
    )


def test_provider_schema_explains_retained_evidence_uniqueness() -> None:
    context = build_solver_context(
        CaseState(case_id="case-retained-evidence", question="質問"),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )

    retained = _solver_compact_transport_schema(context)["properties"][
        "retain_evidence_ids"
    ]

    assert "同じIDは重複させず1回だけ指定" in retained["description"]


@pytest.mark.parametrize(
    "schema_factory",
    [
        _solver_compact_transport_schema,
        _solver_anthropic_transport_schema,
        _search_review_transport_schema,
        _search_reselection_transport_schema,
    ],
)
def test_provider_contract_schema_explains_every_visible_property(
    schema_factory,
) -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_cycle2_repeated_search_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    missing: set[str] = set()

    def collect(value: object, path: str) -> None:
        if not isinstance(value, dict):
            return
        properties = value.get("properties", {})
        if isinstance(properties, dict):
            for field_name, field_schema in properties.items():
                field_path = f"{path}.{field_name}" if path else field_name
                if not isinstance(field_schema, dict) or not (
                    field_schema.get("description")
                    or field_schema.get("$ref")
                ):
                    missing.add(field_path)
                collect(field_schema, field_path)
        collect(value.get("items"), f"{path}[]")
        for union_name in ("anyOf", "oneOf", "allOf"):
            union = value.get(union_name, ())
            if isinstance(union, list):
                for index, branch in enumerate(union):
                    collect(branch, f"{path}.{union_name}[{index}]")

    collect(schema_factory(context), "")
    assert missing == set()


def test_legal_tool_contracts_describe_input_and_result_semantics() -> None:
    definitions = (
        LegalSearchTool.definition,
        LegalFetchArticlesTool.definition,
        LegalGraphNeighborsTool.definition,
    )

    assert [item.name for item in definitions] == [
        "legal_search",
        "fetch_articles",
        "legal_graph_neighbors",
    ]
    for definition in definitions:
        assert definition.description
        assert definition.result_description
        schema_variants = definition.input_schema.get(
            "anyOf",
            [definition.input_schema],
        )
        assert all(
            variant["additionalProperties"] is False
            for variant in schema_variants
        )
        for variant in schema_variants:
            for property_schema in variant["properties"].values():
                assert property_schema["description"]


class FakeStructuredLLM:
    provider = "fake"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.payloads: list[dict[str, Any]] = [
            {
                "work_items": [{"question": "検証法の適用要件"}],
                "non_work_item_requirements": ["根拠条文を示す"],
            },
            {
                "hypotheses": [
                    {
                        "work_item_id": "wi-1",
                        "statement": "検証法は適用要件を定めている",
                        "gaps": ["適用要件の具体的内容"],
                    }
                ]
            },
            {
                "search_requests": [
                    {
                        "work_item_id": "wi-1",
                        "hypothesis_ids": ["h-1"],
                        "purpose": "適用要件を確認する",
                        "query": "検証法 適用要件",
                        "doc_types": ["law"],
                    }
                ]
            },
            {
                "selections": [
                    {
                        "article_id": "law-test-article-2",
                        "legal_function": "applicability",
                        "summary": "検証法の適用要件を定める",
                        "matched_hypothesis_ids": ["h-1"],
                        "reason": "検証法の要件を定める検索候補である",
                    }
                ],
                "reason": "適用要件を確認する候補を選別した",
            },
            {
                "decision_reason": "取得本文を適用要件の仮説へ反映した",
                "update_hypotheses": [
                    {
                        "hypothesis_id": "h-1",
                        "judgment": "supported",
                        "evidence_ids": ["law-test-article-2"],
                        "gaps": [],
                    }
                ],
                "dependency_decisions": [
                    {
                        "dependency_kind": "lower_norm",
                        "work_item_id": "wi-1",
                        "status": "not_required",
                        "reason": "質問に関係する下位規範への委任はない",
                        "basis_evidence_ids": ["law-test-article-2"],
                        "action_request_id": None,
                    }
                ],
                "tool_requests": [],
            },
            {
                "next": "finalize",
                "decision_reason": "要件本文と下位規範不要判断の根拠が揃ったため完了する",
                "update": {
                    "update_work_items": [
                        {
                            "work_item_id": "wi-1",
                            "state": "resolved",
                            "resolution": "本文を確認した",
                            "basis_hypothesis_ids": ["h-1"],
                        }
                    ],
                    "update_hypotheses": [
                        {
                            "hypothesis_id": "h-1",
                            "judgment": "supported",
                            "evidence_ids": ["law-test-article-2"],
                        }
                    ],
                },
                "answer": {
                    "text": "検証法第2条が要件を定めています。",
                    "citation_ids": ["law-test-article-2"],
                },
                "dependency_decisions": [
                    {
                        "dependency_kind": "lower_norm",
                        "work_item_id": "wi-1",
                        "status": "not_required",
                        "reason": "取得本文に質問の要件を下位規範へ委任する記載がない",
                        "basis_evidence_ids": ["law-test-article-2"],
                    }
                ],
            },
        ]

    def generate_structured_json(self, **kwargs: Any) -> StructuredJSONResult:
        self.calls.append(kwargs)
        decision = self.payloads.pop(0)
        schema_properties = kwargs["schema"]["properties"]
        if set(schema_properties) in (
            {"work_items", "non_work_item_requirements"},
            {"hypotheses"},
            {"search_requests"},
        ):
            return StructuredJSONResult(
                payload=decision,
                provider="fake",
                model=kwargs["model"],
                latencyMs=1,
                inputTokens=10,
                outputTokens=10,
            )
        if "update" in schema_properties:
            defaults = {
                "start_next_cycle": False,
                "next_focus_work_item_ids": [],
                "retain_evidence_ids": [],
                "review_finding_resolutions": [],
                "dependency_decisions": [],
                "graph_candidate_review": None,
                "frontier_re_adoptions": [],
                "deferred_frontier_resolutions": [],
                "unreviewed_graph_resolution": None,
                "tool_requests": [],
                "answer": None,
            }
            payload = {
                "next": decision["next"],
                "decision_reason": decision["decision_reason"],
                "update": decision["update"],
            }
            for name in kwargs["schema"]["required"]:
                if name in payload:
                    continue
                payload[name] = decision.get(name, defaults.get(name))
            return StructuredJSONResult(
                payload=payload,
                provider="fake",
                model=kwargs["model"],
                latencyMs=1,
                inputTokens=10,
                outputTokens=10,
            )
        if "next" not in schema_properties:
            return StructuredJSONResult(
                payload=decision,
                provider="fake",
                model=kwargs["model"],
                latencyMs=1,
                inputTokens=10,
                outputTokens=10,
            )
        return StructuredJSONResult(
            payload={
                "next": decision["next"],
                "decision_reason": decision["decision_reason"],
                "update_json": json.dumps(
                    decision.get("update", {}),
                    ensure_ascii=False,
                ),
                "next_focus_work_item_ids": decision.get(
                    "next_focus_work_item_ids", []
                ),
                "retain_evidence_ids": decision.get("retain_evidence_ids", []),
                "tool_requests_json": json.dumps(
                    decision.get("tool_requests", []),
                    ensure_ascii=False,
                ),
                "dependency_decisions": decision.get("dependency_decisions", []),
                "graph_candidate_review": decision.get("graph_candidate_review"),
                "search_candidate_review": decision.get("search_candidate_review"),
                "frontier_re_adoptions": decision.get("frontier_re_adoptions", []),
                "deferred_frontier_resolutions": decision.get(
                    "deferred_frontier_resolutions", []
                ),
                "unreviewed_graph_resolution": decision.get(
                    "unreviewed_graph_resolution"
                ),
                "answer": decision.get("answer"),
            },
            provider="fake",
            model=kwargs["model"],
            latencyMs=1,
            inputTokens=10,
            outputTokens=20,
        )


class FakeOpenSearch:
    def search(self, *args: Any) -> list[dict[str, Any]]:
        return [
            {
                "document": {
                    "contentUnitId": "law-test-article-2",
                    "articleContentUnitId": "law-test-article-2",
                    "documentId": "law-test",
                    "docType": "law",
                    "title": "検証法",
                    "heading": "第二条",
                    "text": "第二条 要件を定める。",
                }
            }
        ]

    def get_by_article_ids(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "contentUnitId": "law-test-article-2",
                "articleContentUnitId": "law-test-article-2",
                "documentId": "law-test",
                "docType": "law",
                "title": "検証法",
                "heading": "第二条",
                "text": "第二条 要件を定める。",
            }
        ]


class FakeGraph:
    pass


def test_new_framework_uses_legal_tool_and_skips_reviewer_by_default(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        legal_profiles.settings,
        "agent_framework_reviewer_enabled",
        False,
    )
    monkeypatch.setattr(
        legal_profiles.settings,
        "agent_framework_diagnostics_mode",
        "status",
    )
    monkeypatch.setattr(legal_profiles.settings, "eval_results_dir", tmp_path)
    llm = FakeStructuredLLM()
    service = LegalFrameworkAgentService(
        FakeOpenSearch(),
        FakeGraph(),
        llm,
    )

    response = service.answer(
        AnswerRequest(
            question="検証法の要件は何ですか",
            pattern="pattern_4_deepsearch",
        )
    )

    framework_trace = response.trace["agentFramework"]
    assert response.pattern == "agent_framework_v1"
    assert response.answer == "検証法第2条が要件を定めています。"
    assert response.citations[0].contentUnitId == "law-test-article-2"
    assert framework_trace["reviewerEnabled"] is False
    assert framework_trace["diagnosticsMode"] == "status"
    assert framework_trace["diagnosticsPath"].endswith(".jsonl")
    assert "answerStatus" not in framework_trace
    assert framework_trace["answerCompleteness"] == "complete"
    assert framework_trace["researchCycleCount"] == 1
    assert len(framework_trace["modelCalls"]) == 6
    assert [item["purpose"] for item in framework_trace["modelCalls"]] == [
        "research",
        "hypothesis_generation",
        "search_planning",
        "search_selection",
        "observation_integration",
        "integration",
    ]
    assert framework_trace["toolCalls"][0]["arguments"] == {
        "query": "検証法 適用要件",
        "doc_types": ["law"],
        "document_ids": [],
    }
    assert framework_trace["toolCalls"][0]["purpose"] == "適用要件を確認する"
    assert [item["tool_name"] for item in framework_trace["toolCalls"]] == [
        "legal_search",
        "fetch_articles",
    ]
    assert len(llm.calls) == 6
    assert "decision_json" not in llm.calls[0]["schema"]["properties"]
    assert set(llm.calls[0]["schema"]["properties"]) == {
        "work_items",
        "non_work_item_requirements",
    }
    assert set(llm.calls[1]["schema"]["properties"]) == {"hypotheses"}
    assert set(llm.calls[2]["schema"]["properties"]) == {"search_requests"}
    assert "question_requirement_checklist" not in llm.calls[0]["schema"][
        "properties"
    ]
    assert "dependency_decisions" not in llm.calls[0]["schema"]["properties"]
    assert "dependency_decisions_json" not in llm.calls[0]["schema"]["properties"]
    search_schema = llm.calls[3]["schema"]
    assert set(search_schema["properties"]) == {
        "selections",
        "reason",
    }
    selection_schema = search_schema["properties"]["selections"]
    assert selection_schema["maxItems"] == 5
    assert set(selection_schema["items"]["properties"]) == {
        "article_id",
        "legal_function",
        "summary",
        "matched_hypothesis_ids",
        "reason",
    }
    assert selection_schema["items"]["properties"]["summary"]["maxLength"] == 300
    assert selection_schema["items"]["properties"]["reason"]["maxLength"] == 300
    assert search_schema["properties"]["reason"]["maxLength"] == 400
    assert set(llm.calls[4]["schema"]["properties"]) == {
        "decision_reason",
        "update_hypotheses",
        "dependency_decisions",
        "tool_requests",
    }
    assert set(llm.calls[5]["schema"]["properties"]) == {
        "next",
        "decision_reason",
        "start_next_cycle",
        "update",
        "next_focus_work_item_ids",
        "retain_evidence_ids",
        "tool_requests",
        "answer",
    }
    assert llm.calls[5]["schema"]["properties"]["decision_reason"][
        "description"
    ] == contract_field_description(SolverDecision, "decision_reason")
    assert "未確認" in llm.calls[5]["schema"]["properties"]["update"][
        "properties"
    ]["update_hypotheses"]["items"]["properties"]["gaps"]["description"]
    final_dependency_schema = llm.calls[4]["schema"]["properties"][
        "dependency_decisions"
    ]
    assert final_dependency_schema["minItems"] == 1
    assert final_dependency_schema["maxItems"] == 1
    dependency_kind_schema = final_dependency_schema["items"]["properties"][
        "dependency_kind"
    ]
    assert dependency_kind_schema["type"] == "string"
    assert dependency_kind_schema["enum"] == ["lower_norm"]
    assert dependency_kind_schema["description"] == (
        contract_field_description(DependencyDecision, "dependency_kind")
    )
    assert framework_trace["dependencyDecisions"][0]["status"] == "not_required"
    assert framework_trace["elapsedMs"] >= 0
    assert len(framework_trace["appliedDecisionSequences"]) == 6
    diagnostic_records = [
        json.loads(record_line)
        for output_path in (tmp_path / "agent-framework-diagnostics").glob(
            "*.jsonl"
        )
        for record_line in output_path.read_text(encoding="utf-8").splitlines()
    ]
    assert diagnostic_records
    assert diagnostic_records[0]["event"] == "solver_input"
    assert "caseState" not in diagnostic_records[0]
    assert diagnostic_records[0]["profileName"] == "legal-default"
    assert diagnostic_records[0]["profileVersion"] == "430"
    assert diagnostic_records[0]["runElapsedMs"] >= 0
    assert diagnostic_records[0]["recordedAt"].endswith("+00:00")
    assert len(diagnostic_records[0]["questionHash"]) == 64
    transport_input = next(
        item for item in diagnostic_records if item["event"] == "transport_input"
    )
    assert len(transport_input["promptHash"]) == 64
    assert len(transport_input["schemaHash"]) == 64
    assert len(transport_input["systemPromptHash"]) == 64
    assert transport_input["profileName"] == "legal-default"
    assert transport_input["profileVersion"] == "430"
    assert transport_input["promptBuilder"].endswith(":_solver_prompt")
    assert transport_input["promptAssets"] == []
    assert len(transport_input["instructionsHash"]) == 64
    assert len(transport_input["inputHash"]) == 64
    assert len(transport_input["normalizedSchemaHash"]) == 64
    transport_output = next(
        item for item in diagnostic_records if item["event"] == "transport_output"
    )
    assert len(transport_output["payloadHash"]) == 64
    transport_stages = [
        item["transportStage"]
        for item in diagnostic_records
        if item["event"] == "transport_input"
    ]
    assert "search_selection" in transport_stages
    assert "search_actor_classification" not in transport_stages
    assert "search_assessment" not in transport_stages
    assert "search_reselection" not in transport_stages
    tool_execution = next(
        item for item in diagnostic_records if item["event"] == "tool_execution"
    )
    assert tool_execution["elapsedMs"] >= 0
    assert tool_execution["hypothesisIds"]
    solver_output = next(
        item for item in diagnostic_records if item["event"] == "solver_output"
    )
    assert len(solver_output["solverDecisionHash"]) == 64


def test_framework_diagnostics_off_avoids_detailed_output(monkeypatch) -> None:
    monkeypatch.setattr(
        legal_profiles.settings,
        "agent_framework_diagnostics_mode",
        "off",
    )
    service = LegalFrameworkAgentService(
        FakeOpenSearch(),
        FakeGraph(),
        FakeStructuredLLM(),
    )

    response = service.answer(
        AnswerRequest(
            question="検証法の要件は何ですか",
            pattern="pattern_4_deepsearch",
        )
    )

    framework_trace = response.trace["agentFramework"]
    assert framework_trace["diagnosticsMode"] == "off"
    assert "diagnosticsPath" not in framework_trace
    assert "workItems" not in framework_trace
    assert "hypotheses" not in framework_trace
    assert "dependencyDecisions" not in framework_trace


def test_framework_snapshot_diagnostics_preserve_full_solver_material(
    tmp_path,
) -> None:
    state = CaseState(case_id="case-snapshot", question="質問")
    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=100,
        finalize_only=False,
    )
    profile = ModelCallProfile(model="model", system_prompt="prompt")
    diagnostics = AgentDiagnostics(
        mode="snapshot",
        output_dir=tmp_path,
        case_id=state.case_id,
    )

    diagnostics.record_solver_input(
        state=state,
        context=context,
        profile=profile,
        purpose="integration",
        contract_attempt=0,
    )

    assert diagnostics.output_path is not None
    record = json.loads(
        diagnostics.output_path.read_text(encoding="utf-8").splitlines()[0]
    )
    assert record["stateStatus"]["runStatus"] == "running"
    assert record["caseState"]["case_id"] == "case-snapshot"
    assert record["solverContext"]["question"] == "質問"
    assert record["modelProfile"]["system_prompt"] == "prompt"


def test_reviewer_transport_records_full_view_and_both_contract_boundaries(
    tmp_path,
) -> None:
    class ReviewLLM:
        provider = "fake"

        def generate_structured_json(self, **kwargs: Any) -> StructuredJSONResult:
            assert "<reviewer_view>" in kwargs["prompt"]
            return StructuredJSONResult(
                payload={"verdict": "accept", "findings": []},
                provider="fake",
                model=kwargs["model"],
                latencyMs=1,
                inputTokens=10,
                outputTokens=5,
            )

    evidence = Evidence(
        evidence_id="e1",
        source_ref="fake://article-1",
        content="確認本文",
        created_cycle=1,
    )
    view = ReviewerView(
        case_id="case-review",
        question="質問",
        answer=FinalAnswer(text="回答", citation_ids=("e1",)),
        work_items=(),
        hypotheses=(),
        dependency_decisions=(),
        evidence=(evidence,),
    )
    diagnostics = AgentDiagnostics(
        mode="snapshot",
        output_dir=tmp_path,
        case_id=view.case_id,
        profile_name="legal-default",
        profile_version="100",
    )

    result = StructuredJSONModelAdapter(
        ReviewLLM(),
        diagnostics=diagnostics,
    ).review(
        view,
        ReviewerProfile(
            enabled=True,
            model="review-model",
            system_prompt="review system prompt",
        ),
    )

    assert result.review == ReviewResult(verdict="accept")
    assert diagnostics.output_path is not None
    records = [
        json.loads(line)
        for line in diagnostics.output_path.read_text(encoding="utf-8").splitlines()
    ]
    reviewer_input, reviewer_output = records
    assert reviewer_input["event"] == "reviewer_input"
    assert reviewer_input["reviewerView"]["evidence"][0]["evidence_id"] == "e1"
    assert len(reviewer_input["promptHash"]) == 64
    assert len(reviewer_input["schemaHash"]) == 64
    reviewer_complete = Path(reviewer_input["completeRequestPath"])
    assert reviewer_complete.is_file()
    assert json.loads(reviewer_complete.read_text(encoding="utf-8"))["prompt"] == (
        reviewer_input["prompt"]
    )
    assert reviewer_output["event"] == "reviewer_output"
    assert reviewer_output["verdict"] == "accept"
    assert reviewer_output["findingCount"] == 0
    assert len(reviewer_output["payloadHash"]) == 64
    assert len(reviewer_output["reviewResultHash"]) == 64


def test_solver_transport_requires_one_resolution_per_reviewer_finding() -> None:
    context = build_solver_context(
        CaseState(case_id="case-review-repair", question="質問"),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
        reviewer_findings=(
            ReviewFinding(
                finding_id="finding-1",
                kind="coverage_gap",
                description="観点が回答にない",
            ),
        ),
    )

    schema = _solver_transport_schema(context)
    resolutions = schema["properties"]["review_finding_resolutions"]

    assert resolutions["minItems"] == 1
    assert resolutions["maxItems"] == 1
    finding_id_schema = resolutions["items"]["properties"]["finding_id"]
    assert finding_id_schema["type"] == "string"
    assert finding_id_schema["enum"] == ["finding-1"]
    assert finding_id_schema["description"] == contract_field_description(
        ReviewFindingResolution,
        "finding_id",
    )
    assert "review_finding_resolutions" in schema["required"]


def test_solver_schema_and_prompt_require_a_concise_decision_reason() -> None:
    context = build_solver_context(
        CaseState(case_id="case-reason", question="質問"),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )

    schema = _solver_transport_schema(context)
    prompt = _solver_prompt(context, "system")

    assert "decision_reason" in schema["required"]
    assert "内部思考の逐語記録" in prompt


def test_framework_reviewer_setting_defaults_to_false() -> None:
    from app.config import settings

    assert settings.agent_framework_reviewer_enabled is False


def test_reviewer_and_solver_prompts_define_the_revision_handoff() -> None:
    profile = legal_profiles.legal_agent_profile()

    reviewer_prompt = profile.reviewer.system_prompt
    assert "# 法令回答Reviewer" in reviewer_prompt
    assert "## ReviewerViewの意味" in reviewer_prompt
    assert "## 検査順序" in reviewer_prompt
    assert "## Finding契約" in reviewer_prompt
    assert [line for line in reviewer_prompt.splitlines() if line.startswith("# ")] == [
        "# 法令回答Reviewer"
    ]
    assert "dependency_decisions.status" in reviewer_prompt
    assert "検索、Tool選択、CaseStateの変更は行いません" in reviewer_prompt

    assert profile.solver_reviewer_revision is not None
    solver_prompt = profile.solver_reviewer_revision.system_prompt
    assert "全Reviewer Findingを取得済み本文と照合" in solver_prompt
    assert "review_finding_resolutions" in solver_prompt
    assert "Findingを未処理のまま再度`finalize`しません" in solver_prompt


def test_dependency_audit_scope_uses_llm_tool_bindings_for_grounding_only() -> None:
    state = CaseState(
        case_id="case-1",
        question="質問",
        work_items=(
            WorkItem(work_item_id="w1", question="要件"),
            WorkItem(work_item_id="w2", question="例外"),
        ),
        hypotheses=(
            Hypothesis(hypothesis_id="h1", work_item_id="w1", statement="要件"),
            Hypothesis(hypothesis_id="h2", work_item_id="w2", statement="例外"),
        ),
        tool_requests=(
            ToolRequest(
                request_id="search-1",
                work_item_id="w1",
                tool_name="legal_search",
                purpose="候補検索",
                hypothesis_ids=("h1",),
            ),
            ToolRequest(
                request_id="fetch-1",
                work_item_id="w2",
                tool_name="fetch_articles",
                purpose="例外本文取得",
                hypothesis_ids=("h2",),
            ),
        ),
        tool_results=(
            ToolResult(
                request_id="search-1",
                status="succeeded",
                evidence_ids=("nav-1",),
                cycle_no=1,
            ),
            ToolResult(
                request_id="fetch-1",
                status="succeeded",
                evidence_ids=("body-1",),
                cycle_no=1,
            ),
        ),
        evidence=(
            Evidence(
                evidence_id="nav-1",
                source_ref="opensearch:nav-1",
                content="候補",
                created_cycle=1,
                metadata={"citationEligible": False},
            ),
            Evidence(
                evidence_id="body-1",
                source_ref="opensearch:body-1",
                content="本文",
                created_cycle=1,
                metadata={"citationEligible": True},
            ),
        ),
    )

    assert _dependency_audit_work_item_ids(state) == ("w2",)


def test_dependency_audit_scope_does_not_repeat_an_integrated_result() -> None:
    state = CaseState(
        case_id="case-1",
        question="質問",
        work_items=(WorkItem(work_item_id="w1", question="要件"),),
        hypotheses=(
            Hypothesis(hypothesis_id="h1", work_item_id="w1", statement="要件"),
        ),
        tool_requests=(
            ToolRequest(
                request_id="fetch-1",
                work_item_id="w1",
                tool_name="fetch_articles",
                purpose="本文取得",
                hypothesis_ids=("h1",),
            ),
        ),
        tool_results=(
            ToolResult(
                request_id="fetch-1",
                status="succeeded",
                evidence_ids=("body-1",),
                cycle_no=1,
            ),
        ),
        evidence=(
            Evidence(
                evidence_id="body-1",
                source_ref="opensearch:body-1",
                content="本文",
                created_cycle=1,
                metadata={"citationEligible": True},
            ),
        ),
        integrated_tool_result_request_ids=("fetch-1",),
        dependency_decisions=(
            DependencyDecision(
                dependency_kind="lower_norm",
                work_item_id="w1",
                status="not_required",
                reason="本文上、追加確認は不要である。",
                basis_evidence_ids=("body-1",),
            ),
        ),
    )

    assert _dependency_audit_work_item_ids(state) == ()
    assert _dependency_audit_scope(
        state,
        integration_call=True,
        finalize_only=False,
        required_dependency_kind="lower_norm",
    ) == ()


def test_dependency_audit_scope_keeps_an_unresolved_saved_dependency() -> None:
    state = CaseState(
        case_id="case-1",
        question="質問",
        work_items=(WorkItem(work_item_id="w1", question="要件"),),
        hypotheses=(
            Hypothesis(hypothesis_id="h1", work_item_id="w1", statement="要件"),
        ),
        dependency_decisions=(
            DependencyDecision(
                dependency_kind="lower_norm",
                work_item_id="w1",
                status="needs_action",
                reason="下位規範の本文確認が必要である。",
            ),
        ),
    )

    assert _dependency_audit_scope(
        state,
        integration_call=True,
        finalize_only=False,
        required_dependency_kind="lower_norm",
    ) == ("w1",)


def test_graph_review_paging_preserves_discovery_order_instead_of_hash_order() -> None:
    state = CaseState(
        case_id="case-1",
        question="質問",
        work_items=(WorkItem(work_item_id="w1", question="確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="関連Articleを確認する",
            ),
        ),
    )
    catalog = GraphCandidateCatalog(
        articles=(
            GraphCandidateArticle(
                article_id="article-b",
                document_id=None,
                title=None,
                heading=None,
                content_status="not_requested",
            ),
            GraphCandidateArticle(
                article_id="article-a",
                document_id=None,
                title=None,
                heading=None,
                content_status="not_requested",
            ),
        ),
        links=(
            GraphCandidateLink(
                link_id="link-b",
                seed_article_id="seed",
                candidate_article_id="article-b",
                work_item_ids=("w1",),
                hypothesis_ids=("h1",),
                relations=(),
                graph_request_ids=("graph-1",),
            ),
            GraphCandidateLink(
                link_id="link-a",
                seed_article_id="seed",
                candidate_article_id="article-a",
                work_item_ids=("w1",),
                hypothesis_ids=("h1",),
                relations=(),
                graph_request_ids=("graph-1",),
            ),
        ),
    )

    batch, _ = _graph_review_projection(state, catalog, max_candidates=2)

    assert [item.article_id for item in batch.candidates] == [
        "article-b",
        "article-a",
    ]


def test_graph_review_moves_to_another_hypothesis_after_integrated_fetch() -> None:
    state = CaseState(
        case_id="case-1",
        question="質問",
        research_cycle_count=1,
        work_items=(WorkItem(work_item_id="w1", question="確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="条件を確認する",
            ),
            Hypothesis(
                hypothesis_id="h2",
                work_item_id="w1",
                statement="手続を確認する",
            ),
        ),
        tool_requests=(
            ToolRequest(
                request_id="graph-review-fetch-completed",
                work_item_id="w1",
                tool_name="fetch_articles",
                arguments={"article_ids": ["article-h1-a"]},
                purpose="Graph候補本文を取得する",
                hypothesis_ids=("h1",),
            ),
        ),
        tool_results=(
            ToolResult(
                request_id="graph-review-fetch-completed",
                status="succeeded",
                evidence_ids=("evidence-h1",),
                cycle_no=1,
            ),
        ),
        integrated_tool_result_request_ids=("graph-review-fetch-completed",),
    )
    catalog = GraphCandidateCatalog(
        articles=tuple(
            GraphCandidateArticle(
                article_id=article_id,
                document_id=None,
                title=None,
                heading=None,
                content_status="not_requested",
            )
            for article_id in ("article-h1-a", "article-h1-b", "article-h2")
        ),
        links=(
            GraphCandidateLink(
                link_id="link-h1-a",
                seed_article_id="seed",
                candidate_article_id="article-h1-a",
                work_item_ids=("w1",),
                hypothesis_ids=("h1",),
                relations=(),
                graph_request_ids=("graph-h1",),
            ),
            GraphCandidateLink(
                link_id="link-h1-b",
                seed_article_id="seed",
                candidate_article_id="article-h1-b",
                work_item_ids=("w1",),
                hypothesis_ids=("h1",),
                relations=(),
                graph_request_ids=("graph-h1",),
            ),
            GraphCandidateLink(
                link_id="link-h2",
                seed_article_id="seed",
                candidate_article_id="article-h2",
                work_item_ids=("w1",),
                hypothesis_ids=("h2",),
                relations=(),
                graph_request_ids=("graph-h2",),
            ),
        ),
    )

    current_batch, _ = _graph_review_projection(
        state,
        catalog,
        max_candidates=2,
    )
    next_cycle_batch, _ = _graph_review_projection(
        state.model_copy(update={"research_cycle_count": 2}),
        catalog,
        max_candidates=2,
    )

    assert [item.article_id for item in current_batch.candidates] == ["article-h2"]
    assert current_batch.remaining_unreviewed_count == 2
    assert [item.article_id for item in next_cycle_batch.candidates] == [
        "article-h1-a",
        "article-h1-b",
    ]
    assert next_cycle_batch.remaining_unreviewed_count == 1


def test_legal_solver_prompts_are_projected_by_structural_mode() -> None:
    profile = legal_profiles.legal_agent_profile()

    assert profile.version == "430"
    assert profile.solver_graph_review is not None
    assert profile.solver_graph_review.max_output_tokens == (
        profile.solver_integration.max_output_tokens
    )
    mode_prompts = {
        "research": profile.solver_research.system_prompt,
        "integration": profile.solver_integration.system_prompt,
        "cycle_close": profile.solver_cycle_close.system_prompt,
        "finalization": profile.solver_finalization.system_prompt,
        "reviewer_revision": profile.solver_reviewer_revision.system_prompt,
    }
    assert all(prompt is not None for prompt in mode_prompts.values())
    completion_checks = {
        "research": profile.solver_research.completion_check_prompt,
        "integration": profile.solver_integration.completion_check_prompt,
        "cycle_close": profile.solver_cycle_close.completion_check_prompt,
        "finalization": profile.solver_finalization.completion_check_prompt,
        "reviewer_revision": (
            profile.solver_reviewer_revision.completion_check_prompt
        ),
    }
    assert all(check is not None for check in completion_checks.values())
    for prompt in (
        mode_prompts["integration"],
        mode_prompts["reviewer_revision"],
    ):
        assert [line for line in prompt.splitlines() if line.startswith("# ")] == [
            "# 法令調査Solver"
        ]
        assert "Programへ意味判断" in prompt
        assert "現在のCycleを閉じて次Cycleへ移る場合だけ`true`" in prompt
        assert "初回ToolでCycle 1" not in prompt
        assert "action-observation step" not in prompt
        assert "## 出力前の確認" not in prompt

    finalization_prompt = mode_prompts["finalization"]
    assert [
        line for line in finalization_prompt.splitlines() if line.startswith("# ")
    ] == ["# 法令調査Solver"]
    assert "Programへ意味判断" in finalization_prompt
    assert "`citation_ids`には`grounding_evidence_ids`" in finalization_prompt
    assert "## Solver共通ルール" not in finalization_prompt
    assert "## 調査の完了ルール" not in finalization_prompt

    research_prompt = mode_prompts["research"]
    hypothesis_prompt = profile.solver_hypothesis_generation.system_prompt
    search_prompt = profile.solver_search_planning.system_prompt
    assert profile.solver_research.context_projection == "research_decomposition"
    assert profile.solver_hypothesis_generation.context_projection == (
        "research_hypothesis"
    )
    assert profile.solver_search_planning.context_projection == "research_search"
    assert profile.solver_integration.context_projection == "full"
    assert profile.solver_cycle_close.context_projection == "cycle_close"
    assert "# 法令調査Solver：質問の要求分解" in research_prompt
    assert "non_work_item_requirements" in research_prompt
    assert "法令本文を調べる必要がある要求" in research_prompt
    assert "複数の法的論点がある場合は、WorkItemを分けます" in research_prompt
    assert "質問が上位概念だけを示す場合" in research_prompt
    assert "根拠条文・出典・引用の提示" in research_prompt
    assert "明確でない場合は、推測せず`不明`" in research_prompt
    assert "# 法令調査Solver：法的仮説の立案" in hypothesis_prompt
    assert "WorkItem自体に独立した確認事項が複数ある場合だけ" in (
        hypothesis_prompt
    )
    assert "WorkItemにない行為者" in hypothesis_prompt
    assert "`gaps`" in hypothesis_prompt
    assert "質問にない判定軸や基準" in hypothesis_prompt
    assert "数値又は条文番号" in hypothesis_prompt
    assert "# 法令調査Solver：検索要求の作成" in search_prompt
    assert "legal_search" in search_prompt
    assert "`gaps`を未確認事項として読み分けます" in search_prompt
    assert "別々の検索にすることは強制しません" in search_prompt
    assert "WorkItemにない判定軸" in search_prompt
    assert "Hypothesisまたは`gaps`を検証" not in search_prompt
    assert (
        profile.limits.max_graph_articles_per_hypothesis_per_cycle == 1
    )
    for prompt in (research_prompt, hypothesis_prompt, search_prompt):
        assert "## 共通ルール" not in prompt
        assert "## 現在の作業" not in prompt
        assert "Tool結果を受け取った後" not in prompt

    integration_prompt = mode_prompts["integration"]
    assert "## Tool選択ルール" in integration_prompt
    assert "## Tool結果の統合と次の行動" in integration_prompt
    assert integration_prompt.index("## Tool結果の統合と次の行動") < (
        integration_prompt.index("## Solver共通ルール")
    )
    assert "新しいTool結果と取得本文を調査状態へ反映" in (
        integration_prompt
    )
    assert "### 手順" in integration_prompt
    assert "### 判断ルール" in integration_prompt
    assert "Toolは固定順で使いません" in integration_prompt
    assert "## 調査の完了ルール" in integration_prompt
    assert "## 現在の作業：Cycle Close" not in integration_prompt
    assert "## 現在の作業：Reviewer Revision" not in integration_prompt
    assert "`search_candidates`とGraph候補はArticleを発見" in integration_prompt
    assert "未確認事項を最も直接検証できる行動" in integration_prompt
    assert "同一Decisionで複数Articleの本文を取得" in integration_prompt
    assert "次に取得するArticle IDではありません" in integration_prompt
    assert "項目の意味は`contract_glossary`を正本" in integration_prompt
    assert "Article IDとEvidence IDを混同しません" in integration_prompt

    cycle_close_prompt = mode_prompts["cycle_close"]
    assert "# 法令調査Solver：取得本文の統合" in cycle_close_prompt
    assert "取得した法令本文を既存Hypothesisへ反映" in (
        cycle_close_prompt
    )
    assert "Cycle移行と最終回答も出力しません" in cycle_close_prompt
    assert "grounding_evidence" in cycle_close_prompt
    assert "下位規範へ委ねられた具体的内容" in cycle_close_prompt
    assert "同じArticleの別の要件又は例外" in cycle_close_prompt
    assert "Hypothesisが確認する規律と法的効果" in cycle_close_prompt
    dependency_prompt = profile.solver_cycle_close.dependency_system_prompt
    assert dependency_prompt is not None
    assert "起点規範から末端規範までの本文" in dependency_prompt
    assert "中間規範がさらに下位規範へ委ねている場合" in dependency_prompt
    assert "各下位規範の条番号" in dependency_prompt
    assert "別の行為、段階又は手続" in dependency_prompt
    assert "その委任事項を" in dependency_prompt
    assert "同じArticleにある別の項目" in dependency_prompt
    assert "fetchable_article_ids" in cycle_close_prompt
    assert profile.solver_cycle_close.followup_system_prompt is not None
    transition_prompt = profile.solver_cycle_close.followup_system_prompt
    assert "# 法令調査Solver：Cycleの終了判断" in transition_prompt
    assert "本文の再評価、状態更新、Tool選択、遷移の変更は行いません" in (
        transition_prompt
    )
    assert "`required_transition`に従っている" in transition_prompt
    assert "retainable_evidence" in transition_prompt
    assert "fetchable_article_ids" not in transition_prompt

    finalization_prompt = mode_prompts["finalization"]
    assert "## 実行上限での最終化" in finalization_prompt
    assert "追加調査できない実行上限時" in finalization_prompt
    assert "finalize_only=true" in finalization_prompt
    assert "## Tool選択ルール" not in finalization_prompt

    reviewer_prompt = mode_prompts["reviewer_revision"]
    assert "## Reviewer指摘への対応" in reviewer_prompt
    assert "Reviewerの指摘を受け取ったSolver" in reviewer_prompt
    assert "review_finding_resolutions" in reviewer_prompt
    assert "## Tool選択ルール" in reviewer_prompt

    assert len(research_prompt) < 15000
    assert len(integration_prompt) < 18000
    assert len(cycle_close_prompt) < 5000
    assert len(transition_prompt) < 5000
    assert len(finalization_prompt) < 7000
    assert len(reviewer_prompt) < 12000

    assert profile.automatic_tools == ()
    assert len(profile.tool_list_argument_limits) == 2
    assert profile.graph_review_fetch_tool_name == "fetch_articles"
    assert profile.required_dependency_kind == "lower_norm"


def test_staged_research_preserves_non_work_requirements_and_links_hypotheses() -> None:
    limits = AgentLimits()
    state = CaseState(
        case_id="case-staged-research",
        question="適用条件と例外を根拠条文とともに説明してください。",
    )
    context = build_solver_context(
        state,
        limits,
        remaining_wall_time_sec=120,
        finalize_only=False,
    )
    decomposition = SolverDecision.model_validate(
        _normalize_staged_research_payload(
            {
                "work_items": [
                    {"question": "適用条件"},
                    {"question": "例外"},
                ],
                "non_work_item_requirements": ["根拠条文を示す"],
            },
            projection="research_decomposition",
            context=context,
        )
    )
    state = apply_solver_decision(
        state,
        decomposition,
        limits=limits,
        known_tool_names={"legal_search"},
        material_evidence_ids=(),
        finalize_only=False,
    )
    assert state.non_work_item_requirements == ("根拠条文を示す",)
    assert [item.work_item_id for item in state.work_items] == ["wi-1", "wi-2"]

    context = build_solver_context(
        state,
        limits,
        remaining_wall_time_sec=120,
        finalize_only=False,
    )
    hypothesis_decision = SolverDecision.model_validate(
        _normalize_staged_research_payload(
            {
                "hypotheses": [
                    {
                        "work_item_id": "wi-1",
                        "statement": "一定の主体と行為に適用要件が課される",
                        "gaps": ["主体と行為の具体的範囲"],
                    },
                    {
                        "work_item_id": "wi-2",
                        "statement": "一定の取引類型は適用から除外される",
                        "gaps": ["除外対象となる取引類型"],
                    },
                ]
            },
            projection="research_hypothesis",
            context=context,
        )
    )
    state = apply_solver_decision(
        state,
        hypothesis_decision,
        limits=limits,
        known_tool_names={"legal_search"},
        material_evidence_ids=(),
        finalize_only=False,
    )
    assert [(item.hypothesis_id, item.work_item_id) for item in state.hypotheses] == [
        ("h-1", "wi-1"),
        ("h-2", "wi-2"),
    ]


def test_research_single_completion_unit_fixture_applies_without_grouping() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_research_single_completion_unit_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    profile = legal_profiles.legal_agent_profile()
    initial = CaseState(
        case_id="fixture-tob-research-single-completion-unit-v1",
        question=fixture["question"],
    )
    context = build_solver_context(
        initial,
        profile.limits,
        remaining_wall_time_sec=120,
        finalize_only=False,
    )
    research = profile.solver_research
    assert research is not None
    assert research.completion_check_prompt is not None

    prompt = _solver_prompt(
        context,
        research.system_prompt,
        completion_check_prompt=research.completion_check_prompt,
        compact_transport=True,
    )
    decision = SolverDecision.model_validate(fixture["solverDecision"])
    updated = apply_solver_decision(
        initial,
        decision,
        limits=profile.limits,
        known_tool_names={"legal_search"},
        material_evidence_ids=(),
        finalize_only=False,
    )

    expected = fixture["expectedCompletionUnits"]
    assert fixture["profileVersion"] == "154"
    assert profile.version == "430"
    assert prompt.rindex("## 出力") > prompt.rindex(
        "</solver_context>"
    )
    assert [item.work_item_id for item in updated.work_items] == [
        item["workItemId"] for item in expected
    ]
    assert [item.question for item in updated.work_items] == [
        item["question"] for item in expected
    ]
    assert len(updated.work_items) == len(updated.hypotheses) == 4
    assert {
        item.work_item_id: item.hypothesis_id for item in updated.hypotheses
    } == {
        "wi-condition": "h-condition",
        "wi-scope": "h-scope",
        "wi-exception": "h-exception",
        "wi-procedure": "h-procedure",
    }
    assert all(item.basis_hypothesis_ids == () for item in updated.work_items)
    assert {item.work_item_id for item in updated.tool_requests} == {
        item.work_item_id for item in updated.work_items
    }
    assert all(len(item.hypothesis_ids) == 1 for item in updated.tool_requests)


def test_overtime_hypothesis_gap_failure_fixture_tracks_the_contract_fix() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "overtime_initial_research_hypothesis_gap_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    observed = fixture["observedResearchOutput"]
    assessment = fixture["manualAssessment"]
    accepted = fixture["acceptedExample"]["hypotheses"]
    validated = fixture["validatedHaikuOutput"]
    profile = legal_profiles.legal_agent_profile()
    research = profile.solver_research
    assert research is not None
    assert research.completion_check_prompt is not None

    observed_work_item_ids = {
        item["workItemId"] for item in observed["workItems"]
    }
    observed_hypothesis_ids = {
        item["hypothesisId"] for item in observed["hypotheses"]
    }

    assert fixture["source"]["profileVersion"] == "149"
    assert profile.version == "430"
    assert assessment["workItems"] == "pass"
    assert assessment["hypotheses"] == "fail"
    assert assessment["gaps"] == "fail"
    assert set(assessment["invalidHypothesisIds"]) == observed_hypothesis_ids
    assert set(assessment["invalidGapHypothesisIds"]) == observed_hypothesis_ids
    assert all(
        item["workItemId"] in observed_work_item_ids for item in accepted
    )
    assert {
        item["hypothesisId"] for item in accepted
    } == observed_hypothesis_ids
    assert validated["provider"] == "anthropic"
    assert validated["model"] == "claude-haiku-4-5-20251001"
    assert validated["attemptCount"] == 1
    validated_work_item_ids = {
        item["workItemId"] for item in validated["workItems"]
    }
    assert len(validated_work_item_ids) == 3
    assert len(validated["hypotheses"]) == 3
    assert all(
        item["workItemId"] in validated_work_item_ids
        for item in validated["hypotheses"]
    )
    assert len(validated["searchQueries"]) == len(validated_work_item_ids)
    assert fixture["regressionHistory"][-1]["assessment"] == "pass"

    hypothesis_profile = profile.solver_hypothesis_generation
    assert hypothesis_profile is not None
    research_prompt = hypothesis_profile.system_prompt
    completion_prompt = hypothesis_profile.completion_check_prompt
    assert completion_prompt is not None
    assert "WorkItem自体に独立した確認事項が複数ある場合だけ" in (
        research_prompt
    )
    assert "WorkItemにない行為者" in research_prompt
    assert "`gaps`" in research_prompt
    assert "数値又は条文番号" in research_prompt
    assert "暫定的な法的効果を示す1つの文" in completion_prompt

    update_schema = _case_update_transport_schema()
    hypothesis_schema = update_schema["properties"]["add_hypotheses"]["items"]
    statement_description = hypothesis_schema["properties"]["statement"][
        "description"
    ]
    gaps_description = hypothesis_schema["properties"]["gaps"]["description"]
    assert "誤り得る1つの法的命題" in statement_description
    assert "法令本文によって個別に支持または否定" in statement_description
    assert "WorkItemの言い換えだけにはせず" in statement_description
    assert "該当する要素がなければ空" in gaps_description
    assert "根拠条文、検索語、検索作業" in gaps_description


def test_real_research_failure_fixture_is_rebuilt_with_corrected_prompt() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_research_grouped_work_item_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState.model_validate(fixture["caseState"])
    saved = SolverContext.model_validate(fixture["solverContext"])
    limits = AgentLimits(
        max_research_cycles=(
            saved.research_cycle_count + saved.remaining_research_cycles
        ),
        max_tool_requests_per_step=saved.max_tool_requests_per_step,
        max_fetched_resources_per_cycle=saved.max_fetched_resources_per_cycle,
        max_selected_frontier_per_step=saved.max_selected_frontier_per_step,
        max_retained_evidence=saved.max_retained_evidence,
        max_material_evidence_chars=saved.max_material_evidence_chars,
        max_solver_input_chars=saved.max_solver_input_chars,
        min_next_cycle_budget_sec=saved.min_next_cycle_budget_sec,
        max_wall_time_sec=240,
    )
    rebuilt = build_solver_context(
        state,
        limits,
        remaining_wall_time_sec=saved.remaining_wall_time_sec,
        finalize_only=saved.finalize_only,
    )
    profile = legal_profiles.legal_agent_profile().solver_research
    assert profile is not None
    assert profile.completion_check_prompt is not None
    prompt = _solver_prompt(
        rebuilt,
        profile.system_prompt,
        completion_check_prompt=profile.completion_check_prompt,
        compact_transport=True,
    )
    observed = fixture["observedFailure"]

    assert rebuilt.model_dump(mode="json") == saved.model_copy(
        update={"completed_graph_searches": rebuilt.completed_graph_searches}
    ).model_dump(mode="json")
    assert observed["profileVersion"] == "126"
    assert len(observed["workItems"]) == 1
    assert "対象となる株券等の範囲、主な例外、必要な手続" in (
        observed["workItems"][0]["question"]
    )
    assert "複数の法的論点がある場合は、WorkItemを分けます" in prompt
    assert "元の質問に漏れ、重複、追加がない" in prompt
    assert "必要条件、対象範囲、例外、手続" not in prompt
    assert prompt.rindex("## 出力") > prompt.rindex(
        "</solver_context>"
    )


def test_research_missing_main_request_fixture_is_covered_by_prompt() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_research_missing_main_request_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    saved = SolverContext.model_validate(fixture["solverContext"])
    profile = legal_profiles.legal_agent_profile().solver_research
    assert profile is not None
    assert profile.completion_check_prompt is not None
    prompt = _solver_prompt(
        saved,
        profile.system_prompt,
        completion_check_prompt=profile.completion_check_prompt,
        compact_transport=True,
    )
    observed = fixture["observedFailure"]

    assert observed["profileVersion"] == "127"
    assert len(observed["workItemQuestions"]) == 3
    assert observed["missingRequest"] not in observed["decisionReason"]
    assert "元の質問から、明示された要求を取り出します" in prompt
    assert "法令上の条件、範囲、例外、手続等の内容" in prompt
    assert "元の質問に漏れ、重複、追加がない" in prompt


def test_search_review_duplicate_fixture_uses_article_keyed_assessments() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_search_review_duplicate_assessment_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    candidate_ids = fixture["candidateArticleIds"]
    context = build_solver_context(
        CaseState(case_id="case-search-checklist", question="質問"),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    ).model_copy(
        update={
            "required_search_review_request_ids": tuple(
                fixture["requiredSearchRequestIds"]
            ),
            "search_candidates": tuple(
                SearchCandidateArticle(
                    article_id=article_id,
                    document_id=None,
                    title=None,
                    headings=(),
                    discovery_work_item_ids=(),
                    discovery_hypothesis_ids=(),
                    search_request_ids=tuple(
                        fixture["requiredSearchRequestIds"]
                    ),
                    navigation_evidence_ids=(),
                )
                for article_id in candidate_ids
            ),
        }
    )
    profile = legal_profiles.legal_agent_profile().solver_search_review
    assert profile is not None
    assert profile.completion_check_prompt is not None
    rendered = render_search_assessment_model_call(context, profile)
    prompt = rendered.request
    observed_payload = {
        "search_request_ids": fixture["requiredSearchRequestIds"],
        "assessments": [
            {
                "article_id": article_id,
                "legal_function": "scope",
                "summary": "要約",
            }
            for article_id in fixture["observedAssessmentArticleIds"]
        ],
        "reason": "全候補を確認した",
    }

    assessment_schema = rendered.output_schema["properties"]["assessments"]
    assert list(assessment_schema["properties"]) == candidate_ids
    assert assessment_schema["required"] == candidate_ids
    assert "candidate_checklist" not in rendered.input_payload
    assert profile.completion_check_prompt in prompt
    assert prompt.index(profile.completion_check_prompt) > prompt.index(
        "</solver_context>"
    )
    with pytest.raises(ModelProtocolError, match=fixture["expectedViolation"]):
        _validate_search_assessment_payload(observed_payload, context)
    normalized = _normalize_search_assessment_transport_payload(
        {
            "assessments": {
                article_id: {
                    "legal_function": "scope",
                    "summary": "要約",
                    "matched_hypothesis_ids": [],
                }
                for article_id in candidate_ids
            },
        },
        context,
    )
    assert [item["article_id"] for item in normalized["assessments"]] == (
        candidate_ids
    )
    _validate_search_assessment_payload(normalized, context)


def test_real_search_assessment_duplicate_is_preserved_as_fixture() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_search_assessment_duplicate_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    observed = fixture["observedTransportOutput"]["payload"]

    with pytest.raises(
        ModelProtocolError,
        match="search assessments must be unique",
    ):
        _validate_search_assessment_payload(observed, context)

    profile = legal_profiles.legal_agent_profile().solver_search_review
    assert profile is not None
    rendered = render_search_assessment_model_call(context, profile)
    candidate_ids = [item.article_id for item in context.search_candidates]
    assessment_schema = rendered.output_schema["properties"]["assessments"]
    assert assessment_schema["type"] == "object"
    assert assessment_schema["additionalProperties"] is False
    assert list(assessment_schema["properties"]) == candidate_ids
    assert assessment_schema["required"] == candidate_ids
    assert "article_id" not in rendered.output_schema["$defs"][
        "search_candidate_assessment"
    ]["properties"]


def test_research_missing_procedure_fixture_distinguishes_condition_and_action(
) -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_research_missing_procedure_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    saved = SolverContext.model_validate(fixture["solverContext"])
    profile = legal_profiles.legal_agent_profile().solver_research
    assert profile is not None
    assert profile.completion_check_prompt is not None
    prompt = _solver_prompt(
        saved,
        profile.system_prompt,
        completion_check_prompt=profile.completion_check_prompt,
        compact_transport=True,
    )
    observed = fixture["observedFailure"]

    assert observed["profileVersion"] == "128"
    assert len(observed["workItemQuestions"]) == 3
    assert observed["missingRequest"] not in observed["decisionReason"]
    assert "複数の法的論点がある場合は、WorkItemを分けます" in prompt
    assert "法的論点を自然言語の問いとして1件" in prompt


def test_search_reselection_underselect_fixture_checks_remaining_hypotheses(
) -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_search_reselection_underselect_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    work_items = tuple(
        WorkItem(
            work_item_id=item["hypothesisId"].replace("h-", "w-"),
            question=item["statement"],
        )
        for item in fixture["hypotheses"]
    )
    hypotheses = tuple(
        Hypothesis(
            hypothesis_id=item["hypothesisId"],
            work_item_id=item["hypothesisId"].replace("h-", "w-"),
            statement=item["statement"],
        )
        for item in fixture["hypotheses"]
    )
    context = build_solver_context(
        CaseState(
            case_id="case-reselection-underselect",
            question="質問",
            work_items=work_items,
            hypotheses=hypotheses,
        ),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    ).model_copy(
        update={"remaining_fetch_capacity": fixture["remainingFetchCapacity"]}
    )
    profile = legal_profiles.legal_agent_profile().solver_search_review
    assert profile is not None
    assert profile.followup_system_prompt is not None
    assert profile.followup_completion_check_prompt is not None
    prompt = _search_reselection_prompt(
        context,
        {
            "assessments": [
                {
                    "article_id": item["articleId"],
                    "legal_function": item["legalFunction"],
                    "summary": item["summary"],
                }
                for item in fixture["assessments"]
            ]
        },
        profile.followup_system_prompt,
        completion_check_prompt=profile.followup_completion_check_prompt,
    )

    assert len(fixture["observedSelections"]) < fixture["expectedSelectionCount"]
    assert "同じHypothesisの候補を複数選ぶ前に" in prompt
    assert "直接検証できる候補がある未確認Hypothesis" in prompt
    assert "`matched_hypothesis_ids`には、その候補で直接検証する" in prompt
    assert "検索抜粋だけで確定" in prompt
    assert "selected / deferred" not in prompt


def test_search_reselection_rejects_candidate_without_hypothesis_match() -> None:
    context = build_solver_context(
        CaseState(
            case_id="case-no-match",
            question="質問",
            work_items=(WorkItem(work_item_id="w-1", question="確認事項"),),
            hypotheses=(
                Hypothesis(
                    hypothesis_id="h-1",
                    work_item_id="w-1",
                    statement="命題",
                ),
            ),
        ),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    ).model_copy(
        update={
            "search_candidates": (
                SearchCandidateArticle(
                    article_id="article-other-actor",
                    document_id=None,
                    title=None,
                    headings=(),
                    discovery_work_item_ids=("w-1",),
                    discovery_hypothesis_ids=("h-1",),
                    search_request_ids=("search-1",),
                    navigation_evidence_ids=(),
                ),
            )
        }
    )

    with pytest.raises(
        ModelProtocolError,
        match="must be known and non-empty",
    ):
        _validate_search_reselection_payload(
            {
                "selections": [
                    {
                        "article_id": "article-other-actor",
                        "reason": "同じ法的機能である",
                    }
                ]
            },
            context,
        )


def test_search_reselection_collapses_duplicate_article_transport_entries() -> None:
    context = build_solver_context(
        CaseState(
            case_id="case-duplicate-selection",
            question="質問",
            work_items=(WorkItem(work_item_id="w-1", question="確認事項"),),
            hypotheses=(
                Hypothesis(hypothesis_id="h-1", work_item_id="w-1", statement="命題1"),
                Hypothesis(hypothesis_id="h-2", work_item_id="w-1", statement="命題2"),
            ),
        ),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    ).model_copy(
        update={
            "search_candidates": (
                SearchCandidateArticle(
                    article_id="article-1",
                    document_id=None,
                    title=None,
                    headings=(),
                    discovery_work_item_ids=("w-1",),
                    discovery_hypothesis_ids=("h-1", "h-2"),
                    search_request_ids=("search-1",),
                    navigation_evidence_ids=(),
                ),
            )
        }
    )
    normalized = _normalize_search_reselection_transport_payload(
        {
            "selections": [
                {
                    "article_id": "article-1",
                    "reason": "h-1を確認する",
                    "matched_hypothesis_ids": ["h-1"],
                },
                {
                    "article_id": "article-1",
                    "reason": "h-2を確認する",
                    "matched_hypothesis_ids": ["h-2"],
                },
            ],
            "reason": "同じArticleを二つの命題に使用する",
        }
    )

    assert normalized["selections"] == [
        {
            "article_id": "article-1",
            "reason": "h-1を確認する",
            "matched_hypothesis_ids": ["h-1", "h-2"],
        }
    ]
    _validate_search_reselection_payload(normalized, context)


def test_search_selection_collapses_duplicate_article_transport_entries() -> None:
    context = build_solver_context(
        CaseState(
            case_id="case-duplicate-combined-selection",
            question="質問",
            work_items=(WorkItem(work_item_id="w-1", question="確認事項"),),
            hypotheses=(
                Hypothesis(hypothesis_id="h-1", work_item_id="w-1", statement="命題1"),
                Hypothesis(hypothesis_id="h-2", work_item_id="w-1", statement="命題2"),
            ),
        ),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    normalized = _normalize_search_selection_transport_payload(
        {
            "selections": [
                {
                    "article_id": "article-1",
                    "legal_function": "applicability",
                    "summary": "適用条件を定める。",
                    "matched_hypothesis_ids": ["h-1"],
                    "reason": "h-1を確認できる。",
                },
                {
                    "article_id": "article-1",
                    "legal_function": "scope",
                    "summary": "対象範囲も定める。",
                    "matched_hypothesis_ids": ["h-2"],
                    "reason": "h-2を確認できる。",
                },
            ],
            "reason": "同じArticleが二つの命題に対応する。",
        },
        context,
    )

    assert normalized["selections"] == [
        {
            "article_id": "article-1",
            "reason": "h-1を確認できる。 h-2を確認できる。",
            "matched_hypothesis_ids": ["h-1", "h-2"],
        }
    ]
    assert normalized["assessments"] == [
        {
            "article_id": "article-1",
            "legal_function": "applicability",
            "summary": "適用条件を定める。 対象範囲も定める。",
            "matched_hypothesis_ids": ["h-1", "h-2"],
        }
    ]
    _validate_selected_search_assessments(normalized, context)


def test_cross_domain_actor_object_fixture_uses_semantic_match_not_domain_terms() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "cross_domain_actor_object_selection_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    profile = legal_profiles.legal_agent_profile().solver_search_review
    assert profile is not None
    assert profile.system_prompt is not None

    prompt = profile.followup_system_prompt
    assert prompt is not None
    assert "行為と規律" in prompt
    assert "株式" not in prompt
    assert "買付け" not in prompt
    assert "公開買付け" not in prompt

    verification = fixture["realModelVerification"]
    assert verification["model"] == "gpt-4o-mini-2024-07-18"
    assert verification["validationError"] is None
    assert verification["selection"]["article_id"] == (
        "article-developer-permit"
    )
    assert verification["deferred_article_ids"] == [
        "article-landowner-duty"
    ]

    hypothesis = fixture["hypotheses"][0]
    context = build_solver_context(
        CaseState(
            case_id="case-cross-domain-selection",
            question=fixture["question"],
            work_items=(WorkItem(work_item_id="w-1", question=fixture["question"]),),
            hypotheses=(
                Hypothesis(
                    hypothesis_id=hypothesis["hypothesis_id"],
                    work_item_id="w-1",
                    statement=hypothesis["statement"],
                ),
            ),
        ),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    ).model_copy(
        update={
            "search_candidates": tuple(
                SearchCandidateArticle(
                    article_id=item["article_id"],
                    document_id=None,
                    title=None,
                    headings=(),
                    discovery_work_item_ids=("w-1",),
                    discovery_hypothesis_ids=(hypothesis["hypothesis_id"],),
                    search_request_ids=("search-1",),
                    navigation_evidence_ids=(),
                )
                for item in fixture["assessment"]["assessments"]
            )
        }
    )
    _validate_search_reselection_payload(
        fixture["validSelection"],
        context,
    )
    assert fixture["invalidSelection"]["selections"][0]["article_id"] == (
        "article-landowner-duty"
    )


def test_real_search_actor_mismatch_is_preserved_as_fixture() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_search_actor_mismatch_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    profile = legal_profiles.legal_agent_profile().solver_search_review
    assert profile is not None

    rendered = render_search_assessment_model_call(context, profile)
    assessment_item = rendered.output_schema["$defs"][
        "search_candidate_assessment"
    ]
    assert "matched_hypothesis_ids" not in assessment_item["properties"]
    assert "hypotheses" not in rendered.input_payload
    assert "work_tree" not in rendered.input_payload
    assert "question" not in rendered.input_payload
    assert "Article全文を推測せず" in rendered.instructions
    assert "`search_candidates[].search_excerpts[].content`" in (
        rendered.instructions
    )
    assert "regulated_actor_role" not in assessment_item["properties"]
    assert "actor_match_reason" not in assessment_item["properties"]

    with pytest.raises(
        ModelProtocolError,
        match="must be known and non-empty",
    ):
        _validate_search_reselection_payload(
            {
                "selections": fixture["observedSolverDecision"]
                ["search_candidate_review"]["selections"]
            },
            context,
        )


def test_search_reselection_does_not_require_actor_classification() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_actor_relation_search_v191.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    profile = legal_profiles.legal_agent_profile().solver_search_review
    assert profile is not None
    article_ids = [item.article_id for item in context.search_candidates]
    assessment_payload = {
        "assessments": [
            {
                "article_id": article_id,
                "legal_function": "applicability",
                "summary": "公開買付けに関する候補",
                "matched_hypothesis_ids": ["h-1"],
            }
            for article_id in article_ids
        ],
    }
    reselection = render_search_reselection_model_call(
        context,
        assessment_payload,
        profile,
    )
    input_article_ids = {
        item["article_id"] for item in reselection.input_payload["assessments"]
    }
    assert input_article_ids == set(article_ids)
    assert all(
        "actor_matches" not in item
        and "selectable_hypothesis_ids" not in item
        for item in reselection.input_payload["assessments"]
    )


def test_search_reselection_uses_single_fetch_request_capacity() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_actor_relation_search_v191.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    saved = SolverContext.model_validate(fixture["solverContext"])
    context = saved.model_copy(
        update={
            "remaining_fetch_capacity": 5,
            "available_tools": tuple(
                item.model_copy(
                    update={
                        "input_schema": LegalFetchArticlesTool.definition.input_schema
                    }
                )
                if item.name == "fetch_articles"
                else item
                for item in saved.available_tools
            ),
        }
    )
    profile = legal_profiles.legal_agent_profile().solver_search_review
    assert profile is not None
    assessment_payload = {
        "assessments": [
            {
                "article_id": item.article_id,
                "legal_function": "applicability",
                "summary": "公開買付けに関する候補",
                "matched_hypothesis_ids": ["h-1"],
            }
            for item in context.search_candidates
        ],
    }

    rendered = render_search_reselection_model_call(
        context,
        assessment_payload,
        profile,
    )

    assert "remaining_fetch_capacity" not in rendered.input_payload
    assert rendered.input_payload["current_fetch_request_capacity"] == 5
    assert rendered.output_schema["properties"]["selections"]["maxItems"] == 5
    assert "Cycle全体の上限ではありません" in rendered.instructions


def test_lr_017_real_model_replays_select_buyer_rule_consistently() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_actor_relation_selection_regression_v197.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    expected = fixture["expected"]

    assert len(fixture["realModelRuns"]) == 2
    for run in fixture["realModelRuns"]:
        assert expected["selectedArticleId"] in run["selectedArticleIds"]
        assert expected["excludedArticleId"] not in run["selectedArticleIds"]
        assert expected["excludedArticleId"] in run["deferredArticleIds"]


def test_integration_repeated_search_fixture_prefers_fetchable_candidates() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_integration_repeated_search_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    profile = legal_profiles.legal_agent_profile().solver_integration
    assert profile is not None
    assert profile.completion_check_prompt is not None
    context = build_solver_context(
        CaseState(case_id="case-repeated-search", question="質問"),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    ).model_copy(
        update={
            "fetchable_article_ids": tuple(fixture["fetchableArticleIds"]),
        }
    )
    prompt = _solver_prompt(
        context,
        profile.system_prompt,
        completion_check_prompt=profile.completion_check_prompt,
        compact_transport=True,
    )

    assert fixture["successfulSearch"] == fixture["observedNextSearch"]
    assert fixture["expectedViolation"] == (
        "successful legal_search scope was already completed"
    )
    assert "成功済みの検索・Graph scopeは繰り返しません" in prompt
    assert "`request_id`や`purpose`だけを変えても別scopeにはなりません" in prompt
    assert "`action_feedback`を受けた場合もToolの種類は禁止されません" in prompt


def test_research_capacity_fixture_does_not_limit_work_decomposition() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_research_limited_by_action_capacity_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    saved = SolverContext.model_validate(fixture["solverContext"])
    observed = fixture["observedFailure"]
    profile = legal_profiles.legal_agent_profile().solver_research
    assert profile is not None
    assert profile.completion_check_prompt is not None
    prompt = _solver_prompt(
        saved,
        profile.system_prompt,
        completion_check_prompt=profile.completion_check_prompt,
        compact_transport=True,
    )

    assert observed["profileVersion"] == "129"
    assert observed["remainingFetchCapacity"] == observed["workItemCount"]
    assert observed["missingRequest"] not in observed["decisionReason"]
    assert "法令本文を調べる必要がある要求ごとにWorkItemを作ります" in prompt
    assert "残った回答の示し方" in prompt


def test_integration_refetch_fixture_uses_only_fetchable_article_ids() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_integration_refetches_evidence_ids_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    profile = legal_profiles.legal_agent_profile().solver_integration
    assert profile is not None
    assert profile.completion_check_prompt is not None
    context = build_solver_context(
        CaseState(case_id="case-refetch", question="質問"),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    ).model_copy(
        update={
            "fetchable_article_ids": tuple(fixture["fetchableArticleIds"]),
            "grounding_evidence_ids": tuple(
                item["evidenceId"] for item in fixture["groundingEvidence"]
            ),
        }
    )
    prompt = _solver_prompt(
        context,
        profile.system_prompt,
        completion_check_prompt=profile.completion_check_prompt,
        compact_transport=True,
    )
    repair_context = context.model_copy(
        update={
            "contract_feedback": SolverContractFeedback(
                violation=fixture["expectedViolations"][0],
                previous_decision=SolverDecision(
                    next="continue",
                    start_next_cycle=True,
                ),
            )
        }
    )
    repair = render_solver_model_call(
        repair_context,
        profile,
        provider="openai",
        stage="integration",
    ).instructions

    assert not set(fixture["observedFetchArticleIds"]) <= set(
        fixture["fetchableArticleIds"]
    )
    assert "`article_ids`は`fetchable_article_ids`から選びます" in prompt
    assert "Article IDとEvidence IDを混同しません" in prompt
    assert "本文取得済みなので、再取得要求を削除" in repair
    assert "Paragraph・ItemのEvidence IDをArticle IDとして使いません" in (
        repair
    )


def test_cycle_close_checks_follow_their_projected_inputs() -> None:
    profile = legal_profiles.legal_agent_profile().solver_cycle_close
    assert profile is not None
    assert profile.completion_check_prompt is not None
    assert profile.followup_completion_check_prompt is not None
    context = build_solver_context(
        CaseState(case_id="case-completion-check", question="質問"),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    ).model_copy(update={"cycle_close_required": True})

    observation_call = render_observation_integration_model_call(context, profile)
    assert observation_call.request.index("## 出力前の確認") > (
        observation_call.request.index("</observation_input>")
    )
    assert observation_call.request.endswith(profile.completion_check_prompt)

    transition_call = render_cycle_close_model_call(
        context,
        ObservationIntegrationDecision(decision_reason="本文を評価した"),
        profile,
    )
    assert transition_call.request.index("## 出力前の確認") > (
        transition_call.request.index("</cycle_close_input>")
    )
    assert transition_call.request.endswith(
        profile.followup_completion_check_prompt
    )


def test_candidate_selection_profiles_have_post_context_checks() -> None:
    profile = legal_profiles.legal_agent_profile()
    search = profile.solver_search_review
    graph = profile.solver_graph_review

    assert search is not None
    assert search.completion_check_prompt is not None
    assert search.followup_completion_check_prompt is not None
    assert "全候補を比較" in search.completion_check_prompt
    assert "`matched_hypothesis_ids`のHypothesis" in (
        search.followup_completion_check_prompt
    )
    assert "selected / deferred" not in (
        search.followup_completion_check_prompt
    )
    assert graph is not None
    assert graph.completion_check_prompt is not None
    assert "全Graph候補とLinkを1回ずつ評価" in (
        graph.completion_check_prompt
    )

    context = build_solver_context(
        CaseState(case_id="case-selection-check", question="質問"),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    assessment_prompt = _search_review_prompt(
        context,
        search.system_prompt,
        completion_check_prompt=search.completion_check_prompt,
    )
    reselection_prompt = _search_reselection_prompt(
        context,
        {"assessments": []},
        search.followup_system_prompt,
        completion_check_prompt=search.followup_completion_check_prompt,
    )

    assert assessment_prompt.endswith(search.completion_check_prompt)
    assert assessment_prompt.rindex(search.completion_check_prompt) > (
        assessment_prompt.rindex("</solver_context>")
    )
    assert reselection_prompt.endswith(search.followup_completion_check_prompt)
    assert reselection_prompt.rindex("## 出力前の確認") > (
        reselection_prompt.rindex("</search_review_summary>")
    )


def test_search_reselection_uses_hypotheses_and_keeps_candidate_identity(
) -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_announcement_observation_article_alias_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    candidate = next(
        item
        for item in context.search_candidates
        if item.article_id == "law-323AC0000000025-article-27_3"
    )
    profile = legal_profiles.legal_agent_profile().solver_search_review
    assert profile is not None

    assessment_schema = _search_review_transport_schema(context)
    assessment_properties = assessment_schema["$defs"][
        "search_candidate_assessment"
    ]["properties"]
    assert "matched_non_work_item_requirements" not in assessment_properties
    assert "non_work_item_requirements" not in _search_review_context_payload(
        context
    )

    rendered = render_search_reselection_model_call(
        context,
        {
            "assessments": [
                {
                    "article_id": candidate.article_id,
                    "legal_function": "procedure",
                    "summary": "公開買付開始公告の義務を定める。",
                    "matched_hypothesis_ids": ["h-1"],
                }
            ]
        },
        profile,
    )

    assert "non_work_item_requirements" not in rendered.input_payload
    assessment = rendered.input_payload["assessments"][0]
    assert assessment["title"] == candidate.title
    assert assessment["headings"] == list(candidate.headings)
    assert "義務を自ら定める候補" in rendered.instructions


def test_search_reselection_exposes_the_work_item_legal_issue() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_exception_reselection_rule_mismatch_v349.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    work_item = WorkItem.model_validate(fixture["workItem"])
    hypothesis = Hypothesis.model_validate(fixture["hypothesis"])
    context = build_solver_context(
        CaseState(
            case_id=fixture["source"]["caseId"],
            question="公開買付けの主な例外を確認する。",
            work_items=(work_item,),
            hypotheses=(hypothesis,),
        ),
        AgentLimits(),
        remaining_wall_time_sec=120,
        finalize_only=False,
    ).model_copy(
        update={
            "search_candidates": tuple(
                SearchCandidateArticle(
                    article_id=item["article_id"],
                    document_id="law-340CO0000000321",
                    title="金融商品取引法施行令",
                    headings=(),
                    discovery_work_item_ids=(work_item.work_item_id,),
                    discovery_hypothesis_ids=(hypothesis.hypothesis_id,),
                    search_request_ids=("search-1",),
                    navigation_evidence_ids=(),
                )
                for item in fixture["assessments"]
            ),
            "remaining_fetch_capacity": 1,
        }
    )
    rendered = render_search_reselection_model_call(
        context,
        {"assessments": fixture["assessments"]},
        legal_profiles.legal_agent_profile().solver_search_review,
    )

    assert rendered.input_payload["work_items"] == [
        {
            "work_item_id": work_item.work_item_id,
            "question": work_item.question,
        }
    ]
    assert "WorkItemが確認する規律" in rendered.instructions


def test_anthropic_search_review_keys_assessments_by_article() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_announcement_observation_article_alias_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    profile = legal_profiles.legal_agent_profile().solver_search_review
    assert profile is not None

    assessment_call = render_search_assessment_model_call(
        context,
        profile,
        provider="anthropic",
    )
    assessment_schema = assessment_call.output_schema["properties"][
        "assessments"
    ]
    assert assessment_schema["type"] == "object"
    assert set(assessment_schema["properties"]) == {
        item.article_id for item in context.search_candidates
    }
    assert assessment_schema["required"] == list(
        assessment_schema["properties"]
    )
    assert "$defs" in assessment_call.output_schema

def test_cycle_close_does_not_retain_evidence_already_managed_by_state() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_cycle_close_retains_managed_evidence_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    hypothesis = context.hypotheses[0]
    evidence_id = context.grounding_evidence_ids[0]
    observation = ObservationIntegrationDecision(
        decision_reason="取得本文を仮説へ反映した",
        update_hypotheses=(
            HypothesisUpdate(
                hypothesis_id=hypothesis.hypothesis_id,
                judgment="unresolved",
                evidence_ids=(evidence_id,),
                gaps=hypothesis.gaps,
            ),
        ),
    )

    rendered = render_cycle_close_model_call(
        context,
        observation,
        legal_profiles.legal_agent_profile().solver_cycle_close,
    )

    retainable_ids = {
        item["evidence_id"]
        for item in rendered.input_payload["retainable_evidence"]
    }
    retain_schema = rendered.output_schema["properties"]["retain_evidence_ids"]
    assert evidence_id not in retainable_ids
    assert evidence_id not in retain_schema["items"].get("enum", [])
    assert "自動で再提示される" in rendered.instructions


def test_cycle_close_tells_model_the_retained_evidence_limit() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_cycle_close_missing_retain_limit_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    decision = fixture["observedSolverDecision"]
    observation = ObservationIntegrationDecision.model_validate(
        {
            "decision_reason": decision["decision_reason"],
            "update_work_items": decision["update"]["update_work_items"],
            "update_hypotheses": decision["update"]["update_hypotheses"],
            "dependency_decisions": decision["dependency_decisions"],
        }
    )

    rendered = render_cycle_close_model_call(
        context,
        observation,
        legal_profiles.legal_agent_profile().solver_cycle_close,
    )

    assert (
        rendered.input_payload["max_retained_evidence"]
        == context.max_retained_evidence
    )
    assert "候補全件をコピーしません" in rendered.instructions


def test_cycle2_anthropic_solver_uses_provider_transport_schema() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_cycle2_anthropic_schema_too_large_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    profile = legal_profiles.legal_agent_profile().solver_integration

    rendered = render_solver_model_call(
        context,
        profile,
        provider="anthropic",
    )

    assert set(rendered.output_schema["properties"]) == {
        "decision_reason",
        "start_next_cycle",
        "tool_requests_json",
    }
    assert len(json.dumps(rendered.output_schema)) < 1_000
    assert "ToolRequest array encoded as one JSON array string" in json.dumps(
        rendered.output_schema
    )


def test_search_assessment_batches_large_candidate_sets() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_anthropic_search_assessment_duplicate_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    profile = legal_profiles.legal_agent_profile().solver_search_review

    batches = _search_review_batch_contexts(context)

    assert all(len(batch.search_candidates) <= 8 for batch in batches)
    assert [
        item.article_id
        for batch in batches
        for item in batch.search_candidates
    ] == [item.article_id for item in context.search_candidates]
    for batch in batches:
        rendered = render_search_assessment_model_call(
            batch,
            profile,
            provider="anthropic",
        )
        assessments = rendered.output_schema["properties"]["assessments"]
        assert len(assessments["properties"]) <= 8


def test_search_reselection_schema_does_not_expand_per_candidate() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_anthropic_search_assessment_duplicate_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    assessments = {
        "assessments": [
            {
                "article_id": item.article_id,
                "legal_function": "applicability",
                "summary": "候補自身が扱う規律の要約",
            }
            for item in context.search_candidates
        ]
    }

    rendered = render_search_reselection_model_call(
        context,
        assessments,
        legal_profiles.legal_agent_profile().solver_search_review,
    )
    item_schema = rendered.output_schema["properties"]["selections"]["items"]

    assert "anyOf" not in item_schema
    assert item_schema["properties"]["article_id"]["enum"] == [
        item.article_id for item in context.search_candidates
    ]
    assert len(json.dumps(rendered.output_schema)) < 3_000


def test_scope_search_candidate_keeps_content_match_without_actor_alignment() -> None:
    context = build_solver_context(
        CaseState(
            case_id="actor-neutral",
            question="少数所有者の条件は何か。",
            work_items=(
                WorkItem(
                    work_item_id="wi-1",
                    question="少数所有者の条件を確認する。",
                ),
            ),
            hypotheses=(
                Hypothesis(
                    hypothesis_id="h-1",
                    work_item_id="wi-1",
                    statement="所有者数に条件がある。",
                ),
            ),
        ),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    ).model_copy(
        update={
            "search_candidates": (
                SearchCandidateArticle(
                    article_id="article-scope",
                    document_id="law-scope",
                    title="所有者数の定義",
                    headings=(),
                    discovery_work_item_ids=("wi-1",),
                    discovery_hypothesis_ids=("h-1",),
                    search_request_ids=("search-1",),
                    navigation_evidence_ids=(),
                ),
            )
        }
    )
    assessment = {
        "assessments": [
            {
                "article_id": "article-scope",
                "legal_function": "scope",
                "summary": "所有者数の範囲を定める。",
                "matched_hypothesis_ids": ["h-1"],
            }
        ]
    }

    rendered = render_search_reselection_model_call(
        context,
        assessment,
        legal_profiles.legal_agent_profile().solver_search_review,
    )

    assert "matched_hypothesis_ids" not in rendered.input_payload[
        "assessments"
    ][0]
    selection_schema = rendered.output_schema["properties"]["selections"]
    assert selection_schema["items"]["properties"][
        "matched_hypothesis_ids"
    ]["items"]["enum"] == ["h-1"]
    assert "actor_matches" not in rendered.input_payload["assessments"][0]


def test_solver_profile_selection_uses_only_structural_context_flags() -> None:
    profile = legal_profiles.legal_agent_profile()
    loop = AgentLoop(
        store=InMemoryCaseStore(),
        model=StructuredJSONModelAdapter(FakeStructuredLLM()),
        tools=ToolRegistry(()),
        profile=profile,
    )
    base_context = build_solver_context(
        CaseState(case_id="case-mode", question="質問"),
        profile.limits,
        remaining_wall_time_sec=profile.limits.max_wall_time_sec,
        finalize_only=False,
    )

    cases = (
        ({"graph_review_call": True}, "graph_selection"),
        ({"search_review_call": True}, "search_selection"),
        ({"has_reviewer_findings": True}, "reviewer_revision"),
        ({"context": base_context.model_copy(update={"finalize_only": True})}, "finalization"),
        (
            {
                "context": base_context.model_copy(
                    update={"cycle_close_required": True}
                )
            },
            "cycle_close",
        ),
        ({"integration_call": True}, "integration"),
        ({}, "research"),
    )
    for overrides, expected_purpose in cases:
        call = {
            "context": base_context,
            "graph_review_call": False,
            "search_review_call": False,
            "integration_call": False,
            "has_reviewer_findings": False,
            **overrides,
        }
        _, purpose = loop._solver_profile_for_context(**call)
        assert purpose == expected_purpose

    observation_profile, purpose = loop._solver_profile_for_context(
        context=base_context,
        graph_review_call=False,
        search_review_call=False,
        integration_call=True,
        has_reviewer_findings=False,
        observation_integration_call=True,
    )
    assert purpose == "observation_integration"
    assert observation_profile == profile.solver_observation_integration

    partial_hypothesis_context = build_solver_context(
        CaseState(
            case_id="case-mode-partial-hypothesis",
            question="二つの論点を確認する",
            work_items=(
                WorkItem(work_item_id="wi-1", question="第一の論点"),
                WorkItem(work_item_id="wi-2", question="第二の論点"),
            ),
            hypotheses=(
                Hypothesis(
                    hypothesis_id="h-1",
                    work_item_id="wi-1",
                    statement="第一の論点に対する暫定的な結論",
                ),
            ),
            ),
            profile.limits,
            remaining_wall_time_sec=profile.limits.max_wall_time_sec,
            finalize_only=False,
        )
    _, purpose = loop._solver_profile_for_context(
        context=partial_hypothesis_context,
        graph_review_call=False,
        search_review_call=False,
        integration_call=False,
        has_reviewer_findings=False,
    )
    assert purpose == "hypothesis_generation"


def test_legal_profile_uses_terra_only_for_evidence_integration(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        legal_profiles.settings,
        "agent_framework_research_model",
        "gpt-5.6-luna",
    )
    monkeypatch.setattr(
        legal_profiles.settings,
        "agent_framework_integration_model",
        "gpt-5.6-luna",
    )
    monkeypatch.setattr(
        legal_profiles.settings,
        "agent_framework_evidence_integration_model",
        "gpt-5.6-terra",
    )

    profile = legal_profiles.legal_agent_profile()

    assert profile.solver_observation_integration is not None
    assert profile.solver_observation_integration.model == "gpt-5.6-terra"
    assert profile.solver_research.model == "gpt-5.6-luna"
    assert profile.solver_integration.model == "gpt-5.6-luna"
    assert profile.solver_cycle_close is not None
    assert profile.solver_cycle_close.model == "gpt-5.6-luna"
    assert profile.solver_finalization is not None
    assert profile.solver_finalization.model == "gpt-5.6-luna"


def test_graph_review_prompt_has_one_document_hierarchy() -> None:
    graph_prompt = legal_profiles.legal_agent_profile().solver_graph_review
    assert graph_prompt is not None
    prompt = graph_prompt.system_prompt
    assert [line for line in prompt.splitlines() if line.startswith("# ")] == [
        "# 法令調査Solver：Graph候補の評価"
    ]
    assert "## 入力" in prompt
    assert "## 手順" in prompt
    assert "## Relationの読み方" in prompt
    assert "### 候補の選択" in prompt
    assert "## 出力" in prompt


def test_complete_model_prompts_define_one_role_and_one_purpose() -> None:
    profile = legal_profiles.legal_agent_profile()
    prompts = [
        profile.solver_research.system_prompt,
        profile.solver_hypothesis_generation.system_prompt,
        profile.solver_search_planning.system_prompt,
        profile.solver_integration.system_prompt,
        profile.solver_integration.dependency_action_system_prompt,
        profile.solver_cycle_close.system_prompt,
        profile.solver_cycle_close.followup_system_prompt,
        profile.solver_cycle_close.dependency_system_prompt,
        profile.solver_finalization.system_prompt,
        profile.solver_reviewer_revision.system_prompt,
        profile.solver_search_review.system_prompt,
        profile.solver_search_review.followup_system_prompt,
        profile.solver_graph_review.system_prompt,
        profile.reviewer.system_prompt,
    ]

    for prompt in prompts:
        assert prompt is not None
        assert len(
            [line for line in prompt.splitlines() if line.startswith("# ")]
        ) == 1
        assert len(
            [
                line
                for line in prompt.splitlines()
                if line in {"## 目的", "### 目的"}
            ]
        ) == 1


def test_cycle_close_prompts_separate_observation_from_transition() -> None:
    profile = legal_profiles.legal_agent_profile()
    observation = profile.solver_cycle_close.system_prompt
    transition = profile.solver_cycle_close.followup_system_prompt

    assert "## 手順" in observation
    assert "## ルール" in observation
    assert "`update_hypotheses[]`" in observation
    assert "WorkItemの完了状態は出力しません" in observation
    assert "Cycle移行と最終回答も出力しません" in observation
    assert "`tool_requests[]`" in observation
    assert "入力にないHypothesisを更新せず" in observation
    assert "`dependency_decisions[]`" in observation
    assert transition is not None
    assert "## 手順" in transition
    assert "## ルール" in transition
    assert "work_items_after_observation" in transition
    assert "hypotheses_after_observation" in transition
    assert "retainable_evidence" in transition
    assert "Article ID、検索候補ID、Graph候補ID" in transition


def test_search_selection_prompt_combines_candidate_understanding_and_choice() -> None:
    search_prompt = legal_profiles.legal_agent_profile().solver_search_review
    assert search_prompt is not None
    prompt = search_prompt.system_prompt
    assert [line for line in prompt.splitlines() if line.startswith("# ")] == [
        "# 法令調査Solver：本文取得候補の選択"
    ]
    assert "見出しと検索抜粋" in prompt
    assert "WorkItemとHypothesisへ照合" in prompt
    assert "matched_hypothesis_ids" in prompt
    assert "規律主体を確定できなくても" in prompt
    assert "検索抜粋は候補選択用" in prompt


def test_supported_hypothesis_with_gaps_remains_available_for_search() -> None:
    state = CaseState(
        case_id="case-supported-gap-search",
        question="確認事項",
        non_work_item_requirements=("根拠条文を示す",),
        work_items=(WorkItem(work_item_id="w1", question="具体的内容を確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h-follow-up",
                work_item_id="w1",
                statement="上位規定が方針を認める",
                judgment="supported",
                evidence_ids=("e1",),
                gaps=("下位規定の具体的内容",),
            ),
            Hypothesis(
                hypothesis_id="h-complete",
                work_item_id="w1",
                statement="確認済みの別命題",
                judgment="supported",
                evidence_ids=("e1",),
            ),
        ),
        evidence=(
            Evidence(
                evidence_id="e1",
                source_ref="fixture:e1",
                content="上位規定の本文",
                created_cycle=1,
            ),
        ),
    )
    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
        available_tools=(LegalSearchTool.definition,),
    ).model_copy(
        update={
            "search_candidates": (
                SearchCandidateArticle(
                    article_id="article-1",
                    document_id="document-1",
                    title="候補法令",
                    headings=("第一条",),
                    discovery_work_item_ids=("w1",),
                    discovery_hypothesis_ids=("h-follow-up",),
                    search_request_ids=("search-1",),
                    navigation_evidence_ids=(),
                ),
            ),
            "remaining_fetch_capacity": 1,
        }
    )
    profile = legal_profiles.legal_agent_profile()

    selection = render_search_selection_model_call(
        context,
        profile.solver_search_review,
    )
    assert [
        item["hypothesis_id"] for item in selection.input_payload["hypotheses"]
    ] == ["h-follow-up"]
    assert selection.input_payload["non_work_item_requirements"] == [
        "根拠条文を示す"
    ]
    matched_schema = selection.output_schema["properties"]["selections"][
        "items"
    ]["properties"]["matched_hypothesis_ids"]
    assert matched_schema["items"]["enum"] == ["h-follow-up"]

    planning_profile = profile.solver_search_planning
    assert planning_profile is not None
    planning = render_solver_model_call(
        context,
        planning_profile,
        provider="openai",
        stage="search_planning",
    )
    assert [
        item["hypothesis_id"] for item in planning.input_payload["hypotheses"]
    ] == ["h-follow-up"]
    assert [
        item["work_item_id"] for item in planning.input_payload["work_items"]
    ] == ["w1"]


def test_search_review_selects_and_defers_every_candidate_once() -> None:
    state = CaseState(
        case_id="case-search-adoption",
        question="質問",
        research_cycle_count=1,
        work_items=(
            WorkItem(work_item_id="w-discovery", question="発見元"),
            WorkItem(work_item_id="w-meaning", question="意味上の採用先"),
        ),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h-discovery",
                work_item_id="w-discovery",
                statement="発見元の仮説",
            ),
            Hypothesis(
                hypothesis_id="h-meaning",
                work_item_id="w-meaning",
                statement="候補本文で検証する仮説",
            ),
        ),
    )
    review = SearchCandidateReview(
        search_request_ids=("search-1",),
        selections=(
            SearchCandidateSelection(
                article_id="article-1",
                reason="検索抜粋は意味上の採用先を直接扱う",
                matched_hypothesis_ids=("h-meaning",),
            ),
        ),
        deferred_article_ids=("article-2",),
        reason="本文確認の優先順位を決める",
    )

    updated = apply_solver_decision(
        state,
        SolverDecision(next="continue", search_candidate_review=review),
        limits=AgentLimits(),
        known_tool_names={"fetch_articles"},
        material_evidence_ids=(),
        required_search_review_request_ids=("search-1",),
        search_candidate_article_ids=("article-1", "article-2"),
        remaining_fetch_capacity=1,
        finalize_only=False,
    )

    assert updated.search_candidate_reviews[0].selected_article_ids == (
        "article-1",
    )
    assert updated.search_candidate_reviews[0].selections[
        0
    ].matched_hypothesis_ids == ("h-meaning",)
    assert updated.search_candidate_reviews[0].assessments == ()


def test_deferred_search_candidate_keeps_llm_assessment_across_cycles() -> None:
    article_id = "law-test-article-2"
    evidence_id = "search-nav-law-test-article-2"
    state = CaseState(
        case_id="case-search-handoff",
        question="例外を確認する",
        research_cycle_count=2,
        work_items=(WorkItem(work_item_id="w1", question="例外を確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="例外条件がある",
            ),
        ),
        tool_requests=(
            ToolRequest(
                request_id="search-1",
                work_item_id="w1",
                tool_name="legal_search",
                arguments={"query": "例外条件"},
                purpose="例外候補を探す",
                hypothesis_ids=("h1",),
            ),
        ),
        tool_results=(
            ToolResult(
                request_id="search-1",
                status="succeeded",
                evidence_ids=(evidence_id,),
                cycle_no=1,
            ),
        ),
        evidence=(
            Evidence(
                evidence_id=evidence_id,
                source_ref="test://search",
                content="例外条件を示す検索抜粋",
                created_cycle=1,
                metadata={
                    "articleId": article_id,
                    "citationEligible": False,
                    "evidenceRole": "search_navigation",
                },
            ),
        ),
        search_candidate_reviews=(
            SearchCandidateReview(
                search_request_ids=("search-1",),
                selections=(),
                assessments=(
                    SearchCandidateAssessmentRecord(
                        article_id=article_id,
                        legal_function="exception",
                        summary="このArticleは例外条件を定める",
                        matched_hypothesis_ids=("h1",),
                    ),
                ),
                deferred_article_ids=(article_id,),
                reason="次Cycleで確認する",
                reviewed_cycle=1,
            ),
        ),
    )

    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=120,
        finalize_only=False,
    )

    assert context.search_candidates[0].article_id == article_id
    assert context.search_candidates[0].legal_function == "exception"
    assert (
        context.search_candidates[0].assessment_summary
        == "このArticleは例外条件を定める"
    )
    assert context.search_candidates[0].matched_hypothesis_ids == ("h1",)


def test_dependency_action_projection_focuses_required_work_items() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_cycle2_repeats_search_before_fetch_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    context = context.model_copy(
        update={
            "available_tools": tuple(
                LegalGraphNeighborsTool.definition
                if item.name == "legal_graph_neighbors"
                else item
                for item in context.available_tools
            )
        }
    )
    rendered = render_solver_model_call(
        context,
        legal_profiles.legal_agent_profile().solver_integration,
        provider="openai",
    )

    required_ids = set(context.required_dependency_work_item_ids)
    assert {
        item["work_item_id"] for item in rendered.input_payload["work_tree"]
    } == required_ids
    assert {
        item["work_item_id"] for item in rendered.input_payload["hypotheses"]
    } == required_ids
    assert {
        item["work_item_id"]
        for item in rendered.input_payload["dependency_decisions"]
    } == required_ids
    assert rendered.input_payload["fetchable_article_ids"] == []
    available_tool_names = {
        item["name"] for item in rendered.input_payload["available_tools"]
    }
    assert "fetch_articles" not in available_tool_names
    assert "legal_search" not in available_tool_names
    assert "legal_graph_neighbors" in available_tool_names
    graph_tool = next(
        item
        for item in rendered.input_payload["available_tools"]
        if item["name"] == "legal_graph_neighbors"
    )
    graph_variants = graph_tool["input_schema"]["anyOf"]
    graph_modes = {
        item["properties"]["mode"]["const"] for item in graph_variants
    }
    assert {"semantic_assertion", "reference_edges"} <= graph_modes
    assert "他のopen" in rendered.instructions
    assert "## 下位規範を確認する次の行動" in rendered.instructions
    assert "## Tool結果の統合と次の行動" not in rendered.instructions


def test_dependency_action_allows_following_named_reference() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_cycle2_repeats_search_before_fetch_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    work_item_id = context.required_dependency_work_item_ids[0]
    context = context.model_copy(
        update={
            "required_dependency_work_item_ids": (work_item_id,),
            "dependency_decisions": tuple(
                item
                for item in context.dependency_decisions
                if item.work_item_id == work_item_id
            ),
        }
    )
    hypothesis_id = next(
        item.hypothesis_id
        for item in context.hypotheses
        if item.work_item_id == work_item_id
    )
    article_id = next(
        item.metadata["articleId"]
        for item in context.material_evidence
        if isinstance(item.metadata.get("articleId"), str)
    )

    decision = normalize_dependency_action_decision(
        {
            "decision_reason": "本文に明記された参照先を順引きする。",
            "start_next_cycle": False,
            "tool_requests": [
                {
                    "request_id": "graph-outgoing-reference",
                    "work_item_id": work_item_id,
                    "tool_name": "legal_graph_neighbors",
                    "arguments": {
                        "article_ids": [article_id],
                        "max_relations": 10,
                        "mode": "reference_edges",
                        "predicate": None,
                        "reference_lookup": "follow_reference_in_text",
                    },
                    "purpose": "本文に書かれた参照先Articleを発見する。",
                    "hypothesis_ids": [hypothesis_id],
                }
            ],
        },
        context=context,
    )

    assert decision.tool_requests[0].arguments["reference_lookup"] == (
        "follow_reference_in_text"
    )

    with pytest.raises(ModelProtocolError, match="must inspect the available"):
        normalize_dependency_action_decision(
            {
                "decision_reason": "次Cycleで方針を見直す。",
                "start_next_cycle": True,
                "tool_requests": [],
            },
            context=context,
        )


def test_dependency_action_allows_hypothesis_aligned_semantic_graph() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_cycle2_repeats_search_before_fetch_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    work_item_id = context.required_dependency_work_item_ids[0]
    context = context.model_copy(
        update={
            "required_dependency_work_item_ids": (work_item_id,),
            "dependency_decisions": tuple(
                item
                for item in context.dependency_decisions
                if item.work_item_id == work_item_id
            ),
            "available_tools": tuple(
                LegalGraphNeighborsTool.definition
                if item.name == "legal_graph_neighbors"
                else item
                for item in context.available_tools
            ),
        }
    )
    hypothesis_id = next(
        item.hypothesis_id
        for item in context.hypotheses
        if item.work_item_id == work_item_id
    )
    article_id = next(
        item.metadata["articleId"]
        for item in context.material_evidence
        if isinstance(item.metadata.get("articleId"), str)
    )

    decision = normalize_dependency_action_decision(
        {
            "decision_reason": "具体化関係に沿って下位規範候補を探す。",
            "start_next_cycle": False,
            "tool_requests": [
                {
                    "request_id": "graph-semantic",
                    "work_item_id": work_item_id,
                    "tool_name": "legal_graph_neighbors",
                    "arguments": {
                        "article_ids": [article_id],
                        "max_relations": 10,
                        "mode": "semantic_assertion",
                        "predicate": "IMPLEMENTS",
                        "direction": "from_subject",
                    },
                    "purpose": "仮説に対応する具体化規定を発見する。",
                    "hypothesis_ids": [hypothesis_id],
                }
            ],
        },
        context=context,
    )

    assert decision.tool_requests[0].arguments["mode"] == "semantic_assertion"


def test_uses_definition_graph_checkpoint_isolates_semantic_lookup() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_uses_definition_graph_isolated_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    expected = fixture["expectations"]["expectedGraphRequest"]

    assert {item.name for item in context.available_tools} == {
        "legal_graph_neighbors"
    }
    assert {
        item.metadata.get("articleId") for item in context.material_evidence
    } == {expected["articleId"]}
    assert expected["expectedNeighborArticleId"] not in json.dumps(
        context.model_dump(mode="json"),
        ensure_ascii=False,
    )

    rendered = render_solver_model_call(
        context,
        legal_profiles.legal_agent_profile().solver_integration,
        provider="openai",
        stage="integration",
    )
    assert [item["name"] for item in rendered.input_payload["available_tools"]] == [
        "legal_graph_neighbors"
    ]

    observed = SolverDecision.model_validate(fixture["observedSolverDecision"])
    assert len(observed.tool_requests) == 1
    request = observed.tool_requests[0]
    assert request.tool_name == "legal_graph_neighbors"
    assert request.arguments == {
        "article_ids": [expected["articleId"]],
        "max_relations": 20,
        "mode": expected["mode"],
        "predicate": expected["predicate"],
        "direction": expected["direction"],
    }


def test_dependency_action_may_advance_part_of_needs_action_scope() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_dependency_action_wrong_reference_direction_v369.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    work_item_id = context.required_dependency_work_item_ids[0]
    dependency = next(
        item
        for item in context.dependency_decisions
        if item.work_item_id == work_item_id
    )
    hypothesis_id = next(
        item.hypothesis_id
        for item in context.hypotheses
        if item.work_item_id == work_item_id
    )
    article_id = next(
        item.metadata["articleId"]
        for item in context.material_evidence
        if item.evidence_id in dependency.basis_evidence_ids
        and isinstance(item.metadata.get("articleId"), str)
    )

    decision = normalize_dependency_action_decision(
        {
            "decision_reason": "今回進める一つの下位規範を探索する。",
            "start_next_cycle": False,
            "tool_requests": [
                {
                    "request_id": "graph-one-work-item",
                    "work_item_id": work_item_id,
                    "tool_name": "legal_graph_neighbors",
                    "arguments": {
                        "article_ids": [article_id],
                        "max_relations": 10,
                        "mode": "reference_edges",
                        "predicate": None,
                        "reference_lookup": "find_articles_referencing_this",
                    },
                    "purpose": "今回選んだ下位規範のArticleを逆引きする。",
                    "hypothesis_ids": [hypothesis_id],
                }
            ],
        },
        context=context,
    )

    assert len(decision.tool_requests) == 1
    dependencies = {
        item.work_item_id: item for item in decision.dependency_decisions
    }
    assert dependencies[work_item_id].action_request_id is not None
    assert all(
        item.action_request_id is None
        for item_id, item in dependencies.items()
        if item_id != work_item_id
    )


def test_dependency_action_prompt_distinguishes_unknown_lower_norm_lookup() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_dependency_action_wrong_reference_direction_v369.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    context = context.model_copy(
        update={
            "available_tools": tuple(
                LegalGraphNeighborsTool.definition
                if item.name == "legal_graph_neighbors"
                else item
                for item in context.available_tools
            )
        }
    )
    rendered = render_solver_model_call(
        context,
        legal_profiles.legal_agent_profile().solver_integration,
        provider="anthropic",
        stage="integration",
    )

    assert "まず`semantic_assertion`を使います" in rendered.instructions
    assert "新規候補が得られず参照関係を確認する場合" in (
        rendered.instructions
    )
    graph_tool = next(
        item
        for item in rendered.input_payload["available_tools"]
        if item["name"] == "legal_graph_neighbors"
    )
    graph_variants = graph_tool["input_schema"]["anyOf"]
    assert any(
        item["properties"]["mode"]["const"] == "semantic_assertion"
        and "direction" in item["properties"]
        for item in graph_variants
    )
    assert any(
        item["properties"]["mode"]["const"] == "reference_edges"
        and "reference_lookup" in item["properties"]
        for item in graph_variants
    )

    observed = json.loads(
        fixture["observedTransportOutput"]["payload"]["tool_requests_json"]
    )
    assert {item["arguments"]["direction"] for item in observed} == {
        "outgoing"
    }


def test_finalization_schema_rejects_stale_deferred_frontier_ids() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_finalization_uses_stale_frontier_id_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    rendered = render_solver_model_call(
        context,
        legal_profiles.legal_agent_profile().solver_finalization,
        provider="openai",
    )
    active_ids = [
        item.frontier_item_id
        for item in context.graph_review_ledger
        if item.review_status == "relevant_deferred"
        and item.content_status in {"not_requested", "failed", "timeout"}
        and item.deferred_resolution_action != "no_longer_needed"
    ]
    frontier_schema = rendered.output_schema["properties"][
        "deferred_frontier_resolutions"
    ]

    assert frontier_schema["type"] == "object"
    assert frontier_schema["required"] == active_ids
    assert set(frontier_schema["properties"]) == set(active_ids)
    assert all(
        set(item["properties"]) == {"action", "reason"}
        for item in frontier_schema["properties"].values()
    )


def test_finalization_restores_deferred_frontier_references_from_known_id() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_finalization_mismatched_deferred_frontier_fields_v372.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    active_by_id = {
        item.frontier_item_id: item
        for item in context.graph_review_ledger
        if item.review_status == "relevant_deferred"
        and item.content_status in {"not_requested", "failed", "timeout"}
        and item.deferred_resolution_action != "no_longer_needed"
    }
    normalized = _normalize_solver_payload(
        fixture["observedTransportOutput"]["payload"]
    )

    _normalize_absent_context_branches(normalized, context)

    resolutions = normalized["deferred_frontier_resolutions"]
    assert len(resolutions) == len(active_by_id)
    for resolution in resolutions:
        expected = active_by_id[resolution["frontier_item_id"]]
        assert resolution["article_id"] == expected.article_id
        assert resolution["work_item_id"] == expected.work_item_id
        assert resolution["hypothesis_id"] == expected.hypothesis_id

    keyed_payload = {
        item.frontier_item_id: {
            "action": "unresolved_at_limit",
            "reason": "実行上限で本文未確認。",
        }
        for item in active_by_id.values()
    }
    normalized["deferred_frontier_resolutions"] = keyed_payload
    _normalize_absent_context_branches(normalized, context)
    assert {
        item["frontier_item_id"]
        for item in normalized["deferred_frontier_resolutions"]
    } == set(active_by_id)


def test_search_review_persists_assessed_hypothesis_matches() -> None:
    context = build_solver_context(
        CaseState(
            case_id="case-search-match-provenance",
            question="質問",
            work_items=(WorkItem(work_item_id="wi-1", question="確認事項"),),
            hypotheses=(
                Hypothesis(
                    hypothesis_id="h-1",
                    work_item_id="wi-1",
                    statement="検証する命題",
                ),
            ),
        ),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )

    normalized = _normalize_search_review_payload(
        {
            "search_request_ids": ["search-1"],
            "assessments": [
                {
                    "article_id": "article-1",
                    "legal_function": "applicability",
                    "summary": "命題を直接検証できる候補",
                    "matched_hypothesis_ids": ["h-1"],
                }
            ],
            "selections": [
                {
                    "article_id": "article-1",
                    "reason": "本文を確認する",
                    "matched_hypothesis_ids": ["h-1"],
                }
            ],
            "reason": "候補を評価した",
        },
        context,
    )

    selection = normalized["search_candidate_review"]["selections"][0]
    assert selection["matched_hypothesis_ids"] == ["h-1"]


def test_observation_projects_article_to_hypothesis_candidate_links() -> None:
    state = CaseState(
        case_id="case-observation-hypothesis-links",
        question="質問",
        research_cycle_count=1,
        work_items=(WorkItem(work_item_id="wi-1", question="確認事項"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h-1",
                work_item_id="wi-1",
                statement="検証する命題",
            ),
        ),
        evidence=(
            Evidence(
                evidence_id="e-1",
                source_ref="fixture:e-1",
                content="取得した条文本文",
                created_cycle=1,
                metadata={
                    "articleId": "article-1",
                    "citationEligible": True,
                },
            ),
        ),
        tool_requests=(
            ToolRequest(
                request_id="fetch-1",
                work_item_id="wi-1",
                tool_name="fetch_articles",
                arguments={"article_ids": ["article-1"]},
                purpose="選択した候補本文を取得する",
                hypothesis_ids=("h-1",),
            ),
        ),
        tool_results=(
            ToolResult(
                request_id="fetch-1",
                status="succeeded",
                evidence_ids=("e-1",),
                cycle_no=1,
            ),
        ),
        search_candidate_reviews=(
            SearchCandidateReview(
                search_request_ids=("search-1",),
                selections=(
                    SearchCandidateSelection(
                        article_id="article-1",
                        reason="命題を直接検証できる候補",
                        matched_hypothesis_ids=("h-1",),
                    ),
                ),
                assessments=(
                    SearchCandidateAssessmentRecord(
                        article_id="article-1",
                        legal_function="applicability",
                        summary="候補自身は対象規律の適用条件を定める。",
                        matched_hypothesis_ids=("h-1",),
                    ),
                ),
                deferred_article_ids=(),
                reason="候補を評価した",
                reviewed_cycle=1,
            ),
        ),
    )
    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    profile = legal_profiles.legal_agent_profile().solver_cycle_close
    assert profile is not None

    rendered = render_observation_integration_model_call(context, profile)

    assert rendered.input_payload["evidence_hypothesis_candidates"] == [
        {
            "article_id": "article-1",
            "hypothesis_ids": ["h-1"],
            "assessment_summary": "候補自身は対象規律の適用条件を定める。",
        }
    ]
    assert "question" not in rendered.input_payload
    assert set(rendered.input_payload["work_items"][0]) == {
        "work_item_id",
        "question",
    }
    assert set(rendered.input_payload["grounding_evidence"][0]) == {
        "evidence_id",
        "content",
        "title",
        "metadata",
    }
    assert "判定結果ではなく対応先も制限しない" in rendered.instructions
    assert "`grounding_evidence[].metadata.articleId`と対応する" in (
        rendered.instructions
    )
    assert "Hypothesisが確認する規律と法的効果" in (
        rendered.instructions
    )
    assert "WorkItemの完了状態は出力しません" in rendered.instructions

    observation = ObservationIntegrationDecision(
        decision_reason="本文で命題の一部を確認した",
        update_work_items=(
            {
                "work_item_id": "wi-1",
                "state": "open",
                "resolution": None,
                "basis_hypothesis_ids": (),
            },
        ),
        update_hypotheses=(
            {
                "hypothesis_id": "h-1",
                "judgment": "unresolved",
                "evidence_ids": ("e-1",),
                "gaps": ("本文でまだ確認できない条件",),
            },
        ),
    )
    cycle_close = render_cycle_close_model_call(context, observation, profile)
    projected = cycle_close.input_payload["hypotheses_after_observation"][0]
    assert projected["judgment"] == "unresolved"
    assert projected["evidence_ids"] == ["e-1"]
    assert projected["gaps"] == ["本文でまだ確認できない条件"]


def test_hypothesis_evidence_binding_is_carried_into_later_observation() -> None:
    state = CaseState(
        case_id="case-carry-hypothesis-evidence",
        question="確認する",
        research_cycle_count=2,
        work_items=(WorkItem(work_item_id="wi-1", question="確認事項"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h-1",
                work_item_id="wi-1",
                statement="確認する命題",
                evidence_ids=("e-prior",),
                gaps=("追加条件",),
            ),
        ),
        evidence=(
            Evidence(
                evidence_id="e-prior",
                source_ref="fixture:e-prior",
                content="前Cycleで取得した本文",
                created_cycle=1,
                metadata={"articleId": "article-prior"},
            ),
            Evidence(
                evidence_id="e-new",
                source_ref="fixture:e-new",
                content="現在Cycleで取得した本文",
                created_cycle=2,
                metadata={"articleId": "article-new"},
            ),
        ),
        tool_requests=(
            ToolRequest(
                request_id="fetch-new",
                work_item_id="wi-1",
                tool_name="fetch_articles",
                arguments={"article_ids": ["article-new"]},
                purpose="追加条件を確認する",
                hypothesis_ids=("h-1",),
            ),
        ),
        tool_results=(
            ToolResult(
                request_id="fetch-new",
                status="succeeded",
                evidence_ids=("e-new",),
                cycle_no=2,
            ),
        ),
    )
    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    observation = ObservationIntegrationDecision(
        decision_reason="現在Cycleの本文を追加した",
        update_hypotheses=(
            HypothesisUpdate(
                hypothesis_id="h-1",
                judgment="supported",
                evidence_ids=("e-new",),
                gaps=(),
            ),
        ),
    )
    profile = legal_profiles.legal_agent_profile().solver_cycle_close
    assert profile is not None

    rendered = render_cycle_close_model_call(context, observation, profile)
    projected = rendered.input_payload["hypotheses_after_observation"][0]
    assert projected["evidence_ids"] == ["e-prior", "e-new"]

    applied = apply_solver_decision(
        state,
        SolverDecision(
            next="continue",
            decision_reason="現在Cycleの本文を保存する",
            update={"update_hypotheses": observation.update_hypotheses},
        ),
        limits=AgentLimits(),
        known_tool_names={"fetch_articles"},
        material_evidence_ids=("e-new",),
        finalize_only=False,
    )
    assert applied.hypotheses[0].evidence_ids == ("e-prior", "e-new")

    next_cycle_context = build_solver_context(
        applied.model_copy(update={"research_cycle_count": 3}),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    assert next_cycle_context.hypotheses[0].evidence_ids == (
        "e-prior",
        "e-new",
    )
    assert {item.evidence_id for item in next_cycle_context.material_evidence} >= {
        "e-prior",
        "e-new",
    }


def test_missing_terminal_text_cannot_clear_every_hypothesis_gap() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures/framework/lr_024_missing_terminal_text_consistency_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState.model_validate(fixture["caseState"])
    common = {
        "limits": AgentLimits(),
        "known_tool_names": {"fetch_articles"},
        "material_evidence_ids": ("e-delegating-rule",),
        "required_dependency_kind": "lower_norm",
        "required_dependency_work_item_ids": ("wi-3",),
        "require_dependency_decisions": True,
        "allow_dependency_action_without_tool": True,
        "finalize_only": False,
    }

    with pytest.raises(
        ContractViolation,
        match="missing lower-norm text requires a concrete Hypothesis gap",
    ):
        apply_solver_decision(
            state,
            SolverDecision.model_validate(fixture["badDecision"]),
            **common,
        )

    applied = apply_solver_decision(
        state,
        SolverDecision.model_validate(fixture["correctedDecision"]),
        **common,
    )
    hypothesis = applied.hypotheses[0]
    assert hypothesis.judgment == "supported"
    assert hypothesis.gaps == ("内閣府令で定める例外の具体的条件",)
    assert applied.work_items[0].state == "open"


def test_dependency_update_appends_new_basis_to_previous_basis() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures/framework/lr_024_missing_terminal_text_consistency_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState.model_validate(fixture["caseState"])
    terminal_evidence = Evidence(
        evidence_id="e-terminal-rule",
        source_ref="fixture:terminal-rule",
        content="内閣府令で例外の具体的条件を定める。",
        created_cycle=1,
        metadata={
            "articleId": "article-terminal-rule",
            "citationEligible": True,
        },
    )
    state = state.model_copy(
        update={
            "evidence": (*state.evidence, terminal_evidence),
            "dependency_decisions": (
                DependencyDecision(
                    dependency_kind="lower_norm",
                    work_item_id="wi-3",
                    status="needs_action",
                    reason="末端本文が未確認である。",
                    basis_evidence_ids=("e-delegating-rule",),
                ),
            ),
        }
    )
    decision = SolverDecision(
        next="continue",
        decision_reason="末端本文を確認した。",
        update={
            "update_hypotheses": (
                HypothesisUpdate(
                    hypothesis_id="h-3",
                    judgment="supported",
                    evidence_ids=("e-terminal-rule",),
                    gaps=(),
                ),
            )
        },
        dependency_decisions=(
            DependencyDecision(
                dependency_kind="lower_norm",
                work_item_id="wi-3",
                status="resolved",
                reason="委任元と末端本文を確認した。",
                basis_evidence_ids=("e-terminal-rule",),
            ),
        ),
    )

    applied = apply_solver_decision(
        state,
        decision,
        limits=AgentLimits(),
        known_tool_names={"fetch_articles"},
        material_evidence_ids=("e-terminal-rule",),
        required_dependency_kind="lower_norm",
        required_dependency_work_item_ids=("wi-3",),
        require_dependency_decisions=True,
        finalize_only=False,
    )

    assert applied.dependency_decisions[0].status == "resolved"
    assert applied.dependency_decisions[0].basis_evidence_ids == (
        "e-delegating-rule",
        "e-terminal-rule",
    )


def test_cycle_boundary_requires_a_structural_resolution_for_every_deferred_frontier(
) -> None:
    state = CaseState(case_id="case-1", question="質問", research_cycle_count=1)

    with pytest.raises(ContractViolation, match="every active deferred Frontier"):
        apply_solver_decision(
            state,
            SolverDecision(next="finalize", answer=FinalAnswer(text="回答")),
            limits=AgentLimits(),
            known_tool_names={"fetch_articles"},
            material_evidence_ids=(),
            graph_review_fetch_tool_name="fetch_articles",
            deferred_frontiers={"f1": ("article-1", "w1", "h1")},
            finalize_only=False,
        )


def test_cycle_boundary_accepts_solver_no_longer_needed_judgment() -> None:
    state = CaseState(case_id="case-1", question="質問", research_cycle_count=1)
    resolution = DeferredFrontierResolution(
        frontier_item_id="f1",
        article_id="article-1",
        work_item_id="w1",
        hypothesis_id="h1",
        action="no_longer_needed",
        reason="後続本文により質問への回答には不要と判断した",
    )

    updated = apply_solver_decision(
        state,
        SolverDecision(
            next="finalize",
            deferred_frontier_resolutions=(resolution,),
            answer=FinalAnswer(text="回答"),
        ),
        limits=AgentLimits(),
        known_tool_names={"fetch_articles"},
        material_evidence_ids=(),
        graph_review_fetch_tool_name="fetch_articles",
        deferred_frontiers={"f1": ("article-1", "w1", "h1")},
        finalize_only=False,
    )

    assert updated.deferred_frontier_resolutions[0].action == "no_longer_needed"
    assert updated.deferred_frontier_resolutions[0].decided_cycle == 1


def test_fetch_next_cycle_must_match_the_cycle_start_fetch() -> None:
    state = CaseState(
        case_id="case-1",
        question="質問",
        research_cycle_count=1,
        work_items=(WorkItem(work_item_id="w1", question="確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="Article本文が必要である",
            ),
        ),
    )
    resolution = DeferredFrontierResolution(
        frontier_item_id="f1",
        article_id="article-1",
        work_item_id="w1",
        hypothesis_id="h1",
        action="fetch_next_cycle",
        reason="次Cycleで本文を確認する",
    )
    decision = SolverDecision(
        next="continue",
        start_next_cycle=True,
        next_focus_work_item_ids=("w1",),
        deferred_frontier_resolutions=(resolution,),
        tool_requests=(
            ToolRequest(
                request_id="fetch-1",
                work_item_id="w1",
                tool_name="fetch_articles",
                arguments={"article_ids": ["article-1"]},
                purpose="保留したArticle本文を確認する",
                hypothesis_ids=("h1",),
            ),
        ),
    )

    updated = apply_solver_decision(
        state,
        decision,
        limits=AgentLimits(),
        known_tool_names={"fetch_articles"},
        material_evidence_ids=(),
        fetchable_article_ids=("article-1",),
        graph_review_fetch_tool_name="fetch_articles",
        deferred_frontiers={"f1": ("article-1", "w1", "h1")},
        finalize_only=False,
    )
    assert updated.tool_requests[-1].arguments["article_ids"] == ["article-1"]

    missing_fetch = decision.model_copy(update={"tool_requests": ()})
    projected = apply_solver_decision(
        state,
        missing_fetch,
        limits=AgentLimits(),
        known_tool_names={"fetch_articles"},
        material_evidence_ids=(),
        fetchable_article_ids=("article-1",),
        graph_review_fetch_tool_name="fetch_articles",
        deferred_frontiers={"f1": ("article-1", "w1", "h1")},
        finalize_only=False,
    )
    assert projected.deferred_frontier_resolutions[0].action == "fetch_next_cycle"


def test_carry_forward_keeps_a_deferred_frontier_without_forcing_a_fetch() -> None:
    state = CaseState(
        case_id="case-1",
        question="質問",
        research_cycle_count=1,
        work_items=(WorkItem(work_item_id="w1", question="確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="Article本文が必要である",
            ),
        ),
    )
    resolution = DeferredFrontierResolution(
        frontier_item_id="f1",
        article_id="article-1",
        work_item_id="w1",
        hypothesis_id="h1",
        action="carry_forward",
        reason="次Cycleの取得上限外なのでactive候補として保持する",
    )

    updated = apply_solver_decision(
        state,
        SolverDecision(
            next="continue",
            start_next_cycle=True,
            deferred_frontier_resolutions=(resolution,),
        ),
        limits=AgentLimits(),
        known_tool_names={"fetch_articles"},
        material_evidence_ids=(),
        graph_review_fetch_tool_name="fetch_articles",
        deferred_frontiers={"f1": ("article-1", "w1", "h1")},
        finalize_only=False,
    )

    assert updated.deferred_frontier_resolutions[0].action == "carry_forward"


def test_agent_loop_mechanically_projects_fetch_next_cycle_resolutions() -> None:
    profile = legal_profiles.legal_agent_profile()
    loop = object.__new__(AgentLoop)
    loop._profile = profile
    resolution = DeferredFrontierResolution(
        frontier_item_id="f1",
        article_id="article-1",
        work_item_id="w1",
        hypothesis_id="h1",
        action="fetch_next_cycle",
        reason="次Cycleで取得する",
    )

    request = loop._deferred_frontier_fetch_request(
        CaseState(case_id="case-1", question="質問", research_cycle_count=1),
        (resolution,),
    )

    assert request.tool_name == "fetch_articles"
    assert request.arguments == {"article_ids": ["article-1"]}
    assert request.work_item_id == "w1"
    assert request.hypothesis_ids == ("h1",)


def test_solver_can_start_a_cycle_to_review_preserved_graph_candidates() -> None:
    decision = SolverDecision(
        next="continue",
        start_next_cycle=True,
        unreviewed_graph_resolution=UnreviewedGraphResolution(
            action="review_next_cycle",
            reason="次Cycleで未評価候補を確認する",
        ),
    )

    updated = apply_solver_decision(
        CaseState(case_id="case-1", question="質問", research_cycle_count=1),
        decision,
        limits=AgentLimits(),
        known_tool_names=set(),
        material_evidence_ids=(),
        unreviewed_graph_candidate_count=5,
        finalize_only=False,
    )

    assert updated.final_answer is None
    assert updated.unreviewed_graph_resolutions[0].candidate_count == 5


def test_cycle_boundary_cannot_silently_ignore_unreviewed_graph_candidates() -> None:
    with pytest.raises(ContractViolation, match="unreviewed Graph candidate pool"):
        apply_solver_decision(
            CaseState(case_id="case-1", question="質問", research_cycle_count=1),
            SolverDecision(next="finalize", answer=FinalAnswer(text="回答")),
            limits=AgentLimits(),
            known_tool_names=set(),
            material_evidence_ids=(),
            unreviewed_graph_candidate_count=5,
            finalize_only=False,
        )


def test_cycle_boundary_schema_requires_unreviewed_graph_resolution() -> None:
    context = build_solver_context(
        CaseState(case_id="case-1", question="質問", research_cycle_count=1),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    ).model_copy(
        update={
            "graph_review_batch": GraphReviewBatch(remaining_unreviewed_count=5),
            "cycle_budget_reached": True,
            "cycle_close_required": True,
        }
    )

    schema = _solver_transport_schema(context)

    resolution_schema = schema["properties"]["unreviewed_graph_resolution"]
    assert resolution_schema["type"] == "object"
    assert "anyOf" not in resolution_schema


def test_compact_transport_structures_update_and_tool_arguments() -> None:
    context = build_solver_context(
        CaseState(case_id="case-1", question="質問"),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )

    schema = _solver_compact_transport_schema(context)
    properties = schema["properties"]

    assert "update" in properties
    assert "tool_requests" in properties
    assert "update_json" not in properties
    assert "tool_requests_json" not in properties
    request_properties = properties["tool_requests"]["items"]["properties"]
    assert "arguments" in request_properties
    assert "arguments_json" not in request_properties
    assert properties["update"]["properties"]["add_work_items"]["minItems"] == 1
    assert properties["update"]["properties"]["add_hypotheses"]["minItems"] == 1

    normalized = _normalize_solver_payload(
        {
            "next": "continue",
            "start_next_cycle": False,
            "update": {
                "add_work_items": [],
                "update_work_items": [],
                "add_hypotheses": [],
                "update_hypotheses": [],
                "impact_decisions": [],
            },
            "next_focus_work_item_ids": [],
            "retain_evidence_ids": [],
            "dependency_decisions": [],
            "graph_candidate_review": None,
            "frontier_re_adoptions": [],
            "deferred_frontier_resolutions": [],
            "unreviewed_graph_resolution": None,
            "tool_requests": [
                {
                    "request_id": "r1",
                    "work_item_id": "w1",
                    "tool_name": "legal_search",
                    "arguments_json": '{"query":"公開買付け 公告"}',
                    "purpose": "手続を探す",
                    "hypothesis_ids": ["h1"],
                }
            ],
            "answer": None,
        }
    )
    assert normalized["tool_requests"][0]["arguments"] == {
        "query": "公開買付け 公告"
    }

    finalized = _normalize_solver_payload(
        {
            "next": "finalize",
            "start_next_cycle": True,
            "update": {},
            "tool_requests": [
                {
                    "request_id": "unused",
                    "work_item_id": "w1",
                    "tool_name": "legal_search",
                    "arguments_json": '{"query":"unused"}',
                    "purpose": "unused",
                    "hypothesis_ids": [],
                }
            ],
            "frontier_re_adoptions": [
                {
                    "article_id": "article-1",
                    "work_item_id": "w1",
                    "hypothesis_id": "h1",
                    "reason": "unused",
                }
            ],
            "answer": {"text": "回答"},
        }
    )
    assert finalized["start_next_cycle"] is False
    assert finalized["tool_requests"] == []


    assert finalized["frontier_re_adoptions"] == []

    normalized_dependencies = _normalize_solver_payload(
        {
            "next": "continue",
            "dependency_decisions": [
                {
                    "dependency_kind": "lower_norm",
                    "work_item_id": "w1",
                    "status": "resolved",
                    "reason": "確認済み",
                    "basis_evidence_ids": ["upper", "lower"],
                    "action_request_id": "unused",
                },
                {
                    "dependency_kind": "lower_norm",
                    "work_item_id": "w2",
                    "status": "not_required",
                    "reason": "不要",
                    "basis_evidence_ids": ["body"],
                    "action_request_id": "unused-2",
                },
            ],
            "tool_requests": [],
        }
    )["dependency_decisions"]
    assert normalized_dependencies[0]["action_request_id"] is None
    assert normalized_dependencies[0]["basis_evidence_ids"] == ["upper", "lower"]
    assert normalized_dependencies[1]["action_request_id"] is None
    assert normalized_dependencies[1]["basis_evidence_ids"] == ["body"]


@pytest.mark.parametrize(
    "violation",
    [
        (
            "Article body fetches in one SolverDecision must be consolidated "
            "into exactly one request"
        ),
        "dependency action must reference a ToolRequest in the same decision",
    ],
)
def test_contract_repair_limits_consolidation_to_one_tool_request(
    violation: str,
) -> None:
    previous = SolverDecision(
        next="continue",
        tool_requests=(
            ToolRequest(
                request_id="fetch-1",
                work_item_id="w1",
                tool_name="fetch_articles",
                arguments={"article_ids": ["article-1"]},
                purpose="本文を取得する",
                hypothesis_ids=("h1",),
            ),
        ),
    )
    context = build_solver_context(
        CaseState(case_id="case-repair", question="質問"),
        AgentLimits(max_tool_requests_per_step=4),
        remaining_wall_time_sec=60,
        finalize_only=False,
        contract_feedback=SolverContractFeedback(
            violation=violation,
            previous_decision=previous,
        ),
    )

    schema = _solver_compact_transport_schema(context)

    assert schema["properties"]["tool_requests"]["maxItems"] == 1


def test_solver_payload_consolidates_fetch_transport_without_dropping_links() -> None:
    normalized = _normalize_solver_payload(
        {
            "next": "continue",
            "dependency_decisions": [
                {
                    "dependency_kind": "lower_norm",
                    "work_item_id": "w1",
                    "status": "needs_action",
                    "reason": "下位規範を取得する",
                    "basis_evidence_ids": ["e1"],
                    "action_request_id": "fetch-1",
                },
                {
                    "dependency_kind": "lower_norm",
                    "work_item_id": "w2",
                    "status": "needs_action",
                    "reason": "下位規範を取得する",
                    "basis_evidence_ids": ["e2"],
                    "action_request_id": None,
                },
            ],
            "tool_requests": [
                {
                    "request_id": "fetch-1",
                    "work_item_id": "w1",
                    "tool_name": "fetch_articles",
                    "arguments": {"article_ids": ["article-1"]},
                    "purpose": "第一の本文を取得する",
                    "hypothesis_ids": ["h1"],
                },
                {
                    "request_id": "fetch-2",
                    "work_item_id": "w2",
                    "tool_name": "fetch_articles",
                    "arguments": {"article_ids": ["article-2", "article-1"]},
                    "purpose": "第二の本文を取得する",
                    "hypothesis_ids": ["h2"],
                },
            ],
        }
    )

    assert len(normalized["tool_requests"]) == 1
    request = normalized["tool_requests"][0]
    assert request["arguments"]["article_ids"] == ["article-1", "article-2"]
    assert request["hypothesis_ids"] == ["h1", "h2"]
    assert {
        item["action_request_id"]
        for item in normalized["dependency_decisions"]
    } == {"fetch-1"}


def test_identical_tool_executions_must_be_consolidated() -> None:
    state = CaseState(
        case_id="case-duplicate-tool-scope",
        question="質問",
        work_items=(WorkItem(work_item_id="w1", question="確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="条件を確認する",
            ),
            Hypothesis(
                hypothesis_id="h2",
                work_item_id="w1",
                statement="例外を確認する",
            ),
        ),
    )
    graph_arguments = {
        "article_ids": ["article-1"],
        "mode": "semantic_assertion",
        "predicate": "IMPLEMENTS",
        "direction": "from_subject",
        "max_relations": 20,
    }
    decision = SolverDecision(
        next="continue",
        tool_requests=(
            ToolRequest(
                request_id="graph-1",
                work_item_id="w1",
                tool_name="legal_graph_neighbors",
                arguments=graph_arguments,
                purpose="条件の下位規範を探す",
                hypothesis_ids=("h1",),
            ),
            ToolRequest(
                request_id="graph-2",
                work_item_id="w1",
                tool_name="legal_graph_neighbors",
                arguments=graph_arguments,
                purpose="例外の下位規範を探す",
                hypothesis_ids=("h2",),
            ),
        ),
    )

    with pytest.raises(
        ContractViolation,
        match="identical legal_graph_neighbors arguments must be consolidated",
    ):
        apply_solver_decision(
            state,
            decision,
            limits=AgentLimits(),
            known_tool_names={"legal_graph_neighbors"},
            material_evidence_ids=(),
            graph_known_article_ids=("article-1",),
            finalize_only=False,
        )


def test_solver_payload_consolidates_identical_graph_transport() -> None:
    graph_arguments = {
        "article_ids": ["article-1"],
        "mode": "semantic_assertion",
        "predicate": "IMPLEMENTS",
        "direction": "from_subject",
        "max_relations": 20,
    }
    normalized = _normalize_solver_payload(
        {
            "next": "continue",
            "dependency_decisions": [
                {
                    "dependency_kind": "lower_norm",
                    "work_item_id": "w1",
                    "status": "needs_action",
                    "reason": "下位規範を探す",
                    "basis_evidence_ids": ["e1"],
                    "action_request_id": "graph-1",
                },
                {
                    "dependency_kind": "lower_norm",
                    "work_item_id": "w2",
                    "status": "needs_action",
                    "reason": "下位規範を探す",
                    "basis_evidence_ids": ["e2"],
                    "action_request_id": None,
                },
            ],
            "tool_requests": [
                {
                    "request_id": "graph-1",
                    "work_item_id": "w1",
                    "tool_name": "legal_graph_neighbors",
                    "arguments": graph_arguments,
                    "purpose": "条件を確認する",
                    "hypothesis_ids": ["h1"],
                },
                {
                    "request_id": "graph-2",
                    "work_item_id": "w2",
                    "tool_name": "legal_graph_neighbors",
                    "arguments": graph_arguments,
                    "purpose": "例外を確認する",
                    "hypothesis_ids": ["h2"],
                },
            ],
        }
    )

    assert len(normalized["tool_requests"]) == 1
    request = normalized["tool_requests"][0]
    assert request["arguments"] == graph_arguments
    assert request["hypothesis_ids"] == ["h1", "h2"]
    assert {
        item["action_request_id"]
        for item in normalized["dependency_decisions"]
    } == {"graph-1"}


def test_diagnostic_integration_fixture_rebuilds_the_same_solver_context() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_cycle1_after_search_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState.model_validate(fixture["caseState"])
    saved = SolverContext.model_validate(fixture["solverContext"])
    expectations = fixture["expectations"]
    limits = AgentLimits(
        max_research_cycles=(
            saved.research_cycle_count + saved.remaining_research_cycles
        ),
        max_tool_requests_per_step=saved.max_tool_requests_per_step,
        max_fetched_resources_per_cycle=saved.max_fetched_resources_per_cycle,
        max_selected_frontier_per_step=saved.max_selected_frontier_per_step,
        max_retained_evidence=saved.max_retained_evidence,
        max_material_evidence_chars=saved.max_material_evidence_chars,
        max_solver_input_chars=saved.max_solver_input_chars,
        min_next_cycle_budget_sec=saved.min_next_cycle_budget_sec,
        max_wall_time_sec=240,
    )

    rebuilt = build_solver_context(
        state,
        limits,
        remaining_wall_time_sec=saved.remaining_wall_time_sec,
        finalize_only=saved.finalize_only,
    )

    assert rebuilt.model_dump(mode="json") == saved.model_copy(
        update={
            "completed_legal_searches": rebuilt.completed_legal_searches,
            "completed_graph_searches": rebuilt.completed_graph_searches,
        }
    ).model_dump(mode="json")
    assert rebuilt.completed_legal_searches
    assert saved.research_cycle_count == expectations["researchCycleCount"]
    assert saved.remaining_fetch_capacity == expectations["remainingFetchCapacity"]
    assert len(saved.work_tree) == expectations["workItemCount"]
    assert len(saved.hypotheses) == expectations["hypothesisCount"]
    assert len(saved.material_evidence) == expectations["materialEvidenceCount"]
    assert len(saved.recent_tool_results) == expectations["recentToolResultCount"]
    assert len(saved.fetchable_article_ids) == expectations["fetchableArticleCount"]
    assert len(saved.search_candidates) == expectations["searchCandidateCount"]
    assert {
        item.article_id for item in saved.search_candidates
    } == set(saved.fetchable_article_ids)
    ordinance_candidate = next(
        item
        for item in saved.search_candidates
        if item.article_id == "law-402M50000040038-article-2_5"
    )
    assert ordinance_candidate.discovery_work_item_ids == ("work_item_2",)
    assert ordinance_candidate.discovery_hypothesis_ids == ("hypothesis_2",)
    assert ordinance_candidate.search_request_ids == ("legal_search_2",)
    assert (
        len(saved.fetched_resource_ids_this_cycle)
        == expectations["fetchedResourceCount"]
    )
    role_counts: dict[str, int] = {}
    for evidence in saved.material_evidence:
        role = str(evidence.metadata.get("evidenceRole", "unknown"))
        role_counts[role] = role_counts.get(role, 0) + 1
    assert role_counts == expectations["evidenceRoleCounts"]


def test_real_model_initial_research_decomposition_fixture_is_reproducible() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_initial_research_decomposition_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState.model_validate(fixture["caseState"])
    saved = SolverContext.model_validate(fixture["solverContext"])
    observed = SolverDecision.model_validate(
        fixture["observedResearchOutput"]["decision"]
    )
    expected = SolverDecision.model_validate(fixture["expectedResearchDecision"])
    failure = fixture["observedFailure"]
    limits = AgentLimits(
        max_research_cycles=(
            saved.research_cycle_count + saved.remaining_research_cycles
        ),
        max_tool_requests_per_step=saved.max_tool_requests_per_step,
        max_fetched_resources_per_cycle=saved.max_fetched_resources_per_cycle,
        max_selected_frontier_per_step=saved.max_selected_frontier_per_step,
        max_retained_evidence=saved.max_retained_evidence,
        max_material_evidence_chars=saved.max_material_evidence_chars,
        max_solver_input_chars=saved.max_solver_input_chars,
        min_next_cycle_budget_sec=saved.min_next_cycle_budget_sec,
        max_wall_time_sec=240,
    )

    rebuilt = build_solver_context(
        state,
        limits,
        remaining_wall_time_sec=saved.remaining_wall_time_sec,
        finalize_only=saved.finalize_only,
        required_dependency_kind=saved.required_dependency_kind,
        required_dependency_work_item_ids=(
            saved.required_dependency_work_item_ids
        ),
        available_tools=saved.available_tools,
    )
    profile = legal_profiles.legal_agent_profile().solver_research
    assert profile.completion_check_prompt is not None
    prompt = _solver_prompt(
        saved,
        profile.system_prompt,
        completion_check_prompt=profile.completion_check_prompt,
        compact_transport=True,
    )

    assert rebuilt.model_dump(mode="json") == saved.model_copy(
        update={"completed_graph_searches": rebuilt.completed_graph_searches}
    ).model_dump(mode="json")
    assert fixture["source"]["purpose"] == "research"
    assert fixture["source"]["model"] == "gpt-4o-mini"
    assert fixture["source"]["profileVersion"] == "135"
    assert len(observed.update.add_work_items) == 3
    assert "例外と必要手続き" in observed.update.add_work_items[2].question
    assert all(
        "特定の" in item.statement
        for item in observed.update.add_hypotheses
    )
    assert len(expected.update.add_work_items) == 4
    expected_hypotheses = {
        item.hypothesis_id: item for item in expected.update.add_hypotheses
    }
    for work_item in expected.update.add_work_items:
        assert work_item.basis_hypothesis_ids == ()
        assert any(
            hypothesis.work_item_id == work_item.work_item_id
            for hypothesis in expected_hypotheses.values()
        )
    for request in expected.tool_requests:
        assert len(request.hypothesis_ids) == 1
        hypothesis = expected_hypotheses[request.hypothesis_ids[0]]
        assert request.work_item_id == hypothesis.work_item_id
    assert failure["stage"] == "initial_research"
    assert {
        "law-402M50000040038-article-2_5",
        "law-402M50000040038-article-10",
    } == set(failure["downstreamObservation"]["reselectionDroppedArticleIds"])
    assert "法的論点を自然言語の問いとして1件" in prompt
    assert "複数の法的論点がある場合は、WorkItemを分けます" in prompt
    hypothesis_prompt = legal_profiles.legal_agent_profile().solver_hypothesis_generation
    search_prompt = legal_profiles.legal_agent_profile().solver_search_planning
    assert hypothesis_prompt is not None
    assert search_prompt is not None
    assert "結論の判定に必要な未確認事項" in (
        hypothesis_prompt.system_prompt
    )
    assert "質問にない判定軸や基準" in (
        hypothesis_prompt.system_prompt
    )
    assert "短い法令用語・法令表現の組合せ" in search_prompt.system_prompt


def test_real_model_cycle2_repeated_search_fixture_keeps_replanning_options() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_cycle2_repeated_search_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState.model_validate(fixture["caseState"])
    saved = SolverContext.model_validate(fixture["solverContext"])
    failure = fixture["observedFailure"]
    limits = AgentLimits(
        max_research_cycles=(
            saved.research_cycle_count + saved.remaining_research_cycles
        ),
        max_tool_requests_per_step=saved.max_tool_requests_per_step,
        max_fetched_resources_per_cycle=saved.max_fetched_resources_per_cycle,
        max_selected_frontier_per_step=saved.max_selected_frontier_per_step,
        max_retained_evidence=saved.max_retained_evidence,
        max_material_evidence_chars=saved.max_material_evidence_chars,
        max_solver_input_chars=saved.max_solver_input_chars,
        min_next_cycle_budget_sec=saved.min_next_cycle_budget_sec,
        max_wall_time_sec=240,
    )

    rebuilt = build_solver_context(
        state,
        limits,
        remaining_wall_time_sec=saved.remaining_wall_time_sec,
        finalize_only=saved.finalize_only,
        required_dependency_kind=saved.required_dependency_kind,
        required_dependency_work_item_ids=(
            saved.required_dependency_work_item_ids
        ),
        available_tools=saved.available_tools,
    )
    assert rebuilt.model_dump(mode="json") == saved.model_copy(
        update={
            "completed_legal_searches": rebuilt.completed_legal_searches,
            "completed_graph_searches": rebuilt.completed_graph_searches,
            "evidence_hypothesis_candidates": (
                rebuilt.evidence_hypothesis_candidates
            ),
                "required_search_review_request_ids": (
                    rebuilt.required_search_review_request_ids
                ),
                "material_evidence": rebuilt.material_evidence,
                "evidence_manifest": rebuilt.evidence_manifest,
                "grounding_evidence_ids": rebuilt.grounding_evidence_ids,
                "navigation_evidence_ids": rebuilt.navigation_evidence_ids,
            }
        ).model_dump(mode="json")
    assert rebuilt.completed_legal_searches
    assert rebuilt.required_search_review_request_ids == ()
    assert rebuilt.search_candidates
    assert fixture["source"]["model"] == "gpt-4o-mini"
    assert failure["violation"] == (
        "successful legal_search scope was already completed"
    )
    assert failure["contractAttemptCount"] == 3
    assert {
        "law-402M50000040038-article-2_5",
        "law-402M50000040038-article-10",
    } <= set(saved.fetchable_article_ids)


def test_lr_003_second_hop_fixture_keeps_graph_replanning_actionable() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "lr_003_second_hop_integration_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState.model_validate(fixture["caseState"])
    saved = SolverContext.model_validate(fixture["solverContext"])
    expected = fixture["expectedBehavior"]
    limits = AgentLimits(
        max_research_cycles=(
            saved.research_cycle_count + saved.remaining_research_cycles
        ),
        max_tool_requests_per_step=saved.max_tool_requests_per_step,
        max_fetched_resources_per_cycle=saved.max_fetched_resources_per_cycle,
        max_selected_frontier_per_step=saved.max_selected_frontier_per_step,
        max_retained_evidence=saved.max_retained_evidence,
        max_material_evidence_chars=saved.max_material_evidence_chars,
        max_solver_input_chars=saved.max_solver_input_chars,
        min_next_cycle_budget_sec=saved.min_next_cycle_budget_sec,
        max_wall_time_sec=240,
    )

    rebuilt = build_solver_context(
        state,
        limits,
        remaining_wall_time_sec=saved.remaining_wall_time_sec,
        finalize_only=False,
        required_dependency_kind=saved.required_dependency_kind,
        required_dependency_work_item_ids=(
            saved.required_dependency_work_item_ids
        ),
        available_tools=saved.available_tools,
    )
    assert rebuilt.model_dump(mode="json") == saved.model_copy(
        update={"completed_graph_searches": rebuilt.completed_graph_searches}
    ).model_dump(mode="json")
    assert rebuilt.completed_graph_searches
    completed_graph = rebuilt.completed_graph_searches[0]
    assert completed_graph.arguments["mode"] == "semantic_assertion"
    assert completed_graph.candidate_article_ids
    assert completed_graph.new_candidate_article_ids == (
        completed_graph.candidate_article_ids
    )
    assert rebuilt.fetchable_article_ids == ()
    assert rebuilt.graph_review_selection_limit == 1
    assert any(
        item.article_id == expected["graphOriginArticleId"]
        and item.review_status == "selected"
        and item.content_status == "succeeded"
        for item in rebuilt.graph_review_ledger
    )
    assert any(
        expected["graphOriginArticleId"]
        == evidence.metadata.get("articleId")
        for evidence in rebuilt.material_evidence
    )
    assert rebuilt.hypotheses[0].gaps

    profile = legal_profiles.legal_agent_profile().solver_integration
    prompt = _solver_prompt(
        rebuilt,
        profile.system_prompt,
        completion_check_prompt=profile.completion_check_prompt,
        compact_transport=True,
    )
    assert "`hypotheses[].gaps`は、その命題に残る具体的な未確認事項" in prompt
    assert "次の1ホップ探索の起点にできます" in prompt
    assert "順番は固定しません" in prompt
    assert "起点Articleと調べる関係・方向を説明できる" in prompt
    assert "`semantic_assertion / IMPLEMENTS / from_subject`" not in prompt

    graph_request = ToolRequest(
        request_id="graph-order-to-ordinance",
        work_item_id="wi-exception",
        tool_name="legal_graph_neighbors",
        arguments={
            "article_ids": [expected["graphOriginArticleId"]],
            "mode": expected["graphMode"],
            "predicate": expected["graphPredicate"],
            "direction": expected["graphDirection"],
            "max_relations": 20,
        },
        purpose="施行令第七条が委任する府令を1ホップ確認する",
        hypothesis_ids=("h-exception",),
    )
    decision = SolverDecision(
        next=expected["next"],
        next_focus_work_item_ids=("wi-exception",),
        dependency_decisions=(
            DependencyDecision(
                dependency_kind="lower_norm",
                work_item_id="wi-exception",
                status=expected["dependencyStatus"],
                reason="府令の具体的なArticleと条件が未確認",
                basis_evidence_ids=(
                    "law-340CO0000000321-article-7-paragraph-1-item-9",
                ),
                action_request_id=graph_request.request_id,
            ),
        ),
        tool_requests=(graph_request,),
    )
    updated = apply_solver_decision(
        state,
        decision,
        limits=limits,
        known_tool_names={item.name for item in rebuilt.available_tools},
        material_evidence_ids=tuple(
            item.evidence_id for item in rebuilt.material_evidence
        ),
        fetchable_article_ids=rebuilt.fetchable_article_ids,
        graph_known_article_ids=tuple(
            item.article_id for item in rebuilt.graph_review_ledger
        ),
        graph_review_fetch_tool_name="fetch_articles",
        finalize_only=False,
    )
    assert updated.tool_requests[-1] == graph_request


def test_lr_003_graph_review_fixture_exposes_one_effective_selection_slot() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "lr_003_second_hop_graph_review_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    expected = fixture["expectedBehavior"]

    assert context.graph_review_selection_limit == expected[
        "graphReviewSelectionLimit"
    ]
    assert {
        item.article_id for item in context.graph_review_batch.candidates
    } == set(expected["selectOneOfArticleIds"])

    profile = legal_profiles.legal_agent_profile().solver_graph_review
    assert profile is not None
    prompt = _solver_prompt(
        context,
        profile.system_prompt,
        completion_check_prompt=profile.completion_check_prompt,
        compact_transport=True,
    )
    assert "`select`は現在の検証で使う判断" in prompt
    assert "本文未取得の関連候補" in prompt


def test_graph_review_reloads_selected_fetched_article_without_refetching() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_announcement_graph_review_selects_fetched_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState.model_validate(fixture["caseState"])
    context = SolverContext.model_validate(fixture["solverContext"])
    decision = SolverDecision.model_validate(fixture["observedSolverDecision"])
    review = decision.graph_candidate_review
    assert review is not None

    updated = apply_solver_decision(
        state,
        decision,
        limits=legal_profiles.legal_agent_profile().limits,
        known_tool_names={item.name for item in context.available_tools},
        material_evidence_ids=context.material_evidence_ids,
        fetchable_article_ids=context.fetchable_article_ids,
        required_graph_review_request_ids=(
            context.required_graph_review_request_ids
        ),
        graph_candidate_article_ids=tuple(
            item.article_id
            for item in context.graph_review_batch.candidates
            if item.content_status in {"not_requested", "failed", "timeout"}
        ),
        graph_known_article_ids=tuple(
            item.article_id for item in context.graph_review_batch.candidates
        ),
        graph_review_fetch_tool_name="fetch_articles",
        graph_review_frontiers={
            item.frontier_item_id: (
                item.article_id,
                item.work_item_id,
                item.hypothesis_id,
            )
            for item in context.graph_review_batch.candidates
        },
        graph_review_link_ids=tuple(
            link.link_id
            for item in context.graph_review_batch.candidates
            for link in item.links
        ),
        graph_selectable_frontiers={
            item.frontier_item_id: (
                item.article_id,
                item.work_item_id,
                item.hypothesis_id,
            )
            for item in context.graph_review_batch.candidates
            if item.content_status in {"not_requested", "failed", "timeout"}
        },
        remaining_fetch_capacity=0,
        finalize_only=False,
    )
    assert updated.graph_candidate_reviews[-1] == review.model_copy(
        update={"reviewed_cycle": 1}
    )

    loop = AgentLoop(
        store=InMemoryCaseStore(),
        model=StructuredJSONModelAdapter(FakeStructuredLLM()),
        tools=ToolRegistry(()),
        profile=legal_profiles.legal_agent_profile(),
    )
    request = loop._graph_review_fetch_request(
        updated,
        review,
        fetchable_article_ids={
            item.article_id
            for item in context.graph_review_batch.candidates
            if item.content_status in {"not_requested", "failed", "timeout"}
        },
    )
    assert request is None
    load_request = loop._graph_review_load_request(
        updated,
        review,
        fetchable_article_ids={
            item.article_id
            for item in context.graph_review_batch.candidates
            if item.content_status in {"not_requested", "failed", "timeout"}
        },
    )
    assert load_request is not None
    assert load_request.tool_name == "load_evidence"
    assert load_request.work_item_id == review.frontier_decisions[0].work_item_id
    assert load_request.hypothesis_ids == (
        review.frontier_decisions[0].hypothesis_id,
    )
    assert load_request.arguments["evidence_ids"]


def test_graph_review_reloads_reassigned_fetched_article_for_hypothesis() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_selected_fetched_graph_evidence_not_reloaded_v424.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState.model_validate(fixture["caseState"])
    context = SolverContext.model_validate(fixture["solverContext"])
    decision = SolverDecision.model_validate(fixture["observedSolverDecision"])
    review = decision.graph_candidate_review
    assert review is not None
    selected = [
        item for item in review.frontier_decisions if item.action == "select"
    ]
    assert [(item.article_id, item.hypothesis_id) for item in selected] == [
        ("law-402M50000040038-article-2_5", "h-3")
    ]

    loop = AgentLoop(
        store=InMemoryCaseStore(),
        model=StructuredJSONModelAdapter(FakeStructuredLLM()),
        tools=ToolRegistry(()),
        profile=legal_profiles.legal_agent_profile(),
    )
    fetchable_ids = {
        item.article_id
        for item in context.graph_review_batch.candidates
        if item.content_status in {"not_requested", "failed", "timeout"}
    }
    assert loop._graph_review_fetch_request(
        state,
        review,
        fetchable_article_ids=fetchable_ids,
    ) is None

    load_request = loop._graph_review_load_request(
        state,
        review,
        fetchable_article_ids=fetchable_ids,
    )

    assert load_request is not None
    assert load_request.work_item_id == "wi-3"
    assert load_request.hypothesis_ids == ("h-3",)
    assert {
        "law-402M50000040038-article-2_5-paragraph-1",
        "law-402M50000040038-article-2_5-paragraph-2",
    } <= set(load_request.arguments["evidence_ids"])


def test_legal_cycle_close_disables_separate_final_answer_check() -> None:
    profile = legal_profiles.legal_agent_profile().solver_cycle_close
    assert profile is not None
    assert profile.final_answer_check_system_prompt is None
    assert profile.final_answer_check_completion_prompt is None


def test_integration_uses_selected_fetched_graph_article_before_more_tools() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_announcement_integration_repeats_reviewed_graph_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    selected = [
        item
        for item in context.graph_review_ledger
        if item.review_status == "selected" and item.content_status == "succeeded"
    ]
    assert [item.article_id for item in selected] == [
        "law-402M50000040038-article-10"
    ]
    assert fixture["expectedViolation"].startswith(
        "successful legal_graph_neighbors scope was already completed"
    )

    profile = legal_profiles.legal_agent_profile().solver_integration
    assert "本文取得前の候補を、Hypothesisの支持・反証や回答根拠" in (
        profile.system_prompt
    )
    assert "`request_id`または`purpose`だけの変更は同じscope" in (
        profile.completion_check_prompt or ""
    )


def test_cycle_carry_prioritizes_declared_hypothesis_evidence() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_cycle2_dependency_evidence_omitted_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState.model_validate(fixture["caseState"])
    required_ids = {
        evidence_id
        for hypothesis in state.hypotheses
        for evidence_id in hypothesis.evidence_ids
    }
    assert {
        "law-402M50000040038-article-2_5-paragraph-1",
        "law-402M50000040038-article-2_5-paragraph-2",
    } <= required_ids

    rebuilt = build_solver_context(
        state,
        legal_profiles.legal_agent_profile().limits,
        remaining_wall_time_sec=180,
        finalize_only=False,
    )

    assert required_ids <= rebuilt.material_evidence_ids


def test_observation_does_not_fallback_to_unmapped_articles() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_observation_unmapped_work_item_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])

    projected = _observation_work_item_contexts(context)
    by_work_item = {item.work_tree[0].work_item_id: item for item in projected}

    assert context.evidence_hypothesis_candidates
    assert by_work_item["wi-1"].evidence_hypothesis_candidates == ()
    assert by_work_item["wi-1"].material_evidence == ()
    assert by_work_item["wi-1"].grounding_evidence_ids == ()


def test_observation_only_revisits_work_items_from_recent_tool_results() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_exceptions_observation_misses_scoped_evidence_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    graph_fetch = next(
        item
        for item in context.recent_tool_requests
        if item.request_id.startswith("graph-review-fetch-")
    )
    graph_result = next(
        item
        for item in context.recent_tool_results
        if item.request_id == graph_fetch.request_id
    )
    scoped = context.model_copy(
        update={
            "recent_tool_requests": (graph_fetch,),
            "recent_tool_results": (graph_result,),
        }
    )

    projected = _observation_work_item_contexts(scoped)

    assert [item.work_tree[0].work_item_id for item in projected] == ["wi-1"]
    assert [item.hypothesis_id for item in projected[0].hypotheses] == ["h-1"]


def test_observation_only_sends_only_the_latest_fetched_article_text() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_exceptions_observation_misses_scoped_evidence_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    graph_fetch = next(
        item
        for item in context.recent_tool_requests
        if item.request_id.startswith("graph-review-fetch-")
    )
    graph_result = next(
        item
        for item in context.recent_tool_results
        if item.request_id == graph_fetch.request_id
    )
    scoped = context.model_copy(
        update={
            "recent_tool_requests": (graph_fetch,),
            "recent_tool_results": (graph_result,),
        }
    )

    projected = _observation_work_item_contexts(scoped)

    assert len(projected) == 1
    assert {
        item.metadata.get("articleId")
        for item in projected[0].material_evidence
    } == {"law-340CO0000000321-article-7"}
    assert all(
        item.evidence_id in graph_result.evidence_ids
        for item in projected[0].material_evidence
    )


def test_observation_does_not_offer_completed_load_evidence_again() -> None:
    load_request = ToolRequest(
        request_id="load-1",
        work_item_id="wi-1",
        tool_name="load_evidence",
        purpose="省略本文を確認する",
        hypothesis_ids=("h-1",),
        arguments={"evidence_ids": ["e-1"]},
    )
    state = CaseState(
        case_id="case-completed-load",
        question="質問",
        research_cycle_count=1,
        work_items=(WorkItem(work_item_id="wi-1", question="確認事項"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h-1",
                work_item_id="wi-1",
                statement="確認する",
            ),
        ),
        tool_requests=(load_request,),
        tool_results=(
            ToolResult(
                request_id="load-1",
                status="succeeded",
                evidence_ids=("e-1",),
                cycle_no=1,
            ),
        ),
        integrated_tool_result_request_ids=("load-1",),
        evidence=(
            Evidence(
                evidence_id="e-1",
                source_ref="test:e-1",
                content="本文",
                created_cycle=1,
            ),
        ),
    )

    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    projected = _observation_work_item_contexts(context)[0]

    assert context.completed_load_evidence_ids_by_work_item == {
        "wi-1": ("e-1",)
    }
    assert "e-1" in context.omitted_evidence_ids
    assert "e-1" not in projected.omitted_evidence_ids


def test_observation_projects_recent_text_to_one_work_item_per_context() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_exceptions_observation_misses_scoped_evidence_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    graph_fetch = next(
        item
        for item in context.recent_tool_requests
        if item.request_id.startswith("graph-review-fetch-")
    )
    graph_result = next(
        item
        for item in context.recent_tool_results
        if item.request_id == graph_fetch.request_id
    )
    scoped = context.model_copy(
        update={
            "recent_tool_requests": (graph_fetch,),
            "recent_tool_results": (graph_result,),
        }
    )

    projected_contexts = _observation_work_item_contexts(scoped)

    assert projected_contexts
    assert all(len(projected.work_tree) == 1 for projected in projected_contexts)
    assert all(
        {item.work_item_id for item in projected.hypotheses}
        == {projected.work_tree[0].work_item_id}
        for projected in projected_contexts
    )
    assert all(
        item.evidence_id in graph_result.evidence_ids
        for projected in projected_contexts
        for item in projected.material_evidence
    )
    projected = projected_contexts[0]
    profile = legal_profiles.legal_agent_profile().solver_cycle_close
    assert profile is not None
    rendered = render_observation_integration_model_call(
        projected.model_copy(update={"cycle_close_required": False}),
        profile,
    )
    assert rendered.output_schema["properties"]["tool_requests"][
        "maxItems"
    ] == min(
        len(projected.work_tree),
        projected.max_tool_requests_per_step,
    )


def test_observation_splits_four_open_work_items_into_four_contexts() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_exceptions_observation_misses_scoped_evidence_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    first_work_item, second_work_item = context.work_tree
    first_hypothesis, second_hypothesis = context.hypotheses
    expanded = context.model_copy(
        update={
            "work_tree": (
                *context.work_tree,
                first_work_item.model_copy(update={"work_item_id": "wi-3"}),
                second_work_item.model_copy(update={"work_item_id": "wi-4"}),
            ),
            "hypotheses": (
                *context.hypotheses,
                first_hypothesis.model_copy(
                    update={"hypothesis_id": "h-3", "work_item_id": "wi-3"}
                ),
                second_hypothesis.model_copy(
                    update={"hypothesis_id": "h-4", "work_item_id": "wi-4"}
                ),
            ),
        }
    )

    projected_contexts = _observation_work_item_contexts(
        expanded.model_copy(
            update={"recent_tool_requests": (), "recent_tool_results": ()}
        )
    )

    assert [len(item.work_tree) for item in projected_contexts] == [1, 1, 1, 1]
    assert [len(item.hypotheses) for item in projected_contexts] == [1, 1, 1, 1]
    assert [
        item.work_tree[0].work_item_id for item in projected_contexts
    ] == ["wi-1", "wi-2", "wi-3", "wi-4"]


def test_observation_prompt_limits_follow_up_to_work_item_scope() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_announcement_observation_scope_expansion_v409.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    projected = _observation_work_item_contexts(context)[0]
    profile = legal_profiles.legal_agent_profile().solver_observation_integration
    assert profile is not None

    rendered = render_observation_integration_model_call(
        projected.model_copy(update={"cycle_close_required": False}),
        profile,
    )

    assert "質問への回答に関係しない参照先" in rendered.instructions
    assert "条件、範囲又は手続を参照先へ委ねている場合" in rendered.instructions
    assert "対応する内容を本文で確認した場合だけ削除" in rendered.instructions
    assert "親規定から具体化規定を探す場合は`from_subject`" in (
        rendered.instructions
    )
    assert len(projected.work_tree) == 1
    assert {
        item.work_item_id for item in projected.hypotheses
    } == {projected.work_tree[0].work_item_id}


def test_observation_prompt_keeps_question_scoped_delegation_open() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_exception_delegation_closed_early_v423.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    projected = next(
        item
        for item in _observation_work_item_contexts(context)
        if item.work_tree[0].work_item_id == "wi-3"
    )
    profile = legal_profiles.legal_agent_profile().solver_observation_integration
    assert profile is not None

    prior_payload = fixture["observedTransportOutput"]["payload"]
    assert prior_payload["update_hypotheses"][0]["gaps"] == []
    assert prior_payload["dependency_decisions"][0]["status"] == (
        "terminal_text_confirmed"
    )

    rendered = render_observation_integration_model_call(
        projected.model_copy(update={"cycle_close_required": False}),
        profile,
    )

    assert "質問された\n  条件、範囲又は手続を参照先へ委ねている場合" in (
        rendered.instructions
    )
    assert "本文で確認した場合だけ削除" in rendered.instructions
    assert "親規定から具体化規定を探す場合は`from_subject`" in (
        rendered.instructions
    )
    article_ids = {
        item.metadata.get("articleId") for item in projected.material_evidence
    }
    assert "law-340CO0000000321-article-7" in article_ids


def test_overview_graph_direction_fixture_exposes_subject_mapping() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_graph_direction_v425.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    observed = SolverDecision.model_validate(fixture["observedSolverDecision"])
    graph_requests = tuple(
        item
        for item in observed.tool_requests
        if item.tool_name == "legal_graph_neighbors"
    )

    assert len(graph_requests) == 4
    assert {
        item.arguments["direction"] for item in graph_requests
    } == {"to_subject"}
    assert all(
        "具体化" in item.purpose for item in graph_requests
    )

    current_tools = {
        item.name: item
        for item in (
            LegalSearchTool.definition,
            LegalFetchArticlesTool.definition,
            LegalGraphNeighborsTool.definition,
        )
    }
    context = context.model_copy(
        update={
            "available_tools": tuple(
                current_tools.get(item.name, item)
                for item in context.available_tools
            )
        }
    )
    profile = legal_profiles.legal_agent_profile().solver_integration
    rendered = render_solver_model_call(
        context,
        profile,
        provider="openai",
        stage="integration",
    )

    assert "親規定から具体化規定を探す場合は`from_subject`" in (
        rendered.instructions
    )
    graph_tool = next(
        item
        for item in rendered.input_payload["available_tools"]
        if item["name"] == "legal_graph_neighbors"
    )
    semantic_schema = next(
        item
        for item in graph_tool["input_schema"]["anyOf"]
        if item["properties"]["mode"].get("const") == "semantic_assertion"
    )
    direction_schema = semantic_schema["properties"]["direction"]
    assert "from_subjectは起点をSUBJECTとしてOBJECT側を探し" in (
        direction_schema["description"]
    )


def test_dependency_assessment_is_projected_one_work_item_at_a_time() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_announcement_integration_one_slot_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    saved = SolverContext.model_validate(fixture["solverContext"])
    required_ids = tuple(
        item.work_item_id for item in saved.work_tree[:2]
    )
    context = saved.model_copy(
        update={"required_dependency_work_item_ids": required_ids}
    )

    projected = _dependency_work_item_contexts(context)

    assert len(projected) == 2
    assert [item.required_dependency_work_item_ids for item in projected] == [
        (required_ids[0],),
        (required_ids[1],),
    ]
    assert all(len(item.work_tree) == 1 for item in projected)
    assert all(
        hypothesis.work_item_id == item.work_tree[0].work_item_id
        for item in projected
        for hypothesis in item.hypotheses
    )


def test_integration_repair_does_not_refetch_prior_cycle_articles() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_integration_refetches_prior_cycle_articles_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"]).model_copy(
        update={
            "contract_feedback": SolverContractFeedback(
                violation=fixture["expectedViolation"],
                previous_decision=SolverDecision.model_validate(
                    fixture["observedSolverDecision"]
                ),
            )
        }
    )
    profile = legal_profiles.legal_agent_profile().solver_integration

    rendered = render_solver_model_call(
        context,
        profile,
        provider="openai",
        stage="integration",
    )

    assert "violationに列挙されたArticle ID" in rendered.instructions
    assert "その要求をコピーしません" in rendered.instructions
    assert "現在の`fetchable_article_ids`から選び直します" in (
        rendered.instructions
    )
    assert "`material_evidence`にある本文を再取得しません" in (
        rendered.instructions
    )
    variants = rendered.output_schema["properties"]["tool_requests"]["items"]
    variants = variants.get("anyOf", [variants])
    fetch_variant = next(
        variant
        for variant in variants
        if variant["properties"]["tool_name"]["enum"] == ["fetch_articles"]
    )
    article_schema = fetch_variant["properties"]["arguments"]["properties"][
        "article_ids"
    ]["items"]
    assert article_schema["enum"] == list(context.fetchable_article_ids)
    assert not {
        article_id
        for request in context.contract_feedback.previous_decision.tool_requests
        for article_id in request.arguments.get("article_ids", ())
    }.intersection(article_schema["enum"])


def test_observation_keeps_each_work_item_dimension_separate() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_observation_mixes_scope_and_exception_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    profile = legal_profiles.legal_agent_profile().solver_cycle_close

    observation_call = render_observation_integration_model_call(
        context,
        profile,
    )
    assert "WorkItemの完了状態は出力しません" in observation_call.instructions
    assert "命題を読み替えず`unresolved`" in (
        observation_call.instructions
    )

    observed = ObservationIntegrationDecision.model_validate(
        fixture["observedTransportOutput"]["payload"]
    )
    dependency_call = render_dependency_assessment_model_call(
        context,
        observed,
        profile,
    )
    assert "WorkItemごとに、その確認事項と関係する規範だけ" in (
        dependency_call.instructions
    )


def test_action_feedback_removes_the_rejected_tool_kind_from_repair() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_integration_repeats_successful_graph_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    repaired_context = context.model_copy(
        update={
            "action_feedback": SolverActionFeedback(
                code="already_completed",
                message=fixture["expectedViolation"],
                rejected_tool_requests=SolverDecision.model_validate(
                    fixture["observedSolverDecision"]
                ).tool_requests,
            )
        }
    )
    rendered = render_solver_model_call(
        repaired_context,
        legal_profiles.legal_agent_profile().solver_integration,
        provider="openai",
        stage="integration",
    )
    variants = rendered.output_schema["properties"]["tool_requests"]["items"]
    variants = variants.get("anyOf", [variants])
    allowed_tools = {
        variant["properties"]["tool_name"]["enum"][0]
        for variant in variants
    }
    rejected_tool_names = {
        request.tool_name
        for request in repaired_context.action_feedback.rejected_tool_requests
    }

    assert repaired_context.can_start_next_cycle is True
    assert not (allowed_tools & rejected_tool_names)
    assert "legal_search" in allowed_tools
    assert "fetch_articles" in allowed_tools
    assert rendered.input_payload["action_feedback"]["code"] == (
        "already_completed"
    )
    assert next(iter(rendered.input_payload)) == "action_feedback"
    assert set(rendered.input_payload) == {
        "action_feedback",
        "question",
        "can_start_next_cycle",
        "work_tree",
        "hypotheses",
        "dependency_decisions",
        "required_dependency_kind",
        "required_dependency_work_item_ids",
        "material_evidence",
        "omitted_evidence_ids",
        "fetchable_article_ids",
        "search_candidates",
        "graph_review_ledger",
        "completed_legal_searches",
        "completed_graph_searches",
        "available_tools",
        "remaining_fetch_capacity",
    }
    assert "contract_feedback" not in rendered.input_payload or (
        rendered.input_payload["contract_feedback"] is None
    )
    assert "棄却されたTool種類を使わず" in (
        rendered.instructions
    )
    assert "別種のToolが適切でなければ`start_next_cycle=true`" in (
        rendered.instructions
    )
    with pytest.raises(
        ModelProtocolError,
        match="dependency action cannot use a blocked Tool kind",
    ):
        normalize_dependency_action_decision(
            {
                "decision_reason": "棄却済みのToolを再び選ぶ。",
                "start_next_cycle": False,
                "tool_requests": [
                    request.model_dump(mode="json")
                    for request in repaired_context.action_feedback.rejected_tool_requests
                ],
            },
            context=repaired_context,
        )


def test_graph_review_model_call_projects_only_selection_context() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_exceptions_graph_review_wrong_selection_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    profile = legal_profiles.legal_agent_profile().solver_graph_review
    assert profile is not None

    rendered = render_solver_model_call(context, profile, provider="openai")

    assert set(rendered.input_payload) == {
        "case_id",
        "question",
        "work_tree",
        "hypotheses",
        "graph_review_batch",
        "required_graph_review_request_ids",
        "graph_review_selection_limit",
    }
    assert rendered.input_payload["graph_review_selection_limit"] == 1
    assert "evidence_manifest" not in rendered.input_payload
    assert "recent_tool_results" not in rendered.input_payload
    assert set(rendered.output_schema["properties"]) == {
        "graph_request_ids",
        "reviewed_link_ids",
        "frontier_decisions",
        "reason",
    }
    assert "next" not in rendered.output_schema["properties"]
    assert "SolverDecision" not in rendered.instructions


def test_graph_review_output_is_limited_to_the_current_delta_batch() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_exceptions_graph_review_wrong_selection_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    context = context.model_copy(
        update={
            "graph_review_ledger": (
                GraphReviewLedgerItem(
                    frontier_item_id="ledger-frontier-1",
                    article_id="ledger-article-1",
                    title="過去の候補",
                    heading="第一条",
                    work_item_id=context.work_tree[0].work_item_id,
                    hypothesis_id=context.hypotheses[0].hypothesis_id,
                    review_status="relevant_deferred",
                    reason="過去の差分で保留した。",
                    content_status="not_requested",
                    last_reviewed_cycle=1,
                ),
            )
        }
    )
    profile = legal_profiles.legal_agent_profile().solver_graph_review
    assert profile is not None

    rendered = render_solver_model_call(context, profile, provider="openai")
    decisions = rendered.output_schema["properties"]["frontier_decisions"]
    batch_ids = {
        item.frontier_item_id for item in context.graph_review_batch.candidates
    }

    assert decisions["minItems"] == len(batch_ids)
    assert decisions["maxItems"] == len(batch_ids)
    assert set(
        decisions["items"]["properties"]["frontier_item_id"]["enum"]
    ) == batch_ids
    assert "ledger-frontier-1" not in batch_ids
    assert "graph_review_ledger" not in rendered.input_payload
    assert "prior_review_status" in rendered.instructions


def test_graph_review_prompt_keeps_selection_below_capacity_without_forcing_it() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_graph_review_underfills_fetch_capacity_v370.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    profile = legal_profiles.legal_agent_profile().solver_graph_review
    assert profile is not None

    rendered = render_solver_model_call(
        context,
        profile,
        provider="anthropic",
    )

    assert "`graph_review_selection_limit`以内で`select`" in (
        rendered.instructions
    )
    assert "関連候補が取得枠以下ならすべて" not in rendered.instructions
    assert fixture["expectedViolation"].startswith(
        "graph review left relevant fetchable Articles deferred"
    )


def test_observation_integration_projects_llm_selected_articles_per_work_item(
) -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_exceptions_observation_misses_scoped_evidence_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])

    projected_by_work_item = {
        projected.work_tree[0].work_item_id: projected
        for projected in _observation_work_item_contexts(context)
    }
    minority_owner_context = projected_by_work_item["wi-2"]

    assert {
        item.article_id
        for item in minority_owner_context.evidence_hypothesis_candidates
    } == {"law-402M50000040038-article-2_5"}
    assert {
        item.metadata.get("articleId")
        for item in minority_owner_context.material_evidence
    } == {"law-402M50000040038-article-2_5"}
    assert len(minority_owner_context.material_evidence) == 5


def test_integrated_article_remains_complete_for_later_observation() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_exceptions_observation_misses_scoped_evidence_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState.model_validate(fixture["caseState"])
    hypotheses = tuple(
        item.model_copy(
            update={
                "judgment": "supported",
                "evidence_ids": (
                    "law-402M50000040038-article-2_5-paragraph-1",
                ),
                "gaps": (),
            }
        )
        if item.hypothesis_id == "h-2"
        else item
        for item in state.hypotheses
    )
    state = state.model_copy(
        update={
            "hypotheses": hypotheses,
            "integrated_tool_result_request_ids": tuple(
                item.request_id for item in state.tool_results
            ),
        }
    )

    rebuilt = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    projected_by_work_item = {
        projected.work_tree[0].work_item_id: projected
        for projected in _observation_work_item_contexts(rebuilt)
    }
    minority_owner_context = projected_by_work_item["wi-2"]

    assert tuple(
        item.evidence_id for item in minority_owner_context.material_evidence
    ) == (
        "law-402M50000040038-article-2_5-paragraph-1",
        "law-402M50000040038-article-2_5-paragraph-2",
        "law-402M50000040038-article-2_5-paragraph-3",
        "law-402M50000040038-article-2_5-paragraph-4",
        "law-402M50000040038-article-2_5-paragraph-5",
    )


def test_cycle2_carried_candidates_are_options_for_replanning() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_cycle2_replanning_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState.model_validate(fixture["caseState"])
    expected = fixture["expectedBehavior"]
    limits = AgentLimits(max_research_cycles=4)

    context = build_solver_context(
        state,
        limits,
        remaining_wall_time_sec=180,
        finalize_only=False,
    )

    assert not context.recent_tool_results
    assert list(context.required_search_review_request_ids) == expected[
        "requiredSearchReviewRequestIds"
    ]
    assert {item.article_id for item in context.search_candidates} == set(
        expected["candidateArticleIds"]
    )
    assert set(context.fetchable_article_ids) == set(
        expected["candidateArticleIds"]
    )

    profile = legal_profiles.legal_agent_profile()
    loop = AgentLoop(
        store=InMemoryCaseStore(),
        model=StructuredJSONModelAdapter(FakeStructuredLLM()),
        tools=ToolRegistry(()),
        profile=profile,
    )
    _, purpose = loop._solver_profile_for_context(
        context=context,
        graph_review_call=False,
        search_review_call=bool(context.required_search_review_request_ids),
        integration_call=True,
        has_reviewer_findings=False,
    )
    assert purpose == expected["purpose"]
    assert profile.solver_integration.available_tool_names is None


def test_cycle3_repeated_search_fixture_projects_completed_searches() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_cycle3_repeated_search_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState.model_validate(fixture["caseState"])
    saved = SolverContext.model_validate(fixture["solverContext"])
    decision = SolverDecision.model_validate(fixture["observedSolverDecision"])
    limits = AgentLimits(
        max_research_cycles=(
            saved.research_cycle_count + saved.remaining_research_cycles
        ),
        max_tool_requests_per_step=saved.max_tool_requests_per_step,
        max_fetched_resources_per_cycle=saved.max_fetched_resources_per_cycle,
        max_selected_frontier_per_step=saved.max_selected_frontier_per_step,
        max_retained_evidence=saved.max_retained_evidence,
        max_material_evidence_chars=saved.max_material_evidence_chars,
        max_solver_input_chars=saved.max_solver_input_chars,
        min_next_cycle_budget_sec=saved.min_next_cycle_budget_sec,
        max_wall_time_sec=240,
    )
    rebuilt = build_solver_context(
        state,
        limits,
        remaining_wall_time_sec=saved.remaining_wall_time_sec,
        finalize_only=saved.finalize_only,
        required_dependency_kind=saved.required_dependency_kind,
        required_dependency_work_item_ids=(
            saved.required_dependency_work_item_ids
        ),
        available_tools=saved.available_tools,
    )

    assert len(rebuilt.completed_legal_searches) == 8
    completed_scopes = {
        (
            item.work_item_id,
            item.hypothesis_ids,
            json.dumps(item.arguments, ensure_ascii=False, sort_keys=True),
        )
        for item in rebuilt.completed_legal_searches
    }
    observed_scopes = {
        (
            item.work_item_id,
            item.hypothesis_ids,
            json.dumps(item.arguments, ensure_ascii=False, sort_keys=True),
        )
        for item in decision.tool_requests
    }
    assert observed_scopes <= completed_scopes

    with pytest.raises(
        ContractViolation,
        match="successful legal_search scope was already completed:",
    ) as error:
        apply_solver_decision(
            state,
            decision,
            limits=limits,
            known_tool_names={item.name for item in rebuilt.available_tools},
            material_evidence_ids=rebuilt.material_evidence_ids,
            fetchable_article_ids=rebuilt.fetchable_article_ids,
            remaining_fetch_capacity=rebuilt.remaining_fetch_capacity,
            cycle_close_required=rebuilt.cycle_close_required,
            can_start_next_cycle=rebuilt.can_start_next_cycle,
            finalize_only=rebuilt.finalize_only,
        )
    violation = str(error.value)
    assert violation.count('"work_item_id"') == 4
    assert "公開買付けが不要となる条件" in violation


def test_solver_context_keeps_used_tool_ids_across_cycle_boundaries() -> None:
    prior_request = ToolRequest(
        request_id="cycle-1-search",
        work_item_id="w1",
        tool_name="legal_search",
        arguments={"query": "確認語", "doc_types": ["law"]},
        purpose="Cycle 1で検索する",
        hypothesis_ids=("h1",),
    )
    state = CaseState(
        case_id="case-cycle-2",
        question="質問",
        research_cycle_count=2,
        work_items=(WorkItem(work_item_id="w1", question="確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="確認命題",
            ),
        ),
        tool_requests=(prior_request,),
    )

    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )

    assert context.recent_tool_requests == ()
    assert context.used_tool_request_ids == ("cycle-1-search",)


def _cross_cycle_search_state() -> CaseState:
    search_request = ToolRequest(
        request_id="cycle-1-search",
        work_item_id="w1",
        tool_name="legal_search",
        arguments={"query": "確認語", "doc_types": ["law"]},
        purpose="Cycle 1で候補を探す",
        hypothesis_ids=("h1",),
    )
    navigation = Evidence(
        evidence_id="cycle-1-nav",
        source_ref="opensearch://cycle-1-nav",
        title="確認法",
        content="第2条　確認対象となる手続を定める。",
        created_cycle=1,
        metadata={
            "articleId": "law-test-article-2",
            "documentId": "law-test",
            "heading": "第2条",
            "citationEligible": False,
        },
    )
    return CaseState(
        case_id="case-cycle-search-carry",
        question="質問",
        research_cycle_count=2,
        work_items=(WorkItem(work_item_id="w1", question="確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="確認命題",
            ),
        ),
        tool_requests=(search_request,),
        evidence=(navigation,),
        tool_results=(
            ToolResult(
                request_id=search_request.request_id,
                status="succeeded",
                evidence_ids=(navigation.evidence_id,),
                cycle_no=1,
            ),
        ),
        search_candidate_reviews=(
            SearchCandidateReview(
                search_request_ids=(search_request.request_id,),
                selections=(),
                deferred_article_ids=("law-test-article-2",),
                reason="別の候補を先に確認する",
                reviewed_cycle=1,
            ),
        ),
    )


def test_solver_context_carries_search_candidates_into_cycle_replanning() -> None:
    state = _cross_cycle_search_state()
    navigation = state.evidence[0]

    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )

    assert context.recent_tool_requests == ()
    assert context.recent_tool_results == ()
    assert context.required_search_review_request_ids == ()
    assert context.fetchable_article_ids == ("law-test-article-2",)
    assert tuple(item.article_id for item in context.search_candidates) == (
        "law-test-article-2",
    )
    assert context.navigation_evidence_ids == (navigation.evidence_id,)
    assert tuple(item.evidence_id for item in context.material_evidence) == (
        navigation.evidence_id,
    )


def test_solver_context_stops_carrying_search_candidate_after_fetch() -> None:
    state = _cross_cycle_search_state()
    navigation_evidence_id = state.evidence[0].evidence_id
    fetch_request = ToolRequest(
        request_id="cycle-2-fetch",
        work_item_id="w1",
        tool_name="fetch_articles",
        arguments={"article_ids": ["law-test-article-2"]},
        purpose="保留候補の本文を取得する",
        hypothesis_ids=("h1",),
    )
    body = Evidence(
        evidence_id="cycle-2-body",
        source_ref="opensearch://cycle-2-body",
        content="第2条の本文",
        created_cycle=2,
        metadata={
            "articleId": "law-test-article-2",
            "citationEligible": True,
        },
    )
    state = state.model_copy(
        update={
            "case_id": "case-cycle-search-fetched",
            "tool_requests": (*state.tool_requests, fetch_request),
            "evidence": (*state.evidence, body),
            "tool_results": (
                *state.tool_results,
                ToolResult(
                    request_id=fetch_request.request_id,
                    status="succeeded",
                    evidence_ids=(body.evidence_id,),
                    cycle_no=2,
                ),
            ),
        }
    )

    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )

    assert context.search_candidates == ()
    assert "law-test-article-2" not in context.fetchable_article_ids
    assert navigation_evidence_id not in context.navigation_evidence_ids


def test_fresh_search_review_does_not_mix_prior_deferred_candidates() -> None:
    state = _cross_cycle_search_state()
    fresh_request = ToolRequest(
        request_id="cycle-2-search",
        work_item_id="w1",
        tool_name="legal_search",
        purpose="別の表現で候補を探す",
        hypothesis_ids=("h1",),
    )
    fresh_navigation = Evidence(
        evidence_id="fresh-nav",
        source_ref="opensearch://fresh-nav",
        content="今回の候補",
        created_cycle=2,
        metadata={"articleId": "article-fresh", "citationEligible": False},
    )
    later_fetch_request = ToolRequest(
        request_id="cycle-2-later-fetch",
        work_item_id="w1",
        tool_name="fetch_articles",
        arguments={"article_ids": ["article-unrelated"]},
        purpose="検索後に別の本文を取得する",
        hypothesis_ids=("h1",),
    )
    state = state.model_copy(
        update={
            "case_id": "case-cycle-fresh-search",
            "tool_requests": (
                *state.tool_requests,
                fresh_request,
                later_fetch_request,
            ),
            "evidence": (*state.evidence, fresh_navigation),
            "tool_results": (
                *state.tool_results,
                ToolResult(
                    request_id=fresh_request.request_id,
                    status="succeeded",
                    evidence_ids=(fresh_navigation.evidence_id,),
                    cycle_no=2,
                ),
                ToolResult(
                    request_id=later_fetch_request.request_id,
                    status="failed",
                    error_code="not_found",
                    cycle_no=2,
                ),
            ),
        }
    )

    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )

    assert context.required_search_review_request_ids == (
        fresh_request.request_id,
    )
    assert tuple(item.article_id for item in context.search_candidates) == (
        "article-fresh",
    )


def test_program_assigns_persistent_tool_ids_and_preserves_local_action_link() -> None:
    context = build_solver_context(
        CaseState(
            case_id="case-tool-id",
            question="質問",
            tool_requests=(
                ToolRequest(
                    request_id="request-1",
                    work_item_id="w1",
                    tool_name="legal_search",
                    arguments={"query": "以前の検索", "doc_types": ["law"]},
                    purpose="以前の検索",
                    hypothesis_ids=("h1",),
                ),
            ),
        ),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    normalized = {
        "tool_requests": [
            {
                "request_id": "request-1",
                "work_item_id": "w1",
                "tool_name": "legal_search",
                "arguments": {"query": "次の検索", "doc_types": ["law"]},
                "purpose": "次の検索",
                "hypothesis_ids": ["h1"],
            }
        ],
        "dependency_decisions": [
            {
                "dependency_kind": "lower_norm",
                "work_item_id": "w1",
                "status": "needs_action",
                "reason": "下位規範を確認する",
                "basis_evidence_ids": ["e1"],
                "action_request_id": "request-1",
            }
        ],
    }

    _assign_tool_request_ids(normalized, context)

    assigned_id = normalized["tool_requests"][0]["request_id"]
    assert assigned_id.startswith("solver-tool-")
    assert assigned_id != "request-1"
    assert normalized["dependency_decisions"][0]["action_request_id"] == assigned_id


def test_program_relinks_stale_dependency_action_when_one_new_request_exists() -> None:
    context = build_solver_context(
        CaseState(
            case_id="case-stale-dependency-action",
            question="質問",
            tool_requests=(
                ToolRequest(
                    request_id="old-fetch",
                    work_item_id="w1",
                    tool_name="fetch_articles",
                    arguments={"article_ids": ["article-1"]},
                    purpose="以前の本文取得",
                    hypothesis_ids=("h1",),
                ),
                ToolRequest(
                    request_id="old-graph",
                    work_item_id="w1",
                    tool_name="legal_graph_neighbors",
                    arguments={"article_ids": ["article-1"]},
                    purpose="以前のGraph探索",
                    hypothesis_ids=("h1",),
                ),
            ),
        ),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    normalized = {
        "tool_requests": [
            {
                "request_id": "old-graph",
                "work_item_id": "w1",
                "tool_name": "legal_graph_neighbors",
                "arguments": {
                    "article_ids": ["article-2"],
                    "mode": "semantic_assertion",
                    "predicate": "IMPLEMENTS",
                    "direction": "from_subject",
                },
                "purpose": "次の1ホップを確認する",
                "hypothesis_ids": ["h1"],
            }
        ],
        "dependency_decisions": [
            {
                "dependency_kind": "lower_norm",
                "work_item_id": "w1",
                "status": "needs_action",
                "reason": "末端本文が未確認",
                "basis_evidence_ids": ["e1"],
                "action_request_id": "old-fetch",
            }
        ],
    }

    _assign_tool_request_ids(normalized, context)

    assigned_id = normalized["tool_requests"][0]["request_id"]
    assert assigned_id.startswith("solver-tool-")
    assert normalized["dependency_decisions"][0]["action_request_id"] == assigned_id


def test_duplicate_local_tool_ids_are_reassigned_from_fixture() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_cycle2_duplicate_local_tool_ids_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    normalized = _normalize_solver_payload(
        fixture["observedTransportOutput"]["payload"]
    )

    _assign_tool_request_ids(normalized, context)

    request_ids = [item["request_id"] for item in normalized["tool_requests"]]
    assert len(request_ids) == len(set(request_ids)) == 3
    requests_by_work_item = {
        item["work_item_id"]: item["request_id"]
        for item in normalized["tool_requests"]
    }
    for dependency in normalized["dependency_decisions"]:
        assert dependency["action_request_id"] == requests_by_work_item[
            dependency["work_item_id"]
        ]
    SolverDecision.model_validate(normalized)


def test_cycle_close_fixture_preserves_the_unresolved_boundary_state() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_cycle1_close_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState.model_validate(fixture["caseState"])
    context = SolverContext.model_validate(fixture["solverContext"])

    assert fixture["source"]["purpose"] == "cycle_close"
    assert context.cycle_close_required is True
    assert context.can_start_next_cycle is True
    assert context.remaining_fetch_capacity == 0
    assert len(context.fetched_resource_ids_this_cycle) == 3
    assert all(item.state == "open" for item in context.work_tree)
    assert all(item.judgment == "unresolved" for item in context.hypotheses)
    assert set(context.required_dependency_work_item_ids) == {
        item.work_item_id for item in context.work_tree
    }
    assert state.final_answer is None


def test_lr_003_cycle_close_fixture_requires_every_deferred_frontier_resolution() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "lr_003_cycle_close_deferred_frontiers_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    expected_ids = set(
        fixture["expectedBehavior"]["activeDeferredFrontierIds"]
    )
    active_ids = {
        item.frontier_item_id
        for item in context.graph_review_ledger
        if item.review_status == "relevant_deferred"
        and item.content_status in {"not_requested", "failed", "timeout"}
    }
    assert active_ids == expected_ids

    profile = legal_profiles.legal_agent_profile().solver_cycle_close
    assert profile is not None
    call = render_cycle_close_model_call(
        context,
        ObservationIntegrationDecision(decision_reason="取得本文を評価した"),
        profile,
    )
    projected_ids = {
        item["frontier_item_id"]
        for item in call.input_payload["active_deferred_frontiers"]
    }
    assert projected_ids == expected_ids
    assert call.output_schema["properties"][
        "deferred_frontier_resolutions"
    ]["maxItems"] == len(expected_ids)
    assert "各候補について、次の扱いを1件ずつ" in call.request
    assert "全IDを`deferred_frontier_resolutions`で1回ずつ" in call.request


def test_tob_announcement_repair_schema_limits_the_aggregate_fetch_request() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_announcement_integration_one_slot_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    profile = legal_profiles.legal_agent_profile().solver_integration
    base_call = render_solver_model_call(
        context,
        profile,
        provider="openai",
        stage="integration",
    )

    repaired = render_solver_transport_repair_model_call(
        context,
        base_call=base_call,
        payload={"tool_requests": [{}, {}]},
        error=ModelProtocolError(
            "all fetch_articles requests combined must contain at most 1 unique "
            "Article IDs; the LLM must choose the current verification set"
        ),
    )

    assert context.remaining_fetch_capacity == 1
    assert repaired.output_schema["properties"]["tool_requests"]["maxItems"] == 1
    assert "現在の残り件数以内" in repaired.instructions

    normalized = {
        "next": "continue",
        "dependency_decisions": [
            {
                "work_item_id": "wi-2",
                "action_request_id": "fetch-2",
            }
        ],
        "tool_requests": [
            {
                "request_id": "fetch-1",
                "work_item_id": "wi-1",
                "tool_name": "fetch_articles",
                "arguments": {
                    "article_ids": ["law-402M50000040038-article-10"]
                },
                "hypothesis_ids": ["h-1"],
            },
            {
                "request_id": "fetch-2",
                "work_item_id": "wi-2",
                "tool_name": "fetch_articles",
                "arguments": {
                    "article_ids": [
                        "law-323AC0000000025-article-27_3",
                        "law-323AC0000000025-article-27_4",
                    ]
                },
                "hypothesis_ids": ["h-2"],
            },
        ],
    }

    _normalize_absent_context_branches(normalized, context)

    assert len(normalized["tool_requests"]) == 1
    assert normalized["tool_requests"][0]["arguments"]["article_ids"] == [
        "law-402M50000040038-article-10"
    ]
    assert normalized["tool_requests"][0]["hypothesis_ids"] == ["h-1", "h-2"]
    assert normalized["dependency_decisions"][0]["action_request_id"] == "fetch-1"


def test_tob_announcement_cycle_close_must_continue_for_projected_open_work(
) -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_announcement_cycle_close_unresolved_v2.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    observation = ObservationIntegrationDecision.model_validate(
        {
            "decision_reason": "公告の掲載事項は確認できたが、根拠法の本文は未確認",
            "update_work_items": [
                {
                    "work_item_id": "wi-1",
                    "state": "resolved",
                    "resolution": "府令10条で公告の掲載事項を確認した",
                    "basis_hypothesis_ids": ["h-1"],
                },
                {
                    "work_item_id": "wi-2",
                    "state": "open",
                    "resolution": None,
                    "basis_hypothesis_ids": [],
                },
            ],
            "update_hypotheses": [
                {
                    "hypothesis_id": "h-1",
                    "judgment": "supported",
                    "evidence_ids": [
                        "law-402M50000040038-article-10"
                    ],
                    "gaps": [],
                },
                {
                    "hypothesis_id": "h-2",
                    "judgment": "unresolved",
                    "evidence_ids": [],
                    "gaps": ["公告義務の根拠法本文"],
                },
            ],
        }
    )
    profile = legal_profiles.legal_agent_profile().solver_cycle_close
    assert profile is not None

    rendered = render_cycle_close_model_call(context, observation, profile)

    assert "outcome" not in rendered.output_schema["properties"]
    assert rendered.input_payload["required_transition"] == "start_next_cycle"
    assert rendered.output_schema["properties"]["answer"]["type"] == "null"
    projected_work_items = {
        item["work_item_id"]: item
        for item in rendered.input_payload["work_items_after_observation"]
    }
    assert projected_work_items["wi-1"]["state"] == "resolved"
    assert projected_work_items["wi-2"]["state"] == "open"


def test_cycle_close_requires_finalize_after_observation_resolves_all() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_announcement_cycle_close_complete_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    evidence_ids = (
        "law-402M50000040038-article-10",
        "law-323AC0000000025-article-27_3-paragraph-1",
    )
    observation = ObservationIntegrationDecision.model_validate(
        {
            "decision_reason": "公告の掲載事項と根拠を本文で確認した。",
            "update_work_items": [
                {
                    "work_item_id": "wi-1",
                    "state": "resolved",
                    "resolution": "公告の掲載事項を確認した。",
                    "basis_hypothesis_ids": ["h-1"],
                }
            ],
            "update_hypotheses": [
                {
                    "hypothesis_id": "h-1",
                    "judgment": "supported",
                    "evidence_ids": list(evidence_ids),
                    "gaps": [],
                }
            ],
            "dependency_decisions": [
                {
                    "dependency_kind": "lower_norm",
                    "work_item_id": "wi-1",
                    "status": "resolved",
                    "reason": "委任元と具体化規定を確認した。",
                    "basis_evidence_ids": list(evidence_ids),
                    "action_request_id": None,
                }
            ],
        }
    )
    profile = legal_profiles.legal_agent_profile().solver_cycle_close
    assert profile is not None

    rendered = render_cycle_close_model_call(context, observation, profile)

    assert "outcome" not in rendered.output_schema["properties"]
    assert rendered.input_payload["required_transition"] == "finalize"
    assert rendered.output_schema["properties"]["answer"]["type"] == "object"


def test_cycle_close_ignores_unresolved_hypothesis_of_resolved_work_item() -> None:
    state = CaseState(
        case_id="cycle-close-resolved-parent",
        question="確認する。",
        research_cycle_count=1,
        work_items=(
            WorkItem(work_item_id="wi-1", question="法的要件を確認する。"),
        ),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h-basis",
                work_item_id="wi-1",
                statement="本文で確認できた命題。",
            ),
            Hypothesis(
                hypothesis_id="h-unused",
                work_item_id="wi-1",
                statement="結論に採用しなかった命題。",
            ),
        ),
    )
    context = build_solver_context(
        state,
        AgentLimits(max_research_cycles=4),
        remaining_wall_time_sec=180,
        finalize_only=False,
    ).model_copy(update={"cycle_close_required": True})
    observation = ObservationIntegrationDecision.model_validate(
        {
            "decision_reason": "採用した命題でWorkItemへ回答できる。",
            "update_work_items": [
                {
                    "work_item_id": "wi-1",
                    "state": "resolved",
                    "resolution": "本文で法的要件を確認した。",
                    "basis_hypothesis_ids": ["h-basis"],
                }
            ],
            "update_hypotheses": [
                {
                    "hypothesis_id": "h-basis",
                    "judgment": "supported",
                    "evidence_ids": [],
                    "gaps": [],
                },
                {
                    "hypothesis_id": "h-unused",
                    "judgment": "unresolved",
                    "evidence_ids": [],
                    "gaps": ["未採用の確認事項"],
                },
            ],
        }
    )
    profile = legal_profiles.legal_agent_profile().solver_cycle_close
    assert profile is not None

    rendered = render_cycle_close_model_call(context, observation, profile)

    assert rendered.input_payload["required_transition"] == "finalize"
    assert rendered.output_schema["properties"]["answer"]["type"] == "object"
    unresolved_ids = rendered.output_schema["properties"]["answer"][
        "properties"
    ]["unresolved_hypothesis_ids"]
    assert unresolved_ids["maxItems"] == 0


def test_tob_exceptions_mixed_refetch_keeps_only_the_unfetched_article() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_exceptions_integration_refetch_mix_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    normalized = {
        "next": "continue",
        "tool_requests": [
            {
                "request_id": "fetch-exception-detail",
                "work_item_id": "wi-1",
                "tool_name": "fetch_articles",
                "arguments": {
                    "article_ids": [
                        "law-323AC0000000025-article-27_2",
                        "law-402M50000040038-article-2_5",
                    ]
                },
                "purpose": "例外の具体的条件を確認する",
                "hypothesis_ids": ["h-1", "h-2"],
            }
        ],
    }

    _normalize_absent_context_branches(normalized, context)

    assert normalized["tool_requests"][0]["arguments"]["article_ids"] == [
        "law-402M50000040038-article-2_5"
    ]


def test_tob_final_cycle_requires_all_deferred_frontiers_in_both_schemas() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_final_cycle_deferred_frontiers_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    active_count = len(
        [
            item
            for item in context.graph_review_ledger
            if item.review_status == "relevant_deferred"
            and item.content_status in {"not_requested", "failed", "timeout"}
            and item.deferred_resolution_action != "no_longer_needed"
        ]
    )
    cycle_profile = legal_profiles.legal_agent_profile().solver_cycle_close
    assert cycle_profile is not None
    cycle_call = render_cycle_close_model_call(
        context,
        ObservationIntegrationDecision(decision_reason="本文評価済み"),
        cycle_profile,
    )
    cycle_resolutions = cycle_call.output_schema["properties"][
        "deferred_frontier_resolutions"
    ]
    assert cycle_resolutions["minItems"] == active_count
    assert cycle_resolutions["maxItems"] == active_count

    final_context = context.model_copy(update={"finalize_only": True})
    final_profile = legal_profiles.legal_agent_profile().solver_finalization
    final_call = render_solver_model_call(
        final_context,
        final_profile,
        provider="openai",
        stage="finalization",
    )
    anthropic_call = render_solver_model_call(
        final_context,
        final_profile,
        provider="anthropic",
        stage="finalization",
    )
    assert final_call.output_schema == anthropic_call.output_schema
    assert set(final_call.output_schema["properties"]) == {
        "next",
        "decision_reason",
        "answer",
        "deferred_frontier_resolutions",
    }
    assert len(final_call.input_payload["graph_review_ledger"]) == active_count
    final_resolutions = final_call.output_schema["properties"][
        "deferred_frontier_resolutions"
    ]
    assert final_resolutions["type"] == "object"
    assert len(final_resolutions["required"]) == active_count
    assert set(final_resolutions["required"]) == set(
        final_resolutions["properties"]
    )
    assert "`unresolved_at_limit`" in final_call.instructions


def test_finalization_drops_next_cycle_evidence_retention() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_finalization_retains_evidence_v347.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = build_solver_context(
        CaseState(case_id=fixture["source"]["caseId"], question="質問"),
        AgentLimits(),
        remaining_wall_time_sec=0,
        finalize_only=True,
    )
    normalized = dict(fixture["observedSolverDecision"])

    _normalize_absent_context_branches(normalized, context)

    assert normalized["retain_evidence_ids"] == fixture["expected"][
        "retain_evidence_ids"
    ]
    profile = legal_profiles.legal_agent_profile().solver_finalization
    rendered = render_solver_model_call(
        context,
        profile,
        provider="anthropic",
        stage="finalization",
    )
    assert "retain_evidence_ids" not in rendered.output_schema["properties"]


def test_finalization_mechanically_keeps_resolved_work_item_basis_ids() -> None:
    state = CaseState(
        case_id="finalization-citations",
        question="確認済み事項を回答する。",
        research_cycle_count=1,
        work_items=(
            WorkItem(
                work_item_id="w1",
                question="根拠を確認する。",
                state="resolved",
                resolution="根拠本文を確認した。",
                basis_hypothesis_ids=("h1",),
            ),
        ),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="根拠がある。",
                judgment="supported",
                evidence_ids=("e1",),
            ),
        ),
        evidence=(
            Evidence(
                evidence_id="e1",
                source_ref="fixture:e1",
                content="確認済み本文",
                created_cycle=1,
            ),
        ),
    )
    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=True,
    )
    payload = {
        "answer": {
            "text": "確認済み事項を回答する。",
            "citation_ids": [],
            "limitations": [],
            "unresolved_work_item_ids": [],
            "unresolved_hypothesis_ids": [],
        }
    }

    _include_required_finalization_citations(payload, context)

    assert payload["answer"]["citation_ids"] == ["e1"]
    rendered = render_solver_model_call(
        context,
        legal_profiles.legal_agent_profile().solver_finalization,
        provider="openai",
        stage="finalization",
    )
    assert rendered.input_payload["required_answer_evidence_ids"] == ["e1"]


def test_cycle_close_keeps_verified_evidence_for_a_limited_answer() -> None:
    state = CaseState(
        case_id="limited-answer-citations",
        question="確認できた範囲を根拠付きで回答する。",
        research_cycle_count=1,
        work_items=(
            WorkItem(
                work_item_id="w1",
                question="条件を確認する。",
            ),
        ),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="条件の一部は本文で確認できる。",
                judgment="supported",
                evidence_ids=("e-confirmed",),
                gaps=("残る条件",),
            ),
        ),
        evidence=(
            Evidence(
                evidence_id="e-confirmed",
                source_ref="fixture:e-confirmed",
                content="確認済み部分を定める本文。",
                created_cycle=1,
            ),
        ),
    )
    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=30,
        finalize_only=True,
    )
    observation = ObservationIntegrationDecision(decision_reason="評価済み")
    profile = legal_profiles.legal_agent_profile().solver_cycle_close
    assert profile is not None

    rendered = render_cycle_close_model_call(context, observation, profile)

    assert rendered.input_payload["required_answer_evidence_ids"] == []
    assert [
        item["evidence_id"]
        for item in rendered.input_payload["grounding_evidence"]
    ] == ["e-confirmed"]
    assert rendered.output_schema["properties"]["answer"]["properties"][
        "citation_ids"
    ]["maxItems"] == 1

    transition = CycleCloseDecision(
        decision_reason="確認済み部分だけ回答する。",
        answer=FinalAnswer(
            text="確認済み部分。",
            citation_ids=("e-confirmed",),
            limitations=("残る条件は未確認。",),
            unresolved_work_item_ids=("w1",),
        ),
    )
    normalized = _include_required_cycle_close_citations(
        transition,
        context,
        observation,
    )
    assert normalized.answer is not None
    assert normalized.answer.citation_ids == ("e-confirmed",)


def test_gpt4o_mini_cycle_close_fixture_reproduces_unknown_retained_ids(
) -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_cycle1_three_articles_before_cycle_close_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState.model_validate(fixture["caseState"])
    context = SolverContext.model_validate(fixture["solverContext"])
    decision = SolverDecision.model_validate(fixture["observedSolverDecision"])

    assert fixture["source"]["purpose"] == "cycle_close"
    assert fixture["source"]["model"] == "gpt-4o-mini-2024-07-18"
    assert context.cycle_close_required is True
    assert context.remaining_fetch_capacity == 0
    assert len(context.fetched_resource_ids_this_cycle) == 3

    with pytest.raises(ContractViolation) as error:
        apply_solver_decision(
            state,
            decision,
            limits=AgentLimits(),
            known_tool_names={item.name for item in context.available_tools},
            material_evidence_ids=context.grounding_evidence_ids,
            finalize_only=context.finalize_only,
            fetchable_article_ids=context.fetchable_article_ids,
            required_dependency_kind=context.required_dependency_kind,
            required_dependency_work_item_ids=(
                context.required_dependency_work_item_ids
            ),
            require_dependency_decisions=bool(
                context.required_dependency_work_item_ids
            ),
            required_graph_review_request_ids=(
                context.required_graph_review_request_ids
            ),
            required_search_review_request_ids=(
                context.required_search_review_request_ids
            ),
            remaining_fetch_capacity=context.remaining_fetch_capacity,
            cycle_close_required=context.cycle_close_required,
            can_start_next_cycle=context.can_start_next_cycle,
        )

    assert str(error.value) == fixture["expectedViolation"]


def test_cycle_close_fixture_projects_three_small_single_task_calls() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_cycle1_three_articles_before_cycle_close_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    profile = legal_profiles.legal_agent_profile().solver_cycle_close
    assert profile is not None

    observation_call = render_observation_integration_model_call(
        context,
        profile,
    )
    assert set(observation_call.input_payload) == {
        "work_items",
        "hypotheses",
        "evidence_hypothesis_candidates",
            "grounding_evidence",
            "required_dependency_kind",
            "dependency_decisions",
            "remaining_fetch_capacity",
        "fetchable_article_ids",
        "omitted_evidence_ids",
        "search_candidates",
        "completed_legal_searches",
            "completed_graph_searches",
            "graph_fetch_completed_hypothesis_ids_this_cycle",
            "cycle_close_required",
        }
    assert set(observation_call.output_schema["properties"]) == {
        "decision_reason",
        "update_hypotheses",
        "dependency_decisions",
        "tool_requests",
    }
    assert observation_call.input_payload["cycle_close_required"] is True
    assert observation_call.output_schema["properties"]["tool_requests"][
        "maxItems"
    ] == 0
    update_schema = observation_call.output_schema["properties"][
        "update_hypotheses"
    ]["items"]["properties"]
    assert update_schema["hypothesis_id"]["enum"]
    assert update_schema["evidence_ids"]["items"]["enum"] == list(
        context.grounding_evidence_ids
    )

    action_context = context.model_copy(update={"cycle_close_required": False})
    allocated = _allocate_observation_fetch_capacity(
        _observation_work_item_contexts(action_context),
        total_capacity=3,
    )
    assert [item.remaining_fetch_capacity for item in allocated] == [1, 1, 1, 0, 0]
    assert all(
        any(tool.name == "fetch_articles" for tool in item.available_tools)
        == (item.remaining_fetch_capacity > 0)
        for item in allocated
    )
    rendered_action = render_observation_integration_model_call(
        allocated[0],
        profile,
    )
    request_variants = rendered_action.output_schema["properties"][
        "tool_requests"
    ]["items"]["anyOf"]
    fetch_variant = next(
        item
        for item in request_variants
        if item["properties"]["tool_name"]["enum"] == ["fetch_articles"]
    )
    assert fetch_variant["properties"]["arguments"]["properties"][
        "article_ids"
    ]["items"]["enum"] == list(allocated[0].fetchable_article_ids)
    assert "fetchable_article_ids" in observation_call.request
    assert "search_candidates" in observation_call.request
    assert "available_tools" not in observation_call.input_payload

    updated_hypothesis = context.hypotheses[0]
    grounding_id = context.grounding_evidence_ids[0]
    observation = ObservationIntegrationDecision(
        decision_reason="取得本文を既存Hypothesisへ照合した",
        update_hypotheses=(
            HypothesisUpdate(
                hypothesis_id=updated_hypothesis.hypothesis_id,
                judgment="supported",
                evidence_ids=(grounding_id,),
            ),
        ),
        dependency_decisions=tuple(
            DependencyDecision(
                dependency_kind="lower_norm",
                work_item_id=work_item_id,
                status="needs_action",
                reason="末端下位規範本文が未取得である",
                basis_evidence_ids=(grounding_id,),
            )
            for work_item_id in context.required_dependency_work_item_ids
        ),
    )
    assert observation_call.output_schema["properties"][
        "dependency_decisions"
    ]["minItems"] == len(context.required_dependency_work_item_ids)
    transition_call = render_cycle_close_model_call(
        context,
        observation,
        profile,
    )
    assert set(transition_call.output_schema["properties"]) == {
        "decision_reason",
        "next_focus_work_item_ids",
        "retain_evidence_ids",
        "answer",
    }
    assert set(transition_call.input_payload) == {
        "question",
        "required_transition",
        "non_work_item_requirements",
        "work_items_after_observation",
        "hypotheses_after_observation",
        "observation_summary",
        "dependency_decisions_after_observation",
        "max_retained_evidence",
        "retainable_evidence",
        "grounding_evidence",
        "required_answer_evidence_ids",
        "active_deferred_frontiers",
        "unreviewed_graph_candidate_count",
    }
    projected_hypothesis = next(
        item
        for item in transition_call.input_payload[
            "hypotheses_after_observation"
        ]
        if item["hypothesis_id"] == updated_hypothesis.hypothesis_id
    )
    assert projected_hypothesis["judgment"] == "supported"
    assert projected_hypothesis["evidence_ids"] == [grounding_id]
    assert "hypotheses_before_update" not in transition_call.request
    assert "observation_integration" not in transition_call.request
    assert "fetchable_article_ids" not in transition_call.request
    assert "search_candidates" not in transition_call.request
    assert "available_tools" not in transition_call.request
    retain_items = transition_call.output_schema["properties"][
        "retain_evidence_ids"
    ]["items"]
    assert retain_items == {"type": "string"}
    assert "law-340CO0000000321-article-12" not in json.dumps(
        transition_call.output_schema,
        ensure_ascii=False,
    )
    old_transport = fixture["observedTransportInput"]
    old_prompt_chars = len(old_transport["instructions"]) + len(
        json.dumps(old_transport["inputPayload"], ensure_ascii=False)
    )
    old_schema_chars = len(
        json.dumps(old_transport["transportSchema"], ensure_ascii=False)
    )
    assert len(observation_call.request) < old_prompt_chars
    assert len(transition_call.request) < old_prompt_chars
    observation_chars = len(observation_call.request) + len(
        json.dumps(observation_call.output_schema, ensure_ascii=False)
    )
    assert observation_chars < old_prompt_chars + old_schema_chars
    assert len(
        json.dumps(transition_call.output_schema, ensure_ascii=False)
    ) < old_schema_chars


def test_cycle_close_keeps_dependency_evidence_from_prior_cycles() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_cycle1_three_articles_before_cycle_close_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    profile = legal_profiles.legal_agent_profile().solver_cycle_close
    assert profile is not None
    work_item = context.work_tree[0]
    hypothesis = next(
        item
        for item in context.hypotheses
        if item.work_item_id == work_item.work_item_id
    )
    evidence_by_article: dict[str, str] = {}
    for evidence in context.material_evidence:
        article_id = evidence.metadata.get("articleId")
        if isinstance(article_id, str):
            evidence_by_article.setdefault(article_id, evidence.evidence_id)
    prior_basis = tuple(evidence_by_article.values())[:2]
    assert len(prior_basis) == 2
    prior_dependency = DependencyDecision(
        dependency_kind="lower_norm",
        work_item_id=work_item.work_item_id,
        status="resolved",
        reason="前Cycleで委任元と末端本文を確認した",
        basis_evidence_ids=prior_basis,
    )
    context = context.model_copy(
        update={"dependency_decisions": (prior_dependency,)}
    )
    observation = ObservationIntegrationDecision(
        decision_reason="現在Cycleの本文を反映した",
        update_work_items=(
            {
                "work_item_id": work_item.work_item_id,
                "state": "resolved",
                "resolution": "確認済み",
                "basis_hypothesis_ids": [hypothesis.hypothesis_id],
            },
        ),
        update_hypotheses=(
            {
                "hypothesis_id": hypothesis.hypothesis_id,
                "judgment": "supported",
                "evidence_ids": [prior_basis[0]],
                "gaps": [],
            },
        ),
    )

    rendered = render_cycle_close_model_call(context, observation, profile)

    assert rendered.input_payload["dependency_decisions_after_observation"] == [
        prior_dependency.model_dump(mode="json")
    ]
    assert set(prior_basis) <= set(
        rendered.input_payload["required_answer_evidence_ids"]
    )


def test_cycle_close_adapter_uses_the_already_integrated_state() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_cycle1_three_articles_before_cycle_close_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    grounding_id = context.grounding_evidence_ids[0]
    class CycleCloseLLM:
        provider = "openai"

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def generate_structured_json(self, **kwargs: Any) -> StructuredJSONResult:
            self.calls.append(kwargs)
            assert "update_hypotheses" not in kwargs["schema"]["properties"]
            payload = {
                "decision_reason": "未確認の下位規範を次Cycleで確認する",
                "next_focus_work_item_ids": [
                    context.work_tree[0].work_item_id
                ],
                "retain_evidence_ids": [grounding_id],
                "answer": None,
            }
            return StructuredJSONResult(
                payload=payload,
                provider="openai",
                model=kwargs["model"],
                latencyMs=1,
                inputTokens=10,
                outputTokens=10,
            )

    llm = CycleCloseLLM()
    result = StructuredJSONModelAdapter(llm).solve(
        context,
        legal_profiles.legal_agent_profile().solver_cycle_close,
    )

    assert len(llm.calls) == 1
    assert result.attempt_count == 1
    assert result.decision.next == "continue"
    assert result.decision.start_next_cycle is True
    assert result.decision.retain_evidence_ids == (grounding_id,)
    assert result.decision.tool_requests == ()
    assert result.decision.dependency_decisions == ()

    state = CaseState.model_validate(fixture["caseState"])
    applied = apply_solver_decision(
        state,
        result.decision,
        limits=AgentLimits(),
        known_tool_names={item.name for item in context.available_tools},
        material_evidence_ids=context.grounding_evidence_ids,
        finalize_only=context.finalize_only,
        fetchable_article_ids=context.fetchable_article_ids,
        required_dependency_kind=None,
        required_dependency_work_item_ids=(),
        require_dependency_decisions=False,
        remaining_fetch_capacity=context.remaining_fetch_capacity,
        cycle_close_required=context.cycle_close_required,
        can_start_next_cycle=context.can_start_next_cycle,
    )
    assert applied.focus_work_item_ids == (
        context.work_tree[0].work_item_id,
    )
    assert applied.retained_evidence_ids == (grounding_id,)


def test_cycle_close_is_one_call_when_no_work_item_requires_dependency() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_cycle1_three_articles_before_cycle_close_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"]).model_copy(
        update={
            "required_dependency_kind": None,
            "required_dependency_work_item_ids": (),
        }
    )
    class CycleCloseWithoutDependencyLLM:
        provider = "openai"

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def generate_structured_json(self, **kwargs: Any) -> StructuredJSONResult:
            self.calls.append(kwargs)
            properties = kwargs["schema"]["properties"]
            payload = (
                {
                    "decision_reason": "取得本文の評価を反映した",
                    "update_hypotheses": [],
                    "dependency_decisions": [],
                }
                if "update_hypotheses" in properties
                else {
                    "decision_reason": "未確認事項を次Cycleへ引き継ぐ",
                    "next_focus_work_item_ids": [
                        context.work_tree[0].work_item_id
                    ],
                    "retain_evidence_ids": [],
                    "answer": None,
                }
            )
            return StructuredJSONResult(
                payload=payload,
                provider="openai",
                model=kwargs["model"],
                latencyMs=1,
                inputTokens=10,
                outputTokens=10,
            )

    llm = CycleCloseWithoutDependencyLLM()
    result = StructuredJSONModelAdapter(llm).solve(
        context,
        legal_profiles.legal_agent_profile().solver_cycle_close,
    )

    assert len(llm.calls) == 1
    assert result.attempt_count == 1
    assert result.decision.start_next_cycle is True
    assert result.decision.dependency_decisions == ()


def test_cycle_close_transport_timeout_has_no_empty_checkpoint_decision() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_cycle1_three_articles_before_cycle_close_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"]).model_copy(
        update={
            "required_dependency_kind": None,
            "required_dependency_work_item_ids": (),
        }
    )

    class TimeoutLLM:
        provider = "openai"

        def generate_structured_json(self, **kwargs: Any) -> StructuredJSONResult:
            raise requests.Timeout("timeout")

    with pytest.raises(SolverCheckpointTimeout) as captured:
        StructuredJSONModelAdapter(TimeoutLLM()).solve(
            context,
            legal_profiles.legal_agent_profile().solver_cycle_close,
        )

    assert captured.value.completed_stage == "cycle_state_ready"
    assert captured.value.partial_decision is None


def test_real_model_timeout_fixture_preserves_observation_grounding() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_cycle_close_observation_update_lost_on_timeout_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    assert fixture["source"]["purpose"] == "cycle_close"
    assert fixture["source"]["transportStage"] == "observation_integration"
    candidates = fixture["observedTransportInput"]["inputPayload"][
        "evidence_hypothesis_candidates"
    ]
    assert candidates == [
        {
            "article_id": "law-340CO0000000321-article-12",
            "hypothesis_ids": ["h-1", "h-3", "h-4"],
            "reason": (
                "legal_function=exception; 公開買付けによらない買付けが可能な"
                "場合として、特定の条件を満たす場合が列挙されている。"
            ),
        }
    ]
    updates = {
        item["hypothesis_id"]: item
        for item in fixture["observedTransportOutput"]["payload"][
            "update_hypotheses"
        ]
    }
    assert updates["h-3"]["evidence_ids"] == [
        "law-340CO0000000321-article-12"
    ]
    assert updates["h-4"]["evidence_ids"] == [
        "law-340CO0000000321-article-12"
    ]


def test_real_observation_output_projects_work_tree_for_cycle_close() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_cycle1_observation_projection_failure_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    observation = ObservationIntegrationDecision.model_validate(
        fixture["observedTransportOutput"]["payload"]
    )
    profile = legal_profiles.legal_agent_profile().solver_cycle_close
    assert profile is not None

    rendered = render_cycle_close_model_call(
        context,
        observation,
        profile,
    )

    projected_work_items = rendered.input_payload[
        "work_items_after_observation"
    ]
    assert len(projected_work_items) == len(context.work_tree)
    assert all("hypothesis_ids" in item for item in projected_work_items)
    assert all("evidence_count" in item for item in projected_work_items)
    assert {item["state"] for item in projected_work_items} == {"open"}
    assert rendered.stage == "cycle_close"


def test_real_observation_fixture_preserves_evidence_hypothesis_provenance(
) -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_cycle1_observation_projection_failure_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = build_solver_context(
        CaseState.model_validate(fixture["caseState"]),
        AgentLimits(),
        remaining_wall_time_sec=120,
        finalize_only=False,
    )
    profile = legal_profiles.legal_agent_profile().solver_cycle_close
    assert profile is not None

    rendered = render_observation_integration_model_call(context, profile)

    candidates = {
        item["article_id"]: item
        for item in rendered.input_payload["evidence_hypothesis_candidates"]
    }
    assert candidates["law-323AC0000000025-article-27_22_2"][
        "hypothesis_ids"
    ] == ["h-1", "h-2", "h-4"]
    assert "一部だけ確認できた場合" in rendered.instructions
    assert "確認済みEvidenceと未確認事項" in (
        rendered.instructions
    )


def test_observation_integration_projects_only_open_work() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_observation_open_work_focus_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    expected = fixture["expectedBehavior"]
    state = CaseState(
        case_id="fixture-observation-open-focus",
        question="公開買付けの条件、範囲、例外、手続を確認する。",
        research_cycle_count=2,
        work_items=(
            WorkItem(
                work_item_id="wi-1",
                question="条件",
                state="resolved",
                resolution="確認済み",
                basis_hypothesis_ids=("h-1",),
            ),
            WorkItem(work_item_id="wi-2", question="範囲"),
            WorkItem(work_item_id="wi-3", question="例外"),
            WorkItem(work_item_id="wi-4", question="手続"),
        ),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h-1",
                work_item_id="wi-1",
                statement="条件命題",
                judgment="supported",
                evidence_ids=("e-1",),
            ),
            Hypothesis(hypothesis_id="h-2", work_item_id="wi-2", statement="範囲命題"),
            Hypothesis(hypothesis_id="h-3", work_item_id="wi-3", statement="例外命題"),
            Hypothesis(hypothesis_id="h-4", work_item_id="wi-4", statement="手続命題"),
        ),
        evidence=(
            Evidence(
                evidence_id="e-1",
                source_ref="fixture:e-1",
                content="確認済み条件本文",
                created_cycle=1,
                metadata={"articleId": "article-1", "citationEligible": True},
            ),
            Evidence(
                evidence_id="e-2",
                source_ref="fixture:e-2",
                content="未解決事項を確認する新しい本文",
                created_cycle=2,
                metadata={"articleId": "article-2", "citationEligible": True},
            ),
        ),
    )
    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=120,
        finalize_only=False,
    )
    profile = legal_profiles.legal_agent_profile().solver_cycle_close
    assert profile is not None

    rendered = render_observation_integration_model_call(context, profile)

    assert [item["work_item_id"] for item in rendered.input_payload["work_items"]] == (
        expected["workItemIds"]
    )
    assert [item["hypothesis_id"] for item in rendered.input_payload["hypotheses"]] == (
        expected["hypothesisIds"]
    )
    assert "入力にないHypothesisを更新せず" in (
        rendered.instructions
    )


def test_cycle_close_cannot_finalize_needs_action_dependency() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_cycle_close_finalizes_open_dependency_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    observed = ObservationIntegrationDecision.model_validate(
        fixture["observedObservation"]
    )
    state = CaseState(
        case_id="fixture-cycle-close-open-dependency",
        question="公開買付けの条件を確認する。",
        research_cycle_count=2,
        work_items=(
            WorkItem(
                work_item_id="wi-1",
                question="公開買付けの条件は何か。",
            ),
        ),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h-1",
                work_item_id="wi-1",
                statement="公開買付けには法定条件がある。",
            ),
        ),
    )
    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=120,
        finalize_only=False,
    )
    aligned = _derive_observation_work_item_updates(context, observed)

    assert aligned.update_work_items == ()
    context = build_solver_context(
        state,
        AgentLimits(max_research_cycles=4),
        remaining_wall_time_sec=180,
        finalize_only=False,
    ).model_copy(update={"cycle_close_required": True})
    profile = legal_profiles.legal_agent_profile().solver_cycle_close
    assert profile is not None

    rendered = render_cycle_close_model_call(context, aligned, profile)

    assert rendered.input_payload["required_transition"] == (
        fixture["expectedBehavior"]["outcome"]
    )
    assert rendered.output_schema["properties"]["answer"]["type"] == "null"
    projected = rendered.input_payload["work_items_after_observation"]
    assert projected[0]["state"] == "open"


def test_program_derives_resolved_work_item_from_hypothesis_and_dependency() -> None:
    state = CaseState(
        case_id="fixture-derived-work-item-progress",
        question="検証法の要件を確認する。",
        research_cycle_count=1,
        work_items=(WorkItem(work_item_id="wi-1", question="適用要件は何か。"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h-1",
                work_item_id="wi-1",
                statement="検証法には適用要件がある。",
            ),
        ),
    )
    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=120,
        finalize_only=False,
    )
    observation = ObservationIntegrationDecision(
        decision_reason="本文が命題を直接支持する。",
        update_hypotheses=(
            HypothesisUpdate(
                hypothesis_id="h-1",
                judgment="supported",
                evidence_ids=("e-1",),
                gaps=(),
            ),
        ),
        dependency_decisions=(
            DependencyDecision(
                dependency_kind="lower_norm",
                work_item_id="wi-1",
                status="not_required",
                reason="下位規範確認は不要である。",
                basis_evidence_ids=("e-1",),
            ),
        ),
    )

    derived = _derive_observation_work_item_updates(context, observation)

    assert len(derived.update_work_items) == 1
    update = derived.update_work_items[0]
    assert update.work_item_id == "wi-1"
    assert update.state == "resolved"
    assert update.basis_hypothesis_ids == ("h-1",)


def test_program_keeps_work_item_open_while_hypothesis_has_a_gap() -> None:
    state = CaseState(
        case_id="fixture-derived-open-work-item",
        question="検証法の要件を確認する。",
        research_cycle_count=1,
        work_items=(WorkItem(work_item_id="wi-1", question="適用要件は何か。"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h-1",
                work_item_id="wi-1",
                statement="検証法には適用要件がある。",
            ),
        ),
    )
    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=120,
        finalize_only=False,
    )
    observation = ObservationIntegrationDecision(
        decision_reason="本文では要件の一部だけを確認できた。",
        update_hypotheses=(
            HypothesisUpdate(
                hypothesis_id="h-1",
                judgment="unresolved",
                evidence_ids=("e-1",),
                gaps=("具体的な要件",),
            ),
        ),
    )

    derived = _derive_observation_work_item_updates(context, observation)

    assert derived.update_work_items == ()


def test_program_keeps_work_item_open_while_graph_candidate_is_deferred() -> None:
    state = CaseState(
        case_id="fixture-deferred-frontier-blocks-resolution",
        question="検証法の例外を確認する。",
        research_cycle_count=2,
        work_items=(WorkItem(work_item_id="wi-1", question="例外は何か。"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h-1",
                work_item_id="wi-1",
                statement="検証法には例外がある。",
            ),
        ),
    )
    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=120,
        finalize_only=False,
    ).model_copy(
        update={
            "graph_review_ledger": (
                GraphReviewLedgerItem(
                    frontier_item_id="frontier-1",
                    article_id="article-2",
                    title="検証法施行規則",
                    heading="第二条",
                    work_item_id="wi-1",
                    hypothesis_id="h-1",
                    review_status="relevant_deferred",
                    reason="例外の具体的条件を定める候補である。",
                    content_status="not_requested",
                    last_reviewed_cycle=2,
                ),
            ),
        }
    )
    observation = ObservationIntegrationDecision(
        decision_reason="取得済み本文は命題を支持する。",
        update_hypotheses=(
            HypothesisUpdate(
                hypothesis_id="h-1",
                judgment="supported",
                evidence_ids=("e-1",),
                gaps=(),
            ),
        ),
        dependency_decisions=(
            DependencyDecision(
                dependency_kind="lower_norm",
                work_item_id="wi-1",
                status="resolved",
                reason="取得済み本文の系列は確認した。",
                basis_evidence_ids=("e-1",),
            ),
        ),
    )

    derived = _derive_observation_work_item_updates(context, observation)

    assert derived.update_work_items == ()


def test_observation_step_returns_program_derived_work_item_progress() -> None:
    evidence = Evidence(
        evidence_id="e-1",
        source_ref="fixture:e-1",
        content="検証法は、条件Aの場合に適用する。",
        created_cycle=1,
        metadata={"articleId": "article-1", "citationEligible": True},
    )
    state = CaseState(
        case_id="fixture-observation-persists-progress",
        question="検証法の要件を確認する。",
        research_cycle_count=1,
        work_items=(WorkItem(work_item_id="wi-1", question="適用要件は何か。"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h-1",
                work_item_id="wi-1",
                statement="検証法は条件Aの場合に適用される。",
            ),
        ),
        evidence=(evidence,),
        retained_evidence_ids=(evidence.evidence_id,),
    )
    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=120,
        finalize_only=False,
    )

    class ObservationLLM:
        provider = "openai"

        def generate_structured_json(self, **kwargs: Any) -> StructuredJSONResult:
            return StructuredJSONResult(
                payload={
                    "decision_reason": "本文が命題を直接支持する。",
                    "update_hypotheses": [
                        {
                            "hypothesis_id": "h-1",
                            "judgment": "supported",
                            "evidence_ids": ["e-1"],
                            "gaps": [],
                        }
                    ],
                },
                provider="openai",
                model=kwargs["model"],
                latencyMs=1,
                inputTokens=10,
                outputTokens=10,
            )

    base_profile = legal_profiles.legal_agent_profile().solver_cycle_close
    assert base_profile is not None
    profile = base_profile.model_copy(
        update={"context_projection": "observation_integration"}
    )

    result = StructuredJSONModelAdapter(ObservationLLM()).solve(context, profile)

    assert result.decision.update.update_hypotheses[0].judgment == "supported"
    assert result.decision.update.update_work_items[0].work_item_id == "wi-1"
    assert result.decision.update.update_work_items[0].state == "resolved"


def test_observation_step_can_choose_the_immediate_next_tool() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_cycle1_three_articles_before_cycle_close_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    full_context = SolverContext.model_validate(fixture["solverContext"])
    context = _observation_work_item_contexts(full_context)[0]
    context = context.model_copy(update={"cycle_close_required": False})
    work_item_id = context.required_dependency_work_item_ids[0]
    hypothesis_id = context.hypotheses[0].hypothesis_id
    article_id = context.fetchable_article_ids[0]
    grounding_id = context.grounding_evidence_ids[0]

    class ObservationActionLLM:
        provider = "openai"

        def generate_structured_json(self, **kwargs: Any) -> StructuredJSONResult:
            return StructuredJSONResult(
                payload={
                    "decision_reason": "既知候補の本文を続けて確認する。",
                    "update_hypotheses": [
                        {
                            "hypothesis_id": hypothesis_id,
                            "judgment": "unresolved",
                            "evidence_ids": [grounding_id],
                            "gaps": ["下位規範の具体的内容"],
                        }
                    ],
                    "dependency_decisions": [
                        {
                            "dependency_kind": "lower_norm",
                            "work_item_id": work_item_id,
                            "status": "needs_action",
                            "reason": "候補Articleの本文が未確認である。",
                            "basis_evidence_ids": [grounding_id],
                            "action_request_id": "next-fetch",
                        }
                    ],
                    "tool_requests": [
                        {
                            "request_id": "next-fetch",
                            "work_item_id": work_item_id,
                            "tool_name": "fetch_articles",
                            "arguments": {"article_ids": [article_id]},
                            "purpose": "下位規範の具体的内容を確認する",
                            "hypothesis_ids": [hypothesis_id],
                        }
                    ],
                },
                provider="openai",
                model=kwargs["model"],
                latencyMs=1,
                inputTokens=10,
                outputTokens=10,
            )

    profile = legal_profiles.legal_agent_profile().solver_cycle_close
    assert profile is not None
    profile = profile.model_copy(
        update={"context_projection": "observation_integration"}
    )
    result = StructuredJSONModelAdapter(ObservationActionLLM()).solve(
        context,
        profile,
    )

    assert result.decision.next == "continue"
    assert result.decision.next_focus_work_item_ids == (work_item_id,)
    assert len(result.decision.tool_requests) == 1
    request = result.decision.tool_requests[0]
    assert request.request_id.startswith("solver-tool-")
    assert request.tool_name == "fetch_articles"
    assert request.arguments == {"article_ids": [article_id]}
    assert result.decision.dependency_decisions[0].action_request_id == (
        request.request_id
    )


def test_real_dependency_resolution_failure_is_preserved_as_fixture() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_cycle1_dependency_resolution_failure_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState.model_validate(fixture["caseState"])
    context = SolverContext.model_validate(fixture["solverContext"])
    decision = SolverDecision.model_validate(fixture["observedSolverDecision"])

    with pytest.raises(ContractViolation) as error:
        apply_solver_decision(
            state,
            decision,
            limits=AgentLimits(),
            known_tool_names={item.name for item in context.available_tools},
            material_evidence_ids=context.grounding_evidence_ids,
            finalize_only=context.finalize_only,
            fetchable_article_ids=context.fetchable_article_ids,
            required_dependency_kind=context.required_dependency_kind,
            required_dependency_work_item_ids=(
                context.required_dependency_work_item_ids
            ),
            require_dependency_decisions=bool(
                context.required_dependency_work_item_ids
            ),
            remaining_fetch_capacity=context.remaining_fetch_capacity,
            cycle_close_required=context.cycle_close_required,
            can_start_next_cycle=context.can_start_next_cycle,
        )

    assert str(error.value) == fixture["expectedViolation"]
    prompt = legal_profiles.legal_agent_profile().solver_cycle_close
    assert prompt is not None
    observation = ObservationIntegrationDecision(
        decision_reason=decision.decision_reason,
        update_work_items=decision.update.update_work_items,
        update_hypotheses=decision.update.update_hypotheses,
    )
    rendered = render_dependency_assessment_model_call(
        context,
        observation,
        prompt,
    )
    assert rendered.input_payload["contract_feedback"] == {
        "violation": context.contract_feedback.violation,
    }
    assert rendered.input_payload["previous_dependency_assessment"][
        "dependency_decisions"
    ][0]["status"] == "terminal_text_confirmed"
    normalized = _normalize_observation_integration_payload(
        {
            "dependency_decisions": [
                {
                    "status": "terminal_text_missing",
                }
            ]
        }
    )
    assert normalized["dependency_decisions"][0]["status"] == "needs_action"
    status_schema = rendered.output_schema["properties"][
        "dependency_decisions"
    ]["items"]["properties"]["status"]
    assert status_schema["enum"] == [
        "not_required",
        "terminal_text_missing",
        "terminal_text_confirmed",
    ]
    basis_schema = rendered.output_schema["properties"][
        "dependency_decisions"
    ]["items"]["properties"]["basis_evidence_ids"]
    assert "metadata.articleIdが異なる" in basis_schema["description"]
    assert "previous_dependency_assessment" in rendered.instructions
    assert "指摘された違反だけを直します" in rendered.instructions


def test_observation_article_alias_expands_to_grounding_evidence_ids() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_announcement_observation_article_alias_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    article_id = "law-340CO0000000321-article-9_3"
    expected_ids = [
        item.evidence_id
        for item in context.material_evidence
        if item.evidence_id in context.grounding_evidence_ids
        and item.metadata.get("articleId") == article_id
    ]

    normalized = _normalize_observation_integration_payload(
        {
            "update_hypotheses": [
                {
                    "hypothesis_id": "h-2",
                    "judgment": "supported",
                    "evidence_ids": [article_id],
                    "gaps": [],
                }
            ],
            "dependency_decisions": [
                {
                    "status": "terminal_text_confirmed",
                    "basis_evidence_ids": [article_id],
                }
            ],
        },
        context=context,
    )

    assert len(expected_ids) > 1
    assert normalized["update_hypotheses"][0]["evidence_ids"] == expected_ids
    assert normalized["dependency_decisions"][0][
        "basis_evidence_ids"
    ] == expected_ids
    assert normalized["dependency_decisions"][0]["status"] == "resolved"


def test_integration_article_alias_expands_all_grounding_references() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_exceptions_integration_article_alias_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    article_ids = [
        "law-323AC0000000025-article-27_2",
        "law-402M50000040038-article-2_5",
    ]
    expected_ids = [
        item.evidence_id
        for article_id in article_ids
        for item in context.material_evidence
        if item.evidence_id in context.grounding_evidence_ids
        and item.metadata.get("articleId") == article_id
    ]
    normalized = {
        "next": "continue",
        "retain_evidence_ids": article_ids.copy(),
        "dependency_decisions": [
            {
                "status": "needs_action",
                "basis_evidence_ids": article_ids.copy(),
            }
        ],
        "update": {
            "update_hypotheses": [
                {"evidence_ids": article_ids.copy()}
            ]
        },
        "answer": {"citation_ids": article_ids.copy()},
        "tool_requests": [],
    }

    _normalize_absent_context_branches(normalized, context)

    assert len(expected_ids) > len(article_ids)
    assert normalized["retain_evidence_ids"] == expected_ids
    assert normalized["dependency_decisions"][0][
        "basis_evidence_ids"
    ] == expected_ids
    assert normalized["update"]["update_hypotheses"][0][
        "evidence_ids"
    ] == expected_ids
    assert normalized["answer"]["citation_ids"] == expected_ids


def test_false_confirmed_dependency_fixture_supplies_actionable_repair_input(
) -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_dependency_false_confirmed_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState.model_validate(fixture["caseState"])
    context = SolverContext.model_validate(fixture["solverContext"])
    decision = SolverDecision.model_validate(fixture["observedSolverDecision"])

    with pytest.raises(ContractViolation) as error:
        apply_solver_decision(
            state,
            decision,
            limits=AgentLimits(),
            known_tool_names={item.name for item in context.available_tools},
            material_evidence_ids=context.grounding_evidence_ids,
            finalize_only=context.finalize_only,
            fetchable_article_ids=context.fetchable_article_ids,
            required_dependency_kind=context.required_dependency_kind,
            required_dependency_work_item_ids=(
                context.required_dependency_work_item_ids
            ),
            require_dependency_decisions=bool(
                context.required_dependency_work_item_ids
            ),
            remaining_fetch_capacity=context.remaining_fetch_capacity,
            cycle_close_required=context.cycle_close_required,
            can_start_next_cycle=context.can_start_next_cycle,
        )
    assert str(error.value) == fixture["expectedViolation"]

    repair_context = context.model_copy(
        update={
            "contract_feedback": SolverContractFeedback(
                violation=fixture["expectedViolation"],
                previous_decision=decision,
            )
        }
    )
    observation = ObservationIntegrationDecision(
        decision_reason=decision.decision_reason,
        update_work_items=decision.update.update_work_items,
        update_hypotheses=decision.update.update_hypotheses,
    )
    profile = legal_profiles.legal_agent_profile().solver_cycle_close
    assert profile is not None
    rendered = render_dependency_assessment_model_call(
        repair_context,
        observation,
        profile,
    )

    previous = rendered.input_payload["previous_dependency_assessment"]
    assert len(previous["dependency_decisions"]) == 4
    assert {
        item["status"] for item in previous["dependency_decisions"]
    } == {"terminal_text_confirmed"}
    corrected = DependencyAssessmentDecision.model_validate(
        _normalize_observation_integration_payload(
            {
                "decision_reason": "末端下位規範の本文が不足している。",
                "dependency_decisions": [
                    {
                        **item,
                        "status": "terminal_text_missing",
                        "reason": "委任元と末端下位規範の対応を確認できない。",
                    }
                    for item in previous["dependency_decisions"]
                ],
            }
        )
    )
    assert {item.status for item in corrected.dependency_decisions} == {
        "needs_action"
    }


def test_same_article_dependency_confirmation_falls_back_to_unresolved() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_dependency_same_article_fallback_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    observed = DependencyAssessmentDecision.model_validate(
        fixture["observedDependencyAssessment"]
    )

    corrected = _downgrade_unproven_dependency_confirmations(
        observed,
        article_id_by_evidence=fixture["articleIdByEvidence"],
    )

    decision = corrected.dependency_decisions[0]
    expected = fixture["expectedDependencyAssessment"]
    assert decision.status == expected["status"]
    assert list(decision.basis_evidence_ids) == expected["basisEvidenceIds"]
    assert decision.action_request_id == expected["actionRequestId"]
    assert "異なるArticle本文" in decision.reason


def test_distinct_article_dependency_confirmation_is_preserved() -> None:
    assessment = DependencyAssessmentDecision.model_validate(
        {
            "decision_reason": "委任元と末端本文を確認した。",
            "dependency_decisions": [
                {
                    "dependency_kind": "lower_norm",
                    "work_item_id": "wi-1",
                    "status": "resolved",
                    "reason": "上位規定が委任し、下位規定が具体化している。",
                    "basis_evidence_ids": ["source", "terminal"],
                    "action_request_id": None,
                }
            ],
        }
    )

    corrected = _downgrade_unproven_dependency_confirmations(
        assessment,
        article_id_by_evidence={"source": "article-a", "terminal": "article-b"},
    )

    assert corrected == assessment


def test_program_does_not_infer_further_delegation_from_body_words() -> None:
    assessment = DependencyAssessmentDecision.model_validate(
        {
            "decision_reason": "委任元と下位本文を確認した。",
            "dependency_decisions": [
                {
                    "dependency_kind": "lower_norm",
                    "work_item_id": "wi-1",
                    "status": "resolved",
                    "reason": "下位規範が条件を具体化している。",
                    "basis_evidence_ids": ["source", "intermediate"],
                    "action_request_id": None,
                }
            ],
        }
    )

    corrected = _downgrade_unproven_dependency_confirmations(
        assessment,
        article_id_by_evidence={
            "source": "article-a",
            "intermediate": "article-b",
        },
    )

    assert corrected == assessment


def test_missing_dependency_fixture_allows_no_unrelated_basis_evidence() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_dependency_missing_without_evidence_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState.model_validate(fixture["caseState"])
    context = SolverContext.model_validate(fixture["solverContext"])
    observed = SolverDecision.model_validate(fixture["observedSolverDecision"])
    corrected = observed.model_copy(
        update={
            "update": observed.update.model_copy(
                update={
                    "update_work_items": tuple(
                        item.model_copy(
                            update={
                                "state": "open",
                                "resolution": None,
                                "basis_hypothesis_ids": (),
                            }
                        )
                        for item in observed.update.update_work_items
                    ),
                    "update_hypotheses": tuple(
                        item.model_copy(
                            update={
                                "gaps": item.gaps
                                or ("未確認の下位規範の具体的内容",)
                            }
                        )
                        for item in observed.update.update_hypotheses
                    ),
                }
            ),
            "dependency_decisions": tuple(
                item.model_copy(
                    update={
                        "status": "needs_action",
                        "reason": (
                            "委任元と末端下位規範の対応を本文から確認できない。"
                        ),
                        "basis_evidence_ids": (),
                    }
                )
                for item in observed.dependency_decisions
            )
        }
    )

    applied = apply_solver_decision(
        state,
        corrected,
        limits=AgentLimits(),
        known_tool_names={item.name for item in context.available_tools},
        material_evidence_ids=context.grounding_evidence_ids,
        finalize_only=context.finalize_only,
        fetchable_article_ids=context.fetchable_article_ids,
        required_dependency_kind=context.required_dependency_kind,
        required_dependency_work_item_ids=(
            context.required_dependency_work_item_ids
        ),
        require_dependency_decisions=bool(
            context.required_dependency_work_item_ids
        ),
        remaining_fetch_capacity=context.remaining_fetch_capacity,
        cycle_close_required=context.cycle_close_required,
        can_start_next_cycle=context.can_start_next_cycle,
    )
    assert {item.status for item in applied.dependency_decisions} == {
        "needs_action"
    }

    profile = legal_profiles.legal_agent_profile().solver_cycle_close
    assert profile is not None
    assert "無関係な本文を根拠にしません" in (
        profile.dependency_system_prompt or ""
    )
    assert "各規範が同じ法的論点" in (
        profile.dependency_system_prompt or ""
    )


def test_resolved_work_with_open_dependency_fixture_is_rejected() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_resolved_work_with_open_dependency_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState.model_validate(fixture["caseState"])
    context = SolverContext.model_validate(fixture["solverContext"])
    decision = SolverDecision.model_validate(fixture["observedSolverDecision"])

    with pytest.raises(
        ContractViolation,
        match="needs_action dependency requires open WorkItem IDs",
    ) as error:
        apply_solver_decision(
            state,
            decision,
            limits=AgentLimits(),
            known_tool_names={item.name for item in context.available_tools},
            material_evidence_ids=context.grounding_evidence_ids,
            finalize_only=context.finalize_only,
            fetchable_article_ids=context.fetchable_article_ids,
            required_dependency_kind=context.required_dependency_kind,
            required_dependency_work_item_ids=(
                context.required_dependency_work_item_ids
            ),
            require_dependency_decisions=bool(
                context.required_dependency_work_item_ids
            ),
            remaining_fetch_capacity=context.remaining_fetch_capacity,
            cycle_close_required=context.cycle_close_required,
            can_start_next_cycle=context.can_start_next_cycle,
        )
    assert all(
        work_item_id in str(error.value)
        for work_item_id in context.required_dependency_work_item_ids
    )
    profile = legal_profiles.legal_agent_profile().solver_cycle_close
    assert profile is not None
    assert "再試行では`contract_feedback`が示す違反だけを修正" in (
        profile.system_prompt
    )


def test_real_model_cycle_close_fixture_reproduces_duplicate_retained_evidence(
) -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_cycle2_close_duplicate_retained_evidence_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState.model_validate(fixture["caseState"])
    context = SolverContext.model_validate(fixture["solverContext"])
    decision = SolverDecision.model_validate(fixture["observedSolverDecision"])

    with pytest.raises(ContractViolation) as error:
        apply_solver_decision(
            state,
            decision,
            limits=AgentLimits(),
            known_tool_names={item.name for item in context.available_tools},
            material_evidence_ids=context.grounding_evidence_ids,
            finalize_only=context.finalize_only,
            fetchable_article_ids=context.fetchable_article_ids,
            required_dependency_kind=context.required_dependency_kind,
            required_dependency_work_item_ids=(
                context.required_dependency_work_item_ids
            ),
            require_dependency_decisions=bool(
                context.required_dependency_work_item_ids
            ),
            required_graph_review_request_ids=(
                context.required_graph_review_request_ids
            ),
            required_search_review_request_ids=(
                context.required_search_review_request_ids
            ),
            remaining_fetch_capacity=context.remaining_fetch_capacity,
            cycle_close_required=context.cycle_close_required,
            can_start_next_cycle=context.can_start_next_cycle,
        )

    assert str(error.value) == fixture["expectedViolation"]


def test_real_model_cycle_close_fixture_reproduces_unresolved_basis_failure(
) -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_cycle_close_unresolved_basis_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState.model_validate(fixture["caseState"])
    decision = SolverDecision.model_validate(fixture["solverDecision"])
    evidence_ids = tuple(item.evidence_id for item in state.evidence)

    with pytest.raises(ContractViolation) as error:
        apply_solver_decision(
            state,
            decision,
            limits=AgentLimits(),
            known_tool_names=set(),
            material_evidence_ids=evidence_ids,
            required_dependency_kind="lower_norm",
            required_dependency_work_item_ids=("work_item_1",),
            require_dependency_decisions=True,
            cycle_close_required=True,
            can_start_next_cycle=True,
            finalize_only=False,
        )

    assert str(error.value) == fixture["observation"]["expectedViolation"]

    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=120,
        finalize_only=False,
        required_dependency_kind="lower_norm",
        required_dependency_work_item_ids=("work_item_1",),
        contract_feedback=SolverContractFeedback(
            violation=str(error.value),
            previous_decision=decision,
        ),
    ).model_copy(
        update={
            "remaining_fetch_capacity": 0,
            "cycle_budget_reached": True,
            "cycle_close_required": True,
        }
    )
    cycle_close_prompt = legal_profiles.legal_agent_profile().solver_cycle_close

    assert cycle_close_prompt is not None
    assert cycle_close_prompt.completion_check_prompt is not None
    assert "WorkItemの完了状態は出力しません" in cycle_close_prompt.system_prompt
    rendered = render_observation_integration_model_call(
        context,
        cycle_close_prompt,
    )
    hypothesis_updates = rendered.output_schema["properties"][
        "update_hypotheses"
    ]
    assert rendered.request.rindex("## 出力前の確認") > rendered.request.rindex(
        "</observation_input>"
    )
    assert "`cycle_close_required=true`では`tool_requests=[]`" in (
        cycle_close_prompt.completion_check_prompt
    )
    assert "minItems" not in hypothesis_updates

    corrected_payload = json.loads(json.dumps(fixture["solverDecision"]))
    corrected_payload["update"]["update_hypotheses"] = [
        fixture["minimalCorrection"]
    ]
    corrected = apply_solver_decision(
        state,
        SolverDecision.model_validate(corrected_payload),
        limits=AgentLimits(),
        known_tool_names=set(),
        material_evidence_ids=evidence_ids,
        required_dependency_kind="lower_norm",
        required_dependency_work_item_ids=("work_item_1",),
        require_dependency_decisions=True,
        cycle_close_required=True,
        can_start_next_cycle=True,
        finalize_only=False,
    )

    assert corrected.work_items[0].state == "resolved"
    assert corrected.hypotheses[0].judgment == "supported"
    assert corrected.final_answer is not None


def test_search_review_view_groups_each_excerpt_with_its_candidate() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_cycle1_after_search_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])

    view = _search_review_context_payload(context)

    assert len(view["search_candidates"]) == len(context.search_candidates)
    assert view["search_candidates"]
    ordinance = next(
        item
        for item in view["search_candidates"]
        if item["article_id"] == "law-402M50000040038-article-2_5"
    )
    assert ordinance["search_excerpts"]
    assert "公開買付けによらないで行う" in ordinance["search_excerpts"][0][
        "content"
    ]


def test_diagnostic_integration_fixture_rejects_an_identical_successful_search(
) -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_cycle1_after_search_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState.model_validate(fixture["caseState"])
    context = SolverContext.model_validate(fixture["solverContext"])
    decision = SolverDecision(
        next="continue",
        decision_reason="同じ検索を再要求する",
        next_focus_work_item_ids=("work_item_1",),
        tool_requests=(
            {
                "request_id": "legal_search_5",
                "work_item_id": "work_item_1",
                "tool_name": "legal_search",
                "arguments": {
                    "query": "公開買付け 手続 条文",
                    "doc_types": ["law"],
                    "document_ids": [],
                },
                "purpose": "同じscopeを再実行する",
                "hypothesis_ids": ["hypothesis_1"],
            },
        ),
    )

    with pytest.raises(
        ContractViolation,
        match="successful legal_search scope was already completed",
    ):
        apply_solver_decision(
            state,
            decision,
            limits=legal_profiles.legal_agent_profile().limits,
            known_tool_names={"legal_search", "fetch_articles"},
            material_evidence_ids=context.material_evidence_ids,
            fetchable_article_ids=context.fetchable_article_ids,
            graph_review_fetch_tool_name="fetch_articles",
            remaining_fetch_capacity=context.remaining_fetch_capacity,
            cycle_close_required=context.cycle_close_required,
            can_start_next_cycle=context.can_start_next_cycle,
            finalize_only=context.finalize_only,
        )


def test_successful_graph_scope_cannot_be_executed_again() -> None:
    request = ToolRequest(
        request_id="graph-1",
        work_item_id="w1",
        tool_name="legal_graph_neighbors",
        arguments={
            "article_ids": ["law-a-article-1"],
            "mode": "semantic_assertion",
            "predicate": "IMPLEMENTS",
            "direction": "from_subject",
            "max_relations": 20,
        },
        purpose="下位規範を1ホップ確認する",
        hypothesis_ids=("h1",),
    )
    state = CaseState(
        case_id="case-graph-repeat",
        question="質問",
        research_cycle_count=1,
        work_items=(WorkItem(work_item_id="w1", question="確認"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="下位規範がある",
            ),
        ),
        tool_requests=(request,),
        tool_results=(
            ToolResult(request_id="graph-1", status="succeeded", cycle_no=1),
        ),
    )

    with pytest.raises(
        ContractViolation,
        match="successful legal_graph_neighbors scope was already completed",
    ):
        apply_solver_decision(
            state,
            SolverDecision(
                next="continue",
                tool_requests=(
                    request.model_copy(update={"request_id": "graph-2"}),
                ),
            ),
            limits=AgentLimits(),
            known_tool_names={"legal_graph_neighbors"},
            material_evidence_ids=(),
            graph_known_article_ids=("law-a-article-1",),
            finalize_only=False,
        )


def test_repeated_graph_feedback_does_not_remove_the_tool() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_announcement_observation_article_alias_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    repeated = SolverDecision(
        next="continue",
        tool_requests=(
            ToolRequest(
                request_id="graph-repeat",
                work_item_id="wi-1",
                tool_name="legal_graph_neighbors",
                arguments={
                    "article_ids": ["law-402M50000040038-article-10"],
                    "mode": "semantic_assertion",
                    "predicate": "IMPLEMENTS",
                    "direction": "from_subject",
                    "max_relations": 50,
                },
                purpose="同じGraph scopeを繰り返す",
                hypothesis_ids=("h-1",),
            ),
        ),
    )
    feedback_context = context.model_copy(
        update={
            "contract_feedback": None,
            "action_feedback": SolverActionFeedback(
                code="already_completed",
                message=(
                    "successful legal_graph_neighbors scope was already completed"
                ),
                rejected_tool_requests=repeated.tool_requests,
            )
        }
    )

    schema = _tool_requests_transport_schema(feedback_context)
    variants = schema["items"].get("anyOf", [schema["items"]])
    tool_names = {
        variant["properties"]["tool_name"]["enum"][0]
        for variant in variants
    }

    assert "legal_graph_neighbors" in tool_names
    assert "legal_search" in tool_names
    assert "fetch_articles" in tool_names


def test_overview_repeated_graph_fixture_preserves_every_tool_choice() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_dependency_action_repeated_graph_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])

    schema = _tool_requests_transport_schema(context)
    variants = schema["items"].get("anyOf", [schema["items"]])
    tool_names = {
        variant["properties"]["tool_name"]["enum"][0]
        for variant in variants
    }

    assert "legal_graph_neighbors" in tool_names
    assert "legal_search" in tool_names
    assert "fetch_articles" in tool_names


def test_load_evidence_article_alias_expands_to_grounding_ids() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_exceptions_dependency_action_repeat_feedback_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    article_id = "law-402M50000040038-article-2_5"
    expected_ids = [
        item.evidence_id
        for item in context.material_evidence
        if item.evidence_id in context.grounding_evidence_ids
        and item.metadata.get("articleId") == article_id
    ]
    normalized = {
        "tool_requests": [
            {
                "tool_name": "load_evidence",
                "arguments": {"evidence_ids": [article_id]},
            }
        ]
    }

    _normalize_absent_context_branches(normalized, context)

    assert len(expected_ids) > 1
    assert normalized["tool_requests"][0]["arguments"][
        "evidence_ids"
    ] == expected_ids


def test_openai_solver_uses_structured_tool_request_transport() -> None:
    class OpenAIStructuredLLM:
        provider = "openai"

        def __init__(self) -> None:
            self.schema: dict[str, Any] | None = None

        def generate_structured_json(self, **kwargs: Any) -> StructuredJSONResult:
            self.schema = kwargs["schema"]
            return StructuredJSONResult(
                payload={
                    "next": "continue",
                    "decision_reason": "根拠Articleを検索する",
                    "start_next_cycle": False,
                    "update": {
                        "add_work_items": [
                            {
                                "work_item_id": "w1",
                                "parent_work_item_id": None,
                                "question": "公開買付けの要件を確認する",
                                "state": "open",
                                "resolution": None,
                                "basis_hypothesis_ids": ["h1"],
                                "replaces_work_item_id": None,
                            }
                        ],
                        "update_work_items": [],
                        "add_hypotheses": [
                            {
                                "hypothesis_id": "h1",
                                "work_item_id": "w1",
                                "statement": "要件を定めるArticleがある",
                                "judgment": "unresolved",
                                "evidence_ids": [],
                                "gaps": ["本文未取得"],
                            }
                        ],
                        "update_hypotheses": [],
                        "impact_decisions": [],
                    },
                    "next_focus_work_item_ids": ["w1"],
                    "retain_evidence_ids": [],
                    "tool_requests": [
                        {
                            "request_id": "r1",
                            "work_item_id": "w1",
                            "tool_name": "legal_search",
                            "arguments": {
                                "query": "公開買付け 手続 条文",
                                "doc_types": ["law"],
                            },
                            "purpose": "根拠Articleの候補を検索する",
                            "hypothesis_ids": ["h1"],
                        }
                    ],
                    "dependency_decisions": [],
                    "graph_candidate_review": None,
                    "frontier_re_adoptions": [],
                    "deferred_frontier_resolutions": [],
                    "unreviewed_graph_resolution": None,
                    "answer": None,
                },
                provider="openai",
                model=kwargs["model"],
                latencyMs=1,
                inputTokens=10,
                outputTokens=10,
            )

    context = build_solver_context(
        CaseState(case_id="case-1", question="質問"),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    llm = OpenAIStructuredLLM()

    result = StructuredJSONModelAdapter(llm).solve(
        context,
        ModelCallProfile(model="gpt-4o-mini", system_prompt="判断する"),
    )

    assert result.decision.tool_requests[0].request_id.startswith("solver-tool-")
    assert llm.schema is not None
    properties = llm.schema["properties"]
    assert "tool_requests" in properties
    assert "tool_requests_json" not in properties
    request_schema = properties["tool_requests"]["items"]
    assert set(request_schema["required"]) == {
        "request_id",
        "work_item_id",
        "tool_name",
        "arguments",
        "purpose",
        "hypothesis_ids",
    }


def test_anthropic_transport_uses_one_fixed_slot_article_fetch() -> None:
    context = build_solver_context(
        CaseState(
            case_id="case-1",
            question="質問",
            research_cycle_count=1,
            work_items=(WorkItem(work_item_id="w1", question="確認する"),),
            hypotheses=(
                Hypothesis(
                    hypothesis_id="h1",
                    work_item_id="w1",
                    statement="本文を確認する",
                ),
            ),
        ),
        AgentLimits(max_tool_requests_per_step=5),
        remaining_wall_time_sec=120,
        finalize_only=False,
    ).model_copy(
        update={
            "fetchable_article_ids": (
                "article-long-1",
                "article-long-2",
                "article-long-3",
                "article-long-4",
                "article-long-5",
            ),
            "remaining_fetch_capacity": 4,
            "grounding_evidence_ids": ("shown-evidence-1",),
        }
    )

    schema = _solver_anthropic_transport_schema(context)
    properties = schema["properties"]
    fetch_object = properties["fetch_articles"]["anyOf"][0]

    assert "update_json" in schema["required"]
    assert "update_json" in properties
    assert "hypothesis_evidence_bindings" in schema["required"]
    binding_evidence_ids = properties["hypothesis_evidence_bindings"]["items"][
        "properties"
    ]["evidence_ids"]
    assert binding_evidence_ids["items"]["enum"] == ["shown-evidence-1"]
    assert "fetch_articles" in schema["required"]
    assert set(fetch_object["properties"]) == {
        "request_id",
        "work_item_id",
        "purpose",
        "hypothesis_ids",
        "article_ref_1",
        "article_ref_2",
        "article_ref_3",
        "article_ref_4",
    }
    assert fetch_object["properties"]["article_ref_1"]["enum"] == [
        "a1",
        "a2",
        "a3",
        "a4",
        "a5",
    ]
    assert set(properties["tool_requests"]["properties"]) == {
        "tool_request_1_json",
        "tool_request_2_json",
        "tool_request_3_json",
        "tool_request_4_json",
    }
    request_slot = properties["tool_requests"]["properties"][
        "tool_request_1_json"
    ]["anyOf"][0]
    assert request_slot["type"] == "object"
    assert request_slot["properties"]["tool_name"]["enum"] == [
        "legal_search",
        "legal_graph_neighbors",
        "load_evidence",
    ]

    normalized = _normalize_solver_payload(
        {
            "next": "continue",
            "start_next_cycle": False,
            "update_json": "{}",
            "hypothesis_evidence_bindings": [],
            "next_focus_work_item_ids": ["w1"],
            "retain_evidence_ids": [],
            "tool_requests": {
                "tool_request_1_json": {
                    "tool_name": "legal_search",
                    "request_json": json.dumps(
                        {
                            "request_id": "search-1",
                            "work_item_id": "w1",
                            "arguments": {
                                "query": "追加確認",
                                "doc_types": ["law"],
                            },
                            "purpose": "別の条文を探す",
                            "hypothesis_ids": ["h1"],
                        },
                        ensure_ascii=False,
                    ),
                },
                "tool_request_2_json": None,
                "tool_request_3_json": None,
                "tool_request_4_json": None,
            },
            "article_fetch": {
                "request_id": "fetch-1",
                "work_item_id": "w1",
                "purpose": "必要本文を確認する",
                "hypothesis_ids": ["h1"],
                "article_ref_1": "a1",
                "article_ref_2": "a2",
                "article_ref_3": None,
                "article_ref_4": "a4",
            },
            "dependency_decisions": [],
            "graph_candidate_review": None,
            "frontier_re_adoptions": [],
            "deferred_frontier_resolutions": [],
            "unreviewed_graph_resolution": None,
            "answer": None,
        }
    )

    _normalize_absent_context_branches(normalized, context)

    assert normalized["tool_requests"] == [
        {
            "request_id": "search-1",
            "work_item_id": "w1",
            "tool_name": "legal_search",
            "arguments": {"query": "追加確認", "doc_types": ["law"]},
            "purpose": "別の条文を探す",
            "hypothesis_ids": ["h1"],
        },
        {
            "request_id": "fetch-1",
            "work_item_id": "w1",
            "tool_name": "fetch_articles",
            "arguments": {
                "article_ids": [
                    "article-long-1",
                    "article-long-2",
                    "article-long-4",
                ]
            },
            "purpose": "必要本文を確認する",
            "hypothesis_ids": ["h1"],
        }
    ]


def test_anthropic_transport_uses_one_dependency_slot_per_work_item() -> None:
    upper = Evidence(
        evidence_id="upper",
        source_ref="test://upper",
        content="委任元本文",
        created_cycle=1,
        metadata={"articleId": "article-upper", "citationEligible": True},
    )
    lower = Evidence(
        evidence_id="lower",
        source_ref="test://lower",
        content="具体化先本文",
        created_cycle=1,
        metadata={"articleId": "article-lower", "citationEligible": True},
    )
    context = build_solver_context(
        CaseState(
            case_id="case-1",
            question="質問",
            research_cycle_count=1,
            work_items=(
                WorkItem(work_item_id="w1", question="適用要件"),
                WorkItem(work_item_id="w2", question="手続"),
            ),
        ),
        AgentLimits(),
        remaining_wall_time_sec=120,
        finalize_only=False,
    ).model_copy(
        update={
            "required_dependency_kind": "lower_norm",
            "required_dependency_work_item_ids": ("w1", "w2"),
            "grounding_evidence_ids": ("upper", "lower"),
            "material_evidence": (upper, lower),
        }
    )

    properties = _solver_anthropic_transport_schema(context)["properties"]
    dependency_slots = properties["dependency_decisions"]

    assert dependency_slots["type"] == "object"
    assert set(dependency_slots["properties"]) == {
        "dependency_decision_1_json",
        "dependency_decision_2_json",
    }
    dependency_bindings = properties["dependency_article_bindings"]
    binding_properties = dependency_bindings["items"]["properties"]
    assert binding_properties["work_item_id"]["enum"] == ["w1", "w2"]
    assert binding_properties["article_ids"]["items"]["enum"] == [
        "article-upper",
        "article-lower",
    ]
    assert dependency_slots["properties"]["dependency_decision_1_json"][
        "type"
    ] == "string"
    assert "w1" in dependency_slots["properties"][
        "dependency_decision_1_json"
    ]["description"]
    assert "at least two distinct Article IDs" in dependency_slots[
        "properties"
    ]["dependency_decision_1_json"]["description"]
    assert "w2" in dependency_slots["properties"][
        "dependency_decision_2_json"
    ]["description"]

    normalized = _normalize_solver_payload(
        {
            "next": "continue",
            "dependency_decisions": {
                "dependency_decision_1_json": json.dumps(
                    {
                        "dependency_kind": "lower_norm",
                        "work_item_id": "w1",
                        "status": "resolved",
                        "reason": "確認済み",
                        "basis_evidence_ids": ["invented"],
                        "action_request_id": "ignored",
                    },
                    ensure_ascii=False,
                ),
                "dependency_decision_2_json": json.dumps(
                    {
                        "dependency_kind": "lower_norm",
                        "work_item_id": "w2",
                        "status": "needs_action",
                        "reason": "下位規範を確認する",
                        "basis_evidence_ids": ["also-invented"],
                        "action_request_id": "search-2",
                    },
                    ensure_ascii=False,
                ),
            },
            "dependency_article_bindings": [
                {
                    "work_item_id": "w1",
                    "article_ids": ["article-upper", "article-lower"],
                },
                {"work_item_id": "w2", "article_ids": ["article-upper"]},
            ],
            "tool_requests": [],
        }
    )
    _normalize_absent_context_branches(normalized, context)
    dependencies = normalized["dependency_decisions"]

    assert [item["work_item_id"] for item in dependencies] == ["w1", "w2"]
    assert dependencies[0]["basis_evidence_ids"] == ["upper", "lower"]
    assert dependencies[1]["basis_evidence_ids"] == ["upper"]
    assert dependencies[0]["action_request_id"] is None
    assert dependencies[1]["action_request_id"] == "search-2"


def test_anthropic_evidence_bindings_replace_predictable_ids_in_update_json() -> None:
    normalized = _normalize_solver_payload(
        {
            "next": "continue",
            "update_json": json.dumps(
                {
                    "update_hypotheses": [
                        {
                            "hypothesis_id": "h1",
                            "judgment": "supported",
                            "evidence_ids": [
                                "law-known-article-2-paragraph-predicted"
                            ],
                            "gaps": [],
                        }
                    ]
                }
            ),
            "hypothesis_evidence_bindings": [
                {
                    "hypothesis_id": "h1",
                    "evidence_ids": ["shown-evidence-1"],
                }
            ],
            "tool_requests_json": "[]",
        }
    )

    assert normalized["update"]["update_hypotheses"][0]["evidence_ids"] == [
        "shown-evidence-1"
    ]


def test_anthropic_null_evidence_sidecar_clears_update_json_ids() -> None:
    normalized = _normalize_solver_payload(
        {
            "next": "continue",
            "update_json": json.dumps(
                {
                    "update_hypotheses": [
                        {
                            "hypothesis_id": "h1",
                            "judgment": "unresolved",
                            "evidence_ids": ["predicted-navigation-id"],
                            "gaps": ["本文未確認"],
                        }
                    ]
                }
            ),
            "hypothesis_evidence_bindings": None,
            "tool_requests_json": "[]",
        }
    )

    assert normalized["update"]["update_hypotheses"][0]["evidence_ids"] == []


def test_transport_repair_explains_continue_requires_an_actual_action() -> None:
    context = build_solver_context(
        CaseState(case_id="case-1", question="質問"),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    profile = ModelCallProfile(model="test-model", system_prompt="base")
    base_call = render_solver_model_call(context, profile, provider="openai")
    rendered = render_solver_transport_repair_model_call(
        context,
        base_call=base_call,
        payload={"next": "continue"},
        error=ModelProtocolError("continue decision requires a tool request"),
    )
    prompt = rendered.request

    assert "next=continueを維持するなら" in prompt
    assert "fetch_articlesを少なくとも1件" in prompt
    assert "next=finalizeとanswer" in prompt


def test_transport_repair_explains_finalize_requires_an_answer() -> None:
    context = build_solver_context(
        CaseState(case_id="case-1", question="質問"),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    profile = ModelCallProfile(model="test-model", system_prompt="base")
    base_call = render_solver_model_call(context, profile, provider="openai")
    rendered = render_solver_transport_repair_model_call(
        context,
        base_call=base_call,
        payload={"next": "finalize", "answer": None},
        error=ModelProtocolError("finalize decision requires an answer"),
    )
    prompt = rendered.request

    assert "next=finalizeを維持するなら" in prompt
    assert "確認済みEvidenceに基づくanswer" in prompt
    assert "start_next_cycle=true" in prompt


def test_dependency_action_uses_dedicated_contract_and_preserves_prior_decision(
) -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_exceptions_cycle2_finalize_tool_conflict_v275.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])
    profile = legal_profiles.legal_agent_profile().solver_integration

    rendered = render_solver_model_call(
        context,
        profile,
        provider="openai",
        stage="integration",
    )
    work_item_id = context.required_dependency_work_item_ids[0]

    assert set(rendered.output_schema["properties"]) == {
        "decision_reason",
        "start_next_cycle",
        "tool_requests",
    }
    assert rendered.normalized_schema == DependencyActionDecision.model_json_schema()
    assert rendered.output_schema["properties"]["tool_requests"]["minItems"] == 0
    assert rendered.output_schema["properties"]["tool_requests"]["maxItems"] == 1
    assert rendered.output_schema["properties"]["start_next_cycle"]["enum"] == [
        False,
        True,
    ]
    request_item_schema = rendered.output_schema["properties"]["tool_requests"][
        "items"
    ]
    request_variants = request_item_schema.get("anyOf", [request_item_schema])
    assert all(
        variant["properties"]["work_item_id"]["enum"] == [work_item_id]
        for variant in request_variants
    )
    allowed_tools = {
        variant["properties"]["tool_name"]["enum"][0]
        for variant in request_variants
    }
    assert "legal_search" not in allowed_tools
    assert "fetch_articles" in allowed_tools
    assert "legal_search" not in {
        definition["name"]
        for definition in rendered.input_payload["available_tools"]
    }
    assert "continueまたはfinalize" not in rendered.instructions
    assert "DependencyDecisionの再判定は行いません" in rendered.instructions
    assert "まず`semantic_assertion`を使います" in rendered.instructions
    assert "`find_articles_referencing_this`" in rendered.instructions
    assert "`follow_reference_in_text`" in rendered.instructions
    assert "重複しない有効なTool要求がなく次Cycleを開始できる場合" in (
        rendered.instructions
    )

    anthropic_rendered = render_solver_model_call(
        context,
        profile,
        provider="anthropic",
        stage="integration",
    )
    assert set(anthropic_rendered.output_schema["properties"]) == {
        "decision_reason",
        "start_next_cycle",
        "tool_requests_json",
    }
    assert "`tool_requests_json`には" in anthropic_rendered.instructions
    assert "同名の`_json`なし項目は返しません" in anthropic_rendered.instructions

    hypothesis_id = next(
        item.hypothesis_id
        for item in context.hypotheses
        if item.work_item_id == work_item_id
    )
    fetchable_candidate = next(
        candidate
        for candidate in context.search_candidates
        if candidate.article_id in context.fetchable_article_ids
        and hypothesis_id in candidate.matched_hypothesis_ids
    )
    decision = normalize_dependency_action_decision(
        {
            "decision_reason": "評価済み候補の本文を確認する。",
            "start_next_cycle": False,
            "tool_requests": [
                {
                    "request_id": "fetch-next",
                    "work_item_id": work_item_id,
                    "tool_name": "fetch_articles",
                    "arguments": {
                        "article_ids": [fetchable_candidate.article_id],
                    },
                    "purpose": "未確認の適用除外候補本文を確認する。",
                    "hypothesis_ids": [hypothesis_id],
                }
            ],
        },
        context=context,
    )

    assert decision.next == "continue"
    assert decision.answer is None
    assert len(decision.tool_requests) == 1
    assert len(decision.dependency_decisions) == 1
    assert decision.dependency_decisions[0].status == "needs_action"
    assert decision.dependency_decisions[0].reason == next(
        item.reason
        for item in context.dependency_decisions
        if item.work_item_id == work_item_id
    )
    assert decision.dependency_decisions[0].action_request_id == (
        decision.tool_requests[0].request_id
    )


def test_dependency_action_can_move_to_next_cycle_without_repeating_scope(
) -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_exceptions_cycle2_finalize_tool_conflict_v275.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = SolverContext.model_validate(fixture["solverContext"])

    decision = normalize_dependency_action_decision(
        {
            "decision_reason": (
                "現在Cycleに未確認事項を進める重複しないTool要求がないため、"
                "次Cycleで探索方針を見直す。"
            ),
            "start_next_cycle": True,
            "tool_requests": [],
        },
        context=context,
    )

    assert decision.next == "continue"
    assert decision.start_next_cycle is True
    assert decision.tool_requests == ()
    assert decision.next_focus_work_item_ids == (
        *context.required_dependency_work_item_ids,
    )
    assert len(decision.dependency_decisions) == 1
    assert decision.dependency_decisions[0].status == "needs_action"
    assert decision.dependency_decisions[0].action_request_id is None


def test_dependency_action_reports_the_missing_nested_field_path() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_dependency_action_missing_purpose_v346.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState(
        case_id=fixture["source"]["caseId"],
        question="公開買付けの条件、範囲、例外、手続を確認する。",
        research_cycle_count=1,
        work_items=(
            WorkItem(work_item_id="wi-4", question="必要な手続を確認する。"),
        ),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h-6",
                work_item_id="wi-4",
                statement="公開買付開始公告を行う。",
            ),
            Hypothesis(
                hypothesis_id="h-7",
                work_item_id="wi-4",
                statement="必要事項を開示する。",
            ),
            Hypothesis(
                hypothesis_id="h-8",
                work_item_id="wi-4",
                statement="行為制限を確認する。",
            ),
        ),
        dependency_decisions=(
            DependencyDecision(
                dependency_kind="lower_norm",
                work_item_id="wi-4",
                status="needs_action",
                reason="府令本文が未確認である。",
            ),
        ),
    )
    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=120,
        finalize_only=False,
        required_dependency_kind="lower_norm",
        required_dependency_work_item_ids=("wi-4",),
    )

    with pytest.raises(
        ModelProtocolError,
        match=fixture["expected"]["missingFieldPath"].replace(".", r"\."),
    ):
        normalize_dependency_action_decision(
            fixture["observedTransportOutput"],
            context=context,
        )


def test_dependency_action_prompt_defines_every_tool_request_field() -> None:
    context = build_solver_context(
        CaseState(
            case_id="case-dependency-action-contract",
            question="質問",
            research_cycle_count=1,
            work_items=(WorkItem(work_item_id="w1", question="確認する。"),),
            hypotheses=(
                Hypothesis(
                    hypothesis_id="h1",
                    work_item_id="w1",
                    statement="下位規範を確認する。",
                ),
            ),
            dependency_decisions=(
                DependencyDecision(
                    dependency_kind="lower_norm",
                    work_item_id="w1",
                    status="needs_action",
                    reason="下位規範本文が未確認である。",
                ),
            ),
        ),
        AgentLimits(),
        remaining_wall_time_sec=120,
        finalize_only=False,
        required_dependency_kind="lower_norm",
        required_dependency_work_item_ids=("w1",),
    )
    rendered = render_solver_model_call(
        context,
        legal_profiles.legal_agent_profile().solver_integration,
        provider="anthropic",
        stage="integration",
    )

    for field_name in (
        "request_id",
        "work_item_id",
        "tool_name",
        "arguments",
        "purpose",
        "hypothesis_ids",
    ):
        assert f"`{field_name}`" in rendered.instructions
    description = rendered.output_schema["properties"]["tool_requests_json"][
        "description"
    ]
    assert "purpose" in description


def test_dependency_action_can_process_a_subset_within_remaining_capacity() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_dependency_action_exceeds_remaining_capacity_v352.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState(
        case_id=fixture["source"]["caseId"],
        question="公開買付けの条件、範囲、例外、手続を確認する。",
        research_cycle_count=1,
        work_items=(
            WorkItem(work_item_id="wi-2", question="対象範囲を確認する。"),
            WorkItem(work_item_id="wi-4", question="必要な手続を確認する。"),
        ),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h-4",
                work_item_id="wi-2",
                statement="対象となる株券等は政令で定められる。",
            ),
            Hypothesis(
                hypothesis_id="h-7",
                work_item_id="wi-4",
                statement="公開買付開始公告を行う。",
            ),
        ),
        dependency_decisions=tuple(
            DependencyDecision(
                dependency_kind="lower_norm",
                work_item_id=work_item_id,
                status="needs_action",
                reason="下位規範本文が未確認である。",
            )
            for work_item_id in fixture["requiredDependencyWorkItemIds"]
        ),
    )
    context = build_solver_context(
        state,
        AgentLimits(max_fetched_resources_per_cycle=1),
        remaining_wall_time_sec=120,
        finalize_only=False,
        required_dependency_kind="lower_norm",
        required_dependency_work_item_ids=tuple(
            fixture["requiredDependencyWorkItemIds"]
        ),
    ).model_copy(
        update={
            "remaining_fetch_capacity": fixture["remainingFetchCapacity"],
            "fetchable_article_ids": (
                "law-340CO0000000321-article-6",
            ),
        }
    )

    decision = normalize_dependency_action_decision(
        fixture["observedAction"],
        context=context,
    )

    assert len(decision.tool_requests) == fixture["expected"]["toolRequestCount"]
    dependency_by_work_item = {
        item.work_item_id: item for item in decision.dependency_decisions
    }
    selected = dependency_by_work_item[fixture["expected"]["selectedWorkItemId"]]
    deferred = dependency_by_work_item[fixture["expected"]["deferredWorkItemId"]]
    assert selected.action_request_id == decision.tool_requests[0].request_id
    assert deferred.status == "needs_action"
    assert deferred.action_request_id is None

    updated = apply_solver_decision(
        state,
        decision,
        limits=AgentLimits(max_fetched_resources_per_cycle=1),
        known_tool_names={"fetch_articles"},
        material_evidence_ids=(),
        fetchable_article_ids=context.fetchable_article_ids,
        required_dependency_kind="lower_norm",
        required_dependency_work_item_ids=context.required_dependency_work_item_ids,
        require_dependency_decisions=True,
        allow_dependency_action_without_tool=True,
        remaining_fetch_capacity=context.remaining_fetch_capacity,
        finalize_only=False,
    )
    assert len(updated.tool_requests) == 1


def test_rejected_dependency_action_forces_the_next_cycle_contract() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_duplicate_search_feedback_v317.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState(
        case_id=fixture["source"]["caseId"],
        question="公開買付けの条件、範囲、例外、手続を確認する。",
        research_cycle_count=2,
        work_items=(
            WorkItem(work_item_id="wi-1", question="成立条件を確認する。"),
        ),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h-1",
                work_item_id="wi-1",
                statement="一定の場合に公開買付けが必要となる。",
            ),
        ),
        dependency_decisions=(
            DependencyDecision(
                dependency_kind="lower_norm",
                work_item_id="wi-1",
                status="needs_action",
                reason="下位規範の具体的条件が未確認である。",
            ),
        ),
    )
    context = build_solver_context(
        state,
        AgentLimits(),
        remaining_wall_time_sec=120,
        finalize_only=False,
        action_feedback=SolverActionFeedback.model_validate(
            fixture["actionFeedback"]
        ),
        required_dependency_kind="lower_norm",
        required_dependency_work_item_ids=("wi-1",),
    )
    profile = legal_profiles.legal_agent_profile().solver_integration

    rendered = render_solver_model_call(
        context,
        profile,
        provider="openai",
        stage="integration",
    )

    assert context.can_start_next_cycle is True
    assert rendered.output_schema["properties"]["start_next_cycle"]["enum"] == [
        True
    ]
    assert rendered.output_schema["properties"]["tool_requests"]["maxItems"] == 0

    anthropic = render_solver_model_call(
        context,
        profile,
        provider="anthropic",
        stage="integration",
    )
    assert anthropic.output_schema["properties"]["start_next_cycle"]["enum"] == [
        True
    ]
    assert anthropic.output_schema["properties"]["tool_requests_json"]["enum"] == [
        "[]"
    ]

    decision = normalize_dependency_action_decision(
        {
            "decision_reason": "成功済みscopeを繰り返さず、次Cycleで見直す。",
            "start_next_cycle": fixture["expected"]["startNextCycle"],
            "tool_requests": [],
        },
        context=context,
    )
    assert decision.start_next_cycle is fixture["expected"]["startNextCycle"]
    assert len(decision.tool_requests) == fixture["expected"]["toolRequestCount"]


def test_common_prompt_does_not_expose_provider_sidecars() -> None:
    context = build_solver_context(
        CaseState(case_id="case-1", question="質問"),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )

    prompt = _solver_prompt(
        context,
        "system",
        structured_tool_transport=True,
    )

    assert "update_json" in prompt
    assert "update_json、tool_requests_json、arguments_jsonは返しません" in prompt
    assert "hypothesis_evidence_bindings" not in prompt
    assert "dependency_article_bindings" not in prompt
    assert "fetch_articles_aliases" not in prompt
    assert "schemaにないSolverDecision項目は返さず" in prompt


def test_anthropic_generic_fetch_slot_is_canonicalized_without_another_model_call() -> None:
    payload = {
        "next": "continue",
        "update_json": "{}",
        "hypothesis_evidence_bindings": [],
        "tool_requests": {
            "tool_request_1_json": json.dumps(
                {
                    "request_id": "wrong-fetch",
                    "work_item_id": "w1",
                    "tool_name": "fetch_articles",
                    "arguments": {"article_ids": ["a1"]},
                    "purpose": "本文取得",
                    "hypothesis_ids": ["h1"],
                },
                ensure_ascii=False,
            )
        },
        "article_fetch": None,
    }

    normalized = _normalize_solver_payload(payload)

    assert normalized["tool_requests"] == [
        {
            "request_id": "wrong-fetch",
            "work_item_id": "w1",
            "tool_name": "fetch_articles",
            "arguments": {"article_ids": ["a1"]},
            "purpose": "本文取得",
            "hypothesis_ids": ["h1"],
        }
    ]


def test_anthropic_generic_article_fetch_alias_is_canonicalized() -> None:
    payload = {
        "next": "continue",
        "update_json": "{}",
        "hypothesis_evidence_bindings": [],
        "dependency_article_bindings": None,
        "tool_requests": {
            "tool_request_1_json": json.dumps(
                {
                    "request_id": "fetch-alias",
                    "work_item_id": "w1",
                    "tool_name": "article_fetch",
                    "arguments": {"article_ids": ["a1"]},
                    "purpose": "本文取得",
                    "hypothesis_ids": ["h1"],
                },
                ensure_ascii=False,
            )
        },
        "article_fetch": None,
    }

    normalized = _normalize_solver_payload(payload)

    assert normalized["tool_requests"][0]["tool_name"] == "fetch_articles"


@pytest.mark.parametrize("limitations", [None, ""])
def test_anthropic_finalize_normalizes_empty_limitations(limitations) -> None:
    payload = {
        "decision_json": json.dumps(
            {
                "next": "finalize",
                "decision_reason": "確認済み",
                "answer": {
                    "text": "回答",
                    "citation_ids": [],
                    "limitations": limitations,
                    "unresolved_work_item_ids": [],
                    "unresolved_hypothesis_ids": [],
                },
            },
            ensure_ascii=False,
        )
    }

    normalized = _normalize_solver_payload(payload)

    assert normalized["answer"]["limitations"] == []


def test_open_finalize_repair_schema_forces_the_next_cycle_shape() -> None:
    context = build_solver_context(
        CaseState(
            case_id="case-1",
            question="質問",
            research_cycle_count=1,
            work_items=(WorkItem(work_item_id="w1", question="確認する"),),
        ),
        AgentLimits(),
        remaining_wall_time_sec=120,
        finalize_only=False,
        contract_feedback=SolverContractFeedback(
            violation="finalize must account for every open WorkItem",
            previous_decision=SolverDecision(
                next="finalize",
                answer=FinalAnswer(text="回答"),
            ),
        ),
    ).model_copy(
        update={
            "graph_review_batch": GraphReviewBatch(remaining_unreviewed_count=5),
            "cycle_budget_reached": True,
            "cycle_close_required": True,
        }
    )

    schema = _solver_transport_schema(context)
    properties = schema["properties"]

    assert properties["next"]["enum"] == ["continue"]
    assert properties["start_next_cycle"]["enum"] == [True]
    assert properties["tool_requests_json"]["enum"] == ["[]"]
    assert properties["answer"]["type"] == "null"
    assert properties["answer"]["description"] == contract_field_description(
        SolverDecision,
        "answer",
    )
    assert properties["unreviewed_graph_resolution"]["properties"]["action"][
        "enum"
    ] == ["review_next_cycle"]
    assert properties["next_focus_work_item_ids"]["items"]["enum"] == ["w1"]
    assert properties["update_json"]["enum"] == ["{}"]


def test_open_finalize_repair_schema_forces_continue_within_cycle() -> None:
    context = build_solver_context(
        CaseState(
            case_id="case-1",
            question="質問",
            research_cycle_count=1,
            work_items=(WorkItem(work_item_id="w1", question="本文を確認する"),),
        ),
        AgentLimits(),
        remaining_wall_time_sec=120,
        finalize_only=False,
        contract_feedback=SolverContractFeedback(
            violation="finalize must account for every open WorkItem",
            previous_decision=SolverDecision(
                next="finalize",
                answer=FinalAnswer(text="回答"),
            ),
        ),
    )

    schema = _solver_transport_schema(context)
    properties = schema["properties"]

    assert properties["next"]["enum"] == ["continue"]
    assert properties["start_next_cycle"]["enum"] == [False]
    assert "enum" not in properties["tool_requests_json"]
    assert properties["answer"]["type"] == "null"
    assert properties["answer"]["description"] == contract_field_description(
        SolverDecision,
        "answer",
    )
    assert "enum" not in properties["update_json"]


def test_reference_only_contract_repairs_preserve_previous_case_update(
) -> None:
    previous = SolverDecision(
        next="finalize",
        update={
            "update_work_items": [
                {
                    "work_item_id": "w1",
                    "state": "resolved",
                    "resolution": "本文で確認した",
                    "basis_hypothesis_ids": [],
                }
            ]
        },
        answer=FinalAnswer(text="回答", citation_ids=("e1",)),
    )
    context = build_solver_context(
        CaseState(
            case_id="case-1",
            question="質問",
            research_cycle_count=1,
            work_items=(WorkItem(work_item_id="w1", question="本文を確認する"),),
        ),
        AgentLimits(),
        remaining_wall_time_sec=120,
        finalize_only=False,
        contract_feedback=SolverContractFeedback(
            violation=(
                "dependency action must reference a ToolRequest in the same decision"
            ),
            previous_decision=previous,
        ),
    )

    properties = _solver_anthropic_transport_schema(context)["properties"]

    assert properties["update_json"]["enum"] == ["{}"]


def test_tool_request_schema_is_empty_when_repair_has_no_open_work_item(
) -> None:
    previous = SolverDecision(
        next="continue",
        tool_requests=(
            ToolRequest(
                request_id="fetch-1",
                work_item_id="w1",
                tool_name="fetch_articles",
                arguments={"article_ids": ["article-1"]},
                purpose="追加本文を確認する",
            ),
        ),
    )
    context = build_solver_context(
        CaseState(
            case_id="case-1",
            question="質問",
            research_cycle_count=1,
            work_items=(
                WorkItem(
                    work_item_id="w1",
                    question="確認する",
                    state="resolved",
                    resolution="確認済み",
                ),
            ),
        ),
        AgentLimits(),
        remaining_wall_time_sec=120,
        finalize_only=False,
        contract_feedback=SolverContractFeedback(
            violation="tool requests must reference open WorkItem IDs: ['w1']",
            previous_decision=previous,
        ),
    )

    schema = _tool_requests_transport_schema(context)

    assert schema["maxItems"] == 0
    assert schema["items"] == {"type": "string"}


def test_unknown_retained_evidence_repair_schema_restores_known_id_enum(
) -> None:
    context = build_solver_context(
        CaseState(
            case_id="case-1",
            question="質問",
            research_cycle_count=1,
            evidence=(
                Evidence(
                    evidence_id="evidence-1",
                    source_ref="fixture:evidence-1",
                    content="取得本文",
                    created_cycle=1,
                ),
            ),
        ),
        AgentLimits(),
        remaining_wall_time_sec=120,
        finalize_only=False,
        contract_feedback=SolverContractFeedback(
            violation="unknown retained evidence IDs: ['article-1']",
            previous_decision=SolverDecision(
                next="continue",
                start_next_cycle=True,
                retain_evidence_ids=("article-1",),
            ),
        ),
    )

    schema = _solver_common_transport_schema(context)

    assert schema["properties"]["retain_evidence_ids"]["items"]["enum"] == [
        "evidence-1"
    ]


def test_cycle_boundary_transport_schemas_expose_no_new_tool_slots() -> None:
    context = build_solver_context(
        CaseState(
            case_id="case-1",
            question="質問",
            research_cycle_count=1,
            work_items=(WorkItem(work_item_id="w1", question="確認する"),),
        ),
        AgentLimits(max_tool_requests_per_step=5),
        remaining_wall_time_sec=120,
        finalize_only=False,
    ).model_copy(
        update={
            "fetchable_article_ids": ("a1",),
            "remaining_fetch_capacity": 1,
            "cycle_close_required": True,
        }
    )

    base = _solver_transport_schema(context)
    compact = _solver_compact_transport_schema(context)
    anthropic = _solver_anthropic_transport_schema(context)

    assert base["properties"]["tool_requests_json"]["enum"] == ["[]"]
    assert compact["properties"]["tool_requests"]["maxItems"] == 0
    assert anthropic["properties"]["tool_requests"]["properties"] == {}
    assert anthropic["properties"]["fetch_articles"]["type"] == "null"
    assert "description" in anthropic["properties"]["fetch_articles"]
    assert anthropic["properties"]["hypothesis_evidence_bindings"][
        "type"
    ] == "null"
    assert "description" in anthropic["properties"][
        "hypothesis_evidence_bindings"
    ]
    assert anthropic["properties"]["dependency_article_bindings"]["type"] == "null"
    assert "description" in anthropic["properties"]["dependency_article_bindings"]
    assert anthropic["properties"]["retain_evidence_ids"]["items"] == {
        "type": "null"
    }
    answer_object = anthropic["properties"]["answer"]["anyOf"][0]
    assert answer_object["properties"]["citation_ids"]["items"] == {
        "type": "null"
    }


def test_cycle_boundary_continue_is_normalized_to_the_next_cycle_shape() -> None:
    context = build_solver_context(
        CaseState(case_id="case-1", question="質問", research_cycle_count=1),
        AgentLimits(),
        remaining_wall_time_sec=120,
        finalize_only=False,
    ).model_copy(
        update={
            "cycle_close_required": True,
            "can_start_next_cycle": True,
        }
    )
    normalized = {
        "next": "continue",
        "start_next_cycle": False,
        "tool_requests": [{"tool_name": "fetch_articles"}],
        "dependency_decisions": [
            {
                "status": "needs_action",
                "action_request_id": "fetch-next",
            }
        ],
    }

    _normalize_absent_context_branches(normalized, context)

    assert normalized["start_next_cycle"] is True
    assert normalized["tool_requests"] == []
    assert normalized["dependency_decisions"][0]["action_request_id"] is None


def test_combined_fetch_over_cycle_capacity_preserves_llm_priority_order() -> None:
    context = build_solver_context(
        CaseState(case_id="case-1", question="質問", research_cycle_count=1),
        AgentLimits(),
        remaining_wall_time_sec=120,
        finalize_only=False,
    ).model_copy(
        update={
            "remaining_fetch_capacity": 4,
            "cycle_close_required": False,
        }
    )
    normalized = {
        "next": "continue",
        "start_next_cycle": False,
        "tool_requests": [
            {
                "request_id": "fetch-first",
                "tool_name": "fetch_articles",
                "arguments": {
                    "article_ids": ["a1", "a2", "a3"],
                },
            },
            {
                "request_id": "fetch-second",
                "tool_name": "fetch_articles",
                "arguments": {
                    "article_ids": ["a3", "a4", "a5"],
                },
            }
        ],
        "dependency_decisions": [],
    }

    _normalize_absent_context_branches(normalized, context)

    assert normalized["tool_requests"] == [
        {
            "request_id": "fetch-first",
            "tool_name": "fetch_articles",
            "arguments": {
                "article_ids": ["a1", "a2", "a3", "a4"],
            },
            "hypothesis_ids": [],
        }
    ]


def test_missing_basis_citation_repair_can_revise_evidence_selection() -> None:
    context = build_solver_context(
        CaseState(
            case_id="case-1",
            question="質問",
            research_cycle_count=1,
            work_items=(WorkItem(work_item_id="w1", question="本文を確認する"),),
        ),
        AgentLimits(),
        remaining_wall_time_sec=120,
        finalize_only=False,
        contract_feedback=SolverContractFeedback(
            violation=(
                "final answer citations omit Evidence declared as resolved "
                "WorkItem basis: ['e2']"
            ),
            previous_decision=SolverDecision(
                next="finalize",
                answer=FinalAnswer(text="回答", citation_ids=("e1",)),
            ),
        ),
    )

    schema = _solver_transport_schema(context)

    assert "enum" not in schema["properties"]["update_json"]


def test_citation_basis_repair_preserves_previous_final_answer() -> None:
    previous_answer = FinalAnswer(
        text="取得済み本文に基づく完全な回答",
        citation_ids=("e1", "e2"),
    )
    context = build_solver_context(
        CaseState(case_id="case-1", question="質問", research_cycle_count=1),
        AgentLimits(),
        remaining_wall_time_sec=120,
        finalize_only=True,
        contract_feedback=SolverContractFeedback(
            violation=(
                "answer citations require verified Hypothesis or resolved "
                "dependency basis: ['e2']"
            ),
            previous_decision=SolverDecision(
                next="finalize",
                answer=previous_answer,
            ),
        ),
    )

    assert _preserve_previous_answer_for_contract_repair(
        context,
        {"next": "finalize", "answer": {"text": "短縮された回答"}},
    )
    assert not _preserve_previous_answer_for_contract_repair(
        context,
        {"next": "continue", "answer": None},
    )


def test_open_finalize_adapter_preserves_previous_case_update() -> None:
    previous_decision = SolverDecision(
        next="finalize",
        update={
            "update_hypotheses": [
                {
                    "hypothesis_id": "h1",
                    "judgment": "unresolved",
                    "gaps": ["追加確認が必要"],
                }
            ]
        },
        answer=FinalAnswer(text="限定回答"),
    )
    context = build_solver_context(
        CaseState(
            case_id="case-1",
            question="質問",
            research_cycle_count=1,
            work_items=(WorkItem(work_item_id="w1", question="確認する"),),
            hypotheses=(
                Hypothesis(
                    hypothesis_id="h1",
                    work_item_id="w1",
                    statement="確認する",
                ),
            ),
        ),
        AgentLimits(),
        remaining_wall_time_sec=120,
        finalize_only=False,
        contract_feedback=SolverContractFeedback(
            violation="finalize must account for every open WorkItem",
            previous_decision=previous_decision,
        ),
    ).model_copy(
        update={
            "graph_review_batch": GraphReviewBatch(remaining_unreviewed_count=1),
            "cycle_budget_reached": True,
            "cycle_close_required": True,
        }
    )

    class RepairLLM:
        provider = "fake"

        def generate_structured_json(self, **kwargs: Any) -> StructuredJSONResult:
            return StructuredJSONResult(
                payload={
                    "next": "continue",
                    "start_next_cycle": True,
                    "update_json": "{}",
                    "next_focus_work_item_ids": ["w1"],
                    "retain_evidence_ids": [],
                    "tool_requests_json": "[]",
                    "dependency_decisions": [],
                    "graph_candidate_review": None,
                    "frontier_re_adoptions": [],
                    "deferred_frontier_resolutions": [],
                    "unreviewed_graph_resolution": {
                        "action": "review_next_cycle",
                        "reason": "次Cycleで確認する",
                    },
                    "answer": None,
                },
                provider="fake",
                model=kwargs["model"],
                latencyMs=1,
                inputTokens=10,
                outputTokens=10,
            )

    result = StructuredJSONModelAdapter(RepairLLM()).solve(
        context,
        ModelCallProfile(model="fake", system_prompt="判断する"),
    )

    assert result.decision.update == previous_decision.update


def test_preflight_reports_independent_contract_violations_together() -> None:
    state = CaseState(
        case_id="case-1",
        question="質問",
        research_cycle_count=1,
        work_items=(WorkItem(work_item_id="w1", question="確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="確認する",
            ),
        ),
    )
    decision = SolverDecision(
        next="finalize",
        update={
            "update_hypotheses": [
                {
                    "hypothesis_id": "h1",
                    "judgment": "supported",
                    "evidence_ids": ["unknown-evidence"],
                }
            ]
        },
        answer=FinalAnswer(text="回答"),
    )

    with pytest.raises(ContractViolation) as exc_info:
        apply_solver_decision(
            state,
            decision,
            limits=AgentLimits(),
            known_tool_names=set(),
            material_evidence_ids=(),
            unreviewed_graph_candidate_count=5,
            finalize_only=False,
        )

    message = str(exc_info.value)
    assert "multiple contract violations" in message
    assert "unknown evidence IDs" in message
    assert "unreviewed Graph candidate pool" in message
    assert "every open WorkItem" in message


def test_preflight_reports_invalid_tool_and_focus_references_together() -> None:
    state = CaseState(case_id="case-1", question="質問")
    decision = SolverDecision(
        next="continue",
        update={
            "add_work_items": [
                {"work_item_id": "w1", "question": "確認する"}
            ],
            "add_hypotheses": [
                {
                    "hypothesis_id": "h1",
                    "work_item_id": "w1",
                    "statement": "確認する",
                }
            ],
        },
        next_focus_work_item_ids=("unknown-work",),
        tool_requests=(
            ToolRequest(
                request_id="r1",
                work_item_id="unknown-work",
                tool_name="legal_search",
                arguments={"query": "確認"},
                purpose="確認する",
                hypothesis_ids=("unknown-hypothesis",),
            ),
        ),
    )

    with pytest.raises(ContractViolation) as exc_info:
        apply_solver_decision(
            state,
            decision,
            limits=AgentLimits(),
            known_tool_names={"legal_search"},
            material_evidence_ids=(),
            finalize_only=False,
        )

    message = str(exc_info.value)
    assert "multiple contract violations" in message
    assert "focus must reference open WorkItem IDs" in message
    assert "tool requests must reference open WorkItem IDs" in message
    assert "tool requests reference unknown Hypothesis IDs" in message


def test_unreviewed_graph_pool_can_remain_unresolved_only_in_limited_answer() -> None:
    state = CaseState(
        case_id="case-1",
        question="質問",
        research_cycle_count=4,
        work_items=(WorkItem(work_item_id="w1", question="府令を確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="府令が要件を定める",
                gaps=("Graph候補は未評価",),
            ),
        ),
    )
    decision = SolverDecision(
        next="finalize",
        unreviewed_graph_resolution=UnreviewedGraphResolution(
            action="unresolved_at_limit",
            reason="Cycle上限のため候補を評価できない",
        ),
        answer=FinalAnswer(
            text="確認済み範囲の限定回答",
            limitations=("府令候補は未確認",),
            unresolved_work_item_ids=("w1",),
            unresolved_hypothesis_ids=("h1",),
        ),
    )

    finalized = apply_solver_decision(
        state,
        decision,
        limits=AgentLimits(max_research_cycles=4),
        known_tool_names=set(),
        material_evidence_ids=(),
        unreviewed_graph_candidate_count=5,
        finalize_only=True,
        can_start_next_cycle=False,
    )

    assert finalized.unreviewed_graph_resolutions[0].action == "unresolved_at_limit"
    assert finalized.work_items[0].state == "open"


def test_answer_limitations_require_structured_unresolved_scope() -> None:
    with pytest.raises(ContractViolation, match="limitations and unresolved_work_item_ids"):
        apply_solver_decision(
            CaseState(case_id="case-1", question="質問"),
            SolverDecision(
                next="finalize",
                answer=FinalAnswer(
                    text="限定回答",
                    limitations=("府令本文は未確認",),
                ),
            ),
            limits=AgentLimits(),
            known_tool_names=set(),
            material_evidence_ids=(),
            finalize_only=False,
        )


def test_normal_finalize_cannot_leave_structured_unresolved_scope() -> None:
    state = CaseState(
        case_id="case-1",
        question="質問",
        work_items=(WorkItem(work_item_id="w1", question="府令を確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="府令が要件を定める",
                gaps=("本文未確認",),
            ),
        ),
    )
    decision = SolverDecision(
        next="finalize",
        answer=FinalAnswer(
            text="限定回答",
            limitations=("府令本文は未確認",),
            unresolved_work_item_ids=("w1",),
            unresolved_hypothesis_ids=("h1",),
        ),
    )

    with pytest.raises(ContractViolation, match="another Cycle can start"):
        apply_solver_decision(
            state,
            decision,
            limits=AgentLimits(),
            known_tool_names=set(),
            material_evidence_ids=(),
            finalize_only=False,
            can_start_next_cycle=True,
        )

    finalized = apply_solver_decision(
        state,
        decision,
        limits=AgentLimits(),
        known_tool_names=set(),
        material_evidence_ids=(),
        finalize_only=True,
        can_start_next_cycle=False,
    )
    assert finalized.work_items[0].state == "open"
    assert finalized.final_answer.unresolved_work_item_ids == ("w1",)


def test_limited_answer_allows_supported_hypothesis_with_open_dependency() -> None:
    state = CaseState(
        case_id="case-open-dependency",
        question="府令まで確認する",
        research_cycle_count=4,
        work_items=(WorkItem(work_item_id="w1", question="委任先を確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="法律が詳細を府令へ委任する",
                judgment="supported",
                evidence_ids=("e1",),
                gaps=(),
            ),
        ),
        evidence=(
            Evidence(
                evidence_id="e1",
                source_ref="fixture:e1",
                content="内閣府令で定める。",
                created_cycle=4,
                metadata={"articleId": "article-1"},
            ),
        ),
        dependency_decisions=(
            DependencyDecision(
                dependency_kind="lower_norm",
                work_item_id="w1",
                status="needs_action",
                reason="委任先本文が未確認",
                basis_evidence_ids=("e1",),
            ),
        ),
    )
    decision = SolverDecision(
        next="finalize",
        answer=FinalAnswer(
            text="委任元は確認できたが、委任先本文は確認できなかった。",
            limitations=("委任先本文は未確認",),
            unresolved_work_item_ids=("w1",),
            unresolved_hypothesis_ids=(),
        ),
    )

    finalized = apply_solver_decision(
        state,
        decision,
        limits=AgentLimits(max_research_cycles=4),
        known_tool_names=set(),
        material_evidence_ids=(),
        finalize_only=True,
        can_start_next_cycle=False,
    )

    assert finalized.final_answer is not None
    assert finalized.final_answer.unresolved_work_item_ids == ("w1",)

    context = build_solver_context(
        state,
        AgentLimits(max_research_cycles=4),
        remaining_wall_time_sec=60,
        finalize_only=True,
    )
    profile = legal_profiles.legal_agent_profile().solver_finalization
    assert profile is not None
    rendered = render_solver_model_call(
        context,
        profile,
        provider="openai",
        stage="finalization",
    )
    assert rendered.input_payload["open_work_item_ids"] == ["w1"]
    assert rendered.input_payload["unresolved_hypothesis_ids"] == []


def test_limited_answer_allows_supported_hypothesis_with_gaps() -> None:
    state = CaseState(
        case_id="case-supported-gap-finalization",
        question="具体的内容まで確認する",
        research_cycle_count=4,
        work_items=(WorkItem(work_item_id="w1", question="具体的内容を確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="上位規定が方針を認める",
                judgment="supported",
                evidence_ids=("e1",),
                gaps=("下位規定の具体的内容",),
            ),
        ),
        evidence=(
            Evidence(
                evidence_id="e1",
                source_ref="fixture:e1",
                content="上位規定の本文",
                created_cycle=4,
                metadata={"articleId": "article-1"},
            ),
        ),
    )
    decision = SolverDecision(
        next="finalize",
        answer=FinalAnswer(
            text="上位規定は確認できたが、具体的内容は確認できなかった。",
            citation_ids=("e1",),
            limitations=("下位規定の具体的内容は未確認",),
            unresolved_work_item_ids=("w1",),
            unresolved_hypothesis_ids=(),
        ),
    )

    finalized = apply_solver_decision(
        state,
        decision,
        limits=AgentLimits(max_research_cycles=4),
        known_tool_names=set(),
        material_evidence_ids=("e1",),
        finalize_only=True,
        can_start_next_cycle=False,
    )

    assert finalized.final_answer is not None
    assert finalized.final_answer.unresolved_work_item_ids == ("w1",)
    assert finalized.final_answer.unresolved_hypothesis_ids == ()

    context = build_solver_context(
        state,
        AgentLimits(max_research_cycles=4),
        remaining_wall_time_sec=60,
        finalize_only=True,
    )
    profile = legal_profiles.legal_agent_profile().solver_finalization
    assert profile is not None
    rendered = render_solver_model_call(
        context,
        profile,
        provider="openai",
        stage="finalization",
    )
    assert rendered.input_payload["open_work_item_ids"] == ["w1"]
    assert rendered.input_payload["unresolved_hypothesis_ids"] == []
    assert "`supported`でも`gaps`が残るHypothesis" in rendered.instructions


def test_limited_answer_allows_supported_hypothesis_with_unresolved_frontier(
) -> None:
    state = CaseState(
        case_id="case-open-frontier",
        question="保留候補を確認する",
        research_cycle_count=4,
        work_items=(WorkItem(work_item_id="w1", question="候補を確認する"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h1",
                work_item_id="w1",
                statement="取得済み本文は命題を支持する",
                judgment="supported",
                evidence_ids=("e1",),
                gaps=(),
            ),
        ),
        evidence=(
            Evidence(
                evidence_id="e1",
                source_ref="fixture:e1",
                content="取得済み本文。",
                created_cycle=4,
                metadata={"articleId": "article-1"},
            ),
        ),
    )
    resolution = DeferredFrontierResolution(
        frontier_item_id="frontier-1",
        article_id="article-2",
        work_item_id="w1",
        hypothesis_id="h1",
        action="unresolved_at_limit",
        reason="処理上限のため本文を取得できなかった。",
    )
    decision = SolverDecision(
        next="finalize",
        deferred_frontier_resolutions=(resolution,),
        answer=FinalAnswer(
            text="取得済み本文の範囲で回答する。",
            limitations=("保留候補の本文は未確認。",),
            unresolved_work_item_ids=("w1",),
            unresolved_hypothesis_ids=(),
        ),
    )

    finalized = apply_solver_decision(
        state,
        decision,
        limits=AgentLimits(max_research_cycles=4),
        known_tool_names=set(),
        material_evidence_ids=(),
        deferred_frontiers={"frontier-1": ("article-2", "w1", "h1")},
        finalize_only=True,
        can_start_next_cycle=False,
    )

    assert finalized.final_answer is not None
    assert finalized.final_answer.unresolved_work_item_ids == ("w1",)
    assert finalized.deferred_frontier_resolutions[0].action == (
        "unresolved_at_limit"
    )


def test_contract_repair_prompt_handles_unknown_article_ids() -> None:
    context = build_solver_context(
        CaseState(case_id="case-1", question="質問"),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
        contract_feedback=SolverContractFeedback(
            violation="tool request references unknown Article IDs: ['unknown']",
            previous_decision=SolverDecision(
                next="finalize",
                answer={"text": "回答"},
            ),
        ),
    )

    prompt = _solver_prompt(context, "system")

    assert "既存のWorkItem、Hypothesis、Evidence、Articleを参照するID" in prompt
    assert "`fetch_articles`には`fetchable_article_ids`の完全一致だけ" in prompt
    assert "Paragraph・ItemのEvidence IDをArticle IDとして使いません" in prompt
    assert '"violation":"tool request references unknown Article IDs' in prompt


def test_contract_repair_prompt_handles_duplicate_tool_request_ids() -> None:
    context = build_solver_context(
        CaseState(case_id="case-1", question="質問"),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
        contract_feedback=SolverContractFeedback(
            violation="duplicate tool request ID: legal_search_1",
            previous_decision=SolverDecision(
                next="finalize",
                answer={"text": "回答"},
            ),
        ),
    )

    prompt = _solver_prompt(context, "system")

    assert "同じDecision内で相互に異なる短い局所ID" in prompt
    assert "action_request_idにも同じ局所IDをコピー" in prompt
    assert '"violation":"duplicate tool request ID' in prompt


def test_contract_repair_prompt_aligns_resolved_work_item_and_hypotheses() -> None:
    context = build_solver_context(
        CaseState(case_id="case-1", question="質問"),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
        contract_feedback=SolverContractFeedback(
            violation=(
                "resolved work item retains unresolved basis hypotheses: "
                "w1=['h1']"
            ),
            previous_decision=SolverDecision(
                next="finalize",
                answer={"text": "回答"},
            ),
        ),
    )

    prompt = _solver_prompt(context, "system")

    assert "WorkItemだけをresolvedにしません" in prompt
    assert "同じDecisionで各Hypothesisを根拠付き" in prompt
    assert "WorkItemをopen、resolution=nullのままcontinue" in prompt


def test_action_feedback_reports_repeated_successful_search() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_same_cycle_repeated_search_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    context = build_solver_context(
        CaseState(case_id="case-1", question="質問"),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
        action_feedback=SolverActionFeedback(
            code="already_completed",
            message=fixture["observedFailure"]["violation"],
            rejected_tool_requests=(
                ToolRequest(
                    request_id="repeat-search",
                    work_item_id="w1",
                    tool_name="legal_search",
                    arguments={"query": "公開買付け 手続"},
                    purpose="成功済み検索を繰り返す",
                ),
            ),
        ),
        available_tools=(
            LegalSearchTool.definition,
            LegalFetchArticlesTool.definition,
            LegalGraphNeighborsTool.definition,
        ),
    )

    prompt = _solver_prompt(context, "system")

    assert '"code":"already_completed"' in prompt
    assert '"tool_name":"legal_search"' in prompt
    assert fixture["observedFailure"]["violation"] in prompt

    profile = legal_profiles.legal_agent_profile().solver_integration
    rendered = render_solver_model_call(
        context,
        profile,
        provider="openai",
        stage="integration",
    )
    variants = rendered.output_schema["properties"]["tool_requests"][
        "items"
    ]["anyOf"]
    allowed_names = {
        variant["properties"]["tool_name"]["enum"][0]
        for variant in variants
    }
    assert "legal_search" in allowed_names
    assert "fetch_articles" in allowed_names
    assert "legal_graph_neighbors" in allowed_names


def test_action_feedback_reports_repeated_successful_graph() -> None:
    context = build_solver_context(
        CaseState(case_id="case-1", question="質問"),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
        action_feedback=SolverActionFeedback(
            code="already_completed",
            message=(
                "successful legal_graph_neighbors scope was already completed"
            ),
            rejected_tool_requests=(
                ToolRequest(
                    request_id="repeat-graph",
                    work_item_id="w1",
                    tool_name="legal_graph_neighbors",
                    arguments={
                        "article_ids": ["law-a-article-1"],
                        "mode": "semantic_assertion",
                        "predicate": "IMPLEMENTS",
                        "direction": "from_subject",
                    },
                    purpose="成功済みGraph探索を繰り返す",
                ),
            ),
        ),
    )

    prompt = _solver_prompt(context, "system")

    assert '"code":"already_completed"' in prompt
    assert '"tool_name":"legal_graph_neighbors"' in prompt
    assert "successful legal_graph_neighbors scope was already completed" in prompt


def test_minimal_solver_contract_defines_state_field_invariants() -> None:
    context = build_solver_context(
        CaseState(case_id="case-1", question="質問"),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )

    prompt = _solver_prompt(context, "system")

    assert "state=openは未完了なのでresolution=null" in prompt
    assert "resolved/droppedは終了状態なので空でないresolution" in prompt
    assert "judgment=unresolvedは未確認" in prompt
    assert "supported/contradictedは本文根拠で確認済み" in prompt
    assert "正規契約のupdateに許されるキーはadd_work_items" in prompt
    assert "work_tree等の現在状態を返さない" in prompt
    assert "add_work_items要素: work_item_id" in prompt
    assert "今回適用する最終差分を1件だけ返す" in prompt
    assert "state、resolution" in prompt
    assert "statusは使わない" in prompt
    assert "ToolRequest.work_item_idは、このupdate適用後もstate=open" in prompt
    assert "Toolが必要ならWorkItemを閉じない" in prompt
    assert "actionはretain / replace / drop" in prompt
    assert "それ以外は空配列" in prompt
    assert "retain_evidence_idsはmax_retained_evidence件以内" in prompt
    assert "既存のWorkItem、Hypothesis、Evidence、Articleを参照するID" in prompt
    assert "ToolRequestのrequest_idは同じDecision内だけで重複しない短い局所ID" in prompt
    assert "Programが永続化用IDへ置き換える" in prompt
    assert "IDはSolverContextまたは直前Decisionに表示された値だけ" not in prompt


def test_contract_repair_instructions_are_stable_across_violations() -> None:
    first_context = build_solver_context(
        CaseState(case_id="case-1", question="質問"),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
        contract_feedback=SolverContractFeedback(
            violation="retained evidence count exceeds the profile limit",
            previous_decision=SolverDecision(next="finalize", answer={"text": "回答"}),
        ),
    )

    second_context = build_solver_context(
        CaseState(case_id="case-2", question="別の質問"),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
        contract_feedback=SolverContractFeedback(
            violation="duplicate tool request ID: request-1",
            previous_decision=SolverDecision(
                next="finalize",
                answer={"text": "回答"},
            ),
        ),
    )
    profile = ModelCallProfile(model="test-model", system_prompt="system")

    first = render_solver_model_call(first_context, profile, provider="openai")
    second = render_solver_model_call(second_context, profile, provider="openai")

    assert "後続Cycleにも本文が必要なEvidenceをLLMが選びます" in first.instructions
    assert first.instructions_hash == second.instructions_hash
    assert first.input_hash != second.input_hash


def test_validated_copy_reports_the_invalid_state_field() -> None:
    item = WorkItem(work_item_id="w1", question="確認する")

    with pytest.raises(ContractViolation) as exc_info:
        _validated_copy(item, resolution="未完了なのに解決文がある")

    message = str(exc_info.value)
    assert "updated state violates its schema" in message
    assert "open work item cannot have a resolution" in message


def test_contract_repair_prompt_does_not_close_work_only_to_pass_finalize() -> None:
    context = build_solver_context(
        CaseState(
            case_id="case-1",
            question="質問",
            work_items=(WorkItem(work_item_id="w1", question="府令を確認する"),),
            hypotheses=(
                Hypothesis(
                    hypothesis_id="h1",
                    work_item_id="w1",
                    statement="府令が要件を定める",
                    gaps=("本文未確認",),
                ),
            ),
        ),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
        contract_feedback=SolverContractFeedback(
            violation="finalize must account for every open WorkItem",
            previous_decision=SolverDecision(
                next="finalize",
                answer=FinalAnswer(text="回答"),
            ),
        ),
    )

    prompt = _solver_prompt(context, "system")

    assert "追加調査できるならopenのままcontinue" in prompt
    assert "不能時だけlimitationsと既知の未解決IDを対応" in prompt
    assert '"violation":"finalize must account for every open WorkItem"' in prompt


def test_solver_prompt_fails_instead_of_dropping_context() -> None:
    context = build_solver_context(
        CaseState(case_id="case-1", question="質問"),
        AgentLimits(
            max_material_evidence_chars=1000,
            max_solver_input_chars=2000,
        ),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )

    with pytest.raises(ContextCapacityExceeded, match="context_capacity_exceeded"):
        _solver_prompt(context, "system")


def test_finalize_only_context_never_claims_that_another_cycle_can_start() -> None:
    context = build_solver_context(
        CaseState(case_id="case-1", question="質問", research_cycle_count=4),
        AgentLimits(max_research_cycles=4),
        remaining_wall_time_sec=60,
        finalize_only=True,
    )

    assert context.remaining_research_cycles == 0
    assert context.can_start_next_cycle is False


def test_finalization_requires_all_open_scope_ids() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_finalization_omits_open_scope_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    work_item_ids = tuple(fixture["expectedBehavior"]["unresolvedWorkItemIds"])
    hypothesis_ids = tuple(
        fixture["expectedBehavior"]["unresolvedHypothesisIds"]
    )
    state = CaseState(
        case_id="fixture-finalization-open-scope",
        question="公開買付けの条件、範囲、例外、手続を確認する。",
        research_cycle_count=4,
        work_items=tuple(
            WorkItem(work_item_id=item_id, question=item_id)
            for item_id in work_item_ids
        ),
        hypotheses=tuple(
            Hypothesis(
                hypothesis_id=hypothesis_id,
                work_item_id=work_item_ids[index],
                statement=hypothesis_id,
            )
            for index, hypothesis_id in enumerate(hypothesis_ids)
        ),
    )
    profile = legal_profiles.legal_agent_profile().solver_finalization
    assert profile is not None
    context = build_solver_context(
        state,
        AgentLimits(max_research_cycles=4),
        remaining_wall_time_sec=60,
        finalize_only=True,
    )

    rendered = render_solver_model_call(
        context,
        profile,
        provider="openai",
        stage="finalization",
    )

    schema = rendered.output_schema["properties"]
    assert schema["next"]["enum"] == ["finalize"]
    assert "start_next_cycle" not in schema
    assert "update" not in schema
    assert schema["answer"]["type"] == "object"
    answer = schema["answer"]["properties"]
    assert answer["limitations"]["minItems"] == 1
    assert answer["unresolved_work_item_ids"]["minItems"] == len(work_item_ids)
    assert answer["unresolved_work_item_ids"]["maxItems"] == len(work_item_ids)
    assert answer["unresolved_hypothesis_ids"]["minItems"] == len(
        hypothesis_ids
    )
    assert answer["unresolved_hypothesis_ids"]["maxItems"] == len(
        hypothesis_ids
    )


def test_anthropic_finalization_exposes_the_nested_answer_contract() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_finalization_hidden_answer_shape_v350.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState(
        case_id=fixture["source"]["caseId"],
        question="公開買付けの条件、範囲、例外、手続を確認する。",
        research_cycle_count=4,
        work_items=(WorkItem(work_item_id="wi-1", question="確認する。"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h-1",
                work_item_id="wi-1",
                statement="確認事項がある。",
            ),
        ),
    )
    context = build_solver_context(
        state,
        AgentLimits(max_research_cycles=4),
        remaining_wall_time_sec=35,
        finalize_only=True,
    )
    rendered = render_solver_model_call(
        context,
        legal_profiles.legal_agent_profile().solver_finalization,
        provider="anthropic",
        stage="finalization",
    )

    assert "decision_json" not in rendered.output_schema["properties"]
    assert set(rendered.output_schema["properties"]) <= {
        "next",
        "decision_reason",
        "answer",
        "deferred_frontier_resolutions",
        "unreviewed_graph_resolution",
    }
    assert "update" not in rendered.output_schema["properties"]
    assert "tool_requests" not in rendered.output_schema["properties"]
    answer_schema = rendered.output_schema["properties"]["answer"]
    assert list(answer_schema["properties"]) == fixture["expectedAnswerKeys"]
    assert "summary" not in answer_schema["properties"]
    assert "answer_body" not in answer_schema["properties"]


def test_graph_review_prompt_limits_each_batch_to_one_hypothesis() -> None:
    profile = legal_profiles.legal_agent_profile().solver_graph_review

    assert "同じWorkItem・Hypothesisについて今回判断する候補" in (
        profile.system_prompt
    )
    assert "同じHypothesisの残り候補は、必要なら次Cycle" in (
        profile.system_prompt
    )


def test_anthropic_integration_describes_the_nested_answer_contract() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_integration_hidden_answer_shape_v351.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState(
        case_id=fixture["source"]["caseId"],
        question="公開買付けの条件、範囲、例外、手続を確認する。",
        research_cycle_count=1,
        work_items=(WorkItem(work_item_id="wi-1", question="確認する。"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h-1",
                work_item_id="wi-1",
                statement="確認事項がある。",
            ),
        ),
    )
    context = build_solver_context(
        state,
        AgentLimits(max_research_cycles=4),
        remaining_wall_time_sec=100,
        finalize_only=False,
    )
    rendered = render_solver_model_call(
        context,
        legal_profiles.legal_agent_profile().solver_integration,
        provider="anthropic",
        stage="integration",
    )

    description = rendered.output_schema["properties"]["decision_json"][
        "description"
    ]
    for field_name in fixture["expectedAnswerKeys"]:
        assert field_name in description
    assert "exactly these current-step top-level fields" in description
    for field_name in fixture["forbiddenTopLevelInputFields"]:
        assert field_name not in description


def test_finalization_excludes_unverified_work_item_evidence() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_finalization_asserts_unresolved_threshold_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState(
        case_id="fixture-finalization-unsupported-threshold",
        question="公開買付けの条件、範囲、例外、手続を確認する。",
        research_cycle_count=4,
        work_items=(
            WorkItem(work_item_id="wi-1", question="成立条件"),
            WorkItem(work_item_id="wi-2", question="対象範囲"),
            WorkItem(work_item_id="wi-3", question="例外"),
            WorkItem(work_item_id="wi-4", question="必要手続"),
        ),
        hypotheses=tuple(
            Hypothesis(
                hypothesis_id=f"h-{index}",
                work_item_id=f"wi-{index}",
                statement=f"未確認命題{index}",
                gaps=(f"未確認事項{index}",),
            )
            for index in range(1, 5)
        ),
        evidence=(
            Evidence(
                evidence_id="search-nav-50-percent",
                source_ref="fixture:search-nav-50-percent",
                content="特別支配関係について議決権の百分の五十を超える場合。",
                created_cycle=4,
                metadata={
                    "evidenceRole": "search_navigation",
                    "articleId": "ordinance-article-2_3",
                },
            ),
            Evidence(
                evidence_id="unresolved-grounding",
                source_ref="fixture:unresolved-grounding",
                content="質問とは異なる規律の取得本文。",
                created_cycle=4,
                metadata={
                    "evidenceRole": "retrieved_text",
                    "articleId": "ordinance-article-63",
                    "citationEligible": True,
                },
            ),
        ),
        retained_evidence_ids=(
            "search-nav-50-percent",
            "unresolved-grounding",
        ),
    )
    context = build_solver_context(
        state,
        AgentLimits(max_research_cycles=4),
        remaining_wall_time_sec=60,
        finalize_only=True,
    )
    assert {item.evidence_id for item in context.material_evidence} == {
        "search-nav-50-percent",
        "unresolved-grounding",
    }

    profile = legal_profiles.legal_agent_profile().solver_finalization
    assert profile is not None
    rendered = render_solver_model_call(
        context,
        profile,
        provider="openai",
        stage="finalization",
    )

    expected = fixture["expectedBehavior"]
    assert expected["excludeNavigationEvidence"] is True
    assert rendered.input_payload["material_evidence"] == []
    assert rendered.input_payload["grounding_evidence_ids"] == []
    assert "navigation_evidence_ids" not in rendered.input_payload
    assert "`verified_hypothesis_ids=[]`なら" in (
        rendered.instructions
    )
    assert rendered.input_payload["resolved_work_item_ids"] == []
    assert rendered.input_payload["open_work_item_ids"] == [
        "wi-1",
        "wi-2",
        "wi-3",
        "wi-4",
    ]
    assert rendered.input_payload["unresolved_hypothesis_ids"] == [
        "h-1",
        "h-2",
        "h-3",
        "h-4",
    ]
    assert rendered.input_payload["verified_hypothesis_ids"] == []
    assert "limitationsで未確認とした内容を回答本文で断定していないか" in (
        rendered.instructions
    )
    answer_schema = rendered.output_schema["properties"]["answer"]["properties"]
    assert answer_schema["citation_ids"]["maxItems"] == 0


def test_finalization_rejects_citations_without_resolved_work_item_basis() -> None:
    state = CaseState(
        case_id="fixture-finalization-no-resolved-basis",
        question="公開買付けの成立条件を確認する。",
        research_cycle_count=4,
        work_items=(WorkItem(work_item_id="wi-1", question="成立条件"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h-1",
                work_item_id="wi-1",
                statement="取得割合が基準になる。",
                gaps=("具体的な割合",),
            ),
        ),
        evidence=(
            Evidence(
                evidence_id="e-unresolved",
                source_ref="fixture:e-unresolved",
                content="別の条件について百分の五十と定める。",
                created_cycle=4,
                metadata={"citationEligible": True},
            ),
        ),
    )
    decision = SolverDecision(
        next="finalize",
        answer=FinalAnswer(
            text="取得割合は50%である。",
            citation_ids=("e-unresolved",),
            limitations=("具体的な取得割合は未確認。",),
            unresolved_work_item_ids=("wi-1",),
            unresolved_hypothesis_ids=("h-1",),
        ),
    )

    with pytest.raises(
        ContractViolation,
        match="citations require verified Hypothesis or resolved dependency basis",
    ):
        apply_solver_decision(
            state,
            decision,
            limits=AgentLimits(max_research_cycles=4),
            known_tool_names=set(),
            material_evidence_ids=("e-unresolved",),
            finalize_only=True,
            can_start_next_cycle=False,
        )


def test_overview_finalization_projects_verified_open_work_material() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_finalization_cites_open_work_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState.model_validate(fixture["caseState"])
    observed = SolverDecision.model_validate(fixture["observedDecision"])

    finalized = apply_solver_decision(
        state,
        observed,
        limits=AgentLimits(max_research_cycles=4),
        known_tool_names=set(),
        material_evidence_ids=("e-resolved", "e-open"),
        finalize_only=True,
        can_start_next_cycle=False,
    )
    assert finalized.final_answer is not None
    assert finalized.final_answer.citation_ids == ("e-resolved", "e-open")

    context = build_solver_context(
        state,
        AgentLimits(max_research_cycles=4),
        remaining_wall_time_sec=30,
        finalize_only=True,
    )
    profile = legal_profiles.legal_agent_profile().solver_finalization
    assert profile is not None
    rendered = render_solver_model_call(
        context,
        profile,
        provider="openai",
        stage="finalization",
    )

    assert set(rendered.input_payload) == {
        "question",
        "non_work_item_requirements",
        "finalize_only",
        "work_tree",
        "hypotheses",
        "grounding_evidence_ids",
        "material_evidence",
        "graph_review_ledger",
        "contract_feedback",
        "resolved_work_item_ids",
        "open_work_item_ids",
        "unresolved_hypothesis_ids",
        "verified_hypothesis_ids",
        "required_answer_evidence_ids",
    }
    assert rendered.input_payload["required_answer_evidence_ids"] == [
        "e-resolved"
    ]
    assert rendered.input_payload["grounding_evidence_ids"] == [
        "e-resolved",
        "e-open",
    ]
    assert [
        item["hypothesis_id"] for item in rendered.input_payload["hypotheses"]
    ] == ["h-resolved", "h-open-supported", "h-open-unresolved"]
    assert rendered.input_payload["verified_hypothesis_ids"] == [
        "h-resolved",
        "h-open-supported",
    ]
    citation_schema = rendered.output_schema["properties"]["answer"][
        "properties"
    ]["citation_ids"]
    assert citation_schema["items"]["enum"] == ["e-resolved", "e-open"]
    assert "## Solver共通ルール" not in rendered.instructions
    assert "## 調査の完了ルール" not in rendered.instructions


def test_finalization_does_not_reaudit_saved_dependency_decisions() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "framework"
        / "tob_overview_finalization_reaudits_omitted_dependency_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    state = CaseState(
        case_id="fixture-finalization-dependency",
        question="公開買付けの例外を確認する。",
        research_cycle_count=4,
        work_items=(WorkItem(work_item_id="wi-3", question="例外"),),
        hypotheses=(
            Hypothesis(
                hypothesis_id="h-3",
                work_item_id="wi-3",
                statement="例外がある。",
            ),
        ),
        tool_requests=(
            ToolRequest(
                request_id="fetch-1",
                work_item_id="wi-3",
                tool_name="fetch_articles",
                arguments={"article_ids": ["article-63"]},
                purpose="例外本文を取得する",
                hypothesis_ids=("h-3",),
            ),
        ),
        tool_results=(
            ToolResult(
                request_id="fetch-1",
                status="succeeded",
                evidence_ids=("e-63",),
                cycle_no=3,
            ),
        ),
        evidence=(
            Evidence(
                evidence_id="e-63",
                source_ref="fixture:e-63",
                content="例外本文",
                created_cycle=3,
                metadata={
                    "articleId": "article-63",
                    "citationEligible": True,
                },
            ),
        ),
    )

    assert _dependency_audit_scope(
        state,
        integration_call=True,
        finalize_only=False,
        required_dependency_kind="lower_norm",
    ) == ("wi-3",)
    assert _dependency_audit_scope(
        state,
        integration_call=True,
        finalize_only=True,
        required_dependency_kind="lower_norm",
    ) == tuple(fixture["expectedBehavior"]["requiredDependencyWorkItemIds"])


def test_explicit_framework_endpoint_does_not_use_legacy_service(monkeypatch) -> None:
    expected = AnswerResponse(
        pattern="agent_framework_v1",
        route=["agent_framework"],
        answer="新経路",
        citations=[],
        graphPaths=[],
        trace={"agentFramework": {"reviewerEnabled": False}},
    )
    monkeypatch.setattr(
        main.framework_agent_service,
        "answer",
        lambda request: expected,
    )
    monkeypatch.setattr(
        main.agent_service,
        "answer",
        lambda request: (_ for _ in ()).throw(AssertionError("legacy called")),
    )

    response = main.framework_answer(AnswerRequest(question="質問"))

    assert response["pattern"] == "agent_framework_v1"


def test_answer_feature_flag_selects_new_framework(monkeypatch) -> None:
    expected = AnswerResponse(
        pattern="agent_framework_v1",
        route=["agent_framework"],
        answer="新経路",
        citations=[],
        graphPaths=[],
        trace={},
    )
    monkeypatch.setattr(main.settings, "agent_framework_active", True)
    monkeypatch.setattr(
        main.framework_agent_service,
        "answer",
        lambda request: expected,
    )

    response = main.answer(AnswerRequest(question="質問"))

    assert response["pattern"] == "agent_framework_v1"


def test_model_adapter_repairs_transport_json_once(tmp_path) -> None:
    class RepairClient:
        def __init__(self) -> None:
            self.calls = 0

        def generate_structured_json(self, **kwargs: Any) -> StructuredJSONResult:
            self.calls += 1
            payload = (
                {"next": "finalize", "answer": None}
                if self.calls == 1
                else {
                    "next": "finalize",
                    "answer": {"text": "修復済み"},
                }
            )
            return StructuredJSONResult(
                payload=payload,
                provider="fake",
                model="fake-model",
                latencyMs=1,
                inputTokens=1,
                outputTokens=1,
            )

    client = RepairClient()
    limits = AgentLimits()
    context = build_solver_context(
        CaseState(case_id="case-1", question="質問"),
        limits,
        remaining_wall_time_sec=60,
        finalize_only=False,
    )
    diagnostics = AgentDiagnostics(
        mode="snapshot",
        output_dir=tmp_path,
        case_id=context.case_id,
        profile_name="test-profile",
        profile_version="1",
    )

    result = StructuredJSONModelAdapter(client, diagnostics=diagnostics).solve(
        context,
        ModelCallProfile(
            model="fake-model",
            system_prompt="prompt",
        ),
    )

    assert result.decision.answer is not None
    assert result.decision.answer.text == "修復済み"
    assert result.attempt_count == 2
    assert client.calls == 2
    assert diagnostics.output_path is not None
    records = [
        json.loads(line)
        for line in diagnostics.output_path.read_text(encoding="utf-8").splitlines()
    ]
    transport_inputs = [
        item for item in records if item["event"] == "transport_input"
    ]
    assert len(transport_inputs) == 2
    assert transport_inputs[0]["promptAssets"] == []
    repair_sections = [
        item["name"]
        for item in transport_inputs[1]["promptAssets"][0]["sections"]
    ]
    assert repair_sections[0] == "stable"
    assert "finalize_requires_answer" in repair_sections
    assert transport_inputs[0]["promptHash"] != transport_inputs[1]["promptHash"]
    assert Path(transport_inputs[0]["artifactPath"], "instructions.md").is_file()
    assert Path(transport_inputs[1]["artifactPath"], "output_schema.json").is_file()
    complete_path = Path(transport_inputs[0]["completeRequestPath"])
    assert complete_path.is_file()
    complete = json.loads(complete_path.read_text(encoding="utf-8"))
    assert complete["prompt"] == transport_inputs[0]["prompt"]
    assert complete["outputSchema"] == transport_inputs[0]["transportSchema"]


def test_model_adapter_repairs_semantic_judgment_without_evidence_once() -> None:
    class RepairClient:
        def __init__(self) -> None:
            self.calls = 0
            self.prompts: list[str] = []

        def generate_structured_json(self, **kwargs: Any) -> StructuredJSONResult:
            self.calls += 1
            self.prompts.append(kwargs["prompt"])
            judgment = "supported" if self.calls == 1 else "unresolved"
            return StructuredJSONResult(
                payload={
                    "next": "continue",
                    "update": {
                        "update_hypotheses": [
                            {
                                "hypothesis_id": "h1",
                                "judgment": judgment,
                                "evidence_ids": [],
                                "gaps": ["本文未取得"],
                            }
                        ]
                    },
                    "next_focus_work_item_ids": ["w1"],
                    "tool_requests": [
                        {
                            "request_id": "search-1",
                            "work_item_id": "w1",
                            "tool_name": "legal_search",
                            "arguments": {"query": "根拠条文", "doc_types": ["law"]},
                            "purpose": "本文候補を探す",
                            "hypothesis_ids": ["h1"],
                        }
                    ],
                },
                provider="fake",
                model="fake-model",
                latencyMs=1,
                inputTokens=1,
                outputTokens=1,
            )

    client = RepairClient()
    context = build_solver_context(
        CaseState(
            case_id="case-1",
            question="質問",
            work_items=(WorkItem(work_item_id="w1", question="確認する"),),
            hypotheses=(
                Hypothesis(
                    hypothesis_id="h1",
                    work_item_id="w1",
                    statement="根拠本文で確認する",
                ),
            ),
        ),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )

    result = StructuredJSONModelAdapter(client).solve(
        context,
        ModelCallProfile(model="fake-model", system_prompt="prompt"),
    )

    update = result.decision.update.update_hypotheses[0]
    assert update.judgment == "unresolved"
    assert result.attempt_count == 2
    assert client.calls == 2
    assert "supported or contradicted hypothesis requires evidence" in client.prompts[1]


def test_model_adapter_normalizes_provider_timeout() -> None:
    class TimeoutClient:
        def generate_structured_json(self, **kwargs: Any) -> StructuredJSONResult:
            del kwargs
            raise requests.ReadTimeout("provider did not respond")

    context = build_solver_context(
        CaseState(case_id="case-1", question="質問"),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )

    with pytest.raises(TimeoutError, match="model provider request timed out"):
        StructuredJSONModelAdapter(TimeoutClient()).solve(
            context,
            ModelCallProfile(
                model="fake-model",
                system_prompt="prompt",
            ),
        )
