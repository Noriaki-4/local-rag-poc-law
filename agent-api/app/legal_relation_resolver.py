"""条文本文とGraph関係から、語句シグナルと旧経路用の子Requirementを得る。

計画書 §6.1(IMPLEMENTSの段階的confidence)、§8.4(子Requirementの生成)、§9.5(充足)に対応する。

ここに置くのは本文の決定的な手掛かり検出と旧レイヤー探索の補助規則だけで、外部I/Oは持たない。
schema version 5のseedは`assess_implements`のconfidenceで正式IMPLEMENTSを作らず、検出した
語句を未確認RelationAssertionの監査シグナルとしてだけ保存する。法的関係の確定は両端本文を
読んだLLMが案件内で行う。
"""

import re
from dataclasses import dataclass
from typing import Any

from .evidence_requirements import (
    ORIGIN_ARTICLE_TEXT,
    ORIGIN_GRAPH,
    EvidenceRequirement,
    child_requirement,
)
from .law_family import family_root_for_article
from .legal_ontology import (
    AUTHORITY_CABINET_OFFICE_ORDINANCE,
    AUTHORITY_CABINET_ORDER,
    AUTHORITY_MINISTERIAL_ORDINANCE,
    AUTHORITY_ORDINANCE_UNSPECIFIED,
    REFERENCE_KIND_APPLICATION,
    REFERENCE_KIND_ARTICLE_REFERENCE,
    REFERENCE_KIND_DEFINITION,
    REFERENCE_KIND_EXCEPTION,
    REFERENCE_KIND_FORM_OR_TABLE,
    REFERENCE_KIND_PARENT_LAW_REFERENCE,
    REFERENCE_ONLY_CONFIDENCE,
    edge_spec,
    implements_confidence,
    is_trusted_relation,
)

# 親条文側の委任文言。レイヤーごとに、どの下位法令へ委任しているかを見分ける。
DELEGATION_CUES: dict[str, tuple[str, ...]] = {
    AUTHORITY_CABINET_ORDER: ("政令で定める", "政令に定める", "政令で"),
    AUTHORITY_CABINET_OFFICE_ORDINANCE: (
        "内閣府令で定める",
        "内閣府令に定める",
        "内閣府令・省令で定める",
        "内閣府令で",
    ),
    AUTHORITY_MINISTERIAL_ORDINANCE: (
        "省令で定める",
        "省令に定める",
        "主務省令で定める",
        "厚生労働省令で定める",
        "内閣府令・省令で定める",
    ),
}

ALL_DELEGATION_CUES: tuple[str, ...] = tuple(
    dict.fromkeys(cue for cues in DELEGATION_CUES.values() for cue in cues)
)

# 下位法令側の具体化表現。単純な条文参照と区別する。
SPECIFICATION_CUES: tuple[str, ...] = (
    "の規定により",
    "の規定による",
    "で定めるもの",
    "で定める事項",
    "で定める場合",
    "で定めるところにより",
)

# 「A条に規定するX」という定義上の単純参照は、A条を具体化する関係ではない。
# 下位法令が参照先を受けて何を定めるかまで同じ局所文脈に現れる場合だけ、
# 具体化表現として扱う。
REFERENCE_SPECIFICATION_PATTERN = re.compile(
    r"に規定する[^。\n]{0,50}"
    r"(?:事項|方式|方法|条件|要件|もの|場合)を定める"
)

APPLICATION_CUES: tuple[str, ...] = ("準用", "読み替え")
EXCEPTION_CUES: tuple[str, ...] = ("ただし", "この限りでない", "except", "を除く", "除く")
DEFINITION_CUES: tuple[str, ...] = ("定義", "とは、", "をいう", "の意義")
FORM_CUES: tuple[str, ...] = ("別表", "様式", "別紙")

# 「政令で定める」等から追加すべき下位レイヤーの対応表(§8.4)。
DELEGATION_TARGET_AUTHORITIES: tuple[tuple[str, str], ...] = (
    ("政令で定める", AUTHORITY_CABINET_ORDER),
    ("内閣府令・省令で定める", AUTHORITY_ORDINANCE_UNSPECIFIED),
    ("内閣府令で定める", AUTHORITY_CABINET_OFFICE_ORDINANCE),
    ("主務省令で定める", AUTHORITY_MINISTERIAL_ORDINANCE),
    ("厚生労働省令で定める", AUTHORITY_MINISTERIAL_ORDINANCE),
    ("省令で定める", AUTHORITY_MINISTERIAL_ORDINANCE),
)

