import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from minio import Minio
from minio.error import S3Error
import requests

from .config import settings
from .embeddings import embed_text, embed_texts
from .graph_client import GraphClient
from .opensearch_client import OpenSearchClient

EGOV_LAW_ID_PATTERN = re.compile(r"laws\.e-gov\.go\.jp/law/([^/?#]+)")


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
    egov_nodes, egov_edges = _graph_artifacts_from_documents(documents)
    nodes = _dedupe_by_key([*nodes, *egov_nodes], "graphNodeId")
    edges = _dedupe_by_key([*edges, *egov_edges], "graphEdgeId")
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
    law_fsa["embedding"] = embed_text(_embedding_text(law_fsa))
    documents = [law_fsa]
    if settings.seed_lawqa_egov:
        documents.extend(_lawqa_egov_documents(samples_dir))
    return _dedupe_by_key(documents, "contentUnitId")


def _lawqa_egov_documents(samples_dir: Path) -> list[dict[str, Any]]:
    law_ids = _lawqa_egov_law_ids(samples_dir)
    documents: list[dict[str, Any]] = []
    for law_id in law_ids:
        documents.extend(_egov_law_documents(law_id))
    for batch in _chunks(documents, settings.embedding_batch_size):
        embeddings = embed_texts([_embedding_text(document) for document in batch])
        for document, embedding in zip(batch, embeddings, strict=True):
            document["embedding"] = embedding
    return documents


def _lawqa_egov_law_ids(samples_dir: Path) -> list[str]:
    explicit_ids = [item.strip() for item in settings.lawqa_egov_law_ids.split(",") if item.strip()]
    if explicit_ids:
        return sorted(set(explicit_ids))

    payload = _read_lawqa_payload(samples_dir)
    law_ids: set[str] = set()
    samples = payload.get("samples") if isinstance(payload, dict) else payload
    if not isinstance(samples, list):
        raise ValueError("lawqa_jp payload must be a list or an object with a samples list")
    for sample in samples:
        for reference in sample.get("references", []):
            match = EGOV_LAW_ID_PATTERN.search(str(reference))
            if match:
                law_ids.add(match.group(1))
    return sorted(law_ids)


def _read_lawqa_payload(samples_dir: Path) -> Any:
    if settings.lawqa_eval_url:
        response = requests.get(settings.lawqa_eval_url, timeout=60)
        response.raise_for_status()
        return response.json()
    if settings.lawqa_eval_path:
        path = Path(settings.lawqa_eval_path)
        if not path.is_absolute():
            path = samples_dir / path
    else:
        path = samples_dir / "eval" / "lawqa_eval_item.sample.jsonl"
    if path.suffix == ".jsonl":
        return {"samples": _read_jsonl(path)}
    return json.loads(path.read_text(encoding="utf-8"))


def _egov_law_documents(law_id: str) -> list[dict[str, Any]]:
    url = f"{settings.egov_api_base_url.rstrip('/')}/lawdata/{law_id}"
    response = requests.get(url, timeout=120)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    code = root.findtext("./Result/Code")
    if code and code != "0":
        message = root.findtext("./Result/Message") or "unknown e-Gov API error"
        raise ValueError(f"e-Gov API error for {law_id}: {message}")

    title = root.findtext(".//LawTitle") or law_id
    law_num = root.findtext(".//LawNum")
    documents = []
    for article in root.iter("Article"):
        article_num = article.get("Num") or _article_num_from_title(article.findtext("ArticleTitle"))
        if not article_num:
            continue
        heading = article.findtext("ArticleTitle") or f"第{article_num}条"
        caption = article.findtext("ArticleCaption")
        text = _article_text(article)
        if not text:
            continue
        content_unit_id = f"law-{law_id}-article-{article_num}"
        documents.append(
            {
                "documentId": f"law-{law_id}",
                "contentUnitId": content_unit_id,
                "parentContentUnitId": None,
                "chunkId": content_unit_id,
                "deptCode": "common",
                "docType": "law",
                "contentDomain": "legal",
                "title": title,
                "heading": f"{heading} {caption or ''}".strip(),
                "sectionPath": f"{title} > {heading}",
                "articleNumber": _int_or_none(article_num),
                "paragraphNumber": None,
                "itemNumber": None,
                "text": text,
                "publishStatus": "published",
                "isLatest": True,
                "confidentiality": "public",
                "clearanceLevel": 1,
                "sourceObjectUri": url,
                "processedObjectUri": (
                    "minio://knowledge-root/derived-artifacts/vector-documents/"
                    f"dept=common/docType=law/law-{law_id}/{content_unit_id}.md"
                ),
                "sourcePage": None,
                "parserType": "egov_xml_rule",
                "chunkStrategy": "law_article_split_v1",
                "lawNum": law_num,
            }
        )
    return documents


