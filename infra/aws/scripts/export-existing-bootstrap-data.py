#!/usr/bin/env python3
"""稼働中のローカル索引からAWS bootstrap用の固定成果物をexportする。

正規seedや非同期Relation分類を再実行せず、公開済みのOpenSearch文書と
Neo4j GraphをJSONLへ固定する。検索とGraphは別snapshotでもよい。
bge-m3のembeddingはTitanと互換性がないため出力しない。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _request_json(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    basic_auth: tuple[str, str] | None = None,
) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if basic_auth is not None:
        token = base64.b64encode(
            f"{basic_auth[0]}:{basic_auth[1]}".encode("utf-8")
        ).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"HTTP {error.code} from {url}: {detail}") from error


def _neo4j_rows(
    neo4j_url: str,
    auth: tuple[str, str],
    statement: str,
    parameters: dict[str, Any],
) -> list[list[Any]]:
    payload = _request_json(
        f"{neo4j_url.rstrip('/')}/db/neo4j/tx/commit",
        method="POST",
        body={"statements": [{"statement": statement, "parameters": parameters}]},
        basic_auth=auth,
    )
    errors = payload.get("errors") or []
    if errors:
        raise RuntimeError(f"Neo4j query failed: {errors}")
    results = payload.get("results") or []
    if len(results) != 1:
        raise RuntimeError("Neo4j response did not contain exactly one result")
    return [item["row"] for item in results[0].get("data") or []]


def _export_opensearch_documents(
    base_url: str,
    index_name: str,
    snapshot_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    base_url = base_url.rstrip("/")
    source_index = _request_json(f"{base_url}/{index_name}")
    raw_index = source_index.get(index_name)
    if not isinstance(raw_index, dict):
        raise RuntimeError(f"OpenSearch response has no index definition: {index_name}")
    raw_settings = raw_index.get("settings") or {}
    index_settings = raw_settings.get("index") or {}
    analysis = index_settings.get("analysis")
    mappings = raw_index.get("mappings")
    if not isinstance(analysis, dict) or not isinstance(mappings, dict):
        raise RuntimeError("OpenSearch index definition has no analysis or mappings")
    index_definition = {
        "settings": {"index": {"knn": True, "analysis": analysis}},
        "mappings": mappings,
    }
    response = _request_json(
        f"{base_url}/{index_name}/_search?scroll=2m",
        method="POST",
        body={
            "size": 500,
            "sort": ["_doc"],
            "_source": {"excludes": ["embedding"]},
            "query": {"term": {"sourceSnapshotId": snapshot_id}},
        },
    )
    scroll_id = response.get("_scroll_id")
    documents: list[dict[str, Any]] = []
    try:
        while True:
            hits = ((response.get("hits") or {}).get("hits") or [])
            if not hits:
                break
            documents.extend(
                {"_id": hit["_id"], "_source": hit["_source"]} for hit in hits
            )
            if not scroll_id:
                raise RuntimeError("OpenSearch did not return a scroll ID")
            response = _request_json(
                f"{base_url}/_search/scroll",
                method="POST",
                body={"scroll": "2m", "scroll_id": scroll_id},
            )
            scroll_id = response.get("_scroll_id", scroll_id)
    finally:
        if scroll_id:
            try:
                _request_json(
                    f"{base_url}/_search/scroll",
                    method="DELETE",
                    body={"scroll_id": [scroll_id]},
                )
            except RuntimeError:
                pass

    documents.sort(key=lambda item: item["_id"])
    if not documents:
        raise RuntimeError(f"No OpenSearch documents found for snapshot {snapshot_id}")
    mismatches = [
        item["_id"]
        for item in documents
        if item["_source"].get("sourceSnapshotId") != snapshot_id
    ]
    if mismatches:
        raise RuntimeError(f"OpenSearch snapshot mismatch: {mismatches[:5]}")
    return index_definition, documents


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object: {path}")
    return value


def _write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for value in values:
            stream.write(
                json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
            )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prepare_output_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if any(path.iterdir()):
        raise ValueError(f"Output directory must be empty: {path}")


def _copy_manifest_sources(
    manifest_path: Path, manifest: dict[str, Any], destination: Path, list_key: str
) -> list[Path]:
    """manifestとその相対source fileをportableな成果物へ固定する。"""

    destination.mkdir(parents=True, exist_ok=False)
    copied = [destination / "manifest.json"]
    shutil.copy2(manifest_path, copied[0])
    entries = manifest.get(list_key)
    if not isinstance(entries, list):
        raise ValueError(f"Manifest has no {list_key} array: {manifest_path}")
    for entry in entries:
        relative = entry.get("path") if list_key == "laws" else entry.get("file")
        if not isinstance(relative, str):
            raise ValueError(f"Manifest source entry has no path: {manifest_path}")
        source = (manifest_path.parent / relative).resolve()
        try:
            source.relative_to(manifest_path.parent.resolve())
        except ValueError as error:
            raise ValueError(f"Manifest source escapes its directory: {relative}") from error
        if not source.is_file():
            raise ValueError(f"Manifest source file does not exist: {source}")
        declared_hash = entry.get("sha256")
        if isinstance(declared_hash, str):
            expected_hash = declared_hash.removeprefix("sha256:")
            if _sha256(source) != expected_hash:
                raise ValueError(f"Manifest source hash mismatch: {source}")
        declared_bytes = entry.get("bytes")
        if isinstance(declared_bytes, int) and source.stat().st_size != declared_bytes:
            raise ValueError(f"Manifest source byte count mismatch: {source}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(target)
    return copied


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--opensearch-url",
        default=os.getenv("OPENSEARCH_URL", "http://localhost:9200"),
    )
    parser.add_argument("--opensearch-index", required=True)
    parser.add_argument(
        "--neo4j-url", default=os.getenv("NEO4J_HTTP_URL", "http://localhost:7474")
    )
    parser.add_argument("--neo4j-user", default=os.getenv("NEO4J_USER", "neo4j"))
    parser.add_argument(
        "--neo4j-password", default=os.getenv("NEO4J_PASSWORD", "password")
    )
    parser.add_argument("--search-snapshot-id", required=True)
    parser.add_argument("--graph-snapshot-id", required=True)
    parser.add_argument("--source-corpus-manifest", type=Path, required=True)
    parser.add_argument("--source-guidance-manifest", type=Path, required=True)
    parser.add_argument("--scenario-manifest", type=Path, required=True)
    parser.add_argument("--classification-run-id", required=True)
    args = parser.parse_args()

    _prepare_output_directory(args.output_dir)
    source_corpus_manifest = _read_json(
        args.source_corpus_manifest, "source corpus manifest"
    )
    source_guidance_manifest = _read_json(
        args.source_guidance_manifest, "source guidance manifest"
    )
    scenario_manifest = _read_json(args.scenario_manifest, "scenario manifest")
    source_dataset_snapshot_id = source_corpus_manifest.get("datasetSnapshotId")
    if not source_dataset_snapshot_id:
        raise ValueError("Source corpus manifest has no datasetSnapshotId")
    if scenario_manifest.get("parentDatasetSnapshotId") != source_dataset_snapshot_id:
        raise ValueError(
            "Scenario manifest does not reference the source corpus dataset snapshot"
        )

    index_definition, documents = _export_opensearch_documents(
        args.opensearch_url,
        args.opensearch_index,
        args.search_snapshot_id,
    )
    actual_document_ids_by_type: dict[str, set[str]] = {}
    for item in documents:
        source = item["_source"]
        document_type = source.get("docType")
        document_id = source.get("documentId")
        if not isinstance(document_type, str) or not isinstance(document_id, str):
            raise RuntimeError(
                f"OpenSearch document {item['_id']} has no docType or documentId"
            )
        actual_document_ids_by_type.setdefault(document_type, set()).add(document_id)
    corpus_laws = source_corpus_manifest.get("laws")
    guidance_documents = source_guidance_manifest.get("documents")
    if not isinstance(corpus_laws, list) or not isinstance(guidance_documents, list):
        raise ValueError("Source manifests have no laws or documents array")
    expected_law_ids = {
        f"law-{law.get('lawId')}"
        for law in corpus_laws
        if isinstance(law, dict) and isinstance(law.get("lawId"), str)
    }
    expected_guidance_ids = {
        document.get("documentId")
        for document in guidance_documents
        if isinstance(document, dict) and isinstance(document.get("documentId"), str)
    }
    if actual_document_ids_by_type != {
        "law": expected_law_ids,
        "guideline": expected_guidance_ids,
    }:
        raise RuntimeError(
            "OpenSearch document IDs do not match the corpus and guidance manifests"
        )

    auth = (args.neo4j_user, args.neo4j_password)
    run_rows = _neo4j_rows(
        args.neo4j_url,
        auth,
        """
        MATCH (run:ClassificationRun {classificationRunId: $runId})
        RETURN properties(run) AS properties
        """,
        {"runId": args.classification_run_id},
    )
    if len(run_rows) != 1:
        raise RuntimeError("ClassificationRun was not found or was not unique")
    run = run_rows[0][0]
    if run.get("phase") != "published":
        raise RuntimeError("ClassificationRun must be published before export")
    if run.get("sourceSnapshotId") != args.graph_snapshot_id:
        raise RuntimeError("ClassificationRun belongs to a different Graph snapshot")

    node_rows = _neo4j_rows(
        args.neo4j_url,
        auth,
        """
        MATCH (node:GraphNode {sourceSnapshotId: $snapshotId})
        RETURN labels(node) AS labels, properties(node) AS properties
        ORDER BY node.graphNodeId
        """,
        {"snapshotId": args.graph_snapshot_id},
    )
    nodes = [
        {"labels": labels, "properties": properties}
        for labels, properties in node_rows
    ]
    graph_node_ids = {
        item["properties"].get("graphNodeId") for item in nodes
    }
    if None in graph_node_ids or len(graph_node_ids) != len(nodes):
        raise RuntimeError("Graph nodes must have unique graphNodeId values")

    edge_rows = _neo4j_rows(
        args.neo4j_url,
        auth,
        """
        MATCH (source:GraphNode {sourceSnapshotId: $snapshotId})
              -[relation]->
              (target:GraphNode {sourceSnapshotId: $snapshotId})
        RETURN source.graphNodeId AS sourceGraphNodeId,
               type(relation) AS relationType,
               properties(relation) AS properties,
               target.graphNodeId AS targetGraphNodeId
        ORDER BY sourceGraphNodeId, relationType, targetGraphNodeId
        """,
        {"snapshotId": args.graph_snapshot_id},
    )
    edges = [
        {
            "sourceGraphNodeId": source_id,
            "relationType": relation_type,
            "properties": properties,
            "targetGraphNodeId": target_id,
        }
        for source_id, relation_type, properties, target_id in edge_rows
    ]
    for edge in edges:
        if (
            edge["sourceGraphNodeId"] not in graph_node_ids
            or edge["targetGraphNodeId"] not in graph_node_ids
        ):
            raise RuntimeError(f"Graph edge has a missing endpoint: {edge}")

    index_definition_path = args.output_dir / "opensearch-index.json"
    documents_path = args.output_dir / "opensearch-documents.jsonl"
    nodes_path = args.output_dir / "graph-nodes.jsonl"
    edges_path = args.output_dir / "graph-edges.jsonl"
    _write_json(index_definition_path, index_definition)
    _write_jsonl(documents_path, documents)
    _write_jsonl(nodes_path, nodes)
    _write_jsonl(edges_path, edges)

    corpus_source_files = _copy_manifest_sources(
        args.source_corpus_manifest,
        source_corpus_manifest,
        args.output_dir / "source" / "corpus",
        "laws",
    )
    guidance_source_files = _copy_manifest_sources(
        args.source_guidance_manifest,
        source_guidance_manifest,
        args.output_dir / "source" / "guidance",
        "documents",
    )
    scenario_target = args.output_dir / "source" / "scenario" / "manifest.json"
    scenario_target.parent.mkdir(parents=True, exist_ok=False)
    shutil.copy2(args.scenario_manifest, scenario_target)
    files = [
        index_definition_path,
        documents_path,
        nodes_path,
        edges_path,
        *corpus_source_files,
        *guidance_source_files,
        scenario_target,
    ]
    document_ids = {
        item["_source"].get("documentId")
        for item in documents
        if item["_source"].get("documentId") is not None
    }
    document_type_counts: dict[str, int] = {}
    for item in documents:
        document_type = str(item["_source"].get("docType", "missing"))
        document_type_counts[document_type] = (
            document_type_counts.get(document_type, 0) + 1
        )
    graph_label_counts: dict[str, int] = {}
    for item in nodes:
        for label in item["labels"]:
            graph_label_counts[label] = graph_label_counts.get(label, 0) + 1

    manifest = {
        "schemaVersion": 3,
        "artifactType": "existing-local-snapshot-bootstrap",
        "exportedAt": datetime.now(UTC).isoformat(),
        "searchSnapshotId": args.search_snapshot_id,
        "graphSnapshotId": args.graph_snapshot_id,
        "graphSchemaVersion": run.get("graphSchemaVersion"),
        "sourceManifests": {
            "corpus": {
                "path": str(args.source_corpus_manifest),
                "sha256": _sha256(args.source_corpus_manifest),
                "datasetSnapshotId": source_dataset_snapshot_id,
                "lawCount": source_corpus_manifest.get("lawCount"),
            },
            "guidance": {
                "path": str(args.source_guidance_manifest),
                "sha256": _sha256(args.source_guidance_manifest),
                "documentCount": len(guidance_documents),
            },
            "scenario": {
                "path": str(args.scenario_manifest),
                "sha256": _sha256(args.scenario_manifest),
                "datasetId": scenario_manifest.get("datasetId"),
                "datasetSnapshotId": scenario_manifest.get("datasetSnapshotId"),
                "parentDatasetSnapshotId": scenario_manifest.get(
                    "parentDatasetSnapshotId"
                ),
            },
        },
        "classificationRun": {
            "classificationRunId": args.classification_run_id,
            "phase": run.get("phase"),
            "processedCount": run.get("processedCount"),
            "assertionCount": run.get("assertionCount"),
            "publishedAt": run.get("publishedAt"),
        },
        "openSearch": {
            "sourceIndex": args.opensearch_index,
            "documentCount": len(documents),
            "uniqueDocumentCount": len(document_ids),
            "contentUnitCountByDocType": dict(sorted(document_type_counts.items())),
            "sourceEmbeddingExcluded": True,
            "targetEmbeddingProvider": "bedrock",
            "targetEmbeddingModel": "amazon.titan-embed-text-v2:0",
            "targetEmbeddingDimensions": 1024,
            "targetEmbeddingNormalize": True,
            "targetEmbeddingMaxChars": 1000,
        },
        "graph": {
            "nodeCount": len(nodes),
            "edgeCount": len(edges),
            "nodeCountByLabel": dict(sorted(graph_label_counts.items())),
        },
        "files": {
            str(path.relative_to(args.output_dir)): {
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
            for path in files
        },
    }
    _write_json(args.output_dir / "manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as error:
        print(f"export failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
