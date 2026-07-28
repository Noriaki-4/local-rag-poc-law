"""ガイドレーン。ガイド・監督指針・Q&Aを法令の代替ではなく羅針盤として使う。

計画書 §10(ガイドの扱い)、§16.4(ガイドテスト)に対応する。

ガイドは法令と同じ根拠枠で競争させない。役割は次の3つに限定する。

1. 自然言語と法令用語の橋渡し（質問文でガイドを検索する）
2. 関連Article・法令間関係の候補発見（EXPLAINS / MENTIONS / RelationAssertion）
3. 行政解釈・実務運用の補足（回答コンテキストでは補助枠のみ、法的結論の直接根拠にしない）

信頼度の区別:

- `EXPLAINS`      明示的な解説対象。法令本文の取得先(索引)として使う
- `MENTIONS`      単なる言及。対象法令の検索範囲を広げるだけで、条文を確実投入しない
- `RelationAssertion` ガイドが示唆した未確認の法令間関係。候補拡張だけに使う
"""

from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from .config import settings
from .evidence_requirements import LegalIssue
from .legal_ontology import RELATION_STATUS_UNVERIFIED, is_trusted_relation
from .opensearch_client import RequirementSearchSpec
from .retrieval_budget import COMPONENT_GRAPH, COMPONENT_SEARCH, BudgetTracker

EVIDENCE_LANE_GUIDANCE = "guidance"
# 回答・UIで明示する位置づけ(§10-7)。法令本文と同じ重みで扱わせない。
GUIDANCE_EVIDENCE_ROLE = "行政解釈・実務上の取扱い(法令本文ではない)"


@dataclass(frozen=True)
class GuidanceFinding:
    """1つのガイドチャンクと、そこから辿れた条文候補。"""

    issue_id: str
    document_id: str
    content_unit_id: str
    title: str = ""
    authority: str = ""
    score: float = 0.0
    text: str = ""
    published_at: str | None = None
    source: dict[str, Any] = field(default_factory=dict)

    @property
    def item(self) -> dict[str, Any]:
        """既存の検索結果itemと同じ形。ガイドであることを明示して渡す(§10-7)。"""
        return {
            "document": self.source,
            "score": self.score,
            "introducedBy": "guidance_lane",
            "sources": ["guidance_lane"],
            "evidenceLane": EVIDENCE_LANE_GUIDANCE,
            "evidenceRole": GUIDANCE_EVIDENCE_ROLE,
        }


@dataclass(frozen=True)
class GuidanceLaneResult:
    """ガイドレーンの成果。法令レーンへ渡すのは「候補」だけで、根拠そのものではない。"""

    findings: tuple[GuidanceFinding, ...] = ()
    # EXPLAINS由来。法令本文を直接取得する索引として使ってよい。
    explained_article_ids_by_issue: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # MENTIONS・未確認assertion由来。検索範囲の拡張だけに使う。
    candidate_document_ids_by_issue: dict[str, tuple[str, ...]] = field(default_factory=dict)
    assertions: tuple[dict[str, Any], ...] = ()
    trace: dict[str, Any] = field(default_factory=dict)

    @property
    def used(self) -> bool:
        return bool(self.findings)


