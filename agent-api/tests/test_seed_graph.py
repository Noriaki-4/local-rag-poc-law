import xml.etree.ElementTree as ET
import json
from hashlib import sha256

from app.seed import (
    _article_chunks,
    _docling_guidance_chunks,
    _drop_dangling_explains_edges,
    _delegation_edges,
    _external_guidance_documents,
    _external_guidance_sources,
    _guidance_graph_artifacts,
    _guidance_page_relation_matches,
    _guidance_primary_law_document_id,
    _guidance_relation_article_ids,
    _japanese_number_to_int,
    _incorporation_edges,
    _parent_law_article_reference_ids,
    _parent_order_article_reference_ids,
    _reference_edges,
    _related_articles_for_chunk,
    _table_self_ref_article_ids,
)


def _document(article: str, text: str) -> dict:
    return {
        "documentId": "law-test",
        "contentUnitId": f"law-test-article-{article}",
        "docType": "law",
        "text": text,
    }


def test_japanese_number_conversion():
    assert _japanese_number_to_int("二十四") == 24
    assert _japanese_number_to_int("百二十三") == 123
    assert _japanese_number_to_int("12") == 12


def test_reference_edges_include_explicit_and_previous_article():
    documents = [
        _document("1", "第一条 この法律の目的を定める。"),
        _document("2", "第二条 前条及び第三条の規定を参照する。"),
        _document("3", "第三条 必要な事項を定める。"),
    ]

    edges = _reference_edges(documents)
    pairs = {(edge["fromGraphNodeId"], edge["toGraphNodeId"]) for edge in edges}

    assert ("law-test-article-2", "law-test-article-1") in pairs
    assert ("law-test-article-2", "law-test-article-3") in pairs
    assert all(edge["edgeType"] == "REFERENCES" for edge in edges)


def test_delegation_edges_reverse_subordinate_law_parent_references():
    parent = _document("5", "第五条 政令で定めるものを除く。")
    subordinate = {
        **_document("2_13", "法第五条第一項に規定する政令で定める有価証券を定める。"),
        "documentId": "law-order",
        "contentUnitId": "law-order-article-2_13",
    }

    edges = _delegation_edges(
        [parent, subordinate],
        {"law-test": "law-test", "law-order": "law-test"},
    )

    assert _parent_law_article_reference_ids(
        "law-test", "法第五条第一項及び同法第六条に規定する事項"
    ) == ["law-test-article-5", "law-test-article-6"]
    assert [
        (edge["fromGraphNodeId"], edge["toGraphNodeId"], edge["edgeType"])
        for edge in edges
    ] == [
        ("law-order-article-2_13", "law-test-article-5", "REFERENCES"),
        ("law-test-article-5", "law-order-article-2_13", "IMPLEMENTS"),
    ]


def test_delegation_edges_link_ordinance_order_references():
    act = {
        **_document("2", "第二条 政令で定める。"),
        "title": "検証法",
    }
    order = {
        **_document("1_7", "第一条の七 内閣府令で定める方式による。"),
        "documentId": "law-order",
        "contentUnitId": "law-order-article-1_7",
        "title": "検証法施行令",
    }
    ordinance = {
        **_document("13", "令第一条の七第二号に規定する方式を定める。"),
        "documentId": "law-ordinance",
        "contentUnitId": "law-ordinance-article-13",
        "title": "検証法施行規則",
    }

    edges = _delegation_edges(
        [act, order, ordinance],
        {
            "law-test": "law-test",
            "law-order": "law-test",
            "law-ordinance": "law-test",
        },
    )

    assert _parent_order_article_reference_ids(
        "law-order",
        "令第一条の七第二号及び同令第二条に規定する事項",
    ) == ["law-order-article-1_7", "law-order-article-2"]
    assert (
        "law-ordinance-article-13",
        "law-order-article-1_7",
        "REFERENCES",
    ) in {
        (edge["fromGraphNodeId"], edge["toGraphNodeId"], edge["edgeType"])
        for edge in edges
    }
    assert (
        "law-order-article-1_7",
        "law-ordinance-article-13",
        "IMPLEMENTS",
    ) in {
        (edge["fromGraphNodeId"], edge["toGraphNodeId"], edge["edgeType"])
        for edge in edges
    }


