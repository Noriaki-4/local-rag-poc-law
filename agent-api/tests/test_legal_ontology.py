"""legal_ontology の単体テスト (計画書 §16.1 オントロジー単体テスト)。"""

import pytest

from app.legal_ontology import (
    AUTHORITY_ACT,
    AUTHORITY_CABINET_OFFICE_ORDINANCE,
    AUTHORITY_CABINET_ORDER,
    AUTHORITY_GUIDANCE,
    AUTHORITY_MINISTERIAL_ORDINANCE,
    AUTHORITY_ORDINANCE_UNSPECIFIED,
    AUTHORITY_UNKNOWN,
    EDGE_REGISTRY,
    GRAPH_SCHEMA_VERSION,
    IMPLEMENTS_CONFIDENCE_EXPLICIT_DELEGATION,
    IMPLEMENTS_CONFIDENCE_FAMILY_RULE,
    REFERENCE_ONLY_CONFIDENCE,
    ROLE_FAMILIES,
    authority_type_rank,
    edge_spec,
    expandable_edge_types,
    implements_confidence,
    is_trusted_relation,
    normalize_role_subtypes,
    resolve_authority_type,
    search_authority_types,
    validate_edge_endpoints,
)


class TestAuthorityTypeResolution:
    def test_registry_value_wins(self) -> None:
        resolution = resolve_authority_type(
            law_id="402M50000040038",
            registry_authority_type=AUTHORITY_CABINET_OFFICE_ORDINANCE,
            law_type="MinisterialOrdinance",
            title="発行者以外の者による株券等の公開買付けの開示に関する内閣府令",
        )
        assert resolution.authority_type == AUTHORITY_CABINET_OFFICE_ORDINANCE
        assert resolution.authority_source == "registry_manual_verified"

    def test_act_and_cabinet_order_from_law_id(self) -> None:
        act = resolve_authority_type(law_id="323AC0000000025")
        order = resolve_authority_type(law_id="340CO0000000321")
        assert act.authority_type == AUTHORITY_ACT
        assert act.authority_source == "law_id"
        assert order.authority_type == AUTHORITY_CABINET_ORDER

    def test_m_series_is_not_confirmed_as_either_ordinance(self) -> None:
        """LawType=MinisterialOrdinance だけで内閣府令・省令を確定しない (§5.2)。"""
        resolution = resolve_authority_type(
            law_id="419M60000002052",
            law_type="MinisterialOrdinance",
            title="金融商品取引業等に関する内閣府令",
        )
        assert resolution.authority_type == AUTHORITY_ORDINANCE_UNSPECIFIED
        assert resolution.authority_source == "law_id"
        # タイトル由来は監査用の候補値であり、確定値にしない。
        assert resolution.candidate_authority_type == AUTHORITY_CABINET_OFFICE_ORDINANCE
        assert resolution.needs_manual_review is True

    def test_ministerial_candidate_from_title(self) -> None:
        resolution = resolve_authority_type(
            law_id="336M50000100001",
            title="医薬品、医療機器等の品質、有効性及び安全性の確保等に関する法律施行規則",
        )
        assert resolution.authority_type == AUTHORITY_ORDINANCE_UNSPECIFIED
        assert resolution.candidate_authority_type == AUTHORITY_MINISTERIAL_ORDINANCE

    def test_unknown_when_undecidable(self) -> None:
        resolution = resolve_authority_type(law_id=None, title="なにかの資料")
        assert resolution.authority_type == AUTHORITY_UNKNOWN
        assert resolution.needs_manual_review is True

    def test_guidance_doc_type(self) -> None:
        resolution = resolve_authority_type(law_id="guidance-001", doc_type="guideline")
        assert resolution.authority_type == AUTHORITY_GUIDANCE
        assert resolution.needs_manual_review is False

    def test_registry_rejects_invalid_value(self) -> None:
        with pytest.raises(ValueError):
            resolve_authority_type(law_id="323AC0000000025", registry_authority_type="政令")


class TestSearchInclusionRules:
    def test_ministerial_includes_unspecified_and_unknown(self) -> None:
        assert search_authority_types(AUTHORITY_MINISTERIAL_ORDINANCE) == (
            AUTHORITY_MINISTERIAL_ORDINANCE,
            AUTHORITY_ORDINANCE_UNSPECIFIED,
            AUTHORITY_UNKNOWN,
        )

    def test_cabinet_office_includes_unspecified_and_unknown(self) -> None:
        assert search_authority_types(AUTHORITY_CABINET_OFFICE_ORDINANCE) == (
            AUTHORITY_CABINET_OFFICE_ORDINANCE,
            AUTHORITY_ORDINANCE_UNSPECIFIED,
            AUTHORITY_UNKNOWN,
        )

    def test_unspecified_includes_both_ordinances(self) -> None:
        assert search_authority_types(AUTHORITY_ORDINANCE_UNSPECIFIED) == (
            AUTHORITY_MINISTERIAL_ORDINANCE,
            AUTHORITY_CABINET_OFFICE_ORDINANCE,
            AUTHORITY_ORDINANCE_UNSPECIFIED,
            AUTHORITY_UNKNOWN,
        )

    def test_act_is_exact(self) -> None:
        assert search_authority_types(AUTHORITY_ACT) == (AUTHORITY_ACT,)

    def test_none_means_no_filter(self) -> None:
        assert search_authority_types(None) == ()

    def test_exact_match_ranks_first(self) -> None:
        assert authority_type_rank(
            AUTHORITY_CABINET_OFFICE_ORDINANCE, AUTHORITY_CABINET_OFFICE_ORDINANCE
        ) == 0
        assert authority_type_rank(
            AUTHORITY_CABINET_OFFICE_ORDINANCE, AUTHORITY_ORDINANCE_UNSPECIFIED
        ) == 1
        assert authority_type_rank(AUTHORITY_CABINET_OFFICE_ORDINANCE, AUTHORITY_UNKNOWN) == 2


