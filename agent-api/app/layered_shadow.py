"""法令レイヤー別探索(vNext)を現行経路の隣で実行するエントリポイント。

計画書 §11.3(shadow専用予算)、§17.1(shadow比較)、§19(既存方式との互換性)に対応する。

shadowは現行検索・回答を守るため独立したphase budgetを持ち、超過した場合は新方式だけを
打ち切って`shadowIncomplete`を記録する。新方式の内部障害が現行回答へ影響してはならない。
"""

from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from .config import settings
from .evidence_requirements import EvidenceRequirement
from .layered_context_assembler import (
    ANSWER_STATUS_INSUFFICIENT_PRIMARY,
    ChunkCandidate,
    ContextAssembly,
    assemble_context,
)
from .layered_retriever import LayeredRetrievalResult, LayeredRetriever
from .legal_issue_planner import (
    HARD_MAX_PRIMARY_ISSUES,
    IssuePlan,
    fallback_issue_plan,
    merge_explicit_references,
)
from .requirement_satisfaction import ROLE_SATISFACTION_CUES
from .retrieval_budget import BudgetTracker, current_profile, shadow_phase_budget_sec


@dataclass(frozen=True)
class LayeredOutcome:
    """新方式の実行結果。activeモードではこのコンテキストを回答へ渡す。"""

    plan: IssuePlan
    retrieval: LayeredRetrievalResult
    assembly: ContextAssembly
    trace: dict[str, Any] = field(default_factory=dict)
    incomplete: bool = False

    @property
    def usable_for_answer(self) -> bool:
        """主論点の根拠を確保できたときだけ、回答コンテキストとして使える(§11.6)。"""
        return (
            self.assembly.answer_status != ANSWER_STATUS_INSUFFICIENT_PRIMARY
            and bool(self.assembly.items)
        )

    @property
    def unresolved_for_answer(self) -> tuple[EvidenceRequirement, ...]:
        ids = set(self.assembly.unresolved_for_answer_requirement_ids)
        return tuple(
            requirement
            for requirement in self.assembly.requirements
            if requirement.requirement_id in ids
        )