def test_incorporation_edges_reverse_mutatis_mutandis_references():
    documents = [
        _document("1", "第一条 基本要件を定める。"),
        _document("2", "第二条 第一条の規定を準用する。"),
    ]

    edges = _incorporation_edges(documents)

    assert [
        (edge["fromGraphNodeId"], edge["toGraphNodeId"], edge["edgeType"])
        for edge in edges
    ] == [("law-test-article-1", "law-test-article-2", "APPLIED_BY")]


def _guideline_chunk(content_unit_id: str, related_article_ids: list[str]) -> dict:
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
        "relatedArticleContentUnitIds": related_article_ids,
    }


def test_guidance_graph_artifacts_builds_document_node_and_explains_edges():
    documents = [
        _guideline_chunk("guidance-test-page-6-chunk-1", []),  # 本文チャンク(条文注釈なし)
        _guideline_chunk("guidance-test-page-20-chunk-1", ["law-335AC0000000145-article-18_2"]),
        _guideline_chunk(
            "guidance-test-page-21-chunk-1",
            ["law-335AC0000000145-article-18_2", "law-335AC0000000145-article-23_35_2"],
        ),
    ]

    nodes, edges = _guidance_graph_artifacts(documents)

    assert len(nodes) == 1
    assert nodes[0]["graphNodeId"] == "guidance-test"
    assert nodes[0]["nodeType"] == "Document"
    assert nodes[0]["docType"] == "guideline"

    # 文書単位で条文を集約(重複除去・出現順)し、EXPLAINSエッジを張る。
    targets = [edge["toGraphNodeId"] for edge in edges]
    assert targets == ["law-335AC0000000145-article-18_2", "law-335AC0000000145-article-23_35_2"]
    assert all(edge["edgeType"] == "EXPLAINS" for edge in edges)
    assert all(edge["fromGraphNodeId"] == "guidance-test" for edge in edges)


def test_guidance_graph_artifacts_skips_document_without_article_refs():
    documents = [_guideline_chunk("guidance-test-page-1-chunk-1", [])]

    nodes, edges = _guidance_graph_artifacts(documents)

    assert nodes == []  # 対応表・注釈が無い文書は孤立ノードを作らない
    assert edges == []


def test_drop_dangling_explains_edges_removes_only_missing_targets():
    edges = [
        {"edgeType": "EXPLAINS", "fromGraphNodeId": "guidance-test", "toGraphNodeId": "law-a-article-1"},
        {"edgeType": "EXPLAINS", "fromGraphNodeId": "guidance-test", "toGraphNodeId": "law-a-article-999"},
        {"edgeType": "REFERENCES", "fromGraphNodeId": "law-a-article-1", "toGraphNodeId": "law-a-article-2"},
    ]
    node_ids = {"guidance-test", "law-a-article-1"}

    kept, dropped = _drop_dangling_explains_edges(edges, node_ids)

    assert dropped == 1
    # 張り先が存在するEXPLAINSは残す。EXPLAINS以外のdanglingは除去せず assert に委ねる。
    kept_types = [(edge["edgeType"], edge["toGraphNodeId"]) for edge in kept]
    assert ("EXPLAINS", "law-a-article-1") in kept_types
    assert ("EXPLAINS", "law-a-article-999") not in kept_types
    assert ("REFERENCES", "law-a-article-2") in kept_types


