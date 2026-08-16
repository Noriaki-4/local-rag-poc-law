"""法令レイヤー・法的役割・Graphエッジのレジストリ。

計画書 `layered_legal_evidence_retrieval_plan.md` の §5(基本概念)、§6(Graphオントロジー)に
対応する。ノード種別・Graph関係・法的役割・証拠状態を同じenumへ混在させないため、
軸ごとに別の定義を置く。

このモジュールは外部I/Oを持たない純粋な定義とヘルパーだけで構成し、seed・検索・監査・
テストのいずれからも同じ規則を参照できるようにする。
"""

from dataclasses import dataclass
from typing import Any

# オントロジーまたは抽出規則を変更した場合に上げる。seed metadataとtraceへ保存し、
# 「どのschemaでseedされたGraphか」を後から判別できるようにする(§6.3)。
GRAPH_SCHEMA_VERSION = 7


# --------------------------------------------------------------------------------------
# 軸1: ノード種別
# --------------------------------------------------------------------------------------

NODE_TYPE_DOCUMENT = "Document"
NODE_TYPE_ARTICLE = "Article"
NODE_TYPE_PARAGRAPH = "Paragraph"
NODE_TYPE_ITEM = "Item"
NODE_TYPE_TERM = "Term"
NODE_TYPE_RELATION_ASSERTION = "RelationAssertion"

NODE_TYPES: tuple[str, ...] = (
    NODE_TYPE_DOCUMENT,
    NODE_TYPE_ARTICLE,
    NODE_TYPE_PARAGRAPH,
    NODE_TYPE_ITEM,
    NODE_TYPE_TERM,
    NODE_TYPE_RELATION_ASSERTION,
)

CONTENT_UNIT_NODE_TYPES: tuple[str, ...] = (
    NODE_TYPE_ARTICLE,
    NODE_TYPE_PARAGRAPH,
    NODE_TYPE_ITEM,
)


# --------------------------------------------------------------------------------------
# 法令レイヤー (authorityType) — §5.2
# --------------------------------------------------------------------------------------

AUTHORITY_ACT = "act"
AUTHORITY_CABINET_ORDER = "cabinet_order"
AUTHORITY_MINISTERIAL_ORDINANCE = "ministerial_ordinance"
AUTHORITY_CABINET_OFFICE_ORDINANCE = "cabinet_office_ordinance"
AUTHORITY_ORDINANCE_UNSPECIFIED = "ordinance_unspecified"
AUTHORITY_GUIDANCE = "guidance"
AUTHORITY_UNKNOWN = "unknown"

AUTHORITY_TYPES: tuple[str, ...] = (
    AUTHORITY_ACT,
    AUTHORITY_CABINET_ORDER,
    AUTHORITY_MINISTERIAL_ORDINANCE,
    AUTHORITY_CABINET_OFFICE_ORDINANCE,
    AUTHORITY_ORDINANCE_UNSPECIFIED,
    AUTHORITY_GUIDANCE,
    AUTHORITY_UNKNOWN,
)

# 規範的法令レイヤー。guidanceは補助資料レーンとして別管理する(§10)。
NORMATIVE_AUTHORITY_TYPES: tuple[str, ...] = (
    AUTHORITY_ACT,
    AUTHORITY_CABINET_ORDER,
    AUTHORITY_MINISTERIAL_ORDINANCE,
    AUTHORITY_CABINET_OFFICE_ORDINANCE,
    AUTHORITY_ORDINANCE_UNSPECIFIED,
)

# 「未判別」を表す値。省令・内閣府令のどちらかへ推測で確定してはならないが、
# レイヤー指定検索から構造的に落としてもならない(§5.2)。
UNDETERMINED_AUTHORITY_TYPES: tuple[str, ...] = (
    AUTHORITY_ORDINANCE_UNSPECIFIED,
    AUTHORITY_UNKNOWN,
)

