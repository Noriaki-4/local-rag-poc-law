import json
from dataclasses import dataclass, replace
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
    stopReason: str | None = None
    contentBlockTypes: list[str] | None = None
    outputChars: int | None = None
    retryCount: int = 0
    questionPolarity: str | None = None
    choiceAssessments: dict[str, dict[str, Any]] | None = None


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
    stopReason: str | None = None
    retryCount: int = 0


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
    stopReason: str | None = None
    retryCount: int = 0


class LLMChoiceAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["entailed", "contradicted", "insufficient"]
    citationIds: list[str] = Field(default_factory=list)
    reason: str = ""


class LLMAnswerPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    answer: str = ""
    questionPolarity: Literal["select_entailed", "select_contradicted"] | None = None
    predictedAnswer: str | None = None
    choiceAssessments: dict[str, LLMChoiceAssessment] | None = None


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
        timeout = timeout_sec or settings.llm_timeout_sec
        started = perf_counter()
        result = self._generate_once(request, prompt, timeout, citations)
        if result is not None and result.validationError:
            retry_timeout = _retry_timeout(timeout, started)
            if retry_timeout is not None:
                retried = self._generate_once(request, prompt, retry_timeout, citations)
                if retried is not None:
                    result = replace(
                        retried,
                        retryCount=1,
                        latencyMs=result.latencyMs + retried.latencyMs,
                        inputTokens=_sum_optional(result.inputTokens, retried.inputTokens),
                        outputTokens=_sum_optional(result.outputTokens, retried.outputTokens),
                    )
        return result

    def _generate_once(
        self,
        request: AnswerRequest,
        prompt: str,
        timeout_sec: int | None,
        citations: list[Citation],
    ) -> LLMResult | None:
        if self.provider == "ollama":
            return self._generate_ollama(request, prompt, timeout_sec, citations)
        if self.provider == "anthropic":
            return self._generate_anthropic(request, prompt, timeout_sec, citations)
        return None

    def plan_search(
        self,
        request: AnswerRequest,
        max_queries: int,
        timeout_sec: int | None = None,
    ) -> SearchPlanResult:
        prompt = build_search_plan_prompt(request, max_queries)
        schema = _search_plan_json_schema(max_queries)
        timeout = timeout_sec or settings.planner_timeout_sec
        started = perf_counter()
        retry_count = 0
        raw_text, latency_ms, input_tokens, output_tokens, stop_reason = self._json_transport(
            prompt, schema, settings.planner_model, settings.planner_max_tokens, timeout
        )
        queries, graph_required, validation_error = _parse_search_plan(raw_text, max_queries)
        retry_timeout = _retry_timeout(timeout, started) if validation_error else None
        if retry_timeout is not None:
            retry_count = 1
            first_input_tokens, first_output_tokens = input_tokens, output_tokens
            raw_text, retry_latency, retry_input_tokens, retry_output_tokens, stop_reason = self._json_transport(
                prompt, schema, settings.planner_model, settings.planner_max_tokens, retry_timeout
            )
            latency_ms += retry_latency
            input_tokens = _sum_optional(first_input_tokens, retry_input_tokens)
            output_tokens = _sum_optional(first_output_tokens, retry_output_tokens)
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
            stopReason=stop_reason,
            retryCount=retry_count,
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
        timeout = timeout_sec or settings.evaluator_timeout_sec
        started = perf_counter()
        retry_count = 0
        raw_text, latency_ms, input_tokens, output_tokens, stop_reason = self._json_transport(
            prompt, schema, settings.evaluator_model, settings.evaluator_max_tokens, timeout
        )
        coverage, queries, graph_required, stop, validation_error = _parse_evidence_evaluation(
            raw_text,
            request.choices,
            max_queries,
        )
        retry_timeout = _retry_timeout(timeout, started) if validation_error else None
        if retry_timeout is not None:
            retry_count = 1
            first_input_tokens, first_output_tokens = input_tokens, output_tokens
            raw_text, retry_latency, retry_input_tokens, retry_output_tokens, stop_reason = self._json_transport(
                prompt, schema, settings.evaluator_model, settings.evaluator_max_tokens, retry_timeout
            )
            latency_ms += retry_latency
            input_tokens = _sum_optional(first_input_tokens, retry_input_tokens)
            output_tokens = _sum_optional(first_output_tokens, retry_output_tokens)
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
            stopReason=stop_reason,
            retryCount=retry_count,
        )

    def _generate_ollama(
        self,
        request: AnswerRequest,
        prompt: str,
        timeout_sec: int | None,
        citations: list[Citation],
    ) -> LLMResult:
        started = perf_counter()
        response = requests.post(
            f"{settings.ollama_base_url.rstrip('/')}/api/generate",
            json={
                "model": settings.answer_model,
                "prompt": prompt,
                "format": _answer_json_schema(request, citations),
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
        answer, predicted_answer, choice_judgements, assessments, polarity, validation_error = _parse_answer_payload(
            raw_text,
            request.choices,
            _citation_ids(citations),
        )
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
            stopReason=data.get("done_reason"),
            outputChars=len(raw_text),
            questionPolarity=polarity,
            choiceAssessments=assessments,
        )

    def _generate_anthropic(
        self,
        request: AnswerRequest,
        prompt: str,
        timeout_sec: int | None,
        citations: list[Citation],
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
                "output_config": {
                    "format": {
                        "type": "json_schema",
                        "schema": _to_anthropic_schema(_answer_json_schema(request, citations)),
                    }
                },
            },
            timeout=timeout_sec or settings.llm_timeout_sec,
        )
        if not response.ok:
            raise ValueError(f"{response.status_code}: {_anthropic_error(response)}")
        data = response.json()
        raw_text = _anthropic_text(data)
        answer, predicted_answer, choice_judgements, assessments, polarity, validation_error = _parse_answer_payload(
            raw_text,
            request.choices,
            _citation_ids(citations),
        )
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
            stopReason=data.get("stop_reason"),
            contentBlockTypes=[str(block.get("type")) for block in data.get("content", []) if isinstance(block, dict)],
            outputChars=len(raw_text),
            questionPolarity=polarity,
            choiceAssessments=assessments,
        )

    def _json_transport(
        self,
        prompt: str,
        schema: dict[str, Any],
        model: str,
        max_tokens: int,
        timeout_sec: int,
    ) -> tuple[str, int, int | None, int | None, str | None]:
        """provider共通のJSON生成トランスポート。(raw_text, latencyMs, inTokens, outTokens, stopReason)を返す。"""
        if self.provider == "ollama":
            return self._ollama_json(prompt, schema, model, timeout_sec)
        if self.provider == "anthropic":
            return self._anthropic_json(prompt, schema, model, max_tokens, timeout_sec)
        raise ValueError(f"Unsupported LLM_PROVIDER: {self.provider}")

    def _ollama_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        model: str,
        timeout_sec: int,
    ) -> tuple[str, int, int | None, int | None, str | None]:
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
            data.get("done_reason"),
        )

    def _anthropic_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        model: str,
        max_tokens: int,
        timeout_sec: int,
    ) -> tuple[str, int, int | None, int | None, str | None]:
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
                "output_config": {"format": {"type": "json_schema", "schema": _to_anthropic_schema(schema)}},
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
            data.get("stop_reason"),
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
    choice_commitment_rule = (
        "まず設問が、条文に適合する選択肢を選ぶ select_entailed か、誤り・非該当を選ぶ select_contradicted かを判定してください。"
        " 次に各選択肢の記述を独立に検証し、entailed、contradicted、insufficient のいずれかを設定してください。"
        " 各判定には、実際に根拠とした引用候補のcontentUnitIdだけをcitationIdsへ設定し、短い理由を付けてください。"
        " 最後にquestionPolarityとchoiceAssessmentsの整合する選択肢をpredictedAnswerへ設定してください。"
        " 根拠不足でも必ず最も可能性の高い選択肢を選び、answer内で不確実性を明記してください。"
        if request.choices
        else "選択肢がない場合、questionPolarity、predictedAnswer、choiceAssessmentsはnullにしてください。"
    )

    return f"""あなたはローカル検証環境の法務RAG回答生成器です。
外部APIや外部検索は使わず、下の引用候補を最優先の根拠として日本語で簡潔に回答してください。
法的判断を断定しすぎず、必要に応じて専門家確認が必要であることが伝わる表現にしてください。
引用する場合は contentUnitId を文中に含めてください。
必ずJSONだけを返してください。JSON以外の説明文やMarkdownコードフェンスは不要です。
answer には正解ラベルだけでなく、引用候補に基づく短い根拠説明を含めてください。
{choice_commitment_rule}

検索ルート: {" -> ".join(route)}
    質問: {request.question}{choices_block}

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


def _answer_json_schema(request: AnswerRequest, citations: list[Citation] | None = None) -> dict[str, Any]:
    labels = sorted(label.upper() for label in (request.choices or {}))
    citation_ids = _citation_ids(citations or [])
    citation_id_schema: dict[str, Any] = {"type": "string"}
    if citation_ids:
        citation_id_schema["enum"] = citation_ids
    assessment_schema = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["entailed", "contradicted", "insufficient"]},
            "citationIds": {
                "type": "array",
                "items": citation_id_schema,
                "maxItems": 3,
            },
            "reason": {"type": "string", "maxLength": 300},
        },
        "required": ["verdict", "citationIds", "reason"],
        "additionalProperties": False,
    }
    assessment_properties = {label: assessment_schema for label in labels}
    predicted_schema: dict[str, Any]
    polarity_schema: dict[str, Any]
    assessments_schema: dict[str, Any]
    if labels:
        predicted_schema = {"type": ["string", "null"], "enum": [*labels, None]}
        polarity_schema = {
            "type": ["string", "null"],
            "enum": ["select_entailed", "select_contradicted", None],
        }
        assessments_schema = {
            "type": ["object", "null"],
            "properties": assessment_properties,
            "required": labels,
            "additionalProperties": False,
        }
    else:
        predicted_schema = {"type": "null"}
        polarity_schema = {"type": "null"}
        assessments_schema = {"type": "null"}
    return {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "questionPolarity": polarity_schema,
            "predictedAnswer": predicted_schema,
            "choiceAssessments": assessments_schema,
        },
        "required": ["answer", "questionPolarity", "predictedAnswer", "choiceAssessments"],
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


# Anthropic構造化出力が未サポートの制約キーワード。件数・文字数の上限はコード側の
# パーサー(_parse_search_plan等)が同じ値で強制しているため、除去しても安全。
_ANTHROPIC_UNSUPPORTED_KEYS = ("minItems", "maxItems", "minLength", "maxLength")


def _to_anthropic_schema(schema: Any) -> Any:
    """内部スキーマ(Ollama互換のunion型記法)をAnthropic構造化出力の方言へ変換する。

    Anthropicのスキーマ検証は `"type": ["string", "null"]` のようなunion型と enum の併用を
    拒否するため、union型を anyOf のブランチへ再帰的に展開する。enum値は各ブランチの型に
    一致するものだけを残す。また minItems/maxItems 等の未サポート制約は除去する。
    スキーマ定義自体は1ソース(内部表現)のまま、プロバイダ差はこの変換に閉じ込める。
    """
    if isinstance(schema, list):
        return [_to_anthropic_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    converted = {
        key: _to_anthropic_schema(value)
        for key, value in schema.items()
        if key not in _ANTHROPIC_UNSUPPORTED_KEYS
    }
    type_value = converted.get("type")
    if not isinstance(type_value, list):
        return converted

    branches = []
    for type_name in type_value:
        if type_name == "null":
            branches.append({"type": "null"})
            continue
        branch = {key: value for key, value in converted.items() if key != "type"}
        branch["type"] = type_name
        if "enum" in branch:
            allowed = [value for value in branch["enum"] if _enum_value_matches_type(value, type_name)]
            if allowed:
                branch["enum"] = allowed
            else:
                del branch["enum"]
        if type_name != "object":
            for object_key in ("properties", "required", "additionalProperties"):
                branch.pop(object_key, None)
        branches.append(branch)
    return {"anyOf": branches}


def _enum_value_matches_type(value: Any, type_name: str) -> bool:
    if type_name == "null":
        return value is None
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return value is not None


def _retry_timeout(total_timeout_sec: int, started: float) -> int | None:
    remaining = int(total_timeout_sec - (perf_counter() - started))
    return remaining if remaining > 1 else None


def _sum_optional(first: int | None, second: int | None) -> int | None:
    if first is None and second is None:
        return None
    return int(first or 0) + int(second or 0)


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
    allowed_citation_ids: list[str] | None = None,
) -> tuple[
    str,
    str | None,
    dict[str, str] | None,
    dict[str, dict[str, Any]] | None,
    str | None,
    str | None,
]:
    try:
        raw_payload = json.loads(_strip_markdown_fence(raw_text))
    except json.JSONDecodeError as exc:
        return raw_text, None, None, None, None, f"json_parse_error: {exc}"

    try:
        payload = LLMAnswerPayload.model_validate(raw_payload)
        _validate_choice_fields(payload, choices, set(allowed_citation_ids or []))
    except (ValidationError, ValueError) as exc:
        return _answer_from_raw_payload(raw_payload, raw_text), None, None, None, None, f"validation_error: {exc}"

    answer_text = payload.answer or "（回答テキストが取得できなかったため、選択肢判定のみ返します。）"
    assessments = None
    if payload.choiceAssessments:
        assessments = {}
        for label, assessment in payload.choiceAssessments.items():
            normalized = assessment.model_dump()
            normalized["citationIds"] = list(dict.fromkeys(assessment.citationIds))[:3]
            normalized["reason"] = assessment.reason[:300]
            assessments[label] = normalized
    judgements = None
    if payload.predictedAnswer is not None and choices:
        judgements = {
            label.upper(): "supported" if label.upper() == payload.predictedAnswer else "not_supported"
            for label in choices
        }
    return answer_text, payload.predictedAnswer, judgements, assessments, payload.questionPolarity, None


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


def _validate_choice_fields(
    payload: LLMAnswerPayload,
    choices: dict[str, str] | None,
    allowed_citation_ids: set[str],
) -> None:
    labels = {label.upper() for label in (choices or {})}
    if not labels:
        if any(
            value is not None
            for value in (payload.questionPolarity, payload.predictedAnswer, payload.choiceAssessments)
        ):
            raise ValueError("Choice assessment fields must be null when choices are absent")
        return

    if payload.questionPolarity is None:
        raise ValueError("questionPolarity is required when choices are present")
    if payload.predictedAnswer not in labels:
        raise ValueError(f"predictedAnswer must be one of {sorted(labels)}")
    if payload.choiceAssessments is None or set(payload.choiceAssessments) != labels:
        raise ValueError(f"choiceAssessments keys must match {sorted(labels)}")

    for label, assessment in payload.choiceAssessments.items():
        unknown = set(assessment.citationIds) - allowed_citation_ids
        if unknown:
            raise ValueError(f"choiceAssessments.{label}.citationIds contains unknown IDs: {sorted(unknown)}")

    target_verdict = "entailed" if payload.questionPolarity == "select_entailed" else "contradicted"
    verdicts = {label: assessment.verdict for label, assessment in payload.choiceAssessments.items()}
    selected_verdict = verdicts[payload.predictedAnswer]
    if target_verdict in verdicts.values() and selected_verdict != target_verdict:
        raise ValueError(f"predictedAnswer must have verdict {target_verdict}")
    if target_verdict not in verdicts.values() and selected_verdict != "insufficient":
        raise ValueError("predictedAnswer must be insufficient when no assessment matches questionPolarity")


def _citation_ids(citations: list[Citation]) -> list[str]:
    return list(dict.fromkeys(citation.contentUnitId for citation in citations if citation.contentUnitId))


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
