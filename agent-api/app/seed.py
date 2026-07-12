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
ARTICLE_REFERENCE_PATTERN = re.compile(
    r"(?<![法令])第([一二三四五六七八九十百千〇零\d]+)条((?:の[一二三四五六七八九十百千〇零\d]+)*)"
)
JAPANESE_DIGITS = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
JAPANESE_UNITS = {"十": 10, "百": 100, "千": 1000}


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


def _egov_law_documents(law_id_spec: str) -> list[dict[str, Any]]:
    law_id, article_range = _parse_law_id_spec(law_id_spec)
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

    # 本則と附則は条番号を別々に振るため（本則第8条と附則第8条が衝突する）、
    # 附則は law-{id}-suppl-{k}-article-{num} という別名前空間に分離する。
    suppl_provisions = root.findall(".//SupplProvision")
    main = root.find(".//MainProvision")
    if main is not None:
        main_articles = list(main.iter("Article"))
    else:
        suppl_article_ids = {id(article) for suppl in suppl_provisions for article in suppl.iter("Article")}
        main_articles = [article for article in root.iter("Article") if id(article) not in suppl_article_ids]

    if article_range:
        start, end = article_range
        main_articles = [
            article
            for article in main_articles
            if (base := _article_base_num(article.get("Num"))) is not None and start <= base <= end
        ]

    documents: list[dict[str, Any]] = []
    documents.extend(
        _section_documents(main_articles, law_id, title, law_num, url, id_prefix="article", section_key="main")
    )
    # 範囲指定時は該当法令の一部条文だけが目的のため、附則（別条番号体系の経過措置等）は対象外にする。
    if not article_range:
        for suppl_index, suppl in enumerate(suppl_provisions):
            section_key = f"suppl-{suppl_index}"
            documents.extend(
                _section_documents(
                    list(suppl.iter("Article")),
                    law_id,
                    title,
                    law_num,
                    url,
                    id_prefix=f"{section_key}-article",
                    section_key=section_key,
                    section_label=_suppl_label(suppl),
                )
            )
    return documents


def _parse_law_id_spec(law_id_spec: str) -> tuple[str, tuple[int, int] | None]:
    """'129AC0000000089:601-622_2' のような指定を (law_id, (開始条番号, 終了条番号)) に分解する。
    範囲省略時は (law_id, None) を返し、全条投入する。"""
    if ":" not in law_id_spec:
        return law_id_spec, None
    law_id, range_text = law_id_spec.split(":", 1)
    start_text, _, end_text = range_text.partition("-")
    start = _article_base_num(start_text)
    end = _article_base_num(end_text) if end_text else start
    if start is None or end is None:
        raise ValueError(f"Invalid article range in LAWQA_EGOV_LAW_IDS entry: {law_id_spec!r}")
    return law_id, (start, end)


def _article_base_num(value: str | None) -> int | None:
    """'622_2' の枝番を無視した基本条番号（622）を返す。"""
    if not value:
        return None
    try:
        return int(value.split("_", 1)[0])
    except ValueError:
        return None


def _suppl_label(suppl: ET.Element) -> str:
    label = (suppl.findtext("SupplProvisionLabel") or "附則").strip()
    amend_law_num = suppl.get("AmendLawNum")
    return f"{label}（{amend_law_num}）" if amend_law_num else label