AUTHORITY_SOURCE_REGISTRY = "registry_manual_verified"
AUTHORITY_SOURCE_LAW_ID = "law_id"
AUTHORITY_SOURCE_LAW_TYPE = "egov_law_type"
AUTHORITY_SOURCE_DOC_TYPE = "doc_type"
AUTHORITY_SOURCE_UNRESOLVED = "unresolved"

# e-Gov lawId の種別コード。M系(省令・内閣府令)はここで確定させない。
_LAW_ID_ACT_CODES = ("AC",)
_LAW_ID_CABINET_ORDER_CODES = ("CO",)
_LAW_ID_ORDINANCE_PREFIX = "M"

# e-Gov LawType。MinisterialOrdinanceは内閣府令も含むため未判別扱いにする。
_LAW_TYPE_MAP = {
    "Act": AUTHORITY_ACT,
    "CabinetOrder": AUTHORITY_CABINET_ORDER,
    "MinisterialOrdinance": AUTHORITY_ORDINANCE_UNSPECIFIED,
}

# タイトル由来の候補値。確定値にせず、人手確認の対象として記録する(§5.2優先順位4)。
_TITLE_CANDIDATE_RULES: tuple[tuple[str, str], ...] = (
    ("内閣府令", AUTHORITY_CABINET_OFFICE_ORDINANCE),
    ("施行規則", AUTHORITY_MINISTERIAL_ORDINANCE),
    ("省令", AUTHORITY_MINISTERIAL_ORDINANCE),
    ("施行令", AUTHORITY_CABINET_ORDER),
)


@dataclass(frozen=True)
class AuthorityResolution:
    """authorityTypeの確定値と、その生成元・監査要否。"""

    authority_type: str
    authority_source: str
    candidate_authority_type: str | None = None
    candidate_source: str | None = None

    @property
    def needs_manual_review(self) -> bool:
        """省令・内閣府令を確定できておらず、registryへの人手確認が必要か。"""
        return self.authority_type in UNDETERMINED_AUTHORITY_TYPES


def resolve_authority_type(
    law_id: str | None,
    *,
    registry_authority_type: str | None = None,
    law_type: str | None = None,
    title: str | None = None,
    doc_type: str | None = None,
) -> AuthorityResolution:
    """§5.2の優先順位でauthorityTypeを決める。

    1. 人が確認したlaw registryの明示値
    2. lawId / e-Gov LawTypeから一意に判定できる法律・政令
    3. LawType=MinisterialOrdinance は ordinance_unspecified
    4. タイトル等からの候補値は確定させず監査対象にする
    5. 判定できない場合は unknown
    """
    if registry_authority_type:
        if registry_authority_type not in AUTHORITY_TYPES:
            raise ValueError(f"Unknown authorityType: {registry_authority_type}")
        return AuthorityResolution(registry_authority_type, AUTHORITY_SOURCE_REGISTRY)

    if doc_type == "guideline":
        return AuthorityResolution(AUTHORITY_GUIDANCE, AUTHORITY_SOURCE_DOC_TYPE)

    candidate = _title_candidate(title)
    from_law_id = _authority_from_law_id(law_id)
    if from_law_id:
        return AuthorityResolution(
            from_law_id,
            AUTHORITY_SOURCE_LAW_ID,
            candidate_authority_type=candidate if from_law_id in UNDETERMINED_AUTHORITY_TYPES else None,
            candidate_source="title" if candidate else None,
        )

    from_law_type = _LAW_TYPE_MAP.get(str(law_type or ""))
    if from_law_type:
        return AuthorityResolution(
            from_law_type,
            AUTHORITY_SOURCE_LAW_TYPE,
            candidate_authority_type=candidate if from_law_type in UNDETERMINED_AUTHORITY_TYPES else None,
            candidate_source="title" if candidate else None,
        )

    return AuthorityResolution(
        AUTHORITY_UNKNOWN,
        AUTHORITY_SOURCE_UNRESOLVED,
        candidate_authority_type=candidate,
        candidate_source="title" if candidate else None,
    )


