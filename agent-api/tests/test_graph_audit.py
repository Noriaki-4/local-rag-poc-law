"""Graph監査の単体テスト (計画書 §6.3, §16.1)。"""

from app.graph_audit import audit_graph, compare_edge_inventory, missing_edge_types
from app.legal_ontology import (
    AUTHORITY_ACT,
    AUTHORITY_CABINET_ORDER,
    GRAPH_SCHEMA_VERSION,
    IMPLEMENTS_CONFIDENCE_EXPLICIT_DELEGATION,
)


def _article_node(node_id: str, document_id: str, authority_type: str = AUTHORITY_ACT) -> dict:
    return {
        "graphNodeId": node_id,
        "nodeType": "Article",
        "documentId": document_id,
        "docType": "law",
        "authorityType": authority_type,
    }


def _document_node(node_id: str, doc_type: str = "law", authority_type: str = AUTHORITY_ACT) -> dict:
    return {
        "graphNodeId": node_id,
        "nodeType": "Document",
        "documentId": node_id,
        "docType": doc_type,
        "authorityType": authority_type,
    }


def _reference_edge(from_id: str, to_id: str, edge_id: str = "edge-ref-1") -> dict:
    return {
        "graphEdgeId": edge_id,
        "edgeType": "REFERENCES",
        "fromGraphNodeId": from_id,
        "toGraphNodeId": to_id,
        "documentId": "law-a",
        "relationSource": "xml_reference_rule",
        "relationConfidence": 0.9,
    }


def _rules(report) -> set[str]:
    return {violation["rule"] for violation in report.violations}


class TestCleanGraph:
    def test_valid_graph_has_no_violations(self) -> None:
        nodes = [
            _document_node("law-a"),
            _article_node("law-a-article-1", "law-a"),
            _article_node("law-b-article-2", "law-b", AUTHORITY_CABINET_ORDER),
            _document_node("law-b", authority_type=AUTHORITY_CABINET_ORDER),
        ]
        edges = [
            {
                "graphEdgeId": "edge-has-1",
                "edgeType": "HAS_CONTENT_UNIT",
                "fromGraphNodeId": "law-a",
                "toGraphNodeId": "law-a-article-1",
            },
            _reference_edge("law-b-article-2", "law-a-article-1"),
            {
                "graphEdgeId": "edge-implements-1",
                "edgeType": "IMPLEMENTS",
                "fromGraphNodeId": "law-a-article-1",
                "toGraphNodeId": "law-b-article-2",
                "derivedFromEdgeId": "edge-ref-1",
                "relationConfidence": IMPLEMENTS_CONFIDENCE_EXPLICIT_DELEGATION,
                "delegationWordingDetected": True,
                "relationSource": "subordinate_law_parent_reference",
            },
        ]
        report = audit_graph(nodes, edges)
        assert report.ok, report.violations
        assert report.graph_schema_version == GRAPH_SCHEMA_VERSION
        assert report.edge_type_counts["IMPLEMENTS"] == 1
        assert report.authority_type_counts[AUTHORITY_ACT] == 2


