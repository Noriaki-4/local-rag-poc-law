"""質問から主論点(LegalIssue)を構造化して取り出すプランナー。

計画書 §7.2(初期論点抽出)、§7.3(ルール補正)、§7.4(論点数)、§12(フォールバック)に対応する。

plannerには正式法令名や条番号を断定させない。質問に明示された法令名・条番号は既存の
決定的パーサーの結果を正とし、`merge_explicit_references` で統合する。
"""

import json
from dataclasses import dataclass, replace
from hashlib import sha1
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .evidence_requirements import ORIGIN_PLANNER, ORIGIN_RULE, LegalIssue
from .legal_ontology import ROLE_FAMILIES

# §7.4 論点数。4件へ固定しない。hard limitを超えた論点も削除せずoverflowへ保持する。
MIN_PRIMARY_ISSUES = 1
SOFT_MAX_PRIMARY_ISSUES = 6
HARD_MAX_PRIMARY_ISSUES = 8

# §7.3 ルール補正表。LLM結果と競合しても両方を仮説として残す。
ROLE_CUE_RULES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("とは", "意味", "対象", "定義", "含まれる"), ("meaning_scope",)),
    (("どのような場合", "要件", "条件", "必要となる", "該当する"), ("qualification",)),
    (("例外", "除外", "ただし", "適用しない", "除く"), ("qualification",)),
    (("手続", "提出", "公告", "届出", "申請", "公表", "様式", "記載"), ("procedure",)),
    (("違反", "罰則", "責任", "処分", "無効", "課徴金"), ("consequence",)),
    (("期間", "いつまで", "期限", "何日", "起算"), ("procedure", "temporal")),
    (("準用", "同じ扱い", "読み替え"), ("linkage",)),
    (("施行", "経過措置", "改正前", "改正後"), ("temporal",)),
    (("ガイドライン", "監督指針", "実務", "運用"), ("interpretive",)),
)

DEFAULT_ROLE_FAMILIES: tuple[str, ...] = ("normative_rule",)


class IssuePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    label: str = ""
    questionSpan: str = ""
    keyTerms: list[str] = Field(default_factory=list, max_length=8)
    requestedRoleFamilies: list[str] = Field(default_factory=list, max_length=8)
    explicitReferences: list[str] = Field(default_factory=list, max_length=8)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class IssuePlanPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    issues: list[IssuePayload] = Field(default_factory=list, max_length=16)
    graphPotentiallyRequired: bool = False


@dataclass(frozen=True)
class IssuePlan:
    """論点計画。hard limit超過分も捨てずにoverflowとして保持する。"""

    issues: tuple[LegalIssue, ...]
    graph_potentially_required: bool = False
    overflow_issues: tuple[LegalIssue, ...] = ()
    fallback_used: bool = False
    validation_error: str | None = None

    @property
    def out_of_scope_labels(self) -> tuple[str, ...]:
        """回答範囲外として利用者へ明示すべき論点ラベル(§7.4)。"""
        return tuple(issue.label for issue in self.overflow_issues)

    def as_trace(self) -> list[dict[str, Any]]:
        return [
            {
                "issueId": issue.issue_id,
                "label": issue.label,
                "questionSpan": issue.question_span,
                "keyTerms": list(issue.key_terms),
                "requestedRoleFamilies": list(issue.requested_role_families),
                "explicitReferences": list(issue.explicit_references),
                "confidence": issue.confidence,
                "source": issue.source,
                "inScope": issue in self.issues,
            }
            for issue in (*self.issues, *self.overflow_issues)
        ]


def role_families_from_text(text: str) -> tuple[str, ...]:
    """質問表現からroleFamily仮説を補う(§7.3)。手掛かりが無ければ原則規定を仮定する。"""
    return _cue_families(text) or DEFAULT_ROLE_FAMILIES


def _cue_families(text: str) -> tuple[str, ...]:
    """表現に一致した役割だけを返す。一致が無い場合は空を返す(既定値を混ぜない)。"""
    families: list[str] = []
    for cues, cue_families in ROLE_CUE_RULES:
        if any(cue in text for cue in cues):
            families.extend(cue_families)
    return tuple(dict.fromkeys(family for family in families if family in ROLE_FAMILIES))


def parse_issue_plan(raw_text: str, *, question: str) -> IssuePlan:
    """plannerのJSON出力を検証し、ルール補正を適用したIssuePlanへ変換する。"""
    try:
        payload = IssuePlanPayload.model_validate(json.loads(raw_text))
    except (json.JSONDecodeError, ValidationError, TypeError) as error:
        return fallback_issue_plan(question, reason=f"issue_plan_parse_error: {type(error).__name__}")

    parsed = [issue for issue in payload.issues if (issue.label or issue.questionSpan)]
    if not parsed:
        return fallback_issue_plan(question, reason="issue_plan_empty")

    issues: list[LegalIssue] = []
    for index, item in enumerate(parsed):
        span = item.questionSpan or item.label
        llm_families = tuple(
            family for family in item.requestedRoleFamilies if family in ROLE_FAMILIES
        )
        # 論点自身の表現を優先する。論点側に手掛かりが無いときだけ質問全文から補う
        # (全論点へ質問全文の役割を配ると、必要根拠スロットが不必要に増えるため)。
        rule_families = _cue_families(f"{span} {item.label}") or _cue_families(question)
        families = tuple(dict.fromkeys([*llm_families, *rule_families])) or DEFAULT_ROLE_FAMILIES
        issues.append(
            LegalIssue(
                issue_id=_issue_id(index, item.label or span),
                label=item.label or span,
                question_span=span,
                key_terms=tuple(term for term in item.keyTerms if term),
                requested_role_families=families,
                # 条番号はplannerの出力を採用しない(§7.2)。決定的パーサー結果を後で統合する。
                explicit_references=(),
                confidence=item.confidence,
                source=ORIGIN_PLANNER,
            )
        )

    in_scope = tuple(issues[:HARD_MAX_PRIMARY_ISSUES])
    overflow = tuple(issues[HARD_MAX_PRIMARY_ISSUES:])
    return IssuePlan(
        issues=in_scope,
        graph_potentially_required=payload.graphPotentiallyRequired,
        overflow_issues=overflow,
    )


