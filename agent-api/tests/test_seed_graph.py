import xml.etree.ElementTree as ET

from app.seed import _article_chunks, _japanese_number_to_int, _reference_edges


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
