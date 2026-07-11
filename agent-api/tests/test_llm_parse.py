import json

import pytest

from app.llm import _derive_predicted_answer, _parse_answer_payload

CHOICES = {"A": "選択肢A", "B": "選択肢B", "C": "選択肢C", "D": "選択肢D"}


def _payload(**overrides) -> str:
    payload = {
        "answer": "根拠説明",
        "predictedAnswer": "A",
        "choiceJudgements": {
            "A": "supported",
            "B": "not_supported",
            "C": "not_supported",
            "D": "not_supported",
        },
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


class TestParseAnswerPayload:
    def test_valid_payload(self):
        answer, predicted, judgements, error = _parse_answer_payload(_payload(), CHOICES)
        assert answer == "根拠説明"
        assert predicted == "A"
        assert judgements["A"] == "supported"
        assert error is None

    def test_json_parse_error_returns_raw_text(self):
        answer, predicted, judgements, error = _parse_answer_payload("答えはAです", CHOICES)
        assert answer == "答えはAです"
        assert predicted is None
        assert judgements is None
        assert error.startswith("json_parse_error")

    def test_predicted_null_with_judgements_is_accepted(self):
        raw = _payload(predictedAnswer=None, choiceJudgements={label: "not_supported" for label in CHOICES})
        answer, predicted, judgements, error = _parse_answer_payload(raw, CHOICES)
        assert error is None
        assert predicted is None
        assert judgements == {label: "not_supported" for label in CHOICES}

    def test_predicted_null_with_single_supported_is_derived(self):
        raw = _payload(predictedAnswer=None)
        _, predicted, _, error = _parse_answer_payload(raw, CHOICES)
        assert error is None
        assert predicted == "A"

    def test_predicted_out_of_labels_is_rejected(self):
        _, predicted, judgements, error = _parse_answer_payload(_payload(predictedAnswer="E"), CHOICES)
        assert predicted is None
        assert judgements is None
        assert error.startswith("validation_error")

    def test_predicted_without_judgements_is_rejected(self):
        _, predicted, _, error = _parse_answer_payload(_payload(choiceJudgements=None), CHOICES)
        assert predicted is None
        assert error.startswith("validation_error")

    def test_judgement_keys_mismatch_is_rejected(self):
        raw = _payload(choiceJudgements={"A": "supported"})
        _, predicted, _, error = _parse_answer_payload(raw, CHOICES)
        assert predicted is None
        assert error.startswith("validation_error")

    def test_invalid_judgement_value_is_rejected(self):
        judgements = {label: "not_supported" for label in CHOICES}
        judgements["A"] = "maybe"
        _, predicted, _, error = _parse_answer_payload(_payload(choiceJudgements=judgements), CHOICES)
        assert predicted is None
        assert error.startswith("validation_error")

    def test_no_choices_requires_null_fields(self):
        raw = _payload(predictedAnswer=None, choiceJudgements=None)
        answer, predicted, judgements, error = _parse_answer_payload(raw, None)
        assert error is None
        assert predicted is None
        assert judgements is None

    def test_no_choices_with_judgements_is_rejected(self):
        _, predicted, _, error = _parse_answer_payload(_payload(), None)
        assert predicted is None
        assert error.startswith("validation_error")

    def test_validation_error_keeps_answer_field(self):
        answer, _, _, error = _parse_answer_payload(_payload(predictedAnswer="E"), CHOICES)
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