class TestViolations:
    def test_dangling_edge_is_detected(self) -> None:
        report = audit_graph(
            [_article_node("law-a-article-1", "law-a")],
            [_reference_edge("law-a-article-1", "law-a-article-999")],
        )
        assert "dangling_edge" in _rules(report)

    def test_derived_edge_without_source_is_detected(self) -> None:
        nodes = [_article_node("law-a-article-1", "law-a"), _article_node("law-b-article-2", "law-b")]
        edges = [
            {
                "graphEdgeId": "edge-implements-1",
                "edgeType": "IMPLEMENTS",
                "fromGraphNodeId": "law-a-article-1",
                "toGraphNodeId": "law-b-article-2",
            }
        ]
        assert "derived_edge_without_source" in _rules(audit_graph(nodes, edges))

    def test_missing_derived_source_edge_is_detected(self) -> None:
        nodes = [_article_node("law-a-article-1", "law-a"), _article_node("law-b-article-2", "law-b")]
        edges = [
            {
                "graphEdgeId": "edge-implements-1",
                "edgeType": "IMPLEMENTS",
                "fromGraphNodeId": "law-a-article-1",
                "toGraphNodeId": "law-b-article-2",
                "derivedFromEdgeId": "edge-not-there",
            }
        ]
        assert "derived_edge_source_missing" in _rules(audit_graph(nodes, edges))

    def test_invalid_endpoints_are_detected(self) -> None:
        nodes = [_document_node("law-a"), _article_node("law-b-article-2", "law-b")]
        edges = [
            {
                "graphEdgeId": "edge-implements-1",
                "edgeType": "IMPLEMENTS",
                "fromGraphNodeId": "law-a",
                "toGraphNodeId": "law-b-article-2",
                "derivedFromEdgeId": "edge-ref-1",
            },
            _reference_edge("law-b-article-2", "law-a", "edge-ref-1"),
        ]
        assert "invalid_edge_endpoints" in _rules(audit_graph(nodes, edges))

    def test_duplicate_edges_are_detected(self) -> None:
        nodes = [_article_node("law-a-article-1", "law-a"), _article_node("law-a-article-2", "law-a")]
        edges = [
            _reference_edge("law-a-article-1", "law-a-article-2", "edge-1"),
            _reference_edge("law-a-article-1", "law-a-article-2", "edge-2"),
        ]
        assert "duplicate_edge" in _rules(audit_graph(nodes, edges))

    def test_hierarchy_cycle_is_detected(self) -> None:
        nodes = [_article_node("law-a-article-1", "law-a"), _article_node("law-a-article-2", "law-a")]
        edges = [
            {
                "graphEdgeId": "edge-1",
                "edgeType": "HAS_CONTENT_UNIT",
                "fromGraphNodeId": "law-a-article-1",
                "toGraphNodeId": "law-a-article-2",
            },
            {
                "graphEdgeId": "edge-2",
                "edgeType": "HAS_CONTENT_UNIT",
                "fromGraphNodeId": "law-a-article-2",
                "toGraphNodeId": "law-a-article-1",
            },
        ]
        assert "hierarchy_cycle" in _rules(audit_graph(nodes, edges))

    def test_multiple_parents_are_detected(self) -> None:
        nodes = [
            _document_node("law-a"),
            _document_node("law-b"),
            _article_node("law-a-article-1", "law-a"),
        ]
        edges = [
            {
                "graphEdgeId": "edge-1",
                "edgeType": "HAS_CONTENT_UNIT",
                "fromGraphNodeId": "law-a",
                "toGraphNodeId": "law-a-article-1",
            },
            {
                "graphEdgeId": "edge-2",
                "edgeType": "HAS_CONTENT_UNIT",
                "fromGraphNodeId": "law-b",
                "toGraphNodeId": "law-a-article-1",
            },
        ]
        assert "multiple_content_unit_parents" in _rules(audit_graph(nodes, edges))

    def test_implements_within_same_document_is_detected(self) -> None:
        nodes = [_article_node("law-a-article-1", "law-a"), _article_node("law-a-article-2", "law-a")]
        edges = [
            _reference_edge("law-a-article-2", "law-a-article-1", "edge-ref-1"),
            {
                "graphEdgeId": "edge-implements-1",
                "edgeType": "IMPLEMENTS",
                "fromGraphNodeId": "law-a-article-1",
                "toGraphNodeId": "law-a-article-2",
                "derivedFromEdgeId": "edge-ref-1",
            },
        ]
        assert "implements_within_same_document" in _rules(audit_graph(nodes, edges))

    def test_guidance_edge_from_law_document_is_detected(self) -> None:
        nodes = [_document_node("law-a"), _article_node("law-a-article-1", "law-a")]
        edges = [
            {
                "graphEdgeId": "edge-explains-1",
                "edgeType": "EXPLAINS",
                "fromGraphNodeId": "law-a",
                "toGraphNodeId": "law-a-article-1",
            }
        ]
        assert "guidance_edge_from_non_guidance" in _rules(audit_graph(nodes, edges))

    def test_unverified_assertion_as_edge_is_detected(self) -> None:
        nodes = [_article_node("law-a-article-1", "law-a"), _article_node("law-b-article-2", "law-b")]
        edges = [
            _reference_edge("law-b-article-2", "law-a-article-1", "edge-ref-1"),
            {
                "graphEdgeId": "edge-implements-1",
                "edgeType": "IMPLEMENTS",
                "fromGraphNodeId": "law-a-article-1",
                "toGraphNodeId": "law-b-article-2",
                "derivedFromEdgeId": "edge-ref-1",
                "status": "unverified",
            },
        ]
        assert "unverified_assertion_stored_as_edge" in _rules(audit_graph(nodes, edges))

    def test_missing_authority_type_is_detected(self) -> None:
        nodes = [{"graphNodeId": "law-a-article-1", "nodeType": "Article", "documentId": "law-a"}]
        assert "missing_authority_type" in _rules(audit_graph(nodes, []))

    def test_unimplemented_edge_type_cannot_be_seeded(self) -> None:
        nodes = [_article_node("law-a-article-1", "law-a"), _article_node("law-a-article-2", "law-a")]
        edges = [
            {
                "graphEdgeId": "edge-1",
                "edgeType": "EXCEPTION_TO",
                "fromGraphNodeId": "law-a-article-2",
                "toGraphNodeId": "law-a-article-1",
            }
        ]
        assert "unimplemented_edge_type_seeded" in _rules(audit_graph(nodes, edges))


class TestEdgeInventoryComparison:
    def test_graph_only_edge_type_is_reported(self) -> None:
        violations = compare_edge_inventory(
            {
                "HAS_CONTENT_UNIT": 10,
                "REFERENCES": 5,
                "IMPLEMENTS": 2,
                "APPLIED_BY": 1,
                "EXPLAINS": 3,
                "MENTIONS": 1,
                "SUGGESTS_RELATION": 4,
            }
        )
        assert {violation["edgeType"] for violation in violations} == {"SUGGESTS_RELATION"}

    def test_edge_type_without_instances_is_not_a_violation(self) -> None:
        """コーパスに0件の種別は違反にしない(本当の不一致が埋もれるため)。"""
        assert compare_edge_inventory({"HAS_CONTENT_UNIT": 10}) == []
        assert "MENTIONS" in missing_edge_types({"HAS_CONTENT_UNIT": 10})