def _section_documents(
    articles: list[ET.Element],
    law_id: str,
    title: str,
    law_num: str | None,
    url: str,
    id_prefix: str,
    section_key: str,
    section_label: str | None = None,
) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for article in articles:
        article_num = article.get("Num") or _article_num_from_title(article.findtext("ArticleTitle"))
        if not article_num:
            continue
        base_heading = article.findtext("ArticleTitle") or f"第{article_num}条"
        heading = f"{section_label} {base_heading}".strip() if section_label else base_heading
        caption = article.findtext("ArticleCaption")
        text = _article_text(article)
        if not text:
            continue
        article_content_unit_id = f"law-{law_id}-{id_prefix}-{article_num}"
        common = {
            "documentId": f"law-{law_id}",
            "deptCode": "common",
            "docType": "law",
            "contentDomain": "legal",
            "title": title,
            "articleNumber": _int_or_none(article_num),
            "provisionType": "main" if section_key == "main" else "supplementary",
            "sectionKey": section_key,
            "publishStatus": "published",
            "isLatest": True,
            "confidentiality": "public",
            "clearanceLevel": 1,
            "sourceObjectUri": url,
            "processedObjectUri": (
                "minio://knowledge-root/derived-artifacts/vector-documents/"
                f"dept=common/docType=law/law-{law_id}/"
            ),
            "sourcePage": None,
            "parserType": "egov_xml_rule",
            "lawNum": law_num,
        }
        for chunk in _article_chunks(article, article_content_unit_id, heading, caption):
            content_unit_id = chunk["contentUnitId"]
            documents.append(
                {
                    **common,
                    **chunk,
                    "chunkId": content_unit_id,
                    "sectionPath": f"{title} > {chunk['heading']}",
                    "processedObjectUri": f"{common['processedObjectUri']}{content_unit_id}.md",
                }
            )
    return documents


def _article_chunks(
    article: ET.Element,
    article_content_unit_id: str,
    heading: str,
    caption: str | None,
) -> list[dict[str, Any]]:
    full_heading = f"{heading} {caption or ''}".strip()
    full_text = _article_text(article)
    paragraphs = article.findall("Paragraph")
    if len(paragraphs) <= 1 and len(full_text) <= settings.embedding_max_chars:
        return [
            {
                "contentUnitId": article_content_unit_id,
                "parentContentUnitId": None,
                "articleContentUnitId": article_content_unit_id,
                "heading": full_heading,
                "paragraphNumber": None,
                "itemNumber": None,
                "text": full_text,
                "chunkStrategy": "law_article_v2",
            }
        ]

    chunks = []
    for paragraph_index, paragraph in enumerate(paragraphs, start=1):
        paragraph_num_text = (paragraph.findtext("ParagraphNum") or str(paragraph_index)).strip()
        paragraph_num = _japanese_number_to_int(paragraph_num_text) or paragraph_index
        paragraph_id = f"{article_content_unit_id}-paragraph-{paragraph_num}"
        paragraph_text = _paragraph_text(paragraph)
        items = paragraph.findall("Item")
        if items and len(paragraph_text) > settings.embedding_max_chars:
            intro_text = _direct_sentence_text(paragraph)
            if intro_text:
                chunks.append(
                    {
                        "contentUnitId": paragraph_id,
                        "parentContentUnitId": article_content_unit_id,
                        "articleContentUnitId": article_content_unit_id,
                        "heading": f"{full_heading} 第{paragraph_num}項",
                        "paragraphNumber": paragraph_num,
                        "itemNumber": None,
                        "text": f"{paragraph_num}{intro_text}",
                        "chunkStrategy": "law_article_paragraph_item_split_v2",
                    }
                )
            for item_index, item in enumerate(items, start=1):
                item_num_text = (item.get("Num") or item.findtext("ItemTitle") or str(item_index)).strip()
                # 枝番の号（例: '2_2' = 第二号の二）は int 化すると隣の号と衝突するため接尾辞のまま使う。
                item_suffix = _num_suffix(item_num_text, item_index)
                item_text = _element_sentence_text(item)
                if not item_text:
                    continue
                chunks.append(
                    {
                        "contentUnitId": f"{paragraph_id}-item-{item_suffix}",
                        "parentContentUnitId": paragraph_id,
                        "articleContentUnitId": article_content_unit_id,
                        "heading": f"{full_heading} 第{paragraph_num}項第{item_suffix.replace('_', 'の')}号",
                        "paragraphNumber": paragraph_num,
                        "itemNumber": _int_or_none(item_suffix.split("_")[0]),
                        "text": item_text,
                        "chunkStrategy": "law_article_paragraph_item_split_v2",
                    }
                )
        elif paragraph_text:
            chunks.append(
                {
                    "contentUnitId": paragraph_id,
                    "parentContentUnitId": article_content_unit_id,
                    "articleContentUnitId": article_content_unit_id,
                    "heading": f"{full_heading} 第{paragraph_num}項",
                    "paragraphNumber": paragraph_num,
                    "itemNumber": None,
                    "text": paragraph_text,
                    "chunkStrategy": "law_article_paragraph_split_v2",
                }
            )
    return chunks or [
        {
            "contentUnitId": article_content_unit_id,
            "parentContentUnitId": None,
            "articleContentUnitId": article_content_unit_id,
            "heading": full_heading,
            "paragraphNumber": None,
            "itemNumber": None,
            "text": full_text,
            "chunkStrategy": "law_article_v2",
        }
    ]


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


