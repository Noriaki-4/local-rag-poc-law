import json
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
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


@dataclass
class SearchPlanResult:
    queries: list[str]
    graphRequired: bool
    provider: str
    model: str
    latencyMs: int
    inputTokens: int | None
    outputTokens: int | None
    validationError: str | None = None


@dataclass
class EvidenceEvaluationResult:
    choiceCoverage: dict[str, str]
    followUpQueries: list[str]
    graphRequired: bool
    stop: bool
    provider: str
    model: str
    latencyMs: int
    inputTokens: int | None
    outputTokens: int | None
    validationError: str | None = None


class LLMAnswerPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    answer: str = ""
    predictedAnswer: str | None = None
    choiceJudgements: dict[str, Literal["supported", "not_supported"] | None] | None = None


class SearchPlanPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    queries: list[str] = Field(min_length=1, max_length=8)
    graphRequired: bool = False


class EvidenceEvaluationPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    choiceCoverage: dict[str, Literal["sufficient", "missing", "missing_definition", "missing_exception"]]
    followUpQueries: list[str] = Field(default_factory=list, max_length=8)
    graphRequired: bool = False
    stop: bool = False


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
                "ok": all(
                    model in models
                    for model in {settings.answer_model, settings.planner_model, settings.evaluator_model}
                ),
                "baseUrl": settings.ollama_base_url,
                "answerModel": settings.answer_model,
                "plannerModel": settings.planner_model,
                "evaluatorModel": settings.evaluator_model,
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
            "plannerModel": settings.planner_model,
            "evaluatorModel": settings.evaluator_model,
        }

    def generate_answer(
        self,
        request: AnswerRequest,
        route: list[str],
        citations: list[Citation],
        timeout_sec: int | None = None,
        evidence_by_choice: dict[str, list[str]] | None = None,
    ) -> LLMResult | None:
        prompt = build_answer_prompt(request, route, citations, evidence_by_choice)
        if self.provider == "ollama":
            return self._generate_ollama(request, prompt, timeout_sec)
        if self.provider == "anthropic":
            return self._generate_anthropic(request, prompt, timeout_sec)
        return None

    def plan_search(
        self,
        request: AnswerRequest,
        max_queries: int,
        timeout_sec: int | None = None,
    ) -> SearchPlanResult:
        prompt = build_search_plan_prompt(request, max_queries)
        schema = _search_plan_json_schema(max_queries)
        if self.provider == "ollama":
            raw_text, latency_ms, input_tokens, output_tokens = self._ollama_json(
                prompt,
                schema,
                settings.planner_model,
                timeout_sec or settings.planner_timeout_sec,
            )
        elif self.provider == "anthropic":
            raw_text, latency_ms, input_tokens, output_tokens = self._anthropic_json(
                prompt,
                settings.planner_model,
                settings.planner_max_tokens,
                timeout_sec or settings.planner_timeout_sec,
            )
        else:
            raise ValueError(f"Unsupported LLM_PROVIDER: {self.provider}")
        queries, graph_required, validation_error = _parse_search_plan(raw_text, max_queries)
        return SearchPlanResult(
            queries=queries,
            graphRequired=graph_required,
            provider=self.provider,
            model=settings.planner_model,
            latencyMs=latency_ms,
            inputTokens=input_tokens,
            outputTokens=output_tokens,
            validationError=validation_error,
        )

    def evaluate_evidence(
        self,
        request: AnswerRequest,
        citations: list[Citation],
        max_queries: int = 2,
        timeout_sec: int | None = None,
    ) -> EvidenceEvaluationResult:
        prompt = build_evidence_evaluation_prompt(request, citations, max_queries)
        schema = _evidence_evaluation_json_schema(request, max_queries)
        if self.provider == "ollama":
            raw_text, latency_ms, input_tokens, output_tokens = self._ollama_json(
                prompt,
                schema,
                settings.evaluator_model,
                timeout_sec or settings.evaluator_timeout_sec,
            )
        elif self.provider == "anthropic":
            raw_text, latency_ms, input_tokens, output_tokens = self._anthropic_json(
                prompt,
                settings.evaluator_model,
                settings.evaluator_max_tokens,
                timeout_sec or settings.evaluator_timeout_sec,
            )
        else:
            raise ValueError(f"Unsupported LLM_PROVIDER: {self.provider}")
        coverage, queries, graph_required, stop, validation_error = _parse_evidence_evaluation(
            raw_text,
            request.choices,
            max_queries,
        )
        return EvidenceEvaluationResult(
            choiceCoverage=coverage,
            followUpQueries=queries,
            graphRequired=graph_required,
            stop=stop,
            provider=self.provider,
            model=settings.evaluator_model,
            latencyMs=latency_ms,
            inputTokens=input_tokens,
            outputTokens=output_tokens,
            validationError=validation_error,
        )

    def _generate_ollama(
        self,
        request: AnswerRequest,
        prompt: str,
        timeout_sec: int | None,
    ) -> LLMResult:
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
            timeout=timeout_sec or settings.llm_timeout_sec,
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

    def _generate_anthropic(
        self,
        request: AnswerRequest,
        prompt: str,
        timeout_sec: int | None,
    ) -> LLMResult:
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
            },
            timeout=timeout_sec or settings.llm_timeout_sec,
        )
        if not response.ok:
            error_detail = ""
            try:
                error_detail = response.json().get("error", {}).get("message", "")
            except Exception:
                error_detail = response.text[:200]
            raise ValueError(f"{response.status_code}: {error_detail}")
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

    def _ollama_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        model: str,
        timeout_sec: int,
    ) -> tuple[str, int, int | None, int | None]:
        started = perf_counter()
        response = requests.post(
            f"{settings.ollama_base_url.rstrip('/')}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "format": schema,
                "stream": False,
                "options": {"temperature": 0, "top_p": 1},
            },
            timeout=timeout_sec,
        )
        response.raise_for_status()
        data = response.json()
        return (
            str(data.get("response", "")).strip(),
            int((perf_counter() - started) * 1000),
            data.get("prompt_eval_count"),
            data.get("eval_count"),
        )

    def _anthropic_json(
        self,
        prompt: str,
        model: str,
        max_tokens: int,
        timeout_sec: int,
    ) -> tuple[str, int, int | None, int | None]:
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
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=timeout_sec,
        )
        if not response.ok:
            raise ValueError(f"{response.status_code}: {_anthropic_error(response)}")
        data = response.json()
        usage = data.get("usage", {})
        return (
            _anthropic_text(data),
            int((perf_counter() - started) * 1000),
            usage.get("input_tokens"),
            usage.get("output_tokens"),
        )


