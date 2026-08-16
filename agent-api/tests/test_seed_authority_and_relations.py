"""Phase 2 のseed変更(authorityType / referenceKind / 段階的IMPLEMENTS / MENTIONS /
RelationAssertion)のテスト (計画書 §5.2, §6.1, §16.1, §16.4)。"""

from app.graph_audit import audit_graph
from app.legal_ontology import (
    AUTHORITY_ACT,
    AUTHORITY_CABINET_OFFICE_ORDINANCE,
    AUTHORITY_CABINET_ORDER,
    AUTHORITY_GUIDANCE,
    AUTHORITY_ORDINANCE_UNSPECIFIED,
    GRAPH_SCHEMA_VERSION,
    REFERENCE_KIND_APPLICATION,
    REFERENCE_KIND_PARENT_LAW_REFERENCE,
    RELATION_STATUS_UNVERIFIED,
    UNVERIFIED_ASSERTION_CONFIDENCE,
)
from app.seed import (
    _delegation_edges,
    _delegation_relation_assertions,
    _graph_artifacts_from_documents,
    _guidance_graph_artifacts,
    _guidance_relation_assertions,
    _incorporation_edges,
    _reference_edges,
    _with_authority_type,
)


def _law_document(document_id: str, article: str, text: str, **overrides: object) -> dict:
    document = {
        "documentId": document_id,
        "contentUnitId": f"{document_id}-article-{article}",
        "articleContentUnitId": f"{document_id}-article-{article}",
        "docType": "law",
        "text": text,
        "title": "検証法",
        "authorityType": AUTHORITY_ACT,
        "authoritySource": "law_id",
    }
    document.update(overrides)
    return document


class TestAuthorityTypeOnDocuments:
    def test_act_is_resolved_from_law_id(self) -> None:
        document = _with_authority_type(
            {"documentId": "law-323AC0000000025", "docType": "law", "title": "金融商品取引法"}, {}
        )
        assert document["authorityType"] == AUTHORITY_ACT
        assert document["authoritySource"] == "law_id"

    def test_registry_value_decides_cabinet_office_ordinance(self) -> None:
        registry = {"402M50000040038": {"authorityType": AUTHORITY_CABINET_OFFICE_ORDINANCE}}
        document = _with_authority_type(
            {
                "documentId": "law-402M50000040038",
                "docType": "law",
                "title": "発行者以外の者による株券等の公開買付けの開示に関する内閣府令",
            },
            registry,
        )
        assert document["authorityType"] == AUTHORITY_CABINET_OFFICE_ORDINANCE
        assert document["authoritySource"] == "registry_manual_verified"

    def test_m_series_without_registry_stays_unspecified(self) -> None:
        document = _with_authority_type(
            {"documentId": "law-419M60000002052", "docType": "law", "title": "検証府令"}, {}
        )
        assert document["authorityType"] == AUTHORITY_ORDINANCE_UNSPECIFIED

    def test_existing_value_is_kept(self) -> None:
        document = _with_authority_type(
            {"documentId": "law-x", "authorityType": AUTHORITY_CABINET_ORDER}, {}
        )
        assert document["authorityType"] == AUTHORITY_CABINET_ORDER