class TestEdgeRegistry:
    def test_current_five_edges_are_implemented(self) -> None:
        implemented = {name for name, spec in EDGE_REGISTRY.items() if spec.implemented}
        assert {
            "HAS_CONTENT_UNIT",
            "REFERENCES",
            "IMPLEMENTS",
            "APPLIED_BY",
            "EXPLAINS",
        } <= implemented

    def test_unimplemented_edges_are_declared_but_disabled(self) -> None:
        for edge_type in ("DEFINES", "USES_TERM", "EXCEPTION_TO"):
            spec = edge_spec(edge_type)
            assert spec is not None
            assert spec.implemented is False
        assert "DEFINES" not in expandable_edge_types()

    def test_expandable_edge_types_exclude_hierarchy_only_edges(self) -> None:
        expandable = expandable_edge_types()
        assert "IMPLEMENTS" in expandable
        assert "APPLIED_BY" in expandable
        assert "MENTIONS" not in expandable

    def test_endpoint_validation(self) -> None:
        assert validate_edge_endpoints("IMPLEMENTS", "Article", "Article") is True
        assert validate_edge_endpoints("IMPLEMENTS", "Document", "Article") is False
        assert validate_edge_endpoints("EXPLAINS", "Document", "Article") is True
        assert validate_edge_endpoints("EXPLAINS", "Article", "Article") is False

    def test_relation_assertion_cannot_satisfy_evidence(self) -> None:
        assert edge_spec("IMPLEMENTS").can_satisfy_evidence is False
        assert edge_spec("MENTIONS").can_satisfy_evidence is False

    def test_schema_version_is_tracked(self) -> None:
        assert isinstance(GRAPH_SCHEMA_VERSION, int)
        assert GRAPH_SCHEMA_VERSION >= 2


class TestTrustedRelations:
    def _implements_edge(self, **overrides: object) -> dict[str, object]:
        edge = {
            "edgeType": "IMPLEMENTS",
            "relationSource": "regex_rule",
            "relationConfidence": IMPLEMENTS_CONFIDENCE_EXPLICIT_DELEGATION,
            "derivedFromEdgeId": "edge-x-references-y",
            "delegationWordingDetected": True,
        }
        edge.update(overrides)
        return edge

    def test_trusted_implements(self) -> None:
        assert is_trusted_relation(self._implements_edge()) is True

    def test_plain_reference_is_not_trusted_implements(self) -> None:
        """委任文言のない単純参照を高信頼 IMPLEMENTS にしない (§6.1)。"""
        edge = self._implements_edge(
            relationConfidence=REFERENCE_ONLY_CONFIDENCE,
            delegationWordingDetected=False,
        )
        assert is_trusted_relation(edge) is False

    def test_derived_edge_requires_source_edge_id(self) -> None:
        edge = self._implements_edge(derivedFromEdgeId=None)
        assert is_trusted_relation(edge) is False

    def test_unknown_relation_source_is_rejected(self) -> None:
        edge = self._implements_edge(relationSource="llm_guess")
        assert is_trusted_relation(edge) is False

    def test_unimplemented_edge_type_is_never_trusted(self) -> None:
        edge = {
            "edgeType": "EXCEPTION_TO",
            "relationSource": "regex_rule",
            "relationConfidence": 1.0,
        }
        assert is_trusted_relation(edge) is False

    def test_unverified_assertion_is_not_trusted(self) -> None:
        edge = {
            "edgeType": "IMPLEMENTS",
            "relationSource": "guidance_assertion",
            "relationConfidence": 0.6,
            "status": "unverified",
        }
        assert is_trusted_relation(edge) is False


class TestImplementsConfidence:
    def test_explicit_delegation_wording(self) -> None:
        assert (
            implements_confidence(delegation_wording=True, same_family=True)
            == IMPLEMENTS_CONFIDENCE_EXPLICIT_DELEGATION
        )

    def test_family_rule_without_wording(self) -> None:
        assert (
            implements_confidence(delegation_wording=False, same_family=True, specification_wording=True)
            == IMPLEMENTS_CONFIDENCE_FAMILY_RULE
        )

    def test_reference_only(self) -> None:
        assert (
            implements_confidence(delegation_wording=False, same_family=True, specification_wording=False)
            == REFERENCE_ONLY_CONFIDENCE
        )

    def test_manual_overrides(self) -> None:
        assert implements_confidence(manual=True) == 1.0


class TestRoleRegistry:
    def test_role_families_cover_plan(self) -> None:
        assert {
            "normative_rule",
            "qualification",
            "meaning_scope",
            "procedure",
            "consequence",
            "linkage",
            "temporal",
            "interpretive",
        } == set(ROLE_FAMILIES)

    def test_delegated_detail_is_not_a_role(self) -> None:
        for subtypes in ROLE_FAMILIES.values():
            assert "delegated_detail" not in subtypes

    def test_normalize_drops_unknown_subtypes(self) -> None:
        assert normalize_role_subtypes("qualification", ["exception", "delegated_detail"]) == ("exception",)

    def test_normalize_rejects_unknown_family(self) -> None:
        with pytest.raises(ValueError):
            normalize_role_subtypes("not_a_family", ["exception"])
