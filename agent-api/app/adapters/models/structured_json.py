"""既存provider共通JSON transportを新FrameworkのModel Portへ接続する。"""

from __future__ import annotations

import json
from time import monotonic
from typing import Any

from pydantic import ValidationError

from app.agent_framework.context import ContextCapacityExceeded, SolverContext
from app.agent_framework.contracts import SolverDecision
from app.agent_framework.ports.model import (
    ModelProtocolError,
    ReviewCallResult,
    ReviewContext,
    SolverCallResult,
)
from app.agent_framework.profiles import ModelCallProfile, ReviewerProfile
from app.agent_framework.state import ReviewResult
from app.llm import LLMClient


class StructuredJSONModelAdapter:
    def __init__(self, client: LLMClient) -> None:
        self._client = client

    def solve(
        self,
        context: SolverContext,
        profile: ModelCallProfile,
    ) -> SolverCallResult:
        base_prompt = _solver_prompt(context, profile.system_prompt)
        prompt = base_prompt
        input_tokens = 0
        output_tokens = 0
        input_tokens_known = True
        output_tokens_known = True
        attempt_count = 0
        last_error: ModelProtocolError | ValidationError | None = None
        started_at = monotonic()

        for repair_index in range(2):
            remaining_timeout = profile.timeout_sec - (monotonic() - started_at)
            if remaining_timeout <= 1:
                raise TimeoutError("solver contract repair time exhausted")
            result = self._client.generate_structured_json(
                prompt=prompt,
                schema=_solver_transport_schema(context),
                model=profile.model,
                max_tokens=profile.max_output_tokens,
                timeout_sec=max(1, round(remaining_timeout)),
            )
            attempt_count += 1 + result.retryCount
            if result.inputTokens is None:
                input_tokens_known = False
            else:
                input_tokens += result.inputTokens
            if result.outputTokens is None:
                output_tokens_known = False
            else:
                output_tokens += result.outputTokens

            if result.validationError or result.payload is None:
                last_error = ModelProtocolError(
                    f"solver transport invalid: {result.validationError or 'empty'}"
                )
            else:
                try:
                    decision = SolverDecision.model_validate(
                        _normalize_solver_payload(result.payload)
                    )
                    return SolverCallResult(
                        decision=decision,
                        input_tokens=(input_tokens if input_tokens_known else None),
                        output_tokens=(output_tokens if output_tokens_known else None),
                        attempt_count=attempt_count,
                    )
                except (ModelProtocolError, ValidationError) as exc:
                    last_error = exc

            if repair_index == 0:
                prompt = _solver_repair_prompt(
                    base_prompt,
                    result.payload,
                    last_error,
                )
                _ensure_solver_prompt_capacity(prompt, context.max_solver_input_chars)

        if isinstance(last_error, ValidationError):
            raise ModelProtocolError("solver decision violates schema") from last_error
        if isinstance(last_error, ModelProtocolError):
            raise last_error
        raise ModelProtocolError("solver decision is unavailable")

    def review(
        self,
        context: ReviewContext,
        profile: ReviewerProfile,
    ) -> ReviewCallResult:
        prompt = _review_prompt(context, profile.system_prompt)
        result = self._client.generate_structured_json(
            prompt=prompt,
            schema=ReviewResult.model_json_schema(),
            model=profile.model,
            max_tokens=profile.max_output_tokens,
            timeout_sec=max(1, round(profile.timeout_sec)),
        )
        if result.validationError or result.payload is None:
            raise ModelProtocolError(
                f"review structured output invalid: {result.validationError or 'empty'}"
            )
        try:
            review = ReviewResult.model_validate(result.payload)
        except ValidationError as exc:
            raise ModelProtocolError("review result violates schema") from exc
        return ReviewCallResult(
            review=review,
            input_tokens=result.inputTokens,
            output_tokens=result.outputTokens,
            attempt_count=1 + result.retryCount,
        )


