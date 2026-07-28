"""回答生成のリトライ挙動。

実測（2026-07-25, lawqa_jp 金商法_第3章_問題番号51）で、応答がthinkingブロックだけで
出力上限に達し、textブロックが返らずJSONパースに失敗する事象を確認した。
同じ上限で投げ直しても同じ結果になるため、上限到達時は枠を広げて再試行する。
"""

from dataclasses import replace

import pytest

from app.llm import LLMClient, LLMResult
from app.models import AnswerRequest


def _result(**overrides) -> LLMResult:
    base = LLMResult(
        text="回答",
        provider="anthropic",
        model="claude-sonnet-5",
        latencyMs=1000,
        inputTokens=100,
        outputTokens=200,
        estimatedCost=0,
        answer="回答",
        predictedAnswer=None,
        choiceJudgements=None,
    )
    return replace(base, **overrides)


def _request() -> AnswerRequest:
    return AnswerRequest(question="外務員の権限は何ですか。", pattern="pattern_4_deepsearch", topK=5)


@pytest.fixture
def client(monkeypatch) -> LLMClient:
    monkeypatch.setattr("app.llm.build_answer_prompt", lambda *args, **kwargs: "prompt")
    return LLMClient()


def test_retries_with_a_larger_budget_when_output_hit_the_token_cap(client, monkeypatch):
    calls: list[int] = []

    def fake_generate_once(request, prompt, timeout, citations, max_tokens):
        calls.append(max_tokens)
        if len(calls) == 1:
            return _result(
                text="",
                validationError="json_parse_error: Expecting value: line 1 column 1 (char 0)",
                stopReason="max_tokens",
                outputChars=0,
            )
        return _result(predictedAnswer="D")

    monkeypatch.setattr(client, "_generate_once", fake_generate_once)

    result = client.generate_answer(_request(), [], [], timeout_sec=120)

    assert len(calls) == 2
    assert calls[1] > calls[0], "上限到達時は同じ枠で投げ直さない"
    assert result.predictedAnswer == "D"
    assert result.retryCount == 1


def test_keeps_the_same_budget_for_other_validation_errors(client, monkeypatch):
    calls: list[int] = []

    def fake_generate_once(request, prompt, timeout, citations, max_tokens):
        calls.append(max_tokens)
        if len(calls) == 1:
            return _result(validationError="schema_error", stopReason="end_turn")
        return _result(predictedAnswer="A")

    monkeypatch.setattr(client, "_generate_once", fake_generate_once)

    client.generate_answer(_request(), [], [], timeout_sec=120)

    assert calls == [calls[0], calls[0]]


def test_falls_back_to_the_first_result_when_the_retry_fails(client, monkeypatch):
    """リトライがタイムアウトしても、1回目の判定結果は捨てない。"""

    def fake_generate_once(request, prompt, timeout, citations, max_tokens):
        if timeout == 120:
            return _result(predictedAnswer="B", validationError="json_parse_error", stopReason="max_tokens")
        raise TimeoutError("Read timed out")

    monkeypatch.setattr(client, "_generate_once", fake_generate_once)

    result = client.generate_answer(_request(), [], [], timeout_sec=120)

    assert result is not None
    assert result.predictedAnswer == "B"


def test_skips_the_retry_when_too_little_time_remains(client, monkeypatch):
    calls: list[int] = []

    def fake_generate_once(request, prompt, timeout, citations, max_tokens):
        calls.append(timeout)
        return _result(validationError="json_parse_error", stopReason="max_tokens")

    monkeypatch.setattr(client, "_generate_once", fake_generate_once)

    client.generate_answer(_request(), [], [], timeout_sec=1)

    assert len(calls) == 1
