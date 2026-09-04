import json

import pytest

from contract import (
    ANSWER_OPERATION,
    QUESTION_READINESS_OPERATION,
    AgentCoreContractError,
    encode_stream_event,
    extract_operation,
    extract_question,
    render_answer,
    render_citations,
    unwrap_payload,
)


def test_unwraps_agentcore_input_wrapper():
    assert unwrap_payload({"input": {"prompt": [{"text": "質問"}]}}) == {
        "prompt": [{"text": "質問"}]
    }


def test_operation_defaults_to_answer_and_accepts_question_readiness():
    assert extract_operation({}) == ANSWER_OPERATION
    assert (
        extract_operation({"operation": "question_readiness"})
        == QUESTION_READINESS_OPERATION
    )


def test_rejects_unknown_operation():
    with pytest.raises(AgentCoreContractError, match="unsupported operation"):
        extract_operation({"operation": "seed"})


def test_extracts_all_text_blocks_from_prompt():
    assert (
        extract_question(
            {
                "prompt": [
                    {"text": "公開買付けについて"},
                    {"document": {"name": "ignored.pdf"}},
                    {"text": "適用除外を確認して"},
                ]
            }
        )
        == "公開買付けについて\n適用除外を確認して"
    )


def test_falls_back_to_latest_user_message():
    assert (
        extract_question(
            {
                "messages": [
                    {"role": "user", "content": [{"text": "古い質問"}]},
                    {"role": "assistant", "content": [{"text": "回答"}]},
                    {"role": "user", "content": [{"text": "新しい質問"}]},
                ]
            }
        )
        == "新しい質問"
    )


def test_rejects_request_without_text():
    with pytest.raises(AgentCoreContractError, match="text block"):
        extract_question({"prompt": [{"document": {"name": "only.pdf"}}]})


def test_renders_answer_without_flattening_citations_into_text():
    rendered = render_answer(
        {
            "answer": "回答本文",
            "citations": [
                {
                    "documentId": "law-1",
                    "title": "金融商品取引法",
                    "heading": "第二十七条の二",
                    "sourcePage": 12,
                },
                {
                    "documentId": "law-1",
                    "title": "金融商品取引法",
                    "heading": "第二十七条の二",
                    "sourcePage": 12,
                },
            ],
        }
    )
    assert rendered == "回答本文"


def test_renders_structured_citations_with_text_and_content_unit_id():
    rendered = render_citations(
        {
            "citations": [
                {
                    "documentId": "law-1",
                    "contentUnitId": "law-1-article-27_2",
                    "title": "金融商品取引法",
                    "heading": "第二十七条の二",
                    "sourcePage": 12,
                    "text": "その条文の本文",
                    "ignored": "公開しない内部フィールド",
                },
                {"title": "documentIdがないため除外"},
            ]
        }
    )

    assert rendered == [
        {
            "documentId": "law-1",
            "contentUnitId": "law-1-article-27_2",
            "title": "金融商品取引法",
            "heading": "第二十七条の二",
            "sourcePage": 12,
            "text": "その条文の本文",
        }
    ]


def test_encodes_gen_u_strands_event_as_one_json_line():
    encoded = encode_stream_event({"messageStart": {"role": "assistant"}})
    assert encoded.endswith("\n")
    assert json.loads(encoded) == {
        "event": {"messageStart": {"role": "assistant"}}
    }