def build_answer_prompt(
    request: AnswerRequest,
    route: list[str],
    citations: list[Citation],
    evidence_by_choice: dict[str, list[str]] | None = None,
) -> str:
    citation_block = _format_citations_with_budget(citations, settings.llm_max_context_chars)

    choices_block = ""
    if request.choices:
        choices_block = "\n選択肢:\n" + "\n".join(
            f"{label.upper()}: {text}" for label, text in sorted(request.choices.items())
        )
    evidence_matrix_block = ""
    if evidence_by_choice:
        evidence_matrix_block = "\n選択肢別の根拠候補contentUnitId:\n" + "\n".join(
            f"{label}: {', '.join(content_ids) if content_ids else '候補なし'}"
            for label, content_ids in sorted(evidence_by_choice.items())
        )

    choice_commitment_rule = (
        "選択肢がある場合、predictedAnswer には必ずいずれかの選択肢ラベルを設定してください。"
        " 引用候補が薄く確信が持てない場合でも、null にはせず、引用候補や一般的な法的知識から最も可能性が高いと考えられる選択肢を選んでください。"
        " choiceJudgements は各選択肢に supported または not_supported を設定してください（predictedAnswer と一致する選択肢のみ supported）。"
        " ただし answer 内では、根拠が薄い場合はその旨を明記し、専門家確認が必要であることを伝えてください。"
        if request.choices
        else "選択肢がない場合、predictedAnswer と choiceJudgements は null にしてください。"
    )

    return f"""あなたはローカル検証環境の法務RAG回答生成器です。
外部APIや外部検索は使わず、下の引用候補を最優先の根拠として日本語で簡潔に回答してください。
法的判断を断定しすぎず、必要に応じて専門家確認が必要であることが伝わる表現にしてください。
引用する場合は contentUnitId を文中に含めてください。
必ずJSONだけを返してください。JSON以外の説明文やMarkdownコードフェンスは不要です。
answer には正解ラベルだけでなく、引用候補に基づく短い根拠説明を含めてください。
{choice_commitment_rule}

検索ルート: {" -> ".join(route)}
質問: {request.question}{choices_block}{evidence_matrix_block}

引用候補:
{citation_block}

JSON:"""