def _authority_from_law_id(law_id: str | None) -> str | None:
    value = str(law_id or "").strip()
    # e-Gov lawIdは「元号年3桁 + 種別コード + 連番」。先頭3桁が数字でない値は対象外。
    if len(value) < 5 or not value[:3].isdigit():
        return None
    code = value[3:5]
    if code in _LAW_ID_ACT_CODES:
        return AUTHORITY_ACT
    if code in _LAW_ID_CABINET_ORDER_CODES:
        return AUTHORITY_CABINET_ORDER
    if value[3] == _LAW_ID_ORDINANCE_PREFIX:
        # M系は省令と内閣府令を区別できない。推測で確定しない。
        return AUTHORITY_ORDINANCE_UNSPECIFIED
    return None


def _title_candidate(title: str | None) -> str | None:
    text = str(title or "")
    for keyword, authority_type in _TITLE_CANDIDATE_RULES:
        if keyword in text:
            return authority_type
    return None


def search_authority_types(requirement_authority_type: str | None) -> tuple[str, ...]:
    """Requirementのレイヤー指定に対して、検索対象へ含めるauthorityTypeを返す。

    空タプルは「レイヤーで絞らない」を意味する。未判別候補(ordinance_unspecified /
    unknown)を構造的に落とさないことが目的で、順位付けは authority_type_rank で行う。
    """
    if not requirement_authority_type or requirement_authority_type == AUTHORITY_UNKNOWN:
        return ()
    if requirement_authority_type == AUTHORITY_MINISTERIAL_ORDINANCE:
        return (
            AUTHORITY_MINISTERIAL_ORDINANCE,
            AUTHORITY_ORDINANCE_UNSPECIFIED,
            AUTHORITY_UNKNOWN,
        )
    if requirement_authority_type == AUTHORITY_CABINET_OFFICE_ORDINANCE:
        return (
            AUTHORITY_CABINET_OFFICE_ORDINANCE,
            AUTHORITY_ORDINANCE_UNSPECIFIED,
            AUTHORITY_UNKNOWN,
        )
    if requirement_authority_type == AUTHORITY_ORDINANCE_UNSPECIFIED:
        return (
            AUTHORITY_MINISTERIAL_ORDINANCE,
            AUTHORITY_CABINET_OFFICE_ORDINANCE,
            AUTHORITY_ORDINANCE_UNSPECIFIED,
            AUTHORITY_UNKNOWN,
        )
    return (requirement_authority_type,)


def authority_type_rank(requirement_authority_type: str | None, candidate_authority_type: str | None) -> int:
    """完全一致を最優先し、未判別候補はその次に置く(構造的には落とさない)。"""
    if not requirement_authority_type:
        return 0
    candidate = candidate_authority_type or AUTHORITY_UNKNOWN
    if candidate == requirement_authority_type:
        return 0
    if candidate == AUTHORITY_ORDINANCE_UNSPECIFIED:
        return 1
    if candidate == AUTHORITY_UNKNOWN:
        return 2
    return 3


# --------------------------------------------------------------------------------------
# 軸3: 法的役割 (roleFamily / roleSubtype) — §5.3
# --------------------------------------------------------------------------------------

ROLE_FAMILIES: dict[str, tuple[str, ...]] = {
    "normative_rule": ("general_rule", "obligation", "prohibition", "permission", "entitlement"),
    "qualification": ("requirement", "condition", "exception", "exclusion", "special_rule"),
    "meaning_scope": ("definition", "scope", "deeming", "presumption"),
    "procedure": ("filing", "notice", "publication", "approval", "deadline", "form"),
    "consequence": (
        "legal_effect",
        "invalidity",
        "liability",
        "remedy",
        "administrative_action",
        "penalty",
    ),
    "linkage": ("delegation", "implementation", "reference", "application"),
    "temporal": ("effective_date", "transitional_measure"),
    "interpretive": ("interpretation", "supervisory_expectation", "practice_example"),
}


def is_role_family(value: str | None) -> bool:
    return str(value or "") in ROLE_FAMILIES