def _solver_prompt(context: SolverContext, system_prompt: str) -> str:
    payload = json.dumps(
        context.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    contract_repair_instruction = ""
    if context.contract_feedback is not None:
        contract_repair_instruction = (
            "\nこれは状態へ未適用のDecisionに対する契約修復呼出しです。"
            "同じviolationを繰り返さないでください。"
            "violationがfinalize時のopen WorkItemを示す場合は、"
            "前回の意味判断と整合するresolutionを付けて全open WorkItemを"
            "resolvedまたはdroppedへ更新するか、正当に閉じられないなら"
            "next=continueとして必要なToolRequestを返してください。"
            "どちらを選ぶかとresolutionの内容はあなたが判断します。\n"
            "violationがunknown evidence IDを示す場合は、そのIDを削除するか、"
            "solver_context.grounding_evidence_idsに完全一致するIDだけへ置き換えてください。"
            "検索候補本文中の番号やsourceContentUnitIdをEvidence IDとして使ってはいけません。\n"
            "violationがunknown Article IDを示す場合は、そのIDを削除し、"
            "solver_context.fetchable_article_idsに完全一致するIDだけへ置き換えてください。"
            "前回Decisionの既知IDは維持し、未知IDの条番号を修正した別IDや新しいIDを追加しません。"
            "本文中の条番号、法令番号、documentIdをArticle IDへ変換しません。"
            "修復後のfetch_articlesの全IDをfetchable_article_idsと文字列の完全一致で再確認してください。"
            "未知IDを除くとarticle_idsが空になる、または必要な参照先IDが同一覧にない場合は、"
            "fetch_articlesを残さず、表示済みの法令名・条番号・確認事項をqueryにしたlegal_searchを返してください。"
            "下位規範のDependencyDecisionは、委任元が既知ならdiscover_target、"
            "委任元候補も未発見ならdiscover_sourceとして、そのlegal_searchのrequest_idを"
            "action_request_idへ指定してください。\n"
            "violationがdependency target Article repeats its source articleを示す場合、"
            "同じArticle本文を取得するRequestならaction=assess_source、target_article_ids=[]へ"
            "修正してください。委任先を取得したいが正確な別Article IDが未特定なら、"
            "action=discover_targetとしてlegal_searchを返してください。"
            "委任元Articleをfetch_targetとして残してはいけません。\n"
            "violationがfetch_articles.article_ids exceeds the profile limitを示す場合は、"
            "未確認の命題に直接必要なArticleを4個以下に意味選択してください。"
            "プログラムは候補を選別しません。上限回避のためにfetch_articlesを"
            "複数Requestへ分割せず、残りは後続Decisionの候補として残します。\n"
            "violationがremaining Cycle capacityまたはCycle boundaryを示す場合は、"
            "現CycleへToolを追加しません。取得済み結果を評価し、can_start_next_cycle=trueなら"
            "次に検証する命題・方針を明示してstart_next_cycle=true、falseならfinalizeします。\n"
            "violationがdependency decisionを示す場合は、指定されたdependency_kindについて、"
            "判断開始時にopenだった各WorkItemへちょうど1件ずつ返してください。"
            "必要な種別とWorkItem IDはsolver_context.required_dependency_kindと"
            "required_dependency_work_item_idsに列挙されています。"
            "委任元候補が未発見ならsource_evidence_ids=[]かつdiscover_source、"
            "それ以外のneeds_actionならassess_source / discover_target / fetch_targetのactionと、"
            "同じDecision内のToolRequest IDをaction_request_idへ、"
            "fetch_targetならその一括Request中の委任先Articleだけをtarget_article_idsへ、"
            "resolvedならsolver_context.grounding_evidence_idsに完全一致する根拠IDを"
            "evidence_idsへ指定してください。not_required / needs_action / resolvedのどれが"
            "妥当かはあなたが意味を判断してください。\n"
            "violationがgraph reviewを示す場合は、required_graph_review_request_idsと"
            "graph_review_batchの全link_idを各対応欄へ完全一致でコピーしてください。"
            "batchの全frontier_item_idへselect/defer/rejectを1件ずつ返し、selectは"
            "remaining_fetch_capacityとmax_selected_frontier_per_stepの小さい方以内にします。"
            "候補の関連性はあなたが判断し、プログラムに選別を要求しません。\n"
        )
    prompt = (
        f"{system_prompt}\n\n"
        f"{_SOLVER_CONTRACT}\n\n"
        "contract_feedbackがある場合、直前Decisionは状態へ適用されていません。"
        "意味上の判断を保ち、violationで示された構造だけを修正してください。\n"
        f"{contract_repair_instruction}"
        "以下は現在のSolverContextです。Provider輸送schemaに従い、"
        "next、next_focus_work_item_ids、retain_evidence_ids、answerは直接返し、"
        "update全体をupdate_json、tool_requests全体をtool_requests_jsonへ"
        "JSON文字列化し、dependency_decisionsはschemaどおりの配列として直接"
        "返し、graph_candidate_reviewとfrontier_re_adoptionsもschemaどおり直接返してください。"
        "Adapterが2つのJSON文字列を復元し、"
        "SolverDecisionとして上記契約で完全検証します。\n"
        f"<solver_context>{payload}</solver_context>"
    )
    _ensure_solver_prompt_capacity(prompt, context.max_solver_input_chars)
    return prompt


def _ensure_solver_prompt_capacity(prompt: str, max_input_chars: int) -> None:
    if len(prompt) > max_input_chars:
        raise ContextCapacityExceeded(
            "context_capacity_exceeded: solver prompt exceeds "
            "max_solver_input_chars"
        )


def _review_prompt(context: ReviewContext, system_prompt: str) -> str:
    payload = json.dumps(
        context.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        f"{system_prompt}\n\n"
        "以下の質問・回答・実際に引用された根拠だけを確認し、"
        "ReviewResultだけを返してください。\n"
        f"<review_context>{payload}</review_context>"
    )


def _solver_repair_prompt(
    base_prompt: str,
    payload: dict | None,
    error: ModelProtocolError | ValidationError,
) -> str:
    previous = ""
    if isinstance(payload, dict):
        previous = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if isinstance(error, ValidationError):
        error_detail = json.dumps(
            error.errors(include_url=False, include_input=False),
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    else:
        error_detail = str(error)
    return (
        f"{base_prompt}\n\n"
        "直前の出力は輸送またはschema検証だけに失敗しました。"
        "意味上の判断を変えず、契約に適合するSolverDecisionへ修復してください。\n"
        f"<validation_error>{error_detail}</validation_error>\n"
        f"<previous_solver_decision>{previous}</previous_solver_decision>"
    )


def _solver_transport_schema(context: SolverContext) -> dict:
    string_array = {"type": "array", "items": {"type": "string"}}
    answer = _strict_object(
        {
            "text": {"type": "string"},
            "citation_ids": string_array,
            "limitations": string_array,
        }
    )
    dependency_decision = _strict_object(
        {
            "dependency_kind": {"type": "string"},
            "work_item_id": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["not_required", "needs_action", "resolved"],
            },
            "reason": {"type": "string"},
            "source_evidence_ids": string_array,
            "action": {
                "anyOf": [
                    {
                        "type": "string",
                        "enum": [
                            "discover_source",
                            "assess_source",
                            "discover_target",
                            "fetch_target",
                        ],
                    },
                    {"type": "null"},
                ]
            },
            "action_request_id": {
                "anyOf": [{"type": "string"}, {"type": "null"}]
            },
            "target_article_ids": string_array,
            "evidence_ids": string_array,
        }
    )
    required_dependency_count = len(context.required_dependency_work_item_ids)
    dependency_decisions = {
        "type": "array",
        "items": dependency_decision,
        "minItems": required_dependency_count,
        "maxItems": required_dependency_count,
    }
    batch_candidates = context.graph_review_batch.candidates
    batch_frontier_ids = tuple(item.frontier_item_id for item in batch_candidates)
    selectable_ledger = tuple(
        item
        for item in context.graph_review_ledger
        if (
            item.review_status == "relevant_deferred"
            and item.content_status in {"not_requested", "failed", "timeout"}
        )
        or (
            item.review_status == "selected"
            and item.content_status in {"failed", "timeout"}
        )
    )
    selectable_frontier_ids = tuple(
        dict.fromkeys(
            [
                *batch_frontier_ids,
                *(item.frontier_item_id for item in selectable_ledger),
            ]
        )
    )
    graph_article_ids = tuple(
        dict.fromkeys(
            [
                *(item.article_id for item in batch_candidates),
                *(item.article_id for item in selectable_ledger),
            ]
        )
    )
    graph_work_item_ids = tuple(
        dict.fromkeys(
            [
                *(item.work_item_id for item in batch_candidates),
                *(item.work_item_id for item in selectable_ledger),
            ]
        )
    )
    graph_hypothesis_ids = tuple(
        dict.fromkeys(
            item.hypothesis_id
            for item in (*batch_candidates, *selectable_ledger)
            if item.hypothesis_id is not None
        )
    )
    graph_frontier_decision = _strict_object(
        {
            "frontier_item_id": _enum_string(selectable_frontier_ids),
            "article_id": _enum_string(graph_article_ids),
            "work_item_id": _enum_string(graph_work_item_ids),
            "hypothesis_id": {
                "anyOf": [_enum_string(graph_hypothesis_ids), {"type": "null"}]
            },
            "action": {
                "type": "string",
                "enum": ["select", "defer", "reject"],
            },
            "reason": {"type": "string"},
        }
    )
    reviewed_link_ids = tuple(
        dict.fromkeys(
            link.link_id
            for item in batch_candidates
            for link in item.links
        )
    )
    graph_candidate_review = _strict_object(
        {
            "graph_request_ids": {
                "type": "array",
                "items": _enum_string(context.required_graph_review_request_ids),
                "minItems": len(context.required_graph_review_request_ids),
                "maxItems": len(context.required_graph_review_request_ids),
            },
            "reviewed_link_ids": {
                "type": "array",
                "items": _enum_string(reviewed_link_ids),
                "minItems": len(reviewed_link_ids),
                "maxItems": len(reviewed_link_ids),
            },
            "frontier_decisions": {
                "type": "array",
                "items": graph_frontier_decision,
                "minItems": len(batch_frontier_ids),
                "maxItems": len(selectable_frontier_ids),
            },
            "reason": {"type": "string"},
        }
    )
    known_ledger_article_ids = tuple(
        dict.fromkeys(item.article_id for item in context.graph_review_ledger)
    )
    open_work_item_ids = tuple(
        item.work_item_id for item in context.work_tree if item.state == "open"
    )
    open_hypothesis_ids = tuple(
        item.hypothesis_id
        for item in context.hypotheses
        if item.work_item_id in open_work_item_ids
    )
    frontier_re_adoption = _strict_object(
        {
            "article_id": _enum_string(known_ledger_article_ids),
            "work_item_id": _enum_string(open_work_item_ids),
            "hypothesis_id": _enum_string(open_hypothesis_ids),
            "reason": {"type": "string"},
        }
    )
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "next": {"type": "string", "enum": ["continue", "finalize"]},
            "start_next_cycle": {"type": "boolean"},
            "update_json": {
                "type": "string",
                "description": "CaseUpdate encoded as one JSON object string",
            },
            "next_focus_work_item_ids": string_array,
            "retain_evidence_ids": string_array,
            "tool_requests_json": {
                "type": "string",
                "description": "ToolRequest array encoded as one JSON array string",
            },
            "dependency_decisions": dependency_decisions,
            "graph_candidate_review": (
                graph_candidate_review
                if batch_candidates and not context.finalize_only
                else {"type": "null"}
            ),
            "frontier_re_adoptions": {
                "type": "array",
                "items": frontier_re_adoption,
                "maxItems": len(context.graph_review_ledger),
            },
            "answer": {"anyOf": [answer, {"type": "null"}]},
        },
        "required": [
            "next",
            "start_next_cycle",
            "update_json",
            "next_focus_work_item_ids",
            "retain_evidence_ids",
            "tool_requests_json",
            "dependency_decisions",
            "graph_candidate_review",
            "frontier_re_adoptions",
            "answer",
        ],
    }