class TestGraphNodeProperties:
    def test_nodes_carry_authority_type_and_schema_version(self) -> None:
        documents = [_law_document("law-test", "1", "第一条 目的を定める。")]
        nodes, _ = _graph_artifacts_from_documents(documents, {"law-test": "law-test"})
        by_type = {node["nodeType"]: node for node in nodes}
        assert by_type["Document"]["authorityType"] == AUTHORITY_ACT
        assert by_type["Article"]["authorityType"] == AUTHORITY_ACT
        assert by_type["Article"]["graphSchemaVersion"] == GRAPH_SCHEMA_VERSION

    def test_paragraph_and_item_nodes_also_carry_schema_version(self) -> None:
        documents = [
            _law_document(
                "law-test",
                "1",
                "第一条第一項第一号 本文。",
                contentUnitId="law-test-article-1-paragraph-1-item-1",
                articleContentUnitId="law-test-article-1",
                parentContentUnitId="law-test-article-1-paragraph-1",
                paragraphNumber=1,
                itemNumber=1,
            )
        ]
        nodes, _ = _graph_artifacts_from_documents(
            documents,
            {"law-test": "law-test"},
        )

        assert nodes
        assert all(
            node["graphSchemaVersion"] == GRAPH_SCHEMA_VERSION
            for node in nodes
        )

    def test_article_heading_is_not_overwritten_by_last_paragraph(self) -> None:
        documents = [
            _law_document(
                "law-test",
                "27_2",
                "第一項本文。",
                contentUnitId="law-test-article-27_2-paragraph-1",
                heading="第二十七条の二 （公開買付け） 第1項",
            ),
            _law_document(
                "law-test",
                "27_2",
                "第八項本文。",
                contentUnitId="law-test-article-27_2-paragraph-8",
                heading="第二十七条の二 （公開買付け） 第8項",
            ),
        ]

        nodes, _ = _graph_artifacts_from_documents(
            documents,
            {"law-test": "law-test"},
        )
        article = next(
            node
            for node in nodes
            if node["graphNodeId"] == "law-test-article-27_2"
        )

        assert article["heading"] == "第二十七条の二 （公開買付け）"

    def test_full_graph_build_keeps_parent_reference_as_audited_candidate(
        self,
    ) -> None:
        parent = _law_document(
            "law-test",
            "5",
            "第五条 対象は政令で定める。",
        )
        child = _law_document(
            "law-order",
            "2",
            "法第五条に規定する対象を定める。",
            title="検証法施行令",
            authorityType=AUTHORITY_CABINET_ORDER,
        )

        nodes, edges = _graph_artifacts_from_documents(
            [parent, child],
            {"law-test": "law-test", "law-order": "law-test"},
        )

        assert audit_graph(nodes, edges).ok
        assert not any(edge["edgeType"] == "IMPLEMENTS" for edge in edges)
        reference = next(
            edge
            for edge in edges
            if edge.get("relationSource")
            == "subordinate_law_parent_reference"
        )
        assertion = next(
            node
            for node in nodes
            if node.get("nodeType") == "RelationAssertion"
        )
        assert reference["referenceKind"] == (
            REFERENCE_KIND_PARENT_LAW_REFERENCE
        )
        assert assertion["status"] == RELATION_STATUS_UNVERIFIED
        assert assertion["sourceReferenceEdgeId"] == reference["graphEdgeId"]


class TestReferenceKind:
    def test_reference_edges_get_reference_kind(self) -> None:
        documents = [
            _law_document("law-test", "1", "第一条 基本要件を定める。"),
            _law_document("law-test", "2", "第二条 第一条の規定を準用する。"),
        ]
        edges = _reference_edges(documents)
        kinds = {edge["referenceKind"] for edge in edges}
        assert REFERENCE_KIND_APPLICATION in kinds

    def test_parent_law_reference_is_delegation_parent(self) -> None:
        parent = _law_document("law-test", "5", "第五条 政令で定めるものを除く。")
        child = _law_document(
            "law-order",
            "2_13",
            "法第五条第一項に規定する政令で定める有価証券を定める。",
            authorityType=AUTHORITY_CABINET_ORDER,
        )
        edges = _delegation_edges([parent, child], {"law-test": "law-test", "law-order": "law-test"})
        reference = next(edge for edge in edges if edge["edgeType"] == "REFERENCES")
        assert reference["referenceKind"] == REFERENCE_KIND_PARENT_LAW_REFERENCE