def normalize_role_subtypes(role_family: str, role_subtypes: Any) -> tuple[str, ...]:
    """未知のsubtypeを落として正規化する。`delegated_detail`はroleにしない(§5.3)。"""
    if role_family not in ROLE_FAMILIES:
        raise ValueError(f"Unknown roleFamily: {role_family}")
    allowed = ROLE_FAMILIES[role_family]
    values = role_subtypes if isinstance(role_subtypes, (list, tuple)) else []
    return tuple(dict.fromkeys(str(value) for value in values if str(value) in allowed))


# --------------------------------------------------------------------------------------
# 軸2: Graph関係 — §6
# --------------------------------------------------------------------------------------

REFERENCE_KIND_ARTICLE_REFERENCE = "article_reference"
# 親法令への明示参照という構文上の事実。委任関係の確定を意味しない。
REFERENCE_KIND_PARENT_LAW_REFERENCE = "parent_law_reference"
# schema version 4以前の読込互換。version 5のseedでは生成しない。
REFERENCE_KIND_DELEGATION_PARENT = "delegation_parent"
REFERENCE_KIND_APPLICATION = "application"
REFERENCE_KIND_DEFINITION = "definition"
REFERENCE_KIND_EXCEPTION = "exception"
REFERENCE_KIND_FORM_OR_TABLE = "form_or_table"

REFERENCE_KINDS: tuple[str, ...] = (
    REFERENCE_KIND_ARTICLE_REFERENCE,
    REFERENCE_KIND_PARENT_LAW_REFERENCE,
    REFERENCE_KIND_DELEGATION_PARENT,
    REFERENCE_KIND_APPLICATION,
    REFERENCE_KIND_DEFINITION,
    REFERENCE_KIND_EXCEPTION,
    REFERENCE_KIND_FORM_OR_TABLE,
)

# IMPLEMENTS の段階的confidence (§6.1)。固定0.95をやめ、根拠の強さで段階化する。
IMPLEMENTS_CONFIDENCE_MANUAL = 1.00
IMPLEMENTS_CONFIDENCE_EXPLICIT_DELEGATION = 0.98
IMPLEMENTS_CONFIDENCE_FAMILY_RULE = 0.90
REFERENCE_ONLY_CONFIDENCE = 0.70
UNVERIFIED_ASSERTION_CONFIDENCE = 0.50

RELATION_STATUS_UNVERIFIED = "unverified"
RELATION_STATUS_LAW_TEXT_VERIFIED = "law_text_verified"
RELATION_STATUS_LLM_IMPLEMENTS = "llm_classified_implements"
RELATION_STATUS_LLM_REFERENCE_ONLY = "llm_classified_reference_only"
RELATION_STATUS_LLM_UNCERTAIN = "llm_classified_uncertain"

RELATION_ASSERTION_CLASSIFICATION_STATUSES: tuple[str, ...] = (
    RELATION_STATUS_UNVERIFIED,
    RELATION_STATUS_LLM_IMPLEMENTS,
    RELATION_STATUS_LLM_REFERENCE_ONLY,
    RELATION_STATUS_LLM_UNCERTAIN,
)


def implements_confidence(
    *,
    manual: bool = False,
    delegation_wording: bool = False,
    same_family: bool = False,
    specification_wording: bool = False,
) -> float:
    """委任関係の確からしさを段階値で返す。

    単純参照しか確認できない場合は REFERENCE_ONLY_CONFIDENCE を返す。呼び出し側は
    この値のとき IMPLEMENTS を作らず REFERENCES のままにする(§6.1)。
    """
    if manual:
        return IMPLEMENTS_CONFIDENCE_MANUAL
    if delegation_wording and same_family:
        return IMPLEMENTS_CONFIDENCE_EXPLICIT_DELEGATION
    if same_family and specification_wording:
        return IMPLEMENTS_CONFIDENCE_FAMILY_RULE
    return REFERENCE_ONLY_CONFIDENCE


@dataclass(frozen=True)
class EdgeSpec:
    """§6.2 エッジレジストリの1エントリ。"""

    edge_type: str
    from_node_types: tuple[str, ...]
    to_node_types: tuple[str, ...]
    direction: str
    inverse_display_name: str
    normative: bool
    can_expand_search: bool
    can_satisfy_evidence: bool
    requires_fetched_target_text: bool
    allowed_sources: tuple[str, ...]
    minimum_trusted_confidence: float
    implemented: bool
    derived_from_reference: bool = False
    requires_delegation_wording: bool = False
    from_doc_types: tuple[str, ...] = ()


