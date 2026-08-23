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
    _case_update_transport_schema,
    _normalize_absent_context_branches,
    _normalize_solver_payload,
    _search_reselection_prompt,
    _search_reselection_transport_schema,
    _search_review_prompt,
    _search_review_context_payload,
    _search_review_transport_schema,
    _solver_anthropic_transport_schema,
    _solver_compact_transport_schema,
    _solver_prompt,
    _solver_transport_schema,
    _validate_search_assessment_payload,
    render_search_assessment_model_call,
    render_solver_model_call,
    render_solver_transport_repair_model_call,
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
    SearchCandidateArticle,
    SolverContext,
    SolverContractFeedback,
    _graph_review_projection,
    build_solver_context,
)
from app.agent_framework.contracts import SolverDecision, WorkItemImpactDecision
from app.agent_framework.diagnostics import AgentDiagnostics
from app.agent_framework.loop import AgentLoop, _dependency_audit_work_item_ids
from app.agent_framework.ports.model import ModelProtocolError, ReviewerView
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
                if not isinstance(field_schema, dict) or not field_schema.get(
                    "description"
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
                if not isinstance(field_schema, dict) or not field_schema.get(
                    "description"
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
        assert definition.input_schema["additionalProperties"] is False
        for property_schema in definition.input_schema["properties"].values():
            assert property_schema["description"]


class FakeStructuredLLM:
    provider = "fake"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.payloads: list[dict[str, Any]] = [
            {
                "next": "continue",
                "decision_reason": "要件を定めるArticleをまだ取得していないため検索する",
                "update": {
                    "add_work_items": [
                        {
                            "work_item_id": "w1",
                            "question": "根拠条文を確認する",
                        }
                    ],
                    "add_hypotheses": [
                        {
                            "hypothesis_id": "h1",
                            "work_item_id": "w1",
                            "statement": "要件が条文に定められている",
                        }
                    ],
                },
                "next_focus_work_item_ids": ["w1"],
                "tool_requests": [
                    {
                        "request_id": "r1",
                        "work_item_id": "w1",
                        "tool_name": "legal_search",
                        "arguments": ('{"query":"検証法 要件","doc_types":["law"]}'),
                        "purpose": "根拠条文を検索する",
                        "hypothesis_ids": ["h1"],
                    }
                ],
            },
            {
                "search_request_ids": ["r1"],
                "assessments": [
                    {
                        "article_id": "law-test-article-2",
                        "legal_function": "applicability",
                        "summary": "検証法の適用要件を定める",
                    }
                ],
                "reason": "検索候補1件を評価した",
            },
            {
                "selections": [
                    {
                        "article_id": "law-test-article-2",
                        "reason": "検証法の要件を定める検索候補である",
                    }
                ],
                "reason": "適用要件を確認する候補を選別した",
            },
            {
                "next": "finalize",
                "decision_reason": "要件本文と下位規範不要判断の根拠が揃ったため完了する",
                "update": {
                    "update_work_items": [
                        {
                            "work_item_id": "w1",
                            "state": "resolved",
                            "resolution": "本文を確認した",
                        }
                    ],
                    "update_hypotheses": [
                        {
                            "hypothesis_id": "h1",
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
                        "work_item_id": "w1",
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
        if "question_requirement_checklist" in kwargs["schema"]["properties"]:
            work_items = decision.get("update", {}).get("add_work_items", [])
            return StructuredJSONResult(
                payload={
                    "question_requirement_checklist": [
                        item["question"] for item in work_items
                    ],
                    "next": decision["next"],
                    "decision_reason": decision["decision_reason"],
                    "start_next_cycle": False,
                    "update": decision["update"],
                    "next_focus_work_item_ids": decision.get(
                        "next_focus_work_item_ids", []
                    ),
                    "tool_requests": decision.get("tool_requests", []),
                },
                provider="fake",
                model=kwargs["model"],
                latencyMs=1,
                inputTokens=10,
                outputTokens=10,
            )
        if "next" not in kwargs["schema"]["properties"]:
            search_request_schema = kwargs["schema"]["properties"].get(
                "search_request_ids"
            )
            if search_request_schema is not None:
                decision["search_request_ids"] = search_request_schema["items"][
                    "enum"
                ]
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
    assert framework_trace["researchCycleCount"] == 1
    assert len(framework_trace["modelCalls"]) == 3
    assert [item["purpose"] for item in framework_trace["modelCalls"]] == [
        "research",
        "search_selection",
        "integration",
    ]
    assert framework_trace["toolCalls"][0]["arguments"] == {
        "query": "検証法 要件",
        "doc_types": ["law"],
    }
    assert framework_trace["toolCalls"][0]["purpose"] == "根拠条文を検索する"
    assert [item["tool_name"] for item in framework_trace["toolCalls"]] == [
        "legal_search",
        "fetch_articles",
    ]
    assert len(llm.calls) == 4
    assert "decision_json" not in llm.calls[0]["schema"]["properties"]
    assert "next" in llm.calls[0]["schema"]["properties"]
    assert "decision_reason" in llm.calls[0]["schema"]["required"]
    assert "question_requirement_checklist" in (
        llm.calls[0]["schema"]["properties"]
    )
    assert "dependency_decisions" not in llm.calls[0]["schema"]["properties"]
    assert "dependency_decisions_json" not in llm.calls[0]["schema"]["properties"]
    search_schema = llm.calls[1]["schema"]
    assert set(search_schema["properties"]) == {
        "search_request_ids",
        "assessments",
        "reason",
    }
    assert search_schema["properties"]["assessments"]["minItems"] == 1
    assert set(llm.calls[2]["schema"]["properties"]) == {
        "selections",
        "reason",
    }
    final_dependency_schema = llm.calls[3]["schema"]["properties"][
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
    assert len(framework_trace["appliedDecisionSequences"]) == 3
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
    assert diagnostic_records[0]["profileVersion"] == "145"
    transport_input = next(
        item for item in diagnostic_records if item["event"] == "transport_input"
    )
    assert len(transport_input["promptHash"]) == 64
    assert len(transport_input["schemaHash"]) == 64
    assert len(transport_input["systemPromptHash"]) == 64
    assert transport_input["profileName"] == "legal-default"
    assert transport_input["profileVersion"] == "145"
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
    assert "search_assessment" in transport_stages
    assert "search_reselection" in transport_stages
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


def test_legal_solver_prompts_are_projected_by_structural_mode() -> None:
    profile = legal_profiles.legal_agent_profile()

    assert profile.version == "145"
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
    for prompt in mode_prompts.values():
        assert [line for line in prompt.splitlines() if line.startswith("# ")] == [
            "# 法令調査Solver"
        ]
        assert "Programへ意味判断" in prompt
        assert "現在のCycleを閉じて次Cycleへ移る場合だけ`true`" in prompt
        assert "初回ToolでCycle 1" not in prompt
        assert "action-observation step" not in prompt
        assert "## 出力前の完了確認" not in prompt

    research_prompt = mode_prompts["research"]
    assert profile.solver_research.context_projection == "initial_research"
    assert profile.solver_integration.context_projection == "full"
    assert "## Tool選択ルール" in research_prompt
    assert "## 現在の作業：Research" in research_prompt
    assert research_prompt.index("## 現在の作業：Research") < (
        research_prompt.index("## 共通ルール")
    )
    assert research_prompt.index("## 現在の作業：Research") < (
        research_prompt.index("## Tool選択ルール")
    )
    assert "案件開始時に、元の質問の回答範囲" in research_prompt
    assert "## 完了ルール" in research_prompt
    assert "1つの完了判定で閉じられる1つの確認事項" in research_prompt
    assert "近くにある名詞ではなく、質問が求める答え" in research_prompt
    assert "主文の問い" in research_prompt
    assert "A、B、Cを一項目ずつ照合" in research_prompt
    assert "列挙された問いを、近い意味の別の問いへ吸収しません" in (
        research_prompt
    )
    assert "必要となる条件、対象行為、例外、届出方法の4項目" in (
        research_prompt
    )
    assert "「根拠」を5番目のWorkItemにもしません" in research_prompt
    assert "この例の項目名や件数は、別の質問へコピーしません" in (
        research_prompt
    )
    assert "`question_requirement_checklist`へ" in research_prompt
    assert "いつ・どのような場合に必要か" in research_prompt
    assert "必要になった場合に何をするか" in research_prompt
    assert "作業分解の数を減らしません" in research_prompt
    assert "`legal_search`要求数の上限ではありません" in research_prompt
    assert "理由: <分解と最初の行動を選んだ理由>" in research_prompt
    assert "3つの確認事項" not in research_prompt
    assert "質問の「必要な手続」" not in research_prompt
    assert "機械的に分割" in research_prompt
    assert "情報の種類を条件・範囲・行為等へ限定しません" in (
        research_prompt
    )
    assert "同じ制度や近い手続に関する本文でも" in research_prompt
    assert "## 現在の作業：Integration" not in research_prompt
    assert "## 現在の作業：Cycle Close" not in research_prompt

    integration_prompt = mode_prompts["integration"]
    assert "## Tool選択ルール" in integration_prompt
    assert "## 現在の作業：Integration" in integration_prompt
    assert integration_prompt.index("## 現在の作業：Integration") < (
        integration_prompt.index("## 共通ルール")
    )
    assert "新しいTool結果と取得本文を現在の調査状態へ反映" in (
        integration_prompt
    )
    assert "### 次の行動を決める手順" in integration_prompt
    assert "### Tool選択ルール" in integration_prompt
    assert "`request_id`を`action_request_id`へそのままコピー" in integration_prompt
    assert "## 完了ルール" in integration_prompt
    assert "## 現在の作業：Cycle Close" not in integration_prompt
    assert "## 現在の作業：Reviewer Revision" not in integration_prompt
    assert "`search_candidates`があれば" in integration_prompt
    assert "候補で不足する確認事項を`decision_reason`" in integration_prompt
    assert "新しい`legal_search`を考える前に全候補" in integration_prompt
    assert "1つの`fetch_articles`で本文取得" in integration_prompt
    assert "次に取得するArticle IDではありません" in integration_prompt
    assert "項目の意味は`contract_glossary`を正本" in integration_prompt
    assert "`basis_evidence_ids`、`metadata.articleId`から作りません" in (
        integration_prompt
    )

    cycle_close_prompt = mode_prompts["cycle_close"]
    assert "## 現在の作業：Cycle Close" in cycle_close_prompt
    assert cycle_close_prompt.index("## 現在の作業：Cycle Close") < (
        cycle_close_prompt.index("## 共通ルール")
    )
    assert "deferred_frontier_resolutions" in cycle_close_prompt
    assert "同じIDを重複させず" in cycle_close_prompt
    assert "## Tool選択ルール" not in cycle_close_prompt

    finalization_prompt = mode_prompts["finalization"]
    assert "## 現在の作業：Finalization" in finalization_prompt
    assert "追加調査できない実行上限時" in finalization_prompt
    assert "finalize_only=true" in finalization_prompt
    assert "## Tool選択ルール" not in finalization_prompt

    reviewer_prompt = mode_prompts["reviewer_revision"]
    assert "## 現在の作業：Reviewer Revision" in reviewer_prompt
    assert "Reviewerの指摘を受け取ったSolver" in reviewer_prompt
    assert "review_finding_resolutions" in reviewer_prompt
    assert "## Tool選択ルール" in reviewer_prompt

    assert len(research_prompt) < 15000
    assert len(integration_prompt) < 18000
    assert len(cycle_close_prompt) < 9000
    assert len(finalization_prompt) < 7000
    assert len(reviewer_prompt) < 12000

    assert profile.automatic_tools == ()
    assert len(profile.tool_list_argument_limits) == 2
    assert profile.graph_review_fetch_tool_name == "fetch_articles"
    assert profile.required_dependency_kind == "lower_norm"


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
    assert fixture["profileVersion"] == profile.version
    assert prompt.rindex("## 出力前の完了確認") > prompt.rindex(
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

    assert rebuilt.model_dump(mode="json") == saved.model_dump(mode="json")
    assert observed["profileVersion"] == "126"
    assert len(observed["workItems"]) == 1
    assert "対象となる株券等の範囲、主な例外、必要な手続" in (
        observed["workItems"][0]["question"]
    )
    assert "複数の問いを質問全文の写しへまとめたりしません" in prompt
    assert "総数が`add_work_items`の件数と一致" in prompt
    assert "必要条件、対象範囲、例外、手続" not in prompt
    assert prompt.rindex("## 出力前の完了確認") > prompt.rindex(
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
    assert "主文の問い" in prompt
    assert "追加された問いだけを扱ったり" in prompt
    assert "列挙された各項目を省略" in prompt


def test_search_review_duplicate_fixture_gets_ordered_id_checklist() -> None:
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
    checklist = rendered.input_payload["candidate_checklist"]
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

    assert checklist == {
        "candidate_count": len(candidate_ids),
        "article_ids_in_input_order": candidate_ids,
    }
    assert prompt.index("## 出力前の完了確認") > prompt.index(
        "</solver_context>"
    )
    with pytest.raises(ModelProtocolError, match=fixture["expectedViolation"]):
        _validate_search_assessment_payload(observed_payload, context)
    _validate_search_assessment_payload(
        {
            "search_request_ids": fixture["requiredSearchRequestIds"],
            "assessments": [
                {
                    "article_id": article_id,
                    "legal_function": "scope",
                    "summary": "要約",
                }
                for article_id in candidate_ids
            ],
            "reason": "全候補を確認した",
        },
        context,
    )


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
    assert "いつ・どのような場合に必要か" in prompt
    assert "必要になった場合に何をするか" in prompt
    assert "いつ・どの条件で必要か" in prompt


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
    assert "中心命題の1件だけで選択を止めません" in prompt
    assert "直接検証できる候補がある未確認Hypothesis" in prompt


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
    assert "成功済みの同じ`legal_search`を繰り返さず" in prompt
    assert "成功済み検索と異なる検索表現" in prompt


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
    assert "作業分解の数を減らしません" in prompt
    assert "今Stepで探索しないWorkItemもopen" in prompt
    assert "取得枠やToolRequest上限に合わせて" in prompt


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
    assert "`fetchable_article_ids`の完全一致だけ" in prompt
    assert "Paragraph・ItemのEvidence ID" in prompt
    assert "本文取得済みなので、再取得要求を削除" in repair
    assert "Paragraph・ItemのEvidence IDをArticle IDとして使いません" in (
        repair
    )


def test_solver_completion_check_follows_the_complete_context() -> None:
    profile = legal_profiles.legal_agent_profile().solver_cycle_close
    assert profile is not None
    assert profile.completion_check_prompt is not None
    context = build_solver_context(
        CaseState(case_id="case-completion-check", question="質問"),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
    )

    prompt = _solver_prompt(
        context,
        profile.system_prompt,
        completion_check_prompt=profile.completion_check_prompt,
    )
    context_start = prompt.index("<solver_context>") + len("<solver_context>")
    context_end = prompt.index("</solver_context>")
    check_start = prompt.index("## 出力前の完了確認")

    assert check_start > context_end
    assert json.loads(prompt[context_start:context_end]) == context.model_dump(
        mode="json"
    )
    assert prompt.endswith(profile.completion_check_prompt)
    assert "resolutionを支える判定済みHypothesis ID" in prompt[check_start:]


def test_candidate_selection_profiles_have_post_context_checks() -> None:
    profile = legal_profiles.legal_agent_profile()
    search = profile.solver_search_review
    graph = profile.solver_graph_review

    assert search is not None
    assert search.completion_check_prompt is not None
    assert search.followup_completion_check_prompt is not None
    assert "重複も欠落もない" in search.completion_check_prompt
    assert "selected / deferred" in search.followup_completion_check_prompt
    assert graph is not None
    assert graph.completion_check_prompt is not None
    assert "全Graph候補とLinkを一度ずつ評価" in (
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
    assert assessment_prompt.rindex("## 出力前の完了確認") > (
        assessment_prompt.rindex("</solver_context>")
    )
    assert reselection_prompt.endswith(search.followup_completion_check_prompt)
    assert reselection_prompt.rindex("## 出力前の完了確認") > (
        reselection_prompt.rindex("</search_review_summary>")
    )


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
        remaining_wall_time_sec=120,
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


def test_graph_review_prompt_has_one_document_hierarchy() -> None:
    graph_prompt = legal_profiles.legal_agent_profile().solver_graph_review
    assert graph_prompt is not None
    prompt = graph_prompt.system_prompt
    assert [line for line in prompt.splitlines() if line.startswith("# ")] == [
        "# 法令調査Solver：Graph Reviewモード"
    ]
    assert "## 入力" in prompt
    assert "## 手順" in prompt
    assert "## Relationの読み方" in prompt
    assert "## 選択ルール" in prompt
    assert "## 出力" in prompt


def test_cycle_close_prompt_separates_steps_from_decision_rules() -> None:
    profile = legal_profiles.legal_agent_profile()
    prompt = profile.solver_cycle_close.system_prompt

    assert "### 手順" in prompt
    assert "### 判断ルール" in prompt
    assert "### 出力ルール" in prompt
    assert "### 引継ぎルール" in prompt
    assert "open WorkItemまたはunresolved Hypothesisが残り" in prompt
    assert "`next=continue`、`start_next_cycle=true`" in prompt
    assert "`next=finalize`では`answer`を必ず返します" in prompt
    assert "| `needs_action` |" in prompt
    assert "末端の具体化規定を未確認" in prompt
    assert "「政令で定める」「府令で定める」" in prompt
    assert "`basis_evidence_ids`には、その判断に使ったgrounding Evidence" in prompt
    assert "`action_request_id`は`null`" in prompt


def test_search_review_prompt_defines_evidence_join_and_limited_role() -> None:
    search_prompt = legal_profiles.legal_agent_profile().solver_search_review
    assert search_prompt is not None
    prompt = search_prompt.system_prompt
    assert [line for line in prompt.splitlines() if line.startswith("# ")] == [
        "# 法令調査Solver：Search Assessmentモード"
    ]
    assert "`search_excerpts`" in prompt
    assert "法的結論、Hypothesisの支持・反証" in prompt
    assert "自分の言葉" in prompt
    assert "この段階では候補を選びません" in prompt
    assert search_prompt.followup_system_prompt is not None
    assert "提示された`assessments`だけを比較" in (
        search_prompt.followup_system_prompt
    )


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
    assert "assessments" not in updated.search_candidate_reviews[0].model_dump()

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

    assert rebuilt.model_dump(mode="json") == saved.model_dump(mode="json")
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

    assert rebuilt.model_dump(mode="json") == saved.model_dump(mode="json")
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
    assert "1つの確認事項につき1つのWorkItem" in prompt
    assert "何らかの規定がある" in prompt
    assert "条文に現れやすい法令表現へ言い換えます" in prompt
    assert "WorkItem wi-condition" in prompt
    assert "basis_hypothesis_ids: []" in prompt
    assert "Hypothesis.work_item_id" in prompt
    assert "ToolRequest tr-condition" in prompt


def test_real_model_cycle2_repeated_search_fixture_is_reproducible() -> None:
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
    profile = legal_profiles.legal_agent_profile().solver_integration
    assert profile is not None
    assert profile.completion_check_prompt is not None
    prompt = _solver_prompt(
        saved,
        profile.system_prompt,
        completion_check_prompt=profile.completion_check_prompt,
        compact_transport=True,
    )

    assert rebuilt.model_dump(mode="json") == saved.model_dump(mode="json")
    assert fixture["source"]["model"] == "gpt-4o-mini"
    assert failure["violation"] == (
        "successful legal_search scope was already completed"
    )
    assert failure["contractAttemptCount"] == 3
    assert {
        "law-402M50000040038-article-2_5",
        "law-402M50000040038-article-10",
    } <= set(saved.fetchable_article_ids)
    assert "新しい`legal_search`を考える前に全候補" in prompt
    assert "成功済みの同じ`legal_search`を繰り返さず" in prompt


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


def test_solver_context_carries_unfetched_search_candidates_across_cycles() -> None:
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
    schema = _solver_compact_transport_schema(context)
    hypothesis_updates = schema["properties"]["update"]["properties"][
        "update_hypotheses"
    ]
    cycle_close_prompt = legal_profiles.legal_agent_profile().solver_cycle_close

    assert cycle_close_prompt is not None
    assert cycle_close_prompt.completion_check_prompt is not None
    assert "resolved WorkItemの`basis_hypothesis_ids`へunresolved Hypothesis" in (
        cycle_close_prompt.system_prompt
    )
    rendered_prompt = _solver_prompt(
        context,
        cycle_close_prompt.system_prompt,
        completion_check_prompt=cycle_close_prompt.completion_check_prompt,
        compact_transport=True,
    )
    assert rendered_prompt.rindex("## 出力前の完了確認") > rendered_prompt.rindex(
        "</solver_context>"
    )
    assert "can_start_next_cycle=true" in cycle_close_prompt.completion_check_prompt
    assert "WorkItemだけをresolvedにしません" in render_solver_model_call(
        context,
        cycle_close_prompt,
        provider="openai",
        stage="cycle_close",
    ).instructions
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

    assert view["candidate_count"] == len(context.search_candidates)
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


def test_anthropic_prompt_limits_bindings_to_current_hypothesis_updates() -> None:
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

    assert "今回のupdate_jsonのadd_hypotheses" in prompt
    assert "変更しない既存Hypothesisは返しません" in prompt
    assert "専用fetch_articles欄は正規のfetch_articles ToolRequestの輸送表現" in prompt
    assert "tool_name=fetch_articlesを決して再掲しません" in prompt
    assert "dependency_article_bindingsへ判断に使った取得済みArticle ID" in prompt
    assert "160文字以内の短いASCII識別子" in prompt


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


def test_combined_fetch_over_cycle_capacity_is_rejected_before_contract_validation() -> None:
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

    with pytest.raises(ModelProtocolError, match="at most 4 unique Article IDs"):
        _normalize_absent_context_branches(normalized, context)


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


def test_contract_repair_prompt_handles_repeated_successful_search() -> None:
    context = build_solver_context(
        CaseState(case_id="case-1", question="質問"),
        AgentLimits(),
        remaining_wall_time_sec=60,
        finalize_only=False,
        contract_feedback=SolverContractFeedback(
            violation="successful legal_search scope was already completed",
            previous_decision=SolverDecision(
                next="finalize",
                answer={"text": "回答"},
            ),
        ),
    )

    prompt = _solver_prompt(context, "system")

    assert "成功済みのlegal_searchを再要求しません" in prompt
    assert "search_candidatesと対応する検索抜粋を評価" in prompt
    assert "既存候補では検証できないと判断した場合だけ" in prompt


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