def _strict_object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


def _bounded_enum_array(values: tuple[str, ...]) -> dict[str, Any]:
    items = _enum_string(values)
    return {
        "type": "array",
        "items": items,
        "maxItems": len(values),
    }


def _enum_string(values: tuple[str, ...]) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string"}
    if values:
        schema["enum"] = list(values)
    return schema


def _normalize_solver_payload(payload: dict) -> dict:
    decision_payload = payload.get("decision_json")
    if isinstance(decision_payload, str):
        try:
            decoded = json.loads(decision_payload)
        except json.JSONDecodeError as exc:
            raise ModelProtocolError(
                f"decision_json invalid_json_at_{exc.pos}"
            ) from exc
        if not isinstance(decoded, dict):
            raise ModelProtocolError("decision_json root must be an object")
    else:
        decoded = payload

    normalized = dict(decoded)
    if "update_json" in normalized:
        normalized["update"] = _decode_transport_json(
            normalized.pop("update_json"),
            expected_type=dict,
            label="update_json",
        )
    if "tool_requests_json" in normalized:
        normalized["tool_requests"] = _decode_transport_json(
            normalized.pop("tool_requests_json"),
            expected_type=list,
            label="tool_requests_json",
        )
    if "dependency_decisions_json" in normalized:
        normalized["dependency_decisions"] = _decode_transport_json(
            normalized.pop("dependency_decisions_json"),
            expected_type=list,
            label="dependency_decisions_json",
        )
    requests = []
    for raw_request in normalized.get("tool_requests") or []:
        if not isinstance(raw_request, dict):
            requests.append(raw_request)
            continue
        request = dict(raw_request)
        arguments = request.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise ModelProtocolError(
                    "tool arguments string is not valid JSON"
                ) from exc
        if not isinstance(arguments, dict):
            raise ModelProtocolError("tool arguments must decode to an object")
        request["arguments"] = arguments
        requests.append(request)
    normalized["tool_requests"] = requests
    return normalized