ITEM_PARAGRAPH_PATTERN = re.compile(r"第[0-9一二三四五六七八九十百千〇零]+条")


@dataclass(frozen=True)
class ImplementsAssessment:
    """親・下位条文にある委任候補語句の検出結果（正式関係ではない）。"""

    confidence: float
    delegation_wording_detected: bool
    specification_wording_detected: bool

    @property
    def is_implements(self) -> bool:
        """IMPLEMENTSとして保存してよいか。単純参照ならREFERENCESのままにする(§6.1)。"""
        spec = edge_spec("IMPLEMENTS")
        assert spec is not None
        return self.confidence >= spec.minimum_trusted_confidence


def has_delegation_wording(text: str, authority_type: str | None = None) -> bool:
    """親条文本文に、下位法令への委任文言があるか。"""
    cues = DELEGATION_CUES.get(str(authority_type or ""), ALL_DELEGATION_CUES)
    if authority_type == AUTHORITY_ORDINANCE_UNSPECIFIED:
        cues = tuple(
            dict.fromkeys(
                [
                    *DELEGATION_CUES[AUTHORITY_CABINET_OFFICE_ORDINANCE],
                    *DELEGATION_CUES[AUTHORITY_MINISTERIAL_ORDINANCE],
                ]
            )
        )
    return any(cue in text for cue in cues)


def has_specification_wording(text: str) -> bool:
    return (
        any(cue in text for cue in SPECIFICATION_CUES)
        or any(cue in text for cue in ALL_DELEGATION_CUES)
        or bool(REFERENCE_SPECIFICATION_PATTERN.search(text))
    )


def assess_implements(
    *,
    parent_text: str,
    child_text: str,
    child_authority_type: str | None,
    same_family: bool,
    manual: bool = False,
) -> ImplementsAssessment:
    """委任候補の語句シグナルを旧confidence形式で返す(§6.1)。

    schema version 5のseedではconfidenceを候補の採否・昇格に使わない。旧レイヤー探索との
    互換性を保ちながら、親条文側の委任文言と下位法令側の具体化表現を観測する。
    """
    delegation = has_delegation_wording(parent_text, child_authority_type)
    specification = has_specification_wording(child_text)
    # 親条文のどこかに委任文言があるだけでは足りない。下位法令側の当該参照箇所にも
    # 具体化表現がなければ、単純REFERENCESとして残す。
    confidence = implements_confidence(
        manual=manual,
        delegation_wording=delegation and specification,
        same_family=same_family,
        specification_wording=specification,
    )
    return ImplementsAssessment(
        confidence=confidence,
        delegation_wording_detected=delegation,
        specification_wording_detected=specification,
    )


def classify_reference_kind(source_text: str, *, is_parent_law_reference: bool = False) -> str:
    """REFERENCESへ付与する参照の種類を決める(§6.1)。"""
    if is_parent_law_reference:
        return REFERENCE_KIND_PARENT_LAW_REFERENCE
    if any(cue in source_text for cue in APPLICATION_CUES):
        return REFERENCE_KIND_APPLICATION
    if any(cue in source_text for cue in FORM_CUES):
        return REFERENCE_KIND_FORM_OR_TABLE
    if any(cue in source_text for cue in DEFINITION_CUES):
        return REFERENCE_KIND_DEFINITION
    if any(cue in source_text for cue in EXCEPTION_CUES):
        return REFERENCE_KIND_EXCEPTION
    return REFERENCE_KIND_ARTICLE_REFERENCE


# --------------------------------------------------------------------------------------
# 子Requirementの生成 (§8.4)
# --------------------------------------------------------------------------------------