def test_long_article_is_split_into_paragraphs_and_items(monkeypatch):
    from app import seed as seed_module

    monkeypatch.setattr(seed_module.settings, "embedding_max_chars", 20)
    article = ET.fromstring(
        """
        <Article Num="2">
          <ArticleTitle>第二条</ArticleTitle>
          <Paragraph Num="1"><ParagraphNum>1</ParagraphNum><ParagraphSentence><Sentence>短い本文。</Sentence></ParagraphSentence></Paragraph>
          <Paragraph Num="2"><ParagraphNum>2</ParagraphNum><ParagraphSentence><Sentence>各号を定める長い本文です。</Sentence></ParagraphSentence>
            <Item Num="1"><ItemTitle>一</ItemTitle><ItemSentence><Sentence>第一号の要件を定める。</Sentence></ItemSentence></Item>
            <Item Num="2"><ItemTitle>二</ItemTitle><ItemSentence><Sentence>第二号の例外を定める。</Sentence></ItemSentence></Item>
          </Paragraph>
        </Article>
        """
    )

    chunks = _article_chunks(article, "law-test-article-2", "第二条", None)
    ids = {chunk["contentUnitId"] for chunk in chunks}

    assert "law-test-article-2-paragraph-1" in ids
    assert "law-test-article-2-paragraph-2-item-1" in ids
    assert "law-test-article-2-paragraph-2-item-2" in ids
    assert all(chunk["articleContentUnitId"] == "law-test-article-2" for chunk in chunks)


def test_external_guidance_pdf_is_chunked_with_source_page(monkeypatch, tmp_path):
    from app import seed as seed_module

    source_pdf = tmp_path / "guidance.pdf"
    source_pdf.write_bytes(b"placeholder")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "documentId": "guidance-test",
                        "title": "検証ガイドライン",
                        "authority": "検証庁",
                        "file": "guidance.pdf",
                        "sourceUrl": "https://example.test/guidance.pdf",
                        "sha256": f"sha256:{sha256(source_pdf.read_bytes()).hexdigest()}",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    class FakePage:
        def extract_text(self):
            return "第一節 基本事項\n" + "本文。" * 250

    class FakeReader:
        pages = [FakePage()]

    monkeypatch.setattr(seed_module.settings, "seed_external_guidance", True)
    monkeypatch.setattr(seed_module.settings, "external_guidance_manifest", manifest_path)
    monkeypatch.setattr(seed_module.settings, "external_guidance_chunk_chars", 400)
    monkeypatch.setattr(seed_module, "PdfReader", lambda _: FakeReader())

    sources = _external_guidance_sources()
    documents = _external_guidance_documents(sources)

    assert len(documents) > 1
    assert all(document["docType"] == "guideline" for document in documents)
    assert all(document["sourcePage"] == 1 for document in documents)
    assert all(document["sourceObjectUri"].endswith("guidance.pdf") for document in documents)
    assert all(len(document["text"]) <= 400 for document in documents)


def test_guidance_primary_law_document_id_adds_law_prefix_to_bare_egov_id():
    # manifestのprimaryLawIdはlaw_registry.jsonと同じ裸のe-Gov法令番号("335AC0000000145")で
    # 記録される想定。法令チャンクの実際のcontentUnitId表記("law-<法令番号>")へ正規化する。
    assert _guidance_primary_law_document_id({"primaryLawId": "335AC0000000145"}) == "law-335AC0000000145"


def test_guidance_primary_law_document_id_is_idempotent_for_already_prefixed_id():
    assert _guidance_primary_law_document_id({"primaryLawId": "law-335AC0000000145"}) == "law-335AC0000000145"


def test_guidance_primary_law_document_id_returns_none_when_absent():
    assert _guidance_primary_law_document_id({}) is None


def test_guidance_relation_article_ids_extracts_self_referenced_article():
    ids = _guidance_relation_article_ids("law-335AC0000000145", "法第18条の2第1項第2号関係")
    assert ids == ["law-335AC0000000145-article-18_2"]


def test_guidance_relation_article_ids_dedupes_repeated_ids():
    ids = _guidance_relation_article_ids("law-test", "法第一条及び第一条関係")
    assert ids == ["law-test-article-1"]


def test_guidance_relation_article_ids_skips_other_law_references():
    ids = _guidance_relation_article_ids("law-335AC0000000145", "薬事法施行令第1条関係")
    assert ids == []


def test_guidance_page_relation_matches_returns_empty_without_primary_law_id():
    assert _guidance_page_relation_matches("法第一条関係", None) == []


