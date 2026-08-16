import json

import pytest

from app.config import settings
from app.llm import (
    LLMResult,
    _answer_contract_error,
    _answer_json_schema,
    _format_citations_with_budget,
    _parse_answer_payload,
    _parse_evidence_evaluation,
    _parse_search_plan,
    _sum_optional,
    _to_anthropic_schema,
    _shown_citations_for_prompt,
    citation_context_stats,
    build_answer_prompt,
)
from app.models import AnswerRequest, Citation

CHOICES = {"A": "選択肢A", "B": "選択肢B", "C": "選択肢C", "D": "選択肢D"}
CITATION_IDS = ["law-test-article-1", "law-test-article-2"]


def _payload(**overrides) -> str:
    payload = {
        "answer": f"根拠説明 {CITATION_IDS[0]}",
        "questionPolarity": "select_entailed",
        "predictedAnswer": "A",
        "choiceAssessments": {
            "A": {
                "verdict": "entailed",
                "citationIds": [CITATION_IDS[0]],
                "reason": "条文に合致",
                "confidence": 0.9,
            },
            "B": {
                "verdict": "contradicted",
                "citationIds": [CITATION_IDS[0]],
                "reason": "条文と矛盾",
                "confidence": 0.8,
            },
            "C": {
                "verdict": "contradicted",
                "citationIds": [CITATION_IDS[1]],
                "reason": "条文と矛盾",
                "confidence": 0.7,
            },
            "D": {"verdict": "insufficient", "citationIds": [], "reason": "根拠不足", "confidence": 0.4},
        },
        "answerStatus": None,
        "citationIds": None,
        "missing": None,
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


class TestParseAnswerPayload:
    def test_valid_payload(self):
        answer, predicted, judgements, assessments, polarity, error = _parse_answer_payload(
            _payload(), CHOICES, CITATION_IDS
        )
        assert answer == f"結論: 選択肢A。根拠説明 {CITATION_IDS[0]}"
        assert predicted == "A"
        assert judgements["A"] == "supported"
        assert assessments["A"]["verdict"] == "entailed"
        assert polarity == "select_entailed"
        assert error is None

    def test_json_parse_error_returns_raw_text(self):
        answer, predicted, judgements, assessments, polarity, error = _parse_answer_payload(
            "答えはAです", CHOICES, CITATION_IDS
        )
        assert answer == "答えはAです"
        assert predicted is None
        assert judgements is None
        assert assessments is None
        assert polarity is None
        assert error.startswith("json_parse_error")

    def test_negative_question_selects_contradicted_statement(self):
        assessments = _payload()
        raw_payload = json.loads(assessments)
        raw_payload["questionPolarity"] = "select_contradicted"
        raw_payload["predictedAnswer"] = "B"
        _, predicted, judgements, parsed_assessments, polarity, error = _parse_answer_payload(
            json.dumps(raw_payload, ensure_ascii=False), CHOICES, CITATION_IDS
        )
        assert error is None
        assert predicted == "B"
        assert judgements["B"] == "supported"
        assert parsed_assessments["B"]["verdict"] == "contradicted"
        assert polarity == "select_contradicted"

    def test_unknown_model_prediction_is_rejected(self):
        _, predicted, judgements, _, _, error = _parse_answer_payload(
            _payload(predictedAnswer="E"), CHOICES, CITATION_IDS
        )
        assert predicted is None
        assert judgements is None
        assert "predictedAnswer must be one of" in error

    def test_assessment_keys_mismatch_is_rejected(self):
        raw_payload = json.loads(_payload())
        raw_payload["choiceAssessments"] = {"A": raw_payload["choiceAssessments"]["A"]}
        _, predicted, _, _, _, error = _parse_answer_payload(
            json.dumps(raw_payload), CHOICES, CITATION_IDS
        )
        assert predicted is None
        assert error.startswith("validation_error")

    def test_invalid_assessment_value_is_rejected(self):
        raw_payload = json.loads(_payload())
        raw_payload["choiceAssessments"]["A"]["verdict"] = "maybe"
        _, predicted, _, _, _, error = _parse_answer_payload(
            json.dumps(raw_payload), CHOICES, CITATION_IDS
        )
        assert predicted is None
        assert error.startswith("validation_error")

    def test_unknown_citation_id_is_rejected(self):
        raw_payload = json.loads(_payload())
        raw_payload["choiceAssessments"]["A"]["citationIds"] = ["law-unknown-article-9"]
        _, predicted, _, _, _, error = _parse_answer_payload(
            json.dumps(raw_payload), CHOICES, CITATION_IDS
        )
        assert predicted is None
        assert "unknown IDs" in error

    def test_assessment_citations_over_contract_are_rejected(self):
        ids = [f"law-test-article-{index}" for index in range(1, 6)]
        raw_payload = json.loads(_payload())
        raw_payload["choiceAssessments"]["A"]["citationIds"] = [*ids, ids[0]]
        _, predicted, _, _, _, error = _parse_answer_payload(
            json.dumps(raw_payload), CHOICES, ids
        )
        assert predicted is None
        assert "too_long" in error

    def test_choice_answer_references_must_be_in_selected_assessment(self):
        raw_payload = json.loads(_payload())
        raw_payload["answer"] = f"根拠説明 {CITATION_IDS[1]}"
        *_, error = _parse_answer_payload(
            json.dumps(raw_payload), CHOICES, CITATION_IDS
        )
        assert "must be included in predictedAnswer citationIds" in error

    def test_choice_answer_may_reference_subset_of_selected_assessment(self):
        raw_payload = json.loads(_payload())
        raw_payload["choiceAssessments"]["A"]["citationIds"] = CITATION_IDS
        *_, error = _parse_answer_payload(
            json.dumps(raw_payload), CHOICES, CITATION_IDS
        )
        assert error is None

    def test_inconsistent_model_prediction_is_rejected(self):
        _, predicted, _, _, _, error = _parse_answer_payload(
            _payload(predictedAnswer="B"), CHOICES, CITATION_IDS
        )
        assert predicted is None
        assert "inconsistent with questionPolarity" in error

    def test_no_choices_requires_null_fields(self):
        raw = _payload(
            answer=(
                f"根拠説明 {CITATION_IDS[0]} {CITATION_IDS[1]}"
            ),
            questionPolarity=None,
            predictedAnswer=None,
            choiceAssessments=None,
            answerStatus="ready",
            citationIds=CITATION_IDS,
            missing=[],
        )
        answer, predicted, judgements, assessments, polarity, error = _parse_answer_payload(
            raw, None, CITATION_IDS, 2
        )
        assert error is None
        assert predicted is None
        assert judgements is None
        assert assessments is None
        assert polarity is None

    def test_no_choices_rejects_unknown_final_citation(self):
        raw = _payload(
            questionPolarity=None,
            predictedAnswer=None,
            choiceAssessments=None,
            answerStatus="ready",
            citationIds=["invented-id"],
            missing=[],
        )
        *_, error = _parse_answer_payload(raw, None, CITATION_IDS, 2)
        assert "unknown IDs" in error

    def test_ready_answer_cannot_claim_missing_items(self):
        raw = _payload(
            questionPolarity=None,
            predictedAnswer=None,
            choiceAssessments=None,
            answerStatus="ready",
            citationIds=[CITATION_IDS[0]],
            missing=["例外"],
        )
        *_, error = _parse_answer_payload(raw, None, CITATION_IDS, 2)
        assert "ready answer cannot contain missing" in error

    def test_free_text_answer_references_must_be_in_structured_citations(self):
        raw = _payload(
            answer=(
                f"根拠説明 {CITATION_IDS[0]} {CITATION_IDS[1]}"
            ),
            questionPolarity=None,
            predictedAnswer=None,
            choiceAssessments=None,
            answerStatus="ready",
            citationIds=[CITATION_IDS[0]],
            missing=[],
        )
        *_, error = _parse_answer_payload(raw, None, CITATION_IDS, 2)
        assert "must be included in citationIds" in error

    def test_free_text_answer_may_reference_subset_of_structured_citations(self):
        raw = _payload(
            answer=f"根拠説明 {CITATION_IDS[0]}",
            questionPolarity=None,
            predictedAnswer=None,
            choiceAssessments=None,
            answerStatus="ready",
            citationIds=CITATION_IDS,
            missing=[],
        )
        *_, error = _parse_answer_payload(raw, None, CITATION_IDS, 2)
        assert error is None

    def test_no_choices_with_assessments_is_rejected(self):
        _, predicted, _, _, _, error = _parse_answer_payload(_payload(), None)
        assert predicted is None
        assert error.startswith("validation_error")

    def test_validation_error_keeps_answer_field(self):
        raw_payload = json.loads(_payload())
        raw_payload["choiceAssessments"]["A"]["verdict"] = "invalid"
        answer, _, _, _, _, error = _parse_answer_payload(
            json.dumps(raw_payload), CHOICES, CITATION_IDS
        )
        assert answer == f"根拠説明 {CITATION_IDS[0]}"
        assert error is not None


def test_free_text_prompt_limits_claims_to_main_selected_citations() -> None:
    prompt = build_answer_prompt(
        AnswerRequest(question="全要件を教えてください", topK=5),
        ["answer_composer"],
        [Citation(documentId="law-test", contentUnitId=CITATION_IDS[0], text="本文")],
    )

    assert "citationIdsに選んだ引用だけ" in prompt
    assert "answer本文にはcontentUnitIdを書かず" in prompt
    assert "構造化citationIdsだけ" in prompt
    assert "partial" in prompt
    assert "missing" in prompt


def test_main_revision_prompt_includes_previous_structured_decision() -> None:
    prompt = build_answer_prompt(
        AnswerRequest(question="要件と手続を説明してください", topK=5),
        ["llm_directed_legal_research"],
        [Citation(documentId="law-test", contentUnitId=CITATION_IDS[0], text="本文")],
        review_feedback=["手続が欠落している"],
        review_verdict="needs_research",
        previous_answer="要件だけを回答します。",
        previous_answer_status="ready",
        previous_citation_ids=[CITATION_IDS[0]],
        previous_missing=[],
    )

    assert "前回のMain Agent判断" in prompt
    assert '"answerStatus": "ready"' in prompt
    assert f'"citationIds": ["{CITATION_IDS[0]}"]' in prompt
    assert '"verdict": "needs_research"' in prompt
    assert "各指摘を引用本文と質問に照らして" in prompt
    assert "質問が発生条件、対象、例外、手続などを複数明示" in prompt


def test_research_main_receives_issue_contract_without_intermediate_conclusion() -> None:
    prompt = build_answer_prompt(
        AnswerRequest(question="要件と手続を説明してください", topK=5),
        ["llm_directed_legal_research"],
        [Citation(documentId="law-test", contentUnitId=CITATION_IDS[0], text="本文")],
        research_context={
            "answerContract": {
                "version": "issue-grounding-v1",
                "issues": [{"issueId": "ISSUE-1", "question": "要件は何か"}],
                "availableCitationIds": [CITATION_IDS[0]],
                "maxSelectedCitations": 5,
            },
            "researchConclusion": "誤った中間結論",
            "logicalStructure": {"hypotheses": ["誤った仮説"]},
        },
    )

    assert '"issueId": "ISSUE-1"' in prompt
    assert "issueDecisions" in prompt
    assert "誤った中間結論" not in prompt
    assert "誤った仮説" not in prompt


def test_main_issue_contract_checks_only_structural_consistency() -> None:
    result = LLMResult(
        text="回答",
        provider="fake",
        model="fake",
        latencyMs=1,
        inputTokens=1,
        outputTokens=1,
        estimatedCost=0,
        answer="回答",
        predictedAnswer=None,
        choiceJudgements=None,
        answerStatus="ready",
        answerCitationIds=[CITATION_IDS[0]],
        missing=[],
        answerIssueDecisions=[
            {
                "issueId": "ISSUE-1",
                "status": "ready",
                "conclusion": "LLMが判断した結論",
                "citationIds": [CITATION_IDS[0]],
                "missing": [],
            }
        ],
    )
    context = {
        "answerContract": {
            "issues": [{"issueId": "ISSUE-1", "question": "要件は何か"}]
        }
    }

    assert _answer_contract_error(result, context) is None
    result.answerCitationIds = [CITATION_IDS[1]]
    assert "union of issueDecisions" in _answer_contract_error(result, context)


class TestParseSearchPlan:
    def test_valid_plan_is_deduplicated_and_limited(self):
        raw = json.dumps(
            {"queries": ["定義 条文", "定義  条文", "例外 条文"], "graphRequired": True},
            ensure_ascii=False,
        )
        queries, graph_required, error = _parse_search_plan(raw, 2)
        assert queries == ["定義 条文", "例外 条文"]
        assert graph_required is True
        assert error is None

    def test_invalid_plan_falls_back_outside_parser(self):
        queries, graph_required, error = _parse_search_plan('{"queries": []}', 4)
        assert queries == []
        assert graph_required is False
        assert error.startswith("search_plan_validation_error")


def test_citation_budget_keeps_every_citation_visible():
    citations = [
        Citation(documentId="law-test", contentUnitId=f"article-{index}", text="本文" * 1000)
        for index in range(1, 6)
    ]

    block = _format_citations_with_budget(citations, 4000)

    assert len(block) <= 4000
    assert all(f"article-{index}" in block for index in range(1, 6))


def test_citation_context_stats_report_prompt_truncation_deterministically():
    citations = [
        Citation(documentId="law-test", contentUnitId=f"article-{index}", text="本文" * 1000)
        for index in range(1, 4)
    ]

    stats = citation_context_stats(citations, 1200)
    block = _format_citations_with_budget(citations, 1200)

    assert stats["occurred"] is True
    assert stats["truncatedChunkCount"] > 0
    assert stats["includedChars"] == len(block)
    assert stats["includedChars"] <= 1200
    assert stats["truncatedContentUnitIds"] == [
        "article-1",
        "article-2",
        "article-3",
    ]


def test_omitted_citation_is_not_available_to_answer_schema() -> None:
    citations = [
        Citation(documentId="law-test", contentUnitId=f"article-{index}", text="本文")
        for index in range(1, 4)
    ]
    original_limit = settings.llm_finalization_material_max_items
    settings.llm_finalization_material_max_items = 2
    try:
        shown = _shown_citations_for_prompt(citations)
    finally:
        settings.llm_finalization_material_max_items = original_limit

    assert [item.contentUnitId for item in shown] == ["article-1", "article-2"]


def test_valid_evidence_evaluation():
    raw = json.dumps(
        {
            "choiceCoverage": {label: "sufficient" for label in CHOICES},
            "followUpQueries": ["定義条文", "  定義条文  ", "例外条文"],
            "graphRequired": True,
            "stop": False,
        },
        ensure_ascii=False,
    )

    coverage, queries, graph_required, stop, error = _parse_evidence_evaluation(raw, CHOICES, 2)

    assert coverage["A"] == "sufficient"
    assert queries == ["定義条文", "例外条文"]
    assert graph_required is True
    assert stop is False
    assert error is None


def test_retry_token_counts_are_summed_without_turning_missing_into_missing():
    assert _sum_optional(10, 7) == 17
    assert _sum_optional(None, 7) == 7
    assert _sum_optional(None, None) is None


class TestToAnthropicSchema:
    """Anthropic構造化出力はunion型+enumを拒否するため、方言変換がanyOfへ展開することを固定する。"""

    def test_union_type_with_enum_becomes_anyof(self):
        schema = {"type": ["string", "null"], "enum": ["A", "B", None]}
        converted = _to_anthropic_schema(schema)
        assert converted == {
            "anyOf": [
                {"type": "string", "enum": ["A", "B"]},
                {"type": "null"},
            ]
        }

    def test_union_object_null_keeps_object_keys_only_on_object_branch(self):
        schema = {
            "type": ["object", "null"],
            "properties": {"A": {"type": "string"}},
            "required": ["A"],
            "additionalProperties": False,
        }
        converted = _to_anthropic_schema(schema)
        assert converted["anyOf"][0] == {
            "type": "object",
            "properties": {"A": {"type": "string"}},
            "required": ["A"],
            "additionalProperties": False,
        }
        assert converted["anyOf"][1] == {"type": "null"}

    def test_single_type_schema_is_unchanged(self):
        schema = {"type": "string", "enum": ["supported", "not_supported"]}
        assert _to_anthropic_schema(schema) == schema

    def test_nested_properties_are_converted_recursively(self):
        request = AnswerRequest(question="q", choices=CHOICES)
        citations = [Citation(documentId="law-test", contentUnitId=CITATION_IDS[0])]
        converted = _to_anthropic_schema(_answer_json_schema(request, citations))
        assert converted["properties"]["predictedAnswer"] == {
            "type": "string",
            "enum": sorted(CHOICES),
        }
        assessments = converted["properties"]["choiceAssessments"]
        object_branch = assessments
        assert object_branch["additionalProperties"] is False
        citation_schema = object_branch["properties"]["A"]["properties"]["citationIds"]["items"]
        assert citation_schema["enum"] == [CITATION_IDS[0]]
        assert "confidence" in object_branch["properties"]["A"]["required"]

    def test_no_choices_schema_converts(self):
        request = AnswerRequest(question="q")
        converted = _to_anthropic_schema(_answer_json_schema(request))
        assert converted["properties"]["predictedAnswer"] == {"type": "null"}
        assert converted["properties"]["choiceAssessments"] == {"type": "null"}

    def test_unsupported_constraint_keywords_are_stripped(self):
        schema = {
            "type": "array",
            "items": {
                "type": "number",
                "minLength": 1,
                "maxLength": 200,
                "minimum": 0,
                "maximum": 1,
            },
            "minItems": 1,
            "maxItems": 4,
        }
        converted = _to_anthropic_schema(schema)
        assert converted == {"type": "array", "items": {"type": "number"}}


def test_evidence_evaluation_rejects_choice_key_mismatch():
    raw = json.dumps(
        {
            "choiceCoverage": {"A": "sufficient"},
            "followUpQueries": [],
            "graphRequired": False,
            "stop": True,
        }
    )
    coverage, _, _, _, error = _parse_evidence_evaluation(raw, CHOICES, 1)
    assert coverage == {}
    assert error.startswith("evidence_evaluation_validation_error")