def fallback_issue_plan(question: str, *, reason: str) -> IssuePlan:
    """planner失敗時に、質問全文を1主論点としてルールでrole仮説を生成する(§12)。"""
    issue = LegalIssue(
        issue_id=_issue_id(0, question),
        label=question.strip()[:60] or "質問全文",
        question_span=question.strip(),
        key_terms=(),
        requested_role_families=role_families_from_text(question),
        explicit_references=(),
        confidence=0.3,
        source=ORIGIN_RULE,
    )
    return IssuePlan(
        issues=(issue,),
        graph_potentially_required=True,
        fallback_used=True,
        validation_error=reason,
    )


def merge_explicit_references(
    plan: IssuePlan,
    references: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> IssuePlan:
    """決定的パーサーが抽出した条項参照を、最も関連する論点へ割り当てる。

    どの論点にも語が一致しない参照は先頭論点へ付ける。参照を捨てると利用者が明示した
    条項がP0として扱われなくなるため、必ずどこかの論点へ残す。
    """
    if not references or not plan.issues:
        return plan

    assignments: dict[str, list[str]] = {issue.issue_id: [] for issue in plan.issues}
    for reference in references:
        article_id = str(reference.get("articleContentUnitId") or reference.get("contentUnitId") or "")
        if not article_id:
            continue
        target = _best_issue_for_reference(plan.issues, reference)
        if article_id not in assignments[target.issue_id]:
            assignments[target.issue_id].append(article_id)

    issues = tuple(
        replace(
            issue,
            explicit_references=tuple(
                dict.fromkeys([*issue.explicit_references, *assignments.get(issue.issue_id, [])])
            ),
        )
        for issue in plan.issues
    )
    return replace(plan, issues=issues)


def _best_issue_for_reference(
    issues: tuple[LegalIssue, ...],
    reference: dict[str, Any],
) -> LegalIssue:
    matched_text = str(reference.get("matchedText") or "")
    if matched_text:
        scored = [
            (
                sum(1 for term in (*issue.key_terms, issue.label) if term and term in matched_text),
                -index,
                issue,
            )
            for index, issue in enumerate(issues)
        ]
        best = max(scored, key=lambda item: (item[0], item[1]))
        if best[0] > 0:
            return best[2]
    return issues[0]


def _issue_id(index: int, seed: str) -> str:
    digest = sha1(seed.encode("utf-8")).hexdigest()[:8]  # noqa: S324 - IDの安定生成のみ
    return f"issue-{index}-{digest}"


def build_issue_plan_prompt(
    question: str,
    *,
    choices: dict[str, str] | None = None,
    max_issues: int = HARD_MAX_PRIMARY_ISSUES,
) -> str:
    choices_block = ""
    if choices:
        choices_block = "\n選択肢:\n" + "\n".join(
            f"{label.upper()}: {text}" for label, text in sorted(choices.items())
        )
    return f"""あなたは日本法令の質問を法的論点へ分解するプランナーです。
質問が求めている独立した法的論点を挙げ、各論点について必要な法的役割を選んでください。
正解の選択肢を推測せず、検証に必要な論点だけを作ってください。
法令名・条番号は断定せず、labelとkeyTermsには質問に現れた語句だけを使ってください。
requestedRoleFamilies は次から選びます:
{_role_family_help()}
論点は最大{max_issues}件、重複する論点はまとめてください。必ずJSONだけを返してください。

質問: {question}{choices_block}

JSON:"""


def _role_family_help() -> str:
    labels = {
        "normative_rule": "原則・義務・禁止・権利",
        "qualification": "要件・条件・例外・除外",
        "meaning_scope": "定義・適用範囲・みなし",
        "procedure": "手続・届出・公告・期限・様式",
        "consequence": "法的効果・無効・責任・罰則",
        "linkage": "委任・準用・参照",
        "temporal": "施行日・経過措置",
        "interpretive": "行政解釈・実務運用",
    }
    return "\n".join(f"  {family}: {label}" for family, label in labels.items())


def issue_plan_json_schema(max_issues: int = HARD_MAX_PRIMARY_ISSUES) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "issues": {
                "type": "array",
                "minItems": MIN_PRIMARY_ISSUES,
                "maxItems": max_issues,
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "minLength": 1, "maxLength": 60},
                        "questionSpan": {"type": "string", "maxLength": 200},
                        "keyTerms": {
                            "type": "array",
                            "items": {"type": "string", "maxLength": 40},
                            "maxItems": 8,
                        },
                        "requestedRoleFamilies": {
                            "type": "array",
                            "items": {"type": "string", "enum": list(ROLE_FAMILIES)},
                            "minItems": 1,
                            "maxItems": 4,
                        },
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": [
                        "label",
                        "questionSpan",
                        "keyTerms",
                        "requestedRoleFamilies",
                        "confidence",
                    ],
                    "additionalProperties": False,
                },
            },
            "graphPotentiallyRequired": {"type": "boolean"},
        },
        "required": ["issues", "graphPotentiallyRequired"],
        "additionalProperties": False,
    }