def _decode_transport_json(value: Any, *, expected_type: type, label: str) -> Any:
    if not isinstance(value, str):
        raise ModelProtocolError(f"{label} must be a JSON string")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ModelProtocolError(f"{label} invalid_json_at_{exc.pos}") from exc
    if not isinstance(decoded, expected_type):
        raise ModelProtocolError(f"{label} has an invalid root type")
    return decoded


_SOLVER_CONTRACT = """
復元後のSolverDecision契約:
{
  "next": "continue" | "finalize",
  "start_next_cycle": bool,
  "update": {
    "add_work_items": [{"work_item_id": str, "parent_work_item_id": str|null, "question": str, "state": "open"|"resolved"|"dropped", "resolution": str|null, "basis_hypothesis_ids": [str], "replaces_work_item_id": str|null}],
    "update_work_items": [{"work_item_id": str, "state": "open"|"resolved"|"dropped", "resolution": str|null, "basis_hypothesis_ids": [str]}],
    "add_hypotheses": [{"hypothesis_id": str, "work_item_id": str, "statement": str, "judgment": "supported"|"contradicted"|"unresolved", "evidence_ids": [str], "gaps": [str]}],
    "update_hypotheses": [{"hypothesis_id": str, "judgment": "supported"|"contradicted"|"unresolved", "evidence_ids": [str], "gaps": [str]}],
    "impact_decisions": [{"work_item_id": str, "action": "retain"|"replace"|"drop", "reason": str, "new_basis_hypothesis_ids": [str], "replacement_work_item_id": str|null, "drop_subtree": bool}]
  },
  "next_focus_work_item_ids": [str],
  "retain_evidence_ids": [str],
  "dependency_decisions": [{"dependency_kind": str, "work_item_id": str, "status": "not_required"|"needs_action"|"resolved", "reason": str, "source_evidence_ids": [str], "action": "discover_source"|"assess_source"|"discover_target"|"fetch_target"|null, "action_request_id": str|null, "target_article_ids": [str], "evidence_ids": [str]}],
  "graph_candidate_review": {"graph_request_ids": [str], "reviewed_link_ids": [str], "frontier_decisions": [{"frontier_item_id": str, "article_id": str, "work_item_id": str, "hypothesis_id": str|null, "action": "select"|"defer"|"reject", "reason": str}], "reason": str} | null,
  "frontier_re_adoptions": [{"article_id": str, "work_item_id": str, "hypothesis_id": str, "reason": str}],
  "tool_requests": [{"request_id": str, "work_item_id": str, "tool_name": str, "arguments": object, "purpose": str, "hypothesis_ids": [str]}],
  "answer": {"text": str, "citation_ids": [str], "limitations": [str]} | null
}
continueは原則1件以上のtool_requests、graph_candidate_review、またはfrontier_re_adoptionsとanswer=nullを持つ。Graph候補選別判断は選択の有無にかかわらずtool_requests=[]とし、selectした既知Article IDはAgentLoopが本文取得へ機械転記する。finalizeはtool_requests=[]とanswerを持つ。省略可能な配列は空配列、updateは空objectにできる。
輸送時だけupdate全体をupdate_jsonへ、tool_requests配列全体をtool_requests_jsonへJSON文字列化する。dependency_decisionsはProvider schemaの構造化配列として直接返し、answer本文も二重エンコードしない。

契約語彙:
- next: continueは追加Toolを実行して判断を継続する。finalizeは追加Toolなしで現在の確認済み範囲から回答を確定する。
- start_next_cycle: falseは現在の仮説・探索方針の同じCycleで次のaction-observation stepへ進む。trueは現在の方針を評価して閉じ、更新した仮説・方針で次Cycleを開始する。初回Tool実行はfalseでもCycle 1を開始する。単なる検索、本文取得、Graph候補選別ではfalseとし、初期の作業分解・仮説・探索方針を仕切り直す場合だけtrueにする。Graph候補選別モードでは必ずfalse。
- WorkItem.state: openは未完了で追加作業が必要。resolvedは問いへ結論が出てresolutionにその結論を書く。droppedは前提否定・重複・質問との無関係により作業対象から外し、resolutionに除外理由を書く。取得失敗だけを理由にresolvedへしない。
- WorkItemのparent_work_item_idは包含する親作業、basis_hypothesis_idsはその作業を成立させる前提、replaces_work_item_idは置換した旧作業を表す。next_focus_work_item_idsには次サイクルでTool対象にするopen WorkItemだけを入れる。
- Hypothesis.judgment: supportedは提示された根拠がstatementを支持する。contradictedは提示された根拠がstatementを否定する。unresolvedは根拠不足・両義的・未確認で、真偽を確定していない。supportedでもWorkItem全体が完了したとは限らない。
- 1つのHypothesisは独立に検証できる1つの命題とする。適用要件、数値基準、例外、義務・手続など別の根拠で判断できる観点を束ねず、別の観点のEvidenceで未確認の観点を完了しない。
- Hypothesis.gapsはunresolvedの理由または追加確認事項であり、limitationsは最終回答に残る制約である。調査可能なgapsをlimitationsへ移すだけで完了扱いにしない。
- impact_decisions.action: retainはWorkItemを維持し、否定された前提をnew_basis_hypothesis_idsへ差し替える。replaceは旧WorkItemをdroppedにして、同じupdateで追加するreplacement_work_item_idへ置き換える。dropはWorkItemを不要として閉じ、drop_subtree=trueなら未完了の子孫も閉じる。これはHypothesisを新たにcontradictedへ変更したことで影響を受けるopen WorkItemにだけ使う。
- ToolResult.status: succeededはTool実行が完了した状態であり、得た内容が質問を立証したという意味ではない。failedはToolがエラー終了、timeoutは制限時間内に完了しなかった状態。failed/timeoutではerror_codeを確認し、Evidenceがないことを不存在の根拠にしない。
- Graph ToolResultのgraph_projection_updated=trueは、取得したGraph情報がCaseStoreへ保存され、差分batchまたはledgerへ投影可能になった実行事実であり、候補の関連性や本文確認済みを意味しない。
- Graph Reviewは同じSolverがGraph候補の関連性と本文取得順を判断する処理モードであり、任意のReviewer Agentとは別である。Reviewer無効時も必要なGraph Reviewを行う。
- frontier review statusのunreviewedは現在のHypothesisについて未評価、selectedはSolverが本文取得対象に選択、relevant_deferredは関連ありだが取得枠外、rejectedは現在の質問・Hypothesisに不要との判断を示す。selectedは本文取得成功を意味せず、content_statusと混同しない。
- finalize_only: falseなら必要な追加調査をcontinueできる。trueなら上限到達後の最終化呼出しなので、追加Toolを要求せず確認済み範囲と未確認範囲を分けてfinalizeする。
- material_included: trueのEvidenceだけ本文がmaterial_evidenceに提示されている。falseはmanifest情報だけで本文未提示。
- graph_review_batchは今回判断が必要な新規・再採用・新Link差分だけを示す。graph_review_ledgerは過去の全評価済みfrontierの短い最新状態であり、全Graph履歴の消失を意味しない。Graph navigation Evidenceはこれらの投影がSolver向けの唯一表示で、manifest等へ重複掲載されない。
- graph_review_batchのreview_triggerはnew_frontier=新規、re_adopted=別Hypothesisへの明示的再採用、new_link=既評価候補への新経路追加を表す。new_linkではprior_review_statusを前提にせず、今回提示された全Linkを含めて判断を更新する。
- batch内の全frontierへselect/defer/rejectを返す。selectは質問とHypothesisに関係し今回本文取得、deferは関係するが今回の枠外、rejectは関係しないとの意味判断である。表示順や枠外を理由にrejectしない。選択はmax_selected_frontier_per_stepとremaining_fetch_capacityの小さい方までとする。
- graph_review_ledgerのrelevant_deferredは後続stepまたは次Cycleでselectできる。selectedかつfailed/timeoutは取得再試行だけを判断できる。rejectedを別Hypothesisで使う場合はfrontier_re_adoptionsに既知Article・open WorkItem・所属Hypothesis・理由を明示し、プログラムへ自動転用を要求しない。
- content_statusは本文取得状態である。not_requestedは未要求、pendingは結果待ち、succeededは取得成功、failedはエラー終了、timeoutは時間切れを示し、法的関連性や根拠採用を意味しない。
- Graph探索の上限は1ホップである。Graph候補Articleの本文は取得・評価できるが、そのArticleからGraphを再展開しない。その先の確認が必要ならSolverがlegal_searchを要求する。
- grounding_evidence_idsは意味判断と引用に使える本文Evidence、navigation_evidence_idsはGraph以外の候補発見専用Evidence、fetchable_article_idsはfetch_articlesへ完全一致で渡せるArticle IDである。
- search_navigationの本文抜粋は次のTool選択専用であり、Hypothesisのjudgment、WorkItemのresolution、確認済みの主張、answerの根拠に使わない。必要な命題がsearch_navigationにしかなければunresolved/openを維持し、Article本文を取得する。
- dependency_decisions.status: not_requiredは当該依存確認が回答に不要、needs_actionは必要だが追加Toolが必要、resolvedは依存先本文まで確認済み。actionはdiscover_source=依存元候補の発見、assess_source=依存元本文の確認、discover_target=依存先候補の発見、fetch_target=既知依存先本文の取得を表す。required_dependency_kind=nullならこの契約は使わずdependency_decisions=[]とする。

ID用途契約:
- update_hypotheses.evidence_idsとanswer.citation_idsは、solver_context.grounding_evidence_idsから完全一致でコピーする。
- retain_evidence_idsとload_evidenceのevidence_idsは、solver_context.evidence_manifestのevidence_idから完全一致でコピーする。
- fetch_articlesのarticle_idsは、solver_context.fetchable_article_idsから完全一致でコピーする。
- 本文中の条番号が必要でも対応IDがfetchable_article_idsになければ、IDを作らずfetch_articlesから外し、法令名・条番号・確認事項をqueryにしたlegal_searchを使う。
- articleIdとevidenceIdは別の名前空間である。articleIdへparagraph/item等を付加してevidenceIdを生成・推測しない。
- solver_context.navigation_evidence_idsは検索経路候補であり、Hypothesisの根拠やcitation_idsには使わない。
- supported/contradictedのevidence_idsはstatementを直接支持または否定するEvidenceに限る。特定条文の内容をresolutionやanswerで説明するにはそのArticleのgrounding Evidenceが必要であり、別Article、search_navigation、Graph候補で代用しない。
- finalize時は、resolved WorkItemのbasis_hypothesis_idsが参照するHypothesisのevidence_idsをanswer.citation_idsへ含める。これはSolver自身が宣言した解決根拠と回答引用の参照整合である。
- retain_evidence_idsは全取得結果の列挙欄ではない。次回以降も本文が必要なEvidenceだけをSolverが選び、solver_context.max_retained_evidence以下にする。
- tool_requestsはsolver_context.max_tool_requests_per_step以下にする。これは1 Solver Decisionの上限であり、Cycle累計本文数ではない。プログラムは超過分を選別・切捨てしない。
- 1 Cycleの本文取得成功数はmax_fetched_resources_per_cycle、残りはremaining_fetch_capacityである。cycle_close_required=trueなら現Cycleへ新しいToolを追加せず、取得済み結果を評価する。can_start_next_cycle=trueなら次の命題・方針を示してstart_next_cycleを選べるが、falseならfinalizeする。
- 同じDecisionで取得する既知Article IDは、WorkItemが異なっても4個以内なら1つのfetch_articlesへ統合する。4個は上限であり目標ではない。WorkItemごとにRequestを分けて自動1ホップGraphを重複実行しない。
- 1つのDecisionにfetch_articlesを複数返さず、Article IDを重複させない。4個を超える候補はプログラムに統合・切捨てさせず、Solverが今回検証する4個以下を選ぶ。
- solver_context.required_dependency_kindがnullならdependency_decisions=[]とする。nullでなければrequired_dependency_work_item_idsと同じ件数を返し、各IDをちょうど1回使う。dependency_kindはrequired_dependency_kindへ一致させる。
- 判断対象となった委任元・依存元本文をsource_evidence_idsへ指定する。委任元候補自体が未発見ならneeds_actionのdiscover_sourceだけはsource_evidence_ids=[]とし、同じDecision内のlegal_searchを参照できる。委任元本文の取得ならassess_source、接続先探索ならdiscover_target、接続先本文取得ならfetch_targetをactionへ指定する。fetch_targetでは一括Request中のどれを委任先として取得するかtarget_article_idsへ指定する。resolvedでも確認済み委任先をtarget_article_idsへ、その本文をevidence_idsへ指定する。resolvedのevidence_idsは委任元と異なる文書のsolver_context.grounding_evidence_idsだけを使う。
""".strip()
