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

AUTHORITY_NODE_EVIDENCE_MAX = 20
CHECKPOINT_NEXT_ARTICLE_MAX = 10

TOOL_SEARCH_CORPUS = "search_corpus"
TOOL_FETCH_ARTICLES = "fetch_articles"
TOOL_EXPAND_GRAPH = "expand_graph"

ResearchStatus = Literal["continue", "ready", "insufficient"]
ResearchToolName = Literal["search_corpus", "fetch_articles", "expand_graph"]
ResearchDocType = Literal["law", "guideline"]
ResearchClaimStatus = Literal["verified", "partial", "unresolved"]
ResearchVerificationStatus = Literal[
    "text_verified",
    "graph_verified",
    "text_not_fetched",
    "unverified",
]
ResearchFollowUpAction = Literal[
    "search",
    "fetch_article",
    "expand_graph",
    "verify_text",
]


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


class ResearchAuthorityNode(BaseModel):
    """同じ論点に属する結論から共有される法令・ガイドの根拠ノード。"""

    model_config = ConfigDict(extra="forbid")

    nodeId: str = Field(min_length=1, max_length=80)
    articleId: str | None = Field(default=None, max_length=500)
    title: str = Field(default="", max_length=120)
    legalRole: str = Field(default="", max_length=120)
    verificationStatus: ResearchVerificationStatus
    evidenceIds: list[str] = Field(
        default_factory=list,
        max_length=AUTHORITY_NODE_EVIDENCE_MAX,
    )
    parentNodeId: str | None = Field(default=None, max_length=80)
    relationFromParent: str | None = Field(default=None, max_length=60)
    purpose: str = Field(default="", max_length=160)


class ResearchClaimStructure(BaseModel):
    """質問への結論1件と、論点共通レジストリにある根拠ノードへの参照。"""

    model_config = ConfigDict(extra="forbid")

    claimId: str = Field(min_length=1, max_length=80)
    question: str = Field(default="", max_length=140)
    conclusion: str = Field(default="", max_length=300)
    status: ResearchClaimStatus
    authorityNodeIds: list[str] = Field(
        default_factory=list,
        max_length=12,
    )


class ResearchIssueStructure(BaseModel):
    """論点単位で共有する根拠DAGと、その根拠を参照する結論群。"""

    model_config = ConfigDict(extra="forbid")

    issueId: str = Field(min_length=1, max_length=80)
    question: str = Field(default="", max_length=160)
    status: ResearchClaimStatus
    authorityNodes: list[ResearchAuthorityNode] = Field(
        default_factory=list,
        max_length=20,
    )
    claims: list[ResearchClaimStructure] = Field(
        default_factory=list,
        max_length=8,
    )


class ResearchUnresolvedItem(BaseModel):
    """次サイクルで確認する、論理構造上の不足。"""

    model_config = ConfigDict(extra="forbid")

    issueId: str = Field(min_length=1, max_length=80)
    claimId: str | None = Field(default=None, max_length=80)
    articleId: str | None = Field(default=None, max_length=500)
    action: ResearchFollowUpAction
    reason: str = Field(min_length=1, max_length=180)
    affectsCoreConclusion: bool = True


class ResearchLogicalStructure(BaseModel):
    """論点→結論→根拠ノードの階層と、未確認事項を保持する。

    法令関係は木ではなくDAGになり得るため、根拠本文を入れ子で複製せず、
    Issue単位のauthorityNodesを共有レジストリとし、parentNodeIdで論点内の
    根拠階層を表現する。各ClaimはauthorityNodeIdsで同じノードを共有参照する。
    """

    model_config = ConfigDict(extra="forbid")

    issues: list[ResearchIssueStructure] = Field(
        default_factory=list,
        max_length=4,
    )
    unresolved: list[ResearchUnresolvedItem] = Field(
        default_factory=list,
        max_length=6,
    )

    @model_validator(mode="after")
    def validate_compact_size(self) -> "ResearchLogicalStructure":
        claims = sum(len(issue.claims) for issue in self.issues)
        nodes = sum(len(issue.authorityNodes) for issue in self.issues)
        if claims > 8:
            raise ValueError("logicalStructure supports at most 8 claims")
        if nodes > 20:
            raise ValueError("logicalStructure supports at most 20 authority nodes")
        return self


class ResearchCheckpoint(BaseModel):
    """次サイクルへ渡す、結論と法的論理構造の小さな調査状態。"""

    model_config = ConfigDict(extra="forbid")

    status: ResearchStatus
    conclusion: str = Field(default="", max_length=1200)
    evidenceIds: list[str] = Field(
        default_factory=list,
        max_length=10,
    )
    openEvidenceIds: list[str] = Field(
        default_factory=list,
        max_length=20,
    )
    nextQuestions: list[str] = Field(default_factory=list, max_length=3)
    nextArticleIds: list[str] = Field(
        default_factory=list,
        max_length=CHECKPOINT_NEXT_ARTICLE_MAX,
    )
    logicalStructure: ResearchLogicalStructure = Field(
        default_factory=ResearchLogicalStructure
    )


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


