"""seed直後のGraph自動検査 (計画書 §6.3)。

エッジの向き・端点・派生元・重複・authorityTypeの整合を、Neo4jへ投入する前の
node/edge配列に対して検査する。Graph schema versionを上げる変更を入れた際に、
「文書上の定義」と「実際にseedされるGraph」がずれていないかをここで検出する。
"""

from dataclasses import dataclass, field
from typing import Any

from .legal_ontology import (
    AUTHORITY_TYPES,
    GRAPH_SCHEMA_VERSION,
    NODE_TYPE_ARTICLE,
    NODE_TYPE_DOCUMENT,
    NODE_TYPE_RELATION_ASSERTION,
    RELATION_STATUS_UNVERIFIED,
    SEEDED_EDGE_TYPES,
    edge_spec,
    validate_edge_endpoints,
)

HIERARCHY_EDGE_TYPE = "HAS_CONTENT_UNIT"


@dataclass(frozen=True)
class GraphAuditReport:
    graph_schema_version: int = GRAPH_SCHEMA_VERSION
    node_type_counts: dict[str, int] = field(default_factory=dict)
    edge_type_counts: dict[str, int] = field(default_factory=dict)
    authority_type_counts: dict[str, int] = field(default_factory=dict)
    violations: tuple[dict[str, Any], ...] = ()

    @property
    def ok(self) -> bool:
        return not self.violations

    def as_dict(self) -> dict[str, Any]:
        return {
            "graphSchemaVersion": self.graph_schema_version,
            "nodeTypeCounts": self.node_type_counts,
            "edgeTypeCounts": self.edge_type_counts,
            "authorityTypeCounts": self.authority_type_counts,
            "violations": [dict(violation) for violation in self.violations],
            "ok": self.ok,
        }


