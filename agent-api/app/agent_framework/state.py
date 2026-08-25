"""反復ループがサイクル間で引き継ぐ最小状態。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

RunStatus = Literal["running", "completed", "failed", "cancelled"]
WorkItemState = Literal["open", "resolved", "dropped"]
HypothesisJudgment = Literal["supported", "contradicted", "unresolved"]
ToolStatus = Literal["succeeded", "failed", "timeout"]
ReviewVerdict = Literal["accept", "revise"]
ReviewFindingKind = Literal[
    "unsupported_claim",
    "citation_mismatch",
    "coverage_gap",
    "dependency_gap",
    "limitation_conflict",
    "internal_contradiction",
]
ReviewFindingResolutionOutcome = Literal["addressed", "disputed"]
DependencyStatus = Literal["not_required", "needs_action", "resolved"]
FrontierReviewStatus = Literal[
    "unreviewed",
    "selected",
    "relevant_deferred",
    "rejected",
]
FrontierReviewAction = Literal["select", "defer", "reject"]
DeferredFrontierResolutionAction = Literal[
    "fetch_next_cycle",
    "carry_forward",
    "no_longer_needed",
    "unresolved_at_limit",
]
UnreviewedGraphResolutionAction = Literal[
    "review_next_cycle",
    "no_longer_needed",
    "unresolved_at_limit",
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class FrameworkModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class WorkItem(FrameworkModel):
    work_item_id: str = Field(
        min_length=1,
        max_length=160,
        description="WorkItemを参照するためのCase内一意ID。",
    )
    parent_work_item_id: str | None = Field(
        default=None,
        max_length=160,
        description="階層分解した場合の親WorkItem ID。最上位ではnull。",
    )
    question: str = Field(
        min_length=1,
        max_length=1000,
        description=(
            "1つの完了判定で閉じられる1つの確認事項。"
            "質問の行為者、行為、対象を区別し、省略主語は補う。"
        ),
    )
    action_actor: str | None = Field(
        default=None,
        max_length=600,
        description=(
            "この確認事項で、規制対象となる行為をする者。"
            "質問から特定できなければ不明と明記する。"
        ),
    )
    state: WorkItemState = Field(
        default="open",
        description="openは未完了、resolvedは回答済み、droppedは不要と判断済み。",
    )
    resolution: str | None = Field(
        default=None,
        max_length=2000,
        description="WorkItemをresolvedまたはdroppedへ閉じた理由・結論。",
    )
    basis_hypothesis_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "openではWorkItemの作成・継続を前提づけるHypothesis ID、"
            "resolvedではresolutionを支える判定済みHypothesis ID。"
            "Hypothesis.work_item_idは所属先を表す別項目であり、単なる逆参照には使わない。"
            "元の質問から直接作るopen WorkItemでは通常は空。"
        ),
    )
    replaces_work_item_id: str | None = Field(
        default=None,
        max_length=160,
        description="作業分解を修正した場合に、このWorkItemが置き換える旧WorkItem ID。",
    )

    @model_validator(mode="after")
    def validate_identity_and_state(self) -> WorkItem:
        if self.parent_work_item_id == self.work_item_id:
            raise ValueError("work item cannot be its own parent")
        if self.replaces_work_item_id == self.work_item_id:
            raise ValueError("work item cannot replace itself")
        if len(self.basis_hypothesis_ids) != len(set(self.basis_hypothesis_ids)):
            raise ValueError("basis hypothesis IDs must be unique")
        if self.state == "open" and self.resolution is not None:
            raise ValueError("open work item cannot have a resolution")
        if self.state != "open" and not self.resolution:
            raise ValueError("closed work item requires a resolution")
        return self

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_actor_scope(cls, value: object) -> object:
        """保存済みfixtureの旧自由記述を読める状態に保つ。"""

        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        legacy_scope = migrated.pop("actor_scope", None)
        if legacy_scope is not None:
            migrated.setdefault("action_actor", legacy_scope)
        migrated.pop("target_actor", None)
        migrated.pop("actor_relation", None)
        return migrated


class Hypothesis(FrameworkModel):
    hypothesis_id: str = Field(
        min_length=1,
        max_length=160,
        description="Hypothesisを参照するためのCase内一意ID。",
    )
    work_item_id: str = Field(
        min_length=1,
        max_length=160,
        description="このHypothesisが検証するWorkItem ID。",
    )
    statement: str = Field(
        min_length=1,
        max_length=1200,
        description=(
            "WorkItemの法的論点に対する、誤り得る暫定的な結論。"
            "一般的な法的知識を使い、法令本文によって支持または否定できる"
            "1つの具体的な命題とする。WorkItemの言い換えだけにはせず、"
            "確認済みの事実として扱わない。"
        ),
    )
    judgment: HypothesisJudgment = Field(
        default="unresolved",
        description=(
            "unresolvedは未確認、supportedは本文が支持、contradictedは本文が否定。"
        ),
    )
    evidence_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "現在のjudgmentとgapsの判断に使ったgrounding Evidence ID。"
            "supportedまたはcontradictedでは直接根拠を必須とし、unresolvedでは"
            "確認済み部分に使った本文がある場合だけ保持する。"
        ),
    )

    gaps: tuple[str, ...] = Field(
        default=(),
        description=(
            "暫定的な結論のうち、法令本文で確定すべき基準、値、範囲その他の"
            "未確認の規律要素。抽象的な内容、根拠条文、検索語、検索作業、"
            "WorkItemの言い換えは含めない。"
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_actor_copy(cls, value: object) -> object:
        """主体情報の正本をWorkItemへ一本化し、旧重複項目は読込み時に捨てる。"""

        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        migrated.pop("actor_scope", None)
        migrated.pop("actor_relation", None)
        return migrated

    @model_validator(mode="after")
    def require_evidence_for_semantic_judgment(self) -> Hypothesis:
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("hypothesis evidence IDs must be unique")
        if self.judgment in {"supported", "contradicted"} and not self.evidence_ids:
            raise ValueError("supported or contradicted hypothesis requires evidence")
        return self


class Evidence(FrameworkModel):
    evidence_id: str = Field(
        min_length=1,
        max_length=160,
        description="Evidenceを参照するためのCase内一意ID。",
    )
    source_ref: str = Field(
        min_length=1,
        max_length=500,
        description="取得元Resourceを識別する参照。Article IDとは限らない。",
    )
    content: str = Field(
        min_length=1,
        description="Toolから取得して保存した原文または検索・Graphのナビゲーション情報。",
    )
    title: str | None = Field(
        default=None,
        max_length=500,
        description="取得元Resourceの表示名。取得できない場合はnull。",
    )
    created_cycle: int = Field(
        ge=1,
        description="このEvidenceをCaseへ追加したResearch Cycle番号。",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Programが付与した出典・Article・Evidence役割等の来歴情報。",
    )


class DependencyDecision(FrameworkModel):
    dependency_kind: str = Field(
        min_length=1,
        max_length=160,
        description="確認対象となる下位規範依存の種類。",
    )
    work_item_id: str = Field(
        min_length=1,
        max_length=160,
        description="この依存判断が属する既知WorkItem ID。",
    )
    status: DependencyStatus = Field(
        description=(
            "not_requiredは下位規範確認不要、needs_actionは追加探索必要、"
            "resolvedは委任元と末端の本文確認済み。"
        ),
    )
    reason: str = Field(
        min_length=1,
        max_length=1000,
        description="statusを選んだ本文に基づく短い理由。",
    )
    basis_evidence_ids: tuple[str, ...] = Field(
        default=(),
        description="この状態判断に使用した取得済みgrounding Evidence ID。",
    )
    action_request_id: str | None = Field(
        default=None,
        max_length=160,
        description="needs_actionを現在stepで実行するToolRequest ID。次Cycleへ送る場合はnull。",
    )


class GraphFrontierDecision(FrameworkModel):
    frontier_item_id: str = Field(
        min_length=1,
        max_length=160,
        description="今回のgraph_review_batchにある評価単位の完全一致ID。",
    )
    article_id: str = Field(
        min_length=1,
        max_length=500,
        description="このFrontierが示すGraph候補Article ID。",
    )
    work_item_id: str = Field(
        min_length=1,
        max_length=160,
        description="この候補の関連性を評価するopen WorkItem ID。",
    )
    hypothesis_id: str | None = Field(
        default=None,
        max_length=160,
        description="この候補で検証する既知Hypothesis ID。特定されていなければnull。",
    )
    action: FrontierReviewAction = Field(
        description=(
            "selectは現在の検証で使う関連候補、deferは関連するが後続へ保留、"
            "rejectは現在のWorkItem・Hypothesisには不要。本文未取得のselectだけを"
            "Programが取得する。"
        ),
    )
    reason: str = Field(
        min_length=1,
        max_length=1000,
        description=(
            "候補の見出し・引用から読み取った法的な役割と、Hypothesisまたは"
            "gapsの未確認事項に一致するかを具体的に示すactionの理由。"
            "『関連性が高い』『優先度が低い』だけの理由は不可。"
        ),
    )


class FrontierReAdoption(FrameworkModel):
    article_id: str = Field(
        min_length=1,
        max_length=500,
        description="評価済みGraph台帳から再採用する既知Article ID。",
    )
    work_item_id: str = Field(
        min_length=1,
        max_length=160,
        description="再採用先のopen WorkItem ID。",
    )
    hypothesis_id: str = Field(
        min_length=1,
        max_length=160,
        description="再採用したArticleで検証する既知Hypothesis ID。",
    )
    reason: str = Field(
        min_length=1,
        max_length=1000,
        description="既存候補をこのWorkItem・Hypothesisへ再採用する理由。",
    )


class DeferredFrontierResolution(FrameworkModel):
    """Solverが以前のdefer判断をCycle境界でどう扱うかを明示する。"""

    frontier_item_id: str = Field(
        min_length=1,
        max_length=160,
        description="以前relevant_deferredにしたFrontierの完全一致ID。",
    )
    article_id: str = Field(
        min_length=1,
        max_length=500,
        description="保留Frontierが示す既知Article ID。",
    )
    work_item_id: str = Field(
        min_length=1,
        max_length=160,
        description="保留Frontierが属するopen WorkItem ID。",
    )
    hypothesis_id: str | None = Field(
        default=None,
        max_length=160,
        description="保留Frontierが対応するHypothesis ID。特定されていなければnull。",
    )
    action: DeferredFrontierResolutionAction = Field(
        description=(
            "fetch_next_cycleは次Cycle冒頭で取得、carry_forwardは後続へ保留、"
            "no_longer_neededは不要、unresolved_at_limitは上限で未解決。"
        ),
    )
    reason: str = Field(
        min_length=1,
        max_length=1000,
        description="保留Frontierの次の扱いを選んだ理由。",
    )
    decided_cycle: int | None = Field(
        default=None,
        ge=1,
        description="Programが記録する判断Cycle番号。新しい判断ではnull。",
    )


class UnreviewedGraphResolution(FrameworkModel):
    """SolverがCycle境界で未評価Graph候補群をどう扱うかを明示する。"""

    action: UnreviewedGraphResolutionAction = Field(
        description=(
            "review_next_cycleは次Cycleで評価、no_longer_neededは全候補不要、"
            "unresolved_at_limitは上限で未評価のまま残す。"
        ),
    )
    reason: str = Field(
        min_length=1,
        max_length=1000,
        description="未評価Graph候補群の扱いを選んだ理由。",
    )
    candidate_count: int | None = Field(
        default=None,
        ge=1,
        description="Programが記録する対象候補数。新しい判断ではnull。",
    )
    decided_cycle: int | None = Field(
        default=None,
        ge=1,
        description="Programが記録する判断Cycle番号。新しい判断ではnull。",
    )


class GraphCandidateReview(FrameworkModel):
    """今回の差分batchに対するSolver自身の意味判断。"""

    graph_request_ids: tuple[str, ...] = Field(
        description="今回のGraph Reviewが処理したGraph ToolRequest IDの全件。",
    )
    reviewed_link_ids: tuple[str, ...] = Field(
        description="今回提示されたGraph Link IDの全件。",
    )
    frontier_decisions: tuple[GraphFrontierDecision, ...] = Field(
        description="各Frontierに対するselect・defer・reject判断。",
    )
    reason: str = Field(
        min_length=1,
        max_length=2000,
        description=(
            "未確認事項と各候補の法的な役割を比較し、どの候補を優先したかを"
            "示すbatch全体の評価理由。"
        ),
    )
    reviewed_cycle: int | None = Field(
        default=None,
        ge=1,
        description="Programが記録する評価Cycle番号。新しい判断ではnull。",
    )

    @model_validator(mode="after")
    def require_unique_ids(self) -> "GraphCandidateReview":
        if len(self.graph_request_ids) != len(set(self.graph_request_ids)):
            raise ValueError("graph review request IDs must be unique")
        if len(self.reviewed_link_ids) != len(set(self.reviewed_link_ids)):
            raise ValueError("graph review Link IDs must be unique")
        frontier_ids = tuple(
            item.frontier_item_id for item in self.frontier_decisions
        )
        if len(frontier_ids) != len(set(frontier_ids)):
            raise ValueError("graph review Frontier decisions must be unique")
        return self

    @property
    def selected_article_ids(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                item.article_id
                for item in self.frontier_decisions
                if item.action == "select"
            )
        )


class SearchCandidateSelection(FrameworkModel):
    article_id: str = Field(
        min_length=1,
        max_length=500,
        description="本文取得対象として選ぶ検索候補Article ID。",
    )
    reason: str = Field(
        min_length=1,
        max_length=1000,
        description="この候補がWorkItem・Hypothesisを直接検証できる理由。",
    )
    matched_hypothesis_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "検索候補評価で、このArticleが直接検証できるとSolverが判断した"
            "Hypothesis ID。本文取得後の評価候補として引き継ぐ。"
        ),
    )

    @model_validator(mode="after")
    def require_unique_hypotheses(self) -> SearchCandidateSelection:
        if len(self.matched_hypothesis_ids) != len(
            set(self.matched_hypothesis_ids)
        ):
            raise ValueError("search selection hypothesis IDs must be unique")
        return self


class SearchCandidateAssessmentRecord(FrameworkModel):
    """Search Assessmentで確定した候補理解をCycle間で保持する。"""

    article_id: str = Field(description="評価した候補Article ID。")
    legal_function: Literal[
        "applicability", "exception", "procedure", "scope"
    ] = Field(description="Solverが判断した候補の法的機能。")
    summary: str = Field(description="候補本文の取得要否を判断するための意味要約。")
    matched_hypothesis_ids: tuple[str, ...] = Field(
        default=(),
        description="候補が直接検証できるとSolverが判断したHypothesis ID。",
    )
    matched_non_work_item_requirements: tuple[str, ...] = Field(
        default=(),
        description="候補本文の取得で満たせる回答全体の明示要求。",
    )
    actor_match_reason: str | None = Field(
        default=None,
        description="候補の規律主体とWorkItemの行為者を照合した短い理由。",
    )

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_actor_role(cls, value: object) -> object:
        """旧分類ラベルを捨て、直接照合の結果だけを引き継ぐ。"""

        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        migrated.pop("regulated_actor_role", None)
        return migrated


class SearchCandidateReview(FrameworkModel):
    """OpenSearch候補に対するSolver自身の意味判断。"""

    search_request_ids: tuple[str, ...] = Field(
        description="今回のSearch Reviewが処理したlegal_search Request IDの全件。",
    )
    selections: tuple[SearchCandidateSelection, ...] = Field(
        description="本文取得対象として選んだ候補と理由。",
    )
    assessments: tuple[SearchCandidateAssessmentRecord, ...] = Field(
        default=(),
        description="選択・保留を問わず、全候補についてSolverが行った意味評価。",
    )
    deferred_article_ids: tuple[str, ...] = Field(
        description="関連する可能性はあるが現在の取得枠では選ばなかった候補Article ID。",
    )
    reason: str = Field(
        min_length=1,
        max_length=2000,
        description="検索候補全体を選択・保留に分けた理由。",
    )
    reviewed_cycle: int | None = Field(
        default=None,
        ge=1,
        description="Programが記録する評価Cycle番号。新しい判断ではnull。",
    )

    @model_validator(mode="after")
    def require_unique_ids(self) -> SearchCandidateReview:
        if len(self.search_request_ids) != len(set(self.search_request_ids)):
            raise ValueError("search review request IDs must be unique")
        selected_ids = tuple(item.article_id for item in self.selections)
        if len(selected_ids) != len(set(selected_ids)):
            raise ValueError("search candidate selections must be unique")
        assessment_ids = tuple(item.article_id for item in self.assessments)
        if len(assessment_ids) != len(set(assessment_ids)):
            raise ValueError("search candidate assessments must be unique")
        if len(self.deferred_article_ids) != len(
            set(self.deferred_article_ids)
        ):
            raise ValueError("deferred search candidate IDs must be unique")
        if set(selected_ids) & set(self.deferred_article_ids):
            raise ValueError("selected and deferred search candidates must differ")
        return self

    @property
    def selected_article_ids(self) -> tuple[str, ...]:
        return tuple(item.article_id for item in self.selections)


class ToolRequest(FrameworkModel):
    request_id: str = Field(
        min_length=1,
        max_length=160,
        description="同じSolverDecision内で一意な短い局所ID。",
    )
    work_item_id: str = Field(
        min_length=1,
        max_length=160,
        description="このTool結果を利用する主なopen WorkItem ID。",
    )
    tool_name: str = Field(
        min_length=1,
        max_length=160,
        description="available_toolsにある正規Tool名。",
    )
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="選んだToolのinput_schemaに完全一致する引数object。",
    )
    purpose: str = Field(
        min_length=1,
        max_length=1000,
        description="何を確認するための要求かを示す短い説明。",
    )
    hypothesis_ids: tuple[str, ...] = Field(
        default=(),
        description="このTool結果で検証する既知Hypothesis ID。",
    )

    @model_validator(mode="after")
    def require_unique_hypotheses(self) -> ToolRequest:
        if len(self.hypothesis_ids) != len(set(self.hypothesis_ids)):
            raise ValueError("tool request hypothesis IDs must be unique")
        return self


class ToolResult(FrameworkModel):
    request_id: str = Field(
        min_length=1,
        max_length=160,
        description="結果が対応する既知ToolRequest ID。",
    )
    status: ToolStatus = Field(
        description="succeededは完了、failedは失敗、timeoutは時間切れ。意味的な成否ではない。",
    )
    evidence_ids: tuple[str, ...] = Field(
        default=(),
        description="このTool実行でCaseへ追加されたEvidence ID。",
    )
    error_code: str | None = Field(
        default=None,
        max_length=160,
        description="failedまたはtimeoutの機械的エラーコード。成功時はnull。",
    )
    elapsed_ms: int = Field(
        default=0,
        ge=0,
        description="Tool実行に要したミリ秒。",
    )
    cycle_no: int = Field(
        ge=1,
        description="このToolを実行したResearch Cycle番号。",
    )

    @model_validator(mode="after")
    def validate_result(self) -> ToolResult:
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("tool result evidence IDs must be unique")
        if self.status == "succeeded" and self.error_code is not None:
            raise ValueError("successful tool result cannot have an error code")
        if self.status != "succeeded" and not self.error_code:
            raise ValueError("failed tool result requires an error code")
        return self


class FinalAnswer(FrameworkModel):
    text: str = Field(min_length=1, description="質問へ返す根拠付き回答本文。")
    citation_ids: tuple[str, ...] = Field(
        default=(),
        description="回答で実際に使用したgrounding Evidence ID。",
    )
    limitations: tuple[str, ...] = Field(
        default=(),
        description="上限等により確認できなかった事項と回答上の制約。",
    )
    unresolved_work_item_ids: tuple[str, ...] = Field(
        default=(),
        description="限定回答で未解決のまま残すWorkItem ID。",
    )
    unresolved_hypothesis_ids: tuple[str, ...] = Field(
        default=(),
        description="限定回答で未解決のまま残すHypothesis ID。",
    )

    @model_validator(mode="after")
    def require_unique_unresolved_ids(self) -> FinalAnswer:
        if len(self.unresolved_work_item_ids) != len(
            set(self.unresolved_work_item_ids)
        ):
            raise ValueError("unresolved work item IDs must be unique")
        if len(self.unresolved_hypothesis_ids) != len(
            set(self.unresolved_hypothesis_ids)
        ):
            raise ValueError("unresolved hypothesis IDs must be unique")
        return self


class ReviewFinding(FrameworkModel):
    finding_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        description="このReviewResult内で一意な短いASCII ID。",
    )
    kind: ReviewFindingKind = Field(
        description="指摘の種類。値ごとの意味はReviewer PromptのFinding契約に従う。",
    )
    description: str = Field(
        min_length=1,
        max_length=1000,
        description="何と何が整合しないかを示す具体的な指摘。",
    )
    work_item_id: str | None = Field(
        default=None,
        max_length=160,
        description="指摘に対応する既知WorkItem ID。特定できなければnull。",
    )
    hypothesis_id: str | None = Field(
        default=None,
        max_length=160,
        description="指摘に対応する既知Hypothesis ID。特定できなければnull。",
    )
    basis_evidence_ids: tuple[str, ...] = Field(
        default=(),
        description="指摘の判断に使用した既知grounding Evidence ID。",
    )

    @model_validator(mode="after")
    def require_unique_evidence(self) -> ReviewFinding:
        if len(self.basis_evidence_ids) != len(set(self.basis_evidence_ids)):
            raise ValueError("review finding evidence IDs must be unique")
        return self


class ReviewFindingResolution(FrameworkModel):
    finding_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        description="今回処理する既知Reviewer Finding ID。",
    )
    outcome: ReviewFindingResolutionOutcome = Field(
        description="addressedは修正済み、disputedは本文根拠により採用しない。",
    )
    reason: str = Field(
        min_length=1,
        max_length=1000,
        description="指摘への対応または不採用の理由。",
    )
    basis_evidence_ids: tuple[str, ...] = Field(
        default=(),
        description="disputed判断に使用した既知grounding Evidence ID。",
    )

    @model_validator(mode="after")
    def require_unique_evidence(self) -> ReviewFindingResolution:
        if len(self.basis_evidence_ids) != len(set(self.basis_evidence_ids)):
            raise ValueError("review resolution evidence IDs must be unique")
        return self


class ReviewResult(FrameworkModel):
    verdict: ReviewVerdict = Field(
        description="acceptは回答を承認、reviseはFindingに基づくSolver再判断が必要。",
    )
    findings: tuple[ReviewFinding, ...] = Field(
        default=(),
        description="verdict=reviseの場合に返す、独立した問題ごとの指摘。",
    )

    @model_validator(mode="after")
    def validate_findings(self) -> ReviewResult:
        if self.verdict == "accept" and self.findings:
            raise ValueError("accepted review cannot contain findings")
        if self.verdict == "revise" and not self.findings:
            raise ValueError("revision requires at least one finding")
        return self


class CaseState(FrameworkModel):
    case_id: str = Field(min_length=1, max_length=160)
    question: str = Field(min_length=1)
    non_work_item_requirements: tuple[str, ...] = Field(
        default=(),
        description=(
            "元の質問が明示する要求のうち、独立した法的結論を要するWorkItemには"
            "しなかった要求。根拠・出典・引用・対象時点・地域・出力形式等を保持する。"
            "重要度や適用範囲を表す分類ではなく、元の質問を正本とする。"
        ),
    )
    run_status: RunStatus = "running"
    research_cycle_count: int = Field(default=0, ge=0)
    work_items: tuple[WorkItem, ...] = ()
    hypotheses: tuple[Hypothesis, ...] = ()
    tool_requests: tuple[ToolRequest, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    dependency_decisions: tuple[DependencyDecision, ...] = ()
    graph_candidate_reviews: tuple[GraphCandidateReview, ...] = ()
    search_candidate_reviews: tuple[SearchCandidateReview, ...] = ()
    frontier_re_adoptions: tuple[FrontierReAdoption, ...] = ()
    deferred_frontier_resolutions: tuple[DeferredFrontierResolution, ...] = ()
    unreviewed_graph_resolutions: tuple[UnreviewedGraphResolution, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()
    integrated_tool_result_request_ids: tuple[str, ...] = Field(
        default=(),
        description="取得本文をWorkItem・Hypothesisへ反映済みのToolRequest ID。",
    )
    focus_work_item_ids: tuple[str, ...] = ()
    retained_evidence_ids: tuple[str, ...] = ()
    final_answer: FinalAnswer | None = None
    review: ReviewResult | None = None
    review_finding_resolutions: tuple[ReviewFindingResolution, ...] = ()
    stop_reason: str | None = Field(default=None, max_length=160)
    cycle_step_timeout: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