class TestGradedImplements:
    def test_explicit_delegation_produces_unverified_candidate_with_signals(self) -> None:
        parent = _law_document("law-test", "5", "第五条 政令で定めるものを除く。")
        child = _law_document(
            "law-order",
            "2_13",
            "法第五条第一項に規定する政令で定める有価証券を定める。",
            authorityType=AUTHORITY_CABINET_ORDER,
        )
        edges = _delegation_edges([parent, child], {"law-test": "law-test", "law-order": "law-test"})
        assert [edge["edgeType"] for edge in edges] == ["REFERENCES"]
        candidate = _delegation_relation_assertions(
            [parent, child],
            {"law-test": "law-test", "law-order": "law-test"},
        )[0]
        assert candidate["suggestedType"] == "IMPLEMENTS"
        assert candidate["confidence"] == UNVERIFIED_ASSERTION_CONFIDENCE
        assert candidate["status"] == RELATION_STATUS_UNVERIFIED
        assert candidate["delegationWordingDetected"] is True
        assert candidate["sourceReferenceEdgeId"] == (
            "edge-law-order-article-2_13-references-law-test-article-5"
        )

    def test_plain_parent_reference_does_not_create_implements(self) -> None:
        """単純な親条文参照だけの候補を高信頼IMPLEMENTSとして通さない (§6.1)。"""
        parent = _law_document("law-test", "5", "第五条 有価証券の売買を行うことができる。")
        child = _law_document(
            "law-order",
            "2_13",
            "法第五条を参照。",
            authorityType=AUTHORITY_CABINET_ORDER,
        )
        edges = _delegation_edges([parent, child], {"law-test": "law-test", "law-order": "law-test"})
        assert [edge["edgeType"] for edge in edges] == ["REFERENCES"]
        candidates = _delegation_relation_assertions(
            [parent, child],
            {"law-test": "law-test", "law-order": "law-test"},
        )
        assert len(candidates) == 1
        assert candidates[0]["delegationWordingDetected"] is False
        assert candidates[0]["specificationWordingDetected"] is False

    def test_definition_reference_does_not_create_false_implements(self) -> None:
        parent = _law_document(
            "law-test",
            "27_2",
            "第一項ただし書に規定する政令で定める場合を除く。第七項は特別関係者を定める。",
        )
        child = _law_document(
            "law-order",
            "12",
            (
                "法第二十七条の二第七項に規定する特別関係者をいい、"
                "法第二十七条の五第二号の規定による申出をした者を除く。"
            ),
            authorityType=AUTHORITY_CABINET_ORDER,
        )

        edges = _delegation_edges(
            [parent, child],
            {"law-test": "law-test", "law-order": "law-test"},
        )

        assert [edge["edgeType"] for edge in edges] == ["REFERENCES"]

    def test_applied_by_records_source_reference(self) -> None:
        documents = [
            _law_document("law-test", "1", "第一条 基本要件を定める。"),
            _law_document("law-test", "2", "第二条 第一条の規定を準用する。"),
        ]
        references = _reference_edges(documents)
        edges = _incorporation_edges(documents, references)
        assert edges[0]["derivedFromEdgeId"] in {edge["graphEdgeId"] for edge in references}


def _guideline_chunk(content_unit_id: str, related: list[str], source: str | None, text: str = "") -> dict:
    return {
        "documentId": "guidance-test",
        "contentUnitId": content_unit_id,
        "docType": "guideline",
        "deptCode": "common",
        "contentDomain": "legal_guidance",
        "title": "検証ガイドライン",
        "publishStatus": "published",
        "isLatest": True,
        "confidentiality": "public",
        "clearanceLevel": 1,
        "text": text,
        "relatedArticleContentUnitIds": related,
        "articleReferenceSource": source,
    }


class TestGuidanceEdges:
    def test_explicit_annotation_becomes_explains(self) -> None:
        documents = [
            _guideline_chunk("c1", ["law-a-article-1"], "guideline_relation_annotation"),
        ]
        nodes, edges = _guidance_graph_artifacts(documents)
        assert [edge["edgeType"] for edge in edges] == ["EXPLAINS"]
        assert nodes[0]["authorityType"] == AUTHORITY_GUIDANCE

    def test_carried_forward_reference_becomes_mentions(self) -> None:
        """引き継ぎ参照は明示的な解説対象ではないため MENTIONS にする (§6.1)。"""
        documents = [_guideline_chunk("c1", ["law-a-article-1"], "carried_forward")]
        _, edges = _guidance_graph_artifacts(documents)
        assert [edge["edgeType"] for edge in edges] == ["MENTIONS"]
        assert edges[0]["relationConfidence"] == 0.5

    def test_article_with_both_sources_stays_explains(self) -> None:
        documents = [
            _guideline_chunk("c1", ["law-a-article-1"], "carried_forward"),
            _guideline_chunk("c2", ["law-a-article-1"], "guideline_table_annotation"),
        ]
        _, edges = _guidance_graph_artifacts(documents)
        assert [edge["edgeType"] for edge in edges] == ["EXPLAINS"]