def _article_text(article: ET.Element) -> str:
    parts = []
    caption = article.findtext("ArticleCaption")
    title = article.findtext("ArticleTitle")
    if caption:
        parts.append(caption)
    if title:
        parts.append(title)
    for paragraph in article.findall("Paragraph"):
        paragraph_num = paragraph.findtext("ParagraphNum")
        sentences = [
            sentence.text.strip()
            for sentence in paragraph.iter("Sentence")
            if sentence.text and sentence.text.strip()
        ]
        if not sentences:
            continue
        prefix = paragraph_num.strip() if paragraph_num and paragraph_num.strip() else ""
        paragraph_text = "".join(sentences)
        parts.append(f"{prefix}{paragraph_text}")
    return "\n".join(parts).strip()


def _article_num_from_title(title: str | None) -> str | None:
    if not title:
        return None
    match = re.search(r"第([一二三四五六七八九十百千〇零\d_]+)条", title)
    return match.group(1) if match else None


def _int_or_none(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _graph_artifacts_from_documents(documents: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    nodes_by_id: dict[str, dict[str, Any]] = {}
    edges = []
    for document in documents:
        if document.get("docType") != "law":
            continue
        document_id = document["documentId"]
        content_unit_id = document["contentUnitId"]
        nodes_by_id[document_id] = {
            "graphNodeId": document_id,
            "nodeType": "Document",
            "documentId": document_id,
            "deptCode": document.get("deptCode"),
            "docType": document.get("docType"),
            "contentDomain": document.get("contentDomain"),
            "title": document.get("title"),
            "publishStatus": document.get("publishStatus"),
            "isLatest": document.get("isLatest"),
            "confidentiality": document.get("confidentiality"),
        }
        nodes_by_id[content_unit_id] = {
            "graphNodeId": content_unit_id,
            "nodeType": "Article",
            "documentId": document_id,
            "contentUnitId": content_unit_id,
            "deptCode": document.get("deptCode"),
            "docType": document.get("docType"),
            "contentDomain": document.get("contentDomain"),
            "heading": document.get("heading"),
            "publishStatus": document.get("publishStatus"),
            "isLatest": document.get("isLatest"),
            "confidentiality": document.get("confidentiality"),
        }
        edge_id = f"edge-{document_id}-has-content-unit-{content_unit_id.removeprefix(document_id + '-')}"
        edges.append(
            {
                "graphEdgeId": edge_id,
                "edgeType": "HAS_CONTENT_UNIT",
                "fromGraphNodeId": document_id,
                "toGraphNodeId": content_unit_id,
                "documentId": document_id,
                "relationSource": "xml_rule",
                "relationConfidence": 1.0,
                "publishStatus": "published",
                "isLatest": True,
            }
        )
    return list(nodes_by_id.values()), edges


def _embedding_text(document: dict[str, Any]) -> str:
    return " ".join(str(document.get(key) or "") for key in ["title", "heading", "sectionPath", "text"])


def _dedupe_by_key(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for item in items:
        deduped[item[key]] = item
    return list(deduped.values())


def _chunks(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    chunk_size = max(size, 1)
    return [items[index : index + chunk_size] for index in range(0, len(items), chunk_size)]


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
