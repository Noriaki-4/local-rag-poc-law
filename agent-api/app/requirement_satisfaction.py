"""Article候補がEvidenceRequirementの法的役割を満たすかを決定的に判定する。

検索ヒット数や順位だけでは`resolved`にしない。明示条文・高信頼Graph接続先はArticle IDの
一致を構造的根拠とし、通常検索候補は論点語と役割手掛かりの両方を確認する。
"""

from dataclasses import dataclass
from typing import Any

from .evidence_requirements import EvidenceRequirement
from .legal_relation_resolver import unresolved_reference_cues


ROLE_SATISFACTION_CUES: dict[str, tuple[str, ...]] = {
    "normative_rule": (
        "原則",
        "要件",
        "ものとする",
        "とする",
        "しなければならない",
        "してはならない",
        "することができる",
        "定める",
        "置く",
        "義務",
        "禁止",
        "権利",
    ),
    "qualification": (
        "要件",
        "条件",
        "場合",
        "ただし",
        "この限りでない",
        "を除く",
        "除外",
        "適用しない",
    ),
    "meaning_scope": (
        "定義",
        "意義",
        "とは",
        "をいう",
        "みなす",
        "範囲",
        "適用",
    ),
    "procedure": (
        "手続",
        "届出",
        "提出",
        "公告",
        "公表",
        "通知",
        "申請",
        "承認",
        "期限",
        "以内",
        "様式",
        "別表",
    ),
    "consequence": (
        "効力",
        "無効",
        "責任",
        "損害賠償",
        "取消",
        "処分",
        "罰則",
        "課徴金",
        "懲役",
        "罰金",
    ),
    "linkage": (
        "準用",
        "読み替え",
        "規定による",
        "規定により",
        "政令で定める",
        "内閣府令で定める",
        "省令で定める",
    ),
    "temporal": (
        "施行",
        "経過措置",
        "適用日",
        "改正前",
        "改正後",
        "期日",
        "期間",
    ),
    "interpretive": (
        "取扱い",
        "解釈",
        "考え方",
        "監督",
        "実務",
    ),
}


@dataclass(frozen=True)
class SatisfactionAssessment:
    satisfied: bool
    reasons: tuple[str, ...] = ()
    matched_key_terms: tuple[str, ...] = ()
    matched_role_cues: tuple[str, ...] = ()
    unresolved_reference_cues: tuple[str, ...] = ()
    structurally_required: bool = False

    def as_trace(self) -> dict[str, Any]:
        return {
            "satisfied": self.satisfied,
            "reasons": list(self.reasons),
            "matchedKeyTerms": list(self.matched_key_terms),
            "matchedRoleCues": list(self.matched_role_cues),
            "unresolvedReferenceCues": list(self.unresolved_reference_cues),
            "structurallyRequired": self.structurally_required,
        }


def assess_candidate(
    requirement: EvidenceRequirement,
    candidate: dict[str, Any],
) -> SatisfactionAssessment:
    """候補Articleの本文・見出しとRequirementを照合する。"""
    article_id = str(candidate.get("articleId") or "")
    text = _candidate_text(candidate)
    unresolved = unresolved_reference_cues(text)

    if requirement.article_id:
        if article_id != requirement.article_id:
            return SatisfactionAssessment(
                False,
                ("article_id_mismatch",),
                unresolved_reference_cues=unresolved,
                structurally_required=True,
            )
        return SatisfactionAssessment(
            True,
            ("exact_article_id",),
            unresolved_reference_cues=unresolved,
            structurally_required=True,
        )

    # 明示条文または信頼済みEXPLAINS/Graph接続先を検索側が直接取得した場合。
    # 法令本文の用語が質問文と異なるだけで、確認済みの構造根拠を捨てない。
    if candidate.get("directMatch"):
        return SatisfactionAssessment(
            True,
            ("direct_article_target",),
            unresolved_reference_cues=unresolved,
            structurally_required=True,
        )

    key_terms = tuple(
        term for term in requirement.key_terms if term and len(term.strip()) >= 2
    )
    matched_terms = tuple(term for term in key_terms if term in text)
    role_cues = ROLE_SATISFACTION_CUES.get(requirement.role_family, ())
    matched_cues = tuple(cue for cue in role_cues if cue in text)
    reasons: list[str] = []

    if key_terms and not matched_terms:
        reasons.append("missing_key_term")
    if role_cues and not matched_cues:
        reasons.append("missing_role_cue")
    if not text.strip():
        reasons.append("empty_article_text")

    # keyTermsが無いfallback planでも、役割の手掛かりは必須とする。検索順位だけで
    # satisfiedにしない一方、法令用語を抽出できなかった質問を全面的に停止させない。
    return SatisfactionAssessment(
        not reasons,
        tuple(reasons or ["lexical_role_match"]),
        matched_key_terms=matched_terms,
        matched_role_cues=matched_cues,
        unresolved_reference_cues=unresolved,
    )


def _candidate_text(candidate: dict[str, Any]) -> str:
    chunks = candidate.get("chunks") or []
    parts = [
        str(candidate.get("heading") or ""),
        str(candidate.get("text") or ""),
        *(str(chunk.get("heading") or "") for chunk in chunks),
        *(str(chunk.get("text") or "") for chunk in chunks),
    ]
    return " ".join(part for part in parts if part)
