"""CaseStateから意味選別なしでSolver入力を組み立てる。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import Field, model_validator

from .contracts import SolverDecision
from .profiles import AgentLimits
from .state import (
    AnswerOption,
    CaseState,
    DeferredFrontierResolutionAction,
    DependencyDecision,
    Evidence,
    FrameworkModel,
    FrontierReviewStatus,
    Hypothesis,
    HypothesisGap,
    ReviewFinding,
    ToolRequest,
    ToolResult,
    ToolStatus,
    WorkItem,
    fetched_article_ids_by_work_item,
    tool_request_matches_current_hypotheses,
)
from .tool_contracts import ToolDefinition


GRAPH_REVIEW_FETCH_REQUEST_PREFIX = "graph-review-fetch-"


class WorkTreeItem(FrameworkModel):
    work_item_id: str = Field(description="投影元WorkItemのCase内一意ID。")
    parent_work_item_id: str | None = Field(
        description="階層分解上の親WorkItem ID。最上位ではnull。"
    )
    question: str = Field(description="1つの完了判定で閉じる確認事項。")
    action_actor: str | None = Field(
        default=None,
        description="確認事項で規制対象となる行為をする者。未指定ならnull。"
    )
    state: str = Field(
        description=(
            "openは未完了、resolvedはProgramが導出した完了、"
            "droppedはSolverが判断した構造変更。"
        )
    )
    resolution: str | None = Field(
        description="resolvedの機械的完了理由またはdroppedの除外理由。openではnull。"
    )
    basis_hypothesis_ids: tuple[str, ...] = Field(
        description=(
            "openでは作成・継続の前提Hypothesis ID、resolvedではProgramが集約した"
            "所属先の判定済みHypothesis ID。"
        )
    )
    replaces_work_item_id: str | None = Field(
        description="作業分解の修正で置き換えた旧WorkItem ID。なければnull。"
    )
    hypothesis_ids: tuple[str, ...] = Field(
        description="Hypothesis.work_item_idにより、このWorkItemへ所属する全Hypothesis ID。"
    )
    evidence_count: int = Field(
        ge=0,
        description="このWorkItem所属Hypothesisが参照する重複なしEvidence件数。",
    )

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_actor_scope(cls, value: object) -> object:
        """保存済みSolverContextの旧自由記述を読める状態に保つ。"""

        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        legacy_scope = migrated.pop("actor_scope", None)
        if legacy_scope is not None:
            migrated.setdefault("action_actor", legacy_scope)
        migrated.pop("target_actor", None)
        migrated.pop("actor_relation", None)
        return migrated


class EvidenceHypothesisCandidate(FrameworkModel):
    article_id: str = Field(description="取得本文が属するArticle ID。")
    hypothesis_ids: tuple[str, ...] = Field(
        description=(
            "本文取得前の候補評価で、このArticleに対応するとSolverが判断した"
            "Hypothesis ID。支持・反証の確定結果ではない。"
        ),
    )
    reason: str = Field(description="本文取得対象にした時点での短い選択理由。")
    assessment_summary: str | None = Field(
        default=None,
        description=(
            "Hypothesisとの照合前に、見出しと検索抜粋だけから作成した候補内容の要約。"
            "支持・反証の判定結果ではない。"
        ),
    )


class EvidenceManifestItem(FrameworkModel):
    evidence_id: str = Field(description="Caseで既知のEvidence ID。")
    source_ref: str = Field(description="Evidenceの取得元Resource参照。")
    title: str | None = Field(description="取得元Resourceの表示名。なければnull。")
    content_chars: int = Field(
        ge=0,
        description="保存済みEvidence本文の文字数。",
    )
    created_cycle: int = Field(
        ge=1,
        description="EvidenceをCaseへ追加したResearch Cycle番号。",
    )
    material_included: bool = Field(
        description="trueの場合だけ、今回のmaterial_evidenceに本文が提示されている。"
    )


GraphCandidateContentStatus = Literal[
    "not_requested",
    "pending",
    "succeeded",
    "failed",
    "timeout",
]


class GraphCandidateArticle(FrameworkModel):
    """Graph端点ArticleのSolver向け正規化投影。"""

    article_id: str = Field(description="Graphで発見した候補Article ID。")
    document_id: str | None = Field(description="候補の所属Document ID。なければnull。")
    title: str | None = Field(description="候補Documentの表示名。なければnull。")
    heading: str | None = Field(description="候補Articleの見出し。なければnull。")
    content_status: GraphCandidateContentStatus = Field(
        description=(
            "not_requestedは本文未要求、pendingは要求済み未完了、succeededは全文取得済み、"
            "failedは取得失敗、timeoutは取得時間切れ。"
        )
    )


class GraphCandidateLink(FrameworkModel):
    """Articleの重複なしに全発見経路を保持する投影。"""

    link_id: str = Field(description="同じGraph発見経路を識別する安定ID。")
    seed_article_id: str = Field(description="1ホップGraph検索の起点Article ID。")
    candidate_article_id: str = Field(description="1ホップ先で発見した候補Article ID。")
    work_item_ids: tuple[str, ...] = Field(
        description="この発見経路を要求したToolRequestに紐づくWorkItem ID。"
    )
    hypothesis_ids: tuple[str, ...] = Field(
        description="この発見経路を要求したToolRequestに紐づくHypothesis ID。"
    )
    relations: tuple[dict[str, Any], ...] = Field(
        description="起点と候補の間でToolが返した関係・方向・分類根拠の一覧。"
    )
    graph_request_ids: tuple[str, ...] = Field(
        description="このLinkを発見したlegal_graph_neighbors Request ID。"
    )


class GraphCandidateCatalog(FrameworkModel):
    articles: tuple[GraphCandidateArticle, ...] = Field(
        default=(),
        description="Article IDで重複排除したGraph候補一覧。",
    )
    links: tuple[GraphCandidateLink, ...] = Field(
        default=(),
        description="各Graph候補を発見した全経路。",
    )


class GraphReviewCandidate(FrameworkModel):
    frontier_item_id: str = Field(
        description="Article・WorkItem・Hypothesisの組で作る今回の評価単位ID。"
    )
    article_id: str = Field(description="評価するGraph候補Article ID。")
    document_id: str | None = Field(description="候補の所属Document ID。なければnull。")
    title: str | None = Field(description="候補Documentの表示名。なければnull。")
    heading: str | None = Field(description="候補Articleの見出し。なければnull。")
    work_item_id: str = Field(description="候補との関連性を評価するopen WorkItem ID。")
    hypothesis_id: str | None = Field(
        description="候補で検証するHypothesis ID。発見元で特定されていなければnull。"
    )
    review_trigger: Literal["new_frontier", "re_adopted", "new_link"] = Field(
        description=(
            "new_frontierは初見、re_adoptedは別Hypothesisへの再採用、"
            "new_linkは既評価候補に新しい発見経路が追加された状態。"
        )
    )
    prior_review_status: FrontierReviewStatus | None = Field(
        description="以前の関連性評価状態。初回はnull。"
    )
    content_status: GraphCandidateContentStatus = Field(
        description="候補Article本文の取得状態。関連性評価とは別。"
    )
    links: tuple[GraphCandidateLink, ...] = Field(
        description="この候補を今回のWorkItem・Hypothesisへ結び付ける全発見経路。"
    )


class GraphReviewLedgerItem(FrameworkModel):
    frontier_item_id: str = Field(description="評価済みFrontierの安定ID。")
    article_id: str = Field(description="評価済みGraph候補Article ID。")
    title: str | None = Field(description="候補Documentの表示名。なければnull。")
    heading: str | None = Field(description="候補Articleの見出し。なければnull。")
    work_item_id: str = Field(description="この評価が属するWorkItem ID。")
    hypothesis_id: str | None = Field(
        description="この評価が属するHypothesis ID。特定されていなければnull。"
    )
    review_status: Literal["selected", "relevant_deferred", "rejected"] = Field(
        description="selectedは採用、relevant_deferredは関連するが保留、rejectedは不要。"
    )
    reason: str = Field(description="最新の関連性評価理由。")
    content_status: GraphCandidateContentStatus = Field(
        description="候補Article本文の最新取得状態。関連性評価とは別。"
    )
    last_reviewed_cycle: int | None = Field(
        description="最後に関連性を評価したCycle番号。未記録ならnull。"
    )
    deferred_resolution_action: DeferredFrontierResolutionAction | None = Field(
        default=None,
        description="relevant_deferred候補について最後に決めた後続処理。未決ならnull。",
    )
    deferred_resolution_reason: str | None = Field(
        default=None,
        description="保留候補の後続処理を選んだ理由。未決ならnull。",
    )


class GraphReviewBatch(FrameworkModel):
    candidates: tuple[GraphReviewCandidate, ...] = Field(
        default=(),
        description=(
            "今回の専用Graph Reviewで意味評価する、同じWorkItem・Hypothesisに"
            "属する未評価差分。"
        ),
    )
    remaining_unreviewed_count: int = Field(
        default=0,
        ge=0,
        description="今回のbatch上限から漏れ、まだ意味評価されていない候補数。",
    )


class SolverToolResult(FrameworkModel):
    """CaseStateのToolResultからLLMに必要な実行状態だけを投影する。"""

    request_id: str = Field(description="結果が対応する既知ToolRequest ID。")
    status: ToolStatus = Field(
        description="succeededは実行完了、failedは失敗、timeoutは時間切れ。意味的な成否ではない。"
    )
    evidence_ids: tuple[str, ...] = Field(
        description="このToolResultが追加したEvidence ID。"
    )
    evidence_count: int = Field(
        ge=0,
        description="このToolResultが追加したEvidence件数。",
    )
    graph_projection_updated: bool = Field(
        description="Graphナビゲーション情報をCase投影へ反映したか。関連性や本文取得済みを意味しない。"
    )
    error_code: str | None = Field(
        description="failedまたはtimeoutの機械的エラーコード。成功時はnull。"
    )
    elapsed_ms: int = Field(ge=0, description="Tool実行に要したミリ秒。")
    cycle_no: int = Field(ge=1, description="Toolを実行したResearch Cycle番号。")


class SearchCandidateArticle(FrameworkModel):
    """OpenSearch候補と、その発見要求を意味選別なしで対応付ける。"""

    article_id: str = Field(description="OpenSearchで発見した候補Article ID。")
    document_id: str | None = Field(description="候補の所属Document ID。なければnull。")
    title: str | None = Field(description="候補Documentの表示名。なければnull。")
    headings: tuple[str, ...] = Field(description="検索結果に含まれた候補Articleの見出し。")
    discovery_work_item_ids: tuple[str, ...] = Field(
        description="この候補を発見した検索要求に紐づくWorkItem ID。意味上の採用先を限定しない。"
    )
    discovery_hypothesis_ids: tuple[str, ...] = Field(
        description="この候補を発見した検索要求に紐づくHypothesis ID。意味上の採用先を限定しない。"
    )
    search_request_ids: tuple[str, ...] = Field(
        description="この候補を発見したlegal_search Request ID。"
    )
    navigation_evidence_ids: tuple[str, ...] = Field(
        description="候補選択にだけ使える検索抜粋Evidence ID。回答根拠には使わない。"
    )
    legal_function: str | None = Field(
        default=None,
        description="前CycleのSearch Assessmentで判断済みの法的機能。",
    )
    assessment_summary: str | None = Field(
        default=None,
        description="前CycleでSolverが作成した候補の意味要約。",
    )
    matched_hypothesis_ids: tuple[str, ...] = Field(
        default=(),
        description="前Cycleで候補本文を確認する価値があると判断されたHypothesis ID。",
    )
    matched_non_work_item_requirements: tuple[str, ...] = Field(
        default=(),
        description="前Cycleで候補本文により満たせると判断された明示要求。",
    )
    @model_validator(mode="before")
    @classmethod
    def discard_legacy_actor_fields(cls, value: object) -> object:
        """本文取得前の主体照合を廃止した旧read modelを読み替える。"""

        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        migrated.pop("regulated_actor_role", None)
        migrated.pop("actor_match_reason", None)
        migrated.pop("actor_matches", None)
        return migrated


class SearchAssessmentExcerpt(FrameworkModel):
    """本文取得候補の内容評価に渡すOpenSearch検索抜粋。"""

    content: str = Field(
        description=(
            "このArticleを発見した検索一致箇所と、候補判断のためProgramが同じ"
            "Articleから付加した限定的な構造文脈。Article全文とは限らず、"
            "回答根拠には使わない。"
        )
    )


class SearchAssessmentCandidate(FrameworkModel):
    """検索結果からArticle単位に作った本文取得候補。"""

    article_id: str = Field(description="評価する本文取得候補のArticle ID。")
    title: str | None = Field(
        description="本文取得候補が属する文書の表示名。取得できない場合はnull。"
    )
    headings: tuple[str, ...] = Field(
        description="検索結果に含まれた本文取得候補Articleの見出し。"
    )
    search_excerpts: tuple[SearchAssessmentExcerpt, ...] = Field(
        description="本文取得候補を発見した検索結果の抜粋。回答根拠には使わない。"
    )


class SearchCandidateContentAssessmentInput(FrameworkModel):
    """Hypothesisの影響を受けず候補自身の内容を読むためのread model。"""

    search_candidates: tuple[SearchAssessmentCandidate, ...] = Field(
        description=(
            "legal_searchの検索結果をArticle単位にまとめた本文取得候補。"
            "見出しと検索抜粋から候補自身の規律だけを評価する。"
        )
    )


class SearchAssessmentWorkItem(FrameworkModel):
    work_item_id: str = Field(description="評価対象の既知WorkItem ID。")
    question: str = Field(description="このWorkItemで確認する1つの法的事項。")


class SearchAssessmentHypothesis(FrameworkModel):
    hypothesis_id: str = Field(description="評価対象の既知Hypothesis ID。")
    work_item_id: str = Field(description="このHypothesisが属する既知WorkItem ID。")
    statement: str = Field(description="法令本文で検証する1つの法的命題。")
    gaps: tuple[str, ...] = Field(
        description=(
            "WorkItemへの回答に必要だが法令本文で未確認の内容。"
            "statementが支持済みでも残ることがある。"
        )
    )


class SearchCandidateInput(FrameworkModel):
    """検索候補と確認対象の対応付けに必要な共通read model。"""

    work_tree: tuple[SearchAssessmentWorkItem, ...] = Field(
        description="本文取得候補と対応付けるWorkItemのIDと確認事項。"
    )
    hypotheses: tuple[SearchAssessmentHypothesis, ...] = Field(
        description="本文取得候補と内容面の対応を評価するHypothesis。"
    )
    search_candidates: tuple[SearchAssessmentCandidate, ...] = Field(
        description=(
            "legal_searchの検索結果をArticle単位にまとめた、"
            "Article本文の取得候補。この時点では本文取得対象として未選択。"
        )
    )


class SearchAssessmentInput(SearchCandidateInput):
    """元質問を含めて検索結果の内容を評価するread model。"""

    question: str = Field(description="利用者が回答を求めている元の質問。")


class SearchSelectionInput(SearchCandidateInput):
    """検索候補の理解と本文取得対象の選択を一度に行うread model。"""

    non_work_item_requirements: tuple[str, ...] = Field(
        description=(
            "質問の明示要求のうち、独立したWorkItemにしなかった回答全体の要件。"
            "根拠の提示等、本文取得候補の選択に関係する場合に考慮する。"
        )
    )
    current_fetch_request_capacity: int = Field(
        ge=0,
        description="今回すべてのWorkItemで選べるArticle数の合計上限。",
    )
    remaining_fetch_capacity_by_work_item: dict[str, int] = Field(
        description=(
            "各WorkItemが現在Cycleで追加取得できるArticle数。選択Articleは、"
            "matched_hypothesis_idsが属するWorkItemの残数内に収める。"
        ),
    )


class ResearchStepWorkItem(FrameworkModel):
    work_item_id: str = Field(description="Programが付与した既知WorkItem ID。")
    question: str = Field(description="このWorkItemで確認する1つの法的事項。")
    action_actor: str | None = Field(
        default=None,
        description="確認事項で規制対象となる行為をする者。未指定ならnull。"
    )


class ResearchStepHypothesis(FrameworkModel):
    hypothesis_id: str = Field(description="Programが付与した既知Hypothesis ID。")
    work_item_id: str = Field(description="このHypothesisが属する既知WorkItem ID。")
    statement: str = Field(
        description=(
            "WorkItemの範囲内で、法令本文により支持又は否定する1つの法的命題。"
        )
    )
    action_actor: str | None = Field(
        default=None,
        description="所属WorkItemで確定した、規制対象となる行為をする者。"
    )
    gaps: tuple[str, ...] = Field(
        description=(
            "WorkItemへの回答に必要だが、法令本文による確認が残る事項。"
            "statementが支持済みでも残ることがある。該当しなければ空。"
        )
    )


class ResearchStepInput(FrameworkModel):
    """初回Researchの各Stepへ必要な項目だけを渡すread model。"""

    question: str = Field(description="利用者が回答を求めている元の質問。")
    answer_options: tuple[AnswerOption, ...] = Field(
        default=(),
        description=(
            "利用者が提示した任意の回答候補。候補の内容は未確認であり、"
            "法令本文による調査対象として扱う。"
        ),
    )
    work_items: tuple[ResearchStepWorkItem, ...] = Field(
        default=(),
        description=(
            "今回の質問から作成済みのWorkItem。各要素は既知IDと1つの確認事項を持つ。"
        ),
    )
    non_work_item_requirements: tuple[str, ...] = Field(
        default=(),
        description=(
            "質問の明示要求のうち、独立した法的結論を要するWorkItemにしなかった要求。"
        ),
    )
    hypotheses: tuple[ResearchStepHypothesis, ...] = Field(
        default=(),
        description=(
            "未判定、または支持済みでもgapsが残るHypothesis。"
            "各要素は所属WorkItem、命題、gapsを持つ。"
        ),
    )
    available_tools: tuple[ToolDefinition, ...] = Field(
        default=(),
        description="現在のStepで要求できるTool一覧。",
    )
    max_tool_requests_per_step: int = Field(
        ge=0,
        description="今回のStepで返せるTool要求総数の上限。",
    )


class SolverContractFeedback(FrameworkModel):
    violation: str = Field(
        description="直前SolverDecisionを未適用にした決定的な契約違反。"
    )
    previous_decision: SolverDecision = Field(
        description="修正対象となる、CaseStateへ未適用の直前SolverDecision。"
    )
    repair_work_item_ids: tuple[str, ...] = Field(
        default=(),
        exclude=True,
        description=(
            "WorkItem別処理で契約違反になり、再実行するWorkItem ID。"
            "空配列ならDecision全体を再実行する。"
        ),
    )


class SolverActionFeedback(FrameworkModel):
    code: Literal["already_completed"] = Field(
        description="実行しなかった行動の理由を表す機械判定コード。"
    )
    message: str = Field(
        description=(
            "直前の行動を実行しなかった理由。法的意味の判断ではなく、"
            "成功済み要求との完全一致などの決定的事実を示す。"
        )
    )
    rejected_tool_requests: tuple[ToolRequest, ...] = Field(
        description=(
            "実行されずCaseStateへ保存されなかった直前のToolRequest。"
            "重複scopeの判定ではrequest_idとpurposeを比較しない。"
        )
    )


class CompletedLegalSearch(FrameworkModel):
    work_item_id: str = Field(
        description="成功済みlegal_searchが対象にした既知WorkItem ID。",
    )
    hypothesis_ids: tuple[str, ...] = Field(
        description="成功済みlegal_searchが検証対象にした既知Hypothesis ID。",
    )
    arguments: dict[str, Any] = Field(
        description=(
            "成功済みlegal_searchへ渡した入力引数。work_item_id、hypothesis_ids、"
            "argumentsが同じscopeの再要求は禁止。request_idとpurposeはscopeに含めない。"
        ),
    )


class CompletedGraphSearch(FrameworkModel):
    work_item_id: str = Field(
        description="成功済みlegal_graph_neighborsが対象にした既知WorkItem ID。",
    )
    hypothesis_ids: tuple[str, ...] = Field(
        description="成功済みlegal_graph_neighborsが検証対象にした既知Hypothesis ID。",
    )
    arguments: dict[str, Any] = Field(
        description=(
            "成功済みlegal_graph_neighborsへ渡した入力引数。候補0件の場合も含み、"
            "work_item_id、hypothesis_ids、argumentsが同じscopeの再要求は禁止。"
            "request_idとpurposeはscopeに含めない。"
        ),
    )
    candidate_article_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "このGraph要求が返した隣接Article ID。Graph関係の意味や本文の"
            "関連性をProgramが判定した値ではない。"
        ),
    )
    new_candidate_article_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "このGraph要求でCaseに初めて現れた隣接Article ID。空配列は新規候補0件を"
            "意味し、法的関係が存在しないことは意味しない。"
        ),
    )


class SolverContext(FrameworkModel):
    case_id: str = Field(description="Programが管理する現在CaseのID。")
    question: str = Field(description="利用者が回答を求めている元の質問。")
    answer_options: tuple[AnswerOption, ...] = Field(
        default=(),
        description=(
            "利用者が提示した任意の回答候補。候補の内容は未確認であり、"
            "正解や採点情報は含まない。"
        ),
    )
    non_work_item_requirements: tuple[str, ...] = Field(
        default=(),
        description=(
            "質問の明示要求のうち、独立した法的結論を要するWorkItemにしなかった要求。"
        ),
    )
    research_cycle_count: int = Field(description="開始済みResearch Cycle数。")
    remaining_research_cycles: int = Field(description="開始可能な残りResearch Cycle数。")
    remaining_wall_time_sec: float = Field(description="Case全体の残り実行秒数。")
    min_next_cycle_budget_sec: float = Field(
        description="次Cycleを安全に開始するためProgramが必要とする最小残り秒数。",
    )
    can_start_next_cycle: bool = Field(
        description="時間とCycle上限から、Programが次Cycle開始を許可できるか。",
    )
    max_tool_requests_per_step: int = Field(
        description="今回のSolverDecisionで返せるToolRequest総数の上限。",
    )
    max_fetched_resources_per_cycle: int = Field(
        description="1 WorkItemが1 Cycleで本文取得できるArticle数の上限。",
    )
    fetched_resource_ids_this_cycle: tuple[str, ...] = Field(
        description="現在の投影範囲で、現在Cycleに本文取得したArticle ID。",
    )
    fetched_resource_ids_by_work_item_this_cycle: dict[str, tuple[str, ...]] = Field(
        default_factory=dict,
        exclude=True,
        description="現在Cycleで本文取得したArticle IDを既知WorkItem ID別に集計した値。",
    )
    fetched_resource_ids_by_work_item: dict[str, tuple[str, ...]] = Field(
        default_factory=dict,
        exclude=True,
        description="現在Caseで本文取得したArticle IDを既知WorkItem ID別に集計した値。",
    )
    remaining_fetch_capacity_by_work_item: dict[str, int] = Field(
        default_factory=dict,
        exclude=True,
        description="各open WorkItemが現在Cycleで追加取得できるArticle数。",
    )
    remaining_fetch_capacity: int = Field(
        ge=0,
        description=(
            "現在の投影範囲でfetch_articlesに追加できるArticle数。"
            "WorkItem専属入力ではそのWorkItemの残数。"
        ),
    )
    max_parallel_work_items: int = Field(
        default=4,
        description="WorkItem専属のLLM処理を同時実行する最大数。",
    )
    max_selected_frontier_per_step: int = Field(
        description="今回のGraph reviewでselectedにできる候補数上限。",
    )
    max_graph_articles_per_hypothesis_per_cycle: int = Field(
        default=3,
        description=(
            "同じHypothesisについて1 Cycleで本文取得できるGraph候補Article数上限。"
        ),
    )

    @property
    def graph_review_selection_limit(self) -> int:
        return min(
            self.max_selected_frontier_per_step,
            self.max_graph_articles_per_hypothesis_per_cycle,
            self.remaining_fetch_capacity,
        )

    cycle_budget_reached: bool = Field(
        description="現在Cycleの決定的な実行上限へ到達したか。",
    )
    cycle_close_required: bool = Field(
        description="現在CycleへToolを追加せず、完了または次Cycle移行を判断すべきか。",
    )
    cycle_step_timeout: bool = Field(
        description="直前stepが時間切れで終了したか。法的不存在や仮説否定を意味しない。",
    )
    max_retained_evidence: int = Field(
        description="後続Cycleへ本文を再表示できるEvidence件数上限。",
    )
    max_material_evidence_chars: int = Field(
        description="今回Promptへ載せるEvidence本文の文字数上限。意味的な採否基準ではない。",
    )
    max_solver_input_chars: int = Field(
        description="Solverへ渡すPrompt全体の安全上限。意味的な採否基準ではない。",
    )
    finalize_only: bool = Field(
        description="追加Toolを使わず、既知根拠から最終回答だけを作る呼出しか。",
    )
    available_tools: tuple[ToolDefinition, ...] = Field(
        default=(),
        description=(
            "現在Solverが要求できる正規Tool名、用途、入力Schema、戻り値説明。"
            "Tool選択はSolver、形式検証と実行はProgramが担当する。"
        ),
    )
    grounding_evidence_ids: tuple[str, ...] = Field(
        description="Hypothesis、DependencyDecision、回答の根拠に使用できる取得済み本文Evidence ID。",
    )
    navigation_evidence_ids: tuple[str, ...] = Field(
        description="候補の所在を示す検索・Graph Evidence ID。意味判断や回答根拠には使わない。",
    )
    fetchable_article_ids: tuple[str, ...] = Field(
        description="発見済みかつ本文未取得で、fetch_articlesに指定できるArticle ID。",
    )
    search_candidates: tuple[SearchCandidateArticle, ...] = Field(
        default=(),
        description=(
            "OpenSearch検索結果をArticle単位にまとめた本文取得候補と、"
            "発見元・検索抜粋の対応。本文取得対象としては未選択。"
        ),
    )
    work_tree: tuple[WorkTreeItem, ...] = Field(
        description="WorkItemの階層、状態、対応HypothesisをProgramが投影した一覧。",
    )
    hypotheses: tuple[Hypothesis, ...] = Field(
        description="現在の全Hypothesisとその判定・gap。",
    )
    evidence_hypothesis_candidates: tuple[
        EvidenceHypothesisCandidate, ...
    ] = Field(
        default=(),
        description=(
            "取得本文のArticleと、本文取得前に対応候補とされたHypothesisの来歴。"
            "Programは既知IDを結合するだけで、支持・反証は判断しない。"
        ),
    )
    focus_work_items: tuple[WorkItem, ...] = Field(
        description="現在stepで優先するopen WorkItem。全作業範囲を置き換えない。",
    )
    affected_work_items: tuple[WorkItem, ...] = Field(
        description="前提Hypothesisの否定により維持・置換・破棄を再判断するWorkItem。",
    )
    used_tool_request_ids: tuple[str, ...] = Field(default=(), exclude=True)
    recent_tool_requests: tuple[ToolRequest, ...] = Field(
        description="現在Cycleで結果が観察済みの直近ToolRequest。",
    )
    recent_tool_results: tuple[SolverToolResult, ...] = Field(
        description="直近Toolの機械的な成功・失敗・timeoutとEvidence件数。",
    )
    completed_legal_searches: tuple[CompletedLegalSearch, ...] = Field(
        default=(),
        description=(
            "過去Cycleを含む成功済みlegal_searchのWorkItem、Hypothesis、入力引数。"
            "同じ3要素をscopeとして再要求しないための履歴。"
        ),
    )
    completed_graph_searches: tuple[CompletedGraphSearch, ...] = Field(
        default=(),
        description=(
            "過去Cycleを含む成功済みlegal_graph_neighborsのWorkItem、Hypothesis、"
            "入力引数。候補0件も履歴に含み、同じ3要素をscopeとして再要求しない。"
        ),
    )
    completed_load_evidence_ids_by_work_item: dict[str, tuple[str, ...]] = Field(
        default_factory=dict,
        exclude=True,
        description=(
            "現在CycleでLLMへの提示まで完了したload_evidenceのEvidence IDを、"
            "WorkItem別に保持する内部投影。同じWorkItemへ同じ本文を再提示しないために使う。"
        ),
    )
    graph_fetch_completed_hypothesis_ids_this_cycle: tuple[str, ...] = Field(
        default=(),
        description=(
            "現在CycleでGraph候補本文の取得・統合を1バッチ完了したHypothesis ID。"
            "同じHypothesisの残候補は次Cycleで扱う。"
        ),
    )
    evidence_manifest: tuple[EvidenceManifestItem, ...] = Field(
        description="Caseで既知のEvidenceと、今回本文が提示されているかの一覧。",
    )
    graph_review_batch: GraphReviewBatch = Field(
        description="今回まだ意味評価すべきGraph候補差分。",
    )
    graph_review_ledger: tuple[GraphReviewLedgerItem, ...] = Field(
        description="過去に評価済みのGraph候補と現在の本文取得状態。",
    )
    required_graph_review_request_ids: tuple[str, ...] = Field(
        default=(),
        description="現在のGraph差分Reviewが処理すべき既知Graph ToolRequest ID。",
    )
    required_search_review_request_ids: tuple[str, ...] = Field(
        default=(),
        description="現在の検索候補Reviewが処理すべき既知legal_search Request ID。",
    )
    material_evidence: tuple[Evidence, ...] = Field(
        description="今回のPromptに本文が実際に含まれるEvidence。本文評価はこの内容だけで行う。",
    )
    hypothesis_revision_evidence: tuple[Evidence, ...] = Field(
        default=(),
        exclude=True,
        description="Cycle境界のHypothesis見直しへ渡す、当Cycle取得済みEvidence。",
    )
    omitted_evidence_ids: tuple[str, ...] = Field(
        description="Caseでは既知だが今回本文を省略したEvidence ID。必要ならload_evidenceで取得する。",
    )
    omitted_evidence_ids_by_work_item: dict[str, tuple[str, ...]] = Field(
        default_factory=dict,
        exclude=True,
        description=(
            "省略Evidenceを発見又は対応付けたWorkItem別に保持する内部投影。"
            "法的関連性を推測せず、Tool要求と既存Hypothesis対応の来歴だけを使う。"
        ),
    )
    required_dependency_kind: str | None = Field(
        default=None,
        description="対象WorkItemで確認する下位規範依存の種類。指定がなければnull。",
    )
    required_dependency_work_item_ids: tuple[str, ...] = Field(
        default=(),
        description="今回DependencyDecisionを必ず返す既知WorkItem ID。",
    )
    dependency_decisions: tuple[DependencyDecision, ...] = Field(
        default=(),
        description="これまでに適用済みの下位規範確認判断。",
    )
    reviewer_findings: tuple[ReviewFinding, ...] = Field(
        default=(),
        description="任意Reviewerから差し戻され、今回処理すべき指摘。",
    )
    contract_feedback: SolverContractFeedback | None = Field(
        default=None,
        description="直前Decisionが未適用になった構造違反と、その未適用Decision。",
    )
    action_feedback: SolverActionFeedback | None = Field(
        default=None,
        description=(
            "直前ToolRequestを実行しなかった決定的理由。契約修復ではなく、"
            "既存結果を評価して次の行動を選ぶための観察情報。"
        ),
    )

    @property
    def material_evidence_ids(self) -> frozenset[str]:
        return frozenset(item.evidence_id for item in self.material_evidence)


class HypothesisRevisionWorkItem(FrameworkModel):
    """反証済みHypothesisが属するWorkItemの最小投影。"""

    work_item_id: str = Field(description="既存の非dropped WorkItemの完全一致ID。")
    question: str = Field(description="このWorkItemが確認する事項。")
    hypothesis_ids: tuple[str, ...] = Field(
        description="このWorkItemへ既に所属するHypothesis IDの一覧。"
    )


class HypothesisRevisionHypothesis(FrameworkModel):
    """当Cycleの取得本文で反証されたHypothesisの最小投影。"""

    hypothesis_id: str = Field(description="既存Hypothesisの完全一致ID。")
    work_item_id: str = Field(description="所属する既存WorkItem ID。")
    statement: str = Field(description="見直し前の現在版statement。")
    judgment: str = Field(description="既存Hypothesisの現在の判定。")
    gaps: tuple[HypothesisGap, ...] = Field(
        description="既存HypothesisのID付き未確認事項。"
    )


class HypothesisRevisionEvidence(FrameworkModel):
    """仮説見直しへ渡す、取得済み本文の最小投影。"""

    evidence_id: str = Field(description="取得済みEvidenceの完全一致ID。")
    content: str = Field(description="Evidenceの取得本文。")
    title: str | None = Field(description="Evidenceの表示名。なければnull。")


class HypothesisRevisionInput(FrameworkModel):
    """Cycle境界の仮説見直しに必要な最小read model。"""

    work_items: tuple[HypothesisRevisionWorkItem, ...] = Field(
        description="反証済みHypothesisが所属する既存WorkItem。"
    )
    hypotheses: tuple[HypothesisRevisionHypothesis, ...] = Field(
        description="現在のCycleで取得した本文により反証された既存Hypothesis。"
    )
    acquired_evidence: tuple[HypothesisRevisionEvidence, ...] = Field(
        description="上記Hypothesisを反証した現在Cycleの本文Evidence。"
    )


def build_solver_context(
    state: CaseState,
    limits: AgentLimits,
    *,
    remaining_wall_time_sec: float,
    finalize_only: bool,
    reviewer_findings: tuple[ReviewFinding, ...] = (),
    contract_feedback: SolverContractFeedback | None = None,
    action_feedback: SolverActionFeedback | None = None,
    required_dependency_kind: str | None = None,
    required_dependency_work_item_ids: tuple[str, ...] = (),
    available_tools: tuple[ToolDefinition, ...] = (),
) -> SolverContext:
    hypotheses_by_work: dict[str, list[Hypothesis]] = {}
    for hypothesis in state.hypotheses:
        hypotheses_by_work.setdefault(hypothesis.work_item_id, []).append(hypothesis)

    evidence_by_id = {item.evidence_id: item for item in state.evidence}
    contradicted_ids = {
        item.hypothesis_id
        for item in state.hypotheses
        if item.judgment == "contradicted"
    }
    directly_affected_ids = {
        item.work_item_id
        for item in state.work_items
        if item.state == "open"
        and contradicted_ids.intersection(item.basis_hypothesis_ids)
    }
    affected_ids = set(directly_affected_ids)
    frontier = list(directly_affected_ids)
    while frontier:
        parent_id = frontier.pop()
        child_ids = {
            item.work_item_id
            for item in state.work_items
            if item.parent_work_item_id == parent_id
        }
        unseen_ids = child_ids - affected_ids
        affected_ids.update(unseen_ids)
        frontier.extend(unseen_ids)
    focus_ids = set(state.focus_work_item_ids)

    work_tree = tuple(
        WorkTreeItem(
            work_item_id=item.work_item_id,
            parent_work_item_id=item.parent_work_item_id,
            question=item.question,
            action_actor=item.action_actor,
            state=item.state,
            resolution=item.resolution,
            basis_hypothesis_ids=item.basis_hypothesis_ids,
            replaces_work_item_id=item.replaces_work_item_id,
            hypothesis_ids=tuple(
                hypothesis.hypothesis_id
                for hypothesis in hypotheses_by_work.get(item.work_item_id, ())
            ),
            evidence_count=len(
                {
                    evidence_id
                    for hypothesis in hypotheses_by_work.get(item.work_item_id, ())
                    for evidence_id in hypothesis.evidence_ids
                }
            ),
        )
        for item in state.work_items
    )

    recent_results = tuple(
        item
        for item in state.tool_results
        if item.cycle_no == state.research_cycle_count
        and item.request_id not in state.integrated_tool_result_request_ids
    )
    recent_request_ids = {item.request_id for item in recent_results}
    recent_requests = tuple(
        item for item in state.tool_requests if item.request_id in recent_request_ids
    )
    recent_requests_by_id = {item.request_id: item for item in recent_requests}
    all_requests_by_id = {item.request_id: item for item in state.tool_requests}
    current_cycle_no = max(1, state.research_cycle_count)
    fetched_by_work_item_this_cycle = fetched_article_ids_by_work_item(
        state,
        cycle_no=current_cycle_no,
    )
    fetched_by_work_item = fetched_article_ids_by_work_item(
        state,
        cycle_no=None,
    )
    carried_search_candidates = _carried_search_candidate_projection(
        state=state,
        requests_by_id=all_requests_by_id,
        evidence_by_id=evidence_by_id,
        fetched_article_ids_by_work_item=fetched_by_work_item,
    )
    carried_search_evidence_ids = tuple(
        evidence_id
        for candidate in carried_search_candidates
        for evidence_id in candidate.navigation_evidence_ids
    )
    new_evidence_ids = _round_robin_result_evidence_ids(recent_results)
    cycle_acquired_evidence = tuple(
        item
        for item in state.evidence
        if item.created_cycle == max(1, state.research_cycle_count)
        and item.metadata.get("citationEligible") is not False
        and item.metadata.get("docType") != "graph_navigation"
    )
    declared_basis_evidence_ids = tuple(
        evidence_id
        for work_item in state.work_items
        if work_item.state != "dropped"
        for hypothesis in hypotheses_by_work.get(work_item.work_item_id, ())
        if hypothesis.hypothesis_id in work_item.basis_hypothesis_ids
        for evidence_id in hypothesis.evidence_ids
    )
    active_hypothesis_evidence_ids = _round_robin_hypothesis_evidence_ids(
        state,
        hypotheses_by_work=hypotheses_by_work,
    )
    active_hypothesis_article_ids = {
        article_id
        for evidence_id in active_hypothesis_evidence_ids
        for evidence in (evidence_by_id.get(evidence_id),)
        if evidence is not None
        for article_id in _evidence_article_ids(evidence)
    }
    active_hypothesis_article_evidence_ids = tuple(
        evidence.evidence_id
        for evidence in state.evidence
        if evidence.metadata.get("citationEligible") is not False
        and any(
            article_id in active_hypothesis_article_ids
            for article_id in _evidence_article_ids(evidence)
        )
    )
    dependency_basis_evidence_ids = tuple(
        evidence_id
        for decision in state.dependency_decisions
        for evidence_id in decision.basis_evidence_ids
    )
    reviewer_basis_evidence_ids = tuple(
        evidence_id
        for finding in reviewer_findings
        for evidence_id in finding.basis_evidence_ids
    )
    material_ids = tuple(
        dict.fromkeys(
            [
                *active_hypothesis_evidence_ids,
                *active_hypothesis_article_evidence_ids,
                *dependency_basis_evidence_ids,
                *declared_basis_evidence_ids,
                *reviewer_basis_evidence_ids,
                *state.retained_evidence_ids,
                *new_evidence_ids,
                *carried_search_evidence_ids,
            ]
        )
    )
    material_items: list[Evidence] = []
    material_chars = 0
    for evidence_id in material_ids:
        evidence = evidence_by_id.get(evidence_id)
        if evidence is None:
            continue
        if evidence.metadata.get("docType") == "graph_navigation":
            # 同じ機械情報を本文枠へ重複掲載せず、探索Graphの正本から差分投影する。
            continue
        evidence_chars = len(evidence.content)
        if evidence_chars > limits.max_material_evidence_chars:
            raise ContextCapacityExceeded(
                "context_capacity_exceeded: single Evidence exceeds "
                "max_material_evidence_chars"
            )
        if material_chars + evidence_chars > limits.max_material_evidence_chars:
            continue
        material_items.append(evidence)
        material_chars += evidence_chars
    material = tuple(material_items)
    included_ids = {item.evidence_id for item in material}
    graph_navigation_ids = frozenset(
        item.evidence_id
        for item in state.evidence
        if item.metadata.get("docType") == "graph_navigation"
    )
    open_work_item_ids = tuple(
        item.work_item_id for item in state.work_items if item.state == "open"
    )
    remaining_by_work_item = {
        work_item_id: max(
            0,
            limits.max_fetched_resources_per_cycle
            - len(fetched_by_work_item_this_cycle.get(work_item_id, ())),
        )
        for work_item_id in open_work_item_ids
    }
    fetched_resource_ids_this_cycle = tuple(
        dict.fromkeys(
            article_id
            for work_item_id in open_work_item_ids
            for article_id in fetched_by_work_item_this_cycle.get(
                work_item_id,
                (),
            )
        )
    )
    remaining_fetch_capacity = (
        sum(remaining_by_work_item.values())
        if open_work_item_ids
        else limits.max_fetched_resources_per_cycle
    )
    time_requires_cycle_close = (
        not finalize_only
        and remaining_wall_time_sec
        <= limits.finalization_reserve_sec + limits.cycle_close_reserve_sec
    )
    graph_candidate_catalog = _graph_candidate_catalog(state)
    completed_graph_units = _completed_graph_review_units_for_cycle(state)
    graph_review_batch, graph_review_ledger = _graph_review_projection(
        state,
        graph_candidate_catalog,
        max_candidates=limits.max_graph_candidates_per_review_batch,
    )
    graph_review_batch = _project_graph_content_status_by_work_item(
        graph_review_batch,
        fetched_by_work_item=fetched_by_work_item,
    )
    graph_review_ledger = tuple(
        item.model_copy(
            update={
                "content_status": (
                    "succeeded"
                    if item.article_id
                    in fetched_by_work_item.get(item.work_item_id or "", ())
                    else (
                        "not_requested"
                        if item.content_status == "succeeded"
                        else item.content_status
                    )
                )
            }
        )
        for item in graph_review_ledger
    )
    graph_work_item_id = next(
        (
            item.work_item_id
            for item in graph_review_batch.candidates
            if item.work_item_id is not None
        ),
        None,
    )
    if graph_work_item_id is not None:
        remaining_fetch_capacity = remaining_by_work_item.get(
            graph_work_item_id,
            0,
        )
    graph_fetch_completed_hypothesis_ids = tuple(
        hypothesis.hypothesis_id
        for hypothesis in state.hypotheses
        if (hypothesis.work_item_id, hypothesis.hypothesis_id)
        in completed_graph_units
    )
    if remaining_fetch_capacity == 0 or time_requires_cycle_close:
        graph_review_batch = GraphReviewBatch(
            remaining_unreviewed_count=(
                graph_review_batch.remaining_unreviewed_count
                + len(graph_review_batch.candidates)
            )
        )
    required_graph_review_request_ids = (
        ()
        if finalize_only
        else tuple(
            dict.fromkeys(
                request_id
                for candidate in graph_review_batch.candidates
                for link in candidate.links
                for request_id in link.graph_request_ids
            )
        )
    )
    grounding_ids = tuple(
        item.evidence_id
        for item in material
        if item.metadata.get("citationEligible") is not False
    )
    navigation_ids = tuple(
        dict.fromkeys(
            [
                *(
                    item.evidence_id
                    for item in material
                    if item.metadata.get("citationEligible") is False
                ),
            ]
        )
    )
    graph_fetchable_article_ids = tuple(
        dict.fromkeys(
            [
                *(
                    item.article_id
                    for item in graph_review_batch.candidates
                    if item.content_status in {"not_requested", "failed", "timeout"}
                ),
                *(
                    item.article_id
                    for item in graph_review_ledger
                    if (
                        item.review_status == "relevant_deferred"
                        and (item.work_item_id, item.hypothesis_id)
                        not in completed_graph_units
                        and item.content_status
                        in {"not_requested", "failed", "timeout"}
                        and item.deferred_resolution_action != "no_longer_needed"
                    )
                    or (
                        item.review_status == "selected"
                        and item.content_status in {"failed", "timeout"}
                    )
                ),
            ]
        )
    )
    candidate_article_ids = tuple(
        dict.fromkeys(
            [
                *(
                    article_id
                    for item in material
                    if item.metadata.get("citationEligible") is False
                    for article_id in _evidence_article_ids(item)
                ),
                *(item.article_id for item in carried_search_candidates),
                *graph_fetchable_article_ids,
            ]
        )
    )
    reviewed_search_request_ids = {
        request_id
        for review in state.search_candidate_reviews
        for request_id in review.search_request_ids
    }
    unreviewed_search_results = tuple(
        result
        for result in recent_results
        if result.request_id not in reviewed_search_request_ids
        and (request := recent_requests_by_id.get(result.request_id)) is not None
        and request.tool_name == "legal_search"
    )
    fresh_search_candidates = _search_candidate_projection(
        recent_results=unreviewed_search_results,
        recent_requests_by_id=recent_requests_by_id,
        evidence_by_id=evidence_by_id,
        fetchable_article_ids=candidate_article_ids,
        fetched_article_ids_by_work_item=fetched_by_work_item,
    )
    fresh_search_request_ids = tuple(
        dict.fromkeys(
            request_id
            for candidate in fresh_search_candidates
            for request_id in candidate.search_request_ids
        )
    )
    # 専用Search Reviewは、新しいlegal_search結果だけを処理する。
    # 前Cycleで評価済みの保留候補は通常のIntegrationへ引き継ぎ、Solverが
    # 既知候補、Graph、再検索を比較して次Cycleの行動を選ぶ。
    required_search_review_request_ids = (
        fresh_search_request_ids
        if not finalize_only
        and fresh_search_request_ids
        else ()
    )
    # 新規候補のReview中は、その候補だけを提示する。Review済み候補は
    # 本文取得が完了するまでCycleをまたいでIntegrationへ引き継ぐ。
    search_candidates = (
        fresh_search_candidates
        if fresh_search_request_ids
        else carried_search_candidates
    )
    passthrough_navigation_article_ids = tuple(
        dict.fromkeys(
            article_id
            for result in recent_results
            if (request := recent_requests_by_id.get(result.request_id)) is not None
            and request.tool_name not in {"legal_search", "legal_graph_neighbors"}
            for evidence_id in result.evidence_ids
            if (evidence := evidence_by_id.get(evidence_id)) is not None
            and evidence.metadata.get("citationEligible") is False
            for article_id in _evidence_article_ids(evidence)
            if article_id not in fetched_by_work_item.get(request.work_item_id, ())
        )
    )
    allowed_fetchable_article_ids = {
        *(item.article_id for item in search_candidates),
        *graph_fetchable_article_ids,
        *passthrough_navigation_article_ids,
    }
    fetchable_article_ids = tuple(
        dict.fromkeys(
            [
                *(
                    article_id
                    for article_id in candidate_article_ids
                    if article_id in allowed_fetchable_article_ids
                ),
                *(item.article_id for item in search_candidates),
                *graph_fetchable_article_ids,
                *passthrough_navigation_article_ids,
            ]
        )
    )
    manifest = tuple(
        EvidenceManifestItem(
            evidence_id=item.evidence_id,
            source_ref=item.source_ref,
            title=item.title,
            content_chars=len(item.content),
            created_cycle=item.created_cycle,
            material_included=item.evidence_id in included_ids,
        )
        for item in state.evidence
        if item.evidence_id not in graph_navigation_ids
    )
    solver_recent_results = tuple(
        _solver_tool_result(
            item,
            request=recent_requests_by_id.get(item.request_id),
            graph_navigation_ids=graph_navigation_ids,
        )
        for item in recent_results
    )
    succeeded_results_by_request = {
        item.request_id: item
        for item in state.tool_results
        if item.status == "succeeded"
    }
    succeeded_request_ids = set(succeeded_results_by_request)
    completed_legal_searches = tuple(
        CompletedLegalSearch(
            work_item_id=request.work_item_id,
            hypothesis_ids=request.hypothesis_ids,
            arguments=request.arguments,
        )
        for request in state.tool_requests
        if request.tool_name == "legal_search"
        and (
            result := succeeded_results_by_request.get(request.request_id)
        )
        is not None
        and tool_request_matches_current_hypotheses(
            state,
            request,
        )
    )
    completed_load_ids_by_work_item: dict[str, list[str]] = {}
    integrated_request_ids = set(state.integrated_tool_result_request_ids)
    current_cycle_succeeded_request_ids = {
        item.request_id
        for item in state.tool_results
        if item.status == "succeeded"
        and item.cycle_no == state.research_cycle_count
        and item.request_id in integrated_request_ids
    }
    for request in state.tool_requests:
        if (
            request.tool_name != "load_evidence"
            or request.request_id not in current_cycle_succeeded_request_ids
        ):
            continue
        evidence_ids = request.arguments.get("evidence_ids")
        if not isinstance(evidence_ids, list):
            continue
        known_ids = completed_load_ids_by_work_item.setdefault(
            request.work_item_id,
            [],
        )
        known_ids.extend(
            evidence_id
            for evidence_id in evidence_ids
            if isinstance(evidence_id, str) and evidence_id not in known_ids
        )
    requests_by_id = {item.request_id: item for item in state.tool_requests}
    evidence_by_id = {item.evidence_id: item for item in state.evidence}
    known_article_ids: set[str] = set()
    result_evidence_ids = {
        evidence_id
        for result in state.tool_results
        for evidence_id in result.evidence_ids
    }
    for evidence in state.evidence:
        if evidence.evidence_id in result_evidence_ids:
            continue
        article_id = _nonempty_string(evidence.metadata.get("articleId"))
        if article_id is not None:
            known_article_ids.add(article_id)
        if evidence.metadata.get("docType") != "graph_navigation":
            continue
        payload = _graph_navigation_payload(evidence)
        for graph_article_id in (
            _nonempty_string(
                payload.get("seedArticleId")
                or evidence.metadata.get("seedArticleId")
            ),
            _nonempty_string(
                payload.get("neighborArticleId")
                or evidence.metadata.get("neighborArticleId")
            ),
        ):
            if graph_article_id is not None:
                known_article_ids.add(graph_article_id)
    graph_candidates_by_request: dict[str, tuple[str, ...]] = {}
    new_graph_candidates_by_request: dict[str, tuple[str, ...]] = {}
    for result in state.tool_results:
        if result.status != "succeeded":
            continue
        request = requests_by_id.get(result.request_id)
        result_evidence = tuple(
            evidence_by_id[evidence_id]
            for evidence_id in result.evidence_ids
            if evidence_id in evidence_by_id
        )
        graph_candidate_ids: list[str] = []
        result_article_ids: list[str] = []
        for evidence in result_evidence:
            article_id = _nonempty_string(evidence.metadata.get("articleId"))
            if article_id is not None and article_id not in result_article_ids:
                result_article_ids.append(article_id)
            if evidence.metadata.get("docType") != "graph_navigation":
                continue
            payload = _graph_navigation_payload(evidence)
            seed_article_id = _nonempty_string(
                payload.get("seedArticleId")
                or evidence.metadata.get("seedArticleId")
            )
            neighbor_article_id = _nonempty_string(
                payload.get("neighborArticleId")
                or evidence.metadata.get("neighborArticleId")
            )
            if seed_article_id is not None:
                known_article_ids.add(seed_article_id)
            if (
                neighbor_article_id is not None
                and neighbor_article_id not in graph_candidate_ids
            ):
                graph_candidate_ids.append(neighbor_article_id)
        if request is not None and request.tool_name == "legal_graph_neighbors":
            graph_candidates_by_request[result.request_id] = tuple(
                graph_candidate_ids
            )
            new_graph_candidates_by_request[result.request_id] = tuple(
                article_id
                for article_id in graph_candidate_ids
                if article_id not in known_article_ids
            )
        known_article_ids.update(result_article_ids)
        known_article_ids.update(graph_candidate_ids)

    completed_graph_searches = tuple(
        CompletedGraphSearch(
            work_item_id=request.work_item_id,
            hypothesis_ids=request.hypothesis_ids,
            arguments=request.arguments,
            candidate_article_ids=graph_candidates_by_request.get(
                request.request_id,
                (),
            ),
            new_candidate_article_ids=new_graph_candidates_by_request.get(
                request.request_id,
                (),
            ),
        )
        for request in state.tool_requests
        if request.tool_name == "legal_graph_neighbors"
        and (
            result := succeeded_results_by_request.get(request.request_id)
        )
        is not None
        and tool_request_matches_current_hypotheses(
            state,
            request,
        )
    )
    evidence_hypothesis_candidates = _evidence_hypothesis_candidates(
        state,
        material,
    )

    omitted_evidence_ids = tuple(
        item.evidence_id
        for item in state.evidence
        if item.evidence_id not in included_ids
        and item.evidence_id not in graph_navigation_ids
        and item.metadata.get("citationEligible") is not False
    )

    return SolverContext(
        case_id=state.case_id,
        question=state.question,
        answer_options=state.answer_options,
        non_work_item_requirements=state.non_work_item_requirements,
        research_cycle_count=state.research_cycle_count,
        remaining_research_cycles=max(
            0, limits.max_research_cycles - state.research_cycle_count
        ),
        remaining_wall_time_sec=max(0.0, remaining_wall_time_sec),
        min_next_cycle_budget_sec=limits.min_next_cycle_budget_sec,
        can_start_next_cycle=(
            not finalize_only
            and state.research_cycle_count < limits.max_research_cycles
            and remaining_wall_time_sec
            > limits.finalization_reserve_sec + limits.min_next_cycle_budget_sec
        ),
        max_tool_requests_per_step=limits.max_tool_requests_per_step,
        max_fetched_resources_per_cycle=limits.max_fetched_resources_per_cycle,
        fetched_resource_ids_this_cycle=fetched_resource_ids_this_cycle,
        fetched_resource_ids_by_work_item_this_cycle=(
            fetched_by_work_item_this_cycle
        ),
        fetched_resource_ids_by_work_item=fetched_by_work_item,
        remaining_fetch_capacity_by_work_item=remaining_by_work_item,
        remaining_fetch_capacity=remaining_fetch_capacity,
        max_parallel_work_items=limits.max_parallel_work_items,
        max_selected_frontier_per_step=limits.max_selected_frontier_per_step,
        max_graph_articles_per_hypothesis_per_cycle=(
            limits.max_graph_articles_per_hypothesis_per_cycle
        ),
        cycle_budget_reached=(
            bool(open_work_item_ids)
            and all(value == 0 for value in remaining_by_work_item.values())
        ),
        cycle_close_required=(
            (
                bool(open_work_item_ids)
                and all(value == 0 for value in remaining_by_work_item.values())
            )
            or time_requires_cycle_close
            or state.cycle_step_timeout
        ),
        cycle_step_timeout=state.cycle_step_timeout,
        max_retained_evidence=limits.max_retained_evidence,
        max_material_evidence_chars=limits.max_material_evidence_chars,
        max_solver_input_chars=limits.max_solver_input_chars,
        finalize_only=finalize_only,
        available_tools=available_tools,
        grounding_evidence_ids=grounding_ids,
        navigation_evidence_ids=navigation_ids,
        fetchable_article_ids=fetchable_article_ids,
        search_candidates=search_candidates,
        work_tree=work_tree,
        hypotheses=state.hypotheses,
        evidence_hypothesis_candidates=evidence_hypothesis_candidates,
        focus_work_items=tuple(
            item for item in state.work_items if item.work_item_id in focus_ids
        ),
        affected_work_items=tuple(
            item for item in state.work_items if item.work_item_id in affected_ids
        ),
        used_tool_request_ids=tuple(item.request_id for item in state.tool_requests),
        recent_tool_requests=recent_requests,
        recent_tool_results=solver_recent_results,
        completed_legal_searches=completed_legal_searches,
        completed_graph_searches=completed_graph_searches,
        completed_load_evidence_ids_by_work_item={
            work_item_id: tuple(evidence_ids)
            for work_item_id, evidence_ids in completed_load_ids_by_work_item.items()
        },
        graph_fetch_completed_hypothesis_ids_this_cycle=(
            graph_fetch_completed_hypothesis_ids
        ),
        evidence_manifest=manifest,
        graph_review_batch=graph_review_batch,
        graph_review_ledger=graph_review_ledger,
        required_graph_review_request_ids=required_graph_review_request_ids,
        required_search_review_request_ids=required_search_review_request_ids,
        material_evidence=material,
        hypothesis_revision_evidence=cycle_acquired_evidence,
        omitted_evidence_ids=omitted_evidence_ids,
        omitted_evidence_ids_by_work_item=(
            _omitted_evidence_ids_by_work_item(
                state,
                omitted_evidence_ids=omitted_evidence_ids,
            )
        ),
        required_dependency_kind=required_dependency_kind,
        required_dependency_work_item_ids=required_dependency_work_item_ids,
        dependency_decisions=state.dependency_decisions,
        reviewer_findings=reviewer_findings,
        contract_feedback=contract_feedback,
        action_feedback=action_feedback,
    )


def pending_candidate_review_work_item_ids(
    context: SolverContext,
) -> frozenset[str]:
    """未評価の検索・Graph候補を持つWorkItem IDを機械的に返す。"""

    pending_search_request_ids = set(
        context.required_search_review_request_ids
    )
    hypothesis_work_item_ids = {
        item.hypothesis_id: item.work_item_id for item in context.hypotheses
    }
    work_item_ids: set[str] = set()
    for candidate in context.search_candidates:
        if pending_search_request_ids.isdisjoint(candidate.search_request_ids):
            continue
        work_item_ids.update(candidate.discovery_work_item_ids)
        work_item_ids.update(
            work_item_id
            for hypothesis_id in candidate.discovery_hypothesis_ids
            if (
                work_item_id := hypothesis_work_item_ids.get(hypothesis_id)
            )
            is not None
        )

    pending_graph_request_ids = set(
        context.required_graph_review_request_ids
    )
    work_item_ids.update(
        candidate.work_item_id
        for candidate in context.graph_review_batch.candidates
        if any(
            not pending_graph_request_ids.isdisjoint(link.graph_request_ids)
            for link in candidate.links
        )
    )
    return frozenset(work_item_ids)


def _omitted_evidence_ids_by_work_item(
    state: CaseState,
    *,
    omitted_evidence_ids: tuple[str, ...],
) -> dict[str, tuple[str, ...]]:
    """省略Evidenceを、既存の発見・対応来歴だけでWorkItemへ割り当てる。"""

    hypothesis_work_item = {
        item.hypothesis_id: item.work_item_id for item in state.hypotheses
    }
    owners_by_evidence: dict[str, set[str]] = {}

    def add_owner(evidence_id: str, work_item_id: str | None) -> None:
        if work_item_id is None:
            return
        owners_by_evidence.setdefault(evidence_id, set()).add(work_item_id)

    for hypothesis in state.hypotheses:
        for evidence_id in hypothesis.evidence_ids:
            add_owner(evidence_id, hypothesis.work_item_id)

    requests_by_id = {item.request_id: item for item in state.tool_requests}
    for result in state.tool_results:
        request = requests_by_id.get(result.request_id)
        if request is None:
            continue
        for evidence_id in result.evidence_ids:
            add_owner(evidence_id, request.work_item_id)

    article_work_items: dict[str, set[str]] = {}
    for review in state.search_candidate_reviews:
        for assessment in review.assessments:
            for hypothesis_id in assessment.matched_hypothesis_ids:
                work_item_id = hypothesis_work_item.get(hypothesis_id)
                if work_item_id is not None:
                    article_work_items.setdefault(
                        assessment.article_id,
                        set(),
                    ).add(work_item_id)
    for review in state.graph_candidate_reviews:
        for decision in review.frontier_decisions:
            work_item_id = (
                hypothesis_work_item.get(decision.hypothesis_id)
                if decision.hypothesis_id is not None
                else decision.work_item_id
            )
            if work_item_id is not None:
                article_work_items.setdefault(decision.article_id, set()).add(
                    work_item_id
                )

    evidence_by_id = {item.evidence_id: item for item in state.evidence}
    for evidence_id in omitted_evidence_ids:
        evidence = evidence_by_id.get(evidence_id)
        if evidence is None:
            continue
        for article_id in _evidence_article_ids(evidence):
            for work_item_id in article_work_items.get(article_id, ()):
                add_owner(evidence_id, work_item_id)

    return {
        work_item.work_item_id: tuple(
            evidence_id
            for evidence_id in omitted_evidence_ids
            if work_item.work_item_id in owners_by_evidence.get(evidence_id, ())
        )
        for work_item in state.work_items
    }


def _evidence_hypothesis_candidates(
    state: CaseState,
    material_evidence: tuple[Evidence, ...],
) -> tuple[EvidenceHypothesisCandidate, ...]:
    """本文取得前にSolverが選んだArticleとHypothesisの対応を再投影する。"""

    material_article_ids = {
        article_id
        for evidence in material_evidence
        for article_id in _evidence_article_ids(evidence)
    }
    candidates: dict[str, dict[str, Any]] = {}
    assessment_summary_by_article = {
        assessment.article_id: assessment.summary
        for review in state.search_candidate_reviews
        for assessment in review.assessments
    }

    def merge(
        article_id: str,
        hypothesis_ids: tuple[str, ...],
        reason: str,
    ) -> None:
        if article_id not in material_article_ids or not hypothesis_ids:
            return
        candidate = candidates.setdefault(
            article_id,
            {
                "hypothesis_ids": [],
                "reason": reason,
                "assessment_summary": assessment_summary_by_article.get(
                    article_id
                ),
            },
        )
        for hypothesis_id in hypothesis_ids:
            if hypothesis_id not in candidate["hypothesis_ids"]:
                candidate["hypothesis_ids"].append(hypothesis_id)

    for review in state.search_candidate_reviews:
        for selection in review.selections:
            merge(
                selection.article_id,
                selection.matched_hypothesis_ids,
                selection.reason,
            )

    for review in state.graph_candidate_reviews:
        for decision in review.frontier_decisions:
            if decision.action != "select" or decision.hypothesis_id is None:
                continue
            merge(
                decision.article_id,
                (decision.hypothesis_id,),
                decision.reason,
            )

    succeeded_request_ids = {
        result.request_id
        for result in state.tool_results
        if result.status == "succeeded"
    }
    for request in state.tool_requests:
        if (
            request.tool_name != "fetch_articles"
            or request.request_id not in succeeded_request_ids
            or not request.hypothesis_ids
        ):
            continue
        article_ids = request.arguments.get("article_ids")
        if not isinstance(article_ids, list):
            continue
        for article_id in article_ids:
            if not isinstance(article_id, str) or article_id in candidates:
                continue
            merge(article_id, request.hypothesis_ids, request.purpose)

    return tuple(
        EvidenceHypothesisCandidate(
            article_id=article_id,
            hypothesis_ids=tuple(item["hypothesis_ids"]),
            reason=item["reason"],
            assessment_summary=item["assessment_summary"],
        )
        for article_id, item in candidates.items()
    )


class ContextCapacityExceeded(ValueError):
    """候補を欠落させずにSolver入力へ収められない。"""


def _search_candidate_projection(
    *,
    recent_results: tuple[ToolResult, ...],
    recent_requests_by_id: dict[str, ToolRequest],
    evidence_by_id: dict[str, Evidence],
    fetchable_article_ids: tuple[str, ...],
    fetched_article_ids_by_work_item: dict[str, tuple[str, ...]] | None = None,
    assessment_by_article: dict[str, Any] | None = None,
) -> tuple[SearchCandidateArticle, ...]:
    """検索要求と候補Articleの既存参照だけをArticle単位にまとめる。"""

    fetchable_ids = set(fetchable_article_ids)
    fetched_by_work_item = fetched_article_ids_by_work_item or {}
    candidates: dict[str, dict[str, Any]] = {}
    for result in recent_results:
        request = recent_requests_by_id.get(result.request_id)
        if request is None or request.tool_name != "legal_search":
            continue
        for evidence_id in result.evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                continue
            for article_id in _evidence_article_ids(evidence):
                if article_id not in fetchable_ids:
                    continue
                if article_id in fetched_by_work_item.get(request.work_item_id, ()):
                    continue
                candidate = candidates.setdefault(
                    article_id,
                    {
                        "document_id": _nonempty_string(
                            evidence.metadata.get("documentId")
                        ),
                        "title": evidence.title,
                        "headings": [],
                        "discovery_work_item_ids": [],
                        "discovery_hypothesis_ids": [],
                        "search_request_ids": [],
                        "navigation_evidence_ids": [],
                    },
                )
                heading = _nonempty_string(evidence.metadata.get("heading"))
                if heading is not None:
                    _extend_unique(candidate["headings"], (heading,))
                _extend_unique(
                    candidate["discovery_work_item_ids"],
                    (request.work_item_id,),
                )
                _extend_unique(
                    candidate["discovery_hypothesis_ids"],
                    request.hypothesis_ids,
                )
                _extend_unique(
                    candidate["search_request_ids"],
                    (request.request_id,),
                )
                _extend_unique(
                    candidate["navigation_evidence_ids"],
                    (evidence_id,),
                )

    assessment_by_article = assessment_by_article or {}
    return tuple(
        SearchCandidateArticle(
            article_id=article_id,
            document_id=item["document_id"],
            title=item["title"],
            headings=tuple(item["headings"]),
            discovery_work_item_ids=tuple(item["discovery_work_item_ids"]),
            discovery_hypothesis_ids=tuple(
                item["discovery_hypothesis_ids"]
            ),
            search_request_ids=tuple(item["search_request_ids"]),
            navigation_evidence_ids=tuple(item["navigation_evidence_ids"]),
            legal_function=(
                assessment_by_article[article_id].legal_function
                if article_id in assessment_by_article
                else None
            ),
            assessment_summary=(
                assessment_by_article[article_id].summary
                if article_id in assessment_by_article
                else None
            ),
            matched_hypothesis_ids=(
                assessment_by_article[article_id].matched_hypothesis_ids
                if article_id in assessment_by_article
                else ()
            ),
            matched_non_work_item_requirements=(
                assessment_by_article[
                    article_id
                ].matched_non_work_item_requirements
                if article_id in assessment_by_article
                else ()
            ),
        )
        for article_id, item in candidates.items()
    )


def _carried_search_candidate_projection(
    *,
    state: CaseState,
    requests_by_id: dict[str, ToolRequest],
    evidence_by_id: dict[str, Evidence],
    fetched_article_ids_by_work_item: dict[str, tuple[str, ...]],
) -> tuple[SearchCandidateArticle, ...]:
    """Review済みで本文未取得の検索候補をCase履歴から再投影する。"""

    reviewed_request_ids = tuple(
        dict.fromkeys(
            request_id
            for review in state.search_candidate_reviews
            for request_id in review.search_request_ids
        )
    )
    if not reviewed_request_ids:
        return ()

    carried_article_ids = tuple(
        dict.fromkeys(
            article_id
            for review in state.search_candidate_reviews
            for article_id in (
                *review.selected_article_ids,
                *review.deferred_article_ids,
            )
        )
    )
    if not carried_article_ids:
        return ()

    reviewed_request_id_set = set(reviewed_request_ids)
    reviewed_results = tuple(
        result
        for result in state.tool_results
        if result.request_id in reviewed_request_id_set
    )
    assessment_by_article = {
        assessment.article_id: assessment
        for review in state.search_candidate_reviews
        for assessment in review.assessments
    }
    return _search_candidate_projection(
        recent_results=reviewed_results,
        recent_requests_by_id=requests_by_id,
        evidence_by_id=evidence_by_id,
        fetchable_article_ids=carried_article_ids,
        fetched_article_ids_by_work_item=fetched_article_ids_by_work_item,
        assessment_by_article=assessment_by_article,
    )


def _project_graph_content_status_by_work_item(
    batch: GraphReviewBatch,
    *,
    fetched_by_work_item: dict[str, tuple[str, ...]],
) -> GraphReviewBatch:
    """Graph候補の本文取得状態を、候補のWorkItemから決定的に投影する。"""

    return batch.model_copy(
        update={
            "candidates": tuple(
                item.model_copy(
                    update={
                        "content_status": (
                            "succeeded"
                            if item.work_item_id is not None
                            and item.article_id
                            in fetched_by_work_item.get(item.work_item_id, ())
                            else (
                                "not_requested"
                                if item.content_status == "succeeded"
                                else item.content_status
                            )
                        )
                    }
                )
                for item in batch.candidates
            )
        }
    )


_GRAPH_RELATION_FIELDS = (
    "kind",
    "edgeType",
    "direction",
    "status",
    "referenceKind",
    "basisEdgeId",
    "classificationRunId",
    "subjectArticleId",
    "objectArticleId",
    "subjectSupportingSpanId",
    "objectSupportingSpanId",
    "subjectSupportingQuote",
    "objectSupportingQuote",
    "relationExplanation",
)


def _graph_candidate_catalog(
    state: CaseState,
) -> GraphCandidateCatalog:
    requests_by_id = {item.request_id: item for item in state.tool_requests}
    hypothesis_work_item_ids = {
        item.hypothesis_id: item.work_item_id for item in state.hypotheses
    }
    work_ids_by_evidence: dict[str, list[str]] = {}
    hypothesis_ids_by_evidence: dict[str, list[str]] = {}
    request_ids_by_evidence: dict[str, list[str]] = {}
    for result in state.tool_results:
        request = requests_by_id.get(result.request_id)
        if request is None or request.tool_name != "legal_graph_neighbors":
            continue
        for evidence_id in result.evidence_ids:
            request_ids = request_ids_by_evidence.setdefault(evidence_id, [])
            if request.request_id not in request_ids:
                request_ids.append(request.request_id)
            work_ids = work_ids_by_evidence.setdefault(evidence_id, [])
            if request.work_item_id not in work_ids:
                work_ids.append(request.work_item_id)
            hypothesis_ids = hypothesis_ids_by_evidence.setdefault(evidence_id, [])
            for hypothesis_id in request.hypothesis_ids:
                if hypothesis_id not in hypothesis_ids:
                    hypothesis_ids.append(hypothesis_id)
                hypothesis_work_item_id = hypothesis_work_item_ids.get(hypothesis_id)
                if (
                    hypothesis_work_item_id is not None
                    and hypothesis_work_item_id not in work_ids
                ):
                    work_ids.append(hypothesis_work_item_id)

    content_status_by_article = _article_content_statuses(state)
    articles: dict[str, dict[str, Any]] = {}
    links: dict[tuple[str, str, str], dict[str, Any]] = {}
    for evidence in state.evidence:
        if evidence.metadata.get("docType") != "graph_navigation":
            continue
        payload = _graph_navigation_payload(evidence)
        seed_article_id = _nonempty_string(
            payload.get("seedArticleId")
            or evidence.metadata.get("seedArticleId")
            or evidence.metadata.get("fromArticleId")
        )
        neighbor_article_id = _nonempty_string(
            payload.get("neighborArticleId")
            or evidence.metadata.get("neighborArticleId")
            or evidence.metadata.get("toArticleId")
        )
        if seed_article_id is None or neighbor_article_id is None:
            continue
        _merge_graph_article(
            articles,
            article_id=seed_article_id,
            document_id=_nonempty_string(
                payload.get("seedDocumentId")
                or evidence.metadata.get("seedDocumentId")
            ),
            title=_nonempty_string(
                payload.get("seedTitle") or evidence.metadata.get("seedTitle")
            ),
            heading=_nonempty_string(
                payload.get("seedHeading") or evidence.metadata.get("seedHeading")
            ),
            content_status=content_status_by_article.get(
                seed_article_id,
                "not_requested",
            ),
        )
        _merge_graph_article(
            articles,
            article_id=neighbor_article_id,
            document_id=_nonempty_string(
                payload.get("neighborDocumentId")
                or evidence.metadata.get("neighborDocumentId")
            ),
            title=_nonempty_string(
                payload.get("neighborTitle")
                or evidence.metadata.get("neighborTitle")
            ),
            heading=_nonempty_string(
                payload.get("neighborHeading")
                or evidence.metadata.get("neighborHeading")
            ),
            content_status=content_status_by_article.get(
                neighbor_article_id,
                "not_requested",
            ),
        )
        raw_relations = payload.get("relations")
        projected_relations = tuple(
            {
                key: relation[key]
                for key in _GRAPH_RELATION_FIELDS
                if key in relation and relation[key] is not None
            }
            for relation in (
                raw_relations if isinstance(raw_relations, list) else []
            )
            if isinstance(relation, dict)
        )
        link_key = (evidence.evidence_id, seed_article_id, neighbor_article_id)
        link = links.setdefault(
            link_key,
            {
                "link_id": _stable_id(
                    "graph-link",
                    evidence.evidence_id,
                    seed_article_id,
                    neighbor_article_id,
                ),
                "work_item_ids": [],
                "hypothesis_ids": [],
                "relations": [],
                "graph_request_ids": [],
            },
        )
        _extend_unique(
            link["work_item_ids"],
            work_ids_by_evidence.get(evidence.evidence_id, ()),
        )
        _extend_unique(
            link["hypothesis_ids"],
            hypothesis_ids_by_evidence.get(evidence.evidence_id, ()),
        )
        _extend_unique(
            link["graph_request_ids"],
            request_ids_by_evidence.get(evidence.evidence_id, ()),
        )
        for relation in projected_relations:
            if relation not in link["relations"]:
                link["relations"].append(relation)

    return GraphCandidateCatalog(
        articles=tuple(
            GraphCandidateArticle(
                article_id=article_id,
                document_id=item["document_id"],
                title=item["title"],
                heading=item["heading"],
                content_status=item["content_status"],
            )
            for article_id, item in articles.items()
        ),
        links=tuple(
            GraphCandidateLink(
                link_id=item["link_id"],
                seed_article_id=seed_article_id,
                candidate_article_id=candidate_article_id,
                work_item_ids=tuple(item["work_item_ids"]),
                hypothesis_ids=tuple(item["hypothesis_ids"]),
                relations=tuple(item["relations"]),
                graph_request_ids=tuple(item["graph_request_ids"]),
            )
            for (_, seed_article_id, candidate_article_id), item in links.items()
        ),
    )


def _graph_review_projection(
    state: CaseState,
    catalog: GraphCandidateCatalog,
    *,
    max_candidates: int,
) -> tuple[GraphReviewBatch, tuple[GraphReviewLedgerItem, ...]]:
    """全履歴から、Hypothesis単位の差分batchと短い最新台帳を作る。"""

    articles_by_id = {item.article_id: item for item in catalog.articles}
    hypothesis_work_ids = {
        item.hypothesis_id: item.work_item_id for item in state.hypotheses
    }
    open_work_ids = {
        item.work_item_id for item in state.work_items if item.state == "open"
    }
    latest_decision_by_frontier = {}
    latest_cycle_by_frontier: dict[str, int | None] = {}
    reviewed_link_ids_by_frontier: dict[str, set[str]] = {}
    for review in state.graph_candidate_reviews:
        for decision in review.frontier_decisions:
            latest_decision_by_frontier[decision.frontier_item_id] = decision
            latest_cycle_by_frontier[decision.frontier_item_id] = review.reviewed_cycle
            reviewed_link_ids_by_frontier.setdefault(
                decision.frontier_item_id,
                set(),
            ).update(review.reviewed_link_ids)

    deferred_resolution_by_frontier = {
        item.frontier_item_id: item
        for item in state.deferred_frontier_resolutions
    }

    re_adoption_keys = {
        (item.article_id, item.work_item_id, item.hypothesis_id)
        for item in state.frontier_re_adoptions
    }
    frontier_records: dict[str, dict[str, Any]] = {}
    for link in catalog.links:
        pairs: list[tuple[str, str | None]] = []
        if link.hypothesis_ids:
            for hypothesis_id in link.hypothesis_ids:
                work_item_id = hypothesis_work_ids.get(hypothesis_id)
                if work_item_id in open_work_ids:
                    pairs.append((work_item_id, hypothesis_id))
        if not pairs:
            pairs.extend(
                (work_item_id, None)
                for work_item_id in link.work_item_ids
                if work_item_id in open_work_ids
            )
        for work_item_id, hypothesis_id in dict.fromkeys(pairs):
            frontier_id = _frontier_id(
                link.candidate_article_id,
                work_item_id,
                hypothesis_id,
            )
            record = frontier_records.setdefault(
                frontier_id,
                {
                    "article_id": link.candidate_article_id,
                    "work_item_id": work_item_id,
                    "hypothesis_id": hypothesis_id,
                    "links": [],
                },
            )
            if link not in record["links"]:
                record["links"].append(link)

    for article_id, work_item_id, hypothesis_id in re_adoption_keys:
        if work_item_id not in open_work_ids:
            continue
        frontier_id = _frontier_id(article_id, work_item_id, hypothesis_id)
        record = frontier_records.setdefault(
            frontier_id,
            {
                "article_id": article_id,
                "work_item_id": work_item_id,
                "hypothesis_id": hypothesis_id,
                "links": [],
            },
        )
        for link in catalog.links:
            if link.candidate_article_id == article_id and link not in record["links"]:
                record["links"].append(link)

    pending: list[GraphReviewCandidate] = []
    # Stable hash ID is identity only. Paging by that hash would make an
    # unrelated digest determine which discovered candidate the Solver sees.
    # Preserve the Tool/catalog discovery order without adding semantic ranking.
    for frontier_id, record in frontier_records.items():
        article = articles_by_id.get(record["article_id"])
        if article is None:
            continue
        prior = latest_decision_by_frontier.get(frontier_id)
        current_link_ids = {item.link_id for item in record["links"]}
        reviewed_link_ids = reviewed_link_ids_by_frontier.get(frontier_id, set())
        if prior is None:
            trigger: Literal["new_frontier", "re_adopted", "new_link"] = (
                "re_adopted"
                if (
                    record["article_id"],
                    record["work_item_id"],
                    record["hypothesis_id"],
                )
                in re_adoption_keys
                else "new_frontier"
            )
        elif current_link_ids - reviewed_link_ids:
            trigger = "new_link"
        else:
            continue
        pending.append(
            GraphReviewCandidate(
                frontier_item_id=frontier_id,
                article_id=article.article_id,
                document_id=article.document_id,
                title=article.title,
                heading=article.heading,
                work_item_id=record["work_item_id"],
                hypothesis_id=record["hypothesis_id"],
                review_trigger=trigger,
                prior_review_status=(
                    _frontier_status(prior.action) if prior is not None else None
                ),
                content_status=article.content_status,
                links=tuple(record["links"]),
            )
        )

    completed_units = _completed_graph_review_units_for_cycle(state)
    eligible_pending = tuple(
        item
        for item in pending
        if (item.work_item_id, item.hypothesis_id) not in completed_units
    )
    active_unit = (
        (
            eligible_pending[0].work_item_id,
            eligible_pending[0].hypothesis_id,
        )
        if eligible_pending
        else None
    )
    batch_candidates = tuple(
        item
        for item in eligible_pending
        if (item.work_item_id, item.hypothesis_id) == active_unit
    )[:max_candidates]
    ledger = tuple(
        GraphReviewLedgerItem(
            frontier_item_id=frontier_id,
            article_id=decision.article_id,
            title=(
                articles_by_id[decision.article_id].title
                if decision.article_id in articles_by_id
                else None
            ),
            heading=(
                articles_by_id[decision.article_id].heading
                if decision.article_id in articles_by_id
                else None
            ),
            work_item_id=decision.work_item_id,
            hypothesis_id=decision.hypothesis_id,
            review_status=_frontier_status(decision.action),
            reason=_short_text(decision.reason, 240),
            content_status=(
                articles_by_id[decision.article_id].content_status
                if decision.article_id in articles_by_id
                else "not_requested"
            ),
            last_reviewed_cycle=latest_cycle_by_frontier.get(frontier_id),
            deferred_resolution_action=(
                deferred_resolution_by_frontier[frontier_id].action
                if frontier_id in deferred_resolution_by_frontier
                else None
            ),
            deferred_resolution_reason=(
                _short_text(
                    deferred_resolution_by_frontier[frontier_id].reason,
                    240,
                )
                if frontier_id in deferred_resolution_by_frontier
                else None
            ),
        )
        for frontier_id, decision in sorted(latest_decision_by_frontier.items())
    )
    return (
        GraphReviewBatch(
            candidates=batch_candidates,
            remaining_unreviewed_count=max(0, len(pending) - len(batch_candidates)),
        ),
        ledger,
    )


def _completed_graph_review_units_for_cycle(
    state: CaseState,
) -> frozenset[tuple[str, str | None]]:
    """現在CycleでGraph由来本文の取得に成功した探索単位を返す。"""

    requests_by_id = {item.request_id: item for item in state.tool_requests}
    completed: set[tuple[str, str | None]] = set()
    for result in state.tool_results:
        request = requests_by_id.get(result.request_id)
        if (
            result.cycle_no != state.research_cycle_count
            or result.status != "succeeded"
            or request is None
            or not request.request_id.startswith(GRAPH_REVIEW_FETCH_REQUEST_PREFIX)
        ):
            continue
        if request.hypothesis_ids:
            completed.update(
                (request.work_item_id, hypothesis_id)
                for hypothesis_id in request.hypothesis_ids
            )
        else:
            completed.add((request.work_item_id, None))
    return frozenset(completed)


def _frontier_status(action: str) -> Literal[
    "selected", "relevant_deferred", "rejected"
]:
    return {
        "select": "selected",
        "defer": "relevant_deferred",
        "reject": "rejected",
    }[action]


def _frontier_id(
    article_id: str,
    work_item_id: str,
    hypothesis_id: str | None,
) -> str:
    return _stable_id(
        "graph-frontier",
        article_id,
        work_item_id,
        hypothesis_id or "no-hypothesis",
    )


def _stable_id(prefix: str, *parts: str) -> str:
    payload = "\x1f".join(parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def _short_text(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return f"{value[: max_chars - 1]}…"


def _fetched_article_ids_for_cycle(
    state: CaseState,
    cycle_no: int | None,
) -> tuple[str, ...]:
    requests_by_id = {item.request_id: item for item in state.tool_requests}
    article_ids: list[str] = []
    for result in state.tool_results:
        if (
            (cycle_no is not None and result.cycle_no != cycle_no)
            or result.status != "succeeded"
        ):
            continue
        request = requests_by_id.get(result.request_id)
        if request is None or request.tool_name != "fetch_articles":
            continue
        raw_ids = request.arguments.get("article_ids")
        if not isinstance(raw_ids, (list, tuple)):
            continue
        for raw_id in raw_ids:
            article_id = _nonempty_string(raw_id)
            if article_id is not None and article_id not in article_ids:
                article_ids.append(article_id)
    return tuple(article_ids)


def _merge_graph_article(
    articles: dict[str, dict[str, Any]],
    *,
    article_id: str,
    document_id: str | None,
    title: str | None,
    heading: str | None,
    content_status: GraphCandidateContentStatus,
) -> None:
    item = articles.setdefault(
        article_id,
        {
            "document_id": None,
            "title": None,
            "heading": None,
            "content_status": "not_requested",
        },
    )
    for key, value in (
        ("document_id", document_id),
        ("title", title),
        ("heading", heading),
    ):
        if item[key] is None and value is not None:
            item[key] = value
    if content_status != "not_requested":
        item["content_status"] = content_status


def _extend_unique(target: list[str], values: tuple[str, ...] | list[str]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def _solver_tool_result(
    result: ToolResult,
    *,
    request: ToolRequest | None,
    graph_navigation_ids: frozenset[str],
) -> SolverToolResult:
    graph_projected = (
        request is not None and request.tool_name == "legal_graph_neighbors"
    ) or bool(graph_navigation_ids.intersection(result.evidence_ids))
    return SolverToolResult(
        request_id=result.request_id,
        status=result.status,
        evidence_ids=tuple(
            evidence_id
            for evidence_id in result.evidence_ids
            if evidence_id not in graph_navigation_ids
        ),
        evidence_count=len(result.evidence_ids),
        graph_projection_updated=graph_projected,
        error_code=result.error_code,
        elapsed_ms=result.elapsed_ms,
        cycle_no=result.cycle_no,
    )


def _graph_navigation_payload(evidence: Evidence) -> dict[str, Any]:
    try:
        payload = json.loads(evidence.content)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _article_content_statuses(
    state: CaseState,
) -> dict[str, GraphCandidateContentStatus]:
    results_by_request_id = {
        item.request_id: item for item in state.tool_results
    }
    statuses: dict[str, GraphCandidateContentStatus] = {}
    for request in state.tool_requests:
        if request.tool_name != "fetch_articles":
            continue
        article_ids = request.arguments.get("article_ids")
        if not isinstance(article_ids, (list, tuple)):
            continue
        result = results_by_request_id.get(request.request_id)
        status: GraphCandidateContentStatus = (
            "pending" if result is None else result.status
        )
        for article_id in article_ids:
            normalized = _nonempty_string(article_id)
            if normalized is not None:
                statuses[normalized] = status

    for evidence in state.evidence:
        if evidence.metadata.get("citationEligible") is False:
            continue
        article_id = _nonempty_string(evidence.metadata.get("articleId"))
        if article_id is not None:
            statuses[article_id] = "succeeded"
    return statuses


def _nonempty_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _evidence_article_ids(evidence: Evidence) -> tuple[str, ...]:
    metadata = evidence.metadata
    return tuple(
        dict.fromkeys(
            value
            for key in ("articleId", "fromArticleId", "toArticleId")
            if isinstance((value := metadata.get(key)), str) and value
        )
    )


def _round_robin_result_evidence_ids(
    results: tuple[ToolResult, ...],
) -> tuple[str, ...]:
    """並列Tool結果を呼出順で偏らせず、各結果から機械的に交互採用する。"""
    ordered: list[str] = []
    seen: set[str] = set()
    for index in range(max((len(item.evidence_ids) for item in results), default=0)):
        for result in results:
            if index >= len(result.evidence_ids):
                continue
            evidence_id = result.evidence_ids[index]
            if evidence_id not in seen:
                seen.add(evidence_id)
                ordered.append(evidence_id)
    return tuple(ordered)


def _round_robin_hypothesis_evidence_ids(
    state: CaseState,
    *,
    hypotheses_by_work: dict[str, list[Hypothesis]],
) -> tuple[str, ...]:
    """openなWorkItem間で既存Evidenceの提示順が偏らないよう交互に並べる。"""

    buckets = [
        tuple(
            dict.fromkeys(
                evidence_id
                for hypothesis in hypotheses_by_work.get(work_item.work_item_id, ())
                for evidence_id in hypothesis.evidence_ids
            )
        )
        for work_item in state.work_items
        if work_item.state != "dropped"
    ]
    ordered: list[str] = []
    seen: set[str] = set()
    for index in range(max((len(bucket) for bucket in buckets), default=0)):
        for bucket in buckets:
            if index >= len(bucket):
                continue
            evidence_id = bucket[index]
            if evidence_id not in seen:
                seen.add(evidence_id)
                ordered.append(evidence_id)
    return tuple(ordered)
