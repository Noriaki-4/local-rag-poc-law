"""GenUとLegal Agentの間だけで使うAgentCore wire contract。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


class AgentCoreContractError(ValueError):
    """GenUからのinvoke payloadをLegal Agentへ変換できない。"""


def unwrap_payload(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise AgentCoreContractError("request body must be a JSON object")
    wrapped = payload.get("input")
    if isinstance(wrapped, Mapping):
        return wrapped
    return payload


def extract_question(payload: Mapping[str, Any]) -> str:
    """GenUのpromptを優先し、なければ最後のuser messageから質問を得る。"""

    question = _text_from_content(payload.get("prompt"))
    if not question:
        messages = payload.get("messages")
        if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)):
            for message in reversed(messages):
                if not isinstance(message, Mapping) or message.get("role") != "user":
                    continue
                question = _text_from_content(message.get("content"))
                if question:
                    break
    if not question:
        raise AgentCoreContractError("prompt must include at least one text block")
    return question


def render_answer(response: Mapping[str, Any]) -> str:
    """現行AnswerResponseをGenUのチャット本文へ損失少なく投影する。"""

    answer = response.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise AgentCoreContractError("Legal Agent response does not contain answer text")

    citation_lines: list[str] = []
    citations = response.get("citations")
    if isinstance(citations, Sequence) and not isinstance(citations, (str, bytes)):
        for citation in citations:
            if not isinstance(citation, Mapping):
                continue
            label = _citation_label(citation)
            if label and label not in citation_lines:
                citation_lines.append(label)

    rendered = answer.strip()
    if citation_lines:
        rendered += "\n\n参照:\n" + "\n".join(f"- {line}" for line in citation_lines)
    return rendered


def encode_stream_event(event: Mapping[str, Any]) -> str:
    """GenUのStrandsStreamProcessorが読む1イベント1行のJSONを返す。"""

    return json.dumps({"event": event}, ensure_ascii=False, separators=(",", ":")) + "\n"


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, Mapping):
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts)


def _citation_label(citation: Mapping[str, Any]) -> str:
    title = citation.get("title") or citation.get("documentId")
    if not isinstance(title, str) or not title.strip():
        return ""
    parts = [title.strip()]
    heading = citation.get("heading")
    if isinstance(heading, str) and heading.strip():
        parts.append(heading.strip())
    source_page = citation.get("sourcePage")
    if isinstance(source_page, int):
        parts.append(f"p.{source_page}")
    return " / ".join(parts)
