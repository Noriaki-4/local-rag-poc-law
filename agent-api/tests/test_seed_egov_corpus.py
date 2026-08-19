import json
from hashlib import sha256

import pytest

from app import seed as seed_module


def _xml() -> bytes:
    return """<?xml version="1.0" encoding="UTF-8"?>
<DataRoot>
  <Result><Code>0</Code><Message/></Result>
  <ApplData>
    <Law Era="Reiwa" Year="6" Num="1" LawType="Act" Lang="ja">
      <LawNum>令和六年法律第一号</LawNum>
      <LawBody>
        <LawTitle>検証法</LawTitle>
        <MainProvision>
          <Article Num="1"><ArticleTitle>第一条</ArticleTitle><Paragraph Num="1"><ParagraphSentence><Sentence>本則本文</Sentence></ParagraphSentence></Paragraph></Article>
        </MainProvision>
        <SupplProvision><SupplProvisionLabel>附則</SupplProvisionLabel><Article Num="1"><ArticleTitle>第一条</ArticleTitle><Paragraph Num="1"><ParagraphSentence><Sentence>附則本文</Sentence></ParagraphSentence></Paragraph></Article></SupplProvision>
      </LawBody>
    </Law>
  </ApplData>
</DataRoot>
""".encode()


def _corpus(tmp_path, *, digest_override: str | None = None):
    samples = tmp_path / "samples"
    (samples / "eval").mkdir(parents=True)
    (samples / "eval" / "law_registry.json").write_text(
        json.dumps(
            {
                "laws": [
                    {
                        "lawId": "test-law",
                        "title": "検証法",
                        "authorityType": "act",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    corpus = tmp_path / "corpus"
    payload = _xml()
    digest = sha256(payload).hexdigest()
    relative = f"documents/test-law/{digest}.xml"
    source = corpus / relative
    source.parent.mkdir(parents=True)
    source.write_bytes(payload)
    manifest = corpus / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "datasetType": "egov_law_xml",
                "datasetSnapshotId": "egov-law-corpus-test",
                "lawCount": 1,
                "laws": [
                    {
                        "lawId": "test-law",
                        "sourceUrl": "https://example.test/api/1/lawdata/test-law",
                        "path": relative,
                        "sha256": f"sha256:{digest_override or digest}",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return samples, manifest


def test_seed_reads_cached_main_and_supplement_without_network(
    tmp_path, monkeypatch
):
    samples, manifest = _corpus(tmp_path)
    monkeypatch.setattr(seed_module.settings, "egov_law_corpus_manifest", manifest)
    monkeypatch.setattr(seed_module.settings, "lawqa_egov_law_ids", "")
    monkeypatch.setattr(
        seed_module.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("network must not be used")
        ),
    )
    monkeypatch.setattr(
        seed_module,
        "embed_texts",
        lambda texts: [[0.0] for _ in texts],
    )

    documents = seed_module._lawqa_egov_documents(samples)

    article_ids = {document["articleContentUnitId"] for document in documents}
    assert "law-test-law-article-1" in article_ids
    assert "law-test-law-suppl-0-article-1" in article_ids
    assert {document["sourceObjectUri"] for document in documents} == {
        "https://example.test/api/1/lawdata/test-law"
    }


def test_seed_rejects_cached_xml_hash_mismatch(tmp_path, monkeypatch):
    samples, manifest = _corpus(tmp_path, digest_override="0" * 64)
    monkeypatch.setattr(seed_module.settings, "egov_law_corpus_manifest", manifest)
    monkeypatch.setattr(seed_module.settings, "lawqa_egov_law_ids", "")

    with pytest.raises(ValueError, match="hash mismatch"):
        seed_module._lawqa_egov_documents(samples)