def build_evidence_evaluation_prompt(
    request: AnswerRequest,
    citations: list[Citation],
    max_queries: int,
) -> str:
    choices_block = ""
    if request.choices:
        choices_block = "\n選択肢:\n" + "\n".join(
            f"{label.upper()}: {text}" for label, text in sorted(request.choices.items())
        )
    citation_block = _format_citations_with_budget(citations, min(settings.llm_max_context_chars, 3500))
    return f"""あなたは法令検索の根拠充足度を判定するEvaluatorです。
正解の選択肢は回答せず、各選択肢を検証する根拠が十分かだけを判定してください。
定義条文が不足する場合は missing_definition、ただし書・例外が不足する場合は missing_exception、
その他の不足は missing、十分なら sufficient としてください。
不足がある場合だけfollowUpQueriesを最大{max_queries}件作ってください。
条文間参照の解決が必要ならgraphRequiredをtrueにしてください。
全選択肢の根拠が十分ならstopをtrueにしてください。必ずJSONだけを返してください。

質問: {request.question}{choices_block}

根拠候補:
{citation_block}

JSON:"""


def build_search_plan_prompt(request: AnswerRequest, max_queries: int) -> str:
    choices_block = ""
    if request.choices:
        choices_block = "\n選択肢:\n" + "\n".join(
            f"{label.upper()}: {text}" for label, text in sorted(request.choices.items())
        )
    return f"""あなたは日本法令コーパスを検索するクエリプランナーです。
質問を、条文本文を取得するための互いに重複しすぎない検索クエリへ分解してください。
正解の選択肢を推測するのではなく、各選択肢を検証できる検索語を作ってください。
「前条」「同項」「ただし書」「定義」「準用」など参照先の探索が必要なら graphRequired を true にしてください。
queries は最大 {max_queries} 件、各クエリは200文字以内にしてください。
必ずJSONだけを返してください。

質問: {request.question}{choices_block}

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


def _search_plan_json_schema(max_queries: int) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "queries": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 200},
                "minItems": 1,
                "maxItems": max_queries,
            },
            "graphRequired": {"type": "boolean"},
        },
        "required": ["queries", "graphRequired"],
        "additionalProperties": False,
    }


def _evidence_evaluation_json_schema(request: AnswerRequest, max_queries: int) -> dict[str, Any]:
    labels = sorted(label.upper() for label in (request.choices or {})) or ["overall"]
    coverage_properties = {
        label: {
            "type": "string",
            "enum": ["sufficient", "missing", "missing_definition", "missing_exception"],
        }
        for label in labels
    }
    return {
        "type": "object",
        "properties": {
            "choiceCoverage": {
                "type": "object",
                "properties": coverage_properties,
                "required": labels,
                "additionalProperties": False,
            },
            "followUpQueries": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 200},
                "maxItems": max_queries,
            },
            "graphRequired": {"type": "boolean"},
            "stop": {"type": "boolean"},
        },
        "required": ["choiceCoverage", "followUpQueries", "graphRequired", "stop"],
        "additionalProperties": False,
    }


def _strip_markdown_fence(text: str) -> str:
    """一部モデルはJSON出力指示があってもMarkdownコードフェンスで囲むため除去する。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def _parse_answer_payload(
    raw_text: str,
    choices: dict[str, str] | None,
) -> tuple[str, str | None, dict[str, str] | None, str | None]:
    try:
        raw_payload = json.loads(_strip_markdown_fence(raw_text))
    except json.JSONDecodeError as exc:
        return raw_text, None, None, f"json_parse_error: {exc}"

    try:
        payload = LLMAnswerPayload.model_validate(raw_payload)
        _validate_choice_fields(payload, choices)
    except (ValidationError, ValueError) as exc:
        return _answer_from_raw_payload(raw_payload, raw_text), None, None, f"validation_error: {exc}"

    predicted_answer = _derive_predicted_answer(payload.predictedAnswer, payload.choiceJudgements)
    answer_text = payload.answer or "（回答テキストが取得できなかったため、選択肢判定のみ返します。）"
    return answer_text, predicted_answer, payload.choiceJudgements, None


