import json
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError
import requests

from .config import settings
from .models import AnswerRequest, Citation


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    latencyMs: int
    inputTokens: int | None
    outputTokens: int | None
    estimatedCost: int
    answer: str
    predictedAnswer: str | None
    choiceJudgements: dict[str, str] | None
    validationError: str | None = None


class LLMAnswerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    predictedAnswer: str | None = None
    choiceJudgements: dict[str, Literal["supported", "not_supported"]] | None = None


class LLMClient:
    def __init__(self) -> None:
        self.provider = settings.llm_provider.lower()

    def health(self) -> dict[str, Any]:
        if self.provider == "anthropic":
            return self._anthropic_health()
        if self.provider != "ollama":
            return {"provider": self.provider, "ok": False, "reason": "Unsupported LLM_PROVIDER"}
        return self._ollama_health()

    def _ollama_health(self) -> dict[str, Any]:
        try:
            response = requests.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags", timeout=3)
            response.raise_for_status()
            models = [model["name"] for model in response.json().get("models", [])]
            return {
                "provider": "ollama",
                "ok": settings.answer_model in models,
                "baseUrl": settings.ollama_base_url,
                "answerModel": settings.answer_model,
                "availableModels": models,
            }
        except Exception as exc:
            return {
                "provider": "ollama",
                "ok": False,
                "baseUrl": settings.ollama_base_url,
                "answerModel": settings.answer_model,
                "reason": str(exc),
            }

    def _anthropic_health(self) -> dict[str, Any]:
        if not settings.anthropic_api_key:
            return {
                "provider": "anthropic",
                "ok": False,
                "baseUrl": settings.anthropic_base_url,
                "answerModel": settings.answer_model,
                "reason": "ANTHROPIC_API_KEY is not set",
            }
        return {
            "provider": "anthropic",
            "ok": True,
            "baseUrl": settings.anthropic_base_url,
            "answerModel": settings.answer_model,
        }

    def generate_answer(
        self,
        request: AnswerRequest,
        route: list[str],
        citations: list[Citation],
    ) -> LLMResult | None:
        prompt = build_answer_prompt(request, route, citations)
        if self.provider == "ollama":
            return self._generate_ollama(request, prompt)
        if self.provider == "anthropic":
            return self._generate_anthropic(request, prompt)
        return None

    def _generate_ollama(self, request: AnswerRequest, prompt: str) -> LLMResult:
        started = perf_counter()
        response = requests.post(
            f"{settings.ollama_base_url.rstrip('/')}/api/generate",
            json={
                "model": settings.answer_model,
                "prompt": prompt,
                "format": _answer_json_schema(request),
                "stream": False,
                "options": {
                    "temperature": 0,
                    "top_p": 1,
                },
            },
            timeout=settings.llm_timeout_sec,
        )
        response.raise_for_status()
        data = response.json()
        raw_text = str(data.get("response", "")).strip()
        answer, predicted_answer, choice_judgements, validation_error = _parse_answer_payload(raw_text, request.choices)
        return LLMResult(
            text=answer,
            provider="ollama",
            model=settings.answer_model,
            latencyMs=int((perf_counter() - started) * 1000),
            inputTokens=data.get("prompt_eval_count"),
            outputTokens=data.get("eval_count"),
            estimatedCost=0,
            answer=answer,
            predictedAnswer=predicted_answer,
            choiceJudgements=choice_judgements,
            validationError=validation_error,
        )

    def _generate_anthropic(self, request: AnswerRequest, prompt: str) -> LLMResult:
        if not settings.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")

        started = perf_counter()
        response = requests.post(
            f"{settings.anthropic_base_url.rstrip('/')}/v1/messages",
            headers={
                "content-type": "application/json",
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": settings.anthropic_version,
            },
            json={
                "model": settings.answer_model,
                "max_tokens": settings.anthropic_max_tokens,
                "messages": [{"role": "user", "content": prompt}],
                "output_config": {
                    "format": {
                        "type": "json_schema",
                        "schema": _answer_json_schema(request),
                    }
                },
            },
            timeout=settings.llm_timeout_sec,
        )
        response.raise_for_status()
        data = response.json()
        raw_text = _anthropic_text(data)
        answer, predicted_answer, choice_judgements, validation_error = _parse_answer_payload(raw_text, request.choices)
        usage = data.get("usage", {})
        return LLMResult(
            text=answer,
            provider="anthropic",
            model=settings.answer_model,
            latencyMs=int((perf_counter() - started) * 1000),
            inputTokens=usage.get("input_tokens"),
            outputTokens=usage.get("output_tokens"),
            estimatedCost=0,
            answer=answer,
            predictedAnswer=predicted_answer,
            choiceJudgements=choice_judgements,
            validationError=validation_error,
        )