def test_guidance_page_relation_matches_finds_offset_and_article_ids():
    text = "本文...（法第一条関係）続き"
    matches = _guidance_page_relation_matches(text, "law-test")
    assert len(matches) == 1
    offset, article_ids = matches[0]
    assert text[offset] in "（("
    assert article_ids == ["law-test-article-1"]


def test_related_articles_for_chunk_uses_nearest_preceding_match_on_same_page():
    page_matches = [(0, ["law-test-article-1"]), (100, ["law-test-article-2"])]

    article_ids, source = _related_articles_for_chunk(50, page_matches, None)
    assert article_ids == ["law-test-article-1"]
    assert source == "guideline_relation_annotation"

    article_ids, source = _related_articles_for_chunk(150, page_matches, None)
    assert article_ids == ["law-test-article-2"]
    assert source == "guideline_relation_annotation"


def test_related_articles_for_chunk_falls_back_to_carried_reference():
    article_ids, source = _related_articles_for_chunk(10, [], ["law-test-article-3"])
    assert article_ids == ["law-test-article-3"]
    assert source == "carried_forward"


def test_related_articles_for_chunk_returns_empty_without_any_reference():
    article_ids, source = _related_articles_for_chunk(10, [], None)
    assert article_ids == []
    assert source is None


def test_external_guidance_carries_reference_across_pages(monkeypatch, tmp_path):
    from app import seed as seed_module

    source_pdf = tmp_path / "guidance.pdf"
    source_pdf.write_bytes(b"placeholder")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "documentId": "guidance-test",
                        "title": "検証ガイドライン",
                        "authority": "検証庁",
                        "file": "guidance.pdf",
                        "sourceUrl": "https://example.test/guidance.pdf",
                        "sha256": f"sha256:{sha256(source_pdf.read_bytes()).hexdigest()}",
                        "primaryLawId": "test",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    class FakePage:
        def __init__(self, text: str) -> None:
            self._text = text

        def extract_text(self):
            return self._text

    class FakeReader:
        pages = [
            FakePage("（法第一条関係）\n" + "本文。" * 5),
            FakePage("続きの解説。" * 5),
        ]

    monkeypatch.setattr(seed_module.settings, "seed_external_guidance", True)
    monkeypatch.setattr(seed_module.settings, "external_guidance_manifest", manifest_path)
    monkeypatch.setattr(seed_module.settings, "external_guidance_chunk_chars", 400)
    monkeypatch.setattr(seed_module, "PdfReader", lambda _: FakeReader())

    sources = _external_guidance_sources()
    documents = _external_guidance_documents(sources)

    page1_docs = [document for document in documents if document["sourcePage"] == 1]
    page2_docs = [document for document in documents if document["sourcePage"] == 2]

    assert all(document["relatedArticleContentUnitIds"] == ["law-test-article-1"] for document in page1_docs)
    assert all(document["articleReferenceSource"] == "guideline_relation_annotation" for document in page1_docs)
    assert all(document["relatedArticleContentUnitIds"] == ["law-test-article-1"] for document in page2_docs)
    assert all(document["articleReferenceSource"] == "carried_forward" for document in page2_docs)


def test_external_guidance_without_primary_law_id_has_no_related_articles(monkeypatch, tmp_path):
    from app import seed as seed_module

    source_pdf = tmp_path / "guidance.pdf"
    source_pdf.write_bytes(b"placeholder")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "documentId": "guidance-test",
                        "title": "検証ガイドライン",
                        "authority": "検証庁",
                        "file": "guidance.pdf",
                        "sourceUrl": "https://example.test/guidance.pdf",
                        "sha256": f"sha256:{sha256(source_pdf.read_bytes()).hexdigest()}",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    class FakePage:
        def extract_text(self):
            return "（法第一条関係）\n" + "本文。" * 5

    class FakeReader:
        pages = [FakePage()]

    monkeypatch.setattr(seed_module.settings, "seed_external_guidance", True)
    monkeypatch.setattr(seed_module.settings, "external_guidance_manifest", manifest_path)
    monkeypatch.setattr(seed_module.settings, "external_guidance_chunk_chars", 400)
    monkeypatch.setattr(seed_module, "PdfReader", lambda _: FakeReader())

    sources = _external_guidance_sources()
    documents = _external_guidance_documents(sources)

    assert all(document["relatedArticleContentUnitIds"] == [] for document in documents)
    assert all(document["articleReferenceSource"] is None for document in documents)