def _paragraph_text(paragraph: ET.Element) -> str:
    paragraph_num = (paragraph.findtext("ParagraphNum") or "").strip()
    text = _element_sentence_text(paragraph)
    return f"{paragraph_num}{text}".strip()


def _direct_sentence_text(paragraph: ET.Element) -> str:
    sentences = paragraph.findall("./ParagraphSentence//Sentence")
    return "".join("".join(sentence.itertext()).strip() for sentence in sentences)


def _element_sentence_text(element: ET.Element) -> str:
    return "".join(
        "".join(sentence.itertext()).strip()
        for sentence in element.iter("Sentence")
    )


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
        article_content_unit_id = document.get("articleContentUnitId") or document.get("parentContentUnitId") or content_unit_id
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
            "clearanceLevel": document.get("clearanceLevel", 3),
        }
        nodes_by_id[article_content_unit_id] = {
            "graphNodeId": article_content_unit_id,
            "nodeType": "Article",
            "documentId": document_id,
            "contentUnitId": article_content_unit_id,
            "deptCode": document.get("deptCode"),
            "docType": document.get("docType"),
            "contentDomain": document.get("contentDomain"),
            "heading": document.get("heading"),
            "publishStatus": document.get("publishStatus"),
            "isLatest": document.get("isLatest"),
            "confidentiality": document.get("confidentiality"),
            "clearanceLevel": document.get("clearanceLevel", 3),
        }
        edge_id = f"edge-{document_id}-has-content-unit-{article_content_unit_id.removeprefix(document_id + '-')}"
        edges.append(
            {
                "graphEdgeId": edge_id,
                "edgeType": "HAS_CONTENT_UNIT",
                "fromGraphNodeId": document_id,
                "toGraphNodeId": article_content_unit_id,
                "documentId": document_id,
                "relationSource": "xml_rule",
                "relationConfidence": 1.0,
                "publishStatus": "published",
                "isLatest": True,
            }
        )
        if content_unit_id != article_content_unit_id:
            parent_id = document.get("parentContentUnitId") or article_content_unit_id
            if parent_id != article_content_unit_id and parent_id not in nodes_by_id:
                nodes_by_id[parent_id] = {
                    "graphNodeId": parent_id,
                    "nodeType": "Paragraph",
                    "documentId": document_id,
                    "contentUnitId": parent_id,
                    "publishStatus": document.get("publishStatus"),
                    "isLatest": document.get("isLatest"),
                    "confidentiality": document.get("confidentiality"),
                    "clearanceLevel": document.get("clearanceLevel", 3),
                }
                edges.append(_hierarchy_edge(article_content_unit_id, parent_id, document_id))
            node_type = "Item" if document.get("itemNumber") is not None else "Paragraph"
            nodes_by_id[content_unit_id] = {
                "graphNodeId": content_unit_id,
                "nodeType": node_type,
                "documentId": document_id,
                "contentUnitId": content_unit_id,
                "heading": document.get("heading"),
                "publishStatus": document.get("publishStatus"),
                "isLatest": document.get("isLatest"),
                "confidentiality": document.get("confidentiality"),
                "clearanceLevel": document.get("clearanceLevel", 3),
            }
            edges.append(_hierarchy_edge(parent_id, content_unit_id, document_id))
    edges.extend(_reference_edges(documents))
    return list(nodes_by_id.values()), edges


