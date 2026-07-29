"""LLM主導の法令調査で使う、1ターン分の判断契約と証拠境界。

法的な探索手順はLLMへ委ねる。一方、検索対象、利用可能なツール、引用可能な証拠ID、
呼び出し回数などの技術的・安全上の境界はコードで検証する。

このモジュールは既存のルール主導探索から独立しており、準備段階では検索ループへ接続しない。
"""

import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

RESEARCH_STATUS_CONTINUE = "continue"
RESEARCH_STATUS_READY = "ready"
RESEARCH_STATUS_INSUFFICIENT = "insufficient"

TOOL_SEARCH_CORPUS = "search_corpus"
TOOL_FETCH_ARTICLES = "fetch_articles"
TOOL_EXPAND_GRAPH = "expand_graph"

ResearchStatus = Literal["continue", "ready", "insufficient"]
ResearchToolName = Literal["search_corpus", "fetch_articles", "expand_graph"]
ResearchDocType = Literal["law", "guideline"]


class ResearchAction(BaseModel):
    """LLMが次に要求できる、データソース内の調査操作1件。"""

    model_config = ConfigDict(extra="forbid")

    tool: ResearchToolName
    query: str | None = None
    articleIds: list[str] = Field(default_factory=list, max_length=20)
    documentIds: list[str] = Field(default_factory=list, max_length=10)
    docTypes: list[ResearchDocType] = Field(default_factory=list, max_length=2)
    edgeTypes: list[str] = Field(default_factory=list, max_length=10)
    reason: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_tool_arguments(self) -> "ResearchAction":
        if self.tool == TOOL_SEARCH_CORPUS and not (self.query or "").strip():
            raise ValueError("search_corpus requires query")
        if self.tool in {TOOL_FETCH_ARTICLES, TOOL_EXPAND_GRAPH} and not self.articleIds:
            raise ValueError(f"{self.tool} requires articleIds")
        return self


class ResearchEvidenceSelection(BaseModel):
    """最終回答の根拠としてLLMが選んだ、提示済みcontent unit。"""

    model_config = ConfigDict(extra="forbid")

    contentUnitId: str = Field(min_length=1, max_length=500)
    reason: str = Field(default="", max_length=500)


class ResearchTurn(BaseModel):
    """LLM主導調査の1ターン分の構造化出力。"""

    model_config = ConfigDict(extra="forbid")

    status: ResearchStatus
    actions: list[ResearchAction] = Field(default_factory=list, max_length=8)
    selectedEvidence: list[ResearchEvidenceSelection] = Field(
        default_factory=list, max_length=24
    )
    missingEvidence: list[str] = Field(default_factory=list, max_length=12)
    reason: str = Field(default="", max_length=1000)


@dataclass(frozen=True)
class ResearchTurnValidation:
    """LLM判断を検索ループへ適用できるかの決定的な検証結果。"""

    valid: bool
    errors: tuple[str, ...] = ()
    selected_content_unit_ids: tuple[str, ...] = ()

    def as_trace(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": list(self.errors),
            "selectedContentUnitIds": list(self.selected_content_unit_ids),
        }


