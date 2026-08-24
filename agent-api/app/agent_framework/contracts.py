"""SolverがCaseStateへ適用できる、全体再生成ではない変更契約。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .state import (
    DeferredFrontierResolution,
    DependencyDecision,
    FinalAnswer,
    FrameworkModel,
    FrontierReAdoption,
    GraphCandidateReview,
    Hypothesis,
    HypothesisJudgment,
    ReviewFindingResolution,
    SearchCandidateReview,
    SearchCandidateSelection,
    ToolRequest,
    UnreviewedGraphResolution,
    WorkItem,
    WorkItemState,
)


class WorkItemUpdate(FrameworkModel):
    work_item_id: str = Field(
        min_length=1,
        max_length=160,
        description="更新する既存WorkItemの完全一致ID。",
    )
    state: WorkItemState = Field(description="更新後のWorkItem状態。")
    resolution: str | None = Field(
        default=None,
        max_length=2000,
        description="resolvedまたはdroppedにする理由・結論。openではnull。",
    )
    basis_hypothesis_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "更新後のbasis。openでは作成・継続の前提Hypothesis ID、"
            "resolvedではresolutionを支える判定済みHypothesis ID。"
            "Hypothesis.work_item_idは所属先を表す別項目。"
        ),
    )


class HypothesisUpdate(FrameworkModel):
    hypothesis_id: str = Field(
        min_length=1,
        max_length=160,
        description="更新する既存Hypothesisの完全一致ID。",
    )
    judgment: HypothesisJudgment = Field(description="本文評価後の判定。")
    evidence_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "現在の判定とgapsの判断に使った取得済みgrounding Evidence ID。"
            "unresolvedでも本文で確認できた部分があれば保持できる。"
        ),
    )
    gaps: tuple[str, ...] = Field(
        default=(),
        description="この命題を判定するために、本文でまだ確認すべき具体的情報。",
    )


class WorkItemImpactDecision(FrameworkModel):
    work_item_id: str = Field(
        min_length=1,
        max_length=160,
        description="今回contradictedになった前提の影響を受ける既知WorkItem ID。",
    )
    action: Literal["retain", "replace", "drop"] = Field(
        description=(
            "retainは問いを維持して前提だけ更新、replaceは別の問いへ置換、"
            "dropは質問への回答に不要として終了。"
        ),
    )
    reason: str = Field(
        min_length=1,
        max_length=1000,
        description="反証後もWorkItemを維持・置換・破棄する理由。",
    )
    new_basis_hypothesis_ids: tuple[str, ...] = Field(
        default=(),
        description="retainまたはreplace後のWorkItem判断が依存する既知Hypothesis ID。",
    )
    replacement_work_item_id: str | None = Field(
        default=None,
        max_length=160,
        description="action=replaceで同じDecisionに追加する置換先WorkItem ID。それ以外はnull。",
    )
    drop_subtree: bool = Field(
        default=False,
        description="action=dropで子孫WorkItemも一緒に破棄する場合だけtrue。",
    )

    @model_validator(mode="after")
    def validate_action_shape(self) -> WorkItemImpactDecision:
        if self.action == "replace" and self.replacement_work_item_id is None:
            raise ValueError("replace impact requires replacement work item ID")
        if self.action != "replace" and self.replacement_work_item_id is not None:
            raise ValueError("only replace impact may name a replacement")
        if self.action != "drop" and self.drop_subtree:
            raise ValueError("only drop impact may drop a subtree")
        if self.action == "drop" and self.new_basis_hypothesis_ids:
            raise ValueError("drop impact cannot assign a new basis")
        return self


class SearchCandidateAssessment(FrameworkModel):
    article_id: str = Field(
        min_length=1,
        max_length=500,
        description="今回提示されたOpenSearch候補Article ID。",
    )
    legal_function: Literal[
        "applicability",
        "exception",
        "procedure",
        "scope",
    ] = Field(
        description=(
            "applicabilityは適用条件、exceptionは例外、procedureは手続、"
            "scopeは対象範囲として、この候補が直接検証できる主な機能。"
        ),
    )
    summary: str = Field(
        min_length=1,
        max_length=2000,
        description="検索抜粋から読み取れる候補内容の短い自己要約。回答根拠ではない。",
    )
    matched_hypothesis_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "主体、行為、対象、条件が一致し、この候補が直接検証できる未確認"
            "Hypothesis ID。該当しなければ空。"
        ),
    )


class SearchAssessmentDecision(FrameworkModel):
    search_request_ids: tuple[str, ...] = Field(
        description="今回評価するlegal_search Request IDの全件。",
    )
    assessments: tuple[SearchCandidateAssessment, ...] = Field(
        description="提示順を保ち、全検索候補を一度ずつ評価した一覧。",
    )
    reason: str = Field(
        min_length=1,
        max_length=2000,
        description="候補全体をどの確認事項・法的機能から評価したかの短い説明。",
    )


class SearchReselectionDecision(FrameworkModel):
    selections: tuple[SearchCandidateSelection, ...] = Field(
        description="現在の本文取得枠で取得する候補と選択理由。",
    )
    reason: str = Field(
        min_length=1,
        max_length=2000,
        description="未確認Hypothesisと取得枠から、この候補集合を選んだ理由。",
    )


class CaseUpdate(FrameworkModel):
    set_non_work_item_requirements: tuple[str, ...] | None = Field(
        default=None,
        description=(
            "初回の質問分解で、独立した法的結論を要するWorkItem以外に残った"
            "明示要求を設定する。nullは変更なし。"
        ),
    )
    add_work_items: tuple[WorkItem, ...] = Field(
        default=(),
        description="今回新しく作る、重複しない確認事項。",
    )
    update_work_items: tuple[WorkItemUpdate, ...] = Field(
        default=(),
        description="既存WorkItemに対する今回の状態差分。",
    )
    add_hypotheses: tuple[Hypothesis, ...] = Field(
        default=(),
        description="新しいWorkItem等へ置く、本文で独立検証可能な命題。",
    )
    update_hypotheses: tuple[HypothesisUpdate, ...] = Field(
        default=(),
        description="既存Hypothesisに対する今回の判定差分。",
    )
    impact_decisions: tuple[WorkItemImpactDecision, ...] = Field(
        default=(),
        description="前提Hypothesisが否定されたときの子WorkItemの維持・置換・破棄。",
    )


class ObservationIntegrationDecision(FrameworkModel):
    decision_reason: str = Field(
        min_length=1,
        max_length=1200,
        description="提示された取得本文をどの確認事項へ反映したかの短い説明。",
    )
    update_work_items: tuple[WorkItemUpdate, ...] = Field(
        default=(),
        description="取得本文の評価により状態を変更する既存WorkItem。",
    )
    update_hypotheses: tuple[HypothesisUpdate, ...] = Field(
        default=(),
        description="取得本文の評価により判定または未確認事項を変更する既存Hypothesis。",
    )
    dependency_decisions: tuple[DependencyDecision, ...] = Field(
        default=(),
        description="取得本文から判断した、対象WorkItemごとの下位規範確認状態。",
    )


class EvidenceIntegrationDecision(FrameworkModel):
    decision_reason: str = Field(
        min_length=1,
        max_length=1200,
        description="提示された取得本文をどの確認事項へ反映したかの短い説明。",
    )
    update_work_items: tuple[WorkItemUpdate, ...] = Field(default=())
    update_hypotheses: tuple[HypothesisUpdate, ...] = Field(default=())


class DependencyAssessmentDecision(FrameworkModel):
    decision_reason: str = Field(
        min_length=1,
        max_length=1200,
        description="下位規範の末端本文確認状態を判断した短い説明。",
    )
    dependency_decisions: tuple[DependencyDecision, ...] = Field(default=())


class CycleCloseDecision(FrameworkModel):
    outcome: Literal["start_next_cycle", "finalize"] = Field(
        description="未確認事項を次Cycleへ送るか、既知根拠で回答を完了するか。",
    )
    decision_reason: str = Field(
        min_length=1,
        max_length=1200,
        description="未確認事項、完了条件、残りCycleに結び付けた境界判断の短い説明。",
    )
    next_focus_work_item_ids: tuple[str, ...] = Field(
        default=(),
        description="次Cycleで優先する、統合後もopenの既知WorkItem ID。",
    )
    retain_evidence_ids: tuple[str, ...] = Field(
        default=(),
        description="次Cycleにも本文提示が必要な、提示済みの取得本文Evidence ID。",
    )
    deferred_frontier_resolutions: tuple[DeferredFrontierResolution, ...] = Field(
        default=(),
        description="保留中Graph候補を次Cycleへ引き継ぐか終了するかの判断。",
    )
    unreviewed_graph_resolution: UnreviewedGraphResolution | None = Field(
        default=None,
        description="未評価Graph候補が残る場合のCycle境界での扱い。",
    )
    answer: FinalAnswer | None = Field(
        default=None,
        description="outcome=finalizeの場合だけ返す根拠付き回答。",
    )

    @model_validator(mode="after")
    def validate_outcome_shape(self) -> CycleCloseDecision:
        if self.outcome == "start_next_cycle":
            if self.answer is not None:
                raise ValueError("next cycle decision cannot contain an answer")
        elif self.answer is None:
            raise ValueError("finalize cycle decision requires an answer")
        return self


class SolverDecision(FrameworkModel):
    next: Literal["continue", "finalize"] = Field(
        description=(
            "追加のaction-observation stepまたは次Cycleが必要ならcontinue、"
            "根拠付き回答を返せるならfinalize。Solverが決める。"
        ),
    )
    # Providerの隠れた思考ではなく、このStepでnextを選んだ監査可能な短い理由。
    # defaultは旧fixture・保存データとの互換用で、Provider schemaでは必須にする。
    decision_reason: str = Field(
        default="",
        max_length=1200,
        description=(
            "提示された根拠、未確認事項、上限に結び付けた今回の判断理由。"
            "隠れた思考過程ではなく短い監査説明。"
        ),
    )
    start_next_cycle: bool = Field(
        default=False,
        description=(
            "現在Cycleを評価して閉じ、別の仮説・方針で次Cycleを開始する場合だけtrue。"
        ),
    )
    update: CaseUpdate = Field(
        default_factory=CaseUpdate,
        description="CaseState全体ではなく、今回適用する意味上の差分。",
    )
    next_focus_work_item_ids: tuple[str, ...] = Field(
        default=(),
        description="次のstepで優先する、更新適用後もopenの既知WorkItem ID。",
    )
    retain_evidence_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "後続Cycleにも本文提示が必要な既知Evidence ID。"
            "同じIDは重複させず1回だけ指定する。"
        ),
    )
    review_finding_resolutions: tuple[ReviewFindingResolution, ...] = Field(
        default=(),
        description="Reviewerの各指摘を反映したか、本文に基づき採用しないかの回答。",
    )
    dependency_decisions: tuple[DependencyDecision, ...] = Field(
        default=(),
        description="各対象WorkItemについて下位規範確認が必要かを示すSolver判断。",
    )
    graph_candidate_review: GraphCandidateReview | None = Field(
        default=None,
        description="現在のGraph候補差分に対するselect・defer・reject判断。",
    )
    search_candidate_review: SearchCandidateReview | None = Field(
        default=None,
        description="現在のOpenSearch候補から本文取得対象を選ぶ判断。",
    )
    frontier_re_adoptions: tuple[FrontierReAdoption, ...] = Field(
        default=(),
        description="既存Graph候補を別のopen WorkItem・Hypothesisへ再採用する判断。",
    )
    deferred_frontier_resolutions: tuple[DeferredFrontierResolution, ...] = Field(
        default=(),
        description="保留中Graph候補を継続・不要・上限未解決のいずれかへ更新する判断。",
    )
    unreviewed_graph_resolution: UnreviewedGraphResolution | None = Field(
        default=None,
        description="未評価Graph候補が実行上限時に残る場合の扱い。",
    )
    tool_requests: tuple[ToolRequest, ...] = Field(
        default=(),
        description="未確認Hypothesisを検証するため、Solverが今回選ぶread-only Tool要求。",
    )
    answer: FinalAnswer | None = Field(
        default=None,
        description="next=finalizeの場合だけ返す根拠付き回答。",
    )

    @model_validator(mode="after")
    def validate_next_shape(self) -> SolverDecision:
        if self.next == "finalize" and self.start_next_cycle:
            raise ValueError("finalize decision cannot start the next cycle")
        if self.next == "continue":
            if (
                not self.tool_requests
                and self.update == CaseUpdate()
                and self.graph_candidate_review is None
                and self.search_candidate_review is None
                and not self.frontier_re_adoptions
                and not self.deferred_frontier_resolutions
                and self.unreviewed_graph_resolution is None
                and not self.start_next_cycle
            ):
                raise ValueError("continue decision requires a state update or action")
            if self.answer is not None:
                raise ValueError("continue decision cannot contain an answer")
        else:
            if self.tool_requests:
                raise ValueError("finalize decision cannot contain tool requests")
            if self.frontier_re_adoptions:
                raise ValueError("finalize decision cannot re-adopt a Frontier")
            if self.answer is None:
                raise ValueError("finalize decision requires an answer")
        return self