def build_answer_prompt(
    request: AnswerRequest,
    route: list[str],
    citations: list[Citation],
) -> str:
    citation_block = "\n\n".join(_format_citation(index, citation) for index, citation in enumerate(citations, start=1))
    if not citation_block:
        citation_block = "引用候補なし"
    citation_block = citation_block[: settings.llm_max_context_chars]

    choices_block = ""
    if request.choices:
        choices_block = "\n選択肢:\n" + "\n".join(
            f"{label.upper()}: {text}" for label, text in sorted(request.choices.items())
        )

    return f"""あなたはローカル検証環境の法務RAG回答生成器です。
外部APIや外部検索は使わず、下の引用候補だけを根拠に日本語で簡潔に回答してください。
法的判断を断定しすぎず、必要に応じて専門家確認が必要であることが伝わる表現にしてください。
引用する場合は contentUnitId を文中に含めてください。
必ずJSONだけを返してください。JSON以外の説明文やMarkdownコードフェンスは不要です。
answer には正解ラベルだけでなく、引用候補に基づく短い根拠説明を含めてください。
選択肢がある場合、predictedAnswer は選択肢ラベルまたは null、choiceJudgements は各選択肢に supported または not_supported を設定してください。
引用候補だけでは判断できない場合、predictedAnswer と choiceJudgements は null にしてください。

検索ルート: {" -> ".join(route)}
質問: {request.question}{choices_block}

引用候補:
{citation_block}

JSON:"""


def _answer_json_schema(request: AnswerRequest) -> dict[str, Any]:
    labels = sorted(label.upper() for label in (request.choices or {}))
    judgement_properties = {
        label: {"type": "string", "enum": ["supported", "not_supported"]}
        for label in labels
    }
    predicted_schema: dict[str, Any]
    judgements_schema: dict[str, Any]
    if labels:
        predicted_schema = {"type": ["string", "null"], "enum": [*labels, None]}
        judgements_schema = {
            "type": ["object", "null"],
            "properties": judgement_properties,
            "required": labels,
            "additionalProperties": False,
        }
    else:
        predicted_schema = {"type": "null"}
        judgements_schema = {"type": "null"}
    return {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "predictedAnswer": predicted_schema,
            "choiceJudgements": judgements_schema,
        },
        "required": ["answer", "predictedAnswer", "choiceJudgements"],
        "additionalProperties": False,
    }


def _parse_answer_payload(
    raw_text: str,
    choices: dict[str, str] | None,
) -> tuple[str, str | None, dict[str, str] | None, str | None]:
    try:
        raw_payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return raw_text, None, None, f"json_parse_error: {exc}"

    try:
        payload = LLMAnswerPayload.model_validate(raw_payload)
        _validate_choice_fields(payload, choices)
    except (ValidationError, ValueError) as exc:
        return _answer_from_raw_payload(raw_payload, raw_text), None, None, f"validation_error: {exc}"

    return payload.answer, payload.predictedAnswer, payload.choiceJudgements, None


def _validate_choice_fields(payload: LLMAnswerPayload, choices: dict[str, str] | None) -> None:
    labels = {label.upper() for label in (choices or {})}
    if not labels:
        if payload.predictedAnswer is not None or payload.choiceJudgements is not None:
            raise ValueError("Choice judgement fields must be null when choices are absent")
        return

    if payload.predictedAnswer is None:
        if payload.choiceJudgements is not None:
            raise ValueError("choiceJudgements must be null when predictedAnswer is null")
        return

    if payload.predictedAnswer not in labels:
        raise ValueError(f"predictedAnswer must be one of {sorted(labels)} or null")
    if payload.choiceJudgements is None:
        raise ValueError("choiceJudgements is required when predictedAnswer is set")
    if set(payload.choiceJudgements) != labels:
        raise ValueError(f"choiceJudgements keys must match {sorted(labels)}")


def _answer_from_raw_payload(raw_payload: Any, raw_text: str) -> str:
    if isinstance(raw_payload, dict) and isinstance(raw_payload.get("answer"), str):
        return raw_payload["answer"]
    return raw_text


def _anthropic_text(data: dict[str, Any]) -> str:
    parts = []
    for block in data.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts).strip()


def _format_citation(index: int, citation: Citation) -> str:
    text = citation.text or ""
    return (
        f"[{index}]\n"
        f"documentId: {citation.documentId}\n"
        f"contentUnitId: {citation.contentUnitId}\n"
        f"title: {citation.title or ''}\n"
        f"heading: {citation.heading or ''}\n"
        f"text: {text}"
    )