class TestRelationAssertions:
    def test_guidance_table_pairs_articles_only_within_same_row(self) -> None:
        table = _guideline_chunk(
            "table-1",
            ["law-test-article-5", "law-test-article-6"],
            "guideline_table_annotation",
            text=(
                "| 法律 | 施行令 |\n"
                "| --- | --- |\n"
                "| 法第5条 | 令第7条 |\n"
                "| 法第6条 | 令第8条 |"
            ),
        )
        table["chunkStrategy"] = "guidance_docling_table_v1"
        documents = [
            _law_document("law-test", "5", "第五条 政令で定める。"),
            _law_document("law-test", "6", "第六条 政令で定める。"),
            _law_document(
                "law-order", "7", "第七条 対象。",
                authorityType=AUTHORITY_CABINET_ORDER,
                title="検証法施行令",
            ),
            _law_document(
                "law-order", "8", "第八条 手続。",
                authorityType=AUTHORITY_CABINET_ORDER,
                title="検証法施行令",
            ),
            table,
        ]

        assertions = _guidance_relation_assertions(
            documents, {"law-order": "law-test"}
        )

        assert {
            (item["fromArticleId"], item["toArticleId"])
            for item in assertions
        } == {
            ("law-test-article-5", "law-order-article-7"),
            ("law-test-article-6", "law-order-article-8"),
        }

    def test_guidance_suggested_relation_is_unverified_node(self) -> None:
        documents = [
            _law_document("law-test", "5", "第五条 政令で定める。"),
            _law_document(
                "law-order", "7", "第七条 対象を定める。", authorityType=AUTHORITY_CABINET_ORDER,
                title="検証法施行令",
            ),
            _guideline_chunk(
                "c1",
                ["law-test-article-5"],
                "guideline_relation_annotation",
                text="法第五条の適用対象は令第七条に定めるところによる。",
            ),
        ]
        assertions = _guidance_relation_assertions(documents, {"law-order": "law-test"})
        assert len(assertions) == 1
        assertion = assertions[0]
        assert assertion["nodeType"] == "RelationAssertion"
        assert assertion["fromArticleId"] == "law-test-article-5"
        assert assertion["toArticleId"] == "law-order-article-7"
        assert assertion["suggestedType"] == "IMPLEMENTS"
        assert assertion["status"] == RELATION_STATUS_UNVERIFIED
        assert assertion["confidence"] == 0.5

    def test_assertion_is_not_created_for_missing_target_article(self) -> None:
        documents = [
            _law_document("law-test", "5", "第五条 政令で定める。"),
            _law_document(
                "law-order", "1", "第一条 目的。", authorityType=AUTHORITY_CABINET_ORDER,
                title="検証法施行令",
            ),
            _guideline_chunk(
                "c1",
                ["law-test-article-5"],
                "guideline_relation_annotation",
                text="令第九十九条に定めるところによる。",
            ),
        ]
        assert _guidance_relation_assertions(documents, {"law-order": "law-test"}) == []

    def test_assertions_are_not_edges(self) -> None:
        documents = [
            _law_document("law-test", "5", "第五条 政令で定める。"),
            _law_document(
                "law-order", "7", "第七条 対象を定める。", authorityType=AUTHORITY_CABINET_ORDER,
                title="検証法施行令",
            ),
            _guideline_chunk(
                "c1",
                ["law-test-article-5"],
                "guideline_relation_annotation",
                text="令第七条に定めるところによる。",
            ),
        ]
        nodes, edges = _guidance_graph_artifacts(documents, {"law-order": "law-test"})
        assert any(node["nodeType"] == "RelationAssertion" for node in nodes)
        assert all(edge["edgeType"] in {"EXPLAINS", "MENTIONS"} for edge in edges)
