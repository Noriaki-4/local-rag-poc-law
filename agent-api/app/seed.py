import json
from pathlib import Path
from typing import Any

from minio import Minio
from minio.error import S3Error

from .config import settings
from .embeddings import embed_text
from .graph_client import GraphClient
from .opensearch_client import OpenSearchClient


def seed_all(os_client: OpenSearchClient, graph_client: GraphClient) -> dict[str, Any]:
    samples_dir = settings.samples_dir
    mapping = _read_json(samples_dir / "metadata" / "opensearch_index_mapping.sample.json")
    os_client.recreate_index(mapping)

    documents = _opensearch_documents(samples_dir)
    for document in documents:
        os_client.index_document(document)
    os_client.refresh()

    nodes = _read_jsonl(samples_dir / "metadata" / "nodes.sample.jsonl")
    edges = _read_jsonl(samples_dir / "metadata" / "edges.sample.jsonl")
    _assert_no_dangling_edges(nodes, edges)
    graph_client.clear()
    graph_client.seed_nodes(nodes)
    graph_client.seed_edges(edges)

    minio_count = _seed_minio(samples_dir, documents)

    return {
        "opensearchDocuments": len(documents),
        "graphNodes": len(nodes),
        "graphEdges": len(edges),
        "minioObjects": minio_count,
    }


def _opensearch_documents(samples_dir: Path) -> list[dict[str, Any]]:
    sample = _read_json(samples_dir / "metadata" / "opensearch_document.sample.json")
    law_fsa = dict(sample)
    law_fsa["text"] = "金融商品取引法第2条第1項は、有価証券の定義に関する条文である。法令上の判断には条文本文を根拠として確認する。"
    law_fsa["embedding"] = embed_text(_embedding_text(law_fsa), settings.embedding_dimension)
    return [law_fsa]


def _embedding_text(document: dict[str, Any]) -> str:
    return " ".join(str(document.get(key) or "") for key in ["title", "heading", "sectionPath", "text"])


def _seed_minio(samples_dir: Path, documents: list[dict[str, Any]]) -> int:
    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=False,
    )
    try:
        if not client.bucket_exists(settings.minio_bucket):
            client.make_bucket(settings.minio_bucket)
    except S3Error:
        raise

    count = 0
    for document in documents:
        content = f"# {document['title']} {document['heading']}\n\n{document['text']}\n"
        object_name = document["processedObjectUri"].replace("minio://knowledge-root/", "")
        client.put_object(
            settings.minio_bucket,
            object_name,
            data=_Bytes(content.encode("utf-8")),
            length=len(content.encode("utf-8")),
            content_type="text/markdown; charset=utf-8",
        )
        count += 1

    for path in samples_dir.rglob("*"):
        if path.is_file():
            relative_path = path.relative_to(samples_dir)
            if relative_path.parts[0] == "source-documents":
                object_name = str(relative_path)
            else:
                object_name = f"eval-data/samples/{relative_path}"
            data = path.read_bytes()
            client.put_object(settings.minio_bucket, object_name, data=_Bytes(data), length=len(data))
            count += 1
    return count


class _Bytes:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def read(self, size: int = -1) -> bytes:
        if size == -1:
            size = len(self.data) - self.offset
        chunk = self.data[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


def _assert_no_dangling_edges(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    node_ids = {node["graphNodeId"] for node in nodes}
    missing = [
        edge
        for edge in edges
        if edge["fromGraphNodeId"] not in node_ids or edge["toGraphNodeId"] not in node_ids
    ]
    if missing:
        raise ValueError(f"Dangling graph edges found: {missing}")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