def test_table_self_ref_article_ids_extracts_standalone_law_references():
    text = "２ 体制の整備 法第18条の２第 １項第２号 法第18条の２第 ３項第２号 規則第98条の ９第２号イ"
    ids = _table_self_ref_article_ids("law-335AC0000000145", text)
    assert ids == ["law-335AC0000000145-article-18_2"]


def test_table_self_ref_article_ids_skips_other_law_names():
    assert _table_self_ref_article_ids("law-test", "会社法第330条 民法第644条 施行規則第98条") == []


def test_table_self_ref_article_ids_caps_at_limit():
    text = "".join(f"法第{number}条 " for number in range(1, 20))
    ids = _table_self_ref_article_ids("law-test", text, limit=8)
    assert len(ids) == 8
    assert ids[0] == "law-test-article-1"


def test_table_self_ref_article_ids_without_law_id_returns_empty():
    assert _table_self_ref_article_ids(None, "法第1条") == []


def test_docling_guidance_chunks_emits_table_as_single_chunk_with_references():
    items = [
        {"type": "section_header", "page": 1, "text": "第２ 法令遵守体制"},
        {"type": "text", "page": 1, "text": "本文の解説。" * 10},
        {"type": "table", "page": 19, "text": "体制の整備 法第18条の２第１項第２号", "markdown": "| 項目 | 条文 |\n| 体制の整備 | 法第18条の２第１項第２号 |"},
    ]

    chunks = _docling_guidance_chunks(items, "law-335AC0000000145", chunk_chars=400)

    table_chunks = [chunk for chunk in chunks if chunk["chunkStrategy"] == "guidance_docling_table_v1"]
    assert len(table_chunks) == 1
    assert table_chunks[0]["page"] == 19
    assert table_chunks[0]["text"].startswith("|")
    assert table_chunks[0]["relatedArticleContentUnitIds"] == ["law-335AC0000000145-article-18_2"]
    assert table_chunks[0]["articleReferenceSource"] == "guideline_table_annotation"

    text_chunks = [chunk for chunk in chunks if chunk["chunkStrategy"] == "guidance_docling_text_chunk_v1"]
    assert text_chunks and text_chunks[0]["text"].startswith("第２ 法令遵守体制")
    assert text_chunks[0]["page"] == 1


def test_docling_guidance_chunks_carries_paren_annotation_forward():
    items = [
        {"type": "text", "page": 1, "text": "（法第一条関係）の解説。" + "本文。" * 50},
        {"type": "text", "page": 2, "text": "続きの解説。" * 50},
    ]

    chunks = _docling_guidance_chunks(items, "law-test", chunk_chars=100)

    assert chunks[0]["articleReferenceSource"] == "guideline_relation_annotation"
    assert chunks[0]["relatedArticleContentUnitIds"] == ["law-test-article-1"]
    assert chunks[-1]["articleReferenceSource"] == "carried_forward"
    assert chunks[-1]["relatedArticleContentUnitIds"] == ["law-test-article-1"]


def _artifact_guidance_manifest(tmp_path, source_pdf_bytes: bytes = b"placeholder") -> tuple:
    source_pdf = tmp_path / "guidance.pdf"
    source_pdf.write_bytes(source_pdf_bytes)
    digest = sha256(source_pdf_bytes).hexdigest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "documentId": "guidance-test",
                        "title": "検証ガイドライン",
                        "authority": "検証庁",
                        "file": "guidance.pdf",
                        "sourceUrl": "https://example.test/guidance.pdf",
                        "sha256": f"sha256:{digest}",
                        "primaryLawId": "test",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return manifest_path, digest