def run_layered_retrieval(
    *,
    request: Any,
    os_client: Any,
    graph_client: Any,
    reranker_client: Any = None,
    llm_client: Any = None,
    deadline: float,
    explicit_references: list[dict[str, Any]] | None = None,
    shadow: bool = True,
    allow_planner_call: bool = False,
) -> LayeredOutcome:
    """論点分解 → Requirement別探索 → 最終コンテキスト組み立てを1回実行する。

    例外はここで吸収せず、呼び出し側(agent.py)がshadowとして握りつぶす。予算超過は
    例外ではなく`incomplete`として返し、現行回答を継続できるようにする。
    """
    started = perf_counter()
    profile = current_profile()
    if shadow:
        budget_sec = shadow_phase_budget_sec(deadline=deadline, now=started, profile=profile)
    else:
        budget_sec = max(0.0, deadline - started - profile.full_answer_safe_reserve_sec)
    tracker = BudgetTracker(profile=profile, started=started, exploration_budget_sec=budget_sec)

    plan, planner_trace = _build_plan(
        request,
        llm_client=llm_client,
        allow_planner_call=allow_planner_call,
        explicit_references=explicit_references or [],
        tracker=tracker,
    )

    retriever = LayeredRetriever(os_client, graph_client, reranker_client)
    retrieval = retriever.retrieve(
        plan,
        tracker=tracker,
        user_clearance_level=getattr(request, "userClearanceLevel", 2),
    )
    candidates = (*chunk_candidates(retrieval), *guidance_chunk_candidates(retrieval))
    assembly = assemble_context(
        candidates,
        retrieval.requirements,
        retrieval.groups,
        max_chunks=settings.layered_final_context_chunks,
        max_chunks_per_article=settings.layered_max_chunks_per_article,
        max_auxiliary_chunks=settings.layered_max_auxiliary_context_chunks,
    )
    elapsed_ms = int((perf_counter() - started) * 1000)
    incomplete = retrieval.incomplete or tracker.remaining_sec() <= 0
    issue_labels = {issue.issue_id: issue.label for issue in plan.issues}
    groups_by_id = {group.group_id: group for group in retrieval.groups}
    omitted_primary_issue_labels = list(
        dict.fromkeys(
            issue_labels.get(groups_by_id[group_id].issue_id, group_id)
            for group_id in assembly.omitted_primary_group_ids
            if group_id in groups_by_id
        )
    )
    included_primary_issue_labels = list(
        dict.fromkeys(
            issue_labels.get(groups_by_id[group_id].issue_id, group_id)
            for group_id in assembly.included_primary_group_ids
            if group_id in groups_by_id
        )
    )

    trace: dict[str, Any] = {
        "enabled": True,
        "mode": "shadow" if shadow else "active",
        "legalIssuePlan": plan.as_trace(),
        "issuePlanner": planner_trace,
        "outOfScopeIssueLabels": list(plan.out_of_scope_labels),
        "contextCoverage": assembly.as_trace(),
        "answerControl": {
            "answerStatus": assembly.answer_status,
            "includedPrimaryIssueLabels": included_primary_issue_labels,
            "omittedPrimaryIssueLabels": omitted_primary_issue_labels,
            "outOfScopeIssueLabels": list(plan.out_of_scope_labels),
            "unresolvedForAnswerRequirementIds": list(
                assembly.unresolved_for_answer_requirement_ids
            ),
        },
        "timeBudget": tracker.as_trace(),
        "shadowPhaseBudgetMs": int(budget_sec * 1000),
        "shadowPhaseElapsedMs": elapsed_ms,
        "shadowIncomplete": incomplete if shadow else False,
        "internalFailure": bool(retrieval.trace.get("fallbacks")),
        "guidanceChunkCandidateCount": len(guidance_chunk_candidates(retrieval)),
        "unprocessedRequirementCount": sum(
            1 for requirement in retrieval.requirements if requirement.unresolved_for_answer
        ),
        **retrieval.trace,
    }
    return LayeredOutcome(
        plan=plan,
        retrieval=retrieval,
        assembly=assembly,
        trace=trace,
        incomplete=incomplete,
    )


def _build_plan(
    request: Any,
    *,
    llm_client: Any,
    allow_planner_call: bool,
    explicit_references: list[dict[str, Any]],
    tracker: BudgetTracker,
) -> tuple[IssuePlan, dict[str, Any]]:
    question = str(getattr(request, "question", "") or "")
    trace: dict[str, Any] = {"used": False}
    plan: IssuePlan | None = None
    if llm_client is not None and allow_planner_call and tracker.can_continue(1.0):
        timeout = int(tracker.effective_timeout("planner"))
        if timeout > 0:
            result = llm_client.plan_legal_issues(
                request,
                max_issues=min(settings.layered_max_primary_issues, HARD_MAX_PRIMARY_ISSUES),
                timeout_sec=timeout,
            )
            tracker.record("planner", items=1, elapsed_ms=result.latencyMs)
            plan = result.plan
            trace = {
                "used": True,
                "attemptCount": 1 + result.retryCount,
                "provider": result.provider,
                "model": result.model,
                "latencyMs": result.latencyMs,
                "validationError": result.validationError,
                "retryCount": result.retryCount,
                "fallbackUsed": result.plan.fallback_used,
            }
    if plan is None:
        plan = fallback_issue_plan(question, reason="planner_not_called")
        trace = {
            "used": False,
            "attemptCount": 0,
            "fallbackUsed": True,
            "reason": "planner_not_called",
        }
    # 条番号はplannerではなく決定的パーサーの結果を正とする(§7.2)。
    return merge_explicit_references(plan, explicit_references), trace


