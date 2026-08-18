from types import SimpleNamespace

from app.llm import (
    LLMClient,
    _parse_grounding_review,
    _post_anthropic_with_overload_retry,
    build_grounding_review_prompt,
)
from app.models import AnswerRequest, Citation


def test_ollama_transport_applies_classifier_context_and_disables_thinking(
    monkeypatch,
) -> None:
    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "response": '{"ok":true}',
                "prompt_eval_count": 10,
                "eval_count": 5,
                "done_reason": "stop",
            }

    def fake_post(url, *, json, timeout):
        captured.update({"url": url, "payload": json, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr("app.llm.requests.post", fake_post)
    client = LLMClient(
        provider="ollama",
        ollama_num_ctx=32768,
        ollama_think=False,
    )

    client._ollama_json(
        "prompt",
        {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        "gemma4:e4b",
        30,
    )

    assert captured["payload"]["options"]["num_ctx"] == 32768
    assert captured["payload"]["think"] is False
    assert captured["payload"]["model"] == "gemma4:e4b"


def test_anthropic_529_is_retried_with_same_payload(monkeypatch) -> None:
    responses = [
        SimpleNamespace(status_code=529),
        SimpleNamespace(status_code=200),
    ]
    calls: list[dict] = []

    def fake_post(url, *, headers, json, timeout):
        calls.append(
            {
                "url": url,
                "headers": headers,
                "json": json,
                "timeout": timeout,
            }
        )
        return responses.pop(0)

    monkeypatch.setattr("app.llm.requests.post", fake_post)
    monkeypatch.setattr("app.llm.sleep", lambda _seconds: None)
    payload = {"model": "test", "messages": []}

    response = _post_anthropic_with_overload_retry(
        payload=payload,
        timeout_sec=10,
    )

    assert response.status_code == 200
    assert len(calls) == 2
    assert calls[0]["json"] == payload
    assert calls[1]["json"] == payload


def test_haiku_json_transport_omits_unsupported_effort(monkeypatch) -> None:
    captured: dict = {}

    def fake_post(*, payload, timeout_sec):
        captured["payload"] = payload
        captured["timeoutSec"] = timeout_sec
        return SimpleNamespace(
            ok=True,
            json=lambda: {
                "content": [{"type": "text", "text": '{"ok":true}'}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "stop_reason": "end_turn",
            },
        )

    monkeypatch.setattr("app.llm.settings.anthropic_api_key", "test-key")
    monkeypatch.setattr("app.llm._post_anthropic_with_overload_retry", fake_post)

    LLMClient()._anthropic_json(
        "test",
        {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        "claude-haiku-4-5-20251001",
        128,
        10,
        effort="low",
    )

    assert captured["payload"]["output_config"] == {
        "format": {
            "type": "json_schema",
            "schema": {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
        }
    }


def test_sonnet_json_transport_keeps_supported_effort(monkeypatch) -> None:
    captured: dict = {}

    def fake_post(*, payload, timeout_sec):
        captured["payload"] = payload
        return SimpleNamespace(
            ok=True,
            json=lambda: {
                "content": [{"type": "text", "text": '{"ok":true}'}],
                "usage": {},
                "stop_reason": "end_turn",
            },
        )

    monkeypatch.setattr("app.llm.settings.anthropic_api_key", "test-key")
    monkeypatch.setattr("app.llm._post_anthropic_with_overload_retry", fake_post)

    LLMClient()._anthropic_json(
        "test",
        {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "claude-sonnet-5",
        128,
        10,
        effort="medium",
    )

    assert captured["payload"]["output_config"]["effort"] == "medium"


def test_anthropic_health_checks_models_and_reports_haiku_effective_effort(
    monkeypatch,
) -> None:
    requested_urls: list[str] = []

    def fake_get(url, *, headers, timeout):
        requested_urls.append(url)
        assert headers["x-api-key"] == "test-key"
        assert timeout == 5
        return SimpleNamespace(ok=True)

    monkeypatch.setattr("app.llm.settings.anthropic_api_key", "test-key")
    monkeypatch.setattr("app.llm.settings.answer_model", "claude-haiku-test")
    monkeypatch.setattr("app.llm.settings.reviewer_model", "claude-haiku-test")
    monkeypatch.setattr("app.llm.settings.planner_model", "claude-haiku-test")
    monkeypatch.setattr("app.llm.settings.evaluator_model", "claude-haiku-test")
    monkeypatch.setattr("app.llm.settings.llm_research_model", "claude-haiku-test")
    monkeypatch.setattr(
        "app.llm.settings.llm_research_stage_model", "claude-haiku-test"
    )
    monkeypatch.setattr(
        "app.llm.settings.llm_research_integration_model", "claude-haiku-test"
    )
    monkeypatch.setattr(
        "app.llm.settings.relation_classifier_model", "claude-haiku-test"
    )
    monkeypatch.setattr(
        "app.llm.settings.relation_classifier_reviewer_model",
        "claude-haiku-test",
    )
    monkeypatch.setattr("app.llm.requests.get", fake_get)

    health = LLMClient()._anthropic_health()

    assert health["ok"] is True
    assert requested_urls == ["https://api.anthropic.com/v1/models/claude-haiku-test"]
    assert health["modelChecks"] == [
        {
            "model": "claude-haiku-test",
            "available": True,
            "supportsEffort": False,
        }
    ]
    assert health["researchEffort"]["stageEffective"] is None
    assert health["researchEffort"]["integrationEffective"] is None


def test_anthropic_health_does_not_check_local_classifier_models(
    monkeypatch,
) -> None:
    requested_urls: list[str] = []

    def fake_get(url, *, headers, timeout):
        requested_urls.append(url)
        return SimpleNamespace(ok=True)

    monkeypatch.setattr("app.llm.settings.anthropic_api_key", "test-key")
    for setting_name in (
        "answer_model",
        "reviewer_model",
        "planner_model",
        "evaluator_model",
        "llm_research_stage_model",
        "llm_research_integration_model",
    ):
        monkeypatch.setattr(
            f"app.llm.settings.{setting_name}", "claude-main-test"
        )
    monkeypatch.setattr(
        "app.llm.settings.relation_classifier_provider", "ollama"
    )
    monkeypatch.setattr(
        "app.llm.settings.relation_classifier_model", "gemma4:e4b"
    )
    monkeypatch.setattr(
        "app.llm.settings.relation_classifier_reviewer_model", "gemma4:e4b"
    )
    monkeypatch.setattr("app.llm.requests.get", fake_get)

    health = LLMClient(provider="anthropic")._anthropic_health()

    assert health["ok"] is True
    assert requested_urls == [
        "https://api.anthropic.com/v1/models/claude-main-test"
    ]
    assert health["relationClassifierProvider"] == "ollama"


def test_anthropic_health_rejects_unavailable_model_without_raw_error(
    monkeypatch,
) -> None:
    monkeypatch.setattr("app.llm.settings.anthropic_api_key", "secret-key")
    monkeypatch.setattr("app.llm.settings.answer_model", "missing-model")
    monkeypatch.setattr("app.llm.settings.reviewer_model", "missing-model")
    monkeypatch.setattr("app.llm.settings.planner_model", "missing-model")
    monkeypatch.setattr("app.llm.settings.evaluator_model", "missing-model")
    monkeypatch.setattr("app.llm.settings.llm_research_model", "missing-model")
    monkeypatch.setattr(
        "app.llm.settings.llm_research_stage_model", "missing-model"
    )
    monkeypatch.setattr(
        "app.llm.settings.llm_research_integration_model", "missing-model"
    )
    monkeypatch.setattr(
        "app.llm.requests.get",
        lambda *args, **kwargs: SimpleNamespace(ok=False),
    )

    health = LLMClient()._anthropic_health()

    assert health["ok"] is False
    assert health["reasonCode"] == "anthropic_model_unavailable"
    assert "secret-key" not in str(health)


def test_grounding_review_returns_critique_without_rewriting_answer(
    monkeypatch,
) -> None:
    monkeypatch.setattr("app.llm.settings.llm_provider", "anthropic")
    monkeypatch.setattr("app.llm.settings.answer_model", "claude-haiku-test")
    monkeypatch.setattr("app.llm.settings.reviewer_model", "claude-reviewer-test")
    client = LLMClient()
    captured: dict[str, object] = {}
    citation = Citation(
        documentId="law-1",
        contentUnitId="law-1-article-2",
        text="ただし、一定の場合を除く。",
    )
    monkeypatch.setattr(
        client,
        "_json_transport",
        lambda *args, **kwargs: (
            captured.update({"model": args[2]})
            or (
                (
                    '{"verdict":"needs_revision",'
                    '"issues":["例外の向きを修正"],'
                    '"researchQueries":[]}'
                ),
                12,
                20,
                10,
                "end_turn",
            )
        ),
    )

    result = client.review_answer_grounding(
        AnswerRequest(question="適用対象ですか"),
        "例外でも常に適用されます。",
        [citation],
        timeout_sec=10,
    )

    assert result.verdict == "needs_revision"
    assert result.validationError is None
    assert result.issues == ["例外の向きを修正"]
    assert captured["model"] == "claude-reviewer-test"
    assert result.model == "claude-reviewer-test"


def test_grounding_review_retries_with_larger_budget_after_truncation(
    monkeypatch,
) -> None:
    monkeypatch.setattr("app.llm.settings.llm_provider", "anthropic")
    monkeypatch.setattr("app.llm.settings.reviewer_model", "claude-reviewer-test")
    monkeypatch.setattr("app.llm.settings.reviewer_max_tokens", 4096)
    monkeypatch.setattr("app.llm.settings.anthropic_max_tokens_ceiling", 16384)
    client = LLMClient()
    calls: list[tuple[int, int]] = []
    responses = [
        ("", 12, 100, 4096, "max_tokens"),
        (
            '{"verdict":"supported","issues":[],"researchQueries":[]}',
            15,
            110,
            20,
            "end_turn",
        ),
    ]

    def fake_transport(prompt, schema, model, max_tokens, timeout_sec, **kwargs):
        calls.append((max_tokens, timeout_sec))
        return responses.pop(0)

    monkeypatch.setattr(client, "_json_transport", fake_transport)

    result = client.review_answer_grounding(
        AnswerRequest(question="適用対象ですか"),
        "引用本文の範囲だけを回答します。",
        [Citation(documentId="law-1", contentUnitId="law-1-article-2")],
        timeout_sec=60,
    )

    assert [max_tokens for max_tokens, _ in calls] == [4096, 8192]
    assert result.verdict == "supported"
    assert result.validationError is None
    assert result.retryCount == 1
    assert result.latencyMs == 27
    assert result.inputTokens == 210
    assert result.outputTokens == 4116


def test_grounding_review_retries_inconsistent_action_contract(
    monkeypatch,
) -> None:
    monkeypatch.setattr("app.llm.settings.llm_provider", "anthropic")
    monkeypatch.setattr("app.llm.settings.reviewer_model", "claude-reviewer-test")
    client = LLMClient()
    prompts: list[str] = []
    responses = [
        (
            '{"verdict":"needs_revision","issues":["本文不足"],'
            '"researchQueries":["不足条文"]}',
            10,
            100,
            20,
            "end_turn",
        ),
        (
            '{"verdict":"needs_research","issues":["本文不足"],'
            '"researchQueries":["不足条文"]}',
            12,
            110,
            24,
            "end_turn",
        ),
    ]

    def fake_transport(prompt, schema, model, max_tokens, timeout_sec, **kwargs):
        prompts.append(prompt)
        return responses.pop(0)

    monkeypatch.setattr(client, "_json_transport", fake_transport)

    result = client.review_answer_grounding(
        AnswerRequest(question="適用要件は何ですか"),
        "確認できた範囲を回答します。",
        [Citation(documentId="law-1", contentUnitId="law-1-article-2")],
        timeout_sec=60,
    )

    assert result.verdict == "needs_research"
    assert result.researchQueries == ["不足条文"]
    assert result.validationError is None
    assert result.retryCount == 1
    assert "grounding_review_unexpected_research_queries" in prompts[1]
    assert "法的判断や次動作を推測して補正しません" in prompts[1]


def test_grounding_review_requires_findings_when_revision_is_needed() -> None:
    verdict, issues, findings, research_queries, error = _parse_grounding_review(
        '{"verdict":"needs_revision","issues":[],"researchQueries":[]}'
    )

    assert verdict == "insufficient"
    assert issues == []
    assert findings == []
    assert research_queries == []
    assert error == "grounding_review_missing_findings"


def test_grounding_review_requires_queries_for_more_research() -> None:
    verdict, issues, findings, research_queries, error = _parse_grounding_review(
        '{"verdict":"needs_research","issues":["条文本文が不足"],'
        '"researchQueries":[]}'
    )

    assert verdict == "insufficient"
    assert issues == ["条文本文が不足"]
    assert findings == [
        {"issueId": "overall", "description": "条文本文が不足"}
    ]
    assert research_queries == []
    assert error == "grounding_review_missing_research_queries"


def test_grounding_review_prompt_warns_about_exception_inversion() -> None:
    prompt = build_grounding_review_prompt(
        AnswerRequest(question="対象ですか"),
        "対象です。",
        [Citation(documentId="law-1", contentUnitId="law-1-article-2")],
    )

    assert "ただし" in prompt
    assert "除く" in prompt
    assert "逆転させない" in prompt
    assert "委任元・参照先" in prompt
    assert "別の条項" in prompt
    assert "公開買付義務" not in prompt
    assert "truncatedContentUnitIds" in prompt
    assert "partial" in prompt
    assert "完全回答でない" in prompt
    assert "回答を書き換え" in prompt
    assert "needs_research" in prompt
    assert "researchQueries" in prompt
    assert "同じ材料で書き直しても解決しない" in prompt
    assert "質問が明示して求める事項を独立に分け" in prompt
    assert "一つを中心事項として他を周辺扱いしてはいけません" in prompt
    assert "supported以外でfindingsを空にしてはいけません" in prompt
    assert "insufficientを単なる「回答がpartialである」の意味に使ってはいけません" in prompt
    assert "思考過程や回答の再掲は出力せず" in prompt


def test_grounding_review_prompt_excludes_projection_candidates() -> None:
    prompt = build_grounding_review_prompt(
        AnswerRequest(question="対象ですか"),
        "対象です。",
        [Citation(documentId="law-1", contentUnitId="law-1-article-2")],
        research_context={
            "status": "ready",
            "incomplete": False,
            "missingEvidence": [],
            "selectedEvidence": ["unselected-projection-candidate"],
            "logicalStructure": {"issues": ["projection-only"]},
        },
    )

    assert "unselected-projection-candidate" not in prompt
    assert "projection-only" not in prompt
    assert "共有回答契約" in prompt
    assert "最大3件" in prompt


def test_grounding_review_uses_same_issue_ids_and_separates_unselected_candidates() -> None:
    prompt = build_grounding_review_prompt(
        AnswerRequest(question="要件と手続を説明してください"),
        "要件だけを回答します。",
        [
            Citation(
                documentId="law-1",
                contentUnitId="law-1-article-1",
                text="選択済み本文",
            )
        ],
        research_context={
            "answerContract": {
                "version": "issue-grounding-v1",
                "issues": [
                    {"issueId": "ISSUE-1", "question": "要件は何か"},
                    {"issueId": "ISSUE-2", "question": "手続は何か"},
                ],
            }
        },
        citation_ids=["law-1-article-1"],
        issue_decisions=[
            {
                "issueId": "ISSUE-1",
                "status": "ready",
                "conclusion": "要件を確認",
                "citationIds": ["law-1-article-1"],
                "missing": [],
            }
        ],
        available_citations=[
            Citation(
                documentId="law-1",
                contentUnitId="law-1-article-1",
                text="選択済み本文",
            ),
            Citation(
                documentId="law-1",
                contentUnitId="law-1-article-2",
                text="未選択の手続本文",
            ),
        ],
    )

    assert '"issueId": "ISSUE-2"' in prompt
    assert "未選択の利用可能引用候補" in prompt
    assert "law-1-article-2" in prompt
    assert "利用可能候補全体に無" in prompt
