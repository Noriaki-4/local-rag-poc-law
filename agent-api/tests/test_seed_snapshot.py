import json
from pathlib import Path
from typing import Any

import pytest

from app import seed as seed_module
from app.seed import seed_all


def _law_document(article: str, text: str) -> dict[str, Any]:
    return {
        "documentId": "law-test",
        "contentUnitId": f"law-test-article-{article}",
        "articleContentUnitId": f"law-test-article-{article}",
        "docType": "law",
        "text": text,
        "title": "検証法",
        "authorityType": "act",
        "authoritySource": "law_id",
        "clearanceLevel": 3,
    }


class _OpenSearch:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.documents: list[dict[str, Any]] = []

    def recreate_index(self, mapping: dict[str, Any]) -> None:
        assert mapping == {"mappings": {"properties": {}}}
        self.events.append("opensearch.recreate")

    def index_document(self, document: dict[str, Any]) -> None:
        self.events.append("opensearch.index")
        self.documents.append(document)

    def refresh(self) -> None:
        self.events.append("opensearch.refresh")


class _Graph:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.nodes: list[dict[str, Any]] = []
        self.edges: list[dict[str, Any]] = []

    def clear(self) -> None:
        self.events.append("graph.clear")

    def ensure_legal_graph_schema(self) -> None:
        self.events.append("graph.schema")

    def seed_nodes(self, nodes: list[dict[str, Any]]) -> None:
        self.events.append("graph.nodes")
        self.nodes = nodes

    def seed_edges(self, edges: list[dict[str, Any]]) -> None:
        self.events.append("graph.edges")
        self.edges = edges


def _prepare_seed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    metadata_edges: list[dict[str, Any]] | None = None,
) -> None:
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    (metadata / "mapping.json").write_text(
        json.dumps({"mappings": {"properties": {}}}),
        encoding="utf-8",
    )
    (metadata / "nodes.sample.jsonl").write_text("", encoding="utf-8")
    (metadata / "edges.sample.jsonl").write_text(
        "\n".join(json.dumps(item) for item in metadata_edges or []),
        encoding="utf-8",
    )
    monkeypatch.setattr(seed_module.settings, "samples_dir", tmp_path)
    monkeypatch.setattr(
        seed_module.settings,
        "opensearch_index_mapping",
        Path("metadata/mapping.json"),
    )
    monkeypatch.setattr(seed_module, "_external_guidance_sources", lambda: [])
    monkeypatch.setattr(
        seed_module,
        "_opensearch_documents",
        lambda *_: [
            _law_document("1", "第一条 目的を定める。"),
            _law_document("2", "第二条 第一条を参照する。"),
        ],
    )
    monkeypatch.setattr(seed_module, "_law_family_roots", lambda *_: {})
    monkeypatch.setattr(seed_module, "_seed_minio", lambda *_: (0, 0))


def test_seed_writes_the_same_snapshot_to_both_stores(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _prepare_seed(monkeypatch, tmp_path)
    events: list[str] = []
    opensearch = _OpenSearch(events)
    graph = _Graph(events)

    report = seed_all(opensearch, graph)

    snapshot_id = report["sourceSnapshotId"]
    assert report["graphAudit"]["ok"] is True
    assert all(
        document["sourceSnapshotId"] == snapshot_id
        for document in opensearch.documents
    )
    assert all(node["sourceSnapshotId"] == snapshot_id for node in graph.nodes)
    assert all(edge["sourceSnapshotId"] == snapshot_id for edge in graph.edges)
    assert not any(node["nodeType"] == "RelationAssertion" for node in graph.nodes)
    assert {edge["edgeType"] for edge in graph.edges} <= {
        "HAS_CONTENT_UNIT",
        "REFERENCES",
        "EXPLAINS",
    }
    assert events == [
        "opensearch.recreate",
        "opensearch.index",
        "opensearch.index",
        "opensearch.refresh",
        "graph.clear",
        "graph.schema",
        "graph.nodes",
        "graph.edges",
    ]


def test_invalid_graph_is_rejected_before_destructive_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _prepare_seed(
        monkeypatch,
        tmp_path,
        metadata_edges=[
            {
                "graphEdgeId": "legacy-applied-by",
                "edgeType": "APPLIED_BY",
                "fromGraphNodeId": "law-test-article-1",
                "toGraphNodeId": "law-test-article-2",
            }
        ],
    )
    events: list[str] = []

    with pytest.raises(ValueError, match="Graph audit failed before seed"):
        seed_all(_OpenSearch(events), _Graph(events))

    assert events == []