@dataclass(frozen=True)
class ResearchCheckpointValidation:
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
        self._graph_relations: dict[
            tuple[str, str, str], dict[str, Any]
        ] = {}

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

    def content_ids_for_article_ids(
        self,
        article_ids: tuple[str, ...] | list[str] | set[str],
    ) -> tuple[str, ...]:
        """取得済みArticleに属する原文IDを、カタログ登録順で返す。"""
        wanted = {str(article_id) for article_id in article_ids if article_id}
        return tuple(
            content_unit_id
            for content_unit_id, item in self._evidence_by_content_id.items()
            if str(item.get("articleId") or "") in wanted
        )

    def diversify_content_ids(
        self,
        content_unit_ids: tuple[str, ...] | list[str],
    ) -> tuple[str, ...]:
        """同一Articleの長い項号列が、他Articleの本文を押し出さない順序へする。"""
        groups: dict[str, list[str]] = {}
        for content_unit_id in dict.fromkeys(content_unit_ids):
            item = self._evidence_by_content_id.get(content_unit_id)
            if item is None:
                continue
            article_id = str(
                item.get("articleId")
                or item.get("contentUnitId")
                or "__unknown__"
            )
            groups.setdefault(article_id, []).append(content_unit_id)

        diversified: list[str] = []
        offsets = {article_id: 0 for article_id in groups}
        while True:
            added = False
            for article_id, ids in groups.items():
                offset = offsets[article_id]
                if offset >= len(ids):
                    continue
                diversified.append(ids[offset])
                offsets[article_id] = offset + 1
                added = True
            if not added:
                break
        return tuple(diversified)

    def diversify_content_ids_by_document(
        self,
        content_unit_ids: tuple[str, ...] | list[str],
    ) -> tuple[str, ...]:
        """障害回復候補を文書単位で分散し、先頭法令の独占を防ぐ。"""
        groups: dict[str, list[str]] = {}
        for content_unit_id in dict.fromkeys(content_unit_ids):
            item = self._evidence_by_content_id.get(content_unit_id)
            if item is None:
                continue
            document_id = str(item.get("documentId") or "__unknown__")
            groups.setdefault(document_id, []).append(content_unit_id)
        diversified: list[str] = []
        offsets = {document_id: 0 for document_id in groups}
        while True:
            added = False
            for document_id, ids in groups.items():
                offset = offsets[document_id]
                if offset >= len(ids):
                    continue
                diversified.append(ids[offset])
                offsets[document_id] = offset + 1
                added = True
            if not added:
                break
        return tuple(diversified)

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
        """Graphで確認できたArticleと関係を、次ターンの候補として登録する。"""
        before = len(self._known_article_ids)
        for path in paths:
            for node in path.get("nodes") or []:
                article_id = _graph_article_id(node)
                if article_id:
                    self._known_article_ids.add(article_id)
        for relation in compact_graph_relations(paths):
            relation["fromTitle"] = (
                relation.get("fromTitle")
                or self._known_documents.get(
                    str(relation.get("fromDocumentId") or ""),
                    "",
                )
            )
            relation["toTitle"] = (
                relation.get("toTitle")
                or self._known_documents.get(
                    str(relation.get("toDocumentId") or ""),
                    "",
                )
            )
            key = (
                str(relation.get("fromArticleId") or ""),
                str(relation.get("edgeType") or ""),
                str(relation.get("toArticleId") or ""),
            )
            if all(key):
                self._graph_relations[key] = relation
        return len(self._known_article_ids) - before

    def prompt_graph_relations(self, *, max_items: int = 50) -> list[dict[str, Any]]:
        """LLMが法令関係の意味を判断できる、確認済みの圧縮Graph経路。"""
        return self.prompt_graph_relations_for_articles(
            article_ids=(),
            max_items=max_items,
        )

    def prompt_graph_relations_for_articles(
        self,
        *,
        article_ids: tuple[str, ...] | list[str] | set[str],
        max_items: int,
    ) -> list[dict[str, Any]]:
        """指定Article同士を結ぶ関係だけを、起点別に分散して返す。

        空集合は既存互換として全関係を対象にする。次サイクルではチェックポイントに
        残ったArticle集合を渡し、未採用候補の再注入を防ぐ。
        """
        allowed = {str(article_id) for article_id in article_ids if article_id}
        relations = [
            item
            for item in self._graph_relations.values()
            if (
                not allowed
                or (
                    str(item.get("fromArticleId") or "") in allowed
                    and str(item.get("toArticleId") or "") in allowed
                )
            )
        ]
        return _diversify_graph_relation_items(
            relations,
            max_items=max_items,
        )

    def prompt_inventory_by_ids(
        self,
        content_unit_ids: tuple[str, ...] | list[str],
        *,
        max_items: int,
    ) -> list[dict[str, Any]]:
        """指定された取得済み原文だけを、本文なしの候補一覧へ整形する。"""
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
            for item in self.items_by_ids(
                list(dict.fromkeys(content_unit_ids))
            )[: max(0, max_items)]
        ]

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


def validate_research_checkpoint(
    checkpoint: ResearchCheckpoint,
    catalog: EvidenceCatalog,
) -> ResearchCheckpointValidation:
    """結論が、取得済みの原文・Graph確認済みArticleだけを参照するか検証する。"""
    visible_content_ids = set(catalog.content_unit_ids)
    known_article_ids = set(catalog.known_article_ids)
    errors: list[str] = []
    selected_ids = tuple(dict.fromkeys(checkpoint.evidenceIds))
    open_ids = tuple(dict.fromkeys(checkpoint.openEvidenceIds))
    for content_unit_id in selected_ids:
        if content_unit_id not in visible_content_ids:
            errors.append(f"unknown_evidence_id:{content_unit_id}")
    for content_unit_id in open_ids:
        if content_unit_id not in visible_content_ids:
            errors.append(f"unknown_open_evidence_id:{content_unit_id}")
        if content_unit_id in selected_ids:
            errors.append(f"evidence_also_open:{content_unit_id}")
    for article_id in dict.fromkeys(checkpoint.nextArticleIds):
        if article_id not in known_article_ids:
            errors.append(f"unknown_next_article_id:{article_id}")
    for issue in checkpoint.logicalStructure.issues:
        node_ids = {node.nodeId for node in issue.authorityNodes}
        if len(node_ids) != len(issue.authorityNodes):
            errors.append(f"duplicate_authority_node_id:{issue.issueId}")
        parent_by_node: dict[str, str | None] = {}
        for node in issue.authorityNodes:
            parent_by_node[node.nodeId] = node.parentNodeId
            if node.articleId and node.articleId not in known_article_ids:
                errors.append(
                    f"unknown_structure_article_id:{node.articleId}"
                )
            if node.parentNodeId and node.parentNodeId not in node_ids:
                errors.append(
                    f"unknown_parent_node_id:{node.nodeId}:{node.parentNodeId}"
                )
            if node.parentNodeId and not node.relationFromParent:
                errors.append(f"child_requires_relation:{node.nodeId}")
            if not node.parentNodeId and node.relationFromParent:
                errors.append(f"root_must_not_have_relation:{node.nodeId}")
            for evidence_id in dict.fromkeys(node.evidenceIds):
                if evidence_id not in visible_content_ids:
                    errors.append(
                        f"unknown_structure_evidence_id:{evidence_id}"
                    )
            if (
                node.verificationStatus == "text_verified"
                and not node.evidenceIds
            ):
                errors.append(
                    f"text_verified_requires_evidence:{node.nodeId}"
                )
        for claim in issue.claims:
            claim_node_ids = list(dict.fromkeys(claim.authorityNodeIds))
            if len(claim_node_ids) != len(claim.authorityNodeIds):
                errors.append(
                    f"duplicate_claim_authority_node_id:"
                    f"{issue.issueId}:{claim.claimId}"
                )
            for node_id in claim_node_ids:
                if node_id not in node_ids:
                    errors.append(
                        f"unknown_claim_authority_node_id:"
                        f"{issue.issueId}:{claim.claimId}:{node_id}"
                    )
        for node_id in parent_by_node:
            seen: set[str] = set()
            current: str | None = node_id
            while current:
                if current in seen:
                    errors.append(
                        f"authority_hierarchy_cycle:{issue.issueId}"
                    )
                    break
                seen.add(current)
                current = parent_by_node.get(current)
    for unresolved in checkpoint.logicalStructure.unresolved:
        if unresolved.articleId and unresolved.articleId not in known_article_ids:
            errors.append(
                f"unknown_unresolved_article_id:{unresolved.articleId}"
            )
    if checkpoint.status == RESEARCH_STATUS_READY and not selected_ids:
        errors.append("ready_requires_selected_evidence")
    if checkpoint.status == RESEARCH_STATUS_READY:
        if any(
            issue.status == "unresolved"
            for issue in checkpoint.logicalStructure.issues
        ):
            errors.append("ready_has_unresolved_issue")
        if any(
            item.affectsCoreConclusion
            for item in checkpoint.logicalStructure.unresolved
        ):
            errors.append("ready_has_unresolved_core_item")
    return ResearchCheckpointValidation(
        valid=not errors,
        errors=tuple(dict.fromkeys(errors)),
        selected_content_unit_ids=tuple(
            content_unit_id
            for content_unit_id in selected_ids
            if content_unit_id in visible_content_ids
        ),
    )