class EvidenceCatalog:
    """LLMへ実際に提示した証拠と、参照可能なArticle IDを管理する。

    LLMの学習済み知識や推測で生成したIDを引用・直接取得へ使わせないため、選択可能な
    contentUnitIdとArticle IDはこのカタログに登録済みのものへ限定する。
    """

    def __init__(self) -> None:
        self._evidence_by_content_id: dict[str, dict[str, Any]] = {}
        self._known_article_ids: set[str] = set()
        self._known_documents: dict[str, str] = {}

    @property
    def content_unit_ids(self) -> tuple[str, ...]:
        return tuple(self._evidence_by_content_id)

    @property
    def known_article_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._known_article_ids))

    @property
    def known_document_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._known_documents))

    def add_documents(self, titles_by_document_id: dict[str, str]) -> None:
        """検索基盤で確認できた文書IDと題名を、LLMの検索スコープ候補へ登録する。"""
        for document_id, title in titles_by_document_id.items():
            if document_id:
                self._known_documents[str(document_id)] = str(title or "")

    def prompt_documents(self) -> list[dict[str, str]]:
        return [
            {
                "documentId": document_id,
                "title": self._known_documents[document_id],
            }
            for document_id in self.known_document_ids
        ]

    def items_by_ids(self, content_unit_ids: tuple[str, ...] | list[str]) -> list[dict[str, Any]]:
        """LLMが選択した順序を保って、検証済みの証拠本文を返す。"""
        return [
            dict(self._evidence_by_content_id[content_unit_id])
            for content_unit_id in content_unit_ids
            if content_unit_id in self._evidence_by_content_id
        ]

    def add_results(self, results: list[dict[str, Any]]) -> int:
        """既存search/direct lookup形式を正規化してカタログへ追加する。"""
        added = 0
        for item in results:
            source = _result_source(item)
            content_unit_id = str(source.get("contentUnitId") or "")
            if not content_unit_id:
                continue
            article_id = _article_id(source)
            normalized = {
                "contentUnitId": content_unit_id,
                "articleId": article_id or None,
                "documentId": source.get("documentId"),
                "docType": source.get("docType"),
                "title": source.get("title"),
                "heading": source.get("heading"),
                "sourceObjectUri": source.get("sourceObjectUri"),
                "sourcePage": source.get("sourcePage"),
                "text": str(source.get("text") or ""),
            }
            if content_unit_id not in self._evidence_by_content_id:
                added += 1
                self._evidence_by_content_id[content_unit_id] = normalized
            else:
                current = self._evidence_by_content_id[content_unit_id]
                for key, value in normalized.items():
                    if value not in (None, ""):
                        current[key] = value
            if article_id:
                self._known_article_ids.add(article_id)
            document_id = str(source.get("documentId") or "")
            if document_id:
                self._known_documents.setdefault(
                    document_id, str(source.get("title") or "")
                )
        return added

    def add_graph_paths(self, paths: list[dict[str, Any]]) -> int:
        """Graphで確認できたArticle IDを、次ターンの直接取得候補として登録する。"""
        before = len(self._known_article_ids)
        for path in paths:
            for node in path.get("nodes") or []:
                article_id = str(
                    node.get("articleContentUnitId")
                    or node.get("contentUnitId")
                    or node.get("graphNodeId")
                    or ""
                )
                if "-article-" in article_id:
                    self._known_article_ids.add(
                        article_id.split("-paragraph-", 1)[0]
                    )
        return len(self._known_article_ids) - before

    def prompt_inventory(self, *, max_items: int = 100) -> list[dict[str, Any]]:
        """本文予算から漏れた候補も、ID・条見出し・短い冒頭でLLMへ知らせる。"""
        return [
            {
                "contentUnitId": item.get("contentUnitId"),
                "articleId": item.get("articleId"),
                "documentId": item.get("documentId"),
                "docType": item.get("docType"),
                "title": item.get("title"),
                "heading": item.get("heading"),
                "textPreview": str(item.get("text") or "")[:160],
            }
            for item in self._ordered_prompt_items()[: max(0, max_items)]
        ]

    def prompt_items(
        self,
        *,
        max_items: int,
        max_chars: int,
        preferred_content_ids: tuple[str, ...] | list[str] = (),
    ) -> list[dict[str, Any]]:
        """文字予算内で、LLMが選択可能な証拠を決定的な順序で返す。"""
        remaining = max(0, max_chars)
        output: list[dict[str, Any]] = []
        ordered = self._ordered_prompt_items(preferred_content_ids)
        for item in ordered[: max(0, max_items)]:
            if remaining <= 0:
                break
            metadata_chars = sum(
                len(str(item.get(key) or ""))
                for key in (
                    "contentUnitId",
                    "articleId",
                    "documentId",
                    "title",
                    "heading",
                )
            )
            text_budget = max(0, remaining - metadata_chars)
            prompt_item = {
                **item,
                "text": str(item.get("text") or "")[:text_budget],
            }
            used = metadata_chars + len(prompt_item["text"])
            if used <= 0:
                continue
            output.append(prompt_item)
            remaining -= used
        return output

    def _ordered_prompt_items(
        self,
        preferred_content_ids: tuple[str, ...] | list[str] = (),
    ) -> list[dict[str, Any]]:
        """選択済み根拠を優先し、残りは文書ごとにラウンドロビンする。

        法令内検索を高再現率にすると、最初に検索した法律だけで表示上限を
        使い切りやすい。法的な優先順位は付けず、取得順を各文書内で保ったまま
        法律・政令・府令・ガイドを公平にLLMへ提示する。
        """
        preferred = [
            self._evidence_by_content_id[content_unit_id]
            for content_unit_id in dict.fromkeys(preferred_content_ids)
            if content_unit_id in self._evidence_by_content_id
        ]
        preferred_ids = {
            str(item.get("contentUnitId") or "") for item in preferred
        }
        groups: dict[str, list[dict[str, Any]]] = {}
        for item in self._evidence_by_content_id.values():
            if str(item.get("contentUnitId") or "") in preferred_ids:
                continue
            document_id = str(item.get("documentId") or "__unknown__")
            groups.setdefault(document_id, []).append(item)

        diversified: list[dict[str, Any]] = []
        offsets = {document_id: 0 for document_id in groups}
        while True:
            added = False
            for document_id, items in groups.items():
                offset = offsets[document_id]
                if offset >= len(items):
                    continue
                diversified.append(items[offset])
                offsets[document_id] = offset + 1
                added = True
            if not added:
                break
        return [*preferred, *diversified]


