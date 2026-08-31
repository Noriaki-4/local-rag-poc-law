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
    state: WorkItemState = Field(
        description=(
            "resolvedはProgramが導出した完了、droppedはSolverが判断した構造変更。"
        )
    )
    resolution: str | None = Field(
        default=None,
        max_length=2000,
        description=(
            "resolvedの機械的完了理由またはdroppedの除外理由。openではnull。"
        ),
    )
    basis_hypothesis_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "更新後のbasis。openでは作成・継続の前提Hypothesis ID、"
            "resolvedではProgramが集約した所属先の判定済みHypothesis ID。"
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
            "今回の本文評価で、現在の判定とgapsの判断に新たに使用した"
            "grounding_evidence[].evidence_idの最小集合。"
            "既にHypothesis.evidence_idsにあるIDは再出力しなくてよく、"
            "Programが既存IDを保持して追記する。"
            "metadata.articleIdやmetadata.sourceContentUnitIdは入れない。"
            "関連するだけのParagraph・Itemを全件入れない。"
            "unresolvedでも本文で確認できた部分があれば保持できる。"
        ),
    )
    gaps: tuple[str, ...] = Field(
        default=(),
        description=(
            "このHypothesisをWorkItemへの回答に使うため、本文でまだ確認すべき"
            "具体的情報。statementがsupportedでも、必要な下位規範本文が未確認なら"
            "保持する。該当する情報がなければ空とする。"
        ),
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
            "見出しと検索抜粋が直接示す主な法的機能。applicabilityは適用条件、"
            "exceptionは適用しない場合、procedureは規律に従うための手続、"
            "scopeは対象の意味または範囲。"
        ),
    )
    summary: str = Field(
        min_length=1,
        max_length=2000,
        description=(
            "提示された見出しと検索抜粋が扱う内容の短い要約。"
            "Article全文の内容を推測せず、回答根拠として扱わない。"
        ),
    )
    matched_hypothesis_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "見出しと検索抜粋が同じ行為と規律を直接扱い、本文を確認する価値がある"
            "Hypothesis ID。同じ制度名や語句を含むだけの場合は含めない。"
            "行為者を確定できない場合も、本文確認の価値があれば含められる。"
        ),
    )

    @model_validator(mode="after")
    def require_unique_hypothesis_ids(self) -> SearchCandidateAssessment:
        if len(self.matched_hypothesis_ids) != len(
            set(self.matched_hypothesis_ids)
        ):
            raise ValueError("search assessment hypothesis IDs must be unique")
        return self


class SearchAssessmentDecision(FrameworkModel):
    assessments: tuple[SearchCandidateAssessment, ...] = Field(
        description="提示順を保ち、全本文取得候補を一度ずつ評価した一覧。",
    )


class SearchReselectionDecision(FrameworkModel):
    selections: tuple[SearchCandidateSelection, ...] = Field(
        description=(
            "今回の1回の本文取得要求で取得する候補と選択理由。"
            "同じArticleは重複させず最大1回だけ含める。"
        ),
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
            "初回の質問分解で、根拠・出典の提示や表現・出力形式等、"
            "法令の内容自体ではない回答要件を設定する。nullは変更なし。"
        ),
    )
    add_work_items: tuple[WorkItem, ...] = Field(
        default=(),
        description="今回新しく作る確認事項。work_item_idごとに1件だけ返す。",
    )
    update_work_items: tuple[WorkItemUpdate, ...] = Field(
        default=(),
        description=(
            "既存WorkItemに対する今回の最終状態差分。"
            "同じwork_item_idの更新は1件だけ返す。"
        ),
    )
    add_hypotheses: tuple[Hypothesis, ...] = Field(
        default=(),
        description=(
            "新しいWorkItem等へ置く、本文で独立検証可能な命題。"
            "hypothesis_idごとに1件だけ返す。"
        ),
    )
    update_hypotheses: tuple[HypothesisUpdate, ...] = Field(
        default=(),
        description=(
            "既存Hypothesisに対する今回の最終判定差分。"
            "同じhypothesis_idの更新は1件だけ返す。"
        ),
    )
    impact_decisions: tuple[WorkItemImpactDecision, ...] = Field(
        default=(),
        description=(
            "前提Hypothesisが否定されたときの子WorkItemの維持・置換・破棄。"
            "work_item_idごとに1件だけ返す。"
        ),
    )