def sanitize_research_checkpoint(
    checkpoint: ResearchCheckpoint,
    catalog: EvidenceCatalog,
) -> tuple[ResearchCheckpoint, dict[str, list[str]]]:
    """未確認IDだけを除外し、検証可能な統合結果を安全側で回収する。

    IDを推測で補正したり、未知Articleを既知扱いしたりはしない。何かを除外した場合は
    統合済みとはみなさずstatusをcontinueへ戻し、次サイクルで再確認させる。
    """
    visible_content_ids = set(catalog.content_unit_ids)
    known_article_ids = set(catalog.known_article_ids)
    changes: dict[str, list[str]] = {
        "removedEvidenceIds": [],
        "removedOpenEvidenceIds": [],
        "removedNextArticleIds": [],
        "removedAuthorityNodeIds": [],
        "removedClaimAuthorityNodeIds": [],
        "removedUnresolvedItems": [],
        "downgradedAuthorityNodeIds": [],
    }

    evidence_ids = [
        content_unit_id
        for content_unit_id in dict.fromkeys(checkpoint.evidenceIds)
        if content_unit_id in visible_content_ids
    ]
    changes["removedEvidenceIds"].extend(
        content_unit_id
        for content_unit_id in dict.fromkeys(checkpoint.evidenceIds)
        if content_unit_id not in visible_content_ids
    )
    open_evidence_ids = [
        content_unit_id
        for content_unit_id in dict.fromkeys(checkpoint.openEvidenceIds)
        if (
            content_unit_id in visible_content_ids
            and content_unit_id not in evidence_ids
        )
    ]
    changes["removedOpenEvidenceIds"].extend(
        content_unit_id
        for content_unit_id in dict.fromkeys(checkpoint.openEvidenceIds)
        if (
            content_unit_id not in visible_content_ids
            or content_unit_id in evidence_ids
        )
    )
    next_article_ids = [
        article_id
        for article_id in dict.fromkeys(checkpoint.nextArticleIds)
        if article_id in known_article_ids
    ]
    changes["removedNextArticleIds"].extend(
        article_id
        for article_id in dict.fromkeys(checkpoint.nextArticleIds)
        if article_id not in known_article_ids
    )

    sanitized_issues: list[ResearchIssueStructure] = []
    for issue in checkpoint.logicalStructure.issues:
        retained_nodes: dict[str, ResearchAuthorityNode] = {}
        for node in issue.authorityNodes:
            if node.nodeId in retained_nodes:
                changes["removedAuthorityNodeIds"].append(node.nodeId)
                continue
            if node.articleId and node.articleId not in known_article_ids:
                changes["removedAuthorityNodeIds"].append(node.nodeId)
                continue
            retained_evidence = [
                evidence_id
                for evidence_id in dict.fromkeys(node.evidenceIds)
                if evidence_id in visible_content_ids
            ]
            changes["removedEvidenceIds"].extend(
                evidence_id
                for evidence_id in dict.fromkeys(node.evidenceIds)
                if evidence_id not in visible_content_ids
            )
            verification_status = node.verificationStatus
            if verification_status == "text_verified" and not retained_evidence:
                verification_status = (
                    "text_not_fetched" if node.articleId else "unverified"
                )
                changes["downgradedAuthorityNodeIds"].append(node.nodeId)
            retained_nodes[node.nodeId] = node.model_copy(
                update={
                    "evidenceIds": retained_evidence,
                    "verificationStatus": verification_status,
                }
            )

        # 未知の親を持つノードとその子孫は、親子関係を作り替えず丸ごと除外する。
        while True:
            invalid_node_ids = {
                node_id
                for node_id, node in retained_nodes.items()
                if (
                    node.parentNodeId
                    and node.parentNodeId not in retained_nodes
                )
            }
            if not invalid_node_ids:
                break
            for node_id in invalid_node_ids:
                retained_nodes.pop(node_id, None)
                changes["removedAuthorityNodeIds"].append(node_id)

        sanitized_claims: list[ResearchClaimStructure] = []
        for claim in issue.claims:
            authority_node_ids = [
                node_id
                for node_id in dict.fromkeys(claim.authorityNodeIds)
                if node_id in retained_nodes
            ]
            changes["removedClaimAuthorityNodeIds"].extend(
                f"{issue.issueId}:{claim.claimId}:{node_id}"
                for node_id in dict.fromkeys(claim.authorityNodeIds)
                if node_id not in retained_nodes
            )
            sanitized_claims.append(
                claim.model_copy(
                    update={"authorityNodeIds": authority_node_ids}
                )
            )
        sanitized_issues.append(
            issue.model_copy(
                update={
                    "authorityNodes": list(retained_nodes.values()),
                    "claims": sanitized_claims,
                }
            )
        )

    sanitized_unresolved: list[ResearchUnresolvedItem] = []
    for item in checkpoint.logicalStructure.unresolved:
        if item.articleId and item.articleId not in known_article_ids:
            changes["removedUnresolvedItems"].append(
                f"{item.issueId}:{item.claimId or ''}:{item.articleId}"
            )
            continue
        sanitized_unresolved.append(item)

    changes = {
        key: list(dict.fromkeys(values))
        for key, values in changes.items()
        if values
    }
    sanitized_structure = checkpoint.logicalStructure.model_copy(
        update={
            "issues": sanitized_issues,
            "unresolved": sanitized_unresolved,
        }
    )
    sanitized = checkpoint.model_copy(
        update={
            "status": (
                RESEARCH_STATUS_CONTINUE if changes else checkpoint.status
            ),
            "evidenceIds": evidence_ids,
            "openEvidenceIds": open_evidence_ids,
            "nextArticleIds": next_article_ids,
            "logicalStructure": sanitized_structure,
        }
    )
    return sanitized, changes


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
    phase: str | None = None,
    cycle_index: int | None = None,
    cycle_count: int | None = None,
    checkpoint: ResearchCheckpoint | None = None,
    output_token_limit: int = 4096,
) -> str:
    """探索手順を細かく規定せず、目的・利用可能ツール・証拠境界だけを伝える。"""
    choices_block = ""
    if choices:
        choices_block = "\n選択肢:\n" + "\n".join(
            f"{label.upper()}: {text}" for label, text in sorted(choices.items())
        )
    prompt_content_ids = tuple(dict.fromkeys(preferred_content_ids))
    if checkpoint is not None:
        unresolved_article_ids = [
            item.articleId
            for item in checkpoint.logicalStructure.unresolved
            if item.articleId
        ]
        prompt_content_ids = tuple(
            dict.fromkeys(
                [
                    *prompt_content_ids,
                    *checkpoint.evidenceIds,
                    *checkpoint.openEvidenceIds,
                    *catalog.content_ids_for_article_ids(
                        [
                            *checkpoint.nextArticleIds,
                            *unresolved_article_ids,
                        ]
                    ),
                ]
            )
        )
    if checkpoint is not None:
        # サイクル間では、チェックポイントが選択した原文と今回の新規取得だけを
        # 再提示する。Catalog全体はID検証・オンデマンド取得用に保持するが、
        # 過去の未採用候補をプロンプトへ自動的に戻さない。
        evidence = _prompt_items_by_ids(
            catalog,
            prompt_content_ids,
            max_items=max_evidence_items,
            max_chars=evidence_chars,
        )
    else:
        evidence = catalog.prompt_items(
            max_items=max_evidence_items,
            max_chars=evidence_chars,
            preferred_content_ids=preferred_content_ids,
        )
    # 最終判断では新しい候補を探索できないため、本文の無い候補一覧を重ねて
    # 入力を膨らませず、直前に選択した根拠本文を優先して提示する。
    if finalize_only:
        inventory = []
    elif checkpoint is not None:
        inventory = catalog.prompt_inventory_by_ids(
            prompt_content_ids,
            max_items=max_evidence_items,
        )
    else:
        inventory = catalog.prompt_inventory(max_items=max_evidence_items)
    graph_relations = _research_prompt_graph_relations(
        catalog,
        checkpoint=checkpoint,
        tool_history=tool_history or [],
        max_items=32,
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
    output_reserve_tokens = max(64, output_token_limit // 4)
    target_output_tokens = max(
        128,
        min(2500, output_token_limit - output_reserve_tokens),
    )
    output_budget_notice = f"""
出力上限は{output_token_limit:,}トークンです。
JSON全体を{target_output_tokens:,}トークン以内に収めることを目標にしてください。

情報が多い場合は、status、実行すべきactionsとArticle ID、
selectedEvidenceのcontentUnitId、未確認事項、各項目の理由、全体のreasonの順に
優先してください。
法的結論を支える確認済みの根拠IDと、結論に必要だが未確認のArticle IDを
最優先で保持してください。
理由や説明は、上限へ近づくほど要約してください。条文本文や調査経緯を繰り返さず、
法令名・条番号・確認目的だけを残してください。IDを削る前に理由を短縮してください。
JSONを完全に閉じることを、説明の詳しさより優先してください。
"""
    finalization_notice = ""
    if finalize_only:
        finalization_notice = """
これは最終判断ターンです。追加検索はできません。
actionsは必ず空配列にし、statusはreadyまたはinsufficientのどちらかにしてください。
確認済み本文から質問の中心部分を回答できるならreadyとし、最終回答に使う根拠を
selectedEvidenceへ指定してください。中心部分の根拠が足りない場合はinsufficientとし、
それでも限定回答に使える確認済み根拠はselectedEvidenceへ残してください。
"""
    phase_notice = ""
    if phase == "explore":
        phase_notice = f"""
これは全{cycle_count}回のうち第{(cycle_index or 0) + 1}回の探索段階です。
前回までの調査状態を再検証し、未解決事項、別の法令、例外、委任先を探すための操作を
選んでください。最初から全検索を計画せず、この段階で得るべき情報に絞ってください。
"""
    elif phase == "deepen":
        phase_notice = f"""
これは全{cycle_count}回のうち第{(cycle_index or 0) + 1}回の掘り下げ段階です。
直前の探索結果を読み、必要なArticle本文、確認済みGraph関係、追加の法令内検索を
選んでください。検索で得たArticleの項・号確認にはfetch_articlesを優先してください。
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
- 例外・免除・特則を結論にする場合は、取得できる限り、本則・委任元と例外・免除・
  具体化規定の双方を直接確認する。他条が本則に言及しているだけの間接根拠で代用しない
- 確認済みGraphに、回答で使う条文を直接具体化する下位法令Articleがある場合は、
  質問への具体的回答に必要か判断し、必要なら本文取得を未解決事項として残す
- ガイドは法令本文ではない。法令間のつながりを探す手掛かりや行政解釈として区別する
- 質問と似ているだけの条文を、必要な根拠が揃った証拠とみなさない
- 前回のlogicalStructureを読み、論点→結論→根拠→委任・定義・例外等の関係を
  保ったまま今回の判断へ使う。Article IDだけを見て関係の意味を捨てない
- logicalStructureでは、各IssueのauthorityNodesがその論点の共有根拠レジストリであり、
  各ClaimはauthorityNodeIdsでレジストリ内のnodeIdを参照する。同じ根拠をClaimごとに
  複製せず、複数のClaimから同じnodeIdを参照してよい
- authorityNodesのparentNodeIdは同じIssue内のnodeIdだけを参照し、直接根拠を根、
  委任先・定義・例外・手続具体化・ガイド等を子としてDAGを表す
- 確認済みGraph関係は候補であり、本文未取得の法令を最終根拠とは扱わない
- selectedEvidenceには、利用可能な証拠にあるcontentUnitIdだけを指定する
- selectedEvidenceには、最終回答で実際に根拠として使う本文だけを理由付きで指定する
- selectedEvidenceは最大{max_selected_evidence}件とし、同じ結論を支える重複項号を
  網羅的に並べず、回答に必要な最小限の本文を選ぶ
- 取得できていないArticle IDを推測してfetch_articlesやexpand_graphへ渡さない
- documentIdsを指定する場合は、利用可能な文書にあるIDだけを使用する
- 質問の中心的な結論を取得済み法令で説明できればreadyとする。考え得る全ての例外、
  周辺制度、質問が求めていない手続まで網羅する必要はない
- 未確認事項が中心的な結論を変えず、回答上の留保として明示できる場合はreadyとする
- readyを返す直前に、結論を支える法令本文を確認してselectedEvidenceへ選択したか、
  自分が本文確認を必要と判断してnextArticleIdsまたは未確認事項へ残したArticleが
  未取得のままではないかを見直す
- そのArticleを残りの操作・時間で取得できるなら、readyにせずfetch_articlesで確認する。
  これは質問された全事項や考え得る全論点の網羅を要求するものではなく、自分が回答前に
  必要と判断した本文の取得漏れを防ぐための最終チェックである
- 取得できない場合は探索を無期限に続けない。中心的結論へ影響するならinsufficient、
  影響しないなら未確認事項と回答への影響をmissingEvidenceへ明示してreadyとする
- search_corpusはArticleの代表本文を返す。候補Articleの他の項・号を確認する場合は、
  類似した検索を繰り返さずfetch_articlesを使う
- 根拠が十分ならready、不足を具体化して追加調査できるならcontinue、
  予算内では根拠を確認できないならinsufficientとする
- insufficientでも、確認済みで回答の限定に役立つ証拠があればselectedEvidenceへ含める
- 1ターンのactionsは最大{max_actions}件とする
- 必ずJSONだけを返す

{output_budget_notice}
{budget_notice}
{finalization_notice}
{phase_notice}

質問: {question}{choices_block}

前回までの調査状態:
{json.dumps(checkpoint.model_dump() if checkpoint else {}, ensure_ascii=False)}

これまでの操作:
{json.dumps(prompt_history, ensure_ascii=False)}

利用可能な文書（法令名を特定できる場合はsearch_corpusのdocumentIdsで限定できる）:
{json.dumps(catalog.prompt_documents(), ensure_ascii=False)}

候補一覧（本文が省略された候補はArticle IDをfetch_articlesして確認できる）:
{json.dumps(inventory, ensure_ascii=False)}

確認済みGraph関係（起点、関係種別、到達先を一組として判断する）:
{json.dumps(graph_relations, ensure_ascii=False)}

本文を確認できる証拠:
{json.dumps(evidence, ensure_ascii=False)}

JSON:"""


def build_research_checkpoint_prompt(
    *,
    question: str,
    choices: dict[str, str] | None,
    catalog: EvidenceCatalog,
    checkpoint: ResearchCheckpoint,
    cycle_index: int,
    cycle_count: int,
    cycle_new_content_ids: tuple[str, ...] | list[str],
    tool_history: list[dict[str, Any]] | None,
    max_selected_evidence: int,
) -> str:
    """1サイクルを、原文IDと法的論理構造を持つ小さな状態へ統合する。"""
    choices_block = ""
    if choices:
        choices_block = "\n選択肢:\n" + "\n".join(
            f"{label.upper()}: {text}"
            for label, text in sorted(choices.items())
        )
    previous_ids = tuple(dict.fromkeys(checkpoint.evidenceIds))
    previous_open_ids = tuple(
        content_unit_id
        for content_unit_id in dict.fromkeys(checkpoint.openEvidenceIds)
        if content_unit_id not in set(previous_ids)
    )
    stage_selected_ids = tuple(
        dict.fromkeys(
            str(selected.get("contentUnitId") or "")
            for item in (tool_history or [])
            for selected in (
                (item.get("decision") or {}).get("selectedEvidence")
                or []
            )
            if selected.get("contentUnitId")
        )
    )
    # 段階判断でLLMが明示選択した本文を先頭に置く。取得順のままでは、
    # 長いArticleの冒頭項号が文字予算を使い、選択済みの根拠本文が統合入力から
    # 漏れることがある。残りだけをArticle単位で分散する。
    new_ids = tuple(
        content_unit_id
        for content_unit_id in dict.fromkeys(
            [
                *stage_selected_ids,
                *catalog.diversify_content_ids(cycle_new_content_ids),
            ]
        )
        if content_unit_id not in set(previous_ids)
    )
    previous_evidence = _prompt_items_by_ids(
        catalog,
        previous_ids,
        max_items=max_selected_evidence,
        max_chars=5000,
    )
    previous_open_evidence = _prompt_items_by_ids(
        catalog,
        previous_open_ids,
        max_items=20,
        max_chars=5000,
    )
    new_evidence = _prompt_items_by_ids(
        catalog,
        new_ids,
        max_items=32,
        max_chars=14000,
    )
    tool_summary = [
        {
            "tool": item.get("tool"),
            "articleIds": item.get("articleIds") or [],
            "documentIds": item.get("documentIds") or [],
            "resultCount": item.get("resultCount"),
            "newEvidenceCount": item.get("newEvidenceCount"),
            "newArticleIds": item.get("newArticleIds") or [],
            "autoGraphArticleIds": item.get("autoGraphArticleIds") or [],
            "graphRelationCount": len(item.get("graphRelations") or []),
            "error": item.get("error"),
            "autoGraphError": item.get("autoGraphError"),
        }
        for item in (tool_history or [])
        if item.get("tool")
    ]
    graph_relations = _research_prompt_graph_relations(
        catalog,
        checkpoint=checkpoint,
        tool_history=tool_history or [],
        max_items=32,
    )
    return f"""あなたは、日本法令の反復調査を統合する責任者です。
これは全{cycle_count}回のうち第{cycle_index + 1}回の調査サイクルの統合段階です。
今回の探索・掘り下げで得た原文とGraph関係を読み、次サイクルに必要な結論と
法的論理構造へ圧縮してください。

規則:
- conclusionには、次サイクルが再検証すべき現在の結論だけを1〜3文で書く
- 調査経緯、検索語、根拠の選択理由、条文の長い説明は書かない
- evidenceIdsには、結論または最終回答に実際に使う取得済み原文IDだけを最大10件指定する
- openEvidenceIdsには、本文取得済みだが結論との関係を次回も確認する原文IDだけを
  最大20件指定する。evidenceIdsと重複させない
- nextQuestionsには、質問への回答に必要だが未確認の事項だけを最大3件指定する
- nextArticleIdsには、Graphまたは検索で存在を確認済みだが本文未取得で、
  次回読むべきArticle IDだけを最大10件指定する
- 今回の「直接取得・段階選択した原文」にある各IDは、evidenceIds、
  openEvidenceIds、不要のいずれかへ分類する。不要と判断したIDはどちらにも残さない
- logicalStructureは次の共有DAG規約に従う
  1. IssueのauthorityNodesへ、その論点で確認した根拠ノードを一度だけ登録する
  2. ClaimのauthorityNodeIdsには、そのClaimを支える同じIssue内のnodeIdを指定する
  3. 同じ根拠を複数Claimが使う場合はノードを複製せず、同じnodeIdを共有参照する
  4. authorityNodeのparentNodeIdは同じIssueのauthorityNodes内だけを参照する
  5. 直接根拠を根とし、委任先・定義・例外・手続具体化・ガイド等を子として
     接続する。子にはrelationFromParentとpurposeを必ず書く
  6. 各authorityNodeのevidenceIdsは、そのArticleの確認済み原文IDだけを最大20件指定する
- 形は issue={{issueId, question, status, authorityNodes:[...],
  claims:[{{claimId, question, conclusion, status, authorityNodeIds:[...]}}]}}
  であり、Claimの中へauthorityNodesを入れない
- 同じ論点・結論のissueIdとclaimIdは次サイクルでも維持し、新しい根拠は既存の
  IssueのauthorityNodesへ追加する。既存nodeIdも調査のたびに作り直さない
- Graphで関係だけ確認した本文未取得Articleはgraph_verifiedまたは
  text_not_fetchedとし、text_verifiedにしない
- text_verifiedには、そのArticleの取得済みevidenceIdsを指定する
- 例外・免除・特則を結論に使う場合、本則・委任元と例外・免除・具体化規定の双方が
  取得済みなら、Claimから両方のauthorityNodeを参照し、最終回答に必要な原文を
  evidenceIdsへ残す。他条による言及だけで本則本文を置き換えない
- 確認済みGraph上の下位法令Articleが結論の具体的内容を定める可能性があり、
  本文未取得なら、中心的結論への影響を判断してunresolvedとnextArticleIdsへ残す
- unresolvedには、どのissue・claimの何が不足し、中心的結論へ影響するかを記録する
- nextQuestionsとnextArticleIdsはlogicalStructure.unresolvedと整合させる
- status=readyにする場合、中心的結論へ影響するunresolvedを残さない
- status=readyを確定する直前に、結論を支える法令本文をevidenceIdsへ選択したか、
  自分が本文確認を必要と判断してnextArticleIdsまたはunresolvedへ残したArticleが
  未取得のままではないかを見直す。後続サイクルで取得可能ならstatus=continueとし、
  nextArticleIdsへ残す
- これは質問された全事項の完全調査を要求するものではない。取得不能または最終サイクル
  では無期限に継続せず、中心的結論への影響があればinsufficient、影響がなければ
  未確認事項と影響を残したうえでreadyとする
- conclusion、nextQuestions、nextArticleIdsで同じ説明を繰り返さない
- statusは、中心的な結論を原文で説明できればready、次回で補えるならcontinue、
  投入済み資料では確認できないならinsufficientとする
- 第{cycle_index + 1}回でも、後続サイクルがある場合はreadyの結論を反証・補完対象として引き継ぐ
- 必ずJSONだけを返す

質問: {question}{choices_block}

前回までの調査状態:
{json.dumps(checkpoint.model_dump(), ensure_ascii=False)}

今回のツール実行要約:
{json.dumps(tool_summary, ensure_ascii=False)}

現在確認できるGraph関係:
{json.dumps(graph_relations, ensure_ascii=False)}

前回までに選択した根拠原文:
{json.dumps(previous_evidence, ensure_ascii=False)}

前回から判断を継続する取得済み原文:
{json.dumps(previous_open_evidence, ensure_ascii=False)}

今回の直接取得・段階選択原文（一般検索の未採用候補は含まない）:
{json.dumps(new_evidence, ensure_ascii=False)}

JSON:"""


def checkpoint_integration_prompt_content_ids(
    *,
    catalog: EvidenceCatalog,
    checkpoint: ResearchCheckpoint,
    cycle_new_content_ids: tuple[str, ...] | list[str],
    tool_history: list[dict[str, Any]] | None,
    max_selected_evidence: int,
) -> tuple[str, ...]:
    """統合LLMへ本文付きで実際に提示される原文IDを返す。"""
    previous_ids = tuple(dict.fromkeys(checkpoint.evidenceIds))
    previous_open_ids = tuple(
        content_unit_id
        for content_unit_id in dict.fromkeys(checkpoint.openEvidenceIds)
        if content_unit_id not in set(previous_ids)
    )
    stage_selected_ids = tuple(
        dict.fromkeys(
            str(selected.get("contentUnitId") or "")
            for item in (tool_history or [])
            for selected in (
                (item.get("decision") or {}).get("selectedEvidence")
                or []
            )
            if selected.get("contentUnitId")
        )
    )
    new_ids = tuple(
        content_unit_id
        for content_unit_id in dict.fromkeys(
            [
                *stage_selected_ids,
                *catalog.diversify_content_ids(cycle_new_content_ids),
            ]
        )
        if content_unit_id not in set(previous_ids)
    )
    prompt_groups = (
        _prompt_items_by_ids(
            catalog,
            previous_ids,
            max_items=max_selected_evidence,
            max_chars=5000,
        ),
        _prompt_items_by_ids(
            catalog,
            previous_open_ids,
            max_items=20,
            max_chars=5000,
        ),
        _prompt_items_by_ids(
            catalog,
            new_ids,
            max_items=32,
            max_chars=14000,
        ),
    )
    return tuple(
        dict.fromkeys(
            str(item.get("contentUnitId") or "")
            for group in prompt_groups
            for item in group
            if item.get("contentUnitId")
        )
    )


def _prompt_items_by_ids(
    catalog: EvidenceCatalog,
    content_unit_ids: tuple[str, ...] | list[str],
    *,
    max_items: int,
    max_chars: int,
) -> list[dict[str, Any]]:
    remaining = max(0, max_chars)
    output: list[dict[str, Any]] = []
    for item in catalog.items_by_ids(content_unit_ids)[: max(0, max_items)]:
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


def _research_prompt_graph_relations(
    catalog: EvidenceCatalog,
    *,
    checkpoint: ResearchCheckpoint | None,
    tool_history: list[dict[str, Any]],
    max_items: int,
) -> list[dict[str, Any]]:
    """前回の採用構造と、同一サイクルの新規関係だけをLLMへ提示する。"""
    current_relations = [
        relation
        for item in tool_history
        for relation in (item.get("graphRelations") or [])
        if isinstance(relation, dict)
    ]
    current_relations = _diversify_graph_relation_items(
        current_relations,
        max_items=min(24, max_items),
    )
    if checkpoint is None:
        retained_relations = catalog.prompt_graph_relations(
            max_items=min(16, max_items)
        )
    else:
        retained_relations = catalog.prompt_graph_relations_for_articles(
            article_ids=_checkpoint_article_ids(checkpoint, catalog),
            max_items=min(16, max_items),
        )
    return _diversify_graph_relation_items(
        [*current_relations, *retained_relations],
        max_items=max_items,
    )


def _checkpoint_article_ids(
    checkpoint: ResearchCheckpoint,
    catalog: EvidenceCatalog,
) -> set[str]:
    article_ids = {
        node.articleId
        for issue in checkpoint.logicalStructure.issues
        for node in issue.authorityNodes
        if node.articleId
    }
    article_ids.update(
        article_id
        for article_id in checkpoint.nextArticleIds
        if article_id
    )
    article_ids.update(
        item.articleId
        for item in checkpoint.logicalStructure.unresolved
        if item.articleId
    )
    article_ids.update(
        str(item.get("articleId") or "")
        for item in catalog.items_by_ids(
            [*checkpoint.evidenceIds, *checkpoint.openEvidenceIds]
        )
        if item.get("articleId")
    )
    return {str(article_id) for article_id in article_ids if article_id}


def _diversify_graph_relation_items(
    relations: list[dict[str, Any]],
    *,
    max_items: int,
) -> list[dict[str, Any]]:
    """重複を除き、起点Articleごとのラウンドロビンで関係を圧縮する。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str, str]] = set()
    for relation in relations:
        key = (
            str(relation.get("fromArticleId") or ""),
            str(relation.get("edgeType") or ""),
            str(relation.get("toArticleId") or ""),
        )
        if not all(key) or key in seen:
            continue
        seen.add(key)
        groups.setdefault(key[0], []).append(relation)
    output: list[dict[str, Any]] = []
    offsets = {key: 0 for key in groups}
    while len(output) < max(0, max_items):
        added = False
        for key, items in groups.items():
            offset = offsets[key]
            if offset >= len(items):
                continue
            output.append(dict(items[offset]))
            offsets[key] = offset + 1
            added = True
            if len(output) >= max_items:
                break
        if not added:
            break
    return output


def _prompt_tool_history(
    tool_history: list[dict[str, Any]],
    *,
    finalize_only: bool,
) -> list[dict[str, Any]]:
    """同一サイクルの履歴を圧縮し、Graph本文は専用欄へ一度だけ提示する。"""
    compact: list[dict[str, Any]] = []
    for item in tool_history:
        turn_index = item.get("turnIndex")
        phase = item.get("phase")
        decision = item.get("decision")
        if isinstance(decision, dict):
            compact.append(
                {
                    "turnIndex": turn_index,
                    "phase": phase,
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
                    "phase": phase,
                    "validationErrors": item.get("validationErrors"),
                }
            )
            continue
        compact.append(
            {
                "turnIndex": turn_index,
                "phase": phase,
                "tool": item.get("tool"),
                "articleIds": item.get("articleIds") or [],
                "documentIds": item.get("documentIds") or [],
                "resultCount": item.get("resultCount"),
                "newEvidenceCount": item.get("newEvidenceCount"),
                "newArticleCount": item.get("newArticleCount"),
                "newArticleIds": (item.get("newArticleIds") or [])[:12],
                "autoGraphArticleIds": (
                    item.get("autoGraphArticleIds") or []
                )[:12],
                "graphRelationCount": len(
                    item.get("graphRelations") or []
                ),
                "query": (
                    None
                    if finalize_only
                    else str(item.get("query") or "")[:300]
                ),
                "reason": (
                    None
                    if finalize_only
                    else str(item.get("reason") or "")[:300]
                ),
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
            "reason": {"type": "string", "maxLength": 300},
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


def research_checkpoint_json_schema(
    *,
    max_selected_evidence: int,
) -> dict[str, Any]:
    authority_node_schema = {
        "type": "object",
        "properties": {
            "nodeId": {"type": "string"},
            "articleId": {"type": ["string", "null"]},
            "title": {"type": "string"},
            "legalRole": {"type": "string"},
            "verificationStatus": {
                "type": "string",
                "enum": [
                    "text_verified",
                    "graph_verified",
                    "text_not_fetched",
                    "unverified",
                ],
            },
            "evidenceIds": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": AUTHORITY_NODE_EVIDENCE_MAX,
            },
            "parentNodeId": {"type": ["string", "null"]},
            "relationFromParent": {"type": ["string", "null"]},
            "purpose": {"type": "string"},
        },
        "required": [
            "nodeId",
            "articleId",
            "title",
            "legalRole",
            "verificationStatus",
            "evidenceIds",
            "parentNodeId",
            "relationFromParent",
            "purpose",
        ],
        "additionalProperties": False,
    }
    claim_schema = {
        "type": "object",
        "properties": {
            "claimId": {"type": "string"},
            "question": {"type": "string"},
            "conclusion": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["verified", "partial", "unresolved"],
            },
            "authorityNodeIds": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 12,
            },
        },
        "required": [
            "claimId",
            "question",
            "conclusion",
            "status",
            "authorityNodeIds",
        ],
        "additionalProperties": False,
    }
    issue_schema = {
        "type": "object",
        "properties": {
            "issueId": {"type": "string"},
            "question": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["verified", "partial", "unresolved"],
            },
            "authorityNodes": {
                "type": "array",
                "items": authority_node_schema,
                "maxItems": 20,
            },
            "claims": {
                "type": "array",
                "items": claim_schema,
                "maxItems": 8,
            },
        },
        "required": [
            "issueId",
            "question",
            "status",
            "authorityNodes",
            "claims",
        ],
        "additionalProperties": False,
    }
    unresolved_schema = {
        "type": "object",
        "properties": {
            "issueId": {"type": "string"},
            "claimId": {"type": ["string", "null"]},
            "articleId": {"type": ["string", "null"]},
            "action": {
                "type": "string",
                "enum": [
                    "search",
                    "fetch_article",
                    "expand_graph",
                    "verify_text",
                ],
            },
            "reason": {"type": "string"},
            "affectsCoreConclusion": {"type": "boolean"},
        },
        "required": [
            "issueId",
            "claimId",
            "articleId",
            "action",
            "reason",
            "affectsCoreConclusion",
        ],
        "additionalProperties": False,
    }
    logical_structure_schema = {
        "type": "object",
        "properties": {
            "issues": {
                "type": "array",
                "items": issue_schema,
                "maxItems": 4,
            },
            "unresolved": {
                "type": "array",
                "items": unresolved_schema,
                "maxItems": 6,
            },
        },
        "required": ["issues", "unresolved"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": [
                    RESEARCH_STATUS_CONTINUE,
                    RESEARCH_STATUS_READY,
                    RESEARCH_STATUS_INSUFFICIENT,
                ],
            },
            "conclusion": {"type": "string", "maxLength": 1200},
            "evidenceIds": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": min(max_selected_evidence, 10),
            },
            "openEvidenceIds": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 20,
            },
            "nextQuestions": {
                "type": "array",
                "items": {"type": "string", "maxLength": 100},
                "maxItems": 3,
            },
            "nextArticleIds": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": CHECKPOINT_NEXT_ARTICLE_MAX,
            },
            "logicalStructure": logical_structure_schema,
        },
        "required": [
            "status",
            "conclusion",
            "evidenceIds",
            "openEvidenceIds",
            "nextQuestions",
            "nextArticleIds",
            "logicalStructure",
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


def parse_research_checkpoint(
    raw_text: str,
    *,
    max_selected_evidence: int,
) -> tuple[ResearchCheckpoint | None, str | None]:
    try:
        payload = json.loads(raw_text)
        checkpoint = ResearchCheckpoint.model_validate(payload)
        if len(checkpoint.evidenceIds) > min(max_selected_evidence, 10):
            raise ValueError(
                "evidenceIds exceeds "
                f"max_selected_evidence={max_selected_evidence}"
            )
        return checkpoint, None
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


def compact_graph_relations(paths: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Graph pathを、LLMへ渡せる起点・関係・到達先の組へ圧縮する。"""
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for path in paths:
        nodes = path.get("nodes") or []
        edges = path.get("edges") or []
        for index, edge in enumerate(edges):
            if index + 1 >= len(nodes):
                continue
            source = nodes[index] if isinstance(nodes[index], dict) else {}
            target = (
                nodes[index + 1]
                if isinstance(nodes[index + 1], dict)
                else {}
            )
            from_article_id = _graph_article_id(source)
            to_article_id = _graph_article_id(target)
            edge_type = str(edge.get("edgeType") or "")
            key = (from_article_id, edge_type, to_article_id)
            if not all(key) or key in seen:
                continue
            seen.add(key)
            output.append(
                {
                    "fromArticleId": from_article_id,
                    "fromDocumentId": str(source.get("documentId") or ""),
                    "fromTitle": str(source.get("title") or ""),
                    "fromHeading": str(source.get("heading") or ""),
                    "edgeType": edge_type,
                    "toArticleId": to_article_id,
                    "toDocumentId": str(target.get("documentId") or ""),
                    "toTitle": str(target.get("title") or ""),
                    "toHeading": str(target.get("heading") or ""),
                    "relationSource": str(
                        edge.get("relationSource") or ""
                    ),
                    "relationConfidence": edge.get("relationConfidence"),
                }
            )
    return output


def _graph_article_id(node: dict[str, Any]) -> str:
    raw = str(
        node.get("articleContentUnitId")
        or node.get("contentUnitId")
        or node.get("graphNodeId")
        or ""
    )
    if "-article-" not in raw:
        return ""
    return raw.split("-paragraph-", 1)[0]