EDGE_REGISTRY: dict[str, EdgeSpec] = {
    "HAS_CONTENT_UNIT": EdgeSpec(
        edge_type="HAS_CONTENT_UNIT",
        from_node_types=(NODE_TYPE_DOCUMENT, NODE_TYPE_ARTICLE, NODE_TYPE_PARAGRAPH),
        to_node_types=(NODE_TYPE_ARTICLE, NODE_TYPE_PARAGRAPH, NODE_TYPE_ITEM),
        direction="container_to_child",
        inverse_display_name="包含される",
        normative=False,
        can_expand_search=False,
        can_satisfy_evidence=False,
        requires_fetched_target_text=True,
        allowed_sources=("xml_rule", "manual"),
        minimum_trusted_confidence=1.0,
        implemented=True,
    ),
    "REFERENCES": EdgeSpec(
        edge_type="REFERENCES",
        from_node_types=CONTENT_UNIT_NODE_TYPES,
        to_node_types=CONTENT_UNIT_NODE_TYPES,
        direction="citing_to_cited",
        inverse_display_name="参照される",
        normative=False,
        can_expand_search=True,
        can_satisfy_evidence=False,
        requires_fetched_target_text=True,
        allowed_sources=(
            "xml_reference_rule",
            "subordinate_law_parent_reference",
            "regex_rule",
            "manual",
        ),
        minimum_trusted_confidence=0.9,
        implemented=True,
    ),
    "IMPLEMENTS": EdgeSpec(
        edge_type="IMPLEMENTS",
        from_node_types=(NODE_TYPE_ARTICLE,),
        to_node_types=(NODE_TYPE_ARTICLE,),
        direction="parent_to_child",
        inverse_display_name="具体化元",
        normative=True,
        can_expand_search=True,
        can_satisfy_evidence=False,
        requires_fetched_target_text=True,
        allowed_sources=(
            "subordinate_law_parent_reference",
            "regex_rule",
            "llm_reviewed",
            "manual",
        ),
        minimum_trusted_confidence=IMPLEMENTS_CONFIDENCE_FAMILY_RULE,
        implemented=True,
        derived_from_reference=True,
        requires_delegation_wording=True,
    ),
    "APPLIED_BY": EdgeSpec(
        edge_type="APPLIED_BY",
        from_node_types=(NODE_TYPE_ARTICLE,),
        to_node_types=(NODE_TYPE_ARTICLE,),
        direction="applied_to_applying",
        inverse_display_name="準用する",
        normative=True,
        can_expand_search=True,
        can_satisfy_evidence=False,
        requires_fetched_target_text=True,
        allowed_sources=("incorporation_reference_rule", "regex_rule", "manual"),
        minimum_trusted_confidence=0.9,
        implemented=True,
        derived_from_reference=True,
    ),
    "EXPLAINS": EdgeSpec(
        edge_type="EXPLAINS",
        from_node_types=(NODE_TYPE_DOCUMENT,),
        to_node_types=(NODE_TYPE_ARTICLE,),
        direction="guidance_to_article",
        inverse_display_name="解説される",
        normative=False,
        can_expand_search=True,
        can_satisfy_evidence=False,
        requires_fetched_target_text=True,
        allowed_sources=("guidance_article_annotation", "guidance_table_annotation", "manual"),
        minimum_trusted_confidence=0.8,
        implemented=True,
        from_doc_types=("guideline",),
    ),
    "MENTIONS": EdgeSpec(
        edge_type="MENTIONS",
        from_node_types=(NODE_TYPE_DOCUMENT,),
        to_node_types=(NODE_TYPE_ARTICLE,),
        direction="guidance_to_article",
        inverse_display_name="言及される",
        normative=False,
        # 単なる言及は候補発見の補助であり、探索拡張の信頼経路にはしない(§16.4)。
        can_expand_search=False,
        can_satisfy_evidence=False,
        requires_fetched_target_text=True,
        allowed_sources=("guidance_mention_rule", "manual"),
        minimum_trusted_confidence=0.5,
        implemented=True,
        from_doc_types=("guideline",),
    ),
    "DEFINES": EdgeSpec(
        edge_type="DEFINES",
        from_node_types=(NODE_TYPE_ARTICLE,),
        to_node_types=(NODE_TYPE_TERM,),
        direction="article_to_term",
        inverse_display_name="定義される",
        normative=True,
        can_expand_search=True,
        can_satisfy_evidence=False,
        requires_fetched_target_text=True,
        allowed_sources=("regex_rule", "llm_reviewed", "manual"),
        minimum_trusted_confidence=0.9,
        implemented=False,
    ),
    "USES_TERM": EdgeSpec(
        edge_type="USES_TERM",
        from_node_types=(NODE_TYPE_ARTICLE,),
        to_node_types=(NODE_TYPE_TERM,),
        direction="article_to_term",
        inverse_display_name="用語を使う",
        normative=False,
        can_expand_search=True,
        can_satisfy_evidence=False,
        requires_fetched_target_text=True,
        allowed_sources=("regex_rule", "llm_reviewed", "manual"),
        minimum_trusted_confidence=0.9,
        implemented=False,
    ),
    "EXCEPTION_TO": EdgeSpec(
        edge_type="EXCEPTION_TO",
        from_node_types=(NODE_TYPE_ARTICLE,),
        to_node_types=(NODE_TYPE_ARTICLE,),
        direction="exception_to_base",
        inverse_display_name="例外がある",
        normative=True,
        can_expand_search=True,
        can_satisfy_evidence=False,
        requires_fetched_target_text=True,
        allowed_sources=("regex_rule", "llm_reviewed", "manual"),
        minimum_trusted_confidence=0.9,
        implemented=False,
        derived_from_reference=True,
    ),
}