def child_requirements_from_article_text(
    requirement: EvidenceRequirement,
    *,
    article_id: str,
    text: str,
) -> tuple[EvidenceRequirement, ...]:
    """取得したArticle本文から、必要な下位法令・例外・様式のRequirementを作る。

    plannerが必要役割を完全に予測できなくても、ここで不足を追加できるようにする(§7.5)。
    """
    children: list[EvidenceRequirement] = []
    seen_authorities: set[str] = set()
    # 委任先は親条文と同じ法令系統から探す。無関係な法令系統の施行令等を拾わない。
    family_root = family_root_for_article(article_id) or requirement.family_root
    for cue, authority_type in DELEGATION_TARGET_AUTHORITIES:
        if cue not in text or authority_type in seen_authorities:
            continue
        seen_authorities.add(authority_type)
        children.append(
            child_requirement(
                requirement,
                role_family=requirement.role_family,
                role_subtypes=requirement.role_subtypes,
                authority_type=authority_type,
                parent_article_id=article_id,
                family_root=family_root,
                entered_by="IMPLEMENTS",
                origin=ORIGIN_ARTICLE_TEXT,
                query_hint=f"{requirement.query_hint} {cue}".strip(),
            )
        )

    if any(cue in text for cue in APPLICATION_CUES):
        children.append(
            child_requirement(
                requirement,
                role_family="linkage",
                role_subtypes=("application",),
                parent_article_id=article_id,
                family_root=family_root,
                entered_by="APPLIED_BY",
                origin=ORIGIN_ARTICLE_TEXT,
                query_hint=f"{requirement.query_hint} 準用".strip(),
            )
        )
    if any(cue in text for cue in EXCEPTION_CUES):
        children.append(
            child_requirement(
                requirement,
                role_family="qualification",
                role_subtypes=("exception",),
                parent_article_id=article_id,
                family_root=family_root,
                entered_by="REFERENCES",
                origin=ORIGIN_ARTICLE_TEXT,
                query_hint=f"{requirement.query_hint} 例外 ただし書".strip(),
            )
        )
    if any(cue in text for cue in FORM_CUES):
        children.append(
            child_requirement(
                requirement,
                role_family="procedure",
                role_subtypes=("form",),
                parent_article_id=article_id,
                family_root=family_root,
                entered_by="REFERENCES",
                origin=ORIGIN_ARTICLE_TEXT,
                query_hint=f"{requirement.query_hint} 様式 別表".strip(),
            )
        )
    return tuple(children)


def child_requirements_from_graph(
    requirement: EvidenceRequirement,
    edges: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    authority_types_by_article: dict[str, str] | None = None,
    max_children: int = 6,
) -> tuple[EvidenceRequirement, ...]:
    """高信頼Graph関係の接続先を、子Requirementとして追加する(§8.4)。

    未実装エッジ・未確認assertion・信頼度不足のエッジは拡張に使わない。
    """
    authorities = authority_types_by_article or {}
    children: list[EvidenceRequirement] = []
    for edge in edges:
        if len(children) >= max_children:
            break
        edge_type = str(edge.get("edgeType") or "")
        spec = edge_spec(edge_type)
        if spec is None or not spec.can_expand_search or not spec.implemented:
            continue
        if not is_trusted_relation(edge):
            continue
        target_id = str(edge.get("toGraphNodeId") or "")
        if not target_id or target_id == requirement.article_id:
            continue
        children.append(
            child_requirement(
                requirement,
                role_family=_role_family_for_edge(edge_type, requirement),
                role_subtypes=_role_subtypes_for_edge(edge_type, requirement),
                authority_type=authorities.get(target_id),
                article_id=target_id,
                family_root=family_root_for_article(target_id) or requirement.family_root,
                parent_article_id=str(edge.get("fromGraphNodeId") or requirement.article_id or ""),
                entered_by=edge_type,
                origin=ORIGIN_GRAPH,
            )
        )
    return tuple(children)


def _role_family_for_edge(edge_type: str, requirement: EvidenceRequirement) -> str:
    if edge_type == "APPLIED_BY":
        return "linkage"
    if edge_type == "EXCEPTION_TO":
        return "qualification"
    if edge_type == "DEFINES":
        return "meaning_scope"
    return requirement.role_family


def _role_subtypes_for_edge(edge_type: str, requirement: EvidenceRequirement) -> tuple[str, ...]:
    if edge_type == "APPLIED_BY":
        return ("application",)
    if edge_type == "EXCEPTION_TO":
        return ("exception",)
    if edge_type == "DEFINES":
        return ("definition",)
    return requirement.role_subtypes


def unresolved_reference_cues(text: str) -> tuple[str, ...]:
    """本文に残る未解決参照の手掛かり。resolved判定を候補件数で代替しないために使う。"""
    cues = []
    for cue in ("前条", "次条", "同条", "前項", "同項", "各号", "別表", "様式"):
        if cue in text:
            cues.append(cue)
    return tuple(cues)


def reference_only_confidence() -> float:
    return REFERENCE_ONLY_CONFIDENCE