def chunk_candidates(retrieval: LayeredRetrievalResult) -> tuple[ChunkCandidate, ...]:
    """採用ArticleからParagraph・Item単位のchunk候補を作る(§11.8)。

    検索・Graph・役割判定はArticle単位で行い、chunk化は回答コンテキスト選択の直前に行う。
    """
    requirements_by_article: dict[str, list[str]] = {}
    explicit_articles: set[str] = set()
    for requirement in retrieval.requirements:
        for article_id in requirement.accepted_article_ids:
            requirements_by_article.setdefault(article_id, []).append(requirement.requirement_id)
            if requirement.user_explicit:
                explicit_articles.add(article_id)

    candidates: list[ChunkCandidate] = []
    seen: set[str] = set()
    rank = 0
    requirements_by_id = {
        requirement.requirement_id: requirement
        for requirement in retrieval.requirements
    }
    for requirement in retrieval.requirements:
        for article in retrieval.article_candidates.get(requirement.requirement_id, []):
            article_id = str(article.get("articleId") or "")
            if article_id not in requirements_by_article:
                continue
            article_requirement_ids = tuple(
                dict.fromkeys(requirements_by_article.get(article_id, []))
            )
            chunks = sorted(
                list(article.get("chunks") or []),
                key=lambda chunk: _chunk_relevance_key(
                    chunk,
                    [
                        requirements_by_id[requirement_id]
                        for requirement_id in article_requirement_ids
                        if requirement_id in requirements_by_id
                    ],
                ),
            )[: settings.layered_max_chunks_per_article]
            for chunk in chunks:
                content_unit_id = str(chunk.get("contentUnitId") or article_id)
                if content_unit_id in seen:
                    continue
                seen.add(content_unit_id)
                text = str(chunk.get("text") or "")
                candidates.append(
                    ChunkCandidate(
                        content_unit_id=content_unit_id,
                        article_id=article_id,
                        requirement_ids=article_requirement_ids,
                        is_law=str(chunk.get("docType") or "law") == "law",
                        is_guidance=str(chunk.get("docType") or "") == "guideline",
                        user_explicit=article_id in explicit_articles,
                        rank=rank,
                        chars=len(text),
                        item={
                            "document": chunk,
                            "score": float(article.get("score") or 0.0),
                            "introducedBy": "layered_retrieval",
                            "sources": ["layered_retrieval"],
                        },
                    )
                )
                rank += 1
    return tuple(candidates)


def _chunk_relevance_key(
    chunk: dict[str, Any],
    requirements: list[EvidenceRequirement],
) -> tuple[int, int, str]:
    """Article内でRequirementに必要な項・号を優先する。"""
    text = " ".join(
        [
            str(chunk.get("heading") or ""),
            str(chunk.get("text") or ""),
        ]
    )
    score = 0
    explicit = False
    for requirement in requirements:
        if requirement.article_id and str(chunk.get("contentUnitId") or "") == requirement.article_id:
            explicit = True
        score += 10 * sum(1 for term in requirement.key_terms if term and term in text)
        score += 3 * sum(
            1
            for cue in ROLE_SATISFACTION_CUES.get(requirement.role_family, ())
            if cue in text
        )
    return (
        0 if explicit else 1,
        -score,
        str(chunk.get("contentUnitId") or ""),
    )


def guidance_chunk_candidates(retrieval: LayeredRetrievalResult) -> tuple[ChunkCandidate, ...]:
    """ガイドチャンクを補助枠の候補として渡す(§10, §11.6-9/10)。

    ガイドは法令mandatory枠を満たさない。`is_guidance=True` なので、主論点groupを
    完全被覆できた場合にだけ、最大`MAX_AUXILIARY_CONTEXT_CHUNKS`件まで採用される。
    """
    candidates: list[ChunkCandidate] = []
    seen: set[str] = set()
    for rank, finding in enumerate(retrieval.guidance.findings):
        if not finding.content_unit_id or finding.content_unit_id in seen:
            continue
        seen.add(finding.content_unit_id)
        candidates.append(
            ChunkCandidate(
                content_unit_id=finding.content_unit_id,
                article_id=finding.document_id,
                requirement_ids=(),
                is_law=False,
                is_guidance=True,
                rank=rank,
                chars=len(finding.text),
                item=finding.item,
            )
        )
    return tuple(candidates)
