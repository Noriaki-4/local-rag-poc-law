"""LLM主導調査の確認済み事実、Task、Checkpointを分離して保持する。

初期実装は1リクエスト内のプロセスメモリを保存先とする。永続化技術ではなく、
更新責務とトランザクション境界を先に固定するためのドメインモデルである。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


TASK_CANDIDATE = "candidate"
TASK_PENDING = "pending"
TASK_RUNNING = "running"
TASK_COMPLETED = "completed"
TASK_FAILED = "failed"
TASK_BLOCKED = "blocked"


@dataclass
class ResearchTask:
    task_ref: str
    task_type: str
    status: str
    origin: str
    purpose: str = ""
    query: str | None = None
    article_ids: tuple[str, ...] = ()
    document_ids: tuple[str, ...] = ()
    edge_types: tuple[str, ...] = ()
    hypothesis_ids: tuple[str, ...] = ()
    source_article_id: str | None = None
    target_article_id: str | None = None
    target_document_id: str | None = None
    relation_type: str | None = None
    heading: str = ""
    title: str = ""
    priority: int = 0
    attempt_count: int = 0
    created_version: int = 0
    updated_version: int = 0
    result_event_refs: list[str] = field(default_factory=list)
    error: str | None = None

    def prompt_item(self) -> dict[str, Any]:
        item = {
            "taskRef": self.task_ref,
            "type": self.task_type,
            "status": self.status,
            "origin": self.origin,
            "purpose": self.purpose,
            "query": self.query,
            "articleIds": list(self.article_ids),
            "documentIds": list(self.document_ids),
            "sourceArticleId": self.source_article_id,
            "targetArticleId": self.target_article_id,
            "targetDocumentId": self.target_document_id,
            "relationType": self.relation_type,
            "hypothesisIds": list(self.hypothesis_ids),
            "title": self.title,
            "heading": self.heading,
            "priority": self.priority,
        }
        # PromptViewでは空値を繰り返さない。正本のTaskには全項目を保持する。
        return {
            key: value
            for key, value in item.items()
            if value not in (None, "", [], ())
        }


@dataclass(frozen=True)
class CaseEvent:
    event_ref: str
    version: int
    event_type: str
    occurred_at: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class CheckpointRecord:
    checkpoint_ref: str
    case_id: str
    store_version: int
    status: str
    conclusion_summary: str
    selected_evidence_refs: tuple[str, ...]
    pending_task_refs: tuple[str, ...]


class ResearchCase:
    """1利用者質問を起点とする調査案件の正本。"""

    def __init__(self, *, case_id: str, question: str) -> None:
        self.case_id = case_id
        self.question = question
        self.current_version = 0
        self.articles: dict[str, dict[str, Any]] = {}
        self.evidence: dict[str, dict[str, Any]] = {}
        self.relations: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.relation_assertions: dict[str, dict[str, Any]] = {}
        self.relation_decisions: dict[str, dict[str, Any]] = {}
        self.claims: dict[str, dict[str, Any]] = {}
        self.hypotheses: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, ResearchTask] = {}
        self.events: list[CaseEvent] = []
        self.checkpoints: list[CheckpointRecord] = []
        self._task_counter = 0
        self._checkpoint_counter = 0
        self._action_task_keys: dict[tuple[Any, ...], str] = {}
        self._graph_task_by_article: dict[str, str] = {}
        self._candidate_view_offsets: dict[str, int] = {}
        self._commit("case_created", {"question": question})

    @property
    def latest_checkpoint(self) -> CheckpointRecord | None:
        return self.checkpoints[-1] if self.checkpoints else None

    def register_action(self, action: Any, *, phase: str) -> ResearchTask:
        """LLM ActionをTask化する。同じ候補Taskがあれば昇格して再利用する。"""
        article_ids = tuple(dict.fromkeys(action.articleIds))
        hypothesis_ids = tuple(
            dict.fromkeys(getattr(action, "hypothesisIds", ()))
        )
        if action.tool == "fetch_articles" and len(article_ids) == 1:
            existing_ref = self._graph_task_by_article.get(article_ids[0])
            if existing_ref:
                task = self.tasks[existing_ref]
                if task.status in {TASK_CANDIDATE, TASK_FAILED}:
                    task.status = TASK_PENDING
                    task.purpose = action.reason or task.purpose
                    task.error = None
                    task.hypothesis_ids = tuple(
                        dict.fromkeys(
                            [*task.hypothesis_ids, *hypothesis_ids]
                        )
                    )
                    event = self._commit(
                        "task_promoted",
                        {
                            "taskRef": task.task_ref,
                            "phase": phase,
                            "targetArticleId": article_ids[0],
                        },
                    )
                    task.updated_version = event.version
                    return task
                if task.status in {TASK_PENDING, TASK_RUNNING}:
                    task.hypothesis_ids = tuple(
                        dict.fromkeys(
                            [*task.hypothesis_ids, *hypothesis_ids]
                        )
                    )
                    return task
                # completed は過去の不変な試行記録として残し、LLMが本文を
                # 読み直す場合は後続の新しいTaskを作る。

        key = (
            action.tool,
            str(action.query or ""),
            article_ids,
            tuple(dict.fromkeys(action.documentIds)),
            tuple(dict.fromkeys(action.edgeTypes)),
        )
        existing_ref = self._action_task_keys.get(key)
        if existing_ref:
            task = self.tasks[existing_ref]
            task.hypothesis_ids = tuple(
                dict.fromkeys([*task.hypothesis_ids, *hypothesis_ids])
            )
            if task.status == TASK_FAILED:
                task.status = TASK_PENDING
                task.error = None
                return task
            if task.status in {TASK_PENDING, TASK_RUNNING}:
                return task
            # 同じ検索・取得をLLMが後続サイクルで再要求した場合も、過去の
            # completed Taskを再度runningへ戻さず、新しい試行として記録する。
            # これによりTask履歴と「どこまで完了したか」を壊さない。

        task = self._new_task(
            task_type=action.tool,
            status=TASK_PENDING,
            origin=f"llm_{phase}",
            purpose=str(action.reason or ""),
            query=action.query,
            article_ids=article_ids,
            document_ids=tuple(dict.fromkeys(action.documentIds)),
            edge_types=tuple(dict.fromkeys(action.edgeTypes)),
            hypothesis_ids=hypothesis_ids,
            priority=100,
        )
        self._action_task_keys[key] = task.task_ref
        return task

    def start_task(self, task_ref: str) -> None:
        """直列実行の不変条件を確認してTaskを開始する。"""
        running = [
            task.task_ref
            for task in self.tasks.values()
            if task.status == TASK_RUNNING and task.task_ref != task_ref
        ]
        if running:
            raise RuntimeError(
                "only one research task may run at a time: "
                + ", ".join(running)
            )
        task = self.tasks[task_ref]
        if task.status not in {TASK_PENDING, TASK_FAILED}:
            raise ValueError(
                f"task_not_runnable:{task_ref}:{task.status}"
            )
        task.status = TASK_RUNNING
        task.attempt_count += 1
        event = self._commit(
            "task_started",
            {"taskRef": task_ref, "attempt": task.attempt_count},
        )
        task.updated_version = event.version

    def complete_tool_task(
        self,
        *,
        task_ref: str,
        action: Any,
        execution: Any,
        catalog: Any,
    ) -> None:
        """ツール結果とTask終了を1つの案件versionで確定する。"""
        task = self.tasks[task_ref]
        if task.status != TASK_RUNNING:
            raise ValueError(f"task_not_running:{task_ref}:{task.status}")

        returned_ids = tuple(
            dict.fromkeys(execution.returned_content_unit_ids)
        )
        new_ids = tuple(dict.fromkeys(execution.new_content_unit_ids))
        evidence_ids = tuple(dict.fromkeys([*returned_ids, *new_ids]))
        evidence_items = {
            str(item.get("contentUnitId") or ""): item
            for item in catalog.items_by_ids(list(evidence_ids))
            if item.get("contentUnitId")
        }
        relation_items = [
            dict(item)
            for item in execution.graph_relations
            if isinstance(item, dict)
        ]
        assertion_items = [
            dict(item)
            for item in getattr(execution, "relation_assertions", ())
            if isinstance(item, dict)
        ]
        discovered_article_ids = tuple(
            dict.fromkeys(
                [
                    *execution.new_article_ids,
                    *execution.auto_graph_article_ids,
                    *(
                        str(relation.get("toArticleId") or "")
                        for relation in relation_items
                    ),
                    *(
                        str(assertion.get(endpoint) or "")
                        for assertion in assertion_items
                        for endpoint in ("fromArticleId", "toArticleId")
                    ),
                ]
            )
        )

        next_version = self.current_version + 1
        for article_id in discovered_article_ids:
            if not article_id:
                continue
            current = self.articles.setdefault(
                article_id,
                {
                    "articleId": article_id,
                    "status": "discovered",
                    "firstSeenVersion": next_version,
                    "evidenceRefs": [],
                },
            )
            current["lastSeenVersion"] = next_version

        for content_unit_id, item in evidence_items.items():
            article_id = str(item.get("articleId") or "")
            self.evidence.setdefault(
                content_unit_id,
                {
                    "contentUnitId": content_unit_id,
                    "articleId": article_id or None,
                    "documentId": item.get("documentId"),
                    "title": item.get("title"),
                    "heading": item.get("heading"),
                    "reviewStatus": "unreviewed",
                    "acquiredVersion": next_version,
                },
            )
            if article_id:
                article = self.articles.setdefault(
                    article_id,
                    {
                        "articleId": article_id,
                        "status": "discovered",
                        "firstSeenVersion": next_version,
                        "evidenceRefs": [],
                    },
                )
                refs = article.setdefault("evidenceRefs", [])
                if content_unit_id not in refs:
                    refs.append(content_unit_id)
                if action.tool == "fetch_articles":
                    article["status"] = "fetched"
                elif article.get("status") == "discovered":
                    article["status"] = "representative_fetched"
                article["lastSeenVersion"] = next_version

        for relation in relation_items:
            key = (
                str(relation.get("fromArticleId") or ""),
                str(relation.get("edgeType") or ""),
                str(relation.get("toArticleId") or ""),
            )
            if all(key):
                self.relations[key] = {
                    **relation,
                    "status": (
                        "preclassified_navigation"
                        if relation.get("relationSource")
                        == "offline_llm_classification"
                        else "verified"
                    ),
                    "recordedVersion": next_version,
                }

        for assertion in assertion_items:
            assertion_id = str(assertion.get("assertionId") or "")
            if not assertion_id:
                continue
            self.relation_assertions[assertion_id] = {
                **assertion,
                "status": str(assertion.get("status") or "unverified"),
                "recordedVersion": next_version,
            }

        task.status = TASK_FAILED if execution.error else TASK_COMPLETED
        task.error = execution.error
        task.updated_version = next_version

        for hypothesis_id in task.hypothesis_ids:
            hypothesis = self.hypotheses.get(hypothesis_id)
            if hypothesis is None:
                continue
            hypothesis["updatedVersion"] = next_version
            hypothesis.setdefault("testTaskRefs", [])
            if task_ref not in hypothesis["testTaskRefs"]:
                hypothesis["testTaskRefs"].append(task_ref)
            hypothesis.setdefault("observedEvidenceRefs", [])
            for evidence_id in evidence_ids:
                if evidence_id not in hypothesis["observedEvidenceRefs"]:
                    hypothesis["observedEvidenceRefs"].append(evidence_id)

        # batch fetchで本文を取得したとき、同じArticleを指す既存の個別
        # candidate/pending Taskも同一versionで完了へそろえる。これを行わない
        # と、取得済み本文が次サイクルでも未処理Taskとして表示され続ける。
        reconciled_task_refs: list[str] = []
        for other in self.tasks.values():
            if other.task_ref == task_ref or other.task_type != "fetch_articles":
                continue
            if other.status not in {
                TASK_CANDIDATE,
                TASK_PENDING,
                TASK_FAILED,
                TASK_BLOCKED,
            }:
                continue
            if not other.article_ids or not all(
                self._article_is_fetched(article_id)
                for article_id in other.article_ids
            ):
                continue
            other.status = TASK_COMPLETED
            other.error = None
            other.updated_version = next_version
            reconciled_task_refs.append(other.task_ref)

        # Graphで確認したArticleは事実として残し、本文未取得なら候補Taskを作る。
        # 候補は自動実行せず、次のLLM判断が必要性を評価する。
        for relation in relation_items:
            target = str(relation.get("toArticleId") or "")
            if not target or self._article_is_fetched(target):
                continue
            self._ensure_graph_candidate_task(
                article_id=target,
                source_article_id=str(
                    relation.get("fromArticleId") or ""
                ),
                relation_type=str(relation.get("edgeType") or ""),
                title=str(relation.get("toTitle") or ""),
                heading=str(relation.get("toHeading") or ""),
                target_document_id=str(
                    relation.get("toDocumentId") or ""
                ),
                hypothesis_ids=task.hypothesis_ids,
                version=next_version,
            )

        # RelationAssertionは確定関係へ昇格せず、LLMが必要性を選べる本文取得候補にする。
        ordered_action_article_ids = tuple(
            dict.fromkeys(
                str(article_id)
                for article_id in action.articleIds
                if article_id
            )
        )
        action_article_ids = set(ordered_action_article_ids)
        source_article_id = next(iter(ordered_action_article_ids), "")
        for assertion in assertion_items:
            for target in (
                str(assertion.get("fromArticleId") or ""),
                str(assertion.get("toArticleId") or ""),
            ):
                if (
                    not target
                    or target in action_article_ids
                    or self._article_is_fetched(target)
                ):
                    continue
                self._ensure_graph_candidate_task(
                    article_id=target,
                    source_article_id=source_article_id,
                    relation_type=str(
                        assertion.get("suggestedType") or ""
                    ),
                    title="",
                    heading="",
                    target_document_id="",
                    hypothesis_ids=task.hypothesis_ids,
                    version=next_version,
                )

        # 一般検索の代表本文も、本文全体を後で直接取得できるArticle候補として
        # 保持する。検索候補をCatalogだけに置くと、Prompt表示上限から漏れた時点で
        # 次サイクルから消え、同じ検索を繰り返すことになる。
        if action.tool == "search_corpus":
            for article_id in execution.new_article_ids:
                if not article_id or self._article_is_fetched(article_id):
                    continue
                content_ids = catalog.content_ids_for_article_ids([article_id])
                items = catalog.items_by_ids(content_ids[:1])
                representative = items[0] if items else {}
                self._ensure_search_candidate_task(
                    article_id=article_id,
                    title=str(representative.get("title") or ""),
                    heading=str(representative.get("heading") or ""),
                    target_document_id=str(
                        representative.get("documentId") or ""
                    ),
                    hypothesis_ids=task.hypothesis_ids,
                    version=next_version,
                )

        event = self._commit_at_version(
            next_version,
            "tool_result_committed",
            {
                "taskRef": task_ref,
                "tool": action.tool,
                "status": task.status,
                "resultCount": execution.result_count,
                "newEvidenceCount": execution.new_evidence_count,
                "newArticleCount": execution.new_article_count,
                "evidenceRefs": list(evidence_ids),
                "discoveredArticleIds": list(discovered_article_ids),
                "graphRelationCount": len(relation_items),
                "relationAssertionCount": len(assertion_items),
                "reconciledTaskRefs": reconciled_task_refs,
                "error": execution.error,
            },
        )
        task.result_event_refs.append(event.event_ref)

    def record_stage_decision(self, turn: Any, *, phase: str) -> None:
        selected = [
            item.contentUnitId for item in turn.selectedEvidence
        ]
        event = self._commit(
            "llm_stage_decision_accepted",
            {
                "phase": phase,
                "status": turn.status,
                "selectedEvidenceRefs": selected,
                "missingEvidence": list(turn.missingEvidence),
                "hypothesisIds": [
                    item.hypothesisId for item in turn.hypotheses
                ],
            },
        )
        for hypothesis in turn.hypotheses:
            current = self.hypotheses.get(hypothesis.hypothesisId, {})
            self.hypotheses[hypothesis.hypothesisId] = {
                **current,
                **hypothesis.model_dump(),
                "updatedVersion": event.version,
                "testTaskRefs": list(current.get("testTaskRefs") or []),
                "observedEvidenceRefs": list(
                    current.get("observedEvidenceRefs") or []
                ),
            }
        for content_unit_id in selected:
            if content_unit_id in self.evidence:
                self.evidence[content_unit_id]["reviewStatus"] = "provisional"
                self.evidence[content_unit_id]["reviewedVersion"] = event.version

    def create_checkpoint(self, checkpoint: Any) -> CheckpointRecord:
        """検証済みLLM判断を案件へ反映し、不変Checkpointを追加する。"""
        running = [
            task.task_ref
            for task in self.tasks.values()
            if task.status == TASK_RUNNING
        ]
        if running:
            raise RuntimeError(
                "checkpoint_requires_quiescent_tasks:" + ",".join(running)
            )

        next_version = self.current_version + 1
        selected = tuple(dict.fromkeys(checkpoint.evidenceIds))
        opened = tuple(dict.fromkeys(checkpoint.openEvidenceIds))
        for content_unit_id in selected:
            if content_unit_id in self.evidence:
                self.evidence[content_unit_id]["reviewStatus"] = "selected"
                self.evidence[content_unit_id]["reviewedVersion"] = next_version
        for content_unit_id in opened:
            if content_unit_id in self.evidence:
                self.evidence[content_unit_id]["reviewStatus"] = "open"
                self.evidence[content_unit_id]["reviewedVersion"] = next_version

        for issue in checkpoint.logicalStructure.issues:
            for claim in issue.claims:
                self.claims[claim.claimId] = {
                    "claimId": claim.claimId,
                    "issueId": issue.issueId,
                    "question": claim.question,
                    "conclusion": claim.conclusion,
                    "status": claim.status,
                    "authorityNodeIds": list(claim.authorityNodeIds),
                    "updatedVersion": next_version,
                }

        for hypothesis in checkpoint.logicalStructure.hypotheses:
            current = self.hypotheses.get(hypothesis.hypothesisId, {})
            self.hypotheses[hypothesis.hypothesisId] = {
                **current,
                **hypothesis.model_dump(),
                "updatedVersion": next_version,
                "testTaskRefs": list(current.get("testTaskRefs") or []),
                "observedEvidenceRefs": list(
                    current.get("observedEvidenceRefs") or []
                ),
            }

        for decision in checkpoint.logicalStructure.relationDecisions:
            self.relation_decisions[decision.assertionId] = {
                **decision.model_dump(),
                "status": "case_decision",
                "recordedVersion": next_version,
            }

        for article_id in dict.fromkeys(checkpoint.nextArticleIds):
            self._promote_or_create_fetch_task(
                article_id,
                purpose="Checkpointで次回本文確認が必要と判断",
                version=next_version,
            )

        self._checkpoint_counter += 1
        checkpoint_ref = f"CP-{self._checkpoint_counter:03d}"
        pending_refs = tuple(
            task.task_ref
            for task in self.tasks.values()
            if task.status in {TASK_PENDING, TASK_BLOCKED}
        )
        record = CheckpointRecord(
            checkpoint_ref=checkpoint_ref,
            case_id=self.case_id,
            store_version=next_version,
            status=checkpoint.status,
            conclusion_summary=checkpoint.conclusion,
            selected_evidence_refs=selected,
            pending_task_refs=pending_refs,
        )
        self.checkpoints.append(record)
        self._commit_at_version(
            next_version,
            "checkpoint_created",
            {
                "checkpointRef": checkpoint_ref,
                "status": checkpoint.status,
                "selectedEvidenceRefs": list(selected),
                "pendingTaskRefs": list(pending_refs),
            },
        )
        return record

    def llm_input_context(
        self,
        *,
        max_candidate_tasks: int = 32,
        max_recent_events: int = 12,
        advance_candidate_cursor: bool = True,
    ) -> dict[str, Any]:
        """最新Checkpointとそれ以降の確定差分から小さい入力Viewを作る。"""
        checkpoint = self.latest_checkpoint
        checkpoint_version = checkpoint.store_version if checkpoint else 0
        active_tasks = self._active_tasks()
        candidates = self._select_tasks_for_llm(
            active_tasks,
            limit=max_candidate_tasks,
            advance_cursor=advance_candidate_cursor,
        )
        recent_events = [
            {
                "eventRef": event.event_ref,
                "version": event.version,
                "type": event.event_type,
                "summary": _event_summary(event),
            }
            for event in self.events
            if event.version > checkpoint_version
        ][-max_recent_events:]
        allowed_article_ids = tuple(
            dict.fromkeys(
                article_id
                for task in candidates
                for article_id in (
                    *task.article_ids,
                    task.target_article_id,
                    task.source_article_id,
                )
                if article_id
            )
        )
        return {
            "caseId": self.case_id,
            "currentVersion": self.current_version,
            "latestCheckpoint": (
                {
                    "checkpointRef": checkpoint.checkpoint_ref,
                    "storeVersion": checkpoint.store_version,
                    "status": checkpoint.status,
                    "conclusionSummary": checkpoint.conclusion_summary,
                    "selectedEvidenceRefs": list(
                        checkpoint.selected_evidence_refs
                    ),
                    "pendingTaskRefs": list(checkpoint.pending_task_refs),
                }
                if checkpoint
                else None
            ),
            "eventsAfterCheckpoint": recent_events,
            "candidateTasks": [task.prompt_item() for task in candidates],
            "candidateTaskCount": len(active_tasks),
            "omittedCandidateTaskCount": max(
                0, len(active_tasks) - len(candidates)
            ),
            "allowedArticleIds": list(allowed_article_ids),
            "hypotheses": self._hypotheses_for_llm(max_items=8),
            "relationCandidates": [
                dict(item)
                for assertion_id, item in sorted(
                    self.relation_assertions.items()
                )
                if assertion_id not in self.relation_decisions
            ][:16],
            "relationDecisions": [
                dict(item)
                for _, item in sorted(self.relation_decisions.items())
            ][:8],
        }

    def runnable_tasks(self, *, limit: int | None = None) -> tuple[ResearchTask, ...]:
        """Checkpointで確定した、次に直列実行すべきTaskを返す。"""
        tasks = sorted(
            (
                task
                for task in self.tasks.values()
                if task.status == TASK_PENDING
                and not (
                    task.task_type == "fetch_articles"
                    and task.article_ids
                    and all(
                        self._article_is_fetched(article_id)
                        for article_id in task.article_ids
                    )
                )
            ),
            key=lambda task: (
                -task.priority,
                task.created_version,
                task.task_ref,
            ),
        )
        if limit is not None:
            tasks = tasks[: max(0, limit)]
        return tuple(tasks)

    def ready_blocking_article_ids(
        self,
        checkpoint: Any,
        *,
        max_articles: int = 4,
    ) -> tuple[str, ...]:
        """readyを延期して本文確認すべき、案件内の重要Articleを返す。

        Checkpointまたは既存Taskで本文確認を明示したArticleだけを対象にする。
        Graph候補はLLMへ提示するが、プログラム側で必要根拠へ格上げしない。
        """
        if str(getattr(checkpoint, "status", "")) != "ready":
            return ()
        # LLM自身が本文確認を明示したArticleは、まだCaseStoreへTask登録する
        # 前でもreadyのまま通さない。
        ordered: list[str] = list(
            getattr(checkpoint, "nextArticleIds", ())
        )
        logical_structure = getattr(checkpoint, "logicalStructure", None)
        if logical_structure is not None:
            ordered.extend(
                item.articleId
                for item in logical_structure.unresolved
                if item.articleId
                and item.action in {"fetch_article", "verify_text"}
            )
        for task in self.runnable_tasks():
            if task.task_type != "fetch_articles":
                continue
            ordered.extend(task.article_ids)

        return tuple(
            dict.fromkeys(
                article_id
                for article_id in ordered
                if article_id and not self._article_is_fetched(article_id)
            )
        )[: max(0, max_articles)]

    def trace(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for task in self.tasks.values():
            counts[task.status] = counts.get(task.status, 0) + 1
        return {
            "caseId": self.case_id,
            "currentVersion": self.current_version,
            "latestCheckpointRef": (
                self.latest_checkpoint.checkpoint_ref
                if self.latest_checkpoint
                else None
            ),
            "checkpointCount": len(self.checkpoints),
            "taskStatusCounts": counts,
            "articleCount": len(self.articles),
            "evidenceCount": len(self.evidence),
            "relationCount": len(self.relations),
            "relationAssertionCount": len(self.relation_assertions),
            "relationDecisionCount": len(self.relation_decisions),
            "claimCount": len(self.claims),
            "hypothesisCount": len(self.hypotheses),
            "hypotheses": self._hypotheses_for_llm(max_items=8),
            "checkpoints": [asdict(item) for item in self.checkpoints],
            "pendingTasks": [
                task.prompt_item()
                for task in self.tasks.values()
                if task.status in {
                    TASK_CANDIDATE,
                    TASK_PENDING,
                    TASK_FAILED,
                    TASK_BLOCKED,
                }
            ],
            "recentEvents": [
                {
                    "eventRef": event.event_ref,
                    "version": event.version,
                    "type": event.event_type,
                    "summary": _event_summary(event),
                }
                for event in self.events[-20:]
            ],
        }

    def _new_task(
        self,
        *,
        task_type: str,
        status: str,
        origin: str,
        purpose: str,
        query: str | None = None,
        article_ids: tuple[str, ...] = (),
        document_ids: tuple[str, ...] = (),
        edge_types: tuple[str, ...] = (),
        hypothesis_ids: tuple[str, ...] = (),
        source_article_id: str | None = None,
        target_article_id: str | None = None,
        target_document_id: str | None = None,
        relation_type: str | None = None,
        title: str = "",
        heading: str = "",
        priority: int = 0,
        version: int | None = None,
    ) -> ResearchTask:
        self._task_counter += 1
        task_ref = f"T-{self._task_counter:03d}"
        created_version = version or (self.current_version + 1)
        task = ResearchTask(
            task_ref=task_ref,
            task_type=task_type,
            status=status,
            origin=origin,
            purpose=purpose,
            query=query,
            article_ids=article_ids,
            document_ids=document_ids,
            edge_types=edge_types,
            hypothesis_ids=hypothesis_ids,
            source_article_id=source_article_id,
            target_article_id=target_article_id,
            target_document_id=target_document_id,
            relation_type=relation_type,
            title=title,
            heading=heading,
            priority=priority,
            created_version=created_version,
            updated_version=created_version,
        )
        self.tasks[task_ref] = task
        if version is None:
            event = self._commit(
                "task_created",
                {"taskRef": task_ref, "type": task_type, "origin": origin},
            )
            task.created_version = event.version
            task.updated_version = event.version
        return task

    def _hypotheses_for_llm(self, *, max_items: int) -> list[dict[str, Any]]:
        status_order = {
            "unverified": 0,
            "partially_supported": 1,
            "supported": 2,
            "rejected": 3,
        }
        ordered = sorted(
            self.hypotheses.values(),
            key=lambda item: (
                status_order.get(str(item.get("status") or ""), 9),
                int(item.get("updatedVersion") or 0),
                str(item.get("hypothesisId") or ""),
            ),
        )
        return [dict(item) for item in ordered[: max(0, max_items)]]

    def _ensure_graph_candidate_task(
        self,
        *,
        article_id: str,
        source_article_id: str,
        relation_type: str,
        title: str,
        heading: str,
        target_document_id: str,
        hypothesis_ids: tuple[str, ...],
        version: int,
    ) -> ResearchTask:
        existing_ref = self._graph_task_by_article.get(article_id)
        if existing_ref:
            task = self.tasks[existing_ref]
            task.source_article_id = task.source_article_id or source_article_id
            task.relation_type = task.relation_type or relation_type
            task.title = task.title or title
            task.heading = task.heading or heading
            task.hypothesis_ids = tuple(
                dict.fromkeys([*task.hypothesis_ids, *hypothesis_ids])
            )
            return task
        task = self._new_task(
            task_type="fetch_articles",
            status=TASK_CANDIDATE,
            origin="verified_graph_relation",
            purpose="確認済みGraph関係先の本文が質問の結論に必要か評価する",
            article_ids=(article_id,),
            source_article_id=source_article_id or None,
            target_article_id=article_id,
            target_document_id=(
                target_document_id or _document_id_from_article_id(article_id)
            ),
            relation_type=relation_type or None,
            hypothesis_ids=hypothesis_ids,
            title=title,
            heading=heading,
            # 関係種別は候補の由来であり、法的な重要度ではない。
            # LLMがAction/Checkpointで明示的に選ぶまでは全候補を同順位にする。
            priority=0,
            version=version,
        )
        self._graph_task_by_article[article_id] = task.task_ref
        return task

    def _ensure_search_candidate_task(
        self,
        *,
        article_id: str,
        title: str,
        heading: str,
        target_document_id: str,
        hypothesis_ids: tuple[str, ...],
        version: int,
    ) -> ResearchTask:
        existing_ref = self._graph_task_by_article.get(article_id)
        if existing_ref:
            task = self.tasks[existing_ref]
            task.hypothesis_ids = tuple(
                dict.fromkeys([*task.hypothesis_ids, *hypothesis_ids])
            )
            return task
        task = self._new_task(
            task_type="fetch_articles",
            status=TASK_CANDIDATE,
            origin="search_result",
            purpose="検索で発見したArticleの本文全体が結論に必要か評価する",
            article_ids=(article_id,),
            target_article_id=article_id,
            target_document_id=(
                target_document_id or _document_id_from_article_id(article_id)
            ),
            hypothesis_ids=hypothesis_ids,
            title=title,
            heading=heading,
            # 検索順位を法的な採否へ読み替えず、候補段階では同順位にする。
            priority=0,
            version=version,
        )
        self._graph_task_by_article[article_id] = task.task_ref
        return task

    def _promote_or_create_fetch_task(
        self,
        article_id: str,
        *,
        purpose: str,
        version: int,
    ) -> ResearchTask | None:
        if self._article_is_fetched(article_id):
            return None
        existing_ref = self._graph_task_by_article.get(article_id)
        if existing_ref:
            task = self.tasks[existing_ref]
            if task.status in {TASK_CANDIDATE, TASK_FAILED}:
                task.status = TASK_PENDING
                task.priority = max(task.priority, 100)
                task.purpose = purpose
                task.updated_version = version
            return task
        task = self._new_task(
            task_type="fetch_articles",
            status=TASK_PENDING,
            origin="checkpoint",
            purpose=purpose,
            article_ids=(article_id,),
            target_article_id=article_id,
            priority=100,
            version=version,
        )
        self._graph_task_by_article[article_id] = task.task_ref
        return task

    def _active_tasks(self) -> list[ResearchTask]:
        active_statuses = {
            TASK_PENDING,
            TASK_FAILED,
            TASK_CANDIDATE,
            TASK_BLOCKED,
        }
        return [
            task
            for task in self.tasks.values()
            if task.status in active_statuses
            and not (
                task.task_type == "fetch_articles"
                and task.article_ids
                and all(
                    self._article_is_fetched(article_id)
                    for article_id in task.article_ids
                )
            )
        ]

    def _select_tasks_for_llm(
        self,
        tasks: list[ResearchTask],
        *,
        limit: int,
        advance_cursor: bool = True,
    ) -> list[ResearchTask]:
        """確定pendingを優先し、candidateは対象文書ごとに巡回選択する。"""
        if limit <= 0:
            return []
        status_order = {
            TASK_PENDING: 0,
            TASK_FAILED: 1,
            TASK_BLOCKED: 2,
            TASK_CANDIDATE: 3,
        }
        sort_key = lambda task: (  # noqa: E731
            status_order[task.status],
            -task.priority,
            task.created_version,
            task.task_ref,
        )
        urgent = sorted(
            (task for task in tasks if task.status != TASK_CANDIDATE),
            key=sort_key,
        )
        selected = urgent[:limit]
        remaining = limit - len(selected)
        if remaining <= 0:
            return selected

        groups: dict[str, list[ResearchTask]] = {}
        for task in sorted(
            (task for task in tasks if task.status == TASK_CANDIDATE),
            key=sort_key,
        ):
            article_id = task.target_article_id or (
                task.article_ids[0] if task.article_ids else ""
            )
            document_id = (
                task.target_document_id
                or _document_id_from_article_id(article_id)
                or "unknown"
            )
            groups.setdefault(document_id, []).append(task)

        group_items = sorted(
            groups.items(),
            key=lambda item: sort_key(item[1][0]),
        )
        offsets = {
            document_id: self._candidate_view_offsets.get(document_id, 0)
            % len(group)
            for document_id, group in group_items
        }
        selected_per_document = {document_id: 0 for document_id, _ in group_items}
        depth = 0
        while remaining > 0:
            added = False
            for document_id, group in group_items:
                if depth >= len(group):
                    continue
                index = (offsets[document_id] + depth) % len(group)
                selected.append(group[index])
                selected_per_document[document_id] += 1
                remaining -= 1
                added = True
                if remaining <= 0:
                    break
            if not added:
                break
            depth += 1
        for document_id, count in selected_per_document.items():
            if count and advance_cursor:
                self._candidate_view_offsets[document_id] = (
                    offsets[document_id] + count
                ) % len(groups[document_id])
        return selected

    def _article_is_fetched(self, article_id: str) -> bool:
        return self.articles.get(article_id, {}).get("status") == "fetched"

    def _commit(self, event_type: str, payload: dict[str, Any]) -> CaseEvent:
        return self._commit_at_version(
            self.current_version + 1,
            event_type,
            payload,
        )

    def _commit_at_version(
        self,
        version: int,
        event_type: str,
        payload: dict[str, Any],
    ) -> CaseEvent:
        if version != self.current_version + 1:
            raise ValueError(
                f"non_sequential_case_version:{version}:"
                f"{self.current_version}"
            )
        self.current_version = version
        event = CaseEvent(
            event_ref=f"EV-{version:04d}",
            version=version,
            event_type=event_type,
            occurred_at=datetime.now(UTC).isoformat(),
            payload=payload,
        )
        self.events.append(event)
        return event


class InMemoryCaseStore:
    """単一プロセス用CaseStore。永続化実装と同じ責務境界を持つ。"""

    def __init__(self) -> None:
        self._cases: dict[str, ResearchCase] = {}

    def create_case(self, question: str) -> ResearchCase:
        case_id = f"CASE-{uuid4().hex[:12]}"
        research_case = ResearchCase(case_id=case_id, question=question)
        self._cases[case_id] = research_case
        return research_case

    def get_case(self, case_id: str) -> ResearchCase:
        return self._cases[case_id]


def _event_summary(event: CaseEvent) -> dict[str, Any]:
    payload = event.payload
    if event.event_type == "tool_result_committed":
        return {
            "taskRef": payload.get("taskRef"),
            "tool": payload.get("tool"),
            "status": payload.get("status"),
            "newEvidenceCount": payload.get("newEvidenceCount"),
            "newArticleCount": payload.get("newArticleCount"),
            "graphRelationCount": payload.get("graphRelationCount"),
            "error": payload.get("error"),
        }
    if event.event_type == "checkpoint_created":
        return {
            "checkpointRef": payload.get("checkpointRef"),
            "status": payload.get("status"),
        }
    if event.event_type.startswith("task_"):
        return {
            "taskRef": payload.get("taskRef"),
            "type": payload.get("type"),
            "origin": payload.get("origin"),
        }
    if event.event_type == "llm_stage_decision_accepted":
        return {
            "phase": payload.get("phase"),
            "status": payload.get("status"),
            "selectedEvidenceCount": len(
                payload.get("selectedEvidenceRefs") or []
            ),
        }
    return {
        key: value
        for key, value in payload.items()
        if key in {"question", "status", "phase"}
    }


def _document_id_from_article_id(article_id: str) -> str:
    if "-article-" not in article_id:
        return ""
    document_id = article_id.split("-article-", 1)[0]
    if "-suppl-" in document_id:
        document_id = document_id.split("-suppl-", 1)[0]
    return document_id