def audit_graph(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> GraphAuditReport:
    """§6.3 の検査項目を実行し、違反の一覧を返す(例外は投げない)。"""
    violations: list[dict[str, Any]] = []
    nodes_by_id = {str(node.get("graphNodeId")): node for node in nodes}

    violations.extend(_dangling_edges(edges, nodes_by_id))
    violations.extend(_unregistered_edge_types(edges))
    violations.extend(_invalid_endpoints(edges, nodes_by_id))
    violations.extend(_missing_derived_from(edges))
    violations.extend(_duplicate_edges(edges))
    violations.extend(_hierarchy_cycles(edges))
    violations.extend(_multiple_parents(edges))
    violations.extend(_cross_family_implements(edges, nodes_by_id))
    violations.extend(_guidance_edge_endpoints(edges, nodes_by_id))
    violations.extend(_unverified_assertions_used_as_edges(edges))
    violations.extend(_invalid_relation_assertions(nodes, edges, nodes_by_id))
    violations.extend(_missing_authority_types(nodes))

    return GraphAuditReport(
        node_type_counts=_counts(nodes, "nodeType"),
        edge_type_counts=_counts(edges, "edgeType"),
        authority_type_counts=_counts(
            [node for node in nodes if node.get("nodeType") in (NODE_TYPE_DOCUMENT, NODE_TYPE_ARTICLE)],
            "authorityType",
        ),
        violations=tuple(violations),
    )


def _counts(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = str(item.get(key) or "missing")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _violation(rule: str, **details: Any) -> dict[str, Any]:
    return {"rule": rule, **details}


def _dangling_edges(
    edges: list[dict[str, Any]], nodes_by_id: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    return [
        _violation(
            "dangling_edge",
            graphEdgeId=edge.get("graphEdgeId"),
            edgeType=edge.get("edgeType"),
            missing=[
                node_id
                for node_id in (edge.get("fromGraphNodeId"), edge.get("toGraphNodeId"))
                if str(node_id) not in nodes_by_id
            ],
        )
        for edge in edges
        if str(edge.get("fromGraphNodeId")) not in nodes_by_id
        or str(edge.get("toGraphNodeId")) not in nodes_by_id
    ]


def _unregistered_edge_types(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """seedされるエッジ種別と、registryで実装済みとした種別を一致させる(§6.3-13)。"""
    violations = []
    for edge in edges:
        edge_type = str(edge.get("edgeType") or "")
        spec = edge_spec(edge_type)
        if spec is None:
            violations.append(_violation("unregistered_edge_type", edgeType=edge_type))
        elif not spec.implemented:
            violations.append(
                _violation("unimplemented_edge_type_seeded", edgeType=edge_type)
            )
    return violations


def _invalid_endpoints(
    edges: list[dict[str, Any]], nodes_by_id: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    violations = []
    for edge in edges:
        from_node = nodes_by_id.get(str(edge.get("fromGraphNodeId")))
        to_node = nodes_by_id.get(str(edge.get("toGraphNodeId")))
        if from_node is None or to_node is None:
            continue  # dangling として別途報告済み
        if not validate_edge_endpoints(
            str(edge.get("edgeType") or ""),
            str(from_node.get("nodeType") or ""),
            str(to_node.get("nodeType") or ""),
        ):
            violations.append(
                _violation(
                    "invalid_edge_endpoints",
                    graphEdgeId=edge.get("graphEdgeId"),
                    edgeType=edge.get("edgeType"),
                    fromNodeType=from_node.get("nodeType"),
                    toNodeType=to_node.get("nodeType"),
                )
            )
    return violations


def _missing_derived_from(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    edge_ids = {str(edge.get("graphEdgeId")) for edge in edges}
    violations = []
    for edge in edges:
        spec = edge_spec(str(edge.get("edgeType") or ""))
        if spec is None or not spec.derived_from_reference:
            continue
        derived_from = str(edge.get("derivedFromEdgeId") or "")
        if not derived_from:
            violations.append(
                _violation(
                    "derived_edge_without_source",
                    graphEdgeId=edge.get("graphEdgeId"),
                    edgeType=edge.get("edgeType"),
                )
            )
        elif derived_from not in edge_ids:
            violations.append(
                _violation(
                    "derived_edge_source_missing",
                    graphEdgeId=edge.get("graphEdgeId"),
                    derivedFromEdgeId=derived_from,
                )
            )
    return violations


def _duplicate_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    violations = []
    for edge in edges:
        key = (
            str(edge.get("edgeType")),
            str(edge.get("fromGraphNodeId")),
            str(edge.get("toGraphNodeId")),
        )
        if key in seen:
            violations.append(
                _violation("duplicate_edge", edgeType=key[0], fromGraphNodeId=key[1], toGraphNodeId=key[2])
            )
        seen.add(key)
    return violations


def _hierarchy_cycles(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    children: dict[str, list[str]] = {}
    for edge in edges:
        if edge.get("edgeType") != HIERARCHY_EDGE_TYPE:
            continue
        children.setdefault(str(edge.get("fromGraphNodeId")), []).append(
            str(edge.get("toGraphNodeId"))
        )

    visiting: set[str] = set()
    done: set[str] = set()
    violations: list[dict[str, Any]] = []

    def walk(node_id: str, stack: tuple[str, ...]) -> None:
        if node_id in visiting:
            violations.append(_violation("hierarchy_cycle", path=[*stack, node_id]))
            return
        if node_id in done:
            return
        visiting.add(node_id)
        for child in children.get(node_id, []):
            walk(child, (*stack, node_id))
        visiting.discard(node_id)
        done.add(node_id)

    for root in list(children):
        walk(root, ())
    return violations


def _multiple_parents(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parents: dict[str, set[str]] = {}
    for edge in edges:
        if edge.get("edgeType") != HIERARCHY_EDGE_TYPE:
            continue
        parents.setdefault(str(edge.get("toGraphNodeId")), set()).add(
            str(edge.get("fromGraphNodeId"))
        )
    return [
        _violation("multiple_content_unit_parents", contentUnitId=child, parents=sorted(values))
        for child, values in parents.items()
        if len(values) > 1
    ]


def _cross_family_implements(
    edges: list[dict[str, Any]], nodes_by_id: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """IMPLEMENTSが同一法令系統の親へ接続しているか(§6.3-7)。

    法令系統の判定材料が無い場合は検査せず、少なくとも親子が別法令であることを確認する。
    """
    violations = []
    for edge in edges:
        if edge.get("edgeType") != "IMPLEMENTS":
            continue
        from_node = nodes_by_id.get(str(edge.get("fromGraphNodeId")))
        to_node = nodes_by_id.get(str(edge.get("toGraphNodeId")))
        if from_node is None or to_node is None:
            continue
        if from_node.get("documentId") == to_node.get("documentId"):
            violations.append(
                _violation(
                    "implements_within_same_document",
                    graphEdgeId=edge.get("graphEdgeId"),
                    documentId=from_node.get("documentId"),
                )
            )
    return violations


def _guidance_edge_endpoints(
    edges: list[dict[str, Any]], nodes_by_id: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    violations = []
    for edge in edges:
        if edge.get("edgeType") not in {"EXPLAINS", "MENTIONS"}:
            continue
        from_node = nodes_by_id.get(str(edge.get("fromGraphNodeId")))
        if from_node is None:
            continue
        if from_node.get("docType") != "guideline":
            violations.append(
                _violation(
                    "guidance_edge_from_non_guidance",
                    graphEdgeId=edge.get("graphEdgeId"),
                    docType=from_node.get("docType"),
                )
            )
    return violations


def _unverified_assertions_used_as_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """未確認のRelationAssertionが確定エッジとして保存されていないか(§6.3-9)。"""
    return [
        _violation("unverified_assertion_stored_as_edge", graphEdgeId=edge.get("graphEdgeId"))
        for edge in edges
        if str(edge.get("status") or "") == RELATION_STATUS_UNVERIFIED
    ]


def _invalid_relation_assertions(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    nodes_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """候補関係の端点・出所・未確認状態が自己矛盾していないか検査する。"""
    edges_by_id = {
        str(edge.get("graphEdgeId") or ""): edge
        for edge in edges
        if edge.get("graphEdgeId")
    }
    violations: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for assertion in nodes:
        if assertion.get("nodeType") != NODE_TYPE_RELATION_ASSERTION:
            continue
        assertion_id = str(
            assertion.get("assertionId")
            or assertion.get("graphNodeId")
            or ""
        )
        from_article_id = str(assertion.get("fromArticleId") or "")
        to_article_id = str(assertion.get("toArticleId") or "")
        suggested_type = str(assertion.get("suggestedType") or "")
        source_reference_edge_id = str(
            assertion.get("sourceReferenceEdgeId") or ""
        )
        missing = [
            article_id
            for article_id in (from_article_id, to_article_id)
            if (
                article_id not in nodes_by_id
                or nodes_by_id[article_id].get("nodeType") != NODE_TYPE_ARTICLE
            )
        ]
        if missing:
            violations.append(
                _violation(
                    "relation_assertion_invalid_endpoint",
                    assertionId=assertion_id,
                    missing=missing,
                )
            )
        if str(assertion.get("status") or "") != RELATION_STATUS_UNVERIFIED:
            violations.append(
                _violation(
                    "relation_assertion_not_unverified",
                    assertionId=assertion_id,
                    status=assertion.get("status"),
                )
            )
        spec = edge_spec(suggested_type)
        if spec is None or not spec.implemented:
            violations.append(
                _violation(
                    "relation_assertion_unknown_suggested_type",
                    assertionId=assertion_id,
                    suggestedType=suggested_type,
                )
            )
        if source_reference_edge_id:
            reference = edges_by_id.get(source_reference_edge_id)
            if (
                reference is None
                or reference.get("edgeType") != "REFERENCES"
                or str(reference.get("fromGraphNodeId") or "")
                != to_article_id
                or str(reference.get("toGraphNodeId") or "")
                != from_article_id
            ):
                violations.append(
                    _violation(
                        "relation_assertion_invalid_source_reference",
                        assertionId=assertion_id,
                        sourceReferenceEdgeId=source_reference_edge_id,
                    )
                )
        key = (
            str(assertion.get("assertionSource") or ""),
            from_article_id,
            suggested_type,
            to_article_id,
        )
        if key in seen:
            violations.append(
                _violation(
                    "duplicate_relation_assertion",
                    assertionId=assertion_id,
                    fromArticleId=from_article_id,
                    suggestedType=suggested_type,
                    toArticleId=to_article_id,
                )
            )
        seen.add(key)
    return violations


def _missing_authority_types(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """全Law/Articleに`authorityType`があるか、`unknown`として明示されているか(§6.3-11)。"""
    violations = []
    for node in nodes:
        if node.get("nodeType") not in (NODE_TYPE_DOCUMENT, NODE_TYPE_ARTICLE):
            continue
        authority_type = node.get("authorityType")
        if not authority_type:
            violations.append(
                _violation("missing_authority_type", graphNodeId=node.get("graphNodeId"))
            )
        elif authority_type not in AUTHORITY_TYPES:
            violations.append(
                _violation(
                    "unknown_authority_type",
                    graphNodeId=node.get("graphNodeId"),
                    authorityType=authority_type,
                )
            )
    return violations


def compare_edge_inventory(seeded: dict[str, int]) -> list[dict[str, Any]]:
    """Neo4jの実データとregistryの実装済みエッジ種別を突き合わせる(§6.3-13, Phase 0)。

    registryにあってGraphに0件の種別は違反にしない。抽出条件を満たす資料がコーパスに
    無いだけの場合があり(ガイドの`MENTIONS`など)、それを常に不一致として扱うと
    本当の不一致(Graphにあるのにregistryに無い/未実装のはずの種別が入っている)が埋もれる。
    """
    return [
        _violation("edge_type_in_graph_but_not_registry", edgeType=edge_type)
        for edge_type in seeded
        if edge_type not in SEEDED_EDGE_TYPES
    ]


def missing_edge_types(seeded: dict[str, int]) -> list[str]:
    """registryでは実装済みだが、このコーパスでは1件も生成されなかった種別。"""
    return [edge_type for edge_type in SEEDED_EDGE_TYPES if edge_type not in seeded]