def _parse_search_plan(raw_text: str, max_queries: int) -> tuple[list[str], bool, str | None]:
    try:
        raw_payload = json.loads(_strip_markdown_fence(raw_text))
        payload = SearchPlanPayload.model_validate(raw_payload)
    except (json.JSONDecodeError, ValidationError) as exc:
        return [], False, f"search_plan_validation_error: {exc}"

    queries = []
    seen = set()
    for query in payload.queries:
        normalized = " ".join(query.split()).strip()[:200]
        if normalized and normalized not in seen:
            queries.append(normalized)
            seen.add(normalized)
        if len(queries) >= max_queries:
            break
    if not queries:
        return [], False, "search_plan_validation_error: no usable queries"
    return queries, payload.graphRequired, None


def _parse_evidence_evaluation(
    raw_text: str,
    choices: dict[str, str] | None,
    max_queries: int,
) -> tuple[dict[str, str], list[str], bool, bool, str | None]:
    try:
        raw_payload = json.loads(_strip_markdown_fence(raw_text))
        payload = EvidenceEvaluationPayload.model_validate(raw_payload)
        expected_labels = {label.upper() for label in (choices or {})} or {"overall"}
        if set(payload.choiceCoverage) != expected_labels:
            raise ValueError(f"choiceCoverage keys must match {sorted(expected_labels)}")
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        return {}, [], False, False, f"evidence_evaluation_validation_error: {exc}"
    queries = []
    for query in payload.followUpQueries:
        normalized = " ".join(query.split()).strip()[:200]
        if normalized and normalized not in queries:
            queries.append(normalized)
        if len(queries) >= max_queries:
            break
    return dict(payload.choiceCoverage), queries, payload.graphRequired, payload.stop, None


def _validate_choice_fields(payload: LLMAnswerPayload, choices: dict[str, str] | None) -> None:
    labels = {label.upper() for label in (choices or {})}
    if not labels:
        if payload.predictedAnswer is not None or payload.choiceJudgements is not None:
            raise ValueError("Choice judgement fields must be null when choices are absent")
        return

    if payload.predictedAnswer is not None and payload.predictedAnswer not in labels:
        raise ValueError(f"predictedAnswer must be one of {sorted(labels)} or null")
    if payload.predictedAnswer is not None and payload.choiceJudgements is None:
        raise ValueError("choiceJudgements is required when predictedAnswer is set")
    if payload.choiceJudgements is not None and set(payload.choiceJudgements) != labels:
        raise ValueError(f"choiceJudgements keys must match {sorted(labels)}")


def _derive_predicted_answer(
    predicted_answer: str | None,
    choice_judgements: dict[str, str] | None,
) -> str | None:
    """predictedAnswer が null でも、judgements で supported がちょうど1つなら答えとみなす。"""
    if predicted_answer is not None or not choice_judgements:
        return predicted_answer
    supported = [label for label, judgement in choice_judgements.items() if judgement == "supported"]
    if len(supported) == 1:
        return supported[0]
    return None


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


def _anthropic_error(response: requests.Response) -> str:
    try:
        return str(response.json().get("error", {}).get("message", ""))
    except Exception:
        return response.text[:200]


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


def _format_citations_with_budget(citations: list[Citation], max_chars: int) -> str:
    if not citations:
        return "引用候補なし"
    per_citation_budget = max(300, max_chars // len(citations))
    blocks = []
    for index, citation in enumerate(citations, start=1):
        block = _format_citation(index, citation)
        blocks.append(block[:per_citation_budget])
    return "\n\n".join(blocks)[:max_chars]