def validate_research_turn(
    turn: ResearchTurn,
    catalog: EvidenceCatalog,
    *,
    finalize_only: bool = False,
) -> ResearchTurnValidation:
    """状態整合性と、LLMが参照したIDの出所だけを検証する。

    法的に十分か、どの検索順がよいかはここでは再判定しない。
    """
    errors: list[str] = []
    visible_content_ids = set(catalog.content_unit_ids)
    known_article_ids = set(catalog.known_article_ids)
    known_document_ids = set(catalog.known_document_ids)
    selected_ids = tuple(
        dict.fromkeys(item.contentUnitId for item in turn.selectedEvidence)
    )

    for content_unit_id in selected_ids:
        if content_unit_id not in visible_content_ids:
            errors.append(f"unknown_evidence_id:{content_unit_id}")

    for index, action in enumerate(turn.actions):
        for document_id in action.documentIds:
            if document_id not in known_document_ids:
                errors.append(
                    f"unknown_document_id:actions[{index}]:{document_id}"
                )
        if action.tool in {TOOL_FETCH_ARTICLES, TOOL_EXPAND_GRAPH}:
            for article_id in action.articleIds:
                if article_id not in known_article_ids:
                    errors.append(
                        f"unknown_article_id:actions[{index}]:{article_id}"
                    )

    if turn.status == RESEARCH_STATUS_CONTINUE and not turn.actions:
        errors.append("continue_requires_action")
    if turn.status == RESEARCH_STATUS_READY:
        if turn.actions:
            errors.append("ready_must_not_request_actions")
        if not selected_ids:
            errors.append("ready_requires_selected_evidence")
    if turn.status == RESEARCH_STATUS_INSUFFICIENT and turn.actions:
        errors.append("insufficient_must_not_request_actions")
    if finalize_only:
        if turn.status == RESEARCH_STATUS_CONTINUE:
            errors.append("finalize_requires_terminal_status")
        if turn.actions:
            errors.append("finalize_must_not_request_actions")

    return ResearchTurnValidation(
        valid=not errors,
        errors=tuple(dict.fromkeys(errors)),
        selected_content_unit_ids=tuple(
            content_unit_id
            for content_unit_id in selected_ids
            if content_unit_id in visible_content_ids
        ),
    )


