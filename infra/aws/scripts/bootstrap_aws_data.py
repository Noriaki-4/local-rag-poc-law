#!/usr/bin/env python3
"""固定snapshot成果物をS3、OpenSearch Serverless、Neptune Analyticsへ投入する。

既定動作はローカル検証だけである。AWSへ書き込むには接続先を明示し、さらに
``--apply``を指定する。正規seedや非同期Relation分類は実行しない。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable

LABEL_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
BULK_SIZE = 25
GRAPH_BATCH_SIZE = 50
DEFAULT_EMBEDDING_WORKERS = 2
EMBEDDING_ATTEMPTS = 6
NEPTUNE_JSON_LIST_PREFIX = "local-rag-json-list:v1:"


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line:
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"JSONL row must be an object: {path}:{line_number}")
        rows.append(value)
    return rows


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _chunks(values: list[Any], size: int) -> Iterable[list[Any]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _neptune_properties(properties: dict[str, Any]) -> dict[str, Any]:
    """Encode list properties for Neptune openCypher without changing the artifact."""

    result = copy.deepcopy(properties)
    for key, value in result.items():
        if isinstance(value, list):
            result[key] = NEPTUNE_JSON_LIST_PREFIX + json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
            )
    return result


def _embedding_workers() -> int:
    raw_value = os.environ.get(
        "BOOTSTRAP_EMBEDDING_WORKERS", str(DEFAULT_EMBEDDING_WORKERS)
    )
    try:
        workers = int(raw_value)
    except ValueError as error:
        raise ValueError("BOOTSTRAP_EMBEDDING_WORKERS must be an integer") from error
    if not 1 <= workers <= 32:
        raise ValueError("BOOTSTRAP_EMBEDDING_WORKERS must be between 1 and 32")
    return workers


def _serverless_index_definition(
    definition: dict[str, Any], expected_dimensions: int
) -> dict[str, Any]:
    """Adapt the exported local mapping without changing the snapshot artifact."""
    result = copy.deepcopy(definition)
    try:
        embedding = result["mappings"]["properties"]["embedding"]
        method = embedding["method"]
    except (KeyError, TypeError) as error:
        raise ValueError("OpenSearch mapping has no embedding method") from error
    if embedding.get("type") != "knn_vector":
        raise ValueError("OpenSearch embedding field must be knn_vector")
    if embedding.get("dimension") != expected_dimensions:
        raise ValueError("OpenSearch embedding dimension does not match environment config")
    engine = method.get("engine")
    if engine not in {"lucene", "faiss"}:
        raise ValueError(f"unsupported exported OpenSearch vector engine: {engine}")
    # OpenSearch Serverless vector collections support Faiss, while the local
    # provisioned index was exported with Lucene. The source vector is excluded
    # from the artifact and regenerated with Titan, so this changes no source data.
    method["engine"] = "faiss"
    return result


def _index_not_found(response: Any) -> bool:
    return response is None or (
        isinstance(response, dict) and response.get("status") == 404
    )


def _wait_for_index(
    client: Any,
    index: str,
    *,
    attempts: int = 60,
    delay_seconds: float = 5,
) -> None:
    for _attempt in range(attempts):
        response = client.request("GET", index, allowed=(200, 404))
        if not _index_not_found(response):
            return
        time.sleep(delay_seconds)
    raise RuntimeError(f"OpenSearch index did not become visible: {index}")


def _serverless_bulk_action(index: str) -> dict[str, dict[str, str]]:
    # Vector collections reject caller-supplied document IDs. contentUnitId in
    # _source remains the stable application identifier.
    return {"index": {"_index": index}}


def _snapshot_count(client: Any, index: str, snapshot_id: str) -> int:
    return int(
        client.request(
            "POST",
            f"{index}/_count",
            json_body={"query": {"term": {"sourceSnapshotId": snapshot_id}}},
        )["count"]
    )


def _wait_for_snapshot_count(
    client: Any,
    index: str,
    snapshot_id: str,
    expected: int,
    *,
    attempts: int = 60,
    delay_seconds: float = 5,
) -> int:
    count = -1
    for _attempt in range(attempts):
        count = _snapshot_count(client, index, snapshot_id)
        if count == expected:
            return count
        time.sleep(delay_seconds)
    return count


def validate_artifact(artifact_dir: Path, config: dict[str, Any]) -> dict[str, Any]:
    manifest_path = artifact_dir / "manifest.json"
    manifest = _json(manifest_path)
    if manifest.get("schemaVersion") != 3:
        raise ValueError("bootstrap manifest schemaVersion must be 3")
    bootstrap = config["bootstrapData"]
    expected = {
        "searchSnapshotId": bootstrap["searchSnapshotId"],
        "graphSnapshotId": bootstrap["graphSnapshotId"],
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise ValueError(f"manifest {key} does not match environment config")
    if manifest.get("classificationRun", {}).get("classificationRunId") != bootstrap[
        "classificationRunId"
    ]:
        raise ValueError("manifest ClassificationRun does not match environment config")

    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("manifest has no file inventory")
    for relative, metadata in files.items():
        path = artifact_dir / relative
        if not path.is_file():
            raise ValueError(f"artifact file is missing: {relative}")
        if _sha256(path) != metadata.get("sha256"):
            raise ValueError(f"artifact hash mismatch: {relative}")
        if path.stat().st_size != metadata.get("bytes"):
            raise ValueError(f"artifact byte count mismatch: {relative}")

    documents = _jsonl(artifact_dir / "opensearch-documents.jsonl")
    nodes = _jsonl(artifact_dir / "graph-nodes.jsonl")
    edges = _jsonl(artifact_dir / "graph-edges.jsonl")
    if len(documents) != manifest["openSearch"]["documentCount"]:
        raise ValueError("OpenSearch document count does not match manifest")
    if len(nodes) != manifest["graph"]["nodeCount"]:
        raise ValueError("Graph node count does not match manifest")
    if len(edges) != manifest["graph"]["edgeCount"]:
        raise ValueError("Graph edge count does not match manifest")
    if manifest["openSearch"].get("sourceEmbeddingExcluded") is not True:
        raise ValueError("source embedding must be excluded")
    if any("embedding" in (item.get("_source") or {}) for item in documents):
        raise ValueError("artifact unexpectedly contains source embeddings")
    return manifest


class SignedOpenSearch:
    def __init__(self, endpoint: str, region: str, session: Any) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.region = region
        self.session = session

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        data: bytes | None = None,
        content_type: str = "application/json",
        allowed: tuple[int, ...] = (200,),
    ) -> Any:
        try:
            import requests
            from botocore.auth import SigV4Auth
            from botocore.awsrequest import AWSRequest
        except ImportError as error:  # pragma: no cover - operator environment
            raise RuntimeError(
                "AWS dependencies are missing; install infra/aws/requirements.txt"
            ) from error
        url = f"{self.endpoint}/{path.lstrip('/')}"
        body = data
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": content_type,
            "Accept": "application/json",
            "X-Amz-Content-SHA256": hashlib.sha256(body or b"").hexdigest(),
        }
        credentials = self.session.get_credentials().get_frozen_credentials()
        aws_request = AWSRequest(method=method, url=url, data=body, headers=headers)
        SigV4Auth(credentials, "aoss", self.region).add_auth(aws_request)
        response = requests.request(
            method,
            url,
            data=body,
            headers=dict(aws_request.headers),
            timeout=120,
        )
        if response.status_code not in allowed:
            raise RuntimeError(
                f"OpenSearch {method} {path} failed: {response.status_code} {response.text[:1000]}"
            )
        if not response.content:
            return None
        return response.json()


def _normalize_embedding_input(text: str, max_chars: int) -> str:
    normalized = " ".join(text.split()) or "empty"
    return normalized[:max_chars]


def _embed(client: Any, model_id: str, text: str, dimensions: int, max_chars: int) -> list[float]:
    request = {
        "modelId": model_id,
        "contentType": "application/json",
        "accept": "application/json",
        "body": json.dumps(
            {
                "inputText": _normalize_embedding_input(text, max_chars),
                "dimensions": dimensions,
                "normalize": True,
            }
        ),
    }
    for attempt in range(EMBEDDING_ATTEMPTS):
        try:
            response = client.invoke_model(**request)
            break
        except Exception as error:
            details = getattr(error, "response", {}).get("Error", {})
            if (
                details.get("Code") not in {"ThrottlingException", "TooManyRequestsException"}
                or attempt == EMBEDDING_ATTEMPTS - 1
            ):
                raise
            time.sleep(min(2**attempt, 30))
    payload = json.loads(response["body"].read())
    vector = payload.get("embedding")
    if not isinstance(vector, list) or len(vector) != dimensions:
        raise RuntimeError("Titan returned an invalid embedding dimension")
    result = [float(value) for value in vector]
    if not all(math.isfinite(value) for value in result):
        raise RuntimeError("Titan returned a non-finite embedding")
    return result


def _upload_artifact(s3: Any, bucket: str, prefix: str, artifact_dir: Path, manifest: dict[str, Any]) -> None:
    for relative in sorted(manifest["files"]):
        s3.upload_file(str(artifact_dir / relative), bucket, f"{prefix}/artifact/{relative}")
    s3.upload_file(str(artifact_dir / "manifest.json"), bucket, f"{prefix}/artifact/manifest.json")


def _rewrite_object_uris(
    source: dict[str, Any], bucket: str, prefix: str, artifact_dir: Path, s3: Any
) -> dict[str, Any]:
    result = dict(source)
    content_id = str(result["contentUnitId"])
    safe_id = hashlib.sha256(content_id.encode("utf-8")).hexdigest()
    processed_key = f"{prefix}/processed/{safe_id}.txt"
    s3.put_object(
        Bucket=bucket,
        Key=processed_key,
        Body=str(result.get("text") or "").encode("utf-8"),
        ContentType="text/plain; charset=utf-8",
        Metadata={"content-unit-id-sha256": safe_id},
    )
    if str(result.get("processedObjectUri") or "").startswith("minio://"):
        result["processedObjectUri"] = f"s3://{bucket}/{processed_key}"
    if str(result.get("sourceObjectUri") or "").startswith("minio://"):
        guidance_manifest = _json(artifact_dir / "source/guidance/manifest.json")
        match = next(
            (
                item
                for item in guidance_manifest.get("documents", [])
                if item.get("documentId") == result.get("documentId")
            ),
            None,
        )
        if match is not None:
            relative = str(match["file"])
            result["sourceObjectUri"] = f"s3://{bucket}/{prefix}/artifact/source/guidance/{relative}"
        else:
            result["sourceObjectUri"] = result["processedObjectUri"]
    return result


def _prepare_search_document(
    item: dict[str, Any],
    *,
    bedrock: Any,
    s3: Any,
    bucket: str,
    prefix: str,
    artifact_dir: Path,
    search: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    document = _rewrite_object_uris(
        item["_source"], bucket, prefix, artifact_dir, s3
    )
    document["embedding"] = _embed(
        bedrock,
        search["embeddingModelId"],
        str(document.get("text") or ""),
        search["embeddingDimensions"],
        search["embeddingMaxChars"],
    )
    return str(item["_id"]), document


def _write_checkpoint(path: Path, manifest_sha256: str, completed: set[str]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {"manifestSha256": manifest_sha256, "completedDocumentIds": sorted(completed)},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_checkpoint(path: Path, manifest_sha256: str) -> set[str]:
    if not path.exists():
        return set()
    value = _json(path)
    if value.get("manifestSha256") != manifest_sha256:
        raise ValueError("checkpoint belongs to another bootstrap manifest")
    return {str(value) for value in value.get("completedDocumentIds", [])}


def _restore_s3_checkpoint(s3: Any, bucket: str, key: str, path: Path) -> None:
    if path.exists():
        return
    try:
        s3.download_file(bucket, key, str(path))
    except Exception as error:
        response = getattr(error, "response", {})
        code = str(response.get("Error", {}).get("Code", ""))
        if code not in {"404", "NoSuchKey", "NotFound"}:
            raise


def _load_opensearch(
    client: SignedOpenSearch,
    bedrock: Any,
    s3: Any,
    bucket: str,
    prefix: str,
    artifact_dir: Path,
    config: dict[str, Any],
    manifest_sha256: str,
    checkpoint_path: Path,
) -> None:
    search = config["openSearchServerless"]
    index = search["indexName"]
    snapshot_id = config["bootstrapData"]["searchSnapshotId"]
    existing_count = 0
    existing = client.request("GET", index, allowed=(200, 404))
    if _index_not_found(existing):
        definition = _serverless_index_definition(
            _json(artifact_dir / "opensearch-index.json"),
            search["embeddingDimensions"],
        )
        client.request("PUT", index, json_body=definition, allowed=(200, 201))
        # Serverless can accept index creation before the index is queryable.
        _wait_for_index(client, index)
    else:
        total = client.request("POST", f"{index}/_count", json_body={"query": {"match_all": {}}})[
            "count"
        ]
        current = client.request(
            "POST",
            f"{index}/_count",
            json_body={
                "query": {
                    "term": {
                        "sourceSnapshotId": snapshot_id
                    }
                }
            },
        )["count"]
        if total != current:
            raise RuntimeError(
                "OpenSearch index contains another snapshot; bootstrap will not overwrite it"
            )
        existing_count = current
    client.request(
        "POST",
        f"{index}/_analyze",
        json_body={"analyzer": "ja_morph", "text": "公開買付けの手続"},
    )

    documents = _jsonl(artifact_dir / "opensearch-documents.jsonl")
    checkpoint_key = f"{prefix}/state/opensearch-checkpoint.json"
    _restore_s3_checkpoint(s3, bucket, checkpoint_key, checkpoint_path)
    completed = _load_checkpoint(checkpoint_path, manifest_sha256)
    if existing_count != len(completed):
        existing_count = _wait_for_snapshot_count(
            client, index, snapshot_id, len(completed)
        )
    if existing_count != len(completed):
        raise RuntimeError(
            "OpenSearch document count does not match the S3 checkpoint; "
            "bootstrap refuses to create duplicate Serverless vector documents"
        )
    pending = [item for item in documents if str(item["_id"]) not in completed]
    with ThreadPoolExecutor(max_workers=_embedding_workers()) as executor:
        for batch in _chunks(pending, BULK_SIZE):
            prepared = list(
                executor.map(
                    lambda item: _prepare_search_document(
                        item,
                        bedrock=bedrock,
                        s3=s3,
                        bucket=bucket,
                        prefix=prefix,
                        artifact_dir=artifact_dir,
                        search=search,
                    ),
                    batch,
                )
            )
            lines: list[str] = []
            batch_ids: list[str] = []
            for document_id, document in prepared:
                lines.append(json.dumps(_serverless_bulk_action(index)))
                lines.append(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
                batch_ids.append(document_id)
            body = ("\n".join(lines) + "\n").encode("utf-8")
            response = client.request(
                "POST",
                "_bulk",
                data=body,
                content_type="application/x-ndjson",
            )
            if response.get("errors"):
                failures = [item for item in response.get("items", []) if item.get("index", {}).get("error")]
                raise RuntimeError(f"OpenSearch bulk failed: {failures[:3]}")
            completed.update(batch_ids)
            _write_checkpoint(checkpoint_path, manifest_sha256, completed)
            s3.upload_file(str(checkpoint_path), bucket, checkpoint_key)
    count = _wait_for_snapshot_count(
        client, index, snapshot_id, len(documents)
    )
    if count != len(documents):
        raise RuntimeError(
            f"OpenSearch verification count did not converge: {count} != {len(documents)}"
        )


def _neptune_query(client: Any, graph_id: str, query: str, parameters: dict[str, Any] | None = None) -> Any:
    response = client.execute_query(
        graphIdentifier=graph_id,
        queryString=query,
        language="OPEN_CYPHER",
        parameters=parameters or {},
    )
    payload = response["payload"].read()
    return json.loads(payload) if payload else None


def _query_count(payload: Any, field: str = "count") -> int:
    if not isinstance(payload, dict):
        raise RuntimeError("Neptune count query returned no object")
    results = payload.get("results")
    if not isinstance(results, list) or len(results) != 1:
        raise RuntimeError("Neptune count query returned an unexpected result")
    value = results[0].get(field)
    if not isinstance(value, int):
        raise RuntimeError("Neptune count query returned no integer count")
    return value


def _load_graph(client: Any, graph_id: str, artifact_dir: Path) -> None:
    nodes = _jsonl(artifact_dir / "graph-nodes.jsonl")
    edges = _jsonl(artifact_dir / "graph-edges.jsonl")
    snapshot_id = str(nodes[0]["properties"]["sourceSnapshotId"])
    total_nodes = _query_count(
        _neptune_query(client, graph_id, "MATCH (n:GraphNode) RETURN count(n) AS count")
    )
    snapshot_nodes = _query_count(
        _neptune_query(
            client,
            graph_id,
            "MATCH (n:GraphNode {sourceSnapshotId: $snapshotId}) RETURN count(n) AS count",
            {"snapshotId": snapshot_id},
        )
    )
    if total_nodes != snapshot_nodes:
        raise RuntimeError(
            "Neptune graph contains another snapshot; bootstrap will not mix datasets"
        )
    nodes_by_labels: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for item in nodes:
        labels = tuple(sorted({str(label) for label in item["labels"]}))
        if not labels or not all(LABEL_PATTERN.fullmatch(label) for label in labels):
            raise ValueError(f"invalid Graph labels: {labels}")
        properties = _neptune_properties(dict(item["properties"]))
        graph_node_id = properties.get("graphNodeId")
        if not graph_node_id:
            raise ValueError("Graph node has no graphNodeId")
        nodes_by_labels[labels].append({"graphNodeId": graph_node_id, "props": properties})
    for labels, rows in nodes_by_labels.items():
        label_expression = ":".join(labels)
        for batch in _chunks(rows, GRAPH_BATCH_SIZE):
            _neptune_query(
                client,
                graph_id,
                f"UNWIND $rows AS row MERGE (n:{label_expression} {{graphNodeId: row.graphNodeId}}) SET n += row.props",
                {"rows": batch},
            )

    edges_by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in edges:
        edge_type = str(item["relationType"])
        if not LABEL_PATTERN.fullmatch(edge_type):
            raise ValueError(f"invalid Graph relation type: {edge_type}")
        properties = _neptune_properties(dict(item["properties"]))
        graph_edge_id = properties.get("graphEdgeId")
        if not graph_edge_id:
            raise ValueError("Graph edge has no graphEdgeId")
        edges_by_type[edge_type].append(
            {
                "from": item["sourceGraphNodeId"],
                "to": item["targetGraphNodeId"],
                "graphEdgeId": graph_edge_id,
                "props": properties,
            }
        )
    for edge_type, rows in edges_by_type.items():
        for batch in _chunks(rows, GRAPH_BATCH_SIZE):
            _neptune_query(
                client,
                graph_id,
                f"UNWIND $rows AS row "
                f"MATCH (a:GraphNode {{graphNodeId: row.from}}), (b:GraphNode {{graphNodeId: row.to}}) "
                f"MERGE (a)-[r:{edge_type} {{graphEdgeId: row.graphEdgeId}}]->(b) SET r += row.props",
                {"rows": batch},
            )
    actual_nodes = _query_count(
        _neptune_query(
            client,
            graph_id,
            "MATCH (n:GraphNode {sourceSnapshotId: $snapshotId}) RETURN count(n) AS count",
            {"snapshotId": snapshot_id},
        )
    )
    actual_edges = _query_count(
        _neptune_query(
            client,
            graph_id,
            "MATCH (:GraphNode)-[r]->(:GraphNode) WHERE r.sourceSnapshotId = $snapshotId RETURN count(r) AS count",
            {"snapshotId": snapshot_id},
        )
    )
    if actual_nodes != len(nodes) or actual_edges != len(edges):
        raise RuntimeError(
            f"Neptune verification mismatch: nodes={actual_nodes}/{len(nodes)} "
            f"edges={actual_edges}/{len(edges)}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--bucket")
    parser.add_argument("--opensearch-endpoint")
    parser.add_argument("--neptune-graph-id")
    parser.add_argument("--profile")
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    config = _json(args.config)
    manifest = validate_artifact(args.artifact_dir, config)
    manifest_path = args.artifact_dir / "manifest.json"
    manifest_sha256 = _sha256(manifest_path)
    print(
        json.dumps(
            {
                "validated": True,
                "manifestSha256": manifest_sha256,
                "documents": manifest["openSearch"]["documentCount"],
                "nodes": manifest["graph"]["nodeCount"],
                "edges": manifest["graph"]["edgeCount"],
                "apply": args.apply,
            },
            ensure_ascii=False,
        )
    )
    if not args.apply:
        return 0
    if not args.bucket or not args.opensearch_endpoint or not args.neptune_graph_id:
        raise ValueError("--apply requires bucket, OpenSearch endpoint, and Neptune graph ID")

    try:
        import boto3
    except ImportError as error:  # pragma: no cover - operator environment
        raise RuntimeError(
            "AWS dependencies are missing; install infra/aws/requirements.txt"
        ) from error
    session = boto3.Session(profile_name=args.profile, region_name=config["region"])
    identity = session.client("sts").get_caller_identity()
    if identity.get("Account") != config["account"]:
        raise ValueError("active AWS account does not match environment config")
    prefix = config["bootstrapData"]["s3Prefix"].strip("/")
    s3 = session.client("s3")
    _upload_artifact(s3, args.bucket, prefix, args.artifact_dir, manifest)
    checkpoint = args.checkpoint or args.artifact_dir.parent / f"aws-bootstrap-{manifest_sha256[:12]}.checkpoint.json"
    _load_opensearch(
        SignedOpenSearch(args.opensearch_endpoint, config["region"], session),
        session.client("bedrock-runtime"),
        s3,
        args.bucket,
        prefix,
        args.artifact_dir,
        config,
        manifest_sha256,
        checkpoint,
    )
    from botocore.config import Config

    neptune = session.client(
        "neptune-graph",
        config=Config(
            read_timeout=None,
            retries={"mode": "standard", "total_max_attempts": 1},
        ),
    )
    _load_graph(neptune, args.neptune_graph_id, args.artifact_dir)
    print("AWS bootstrap completed and OpenSearch count was verified")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as error:
        print(f"bootstrap failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
