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
ResearchHypothesisStatus = Literal[
    "unverified",
    "partially_supported",
    "supported",
    "rejected",
]
ResearchRelationVerdict = Literal["confirmed", "rejected", "uncertain"]
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
    hypothesisIds: list[str] = Field(default_factory=list, max_length=8)
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


class ResearchHypothesis(BaseModel):
    """検索前の暫定結論と、取得本文による検証状態。"""

    model_config = ConfigDict(extra="forbid")

    hypothesisId: str = Field(min_length=1, max_length=80)
    statement: str = Field(min_length=1, max_length=300)
    status: ResearchHypothesisStatus = "unverified"
    evidenceIds: list[str] = Field(default_factory=list, max_length=12)
    missing: list[str] = Field(default_factory=list, max_length=6)


class ResearchTurn(BaseModel):
    """LLM主導調査の1ターン分の構造化出力。"""

    model_config = ConfigDict(extra="forbid")

    status: ResearchStatus
    hypotheses: list[ResearchHypothesis] = Field(
        default_factory=list, max_length=8
    )
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


class ResearchRelationDecision(BaseModel):
    """未確認RelationAssertionを本文で検証した、案件内だけのLLM判断。"""

    model_config = ConfigDict(extra="forbid")

    assertionId: str = Field(min_length=1, max_length=500)
    verdict: ResearchRelationVerdict
    relationType: str = Field(min_length=1, max_length=60)
    fromArticleId: str = Field(min_length=1, max_length=500)
    toArticleId: str = Field(min_length=1, max_length=500)
    evidenceIds: list[str] = Field(default_factory=list, max_length=20)
    reason: str = Field(default="", max_length=240)


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
    hypotheses: list[ResearchHypothesis] = Field(
        default_factory=list,
        max_length=8,
    )
    unresolved: list[ResearchUnresolvedItem] = Field(
        default_factory=list,
        max_length=6,
    )
    relationDecisions: list[ResearchRelationDecision] = Field(
        default_factory=list,
        max_length=8,
    )

    @model_validator(mode="after")
    def validate_compact_size(self) -> "ResearchLogicalStructure":
        claims = sum(len(issue.claims) for issue in self.issues)
        nodes = sum(len(issue.authorityNodes) for issue in self.issues)
        hypothesis_ids = [item.hypothesisId for item in self.hypotheses]
        if claims > 8:
            raise ValueError("logicalStructure supports at most 8 claims")
        if nodes > 20:
            raise ValueError("logicalStructure supports at most 20 authority nodes")
        if len(hypothesis_ids) != len(set(hypothesis_ids)):
            raise ValueError("logicalStructure hypothesisId must be unique")
        assertion_ids = [item.assertionId for item in self.relationDecisions]
        if len(assertion_ids) != len(set(assertion_ids)):
            raise ValueError("logicalStructure assertionId must be unique")
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
        self._relation_assertions: dict[str, dict[str, Any]] = {}

    @property
    def content_unit_ids(self) -> tuple[str, ...]:
        return tuple(self._evidence_by_content_id)

    @property
    def known_article_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._known_article_ids))

    @property
    def known_document_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._known_documents))

    @property
    def known_relation_assertion_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._relation_assertions))

    def relation_assertion(self, assertion_id: str) -> dict[str, Any] | None:
        item = self._relation_assertions.get(str(assertion_id or ""))
        return dict(item) if item is not None else None

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

    def diversify_content_ids_for_prompt(
        self,
        content_unit_ids: tuple[str, ...] | list[str],
    ) -> tuple[str, ...]:
        """文書とArticleの双方を分散し、検索実行順によるPrompt独占を防ぐ。"""
        by_document: dict[str, list[str]] = {}
        for content_unit_id in dict.fromkeys(content_unit_ids):
            item = self._evidence_by_content_id.get(content_unit_id)
            if item is None:
                continue
            document_id = str(item.get("documentId") or "__unknown__")
            by_document.setdefault(document_id, []).append(content_unit_id)

        groups = {
            document_id: list(self.diversify_content_ids(ids))
            for document_id, ids in by_document.items()
        }
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

    def add_relation_assertions(
        self,
        assertions: list[dict[str, Any]],
    ) -> int:
        """未確認関係候補と両端Article IDを、意味を確定せず登録する。"""
        added = 0
        for raw in assertions:
            assertion_id = str(
                raw.get("assertionId") or raw.get("graphNodeId") or ""
            )
            from_article_id = str(raw.get("fromArticleId") or "")
            to_article_id = str(raw.get("toArticleId") or "")
            suggested_type = str(raw.get("suggestedType") or "")
            if not all(
                (assertion_id, from_article_id, to_article_id, suggested_type)
            ):
                continue
            normalized = {
                "assertionId": assertion_id,
                "fromArticleId": from_article_id,
                "toArticleId": to_article_id,
                "suggestedType": suggested_type,
                "assertionSource": raw.get("assertionSource"),
                "assertedByDocumentId": raw.get("assertedByDocumentId"),
                "sourceReferenceEdgeId": raw.get("sourceReferenceEdgeId"),
                "sourceText": str(raw.get("sourceText") or "")[:240],
                "delegationWordingDetected": bool(
                    raw.get("delegationWordingDetected")
                ),
                "specificationWordingDetected": bool(
                    raw.get("specificationWordingDetected")
                ),
                "status": str(raw.get("status") or "unverified"),
            }
            if assertion_id not in self._relation_assertions:
                added += 1
            self._relation_assertions[assertion_id] = normalized
            self._known_article_ids.update(
                (from_article_id, to_article_id)
            )
        return added

    def add_preclassified_relations(
        self,
        assertions: list[dict[str, Any]],
    ) -> int:
        """索引時LLMがimplementsと分類した派生関係を検索ナビゲーションへ登録する。

        正式Graphエッジや法令本文の証拠には昇格させない。
        """
        before = len(self._known_article_ids)
        for raw in assertions:
            from_article_id = str(raw.get("fromArticleId") or "")
            to_article_id = str(raw.get("toArticleId") or "")
            if not from_article_id or not to_article_id:
                continue
            relation = {
                "fromArticleId": from_article_id,
                "fromDocumentId": str(raw.get("fromDocumentId") or ""),
                "fromTitle": str(raw.get("fromTitle") or ""),
                "fromHeading": str(raw.get("fromHeading") or ""),
                "edgeType": "IMPLEMENTS",
                "toArticleId": to_article_id,
                "toDocumentId": str(raw.get("toDocumentId") or ""),
                "toTitle": str(raw.get("toTitle") or ""),
                "toHeading": str(raw.get("toHeading") or ""),
                "relationSource": "offline_llm_classification",
                "relationStatus": str(raw.get("status") or ""),
                "classifierModel": str(raw.get("classifierModel") or ""),
                "classifierPromptVersion": str(
                    raw.get("classifierPromptVersion") or ""
                ),
                "assertionId": str(raw.get("assertionId") or ""),
            }
            key = (from_article_id, "IMPLEMENTS", to_article_id)
            self._graph_relations[key] = relation
            self._known_article_ids.update((from_article_id, to_article_id))
        return len(self._known_article_ids) - before

    def prompt_relation_assertions(
        self,
        *,
        article_ids: tuple[str, ...] | list[str] | set[str] = (),
        exclude_assertion_ids: tuple[str, ...] | list[str] | set[str] = (),
        max_items: int = 24,
    ) -> list[dict[str, Any]]:
        """指定Articleに接続する未判断候補を、ID順で小さく表示する。"""
        allowed = {str(item) for item in article_ids if item}
        excluded = {str(item) for item in exclude_assertion_ids if item}
        return [
            dict(item)
            for assertion_id, item in sorted(self._relation_assertions.items())
            if assertion_id not in excluded
            and (
                not allowed
                or str(item.get("fromArticleId") or "") in allowed
                or str(item.get("toArticleId") or "") in allowed
            )
        ][: max(0, max_items)]

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
            prompt_item = _render_prompt_evidence_item(item, text_budget)
            if prompt_item is None:
                break
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
    allowed_content_unit_ids: tuple[str, ...] | list[str] | None = None,
) -> ResearchTurnValidation:
    """状態整合性と、LLMが参照したIDの出所だけを検証する。

    法的に十分か、どの検索順がよいかはここでは再判定しない。
    """
    errors: list[str] = []
    visible_content_ids = set(
        catalog.content_unit_ids
        if allowed_content_unit_ids is None
        else allowed_content_unit_ids
    )
    known_article_ids = set(catalog.known_article_ids)
    known_document_ids = set(catalog.known_document_ids)
    selected_ids = tuple(
        dict.fromkeys(item.contentUnitId for item in turn.selectedEvidence)
    )
    hypothesis_ids = {item.hypothesisId for item in turn.hypotheses}
    if len(hypothesis_ids) != len(turn.hypotheses):
        errors.append("duplicate_hypothesis_id")
    if turn.actions and not hypothesis_ids:
        errors.append("actions_require_hypothesis")

    for hypothesis in turn.hypotheses:
        for evidence_id in hypothesis.evidenceIds:
            if evidence_id not in visible_content_ids:
                errors.append(
                    f"unknown_hypothesis_evidence_id:"
                    f"{hypothesis.hypothesisId}:{evidence_id}"
                )
        if hypothesis.status != "unverified" and not hypothesis.evidenceIds:
            errors.append(
                f"evaluated_hypothesis_requires_evidence:"
                f"{hypothesis.hypothesisId}"
            )

    for content_unit_id in selected_ids:
        if content_unit_id not in visible_content_ids:
            errors.append(f"unknown_evidence_id:{content_unit_id}")

    for index, action in enumerate(turn.actions):
        if hypothesis_ids and not action.hypothesisIds:
            errors.append(f"action_requires_hypothesis_id:actions[{index}]")
        for hypothesis_id in action.hypothesisIds:
            if hypothesis_id not in hypothesis_ids:
                errors.append(
                    f"unknown_action_hypothesis_id:"
                    f"actions[{index}]:{hypothesis_id}"
                )
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
    *,
    allowed_content_unit_ids: tuple[str, ...] | list[str] | None = None,
    required_issue_ids: tuple[str, ...] | list[str] = (),
    final_cycle: bool = False,
    require_structured_follow_up: bool = False,
) -> ResearchCheckpointValidation:
    """結論が、取得済みの原文・Graph確認済みArticleだけを参照するか検証する。"""
    visible_content_ids = set(
        catalog.content_unit_ids
        if allowed_content_unit_ids is None
        else allowed_content_unit_ids
    )
    known_article_ids = set(catalog.known_article_ids)
    errors: list[str] = []
    selected_ids = tuple(dict.fromkeys(checkpoint.evidenceIds))
    open_ids = tuple(dict.fromkeys(checkpoint.openEvidenceIds))
    issue_ids = [
        issue.issueId for issue in checkpoint.logicalStructure.issues
    ]
    if len(set(issue_ids)) != len(issue_ids):
        errors.append("duplicate_issue_id")
    for issue_id in dict.fromkeys(required_issue_ids):
        if issue_id not in issue_ids:
            errors.append(f"missing_previous_issue_id:{issue_id}")
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
            if node.verificationStatus == "text_verified" and node.articleId:
                wrong_article_evidence = [
                    evidence_id
                    for evidence_id in node.evidenceIds
                    if any(
                        str(item.get("articleId") or "") != node.articleId
                        for item in catalog.items_by_ids([evidence_id])
                    )
                ]
                if wrong_article_evidence:
                    errors.append(
                        f"text_verified_evidence_article_mismatch:{node.nodeId}"
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
            if claim.status == "verified":
                referenced_nodes = [
                    node
                    for node in issue.authorityNodes
                    if node.nodeId in claim_node_ids
                ]
                if not referenced_nodes or not any(
                    node.verificationStatus == "text_verified"
                    for node in referenced_nodes
                ):
                    errors.append(
                        f"verified_claim_requires_text_verified_authority:"
                        f"{issue.issueId}:{claim.claimId}"
                    )
        if issue.status == "verified":
            if not any(
                node.verificationStatus == "text_verified"
                for node in issue.authorityNodes
            ):
                errors.append(
                    f"verified_issue_requires_text_verified_authority:"
                    f"{issue.issueId}"
                )
            if any(claim.status != "verified" for claim in issue.claims):
                errors.append(
                    f"verified_issue_requires_verified_claims:{issue.issueId}"
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
    for hypothesis in checkpoint.logicalStructure.hypotheses:
        for evidence_id in hypothesis.evidenceIds:
            if evidence_id not in visible_content_ids:
                errors.append(
                    f"unknown_hypothesis_evidence_id:"
                    f"{hypothesis.hypothesisId}:{evidence_id}"
                )
        if hypothesis.status != "unverified" and not hypothesis.evidenceIds:
            errors.append(
                f"evaluated_hypothesis_requires_evidence:"
                f"{hypothesis.hypothesisId}"
            )
    for decision in checkpoint.logicalStructure.relationDecisions:
        assertion = catalog.relation_assertion(decision.assertionId)
        if assertion is None:
            errors.append(
                f"unknown_relation_assertion_id:{decision.assertionId}"
            )
            continue
        if (
            decision.fromArticleId != assertion.get("fromArticleId")
            or decision.toArticleId != assertion.get("toArticleId")
            or decision.relationType != assertion.get("suggestedType")
        ):
            errors.append(
                f"relation_decision_candidate_mismatch:{decision.assertionId}"
            )
        decision_evidence_ids = tuple(dict.fromkeys(decision.evidenceIds))
        for evidence_id in decision_evidence_ids:
            if evidence_id not in visible_content_ids:
                errors.append(
                    f"unknown_relation_decision_evidence_id:"
                    f"{decision.assertionId}:{evidence_id}"
                )
        if decision.verdict in {"confirmed", "rejected"}:
            covered_articles = {
                str(item.get("articleId") or "")
                for item in catalog.items_by_ids(list(decision_evidence_ids))
            }
            required_articles = {
                decision.fromArticleId,
                decision.toArticleId,
            }
            if not required_articles.issubset(covered_articles):
                errors.append(
                    f"relation_decision_requires_both_article_texts:"
                    f"{decision.assertionId}"
                )
    if checkpoint.status == RESEARCH_STATUS_READY and not selected_ids:
        errors.append("ready_requires_selected_evidence")
    if checkpoint.status == RESEARCH_STATUS_READY:
        if any(
            item.affectsCoreConclusion
            for item in checkpoint.logicalStructure.unresolved
        ):
            errors.append("ready_has_unresolved_core_item")
        selected_id_set = set(selected_ids)
        for issue in checkpoint.logicalStructure.issues:
            if issue.status != "verified":
                errors.append(
                    f"ready_requires_verified_issue:{issue.issueId}"
                )
                continue
            issue_evidence_ids = {
                evidence_id
                for node in issue.authorityNodes
                for evidence_id in node.evidenceIds
            }
            if not selected_id_set.intersection(issue_evidence_ids):
                errors.append(
                    f"ready_issue_requires_selected_evidence:{issue.issueId}"
                )
    if require_structured_follow_up and checkpoint.status == RESEARCH_STATUS_CONTINUE and not (
        checkpoint.nextQuestions
        or checkpoint.nextArticleIds
        or checkpoint.logicalStructure.unresolved
    ):
        errors.append("continue_requires_structured_follow_up")
    if final_cycle and checkpoint.status == RESEARCH_STATUS_CONTINUE:
        errors.append("final_cycle_requires_terminal_status")
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
    *,
    allowed_content_unit_ids: tuple[str, ...] | list[str] | None = None,
) -> tuple[ResearchCheckpoint, dict[str, list[str]]]:
    """未確認IDだけを除外し、検証可能な統合結果を安全側で回収する。

    IDを推測で補正したり、未知Articleを既知扱いしたりはしない。何かを除外した場合は
    統合済みとはみなさずstatusをcontinueへ戻し、次サイクルで再確認させる。
    """
    visible_content_ids = set(
        catalog.content_unit_ids
        if allowed_content_unit_ids is None
        else allowed_content_unit_ids
    )
    known_article_ids = set(catalog.known_article_ids)
    changes: dict[str, list[str]] = {
        "removedEvidenceIds": [],
        "removedOpenEvidenceIds": [],
        "removedNextArticleIds": [],
        "removedAuthorityNodeIds": [],
        "removedClaimAuthorityNodeIds": [],
        "removedUnresolvedItems": [],
        "downgradedAuthorityNodeIds": [],
        "downgradedReadyStatus": [],
        "downgradedHypothesisIds": [],
        "removedRelationDecisionIds": [],
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

    sanitized_hypotheses: list[ResearchHypothesis] = []
    for hypothesis in checkpoint.logicalStructure.hypotheses:
        evidence_ids_for_hypothesis = [
            evidence_id
            for evidence_id in dict.fromkeys(hypothesis.evidenceIds)
            if evidence_id in visible_content_ids
        ]
        status = hypothesis.status
        missing = list(hypothesis.missing)
        if status != "unverified" and not evidence_ids_for_hypothesis:
            status = "unverified"
            if "根拠本文" not in missing:
                missing.append("根拠本文")
            changes["downgradedHypothesisIds"].append(
                hypothesis.hypothesisId
            )
        sanitized_hypotheses.append(
            hypothesis.model_copy(
                update={
                    "status": status,
                    "evidenceIds": evidence_ids_for_hypothesis,
                    "missing": missing,
                }
            )
        )

    sanitized_relation_decisions: list[ResearchRelationDecision] = []
    for decision in checkpoint.logicalStructure.relationDecisions:
        assertion = catalog.relation_assertion(decision.assertionId)
        retained_evidence = [
            evidence_id
            for evidence_id in dict.fromkeys(decision.evidenceIds)
            if evidence_id in visible_content_ids
        ]
        covered_articles = {
            str(item.get("articleId") or "")
            for item in catalog.items_by_ids(retained_evidence)
        }
        matches_candidate = bool(
            assertion
            and decision.fromArticleId == assertion.get("fromArticleId")
            and decision.toArticleId == assertion.get("toArticleId")
            and decision.relationType == assertion.get("suggestedType")
        )
        has_required_texts = {
            decision.fromArticleId,
            decision.toArticleId,
        }.issubset(covered_articles)
        if (
            not matches_candidate
            or (
                decision.verdict in {"confirmed", "rejected"}
                and not has_required_texts
            )
        ):
            changes["removedRelationDecisionIds"].append(
                decision.assertionId
            )
            continue
        sanitized_relation_decisions.append(
            decision.model_copy(update={"evidenceIds": retained_evidence})
        )

    if checkpoint.status == RESEARCH_STATUS_READY and (
        any(item.affectsCoreConclusion for item in sanitized_unresolved)
    ):
        # 内容自体は検証可能であり、次Articleも既知ならCheckpointを捨てない。
        # readyだけをcontinueへ戻し、次サイクルのTaskを確実に残す。
        changes["downgradedReadyStatus"].append(
            "unresolved_core_evidence"
        )

    changes = {
        key: list(dict.fromkeys(values))
        for key, values in changes.items()
        if values
    }
    sanitized_structure = checkpoint.logicalStructure.model_copy(
        update={
            "issues": sanitized_issues,
            "hypotheses": sanitized_hypotheses,
            "unresolved": sanitized_unresolved,
            "relationDecisions": sanitized_relation_decisions,
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
    case_context: dict[str, Any] | None = None,
    output_token_limit: int = 4096,
) -> str:
    """探索手順を細かく規定せず、目的・利用可能ツール・証拠境界だけを伝える。"""
    choices_block = ""
    if choices:
        choices_block = "\n選択肢:\n" + "\n".join(
            f"{label.upper()}: {text}" for label, text in sorted(choices.items())
        )
    prompt_content_ids = _research_turn_candidate_content_ids(
        catalog=catalog,
        checkpoint=checkpoint,
        preferred_content_ids=preferred_content_ids,
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
    visible_evidence_ids = {
        str(item.get("contentUnitId") or "") for item in evidence
    }
    if finalize_only:
        inventory = []
    elif checkpoint is not None:
        inventory = catalog.prompt_inventory_by_ids(
            [
                content_unit_id
                for content_unit_id in prompt_content_ids
                if content_unit_id not in visible_evidence_ids
            ],
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
    relation_assertions = _research_prompt_relation_assertions(
        catalog,
        checkpoint=checkpoint,
        tool_history=tool_history or [],
        decided_assertion_ids=_case_decided_relation_ids(case_context),
        max_items=24,
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

情報が多い場合は、status、仮説IDと検証結果、実行すべきactionsとArticle ID、
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
最初に、質問に現れた重要な事実・条件・求められた結論を確認し、それらを最もよく
説明する検証可能な暫定結論をhypothesesへ置いてください。
質問が「対象、例外、手続」「条件、対象者、期間」のように複数事項を明示している場合は、
各事項を別々の検証対象として保持してください。質問にない周辺的な実装方法を追加して、
明示された事項を置き換えてはいけません。
有力な別の法的構成がある場合だけ競合仮説を残し、件数を満たすために弱い可能性を
並べないでください。
各仮説について「何が確認できれば支持・反証・他仮説との識別ができるか」を考え、
その判定に必要な操作だけをActionにしてください。
前サイクルの仮説IDは同じ仮説について維持しますが、取得済み情報と合わない、または
質問の重要な特徴を説明できない場合は、仮説を修正・棄却し、必要なら別仮説を追加してください。
各ActionのhypothesisIdsとreasonに、検証対象と確認目的を明示してください。
"""
    elif phase == "deepen":
        phase_notice = f"""
これは全{cycle_count}回のうち第{(cycle_index or 0) + 1}回の掘り下げ段階です。
直前の探索結果を、単に似ているかではなく、各仮説が予測する要件・対象・例外・手続等を
本文が実際に定めているかで検証してください。
主仮説を支持する読み方だけでなく、反証、適用範囲の不一致、質問の重要な特徴を説明できない点、
競合仮説のほうがよく説明できる可能性も比較してください。
本文が仮説の一部だけを支えるならpartially_supported、十分に支えるならsupported、
明確に矛盾するならrejected、判断材料がなければunverifiedとします。
検証に使ったevidenceIdsと、判断を変え得る未確認事項をmissingへ残してください。
仮説が合わないと示唆されたときは、同じ仮説の周辺検索を続けず、修正・棄却・
競合仮説の追加を行ってください。
検索で得たArticleの項・号確認にはfetch_articlesを優先してください。
"""
    prompt_history = _prompt_tool_history(
        tool_history or [],
        finalize_only=finalize_only,
    )
    return f"""あなたは、日本法令について根拠を収集する調査責任者です。
プログラムは、許可された検索・本文取得の実行、IDの実在性、TaskとCheckpointの
状態遷移、時間・件数上限、禁止事項の検証を担当します。法的な論点、検索語、候補の比較、
条文間のつながり、追加調査の要否、最終根拠の選択はあなたが判断してください。
調査方法、検索語、探索順序はあなたが判断してください。
固定の法的役割や検索順に質問を当てはめず、質問と取得本文から必要な調査を組み立ててください。

使用できる操作:
- search_corpus: このシステムに投入済みの法令・ガイドを検索する
- fetch_articles: 既知のArticle IDから本文を直接取得する
- expand_graph: 既知のArticle IDから確認済み関係と未確認の関係候補を取得する

仮説検証の中心原則:
- 各サイクルで「質問の重要な特徴を確認する→仮説を立てる→
  仮説を識別できる証拠を取得する→本文で支持・反証を比較する→
  仮説を維持・修正・棄却・追加する」を行う
- hypothesesは検索語ではなく、質問へ答えるための検証可能な暫定結論とする
- hypothesisIdは同じ仮説についてサイクル間で維持し、言い換えのたびに作り直さない
- statusをunverified以外にする場合は、確認済みevidenceIdsを残す
- hypothesesには質問へ答えるために検証が必要な仮説だけを置き、周辺的な可能性を
  無制限に追加しない
- 一律に複数仮説を作るのではなく、質問を同程度に説明し得る有力な別構成が
  あるときだけ競合仮説を残す
- 主仮説が質問の重要な特徴を説明できない場合、関連条文が見つかったことだけで
  その仮説を支持しない。別仮説の追加・交代を検討する
- 検索・取得ActionのhypothesisIdsには、その操作で検証する仮説IDを指定する
- Actionのreasonには、何を確認し、どの結果なら仮説の支持・反証・識別に役立つかを
  簡潔に書く
- 学習済み知識は検索語や仮説を考えるために使えるが、法的結論の根拠にはしない
- 結論ごとに、実際に取得した法令本文で根拠を確認する
- 複数法令、委任先、定義、準用、例外、適用範囲が関係し得るときは、必要性を自ら検討する
- 例外・免除・特則を結論にする場合は、取得できる限り、本則・委任元と例外・免除・
  具体化規定の双方を直接確認する。他条が本則に言及しているだけの間接根拠で代用しない
- 義務、禁止、届出、許可の要否を回答する場合、その義務等を直接定める本則Articleを
  本文確認する。定義条文、告知規定、制裁規定だけで本則本文を代用しない
- 質問が対象範囲、例外、手続の具体的内容を求め、対応する下位法令文書が利用可能なら、
  Graph候補だけを待たず、その文書内も検索して具体化Articleの本文を確認する
- 候補Taskの条見出しが質問の制度名や未確認事項と直接一致する場合は、類似検索を
  繰り返す前にfetch_articlesで本文を確認する
- 確認済みGraphに、回答で使う条文を直接具体化する下位法令Articleがある場合は、
  質問への具体的回答に必要か判断し、必要なら本文取得を未解決事項として残す
- ガイドは法令本文ではない。法令間のつながりを探す手掛かりや行政解釈として区別する
- 質問と似ているだけの条文を、必要な根拠が揃った証拠とみなさない
- 前回のlogicalStructureを読み、論点→結論→根拠→委任・定義・例外等の関係を
  保ったまま今回の仮説・Action判断へ使う。Article IDだけを見て関係の意味を捨てない。
  この段階の出力にはlogicalStructureを追加せず、更新案はhypotheses、actions、
  selectedEvidence、missingEvidenceだけで表す
- 確認済みGraph関係も検索ナビゲーションであり、本文未取得の法令を最終根拠とは扱わない
- RelationAssertionは未確認の関係候補である。この探索段階では質問に関係しそうかを判断し、
  結論へ影響し得る場合だけ両端本文をfetch_articlesする。関係の意味判断は統合段階で行う
- RelationAssertionのsuggestedType、status、文言検出シグナル、sourceTextだけで法的関係を確定しない
- selectedEvidenceには、利用可能な証拠にあるcontentUnitIdだけを指定する
- selectedEvidenceには、最終回答で実際に根拠として使う本文だけを理由付きで指定する
- selectedEvidenceとhypotheses.evidenceIdsには、下の「本文を確認できる証拠」に
  実際に表示されたcontentUnitIdだけを使う。候補一覧のtextPreviewだけを根拠に選ばない
- textTruncated=trueの本文は表示部分だけが確認済みである。表示されていない末尾に
  要件・例外が無い、又は列挙が完結したとは判断しない。末尾確認が結論に必要なら
  同じArticleをfetch_articlesするか、missingEvidenceへ残す
- selectedEvidenceは最大{max_selected_evidence}件とし、同じ結論を支える重複項号を
  網羅的に並べず、回答に必要な最小限の本文を選ぶ
- 取得できていないArticle IDを推測してfetch_articlesやexpand_graphへ渡さない
- Article IDは出力スキーマが許可する候補からそのまま選び、大文字小文字、
  ハイフン、アンダースコアを書き換えない
- 調べたい法令名・条番号に対応するArticle IDが候補にない場合は、IDを
  組み立てず、法令名・条番号・確認目的をqueryに書いてsearch_corpusを使う
- missingの自由文からプログラムが次の調査対象を推測すると期待しない。
  次に取得すべき既知Articleがあるならfetch_articlesを明示し、IDが不明なら
  法令名・条番号・検証目的を持つsearch_corpusを明示する
- documentIdsを指定する場合は、利用可能な文書にあるIDだけを使用する
- 質問が明示して求めた各事項を取得済み法令で説明できればreadyとする。考え得る全ての例外、
  周辺制度、質問が求めていない手続まで網羅する必要はないが、明示された事項は省略しない
- 未確認事項が中心的な結論を変えず、回答上の留保として明示できる場合はreadyとする
- readyを返す直前に、結論を支える法令本文を確認してselectedEvidenceへ選択したか、
  自分が本文確認を必要と判断した、前回CheckpointのArticleや今回の未確認事項が
  未取得のままではないかを見直す
- そのArticleを残りの操作・時間で取得できるなら、readyにせずfetch_articlesで確認する。
  これは質問に明示されていない全論点の網羅を要求するものではない。質問が明示した事項と、
  自分が回答前に必要と判断した本文の取得漏れを防ぐための最終チェックである
- 取得できない場合は探索を無期限に続けない。中心的結論へ影響するならinsufficient、
  影響しないなら未確認事項と回答への影響をmissingEvidenceへ明示してreadyとする
- search_corpusはArticleの代表本文を返す。候補Articleの他の項・号を確認する場合は、
  類似した検索を繰り返さずfetch_articlesを使う
- 根拠が十分ならready、不足を具体化して追加調査できるならcontinue、
  予算内では根拠を確認できないならinsufficientとする
- status=continueではactionsを1件以上設定する。status=readyまたはinsufficientでは
  actionsを空配列にする。status=readyではselectedEvidenceを1件以上設定する
- 残りツール呼び出し数またはactions上限が0ならcontinueを返さず、確認済み範囲に応じて
  readyまたはinsufficientを選ぶ
- これまでの操作にvalidationErrorsがある場合、その出力は受理されていない。同じ誤りを
  繰り返さず、今回のstatus・actions・IDをこの契約へ適合させる
- insufficientでも、確認済みで回答の限定に役立つ証拠があればselectedEvidenceへ含める
- 1ターンのactionsは最大{max_actions}件とする
- 各ActionのarticleIdsは最大20件、documentIdsとedgeTypesは各最大10件、
  hypothesisIdsは最大8件とする。各hypothesisのevidenceIdsは最大12件、missingは最大6件、
  missingEvidenceは最大12件とする
- 案件状態のcandidateTasksは、検索・Graphで存在を確認したが法的必要性を
  まだ確定していない作業候補である。中心的結論に必要なら対応Articleを
  fetch_articlesし、不要なら無理に処理しない
- latestCheckpoint以降のeventsAfterCheckpointは統合失敗後も残った確認済み差分であり、
  前回Checkpointに無いという理由で無視しない
- 必ずJSONだけを返す

{output_budget_notice}
{budget_notice}
{finalization_notice}
{phase_notice}

質問: {question}{choices_block}

前回までの調査状態:
{json.dumps(checkpoint.model_dump() if checkpoint else {}, ensure_ascii=False)}

案件状態（確認済み事実の正本から作った今回用View）:
{json.dumps(_case_context_for_prompt(case_context), ensure_ascii=False)}

これまでの操作:
{json.dumps(prompt_history, ensure_ascii=False)}

利用可能な文書（法令名を特定できる場合はsearch_corpusのdocumentIdsで限定できる）:
{json.dumps(catalog.prompt_documents(), ensure_ascii=False)}

候補一覧（本文が省略された候補はArticle IDをfetch_articlesして確認できる）:
{json.dumps(inventory, ensure_ascii=False)}

Graph・索引時分類済みナビゲーション関係（本文根拠ではない）:
{json.dumps(graph_relations, ensure_ascii=False)}

未確認のGraph関係候補（必要なら両端本文を取得して統合段階で判断する）:
{json.dumps(relation_assertions, ensure_ascii=False)}

本文を確認できる証拠:
{json.dumps(evidence, ensure_ascii=False)}

JSON:"""


def research_turn_prompt_content_ids(
    *,
    catalog: EvidenceCatalog,
    checkpoint: ResearchCheckpoint | None,
    preferred_content_ids: tuple[str, ...] | list[str],
    max_evidence_items: int,
    evidence_chars: int,
) -> tuple[str, ...]:
    """探索LLMへ本文を一文字以上提示した原文IDだけを返す。"""
    candidate_ids = _research_turn_candidate_content_ids(
        catalog=catalog,
        checkpoint=checkpoint,
        preferred_content_ids=preferred_content_ids,
    )
    evidence = (
        _prompt_items_by_ids(
            catalog,
            candidate_ids,
            max_items=max_evidence_items,
            max_chars=evidence_chars,
        )
        if checkpoint is not None
        else catalog.prompt_items(
            max_items=max_evidence_items,
            max_chars=evidence_chars,
            preferred_content_ids=preferred_content_ids,
        )
    )
    return tuple(
        str(item.get("contentUnitId") or "")
        for item in evidence
        if item.get("contentUnitId")
        and (item.get("text") or not item.get("originalTextChars"))
    )


def _research_turn_candidate_content_ids(
    *,
    catalog: EvidenceCatalog,
    checkpoint: ResearchCheckpoint | None,
    preferred_content_ids: tuple[str, ...] | list[str],
) -> tuple[str, ...]:
    prompt_content_ids = list(preferred_content_ids)
    if checkpoint is not None:
        unresolved_article_ids = [
            item.articleId
            for item in checkpoint.logicalStructure.unresolved
            if item.articleId
        ]
        prompt_content_ids.extend(checkpoint.evidenceIds)
        prompt_content_ids.extend(checkpoint.openEvidenceIds)
        prompt_content_ids.extend(
            catalog.content_ids_for_article_ids(
                [*checkpoint.nextArticleIds, *unresolved_article_ids]
            )
        )
    return tuple(dict.fromkeys(prompt_content_ids))


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
    answer_evidence_limit: int | None = None,
    case_context: dict[str, Any] | None = None,
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
            "hypothesisIds": item.get("hypothesisIds") or [],
            "resultCount": item.get("resultCount"),
            "newEvidenceCount": item.get("newEvidenceCount"),
            "newArticleIds": item.get("newArticleIds") or [],
            "autoGraphArticleIds": item.get("autoGraphArticleIds") or [],
            "graphRelationCount": len(item.get("graphRelations") or []),
            "relationAssertionCount": len(
                item.get("relationAssertions") or []
            ),
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
    relation_assertions = _research_prompt_relation_assertions(
        catalog,
        checkpoint=checkpoint,
        tool_history=tool_history or [],
        decided_assertion_ids=_case_decided_relation_ids(case_context),
        max_items=24,
    )
    effective_answer_evidence_limit = min(
        max_selected_evidence,
        answer_evidence_limit or max_selected_evidence,
    )
    cycle_completion_notice = (
        """
- これは最終サイクルである。status=continueは禁止し、確認済み本文で中心的結論を
  支持できるならready、できないならinsufficientを選ぶ。最終回でも未確認事項は
  nextQuestionsまたはunresolvedへ記録できるが、次回実行を前提にしない
"""
        if cycle_index + 1 >= cycle_count
        else """
- status=continueでは、次サイクルが実行できる具体的な作業を必ず残す。
  既知ArticleならnextArticleIds、不明ならnextQuestionsを設定し、対応するunresolvedも残す
"""
    )
    return f"""あなたは、日本法令の反復調査を統合する責任者です。
これは全{cycle_count}回のうち第{cycle_index + 1}回の調査サイクルの統合段階です。
今回の探索・掘り下げで得た原文とGraph関係を読み、次サイクルに必要な結論と
法的論理構造へ圧縮してください。

規則:
- logicalStructure.hypothesesへ、今回扱った仮説と検証結果を平坦な配列で保存する
- 案件状態のrelationDecisionsは引き継ぐ。質問の結論へ影響するRelationAssertionについて、
  両端Article本文を取得済みなら新しいrelationDecisionを作り、案件内の判断として保存する
- relationDecisionのconfirmed / rejectedは両端Article本文の意味を比較して判断する。
  本文が不足する、又は意味が一意に決まらない場合はuncertainとし、候補メタデータだけで確定しない
- relationDecisionには提示されたassertionId、verdict、evidenceIds、reasonだけを出力する。
  新しいIDや関係種別を作らず、質問に無関係な候補はrelationDecisionsへ追加しない
- relationDecisionは案件内の調査判断であり、正式Graphエッジや他案件の法的事実へ昇格させない
- 質問が明示して求める各事項をlogicalStructureのIssueまたはClaimへ一つずつ対応させる。
  例えば「条件、対象者、期間」を求める質問で、関連する別制度の実装方法を追加する一方、
  対象者や期間を省略してはならない
- 前回CheckpointにあるIssueは、後のサイクルでより重要な根拠が見つかっても省略しない。
  同じ明示事項についてissueIdを維持し、結論や根拠を更新する。確認が不十分になった場合は
  Issueを消さずpartialまたはunresolvedへ変更する
- 同じ仮説のhypothesisIdは前回から維持し、statementの軽微な言い換えで作り直さない
- 各仮説はhypothesisId、statement、status、evidenceIds、missingの5項目だけを持つ
- 配列上限はissues 4件、hypotheses 8件、unresolved 6件、relationDecisions 8件とする。
  全Issueを通じてclaimsは合計8件、authorityNodesは合計20件を超えない
- 統合の最初に、質問の重要な事実・条件・求められた結論を各仮説がどこまで説明できるかを
  比較する。取得した条文が関連するというだけで、質問全体を説明できない仮説を支持しない
- 本文が仮説の予測する要件・対象・例外・手続等を十分に支持すればsupported、
  明確に反証すればrejected、一部だけ支持して判断を変え得る確認事項が残るなら
  partially_supported、まだ検証できなければunverifiedとする
- 有力な競合仮説がある場合は、支持証拠の件数だけでなく、質問の重要な特徴をどちらが
  よりよく説明するか、適用範囲の不一致や反証がないかを比較する
- 前回の仮説と合わない情報が得られた場合は、その仮説を詳しくするだけで済ませず、
  statementの実質的修正、rejectedへの変更、または新しい仮説の追加を行う
- rejectedとなった仮説も、なぜ退けたか追跡できる確認済みevidenceIdsとともに残す
- unverifiedまたはpartially_supportedの仮説が残る場合、それが質問の中心的結論を変え得るかを
  あなたが判断する。変え得るならreadyにせず、後続サイクルがあればcontinue、取得不能なら
  insufficientとする。中心的結論へ影響しないなら、未確認範囲と影響をmissingおよび
  unresolved(affectsCoreConclusion=false)へ明示したうえでreadyにできる
- conclusionには、次サイクルが再検証すべき現在の結論だけを1〜3文で書く
- 調査経緯、検索語、根拠の選択理由、条文の長い説明は書かない
- evidenceIdsには、結論または最終回答に実際に使う取得済み原文IDだけを最大10件指定する
- evidenceIds、openEvidenceIds、hypotheses.evidenceIds、authorityNodes.evidenceIds、
  relationDecisions.evidenceIdsには、下の原文欄に実際に表示されたcontentUnitIdだけを使う
- textTruncated=trueの原文は表示部分だけが確認済みである。表示されていない末尾に
  要件・例外が無い、又は列挙が完結したとは判断しない。末尾が必要なら未確認として残す
- evidenceIdsの順序もあなたの法的判断で決める。最終回答へ渡される先頭
  {effective_answer_evidence_limit}件だけで、質問が明示して求めた各事項を直接検証できる
  自己完結した根拠集合にする。発生条件、対象、例外、手続などが併記されている場合、
  中心的結論や数値要件だけで枠を使い切らず、各事項の根拠を残す。同じArticleの親・項・号が
  同じ主張を重複して支える場合は、明示事項の根拠を落としてまで重複採用しない。
  取得順や法令階層名だけで並べない
- 各候補本文の冒頭にある委任元・参照先を読み、質問対象とは別の条項に対する例外・手続を
  質問対象の根拠として選ばない。適用関係はLLM自身が本文とGraph関係から判断する
- openEvidenceIdsには、本文取得済みだが結論との関係を次回も確認する原文IDだけを
  最大20件指定する。evidenceIdsと重複させない
- nextQuestionsには、質問への回答に必要だが未確認の事項だけを最大3件指定する
- nextArticleIdsには、Graphまたは検索で存在を確認済みだが本文未取得で、
  次回読むべきArticle IDだけを最大10件指定する
- missingは仮説の未確認点を説明する記録であり、プログラムが自由文を解釈して
  Taskへ変換する命令欄ではない。次サイクルで実行すべき調査は、既知Articleなら
  nextArticleIds、不明ならnextQuestionsとunresolvedへ構造化して残す
- 統合schemaはサイズ制約のためArticle IDをenumで列挙していない。articleIdと
  nextArticleIdsには、前回状態、案件View、Graph関係、関係候補、原文欄に実際に表示された
  Article単位IDだけを完全一致で使い、表記を変更しない
- `...-paragraph-...`、`...-item-...`等を含む項・号のcontentUnitIdをarticleIdへ
  入れない。本文のcontentUnitIdは各evidenceIdsへ入れる
- 調べたい法令名・条番号に対応するArticle IDが候補にない場合は、
  未確認IDを作らず、nextQuestionsとunresolved(action=search, articleId=null)に
  法令名・条番号・確認目的を残す
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
  であり、Claimの中へauthorityNodesを入れない。仮説は
  logicalStructure.hypothesesへ一度だけ保存し、IssueやClaimへ複製しない
- 同じ論点・結論のissueIdとclaimIdは次サイクルでも維持し、新しい根拠は既存の
  IssueのauthorityNodesへ追加する。既存nodeIdも調査のたびに作り直さない
- Graphで関係だけ確認した本文未取得Articleはgraph_verifiedまたは
  text_not_fetchedとし、text_verifiedにしない
- text_verifiedには、そのArticleの取得済みevidenceIdsを指定する
- graph_verifiedは確認済みGraph関係で存在を確認したが本文未取得のArticle、
  text_not_fetchedは検索・候補Task等で存在を確認したが本文未取得のArticleに使う
- Claimをverifiedにする場合はauthorityNodeIdsを空にせず、その中に少なくとも一つ
  text_verifiedの根拠ノードを含める。Issueをverifiedにする場合は少なくとも一つ
  text_verifiedの根拠ノードを持ち、Claimがある場合はその全Claimをverifiedにする
- 例外・免除・特則を結論に使う場合、本則・委任元と例外・免除・具体化規定の双方が
  取得済みなら、Claimから両方のauthorityNodeを参照し、最終回答に必要な原文を
  evidenceIdsへ残す。他条による言及だけで本則本文を置き換えない
- 確認済みGraph上の下位法令Articleが結論の具体的内容を定める可能性があり、
  本文未取得なら、中心的結論への影響を判断してunresolvedとnextArticleIdsへ残す
- unresolvedには、どのissue・claimの何が不足し、中心的結論へ影響するかを記録する
- nextQuestionsとnextArticleIdsはlogicalStructure.unresolvedと整合させる
- status=readyにする場合、中心的結論へ影響するunresolvedを残さない
- status=readyにする場合、質問が明示して求めた各事項に対応するIssueまたはClaimを残し、
  それぞれをverifiedにする。明示事項をlogicalStructureから省略してreadyにしてはならない
- status=readyにする場合、各Issueのtext_verified根拠を少なくとも1件、トップレベルの
  evidenceIdsへ選ぶ。論理構造にだけ根拠を残して最終回答への根拠集合から落としてはならない
- status=readyを確定する直前に、結論を支える法令本文をevidenceIdsへ選択したか、
  自分が本文確認を必要と判断してnextArticleIdsまたはunresolvedへ残したArticleが
  未取得のままではないかを見直す。後続サイクルで取得可能ならstatus=continueとし、
  nextArticleIdsへ残す
- これは質問に明示されていない全論点の完全調査を要求するものではない。質問が明示した事項の
  根拠が取得不能、又は最終サイクルでも不足する場合は無期限に継続せずinsufficientとし、
  限定回答に使える確認済み根拠はevidenceIdsへ残す。明示事項へ影響しない周辺的な未確認事項だけなら、
  未確認事項と影響を残したうえでreadyにできる
- conclusion、nextQuestions、nextArticleIdsで同じ説明を繰り返さない
- statusは、中心的な結論を原文で説明できればready、次回で補えるならcontinue、
  投入済み資料では確認できないならinsufficientとする
{cycle_completion_notice}
- 案件状態のcandidateTasksとeventsAfterCheckpointは統合の成否とは独立して
  CaseStoreへ確定済みである。中心的結論に必要な未取得Articleがあれば
  LLM自身が候補の法令名・見出し・仮説との関係を比較してnextArticleIdsへ残す。
  プログラムが見出し類似やmissingの文字列から自動選択するとは期待しない
- 第{cycle_index + 1}回でも、後続サイクルがある場合はreadyの結論を反証・補完対象として引き継ぐ
- 必ずJSONだけを返す

質問: {question}{choices_block}

前回までの調査状態:
{json.dumps(checkpoint.model_dump(), ensure_ascii=False)}

案件状態（確認済み事実の正本から作った今回用View）:
{json.dumps(_case_context_for_prompt(case_context), ensure_ascii=False)}

今回のツール実行要約:
{json.dumps(tool_summary, ensure_ascii=False)}

現在確認できるGraph・索引時分類済みナビゲーション関係:
{json.dumps(graph_relations, ensure_ascii=False)}

未分類またはuncertainのGraph関係候補:
{json.dumps(relation_assertions, ensure_ascii=False)}

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
        prompt_item = _render_prompt_evidence_item(item, text_budget)
        if prompt_item is None:
            break
        used = metadata_chars + len(prompt_item["text"])
        if used <= 0:
            continue
        output.append(prompt_item)
        remaining -= used
    return output


def _render_prompt_evidence_item(
    item: dict[str, Any],
    text_budget: int,
) -> dict[str, Any] | None:
    """表示した本文範囲を明示し、本文ゼロ件を証拠欄へ混ぜない。"""
    original_text = str(item.get("text") or "")
    displayed_text = original_text[: max(0, text_budget)]
    if original_text and not displayed_text:
        return None
    return {
        **item,
        "text": displayed_text,
        "textTruncated": len(displayed_text) < len(original_text),
        "originalTextChars": len(original_text),
        "displayedTextChars": len(displayed_text),
    }


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


def _research_prompt_relation_assertions(
    catalog: EvidenceCatalog,
    *,
    checkpoint: ResearchCheckpoint | None,
    tool_history: list[dict[str, Any]],
    max_items: int,
    decided_assertion_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """同一サイクルで発見した候補と、継続判断中の候補だけを表示する。"""
    decided_ids = set(decided_assertion_ids or ())
    decided_ids.update({
        decision.assertionId
        for decision in (
            checkpoint.logicalStructure.relationDecisions
            if checkpoint is not None
            else []
        )
        if decision.verdict in {"confirmed", "rejected"}
    })
    current = [
        dict(assertion)
        for item in tool_history
        for assertion in (item.get("relationAssertions") or [])
        if isinstance(assertion, dict)
        and str(assertion.get("assertionId") or "") not in decided_ids
    ]
    article_ids = (
        _checkpoint_article_ids(checkpoint, catalog)
        if checkpoint is not None
        else set()
    )
    retained = catalog.prompt_relation_assertions(
        article_ids=article_ids,
        exclude_assertion_ids=decided_ids,
        max_items=max_items,
    )
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for assertion in [*current, *retained]:
        assertion_id = str(assertion.get("assertionId") or "")
        if not assertion_id or assertion_id in seen:
            continue
        seen.add(assertion_id)
        output.append(dict(assertion))
        if len(output) >= max(0, max_items):
            break
    return output


def _case_decided_relation_ids(
    case_context: dict[str, Any] | None,
) -> set[str]:
    """CaseStoreで確定・否定済みの候補IDを、再提示防止にだけ使う。"""
    return {
        str(item.get("assertionId") or "")
        for item in (case_context or {}).get("relationDecisions", [])
        if isinstance(item, dict)
        and item.get("verdict") in {"confirmed", "rejected"}
        and item.get("assertionId")
    }


def _case_context_for_prompt(
    case_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """専用欄へ表示する大きい候補配列を案件Viewから除き、二重投入を防ぐ。"""
    if not case_context:
        return {}
    return {
        key: value
        for key, value in case_context.items()
        if key != "relationCandidates"
    }


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
                        "hypotheses": decision.get("hypotheses") or [],
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
                "hypothesisIds": item.get("hypothesisIds") or [],
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
                "relationAssertionCount": len(
                    item.get("relationAssertions") or []
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
    known_article_ids: tuple[str, ...] | list[str] = (),
    known_document_ids: tuple[str, ...] | list[str] = (),
    known_content_unit_ids: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """Ollama/Anthropic共通の構造化出力スキーマ。

    データベースIDは自由記述にせず、その時点でカタログに登録済みの
    値だけをenumとして渡す。未知の条文はIDを推測させず、search_corpusの
    検索語として要求させる。
    """
    article_id_schema = _known_string_schema(known_article_ids)
    document_id_schema = _known_string_schema(known_document_ids)
    content_unit_id_schema = _known_string_schema(known_content_unit_ids)
    hypothesis_schema = _research_hypothesis_json_schema(
        content_unit_id_schema=content_unit_id_schema,
    )
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
            "articleIds": {"type": "array", "items": article_id_schema},
            "documentIds": {"type": "array", "items": document_id_schema},
            "docTypes": {
                "type": "array",
                "items": {"type": "string", "enum": ["law", "guideline"]},
            },
            "edgeTypes": {"type": "array", "items": {"type": "string"}},
            "hypothesisIds": {
                "type": "array",
                "items": {"type": "string", "maxLength": 80},
                "minItems": 1,
                "maxItems": 8,
            },
            "reason": {"type": "string"},
        },
        "required": [
            "tool",
            "query",
            "articleIds",
            "documentIds",
            "docTypes",
            "edgeTypes",
            "hypothesisIds",
            "reason",
        ],
        "additionalProperties": False,
    }
    evidence_schema = {
        "type": "object",
        "properties": {
            "contentUnitId": content_unit_id_schema,
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
            "hypotheses": {
                "type": "array",
                "items": hypothesis_schema,
                "minItems": 1,
                "maxItems": 8,
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
            "hypotheses",
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
    known_article_ids: tuple[str, ...] | list[str] = (),
    known_content_unit_ids: tuple[str, ...] | list[str] = (),
    final_cycle: bool = False,
) -> dict[str, Any]:
    article_id_schema = _known_string_schema(
        known_article_ids,
        nullable=True,
    )
    content_unit_id_schema = _known_string_schema(known_content_unit_ids)
    hypothesis_schema = _research_hypothesis_json_schema(
        content_unit_id_schema=content_unit_id_schema,
    )
    relation_decision_schema = {
        "type": "object",
        "properties": {
            "assertionId": {"type": "string", "maxLength": 500},
            "verdict": {
                "type": "string",
                "enum": ["confirmed", "rejected", "uncertain"],
            },
            "evidenceIds": {
                "type": "array",
                "items": content_unit_id_schema,
                "maxItems": 20,
            },
            "reason": {"type": "string", "maxLength": 240},
        },
        "required": [
            "assertionId",
            "verdict",
            "evidenceIds",
            "reason",
        ],
        "additionalProperties": False,
    }
    authority_node_schema = {
        "type": "object",
        "properties": {
            "nodeId": {"type": "string", "maxLength": 80},
            "articleId": article_id_schema,
            "title": {"type": "string", "maxLength": 120},
            "legalRole": {"type": "string", "maxLength": 120},
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
                "items": content_unit_id_schema,
                "maxItems": AUTHORITY_NODE_EVIDENCE_MAX,
            },
            "parentNodeId": {"type": ["string", "null"]},
            "relationFromParent": {"type": ["string", "null"]},
            "purpose": {"type": "string", "maxLength": 160},
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
            "claimId": {"type": "string", "maxLength": 80},
            "question": {"type": "string", "maxLength": 140},
            "conclusion": {"type": "string", "maxLength": 300},
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
            "issueId": {"type": "string", "maxLength": 80},
            "question": {"type": "string", "maxLength": 160},
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
            "articleId": article_id_schema,
            "action": {
                "type": "string",
                "enum": [
                    "search",
                    "fetch_article",
                    "expand_graph",
                    "verify_text",
                ],
            },
            "reason": {"type": "string", "maxLength": 180},
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
            "hypotheses": {
                "type": "array",
                "items": hypothesis_schema,
                "minItems": 1,
                "maxItems": 8,
            },
            "unresolved": {
                "type": "array",
                "items": unresolved_schema,
                "maxItems": 6,
            },
            "relationDecisions": {
                "type": "array",
                "items": relation_decision_schema,
                "maxItems": 8,
            },
        },
        # 旧Checkpointとの再開互換のため省略時は空配列として扱う。
        "required": ["issues", "hypotheses", "unresolved"],
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
                    if final_cycle
                    else [
                        RESEARCH_STATUS_CONTINUE,
                        RESEARCH_STATUS_READY,
                        RESEARCH_STATUS_INSUFFICIENT,
                    ]
                ),
            },
            "conclusion": {"type": "string", "maxLength": 1200},
            "evidenceIds": {
                "type": "array",
                "items": content_unit_id_schema,
                "maxItems": min(max_selected_evidence, 10),
            },
            "openEvidenceIds": {
                "type": "array",
                "items": content_unit_id_schema,
                "maxItems": 20,
            },
            "nextQuestions": {
                "type": "array",
                "items": {"type": "string", "maxLength": 100},
                "maxItems": 3,
            },
            "nextArticleIds": {
                "type": "array",
                "items": _known_string_schema(known_article_ids),
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


def _research_hypothesis_json_schema(
    *,
    content_unit_id_schema: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "hypothesisId": {"type": "string", "maxLength": 80},
            "statement": {"type": "string", "maxLength": 300},
            "status": {
                "type": "string",
                "enum": [
                    "unverified",
                    "partially_supported",
                    "supported",
                    "rejected",
                ],
            },
            "evidenceIds": {
                "type": "array",
                "items": content_unit_id_schema,
                "maxItems": 12,
            },
            "missing": {
                "type": "array",
                "items": {"type": "string", "maxLength": 160},
                "maxItems": 6,
            },
        },
        "required": [
            "hypothesisId",
            "statement",
            "status",
            "evidenceIds",
            "missing",
        ],
        "additionalProperties": False,
    }


def _known_string_schema(
    values: tuple[str, ...] | list[str],
    *,
    nullable: bool = False,
) -> dict[str, Any]:
    """既知IDだけを許可するJSON Schemaを作る。

    初回探索前など候補が空の場合はenumを付けない。その場合も実行前の
    validate_research_turn/checkpointが未知IDを拒否する。
    """
    known_values = list(
        dict.fromkeys(str(value) for value in values if str(value))
    )
    schema: dict[str, Any] = {
        "type": ["string", "null"] if nullable else "string",
    }
    if known_values:
        schema["enum"] = (
            [*known_values, None] if nullable else known_values
        )
    return schema


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


def hydrate_relation_decision_candidates(
    raw_text: str,
    catalog: EvidenceCatalog,
) -> str:
    """LLMの案件内判定へ、既知候補の不変フィールドをIDから復元する。

    Anthropicのcompiled grammar上限を避けるため、LLMには意味判断に必要な
    assertionId/verdict/evidenceIds/reasonだけを出力させる。両端Articleと関係種別は
    RelationAssertionの既知値をそのまま付与し、未知IDは採用しない。
    """
    payload = json.loads(raw_text)
    logical_structure = payload.get("logicalStructure")
    if not isinstance(logical_structure, dict):
        return raw_text
    raw_decisions = logical_structure.get("relationDecisions") or []
    if not isinstance(raw_decisions, list):
        return raw_text
    hydrated: list[dict[str, Any]] = []
    for raw in raw_decisions:
        if not isinstance(raw, dict):
            continue
        assertion = catalog.relation_assertion(
            str(raw.get("assertionId") or "")
        )
        if assertion is None:
            continue
        hydrated.append(
            {
                **raw,
                "relationType": str(assertion.get("suggestedType") or ""),
                "fromArticleId": str(assertion.get("fromArticleId") or ""),
                "toArticleId": str(assertion.get("toArticleId") or ""),
            }
        )
    logical_structure["relationDecisions"] = hydrated
    return json.dumps(payload, ensure_ascii=False)


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