def build_research_turn_prompt(
    *,
    question: str,
    choices: dict[str, str] | None,
    catalog: EvidenceCatalog,
    tool_history: list[dict[str, Any]] | None,
    max_actions: int,
    max_evidence_items: int,
    evidence_chars: int,
    remaining_turns: int | None = None,
    remaining_tool_calls: int | None = None,
    finalize_only: bool = False,
    max_selected_evidence: int = 16,
    preferred_content_ids: tuple[str, ...] | list[str] = (),
) -> str:
    """探索手順を細かく規定せず、目的・利用可能ツール・証拠境界だけを伝える。"""
    choices_block = ""
    if choices:
        choices_block = "\n選択肢:\n" + "\n".join(
            f"{label.upper()}: {text}" for label, text in sorted(choices.items())
        )
    evidence = catalog.prompt_items(
        max_items=max_evidence_items,
        max_chars=evidence_chars,
        preferred_content_ids=preferred_content_ids,
    )
    # 最終判断では新しい候補を探索できないため、本文の無い候補一覧を重ねて
    # 入力を膨らませず、直前に選択した根拠本文を優先して提示する。
    inventory = (
        []
        if finalize_only
        else catalog.prompt_inventory(max_items=max_evidence_items)
    )
    budget_notice = "\n".join(
        [
            f"残り判断ターン数: {remaining_turns}"
            if remaining_turns is not None
            else "",
            f"残りツール呼び出し数: {remaining_tool_calls}"
            if remaining_tool_calls is not None
            else "",
        ]
    ).strip()
    finalization_notice = ""
    if finalize_only:
        finalization_notice = """
これは最終判断ターンです。追加検索はできません。
actionsは必ず空配列にし、statusはreadyまたはinsufficientのどちらかにしてください。
確認済み本文から質問の中心部分を回答できるならreadyとし、最終回答に使う根拠を
selectedEvidenceへ指定してください。中心部分の根拠が足りない場合はinsufficientとし、
それでも限定回答に使える確認済み根拠はselectedEvidenceへ残してください。
"""
    prompt_history = _prompt_tool_history(
        tool_history or [],
        finalize_only=finalize_only,
    )
    return f"""あなたは、日本法令について根拠を収集する調査責任者です。
プログラムは検索と本文取得だけを担当します。法的な論点、検索語、候補の比較、
条文間のつながり、追加調査の要否、最終根拠の選択はあなたが判断してください。
調査方法、検索語、探索順序はあなたが判断してください。
固定の法的役割や検索順に質問を当てはめず、質問と取得本文から必要な調査を組み立ててください。

使用できる操作:
- search_corpus: このシステムに投入済みの法令・ガイドを検索する
- fetch_articles: 既知のArticle IDから本文を直接取得する
- expand_graph: 既知のArticle IDから確認済みの法令関係を辿る

制約:
- 学習済み知識は検索語や仮説を考えるために使えるが、法的結論の根拠にはしない
- 結論ごとに、実際に取得した法令本文で根拠を確認する
- 複数法令、委任先、定義、準用、例外、適用範囲が関係し得るときは、必要性を自ら検討する
- ガイドは法令本文ではない。法令間のつながりを探す手掛かりや行政解釈として区別する
- 質問と似ているだけの条文を、必要な根拠が揃った証拠とみなさない
- selectedEvidenceには、利用可能な証拠にあるcontentUnitIdだけを指定する
- selectedEvidenceには、最終回答で実際に根拠として使う本文だけを理由付きで指定する
- selectedEvidenceは最大{max_selected_evidence}件とし、同じ結論を支える重複項号を
  網羅的に並べず、回答に必要な最小限の本文を選ぶ
- 取得できていないArticle IDを推測してfetch_articlesやexpand_graphへ渡さない
- documentIdsを指定する場合は、利用可能な文書にあるIDだけを使用する
- 質問の中心的な結論を取得済み法令で説明できればreadyとする。考え得る全ての例外、
  周辺制度、質問が求めていない手続まで網羅する必要はない
- 未確認事項が中心的な結論を変えず、回答上の留保として明示できる場合はreadyとする
- search_corpusはArticleの代表本文を返す。候補Articleの他の項・号を確認する場合は、
  類似した検索を繰り返さずfetch_articlesを使う
- 根拠が十分ならready、不足を具体化して追加調査できるならcontinue、
  予算内では根拠を確認できないならinsufficientとする
- insufficientでも、確認済みで回答の限定に役立つ証拠があればselectedEvidenceへ含める
- 1ターンのactionsは最大{max_actions}件とする
- 必ずJSONだけを返す

{budget_notice}
{finalization_notice}

質問: {question}{choices_block}

これまでの操作:
{json.dumps(prompt_history, ensure_ascii=False)}

利用可能な文書（法令名を特定できる場合はsearch_corpusのdocumentIdsで限定できる）:
{json.dumps(catalog.prompt_documents(), ensure_ascii=False)}

候補一覧（本文が省略された候補はArticle IDをfetch_articlesして確認できる）:
{json.dumps(inventory, ensure_ascii=False)}

本文を確認できる証拠:
{json.dumps(evidence, ensure_ascii=False)}

JSON:"""


