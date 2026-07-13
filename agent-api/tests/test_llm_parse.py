import json

import pytest

from app.llm import (
    _answer_json_schema,
    _derive_predicted_answer,
    _format_citations_with_budget,
    _parse_answer_payload,
    _parse_evidence_evaluation,
    _parse_search_plan,
    _sum_optional,
    _to_anthropic_schema,
)
from app.models import AnswerRequest, Citation

CHOICES = {"A": "選択肢A", "B": "選択肢B", "C": "選択肢C", "D": "選択肢D"}
CITATION_IDS = ["law-test-article-1", "law-test-article-2"]


def _payload(**overrides) -> str:
    payload = {
        "answer": "根拠説明",
        "questionPolarity": "select_entailed",
        "predictedAnswer": "A",
        "choiceAssessments": {
            "A": {"verdict": "entailed", "citationIds": [CITATION_IDS[0]], "reason": "条文に合致"},
            "B": {"verdict": "contradicted", "citationIds": [CITATION_IDS[0]], "reason": "条文と矛盾"},
            "C": {"verdict": "contradicted", "citationIds": [CITATION_IDS[1]], "reason": "条文と矛盾"},
            "D": {"verdict": "insufficient", "citationIds": [], "reason": "根拠不足"},
        },
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


class TestParseAnswerPayload:
    def test_valid_payload(self):
        answer, predicted, judgements, assessments, polarity, error = _parse_answer_payload(
            _payload(), CHOICES, CITATION_IDS
        )
        assert answer == "根拠説明"
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

    def test_predicted_out_of_labels_is_rejected(self):
        _, predicted, judgements, _, _, error = _parse_answer_payload(
            _payload(predictedAnswer="E"), CHOICES, CITATION_IDS
        )
        assert predicted is None
        assert judgements is None
        assert error.startswith("validation_error")

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

    def test_assessment_citations_are_deduplicated_and_capped_after_validation(self):
        ids = [f"law-test-article-{index}" for index in range(1, 6)]
        raw_payload = json.loads(_payload())
        raw_payload["choiceAssessments"]["A"]["citationIds"] = [*ids, ids[0]]
        _, _, _, assessments, _, error = _parse_answer_payload(
            json.dumps(raw_payload), CHOICES, ids
        )
        assert error is None
        assert assessments["A"]["citationIds"] == ids[:3]

    def test_prediction_must_match_question_polarity(self):
        _, predicted, _, _, _, error = _parse_answer_payload(
            _payload(predictedAnswer="B"), CHOICES, CITATION_IDS
        )
        assert predicted is None
        assert "verdict entailed" in error

    def test_no_choices_requires_null_fields(self):
        raw = _payload(questionPolarity=None, predictedAnswer=None, choiceAssessments=None)
        answer, predicted, judgements, assessments, polarity, error = _parse_answer_payload(raw, None)
        assert error is None
        assert predicted is None
        assert judgements is None
        assert assessments is None
        assert polarity is None

    def test_no_choices_with_assessments_is_rejected(self):
        _, predicted, _, _, _, error = _parse_answer_payload(_payload(), None)
        assert predicted is None
        assert error.startswith("validation_error")

    def test_validation_error_keeps_answer_field(self):
        answer, _, _, _, _, error = _parse_answer_payload(_payload(predictedAnswer="E"), CHOICES, CITATION_IDS)
        assert answer == "根拠説明"
        assert error is not None


class TestDerivePredictedAnswer:
    def test_keeps_explicit_prediction(self):
        judgements = {"A": "not_supported", "B": "supported"}
        assert _derive_predicted_answer("A", judgements) == "A"

    @pytest.mark.parametrize(
        "judgements,expected",
        [
            ({"A": "supported", "B": "not_supported"}, "A"),
            ({"A": "not_supported", "B": "not_supported"}, None),
            ({"A": "supported", "B": "supported"}, None),
            (None, None),
        ],
    )
    def test_derives_only_when_single_supported(self, judgements, expected):
        assert _derive_predicted_answer(None, judgements) == expected


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
        predicted = converted["properties"]["predictedAnswer"]
        assert "anyOf" in predicted
        assert {"type": "null"} in predicted["anyOf"]
        string_branch = next(b for b in predicted["anyOf"] if b.get("type") == "string")
        assert string_branch["enum"] == ["A", "B", "C", "D"]
        assessments = converted["properties"]["choiceAssessments"]
        assert {"type": "null"} in assessments["anyOf"]
        object_branch = next(b for b in assessments["anyOf"] if b.get("type") == "object")
        assert object_branch["additionalProperties"] is False
        citation_schema = object_branch["properties"]["A"]["properties"]["citationIds"]["items"]
        assert citation_schema["enum"] == [CITATION_IDS[0]]

    def test_no_choices_schema_converts(self):
        request = AnswerRequest(question="q")
        converted = _to_anthropic_schema(_answer_json_schema(request))
        assert converted["properties"]["predictedAnswer"] == {"type": "null"}
        assert converted["properties"]["choiceAssessments"] == {"type": "null"}

    def test_unsupported_constraint_keywords_are_stripped(self):
        schema = {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 200},
            "minItems": 1,
            "maxItems": 4,
        }
        converted = _to_anthropic_schema(schema)
        assert converted == {"type": "array", "items": {"type": "string"}}


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
