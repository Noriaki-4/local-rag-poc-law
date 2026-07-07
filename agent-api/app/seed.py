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

    law_local = {
        "documentId": "law-322AC0000000067",
        "contentUnitId": "law-322AC0000000067-article-16",
        "parentContentUnitId": None,
        "chunkId": "law-322AC0000000067-article-16",
        "deptCode": "common",
        "docType": "law",
        "contentDomain": "local_government",
        "title": "地方自治法",
        "heading": "第16条",
        "sectionPath": "地方自治法 > 第16条",
        "articleNumber": 16,
        "paragraphNumber": None,
        "itemNumber": None,
        "text": "地方自治法第16条は、条例の公布に関する根拠条文である。条例は議会の議決後、所定の手続により公布される。",
        "publishStatus": "published",
        "isLatest": True,
        "confidentiality": "public",
        "clearanceLevel": 1,
        "sourceObjectUri": "minio://knowledge-root/source-documents/dept=common/docType=law/law-322AC0000000067/source.xml",
        "processedObjectUri": "minio://knowledge-root/derived-artifacts/vector-documents/dept=common/docType=law/law-322AC0000000067/law-322AC0000000067-article-16.md",
        "sourcePage": None,
        "parserType": "egov_xml_rule",
        "chunkStrategy": "law_article_paragraph_split_v1",
    }
    law_local["embedding"] = embed_text(_embedding_text(law_local), settings.embedding_dimension)

    manual = {
        "documentId": "manual-ordinance-001",
        "contentUnitId": "manual-ordinance-001-step-008",
        "parentContentUnitId": None,
        "chunkId": "manual-ordinance-001-step-008",
        "deptCode": "general-affairs",
        "docType": "manual",
        "contentDomain": "legislation",
        "title": "条例制定・改正業務マニュアル",
        "heading": "議決後、公布・施行手続を行う",
        "sectionPath": "条例制定・改正業務マニュアル > Step 008",
        "articleNumber": None,
        "paragraphNumber": None,
        "itemNumber": None,
        "text": "議会で可決された条例案は、公布・施行手続を行う。担当課は公布日、施行日、関係部署への周知を確認する。",
        "publishStatus": "published",
        "isLatest": True,
        "confidentiality": "internal",
        "clearanceLevel": 2,
        "sourceObjectUri": "minio://knowledge-root/source-documents/dept=general-affairs/docType=manual/manual-ordinance-001/source.md",
        "processedObjectUri": "minio://knowledge-root/derived-artifacts/vector-documents/dept=general-affairs/docType=manual/manual-ordinance-001/manual-ordinance-001-step-008.md",
        "sourcePage": None,
        "parserType": "manual_markdown_rule",
        "chunkStrategy": "manual_step_split_v1",
    }
    manual["embedding"] = embed_text(_embedding_text(manual), settings.embedding_dimension)
    return [law_fsa, law_local, manual]


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
            object_name = f"eval-data/samples/{path.relative_to(samples_dir)}"
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