def _prompt_tool_history(
    tool_history: list[dict[str, Any]],
    *,
    finalize_only: bool,
) -> list[dict[str, Any]]:
    """最終ターンでは判断に必要な履歴だけを残し、本文の再提示を優先する。"""
    if not finalize_only:
        return tool_history

    compact: list[dict[str, Any]] = []
    for item in tool_history:
        turn_index = item.get("turnIndex")
        decision = item.get("decision")
        if isinstance(decision, dict):
            compact.append(
                {
                    "turnIndex": turn_index,
                    "decision": {
                        "status": decision.get("status"),
                        "reason": str(decision.get("reason") or "")[:500],
                        "missingEvidence": decision.get("missingEvidence") or [],
                        "selectedEvidence": decision.get("selectedEvidence") or [],
                    },
                }
            )
            continue
        if item.get("validationErrors"):
            compact.append(
                {
                    "turnIndex": turn_index,
                    "validationErrors": item.get("validationErrors"),
                }
            )
            continue
        compact.append(
            {
                "turnIndex": turn_index,
                "tool": item.get("tool"),
                "articleIds": item.get("articleIds") or [],
                "documentIds": item.get("documentIds") or [],
                "resultCount": item.get("resultCount"),
                "newEvidenceCount": item.get("newEvidenceCount"),
                "newArticleCount": item.get("newArticleCount"),
                "newArticleIds": item.get("newArticleIds") or [],
                "autoGraphArticleIds": item.get("autoGraphArticleIds") or [],
                "error": item.get("error"),
                "autoGraphError": item.get("autoGraphError"),
            }
        )
    return compact


def research_turn_json_schema(
    *,
    max_actions: int,
    max_selected_evidence: int,
    finalize_only: bool = False,
) -> dict[str, Any]:
    """Ollama/Anthropic共通の構造化出力スキーマ。"""
    action_schema = {
        "type": "object",
        "properties": {
            "tool": {
                "type": "string",
                "enum": [
                    TOOL_SEARCH_CORPUS,
                    TOOL_FETCH_ARTICLES,
                    TOOL_EXPAND_GRAPH,
                ],
            },
            "query": {"type": ["string", "null"]},
            "articleIds": {"type": "array", "items": {"type": "string"}},
            "documentIds": {"type": "array", "items": {"type": "string"}},
            "docTypes": {
                "type": "array",
                "items": {"type": "string", "enum": ["law", "guideline"]},
            },
            "edgeTypes": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
        },
        "required": [
            "tool",
            "query",
            "articleIds",
            "documentIds",
            "docTypes",
            "edgeTypes",
            "reason",
        ],
        "additionalProperties": False,
    }
    evidence_schema = {
        "type": "object",
        "properties": {
            "contentUnitId": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["contentUnitId", "reason"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": (
                    [
                        RESEARCH_STATUS_READY,
                        RESEARCH_STATUS_INSUFFICIENT,
                    ]
                    if finalize_only
                    else [
                        RESEARCH_STATUS_CONTINUE,
                        RESEARCH_STATUS_READY,
                        RESEARCH_STATUS_INSUFFICIENT,
                    ]
                ),
            },
            "actions": {
                "type": "array",
                "items": action_schema,
                "maxItems": max_actions,
            },
            "selectedEvidence": {
                "type": "array",
                "items": evidence_schema,
                "maxItems": max_selected_evidence,
            },
            "missingEvidence": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 12,
            },
            "reason": {"type": "string"},
        },
        "required": [
            "status",
            "actions",
            "selectedEvidence",
            "missingEvidence",
            "reason",
        ],
        "additionalProperties": False,
    }


def parse_research_turn(
    raw_text: str,
    *,
    max_actions: int,
    max_selected_evidence: int,
) -> tuple[ResearchTurn | None, str | None]:
    """構造化出力を検証し、プロバイダ側で除去される配列上限もコードで強制する。"""
    try:
        payload = json.loads(raw_text)
        turn = ResearchTurn.model_validate(payload)
        if len(turn.actions) > max_actions:
            raise ValueError(f"actions exceeds max_actions={max_actions}")
        if len(turn.selectedEvidence) > max_selected_evidence:
            raise ValueError(
                "selectedEvidence exceeds "
                f"max_selected_evidence={max_selected_evidence}"
            )
        return turn, None
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _result_source(item: dict[str, Any]) -> dict[str, Any]:
    source = item.get("document") or item.get("source") or item
    return source if isinstance(source, dict) else {}


def _article_id(source: dict[str, Any]) -> str:
    article_id = str(
        source.get("articleContentUnitId")
        or source.get("parentContentUnitId")
        or ""
    )
    if article_id:
        return article_id.split("-paragraph-", 1)[0]
    content_unit_id = str(source.get("contentUnitId") or "")
    if "-article-" not in content_unit_id:
        return ""
    return content_unit_id.split("-paragraph-", 1)[0]