# 現時点でseed済みのエッジ種別。ドキュメント・コード・Neo4jの一致検査に使う(§6.3-13)。
SEEDED_EDGE_TYPES: tuple[str, ...] = tuple(
    name for name, spec in EDGE_REGISTRY.items() if spec.implemented
)


def edge_spec(edge_type: str) -> EdgeSpec | None:
    return EDGE_REGISTRY.get(str(edge_type or ""))


def expandable_edge_types() -> tuple[str, ...]:
    """探索拡張に使える実装済みエッジ種別だけを返す。

    未実装エッジを前提にした子Requirement生成を無効にするため、`implemented`を必ず見る。
    """
    return tuple(
        name
        for name, spec in EDGE_REGISTRY.items()
        if spec.implemented and spec.can_expand_search
    )


def validate_edge_endpoints(edge_type: str, from_node_type: str, to_node_type: str) -> bool:
    spec = edge_spec(edge_type)
    if spec is None:
        return False
    return from_node_type in spec.from_node_types and to_node_type in spec.to_node_types


def is_trusted_relation(edge: dict[str, Any]) -> bool:
    """探索拡張・根拠採用に使ってよい確定関係かを判定する(§6.1)。

    confidenceの数値だけでなく、relationSource、委任文言の検出結果、派生元エッジIDの
    存在、未確認assertionでないことも同時に検証する。
    """
    spec = edge_spec(str(edge.get("edgeType") or ""))
    if spec is None or not spec.implemented:
        return False
    if str(edge.get("status") or "") == RELATION_STATUS_UNVERIFIED:
        return False
    if str(edge.get("relationSource") or "") not in spec.allowed_sources:
        return False
    try:
        confidence = float(edge.get("relationConfidence"))
    except (TypeError, ValueError):
        return False
    if confidence < spec.minimum_trusted_confidence:
        return False
    if spec.derived_from_reference and not edge.get("derivedFromEdgeId"):
        return False
    if spec.requires_delegation_wording and not edge.get("delegationWordingDetected"):
        return False
    return True
