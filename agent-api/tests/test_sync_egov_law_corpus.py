import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts/sync_egov_law_corpus.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("sync_egov_law_corpus", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _response(title: str, body: str = "本文") -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<DataRoot>
  <Result><Code>0</Code><Message/></Result>
  <ApplData>
    <Law Era="Reiwa" Year="6" Num="1" LawType="Act" Lang="ja">
      <LawNum>令和六年法律第一号</LawNum>
      <LawBody>
        <LawTitle>{title}</LawTitle>
        <MainProvision>
          <Article Num="1"><ArticleTitle>第一条</ArticleTitle><Paragraph Num="1"><ParagraphSentence><Sentence>{body}</Sentence></ParagraphSentence></Paragraph></Article>
        </MainProvision>
        <SupplProvision><SupplProvisionLabel>附則</SupplProvisionLabel><Article Num="1"><ArticleTitle>第一条</ArticleTitle><Paragraph Num="1"><ParagraphSentence><Sentence>附則本文</Sentence></ParagraphSentence></Paragraph></Article></SupplProvision>
      </LawBody>
    </Law>
  </ApplData>
</DataRoot>
""".encode()


def _registry(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "laws": [
                    {
                        "lawId": "test-law-1",
                        "title": "検証法",
                        "familyRoot": "test-law-1",
                    },
                    {
                        "lawId": "test-law-2",
                        "title": "検証法施行令",
                        "familyRoot": "test-law-1",
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_sync_downloads_then_reuses_content_addressed_xml(tmp_path):
    module = _load_module()
    registry = tmp_path / "registry.json"
    output = tmp_path / "corpus"
    _registry(registry)
    payloads = {
        "test-law-1": _response("検証法"),
        "test-law-2": _response("検証法施行令"),
    }

    def fetcher(url, *, timeout_sec):
        assert timeout_sec == 10
        return payloads[url.rsplit("/", 1)[-1]]

    first, first_counts = module.sync_corpus(
        registry_path=registry,
        output_dir=output,
        api_base_url="https://example.test/api/1",
        timeout_sec=10,
        fetcher=fetcher,
    )

    assert first_counts == {"downloaded": 2, "reused": 0}
    assert first["lawCount"] == 2
    assert first["laws"][0]["mainProvisionCount"] == 1
    assert first["laws"][0]["supplementaryProvisionCount"] == 1
    assert first["laws"][0]["articleCount"] == 2
    assert all((output / item["path"]).is_file() for item in first["laws"])
    assert (
        output / "manifests" / f"{first['datasetSnapshotId']}.json"
    ).is_file()

    def forbidden_fetcher(url, *, timeout_sec):
        raise AssertionError(f"network must not be used: {url} {timeout_sec}")

    second, second_counts = module.sync_corpus(
        registry_path=registry,
        output_dir=output,
        api_base_url="https://example.test/api/1",
        timeout_sec=10,
        fetcher=forbidden_fetcher,
    )

    assert second_counts == {"downloaded": 0, "reused": 2}
    assert second["datasetSnapshotId"] == first["datasetSnapshotId"]
    assert json.loads((output / "manifest.json").read_text(encoding="utf-8")) == first


def test_refresh_keeps_old_xml_and_creates_new_snapshot(tmp_path):
    module = _load_module()
    registry = tmp_path / "registry.json"
    output = tmp_path / "corpus"
    _registry(registry)
    version = {"body": "旧本文"}

    def fetcher(url, *, timeout_sec):
        law_id = url.rsplit("/", 1)[-1]
        title = "検証法" if law_id == "test-law-1" else "検証法施行令"
        return _response(title, version["body"])

    first, _ = module.sync_corpus(
        registry_path=registry,
        output_dir=output,
        api_base_url="https://example.test/api/1",
        fetcher=fetcher,
    )
    version["body"] = "新本文"
    second, counts = module.sync_corpus(
        registry_path=registry,
        output_dir=output,
        api_base_url="https://example.test/api/1",
        refresh=True,
        fetcher=fetcher,
    )

    assert counts == {"downloaded": 2, "reused": 0}
    assert second["datasetSnapshotId"] != first["datasetSnapshotId"]
    assert len(list((output / "documents" / "test-law-1").glob("*.xml"))) == 2
    assert len(list((output / "manifests").glob("*.json"))) == 2


def test_inspection_rejects_registry_title_mismatch():
    module = _load_module()

    with pytest.raises(module.EgovDatasetError, match="law title mismatch"):
        module.inspect_egov_xml(_response("別の法律"), expected_title="検証法")
