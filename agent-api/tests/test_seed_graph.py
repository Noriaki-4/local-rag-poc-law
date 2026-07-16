import xml.etree.ElementTree as ET
import json
from hashlib import sha256

from app.seed import (
    _article_chunks,
    _external_guidance_documents,
    _external_guidance_sources,
    _guidance_page_relation_matches,
    _guidance_relation_article_ids,
    _japanese_number_to_int,
    _reference_edges,
    _related_articles_for_chunk,
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
                        "primaryLawId": "law-test",
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
