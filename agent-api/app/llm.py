from dataclasses import dataclass
from time import perf_counter
from typing import Any

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


class LLMClient:
    def __init__(self) -> None:
        self.provider = settings.llm_provider.lower()

    def health(self) -> dict[str, Any]:
        if self.provider != "ollama":
            return {"provider": self.provider, "ok": False, "reason": "Only ollama is enabled for initial local testing"}
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

    def generate_answer(
        self,
        request: AnswerRequest,
        route: list[str],
        citations: list[Citation],
        predicted_answer: str | None,
    ) -> LLMResult | None:
        if self.provider != "ollama":
            return None
        prompt = build_answer_prompt(request, route, citations, predicted_answer)
        started = perf_counter()
        response = requests.post(
            f"{settings.ollama_base_url.rstrip('/')}/api/generate",
            json={
                "model": settings.answer_model,
                "prompt": prompt,
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
        return LLMResult(
            text=str(data.get("response", "")).strip(),
            provider="ollama",
            model=settings.answer_model,
            latencyMs=int((perf_counter() - started) * 1000),
            inputTokens=data.get("prompt_eval_count"),
            outputTokens=data.get("eval_count"),
            estimatedCost=0,
        )


def build_answer_prompt(
    request: AnswerRequest,
    route: list[str],
    citations: list[Citation],
    predicted_answer: str | None,
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

    predicted_block = f"\n内部の選択肢候補: {predicted_answer}" if predicted_answer else ""

    return f"""あなたはローカル検証環境の法務RAG回答生成器です。
外部APIや外部検索は使わず、下の引用候補だけを根拠に日本語で簡潔に回答してください。
法的判断を断定しすぎず、必要に応じて専門家確認が必要であることが伝わる表現にしてください。
引用する場合は contentUnitId を文中に含めてください。

検索ルート: {" -> ".join(route)}
質問: {request.question}{choices_block}{predicted_block}

引用候補:
{citation_block}

回答:"""


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