def _hierarchy_edge(from_id: str, to_id: str, document_id: str) -> dict[str, Any]:
    return {
        "graphEdgeId": f"edge-{from_id}-has-content-unit-{to_id.removeprefix(document_id + '-')}",
        "edgeType": "HAS_CONTENT_UNIT",
        "fromGraphNodeId": from_id,
        "toGraphNodeId": to_id,
        "documentId": document_id,
        "relationSource": "xml_rule",
        "relationConfidence": 1.0,
        "publishStatus": "published",
        "isLatest": True,
    }


def _reference_edges(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    law_documents: dict[str, list[dict[str, Any]]] = {}
    for document in documents:
        if document.get("docType") == "law":
            law_documents.setdefault(document["documentId"], []).append(document)

    edges = []
    for document_id, scoped_documents in law_documents.items():
        # 前条・次条の隣接は本則・附則をまたがないよう sectionKey ごとに並べる。
        section_article_ids: dict[str, list[str]] = {}
        for document in scoped_documents:
            section_key = document.get("sectionKey", "main")
            article_id = document.get("articleContentUnitId") or document.get("parentContentUnitId") or document["contentUnitId"]
            ids = section_article_ids.setdefault(section_key, [])
            if article_id not in ids:
                ids.append(article_id)
        section_index = {
            section_key: {article_id: index for index, article_id in enumerate(ids)}
            for section_key, ids in section_article_ids.items()
        }
        # 「第N条」は本則の条文を指す慣行なので、本則の存在する条だけを参照先にする。
        main_article_ids = set(section_article_ids.get("main", []))
        for document in scoped_documents:
            source_id = document["contentUnitId"]
            section_key = document.get("sectionKey", "main")
            source_article_id = document.get("articleContentUnitId") or document.get("parentContentUnitId") or source_id
            ids = section_article_ids[section_key]
            index = section_index[section_key][source_article_id]
            text = str(document.get("text") or "")
            targets = set(_explicit_article_reference_ids(document_id, text)) & main_article_ids
            if "前条" in text and index > 0:
                targets.add(ids[index - 1])
            if "次条" in text and index + 1 < len(ids):
                targets.add(ids[index + 1])
            for target_id in sorted(targets):
                if target_id == source_id:
                    continue
                edge_id = f"edge-{source_id}-references-{target_id.removeprefix(document_id + '-')}"
                edges.append(
                    {
                        "graphEdgeId": edge_id,
                        "edgeType": "REFERENCES",
                        "fromGraphNodeId": source_id,
                        "toGraphNodeId": target_id,
                        "documentId": document_id,
                        "relationSource": "xml_reference_rule",
                        "relationConfidence": 0.9,
                        "publishStatus": "published",
                        "isLatest": True,
                    }
                )
    return edges


def _explicit_article_reference_ids(document_id: str, text: str) -> list[str]:
    references = []
    for match in ARTICLE_REFERENCE_PATTERN.finditer(text):
        parts = [match.group(1), *match.group(2).removeprefix("の").split("の")]
        numbers = [_japanese_number_to_int(part) for part in parts if part]
        if any(number is None for number in numbers):
            continue
        suffix = "_".join(str(number) for number in numbers)
        references.append(f"{document_id}-article-{suffix}")
    return references


def _num_suffix(value: str, fallback_index: int) -> str:
    """条・号番号を contentUnitId の接尾辞へ変換する。'2_2' などの枝番はそのまま保持する。"""
    value = value.strip()
    if re.fullmatch(r"\d+(?:_\d+)*", value):
        return value
    number = _japanese_number_to_int(value)
    return str(number) if number is not None else str(fallback_index)


def _japanese_number_to_int(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    if not value or any(char not in JAPANESE_DIGITS and char not in JAPANESE_UNITS for char in value):
        return None
    if not any(char in JAPANESE_UNITS for char in value):
        digits = "".join(str(JAPANESE_DIGITS[char]) for char in value)
        return int(digits)
    total = 0
    pending_digit = 0
    for char in value:
        if char in JAPANESE_DIGITS:
            pending_digit = JAPANESE_DIGITS[char]
        else:
            total += (pending_digit or 1) * JAPANESE_UNITS[char]
            pending_digit = 0
    return total + pending_digit


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