def test_external_guidance_uses_preprocessed_artifact_when_available(monkeypatch, tmp_path):
    from app import seed as seed_module

    manifest_path, digest = _artifact_guidance_manifest(tmp_path)
    artifact = {
        "schemaVersion": 1,
        "sourceSha256": f"sha256:{digest}",
        "converter": "docling",
        "items": [
            {"type": "text", "page": 1, "text": "本文の解説。"},
            {"type": "table", "page": 19, "text": "法第18条の２第１項第２号", "markdown": "| 条文 |\n| 法第18条の２ |"},
        ],
    }

    monkeypatch.setattr(seed_module.settings, "seed_external_guidance", True)
    monkeypatch.setattr(seed_module.settings, "external_guidance_manifest", manifest_path)
    monkeypatch.setattr(seed_module.settings, "external_guidance_chunk_chars", 400)
    monkeypatch.setattr(seed_module, "_load_guidance_artifact", lambda source: artifact)

    documents = _external_guidance_documents(_external_guidance_sources())

    assert all(document["parserType"] == "pdf_docling_structured_v1" for document in documents)
    table_documents = [d for d in documents if d["chunkStrategy"] == "guidance_docling_table_v1"]
    assert len(table_documents) == 1
    assert table_documents[0]["sourcePage"] == 19
    assert table_documents[0]["relatedArticleContentUnitIds"] == ["law-test-article-18_2"]


def test_external_guidance_falls_back_to_pypdf_without_artifact(monkeypatch, tmp_path):
    from app import seed as seed_module

    manifest_path, _ = _artifact_guidance_manifest(tmp_path)

    class FakePage:
        def extract_text(self):
            return "本文。" * 100

    class FakeReader:
        pages = [FakePage()]

    monkeypatch.setattr(seed_module.settings, "seed_external_guidance", True)
    monkeypatch.setattr(seed_module.settings, "external_guidance_manifest", manifest_path)
    monkeypatch.setattr(seed_module.settings, "external_guidance_chunk_chars", 400)
    monkeypatch.setattr(seed_module, "_load_guidance_artifact", lambda source: None)
    monkeypatch.setattr(seed_module, "PdfReader", lambda _: FakeReader())

    documents = _external_guidance_documents(_external_guidance_sources())

    assert documents
    assert all(document["parserType"] == "pdf_pypdf_page_chunk_v1" for document in documents)


def test_load_guidance_artifact_rejects_stale_source_hash(monkeypatch, tmp_path):
    from app import seed as seed_module

    manifest_path, _ = _artifact_guidance_manifest(tmp_path)
    monkeypatch.setattr(seed_module.settings, "seed_external_guidance", True)
    monkeypatch.setattr(seed_module.settings, "external_guidance_manifest", manifest_path)
    source = _external_guidance_sources()[0]

    class FakeResponse:
        def read(self):
            payload = {"schemaVersion": 1, "sourceSha256": "sha256:" + "f" * 64, "items": [{"type": "text", "page": 1, "text": "x"}]}
            return json.dumps(payload).encode("utf-8")

        def close(self):
            pass

        def release_conn(self):
            pass

    class FakeMinio:
        def get_object(self, bucket, object_name):
            return FakeResponse()

    monkeypatch.setattr(seed_module, "_minio_client", lambda http_client=None: FakeMinio())

    try:
        seed_module._load_guidance_artifact(source)
    except ValueError as exc:
        assert "stale" in str(exc)
    else:
        raise AssertionError("stale artifact must be rejected")


def test_external_guidance_rejects_checksum_mismatch(monkeypatch, tmp_path):
    from app import seed as seed_module

    (tmp_path / "guidance.pdf").write_bytes(b"not the expected file")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "documentId": "guidance-test",
                        "title": "検証ガイドライン",
                        "authority": "検証庁",
                        "file": "guidance.pdf",
                        "sourceUrl": "https://example.test/guidance.pdf",
                        "sha256": "sha256:" + "0" * 64,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(seed_module.settings, "seed_external_guidance", True)
    monkeypatch.setattr(seed_module.settings, "external_guidance_manifest", manifest_path)

    try:
        _external_guidance_sources()
    except ValueError as exc:
        assert "checksum mismatch" in str(exc)
    else:
        raise AssertionError("checksum mismatch must reject the source")
