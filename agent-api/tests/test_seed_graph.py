import xml.etree.ElementTree as ET
import json
from hashlib import sha256

from app.seed import _article_chunks, _external_guidance_documents, _external_guidance_sources, _japanese_number_to_int, _reference_edges


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