class ObservationIntegrationDecision(FrameworkModel):
    decision_reason: str = Field(
        min_length=1,
        max_length=1200,
        description="提示された取得本文をどの確認事項へ反映したかの短い説明。",
    )
    update_work_items: tuple[WorkItemUpdate, ...] = Field(
        default=(),
        description=(
            "ProgramがHypothesisと下位規範確認の状態から機械的に"
            "導出した既存WorkItemの完了差分。LLMの直接出力ではない。"
        ),
    )
    update_hypotheses: tuple[HypothesisUpdate, ...] = Field(
        default=(),
        description="取得本文の評価により判定または未確認事項を変更する既存Hypothesis。",
    )
    dependency_decisions: tuple[DependencyDecision, ...] = Field(
        default=(),
        description="取得本文から判断した、対象WorkItemごとの下位規範確認状態。",
    )
    tool_requests: tuple[ToolRequest, ...] = Field(
        default=(),
        description=(
            "取得本文の評価から直ちに必要と判断した次のread-only Tool要求。"
            "WorkItemごとに最大1件。"
        ),
    )


class EvidenceIntegrationDecision(FrameworkModel):
    decision_reason: str = Field(
        min_length=1,
        max_length=1200,
        description="提示された取得本文をどの確認事項へ反映したかの短い説明。",
    )
    update_hypotheses: tuple[HypothesisUpdate, ...] = Field(default=())


class DependencyAssessmentDecision(FrameworkModel):
    decision_reason: str = Field(
        min_length=1,
        max_length=1200,
        description="下位規範の末端本文確認状態を判断した短い説明。",
    )
    dependency_decisions: tuple[DependencyDecision, ...] = Field(default=())


class DependencyActionDecision(FrameworkModel):
    decision_reason: str = Field(
        min_length=1,
        max_length=1200,
        description="未確認の下位規範に対してToolまたは次Cycleを選んだ短い理由。",
    )
    start_next_cycle: bool = Field(
        default=False,
        description=(
            "trueはToolなしで次Cycleへ移り、探索方針を見直す。"
            "棄却後に別種のToolを選ばない場合にも使う。"
        ),
    )
    tool_requests: tuple[ToolRequest, ...] = Field(
        default=(),
        description=(
            "start_next_cycle=falseのとき、処理上限内で選んだneeds_action "
            "WorkItemを進める今回のTool要求。"
        ),
    )

    @model_validator(mode="after")
    def validate_action_shape(self) -> DependencyActionDecision:
        if self.start_next_cycle and self.tool_requests:
            raise ValueError("next Cycle action cannot include ToolRequest")
        if not self.start_next_cycle and not self.tool_requests:
            raise ValueError("current Cycle action requires ToolRequest")
        return self


class CycleCloseDecision(FrameworkModel):
    decision_reason: str = Field(
        min_length=1,
        max_length=1200,
        description=(
            "Programから指定されたCycle遷移について、未確認事項または完了根拠に"
            "結び付けた短い説明。"
        ),
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


class HypothesisRevisionProposal(FrameworkModel):
    """既存の非dropped WorkItemへ追加する、新しい未確認Hypothesisの提案。"""

    hypothesis_id: str = Field(
        min_length=1,
        max_length=160,
        description="新しいHypothesisのCase内一意ID。",
    )
    work_item_id: str = Field(
        min_length=1,
        max_length=160,
        description="追加先の既存WorkItem ID。",
    )
    statement: str = Field(
        min_length=1,
        max_length=1200,
        description="取得本文から判明した、独立して確認する新しい命題。",
    )
    evidence_ids: tuple[str, ...] = Field(
        default=(),
        max_length=12,
        description="この命題の必要性を示した取得済みEvidence ID。",
    )
    gaps: tuple[str, ...] = Field(
        default=(),
        max_length=12,
        description="この命題について本文で未確認の事項。",
    )


class HypothesisRevisionDecision(FrameworkModel):
    """取得本文に独立した未解決命題があった場合だけ返す追加差分。"""

    decision_reason: str = Field(
        min_length=1,
        max_length=1200,
        description="Hypothesisを追加した又は追加しなかった理由。",
    )
    add_hypotheses: tuple[HypothesisRevisionProposal, ...] = Field(
        default=(),
        max_length=8,
        description="既存WorkItemへ追加する新しいHypothesis。",
    )


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
                and not self.dependency_decisions
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