class GuidanceLane:
    """ガイド検索 → 条文候補の発見までを担う。法令本文の取得は法令レーンの役割。"""

    def __init__(self, os_client: Any, graph_client: Any) -> None:
        self.os_client = os_client
        self.graph_client = graph_client

    def explore(
        self,
        issues: tuple[LegalIssue, ...] | list[LegalIssue],
        *,
        tracker: BudgetTracker,
        user_clearance_level: int = 2,
    ) -> GuidanceLaneResult:
        if not issues or settings.layered_max_guidance_per_issue <= 0:
            return GuidanceLaneResult(trace={"used": False, "reason": "disabled"})

        findings, search_trace = self._search(issues, tracker, user_clearance_level)
        if not findings:
            return GuidanceLaneResult(trace={"used": False, **search_trace})

        explained, mentioned, graph_trace = self._articles_from_graph(findings, tracker)
        assertions, assertion_trace = self._relation_assertions(explained, tracker)

        candidate_documents: dict[str, list[str]] = {}
        for issue_id, article_ids in mentioned.items():
            for article_id in article_ids:
                document_id = article_id.split("-article-", 1)[0]
                bucket = candidate_documents.setdefault(issue_id, [])
                if document_id not in bucket:
                    bucket.append(document_id)
        for assertion in assertions:
            issue_id = str(assertion.get("issueId") or "")
            document_id = str(assertion.get("toArticleId") or "").split("-article-", 1)[0]
            if not issue_id or not document_id:
                continue
            bucket = candidate_documents.setdefault(issue_id, [])
            if document_id not in bucket:
                bucket.append(document_id)

        return GuidanceLaneResult(
            findings=findings,
            explained_article_ids_by_issue={
                issue_id: tuple(article_ids) for issue_id, article_ids in explained.items()
            },
            candidate_document_ids_by_issue={
                issue_id: tuple(document_ids)
                for issue_id, document_ids in candidate_documents.items()
            },
            assertions=assertions,
            trace={
                "used": True,
                **search_trace,
                **graph_trace,
                **assertion_trace,
                "explainedArticleIdsByIssue": {
                    issue_id: list(article_ids) for issue_id, article_ids in explained.items()
                },
                "mentionedArticleIdsByIssue": {
                    issue_id: list(article_ids) for issue_id, article_ids in mentioned.items()
                },
                "candidateDocumentIdsByIssue": {
                    issue_id: list(document_ids)
                    for issue_id, document_ids in candidate_documents.items()
                },
                "timeliness": _timeliness_trace(findings),
            },
        )

    # ------------------------------------------------------------------ 内部処理

    def _search(
        self,
        issues: tuple[LegalIssue, ...] | list[LegalIssue],
        tracker: BudgetTracker,
        user_clearance_level: int,
    ) -> tuple[tuple[GuidanceFinding, ...], dict[str, Any]]:
        """自然言語の質問断片でガイドを検索する。論点分をまとめて1回で投げる(§11.7)。"""
        if not tracker.can_invoke(
            COMPONENT_SEARCH, max_invocations=settings.layered_max_search_batch_calls_total
        ):
            return (), {"searchSkipped": "search_call_budget_exhausted"}
        timeout = tracker.effective_timeout(COMPONENT_SEARCH)
        if timeout <= 0:
            return (), {"searchSkipped": "time_budget_exhausted"}

        specs = [
            RequirementSearchSpec(
                requirement_id=issue.issue_id,
                # ガイドは自然言語に近い表現で書かれているため、質問断片をそのまま使う。
                query=" ".join([issue.question_span or issue.label, *issue.key_terms])[:200],
                top_k=settings.layered_max_guidance_per_issue,
                doc_type="guideline",
            )
            for issue in issues
        ]
        started = perf_counter()
        try:
            results = self.os_client.search_requirement_specs(
                specs,
                user_clearance_level=user_clearance_level,
                timeout_sec=timeout,
            )
        except Exception as exc:  # noqa: BLE001 - ガイド検索の失敗で法令レーンを止めない
            return (), {"searchError": str(exc)}
        tracker.record(
            COMPONENT_SEARCH,
            items=len(specs),
            elapsed_ms=int((perf_counter() - started) * 1000),
        )

        findings: list[GuidanceFinding] = []
        for issue in issues:
            for chunk in results.get(issue.issue_id, [])[
                : settings.layered_max_guidance_per_issue
            ]:
                source = chunk.get("source") or {}
                findings.append(
                    GuidanceFinding(
                        issue_id=issue.issue_id,
                        document_id=str(chunk.get("documentId") or ""),
                        content_unit_id=str(chunk.get("contentUnitId") or ""),
                        title=str(source.get("title") or ""),
                        authority=str(source.get("sectionPath") or "").split(" > ")[0],
                        score=float(chunk.get("score") or 0.0),
                        text=str(chunk.get("text") or ""),
                        published_at=source.get("publishedAt"),
                        source=source,
                    )
                )
        return tuple(findings), {"guidanceChunkCount": len(findings)}

    def _articles_from_graph(
        self,
        findings: tuple[GuidanceFinding, ...],
        tracker: BudgetTracker,
    ) -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, Any]]:
        """ガイド文書からEXPLAINS / MENTIONSを辿り、条文候補を得る(§10-2)。

        EXPLAINSは明示的な解説対象なので条文の直接取得に使い、MENTIONSは検索範囲の
        拡張だけに使う。MENTIONSだけで条文を確実投入しない(§16.4)。
        """
        explained: dict[str, list[str]] = {}
        mentioned: dict[str, list[str]] = {}
        document_ids = list(dict.fromkeys(finding.document_id for finding in findings if finding.document_id))
        if not document_ids or self.graph_client is None:
            return explained, mentioned, {"guidanceGraphSkipped": "no_document"}
        if not tracker.can_invoke(
            COMPONENT_GRAPH, max_invocations=settings.layered_max_graph_batch_calls_total
        ):
            return explained, mentioned, {"guidanceGraphSkipped": "graph_call_budget_exhausted"}
        timeout = tracker.effective_timeout(COMPONENT_GRAPH)
        if timeout <= 0:
            return explained, mentioned, {"guidanceGraphSkipped": "time_budget_exhausted"}

        started = perf_counter()
        try:
            paths = self.graph_client.paths_from_many(
                document_ids,
                edge_types=["EXPLAINS", "MENTIONS"],
                max_depth=1,
                limit=settings.agent_max_graph_paths * 2,
                timeout_sec=timeout,
            )
        except Exception as exc:  # noqa: BLE001 - Graph障害時は法令内検索を継続する(§12)
            return explained, mentioned, {"guidanceGraphError": str(exc)}
        tracker.record(
            COMPONENT_GRAPH,
            items=len(document_ids),
            elapsed_ms=int((perf_counter() - started) * 1000),
        )

        issues_by_document: dict[str, list[str]] = {}
        for finding in findings:
            bucket = issues_by_document.setdefault(finding.document_id, [])
            if finding.issue_id not in bucket:
                bucket.append(finding.issue_id)

        limit = settings.layered_max_guide_derived_articles
        for path in paths:
            edges = path.get("edges") or []
            nodes = path.get("nodes") or []
            if not edges or len(nodes) < 2:
                continue
            edge = edges[-1]
            edge_type = str(edge.get("edgeType") or "")
            source_document_id = str(nodes[0].get("graphNodeId") or nodes[0].get("documentId") or "")
            target_id = str(nodes[-1].get("contentUnitId") or nodes[-1].get("graphNodeId") or "")
            if not target_id:
                continue
            target = explained if edge_type == "EXPLAINS" and is_trusted_relation(edge) else mentioned
            for issue_id in issues_by_document.get(source_document_id, []):
                bucket = target.setdefault(issue_id, [])
                if target_id not in bucket and len(bucket) < limit:
                    bucket.append(target_id)
        return explained, mentioned, {"guidanceGraphPathCount": len(paths)}

    def _relation_assertions(
        self,
        explained: dict[str, list[str]],
        tracker: BudgetTracker,
    ) -> tuple[tuple[dict[str, Any], ...], dict[str, Any]]:
        """ガイドが示唆した未確認の法令間関係を候補拡張のためだけに取得する(§10-3)。"""
        article_ids = [article_id for ids in explained.values() for article_id in ids]
        if not article_ids or self.graph_client is None:
            return (), {"relationAssertionCount": 0}
        if not hasattr(self.graph_client, "relation_assertions_from"):
            return (), {"relationAssertionCount": 0}
        if not tracker.can_invoke(
            COMPONENT_GRAPH,
            max_invocations=settings.layered_max_graph_batch_calls_total,
        ):
            return (), {"relationAssertionSkipped": "graph_call_budget_exhausted"}
        timeout = tracker.effective_timeout(COMPONENT_GRAPH)
        if timeout <= 0:
            return (), {"relationAssertionSkipped": "time_budget_exhausted"}
        started = perf_counter()
        try:
            rows = self.graph_client.relation_assertions_from(
                article_ids,
                timeout_sec=timeout,
            )
        except Exception as exc:  # noqa: BLE001 - 未確認関係が取れなくても探索は続く
            return (), {"relationAssertionError": str(exc)}
        tracker.record(
            COMPONENT_GRAPH,
            items=len(article_ids),
            elapsed_ms=int((perf_counter() - started) * 1000),
        )
        issue_by_article = {
            article_id: issue_id
            for issue_id, ids in explained.items()
            for article_id in ids
        }
        assertions = tuple(
            {
                **row,
                "issueId": issue_by_article.get(str(row.get("fromArticleId") or "")),
                # 確定関係として使わないことを明示する(§6.1, §16.4)。
                "status": str(row.get("status") or RELATION_STATUS_UNVERIFIED),
                "usage": "candidate_expansion_only",
            }
            for row in rows
            if str(row.get("status") or RELATION_STATUS_UNVERIFIED) == RELATION_STATUS_UNVERIFIED
        )
        return assertions, {"relationAssertionCount": len(assertions)}


def _timeliness_trace(findings: tuple[GuidanceFinding, ...]) -> list[dict[str, Any]]:
    """ガイドの法令時点・発行主体・位置づけを表示できるようにする(§10 末尾)。

    現状のmanifestは発行日を必須にしていないため、取得できない場合は`unknown`として
    残し、「法令と時点が一致していることを確認済み」と誤解させない。
    """
    seen: dict[str, dict[str, Any]] = {}
    for finding in findings:
        if finding.document_id in seen:
            continue
        seen[finding.document_id] = {
            "documentId": finding.document_id,
            "title": finding.title,
            "authority": finding.authority or "unknown",
            "publishedAt": finding.published_at or "unknown",
            "legalAsOfKnown": bool(finding.published_at),
            "positioning": GUIDANCE_EVIDENCE_ROLE,
        }
    return list(seen.values())
