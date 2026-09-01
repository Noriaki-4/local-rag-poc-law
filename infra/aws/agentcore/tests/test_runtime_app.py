import asyncio
import json

import runtime_app


def test_ping_does_not_initialize_data_clients():
    assert asyncio.run(runtime_app.ping()) == {"status": "Healthy"}


def test_invocation_streams_strands_events_for_genu(monkeypatch):
    monkeypatch.setattr(
        runtime_app,
        "_invoke_legal_agent",
        lambda question: {
            "answer": f"{question}への回答",
            "citations": [{"documentId": "law-1", "title": "会社法"}],
        },
    )

    async def collect_events():
        return [
            json.loads(line)["event"]
            async for line in runtime_app._stream_legal_answer("質問", "session-1")
        ]

    events = asyncio.run(collect_events())

    assert events[0] == {"messageStart": {"role": "assistant"}}
    text_deltas = [
        event["contentBlockDelta"]["delta"]["text"]
        for event in events
        if "contentBlockDelta" in event
        and "text" in event["contentBlockDelta"]["delta"]
    ]
    assert text_deltas == ["質問への回答\n\n参照:\n- 会社法"]
    assert events[-1] == {"messageStop": {"stopReason": "end_turn"}}


def test_invocation_failure_is_returned_as_a_safe_stream_event(monkeypatch):
    def fail(_question):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(runtime_app, "_invoke_legal_agent", fail)

    async def collect_events():
        return [
            json.loads(line)["event"]
            async for line in runtime_app._stream_legal_answer("質問", "session-1")
        ]

    events = asyncio.run(collect_events())
    errors = [
        event["internalServerException"]
        for event in events
        if "internalServerException" in event
    ]
    assert errors == [{"message": "法令検索バックエンドで回答を生成できませんでした。"}]
    assert "secret detail" not in json.dumps(events, ensure_ascii=False)


def test_question_readiness_streams_structured_result(monkeypatch):
    monkeypatch.setattr(
        runtime_app,
        "_invoke_question_readiness",
        lambda question: {
            "decision": "clarification_required",
            "reason": "主体を確認します。",
            "clarification_question": "誰が行いますか。",
            "choices": [
                {
                    "choice_id": "company",
                    "label": "会社",
                    "refined_question": f"会社が行う場合の{question}",
                }
            ],
        },
    )

    async def collect_events():
        return [
            json.loads(line)["event"]
            async for line in runtime_app._stream_question_readiness(
                "要件は何ですか。", "session-1"
            )
        ]

    events = asyncio.run(collect_events())
    text = "".join(
        event["contentBlockDelta"]["delta"]["text"]
        for event in events
        if "contentBlockDelta" in event
        and "text" in event["contentBlockDelta"]["delta"]
    )
    assert json.loads(text) == {
        "decision": "clarification_required",
        "reason": "主体を確認します。",
        "clarification_question": "誰が行いますか。",
        "choices": [
            {
                "choice_id": "company",
                "label": "会社",
                "refined_question": "会社が行う場合の要件は何ですか。",
            }
        ],
    }
    assert events[-1] == {"messageStop": {"stopReason": "end_turn"}}
