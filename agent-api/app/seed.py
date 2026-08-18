import json
import re
import xml.etree.ElementTree as ET
from hashlib import sha256
from pathlib import Path
from typing import Any

from minio import Minio
from minio.deleteobjects import DeleteObject
from minio.error import S3Error
from pypdf import PdfReader
import requests

from .config import settings
from .embeddings import embed_text, embed_texts
from .graph_audit import audit_graph
from .graph_client import GraphClient
from .legal_ontology import (
    AUTHORITY_GUIDANCE,
    GRAPH_SCHEMA_VERSION,
    REFERENCE_KIND_PARENT_LAW_REFERENCE,
    RELATION_STATUS_UNVERIFIED,
    UNVERIFIED_ASSERTION_CONFIDENCE,
    resolve_authority_type,
)
from .legal_relation_classifier import without_repeated_parent_context_with_offset
from .legal_relation_resolver import assess_implements, classify_reference_kind
from .opensearch_client import OpenSearchClient

EGOV_LAW_ID_PATTERN = re.compile(r"laws\.e-gov\.go\.jp/law/([^/?#]+)")
ARTICLE_REFERENCE_PATTERN = re.compile(
    r"(?<![法令])第([一二三四五六七八九十百千〇零\d]+)条((?:の[一二三四五六七八九十百千〇零\d]+)*)"
)
PARENT_LAW_ARTICLE_PATTERN = re.compile(
    r"(?:当該法律|同法|本法|(?<![一-鿏])法)"
    r"第([一二三四五六七八九十百千〇零\d]+)条"
    r"((?:の[一二三四五六七八九十百千〇零\d]+)*)"
)
PARENT_ORDER_ARTICLE_PATTERN = re.compile(
    r"(?:当該政令|同令|本令|(?<![一-鿏])令)"
    r"第([一二三四五六七八九十百千〇零\d]+)条"
    r"((?:の[一二三四五六七八九十百千〇零\d]+)*)"
)
ARTICLE_HEADING_CHILD_SUFFIX_PATTERN = re.compile(
    r"\s+第[一二三四五六七八九十百千〇零\d]+項"
    r"(?:第[一二三四五六七八九十百千〇零\d]+号)?$"
)
JAPANESE_DIGITS = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
JAPANESE_UNITS = {"十": 10, "百": 100, "千": 1000}
FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
# 号チャンクに前置する柱書きの上限文字数。EMBEDDING_MAX_CHARS(既定1000)の中で号本文の場所を残す。
_ITEM_INTRO_MAX_CHARS = 300
# ガイドラインPDF内「(法第N条(のM)第P項第K号関係)」形式の条文参照を抽出するためのパターン。
RELATION_PAREN_PATTERN = re.compile(r"[（(]([^（）()]{0,80}関係)[）)]")
# 括弧内が「法」等の自己参照トークンで始まる場合のみ抽出対象にする(他法令名は v1 対象外)。
# 長いトークンを先に判定できるよう長さ降順で並べる。
GUIDANCE_LAW_SELF_REF_TOKENS = ("当該法律", "同法", "本法", "法")
# docling前処理済みの表テキストから「法第N条(のM)」形式の自己参照を抽出するパターン。
# 「会社法第330条」「施行規則第98条」のような他法令名の一部の「法」は
# 直前の漢字を否定lookbehindで除外する(表中の自己参照は「法第18条の２」のように単独で書かれ、
# 直前は空白・行頭・記号になる)。空白除去はマッチ後に参照部分へのみ行う(先に全体から
# 空白を除くと「整備 法第18条」が「整備法第18条」になりlookbehindが誤発動するため)。
TABLE_SELF_REF_PATTERN = re.compile(
    r"(?:当該法律|同法|本法|(?<![一-鿏])法)\s*(第\s*\d+\s*条(?:\s*の\s*\d+)*)"
)
# 1表あたりの条文参照の上限。対応表のような大きい表がguidance_explainsへ
# 過剰なピン留めを流し込むのを防ぐ。
TABLE_SELF_REF_MAX_ARTICLES = 8
# docling前処理成果物(派生ゾーン)のプレフィックスとスキーマバージョン。
# preprocess-worker/app/handler.py の DERIVED_PREFIX と対で維持する。
PREPROCESSED_GUIDANCE_PREFIX = "derived-artifacts/preprocessed/external-guidance"
PREPROCESSED_SCHEMA_VERSION = 1
# ガイドライン本文中の「(施行)令第N条(のM)」形式。ガイドが示唆する法令間関係の候補抽出に使う。
GUIDANCE_ORDER_ARTICLE_PATTERN = re.compile(
    r"(?:施行令|(?<![一-鿏])令)第([一二三四五六七八九十百千〇零\d]+)条"
    r"((?:の[一二三四五六七八九十百千〇零\d]+)*)"
)
# 1ガイドライン文書から作るRelationAssertionの上限。未確認関係で候補枠を埋めない。
GUIDANCE_ASSERTION_MAX_PER_DOCUMENT = 12
# 条文注釈として明示された参照だけをEXPLAINSにする。前ページからの引き継ぎは
# 明示的な解説対象ではないためMENTIONSに落とす(§6.1)。
GUIDANCE_EXPLAINS_REFERENCE_SOURCES = (
    "guideline_relation_annotation",
    "guideline_table_annotation",
)


def seed_all(os_client: OpenSearchClient, graph_client: GraphClient) -> dict[str, Any]:
    samples_dir = settings.samples_dir
    mapping_path = settings.opensearch_index_mapping
    if not mapping_path.is_absolute():
        mapping_path = samples_dir / mapping_path
    mapping = _read_json(mapping_path)

    external_guidance_sources = _external_guidance_sources()
    documents, source_snapshot_id = _with_seed_identity(
        _opensearch_documents(samples_dir, external_guidance_sources)
    )

    nodes = _read_jsonl(samples_dir / "metadata" / "nodes.sample.jsonl")
    edges = _read_jsonl(samples_dir / "metadata" / "edges.sample.jsonl")
    law_family_roots = _law_family_roots(samples_dir)
    egov_nodes, egov_edges = _graph_artifacts_from_documents(documents, law_family_roots)
    guidance_nodes, guidance_edges = _guidance_graph_artifacts(documents, law_family_roots)
    nodes = _dedupe_by_key([*nodes, *egov_nodes, *guidance_nodes], "graphNodeId")
    edges = _dedupe_by_key([*edges, *egov_edges, *guidance_edges], "graphEdgeId")
    # ガイド由来エッジの張り先条文は、法令の部分投入ではグラフに存在しないことがある。
    # dangling assertでseed全体を止めず、該当エッジだけ落とす。
    edges, dropped_guidance = _drop_dangling_guidance_edges(edges, {node["graphNodeId"] for node in nodes})
    if dropped_guidance:
        print(
            f"[seed] dropped {dropped_guidance} guidance edge(s) whose target article is not in the graph"
        )
    _assert_no_dangling_edges(nodes, edges)
    # 破壊的な再構築へ入る前に投入物を検査する。不正なGraphを成功扱いにしない。
    audit = audit_graph(nodes, edges, source_snapshot_id=source_snapshot_id)
    if not audit.ok:
        raise ValueError(f"Graph audit failed before seed: {audit.violations}")

    os_client.recreate_index(mapping)
    for document in documents:
        os_client.index_document(document)
    os_client.refresh()
    graph_client.clear()
    graph_client.ensure_legal_graph_schema()
    graph_client.seed_nodes(nodes)
    graph_client.seed_edges(edges)

    minio_count, minio_stale_removed = _seed_minio(
        samples_dir,
        documents,
        external_guidance_sources,
    )

    return {
        "opensearchDocuments": len(documents),
        "externalGuidanceDocuments": sum(1 for document in documents if document.get("docType") == "guideline"),
        "graphNodes": len(nodes),
        "graphEdges": len(edges),
        "minioObjects": minio_count,
        "minioStaleVectorObjectsRemoved": minio_stale_removed,
        # オントロジー変更時に再シードが必要かを判別できるよう、manifestへ残す(§6.3)。
        "graphSchemaVersion": GRAPH_SCHEMA_VERSION,
        "sourceSnapshotId": source_snapshot_id,
        "edgeTypeCounts": _count_by_key(edges, "edgeType"),
        "nodeTypeCounts": _count_by_key(nodes, "nodeType"),
        "authorityTypeCounts": _count_by_key(nodes, "authorityType"),
        "graphAudit": audit.as_dict(),
    }


_SEED_IDENTITY_FIELDS = frozenset(
    {
        "embedding",
        "contentHash",
        "articleContentHash",
        "parentContentHash",
        "documentContentHash",
        "sourceSnapshotId",
        "graphSchemaVersion",
    }
)


def _with_seed_identity(
    documents: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    """同じ入力からOpenSearchとNeo4jで共有するsnapshotとhashを作る。"""

    enriched = [dict(document) for document in documents]
    for document in enriched:
        document["contentHash"] = _stable_hash(
            {
                key: value
                for key, value in document.items()
                if key not in _SEED_IDENTITY_FIELDS
            }
        )

    article_units: dict[str, list[tuple[str, str]]] = {}
    parent_units: dict[str, list[tuple[str, str]]] = {}
    document_units: dict[str, list[tuple[str, str]]] = {}
    for document in enriched:
        content_unit_id = str(document["contentUnitId"])
        content_hash = str(document["contentHash"])
        document_id = str(document["documentId"])
        document_units.setdefault(document_id, []).append(
            (content_unit_id, content_hash)
        )
        if document.get("docType") == "law":
            article_units.setdefault(_article_id(document), []).append(
                (content_unit_id, content_hash)
            )
            parent_content_unit_id = document.get("parentContentUnitId")
            if parent_content_unit_id:
                parent_units.setdefault(
                    str(parent_content_unit_id), []
                ).append((content_unit_id, content_hash))

    article_hashes = {
        article_id: _stable_hash(sorted(units))
        for article_id, units in article_units.items()
    }
    document_hashes = {
        document_id: _stable_hash(sorted(units))
        for document_id, units in document_units.items()
    }
    parent_hashes = {
        parent_id: _stable_hash(sorted(units))
        for parent_id, units in parent_units.items()
    }
    manifest = [
        {
            "contentUnitId": str(document["contentUnitId"]),
            "contentHash": str(document["contentHash"]),
            "sourceRevisionId": document.get("sourceRevisionId"),
        }
        for document in sorted(
            enriched,
            key=lambda item: str(item["contentUnitId"]),
        )
    ]
    source_snapshot_id = f"snapshot-{_stable_hash({'graphSchemaVersion': GRAPH_SCHEMA_VERSION, 'contentUnits': manifest})}"

    for document in enriched:
        document_id = str(document["documentId"])
        document["documentContentHash"] = document_hashes[document_id]
        if document.get("docType") == "law":
            document["articleContentHash"] = article_hashes[
                _article_id(document)
            ]
            parent_content_unit_id = document.get("parentContentUnitId")
            if parent_content_unit_id:
                document["parentContentHash"] = parent_hashes[
                    str(parent_content_unit_id)
                ]
        document["sourceSnapshotId"] = source_snapshot_id
        document["graphSchemaVersion"] = GRAPH_SCHEMA_VERSION
    return enriched, source_snapshot_id


def _stable_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _article_id(document: dict[str, Any]) -> str:
    return str(
        document.get("articleContentUnitId")
        or document.get("parentContentUnitId")
        or document["contentUnitId"]
    ).split("-paragraph-", 1)[0]


def _count_by_key(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = item.get(key)
        if value is None:
            continue
        counts[str(value)] = counts.get(str(value), 0) + 1
    return dict(sorted(counts.items()))


def _opensearch_documents(samples_dir: Path, external_guidance_sources: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    sample = _read_json(samples_dir / "metadata" / "opensearch_document.sample.json")
    law_fsa = _with_authority_type(dict(sample), _law_registry_entries(samples_dir))
    law_fsa["embedding"] = embed_text(_embedding_text(law_fsa))
    documents = [law_fsa]
    if settings.seed_lawqa_egov:
        documents.extend(_lawqa_egov_documents(samples_dir))
    guidance_documents = _external_guidance_documents(external_guidance_sources or [])
    for batch in _chunks(guidance_documents, settings.embedding_batch_size):
        embeddings = embed_texts([_embedding_text(document) for document in batch])
        for document, embedding in zip(batch, embeddings, strict=True):
            document["embedding"] = embedding
    documents.extend(guidance_documents)
    return _dedupe_by_key(documents, "contentUnitId")


def _external_guidance_sources() -> list[dict[str, Any]]:
    if not settings.seed_external_guidance:
        return []

    manifest_path = settings.external_guidance_manifest
    if not manifest_path.exists():
        raise FileNotFoundError(
            "External guidance manifest not found. Run scripts/download_lawqa_guidance.sh or set EXTERNAL_GUIDANCE_MANIFEST: "
            f"{manifest_path}"
        )
    manifest = _read_json(manifest_path)
    entries = manifest.get("documents")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"External guidance manifest has no documents: {manifest_path}")

    root = manifest_path.parent.resolve()
    sources: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("External guidance manifest documents must be objects")
        required = ["documentId", "title", "authority", "file", "sourceUrl"]
        missing = [key for key in required if not str(entry.get(key) or "").strip()]
        if missing:
            raise ValueError(f"External guidance entry is missing {missing}: {entry}")
        source_path = (root / str(entry["file"])).resolve()
        try:
            source_path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"External guidance file must stay within manifest directory: {entry['file']}") from exc
        if not source_path.is_file():
            raise FileNotFoundError(f"External guidance source not found: {source_path}")
        expected_hash = str(entry.get("sha256") or "").removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ValueError(f"External guidance entry requires a SHA-256 checksum: {source_path}")
        actual_hash = sha256(source_path.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            raise ValueError(f"External guidance checksum mismatch: {source_path}")
        sources.append({**entry, "_sourcePath": source_path})
    return sorted(sources, key=lambda source: str(source["documentId"]))


def _external_guidance_documents(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for source in sources:
        document_id = str(source["documentId"])
        primary_law_id = _guidance_primary_law_document_id(source)
        artifact = _load_guidance_artifact(source)
        if artifact is not None:
            chunk_specs = _docling_guidance_chunks(
                artifact["items"], primary_law_id, settings.external_guidance_chunk_chars
            )
            parser_type = "pdf_docling_structured_v1"
        else:
            chunk_specs = _pypdf_guidance_chunk_specs(source, primary_law_id)
            parser_type = "pdf_pypdf_page_chunk_v1"
        for chunk_index, spec in enumerate(chunk_specs, start=1):
            documents.append(_guidance_document(source, spec, chunk_index, parser_type))
        if not any(document["documentId"] == document_id for document in documents):
            raise ValueError(f"No extractable text found in external guidance PDF: {source['_sourcePath']}")
    return documents


def _pypdf_guidance_chunk_specs(source: dict[str, Any], primary_law_id: str | None) -> list[dict[str, Any]]:
    """従来のpypdf経路: ページ単位テキストの文字数チャンク。docling前処理成果物が
    無い場合のフォールバック(初回seedやCI環境でも動く後方互換経路)。"""
    reader = PdfReader(str(source["_sourcePath"]))
    specs: list[dict[str, Any]] = []
    carried_reference: list[str] | None = None
    for page_number, page in enumerate(reader.pages, start=1):
        text = _normalize_pdf_text(page.extract_text() or "")
        page_matches = _guidance_page_relation_matches(text, primary_law_id)
        for chunk_start, chunk_text in _pdf_text_chunks(text):
            related_article_ids, reference_source = _related_articles_for_chunk(
                chunk_start, page_matches, carried_reference
            )
            specs.append(
                {
                    "page": page_number,
                    "text": chunk_text,
                    "chunkStrategy": "guidance_pdf_page_chunk_v1",
                    "relatedArticleContentUnitIds": related_article_ids,
                    "articleReferenceSource": reference_source,
                }
            )
        if page_matches:
            carried_reference = page_matches[-1][1]
    return specs


def _guidance_document(
    source: dict[str, Any],
    spec: dict[str, Any],
    chunk_index: int,
    parser_type: str,
) -> dict[str, Any]:
    document_id = str(source["documentId"])
    title = str(source["title"])
    raw_object_name = f"source-documents/external-guidance/{Path(str(source['file'])).name}"
    page_number = int(spec.get("page") or 1)
    content_unit_id = f"{document_id}-page-{page_number}-chunk-{chunk_index}"
    return {
        "documentId": document_id,
        "contentUnitId": content_unit_id,
        "chunkId": content_unit_id,
        "parentContentUnitId": None,
        "articleContentUnitId": None,
        "deptCode": "common",
        "docType": "guideline",
        "contentDomain": "legal_guidance",
        "title": title,
        "heading": f"{title} p.{page_number}",
        "sectionPath": f"{source['authority']} > {title} > p.{page_number}",
        "text": spec["text"],
        "publishStatus": "published",
        "isLatest": True,
        "confidentiality": "public",
        "clearanceLevel": 1,
        "sourceObjectUri": f"minio://{settings.minio_bucket}/{raw_object_name}",
        "processedObjectUri": (
            "minio://"
            f"{settings.minio_bucket}/derived-artifacts/vector-documents/"
            f"dept=common/docType=guideline/{document_id}/{content_unit_id}.md"
        ),
        "sourcePage": page_number,
        "parserType": parser_type,
        "chunkStrategy": spec["chunkStrategy"],
        "relatedArticleContentUnitIds": spec["relatedArticleContentUnitIds"],
        "articleReferenceSource": spec["articleReferenceSource"],
        # ガイドは規範的法令レイヤーに含めず、補助資料レーンとして別管理する(§5.2, §10)。
        "authorityType": AUTHORITY_GUIDANCE,
        "authoritySource": "doc_type",
    }


def _normalize_pdf_text(text: str) -> str:
    lines = [re.sub(r"[ \\t]+", " ", line).strip() for line in text.replace("\u3000", " ").splitlines()]
    return "\n".join(line for line in lines if line)


def _pdf_text_chunks(text: str) -> list[tuple[int, str]]:
    """(チャンク開始オフセット, チャンク本文) のリストを返す。
    オフセットは条文参照(RELATION_PAREN_PATTERN)の紐付けにのみ使う。"""
    if not text:
        return []
    chunk_size = settings.external_guidance_chunk_chars
    overlap = min(120, chunk_size // 4)
    chunks: list[tuple[int, str]] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        if end < len(text):
            boundary = max(text.rfind("\n", start + chunk_size // 2, end), text.rfind("。", start + chunk_size // 2, end))
            if boundary > start:
                end = boundary + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append((start, chunk))
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def _guidance_primary_law_document_id(source: dict[str, Any]) -> str | None:
    """manifestの`primaryLawId`(例: "335AC0000000145"、law_registry.jsonと同じ
    裸のe-Gov法令番号)を、法令チャンクの実際のdocumentId表記("law-<法令番号>"、
    id_naming_rules.md参照)へ正規化する。既に"law-"始まりならそのまま使う。"""
    raw = str(source.get("primaryLawId") or "").strip()
    if not raw:
        return None
    return raw if raw.startswith("law-") else f"law-{raw}"


def _guidance_relation_article_ids(law_id: str, relation_text: str) -> list[str]:
    """括弧内の '法第18条の2第1項第2号関係' のようなテキストから条レベルの
    contentUnitId を抽出する。法令名部分が自己参照トークン(法/同法/本法等)で
    始まらない場合(＝他法令への参照)は v1 では対象外として空を返す。"""
    compacted = re.sub(r"\s+", "", relation_text).translate(FULLWIDTH_DIGITS)
    for token in GUIDANCE_LAW_SELF_REF_TOKENS:
        if compacted.startswith(token):
            stripped = compacted[len(token):]
            references = _explicit_article_reference_ids(law_id, stripped)
            return list(dict.fromkeys(references))
    return []


def _guidance_page_relation_matches(text: str, law_id: str | None) -> list[tuple[int, list[str]]]:
    """ページ本文全体から (開始オフセット, 条レベルcontentUnitId群) を抽出する。
    法令条文が空の候補(他法令参照など)は除外する。"""
    if not law_id:
        return []
    matches = []
    for match in RELATION_PAREN_PATTERN.finditer(text):
        article_ids = _guidance_relation_article_ids(law_id, match.group(1))
        if article_ids:
            matches.append((match.start(), article_ids))
    return matches


def _related_articles_for_chunk(
    chunk_start: int,
    page_matches: list[tuple[int, list[str]]],
    carried_reference: list[str] | None,
) -> tuple[list[str], str | None]:
    """あるチャンクに紐付ける条文参照を、同一ページ内の直近の見出しから、
    無ければ前ページから引き継いだ参照から決定する。"""
    candidates = [(pos, ids) for pos, ids in page_matches if pos <= chunk_start]
    if candidates:
        _, article_ids = max(candidates, key=lambda item: item[0])
        return article_ids, "guideline_relation_annotation"
    if carried_reference:
        return carried_reference, "carried_forward"
    return [], None


def _load_guidance_artifact(source: dict[str, Any]) -> dict[str, Any] | None:
    """preprocess-workerが派生ゾーンへ置いたdocling前処理JSONを取得する。

    成果物が無い・ストレージへ接続できない場合はNoneを返しpypdf経路へ
    フォールバックする(初回seedやテスト環境でも動く)。ただし成果物が存在するのに
    元PDFと食い違う(sourceSha256不一致)場合は、古い成果物での投入を防ぐため
    fail fastでValueErrorを送出する。"""
    stem = Path(str(source["file"])).stem
    object_name = f"{PREPROCESSED_GUIDANCE_PREFIX}/{stem}.json"
    response = None
    try:
        # 成果物が無い/MinIOに届かない環境(テスト・初回seed)で長時間リトライしないよう、
        # 取得プローブは短いタイムアウト・リトライ無しで行う。
        import urllib3

        http_client = urllib3.PoolManager(
            timeout=urllib3.Timeout(connect=2, read=10),
            retries=urllib3.Retry(total=0),
        )
        client = _minio_client(http_client=http_client)
        response = client.get_object(settings.minio_bucket, object_name)
        payload = json.loads(response.read().decode("utf-8"))
    except ValueError:
        raise
    except Exception:
        return None
    finally:
        if response is not None:
            response.close()
            response.release_conn()

    if payload.get("schemaVersion") != PREPROCESSED_SCHEMA_VERSION:
        raise ValueError(f"Unsupported preprocessed guidance schema: {object_name}")
    expected_hash = str(source.get("sha256") or "").removeprefix("sha256:")
    actual_hash = str(payload.get("sourceSha256") or "").removeprefix("sha256:")
    if expected_hash != actual_hash:
        raise ValueError(
            "Preprocessed guidance artifact is stale (sourceSha256 mismatch). "
            f"Re-run preprocess-worker for: {object_name}"
        )
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError(f"Preprocessed guidance artifact has no items: {object_name}")
    return payload


def _docling_guidance_chunks(
    items: list[dict[str, Any]],
    law_id: str | None,
    chunk_chars: int,
) -> list[dict[str, Any]]:
    """docling前処理済みアイテム列(section_header/text/table)を検索用チャンクへ変換する。

    - textは直近の見出しをコンテキストに付与しつつchunk_charsまで連結
    - tableは1表=1チャンク(Markdown表現)とし、セル平文から法令自己参照を抽出
    - 「(法第N条関係)」注釈のcarried_forwardはpypdf経路と同じ意味論を保つ
    """
    chunks: list[dict[str, Any]] = []
    buffer: list[str] = []
    buffer_page: int | None = None
    buffer_annotated = False
    current_header: str | None = None
    carried_reference: list[str] | None = None

    def flush() -> None:
        nonlocal buffer, buffer_page, buffer_annotated
        if not buffer:
            return
        text = "\n".join(buffer)
        if buffer_annotated:
            related, source_kind = carried_reference or [], "guideline_relation_annotation"
        elif carried_reference:
            related, source_kind = carried_reference, "carried_forward"
        else:
            related, source_kind = [], None
        chunks.append(
            {
                "page": buffer_page,
                "text": text,
                "chunkStrategy": "guidance_docling_text_chunk_v1",
                "relatedArticleContentUnitIds": related,
                "articleReferenceSource": source_kind,
            }
        )
        buffer = []
        buffer_page = None
        buffer_annotated = False

    for item in items:
        item_type = str(item.get("type") or "")
        text = str(item.get("text") or "").strip()
        if item_type == "section_header":
            flush()
            current_header = text or None
            continue
        if item_type == "table":
            flush()
            markdown = str(item.get("markdown") or "").strip() or text
            if not markdown:
                continue
            related = _table_self_ref_article_ids(law_id, text or markdown) if law_id else []
            chunks.append(
                {
                    "page": item.get("page"),
                    "text": markdown,
                    "chunkStrategy": "guidance_docling_table_v1",
                    "relatedArticleContentUnitIds": related,
                    "articleReferenceSource": "guideline_table_annotation" if related else None,
                }
            )
            continue
        if not text:
            continue
        matches = _guidance_page_relation_matches(text, law_id)
        if matches:
            carried_reference = matches[-1][1]
            buffer_annotated = True
        if not buffer and current_header:
            buffer.append(current_header)
        if buffer_page is None:
            buffer_page = item.get("page")
        buffer.append(text)
        if sum(len(part) for part in buffer) >= chunk_chars:
            flush()
    flush()
    return chunks


def _table_self_ref_article_ids(
    law_id: str | None,
    table_text: str,
    limit: int = TABLE_SELF_REF_MAX_ARTICLES,
) -> list[str]:
    """表テキストから「法第N条(のM)」形式の自己参照を条レベルIDとして抽出する。

    「会社法第330条」「施行規則第98条」のような他法令参照はTABLE_SELF_REF_PATTERNの
    lookbehindで除外される。上限limit件で打ち切る(大きな対応表対策)。"""
    if not law_id:
        return []
    normalized = table_text.translate(FULLWIDTH_DIGITS)
    article_ids: list[str] = []
    for match in TABLE_SELF_REF_PATTERN.finditer(normalized):
        reference_text = re.sub(r"\s+", "", match.group(1))
        for reference in _explicit_article_reference_ids(law_id, reference_text):
            if reference not in article_ids:
                article_ids.append(reference)
        if len(article_ids) >= limit:
            break
    return article_ids[:limit]


def _minio_client(http_client: Any = None) -> Minio:
    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=False,
        http_client=http_client,
    )


def _law_registry_entries(samples_dir: Path) -> dict[str, dict[str, Any]]:
    """law_registry.jsonのlawId別エントリ。authorityTypeの正の値をここから読む(§5.2)。"""
    registry_path = samples_dir / "eval" / "law_registry.json"
    if not registry_path.exists():
        return {}
    registry = _read_json(registry_path)
    return {
        str(item["lawId"]): item
        for item in registry.get("laws", [])
        if item.get("lawId")
    }


def _with_authority_type(
    document: dict[str, Any],
    registry_entries: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """documentIdからauthorityTypeを解決して付与する(既に値がある場合は尊重する)。"""
    if document.get("authorityType"):
        return document
    law_id = str(document.get("documentId") or "").removeprefix("law-")
    entry = registry_entries.get(law_id, {})
    resolution = resolve_authority_type(
        law_id,
        registry_authority_type=entry.get("authorityType"),
        title=document.get("title"),
        doc_type=document.get("docType"),
    )
    return {
        **document,
        "authorityType": resolution.authority_type,
        "authoritySource": resolution.authority_source,
    }


def _lawqa_egov_documents(samples_dir: Path) -> list[dict[str, Any]]:
    law_ids = _lawqa_egov_law_ids(samples_dir)
    registry_entries = _law_registry_entries(samples_dir)
    documents: list[dict[str, Any]] = []
    for law_id in law_ids:
        documents.extend(_egov_law_documents(law_id, registry_entries))
    for batch in _chunks(documents, settings.embedding_batch_size):
        embeddings = embed_texts([_embedding_text(document) for document in batch])
        for document, embedding in zip(batch, embeddings, strict=True):
            document["embedding"] = embedding
    return documents


def _lawqa_egov_law_ids(samples_dir: Path) -> list[str]:
    explicit_ids = [item.strip() for item in settings.lawqa_egov_law_ids.split(",") if item.strip()]
    if explicit_ids:
        return sorted(set(explicit_ids))

    registry_path = samples_dir / "eval" / "law_registry.json"
    if registry_path.exists():
        registry = _read_json(registry_path)
        configured = [
            str(item.get("seedSpec") or item["lawId"])
            for item in registry.get("laws", [])
            if item.get("lawId")
        ]
        if configured:
            return sorted(set(configured))

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


def _law_family_roots(samples_dir: Path) -> dict[str, str]:
    """law_registryの法令IDを、グラフで使うdocumentId形式へ正規化する。"""
    registry_path = samples_dir / "eval" / "law_registry.json"
    if not registry_path.exists():
        return {}
    registry = _read_json(registry_path)
    return {
        f"law-{item['lawId']}": f"law-{item.get('familyRoot') or item['lawId']}"
        for item in registry.get("laws", [])
        if item.get("lawId")
    }


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


def _egov_law_documents(
    law_id_spec: str,
    registry_entries: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
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
    # e-Gov LawTypeは内閣府令も MinisterialOrdinance に含むため、M系はregistryの
    # 人手確認値を正とする(§5.2)。registryに無い場合は ordinance_unspecified のままにする。
    law_element = root.find(".//Law")
    entry = (registry_entries or {}).get(law_id, {})
    authority = resolve_authority_type(
        law_id,
        registry_authority_type=entry.get("authorityType"),
        law_type=law_element.get("LawType") if law_element is not None else None,
        title=title,
    )

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
        _section_documents(
            main_articles,
            law_id,
            title,
            law_num,
            url,
            id_prefix="article",
            section_key="main",
            authority_type=authority.authority_type,
            authority_source=authority.authority_source,
        )
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
                    authority_type=authority.authority_type,
                    authority_source=authority.authority_source,
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
    authority_type: str | None = None,
    authority_source: str | None = None,
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
            "authorityType": authority_type,
            "authoritySource": authority_source,
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
            # 号は「国債証券」のような短い列挙になりがちで、単体では検索文脈を失う。
            # 親項の柱書き(導入文)を前置して意味的な検索可能性を回復する。
            item_prefix = intro_text[:_ITEM_INTRO_MAX_CHARS]
            for item_index, item in enumerate(items, start=1):
                item_num_text = (item.get("Num") or item.findtext("ItemTitle") or str(item_index)).strip()
                # 枝番の号（例: '2_2' = 第二号の二）は int 化すると隣の号と衝突するため接尾辞のまま使う。
                item_suffix = _num_suffix(item_num_text, item_index)
                item_text = _element_sentence_text(item)
                if not item_text:
                    continue
                item_label = f"第{item_suffix.replace('_', 'の')}号"
                chunks.append(
                    {
                        "contentUnitId": f"{paragraph_id}-item-{item_suffix}",
                        "parentContentUnitId": paragraph_id,
                        "articleContentUnitId": article_content_unit_id,
                        "heading": f"{full_heading} 第{paragraph_num}項{item_label}",
                        "paragraphNumber": paragraph_num,
                        "itemNumber": _int_or_none(item_suffix.split("_")[0]),
                        "text": f"{item_prefix}\n{item_label}　{item_text}" if item_prefix else item_text,
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


def _graph_artifacts_from_documents(
    documents: list[dict[str, Any]],
    law_family_roots: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    nodes_by_id: dict[str, dict[str, Any]] = {}
    edges = []
    for document in documents:
        if document.get("docType") != "law":
            continue
        document_id = document["documentId"]
        content_unit_id = document["contentUnitId"]
        article_content_unit_id = document.get("articleContentUnitId") or document.get("parentContentUnitId") or content_unit_id
        authority_type = document.get("authorityType")
        authority_source = document.get("authoritySource")
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
            "authorityType": authority_type,
            "authoritySource": authority_source,
            **_node_seed_identity(document, "documentContentHash"),
        }
        nodes_by_id[article_content_unit_id] = {
            "graphNodeId": article_content_unit_id,
            "nodeType": "Article",
            "documentId": document_id,
            "contentUnitId": article_content_unit_id,
            "deptCode": document.get("deptCode"),
            "docType": document.get("docType"),
            "contentDomain": document.get("contentDomain"),
            "title": document.get("title"),
            "heading": _article_heading(document.get("heading")),
            "publishStatus": document.get("publishStatus"),
            "isLatest": document.get("isLatest"),
            "confidentiality": document.get("confidentiality"),
            "clearanceLevel": document.get("clearanceLevel", 3),
            "authorityType": authority_type,
            "authoritySource": authority_source,
            **_node_seed_identity(document, "articleContentHash"),
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
                **_relation_seed_identity(document, article_content_unit_id),
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
                    "authorityType": authority_type,
                    "authoritySource": authority_source,
                    "publishStatus": document.get("publishStatus"),
                    "isLatest": document.get("isLatest"),
                    "confidentiality": document.get("confidentiality"),
                    "clearanceLevel": document.get("clearanceLevel", 3),
                    **_node_seed_identity(document, "parentContentHash"),
                }
                edges.append(
                    _hierarchy_edge(article_content_unit_id, parent_id, document)
                )
            node_type = "Item" if document.get("itemNumber") is not None else "Paragraph"
            nodes_by_id[content_unit_id] = {
                "graphNodeId": content_unit_id,
                "nodeType": node_type,
                "documentId": document_id,
                "contentUnitId": content_unit_id,
                "authorityType": authority_type,
                "authoritySource": authority_source,
                "heading": document.get("heading"),
                "publishStatus": document.get("publishStatus"),
                "isLatest": document.get("isLatest"),
                "confidentiality": document.get("confidentiality"),
                "clearanceLevel": document.get("clearanceLevel", 3),
                **_node_seed_identity(document, "contentHash"),
            }
            edges.append(_hierarchy_edge(parent_id, content_unit_id, document))
    reference_edges = _reference_edges(documents)
    edges.extend(reference_edges)
    _, delegation_edges = _delegation_graph_artifacts(
        documents,
        law_family_roots or {},
    )
    edges.extend(delegation_edges)
    return list(nodes_by_id.values()), edges


def _article_texts(documents: list[dict[str, Any]]) -> dict[str, str]:
    """条ID -> 条全体の本文。委任文言の検出は項・号チャンク単位では判断できない。"""
    texts: dict[str, str] = {}
    for document in documents:
        if document.get("docType") != "law":
            continue
        article_id = str(
            document.get("articleContentUnitId")
            or document.get("parentContentUnitId")
            or document["contentUnitId"]
        ).split("-paragraph-", 1)[0]
        text = str(document.get("text") or "")
        if not text:
            continue
        texts[article_id] = f"{texts.get(article_id, '')}\n{text}".strip()
    return texts


def _guidance_graph_artifacts(
    documents: list[dict[str, Any]],
    law_family_roots: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """ガイドライン文書を「羅針盤」としてグラフに載せる。

    文書内の全チャンクの relatedArticleContentUnitIds を文書単位で集約し、
    ガイドライン文書ノード -EXPLAINS-> 法令条文ノード のエッジを張る。検索時は
    上位ヒットしたガイドラインチャンクの documentId からこのエッジを辿り、
    解説対象の条文を特定する(条文本文の取得はOpenSearch側の役割)。

    条文注釈・対応表で明示された参照だけを EXPLAINS とする。前ページからの引き継ぎ
    (carried_forward)や単なる言及はOpenSearch本文にだけ残し、Graph relationへ変換しない。

    条文ノードは法令投入側(_graph_artifacts_from_documents)が作るため、ここでは
    作らない。張り先が存在しないEXPLAINSは seed_all 側の dangling ドロップに委ねる。
    """
    explained_articles_by_document: dict[str, list[str]] = {}
    source_units_by_relation: dict[tuple[str, str], list[str]] = {}
    representative_by_document: dict[str, dict[str, Any]] = {}
    for document in documents:
        if document.get("docType") != "guideline":
            continue
        document_id = document["documentId"]
        representative_by_document.setdefault(document_id, document)
        explained = explained_articles_by_document.setdefault(document_id, [])
        reference_source = document.get("articleReferenceSource")
        # articleReferenceSourceが無い(旧形式・手動投入)場合は明示注記として扱う。
        is_explicit = reference_source is None or reference_source in GUIDANCE_EXPLAINS_REFERENCE_SOURCES
        if not is_explicit:
            continue
        for article_id in document.get("relatedArticleContentUnitIds") or []:
            if article_id not in explained:
                explained.append(article_id)
            source_units_by_relation.setdefault(
                (document_id, article_id), []
            ).append(str(document["contentUnitId"]))

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for document_id, document in representative_by_document.items():
        nodes.append(
            {
                "graphNodeId": document_id,
                "nodeType": "Document",
                "documentId": document_id,
                "deptCode": document.get("deptCode"),
                "docType": "guideline",
                "contentDomain": document.get("contentDomain"),
                "title": document.get("title"),
                "publishStatus": document.get("publishStatus"),
                "isLatest": document.get("isLatest"),
                "confidentiality": document.get("confidentiality"),
                "clearanceLevel": document.get("clearanceLevel", 1),
                "authorityType": AUTHORITY_GUIDANCE,
                "authoritySource": "doc_type",
                **_node_seed_identity(document, "documentContentHash"),
            }
        )
        for article_id in explained_articles_by_document.get(document_id, []):
            source_units = list(
                dict.fromkeys(source_units_by_relation[(document_id, article_id)])
            )
            edges.append(
                {
                    "graphEdgeId": f"edge-{document_id}-explains-{article_id}",
                    "edgeType": "EXPLAINS",
                    "fromGraphNodeId": document_id,
                    "toGraphNodeId": article_id,
                    "documentId": document_id,
                    "relationSource": "guidance_article_annotation",
                    "relationConfidence": 0.9,
                    "sourceContentUnitIds": source_units,
                    "publishStatus": "published",
                    "isLatest": True,
                    **_relation_seed_identity(document, source_units[0]),
                }
            )
    return nodes, edges


def _guidance_relation_assertions(
    documents: list[dict[str, Any]],
    law_family_roots: dict[str, str],
) -> list[dict[str, Any]]:
    """ガイドが示唆する法令間関係を、未確認のRelationAssertionノードとして保存する(§6.1)。

    正式なArticle間エッジにはしない。`unverified`のまま候補拡張だけに使い、根拠充足・
    mustInclude・法令関係図の確定線には使わない。法令本文で確認した後に正式な関係を作る。

    v1では、条文注釈で法律の条(A)に紐づいたチャンク本文に「(施行)令第N条」がある場合に、
    同一法令系統の施行令の条(B)への IMPLEMENTS 候補を作る。それ以外の示唆表現は対象外。
    """
    decree_by_family = {
        law_family_roots.get(document["documentId"], document["documentId"]): document["documentId"]
        for document in documents
        if document.get("docType") == "law" and str(document.get("title") or "").endswith("施行令")
    }
    available_articles = {
        str(
            document.get("articleContentUnitId")
            or document.get("parentContentUnitId")
            or document["contentUnitId"]
        ).split("-paragraph-", 1)[0]
        for document in documents
        if document.get("docType") == "law"
    }
    assertions: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for document in documents:
        if document.get("docType") != "guideline":
            continue
        text = str(document.get("text") or "")
        if not text:
            continue
        guidance_document_id = str(document["documentId"])
        relation_pairs: list[tuple[str, str, str]] = []
        if document.get("chunkStrategy") == "guidance_docling_table_v1":
            relation_pairs = _guidance_table_relation_pairs(
                document,
                law_family_roots,
                decree_by_family,
            )
        else:
            for from_article_id in list(
                document.get("relatedArticleContentUnitIds") or []
            ):
                law_document_id = from_article_id.split("-article-", 1)[0]
                family_root = law_family_roots.get(
                    law_document_id, law_document_id
                )
                decree_document_id = decree_by_family.get(family_root)
                if not decree_document_id or decree_document_id == law_document_id:
                    continue
                relation_pairs.extend(
                    (from_article_id, to_article_id, text[:200])
                    for to_article_id in _prefixed_article_reference_ids(
                        decree_document_id,
                        text,
                        GUIDANCE_ORDER_ARTICLE_PATTERN,
                    )
                )
        for from_article_id, to_article_id, source_text in relation_pairs:
            if from_article_id not in available_articles:
                continue
            if to_article_id not in available_articles:
                continue
            if counts.get(guidance_document_id, 0) >= GUIDANCE_ASSERTION_MAX_PER_DOCUMENT:
                break
            assertion_id = f"assertion-{from_article_id}-implements-{to_article_id}"
            if assertion_id in assertions:
                continue
            counts[guidance_document_id] = counts.get(guidance_document_id, 0) + 1
            assertions[assertion_id] = {
                "graphNodeId": assertion_id,
                "nodeType": "RelationAssertion",
                "assertionId": assertion_id,
                "fromArticleId": from_article_id,
                "toArticleId": to_article_id,
                "suggestedType": "IMPLEMENTS",
                "assertedByDocumentId": guidance_document_id,
                "assertionSource": "guidance_relation_candidate",
                "sourceText": source_text,
                "confidence": UNVERIFIED_ASSERTION_CONFIDENCE,
                "status": RELATION_STATUS_UNVERIFIED,
                "publishStatus": "published",
                "isLatest": True,
                "clearanceLevel": document.get("clearanceLevel", 1),
                "graphSchemaVersion": GRAPH_SCHEMA_VERSION,
            }
    return list(assertions.values())


def _guidance_table_relation_pairs(
    document: dict[str, Any],
    law_family_roots: dict[str, str],
    decree_by_family: dict[str, str],
) -> list[tuple[str, str, str]]:
    """対応表の同じ行にある法律条文と施行令条文だけを組にする。

    表全体の参照集合を直積にすると別行の条文同士を誤接続するため、行境界は
    決定的に保持する。関係の法的意味自体はここでは判断しない。
    """
    related_law_ids = {
        str(article_id).split("-article-", 1)[0]
        for article_id in document.get("relatedArticleContentUnitIds") or []
        if "-article-" in str(article_id)
    }
    pairs: list[tuple[str, str, str]] = []
    for raw_line in str(document.get("text") or "").splitlines():
        line = raw_line.strip()
        if not line or set(line.replace("|", "").replace(":", "").strip()) <= {"-"}:
            continue
        for law_document_id in related_law_ids:
            family_root = law_family_roots.get(law_document_id, law_document_id)
            decree_document_id = decree_by_family.get(family_root)
            if not decree_document_id or decree_document_id == law_document_id:
                continue
            from_ids = _table_self_ref_article_ids(law_document_id, line)
            to_ids = _prefixed_article_reference_ids(
                decree_document_id,
                line,
                GUIDANCE_ORDER_ARTICLE_PATTERN,
            )
            pairs.extend(
                (from_article_id, to_article_id, line[:200])
                for from_article_id in from_ids
                for to_article_id in to_ids
            )
    return list(dict.fromkeys(pairs))


def _drop_dangling_guidance_edges(
    edges: list[dict[str, Any]],
    node_ids: set[str],
) -> tuple[list[dict[str, Any]], int]:
    """張り先/張り元ノードがグラフに無いガイド由来エッジだけを除去する。

    法令の部分投入(例: 民法601-622条の2のみ)では、ガイドが解説する条文がグラフに
    存在しないことがある。ガイド以外の dangling は _assert_no_dangling_edges で
    従来どおり検出させる。"""
    kept: list[dict[str, Any]] = []
    dropped = 0
    for edge in edges:
        if edge.get("edgeType") == "EXPLAINS" and (
            edge["fromGraphNodeId"] not in node_ids or edge["toGraphNodeId"] not in node_ids
        ):
            dropped += 1
            continue
        kept.append(edge)
    return kept, dropped


def _hierarchy_edge(
    from_id: str,
    to_id: str,
    document: dict[str, Any],
) -> dict[str, Any]:
    document_id = str(document["documentId"])
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
        **_relation_seed_identity(document, to_id),
    }


def _node_seed_identity(
    document: dict[str, Any],
    content_hash_field: str,
) -> dict[str, Any]:
    return {
        "sourceSnapshotId": document.get("sourceSnapshotId"),
        "sourceRevisionId": document.get("sourceRevisionId"),
        "contentHash": document.get(content_hash_field),
        "graphSchemaVersion": GRAPH_SCHEMA_VERSION,
    }


def _relation_seed_identity(
    document: dict[str, Any],
    source_content_unit_id: str,
) -> dict[str, Any]:
    return {
        "sourceContentUnitId": source_content_unit_id,
        "sourceRevisionId": document.get("sourceRevisionId"),
        "sourceSnapshotId": document.get("sourceSnapshotId"),
        "graphSchemaVersion": GRAPH_SCHEMA_VERSION,
    }


def _reference_edges(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    law_documents: dict[str, list[dict[str, Any]]] = {}
    for document in documents:
        if document.get("docType") == "law":
            law_documents.setdefault(document["documentId"], []).append(document)

    edges = []
    for document_id, scoped_documents in law_documents.items():
        documents_by_id = {
            str(document["contentUnitId"]): document for document in scoped_documents
        }
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
            raw_text = str(document.get("text") or "")
            parent = documents_by_id.get(str(document.get("parentContentUnitId") or ""))
            if parent is None:
                text = raw_text
                source_offset = 0
            else:
                text, source_offset = without_repeated_parent_context_with_offset(
                    raw_text, str(parent.get("text") or "").strip()
                )
            targets = set(_explicit_article_reference_ids(document_id, text)) & main_article_ids
            if "前条" in text and index > 0:
                targets.add(ids[index - 1])
            if "次条" in text and index + 1 < len(ids):
                targets.add(ids[index + 1])
            for target_id in sorted(targets):
                if target_id == source_id:
                    continue
                edge_id = f"edge-{source_id}-references-{target_id.removeprefix(document_id + '-')}"
                occurrences = _reference_occurrences(
                    document_id,
                    text,
                    target_id,
                    previous_target_id=ids[index - 1] if index > 0 else None,
                    next_target_id=ids[index + 1] if index + 1 < len(ids) else None,
                    source_offset=source_offset,
                )
                edges.append(
                    {
                        "graphEdgeId": edge_id,
                        "edgeType": "REFERENCES",
                        "fromGraphNodeId": source_id,
                        "toGraphNodeId": target_id,
                        "documentId": document_id,
                        # 原文上の参照は事実として保存し、法的な意味は referenceKind で表す(§6.1)。
                        "referenceKind": classify_reference_kind(text),
                        "relationSource": "xml_reference_rule",
                        "relationConfidence": 0.9,
                        "citationText": occurrences[0][0],
                        "citationTexts": [item[0] for item in occurrences],
                        "sourceSpanStarts": [item[1] for item in occurrences],
                        "sourceSpanEnds": [item[2] for item in occurrences],
                        "targetResolutionMethod": occurrences[0][3],
                        "publishStatus": "published",
                        "isLatest": True,
                        **_relation_seed_identity(document, source_id),
                    }
                )
    return edges


def _reference_occurrences(
    document_id: str,
    text: str,
    target_id: str,
    *,
    previous_target_id: str | None,
    next_target_id: str | None,
    source_offset: int = 0,
) -> list[tuple[str, int, int, str]]:
    occurrences: list[tuple[str, int, int, str]] = []
    for match in ARTICLE_REFERENCE_PATTERN.finditer(text):
        if target_id not in _explicit_article_reference_ids(
            document_id, match.group(0)
        ):
            continue
        occurrences.append(
            (
                match.group(0),
                match.start() + source_offset,
                match.end() + source_offset,
                "article_reference",
            )
        )
    for token, adjacent_target, method in (
        ("前条", previous_target_id, "previous_article"),
        ("次条", next_target_id, "next_article"),
    ):
        if adjacent_target != target_id:
            continue
        for match in re.finditer(token, text):
            occurrences.append(
                (token, match.start() + source_offset, match.end() + source_offset, method)
            )
    if occurrences:
        return occurrences
    # 構造的には解決済みでも引用位置を復元できない場合は、参照元Content Unitを
    # 監査対象として残す。意味predicateはここから推測しない。
    return [
        (
            text[:500],
            source_offset,
            source_offset + min(len(text), 500),
            "content_unit_fallback",
        )
    ]


def _incorporation_edges(
    documents: list[dict[str, Any]],
    reference_edges: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """準用される条文から、準用元の条文へ逆引きする APPLIED_BY を生成する。

    通常の REFERENCES は「準用元→準用先」なので、準用される規定から適用場面を
    探したい質問では辿れない。準用語を含む参照だけを反転し、条単位へ集約する。
    """
    source_articles = {
        document["contentUnitId"]: str(
            document.get("articleContentUnitId")
            or document.get("parentContentUnitId")
            or document["contentUnitId"]
        ).split("-paragraph-", 1)[0]
        for document in documents
        if document.get("docType") == "law" and "準用" in str(document.get("text") or "")
    }
    edges: list[dict[str, Any]] = []
    seen: set[str] = set()
    for reference in reference_edges if reference_edges is not None else _reference_edges(documents):
        source_article_id = source_articles.get(reference["fromGraphNodeId"])
        if not source_article_id:
            continue
        target_article_id = reference["toGraphNodeId"]
        if target_article_id == source_article_id:
            continue
        edge_id = f"edge-{target_article_id}-applied-by-{source_article_id}"
        if edge_id in seen:
            continue
        seen.add(edge_id)
        edges.append(
            {
                "graphEdgeId": edge_id,
                "edgeType": "APPLIED_BY",
                "fromGraphNodeId": target_article_id,
                "toGraphNodeId": source_article_id,
                "documentId": reference["documentId"],
                # 派生エッジは元の原文参照へ辿れるようにする。同一意味の順逆エッジを
                # 独立した事実として管理しない(§6.1)。
                "derivedFromEdgeId": reference["graphEdgeId"],
                "relationSource": "incorporation_reference_rule",
                "relationConfidence": 0.9,
                "publishStatus": "published",
                "isLatest": True,
            }
        )
    return edges


def _delegation_edges(
    documents: list[dict[str, Any]],
    law_family_roots: dict[str, str],
) -> list[dict[str, Any]]:
    """下位法令の親法参照を事実として保存するREFERENCESを返す。

    逆向きのIMPLEMENTSは法的意味の判断を伴うため自動確定しない。探索候補は
    `_delegation_graph_artifacts`がRelationAssertionとして別に返す。
    """
    _, edges = _delegation_graph_artifacts(documents, law_family_roots)
    return edges


def _delegation_relation_assertions(
    documents: list[dict[str, Any]],
    law_family_roots: dict[str, str],
) -> list[dict[str, Any]]:
    """下位法令の親法参照から生成した、未確認IMPLEMENTS候補を返す。"""
    assertions, _ = _delegation_graph_artifacts(documents, law_family_roots)
    return assertions


def _delegation_graph_artifacts(
    documents: list[dict[str, Any]],
    law_family_roots: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """親参照の事実エッジと、意味判断前の委任関係候補を一度の走査で作る。

    同一法令系統の下位法令が親Articleを明示参照した組合せは、ルールの文言検出結果に
    かかわらず候補として保存する。文言検出は候補を落とす条件や信頼済み判定には使わず、
    LLMが両Article本文を確認するときの監査可能なシグナルとしてだけ保持する。
    """
    available_articles = {
        str(document.get("articleContentUnitId") or document.get("parentContentUnitId") or document["contentUnitId"])
        .split("-paragraph-", 1)[0]
        for document in documents
        if document.get("docType") == "law"
    }
    documents_by_id = {
        str(document["contentUnitId"]): document
        for document in documents
        if document.get("docType") == "law"
    }
    document_titles = {
        str(document["documentId"]): str(document.get("title") or "")
        for document in documents
        if document.get("docType") == "law"
    }
    family_decrees = {
        law_family_roots.get(document_id, document_id): document_id
        for document_id, title in document_titles.items()
        if title.endswith("施行令")
    }
    article_texts = _article_texts(documents)
    authority_types = {
        str(document["documentId"]): document.get("authorityType")
        for document in documents
        if document.get("docType") == "law"
    }
    article_clearance_levels = {
        str(
            document.get("articleContentUnitId")
            or document.get("parentContentUnitId")
            or document["contentUnitId"]
        ).split("-paragraph-", 1)[0]: int(document.get("clearanceLevel", 3))
        for document in documents
        if document.get("docType") == "law"
    }
    assertions: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    seen: set[str] = set()
    for document in documents:
        if document.get("docType") != "law":
            continue
        lower_document_id = str(document["documentId"])
        parent_document_id = law_family_roots.get(lower_document_id)
        if not parent_document_id or parent_document_id == lower_document_id:
            continue
        lower_article_id = str(
            document.get("articleContentUnitId")
            or document.get("parentContentUnitId")
            or document["contentUnitId"]
        ).split("-paragraph-", 1)[0]
        if lower_article_id not in available_articles:
            continue
        raw_text = str(document.get("text") or "")
        parent = documents_by_id.get(str(document.get("parentContentUnitId") or ""))
        if parent is None:
            text = raw_text
            source_offset = 0
        else:
            text, source_offset = without_repeated_parent_context_with_offset(
                raw_text, str(parent.get("text") or "").strip()
            )
        referenced_parent_articles = _prefixed_article_references_with_context(
            parent_document_id,
            text,
            PARENT_LAW_ARTICLE_PATTERN,
            expected_authority_title=document_titles.get(parent_document_id),
            known_authority_titles=tuple(document_titles.values()),
        )
        decree_document_id = family_decrees.get(parent_document_id)
        if decree_document_id and decree_document_id != lower_document_id:
            referenced_parent_articles.extend(
                _prefixed_article_references_with_context(
                    decree_document_id,
                    text,
                    PARENT_ORDER_ARTICLE_PATTERN,
                    expected_authority_title=document_titles.get(decree_document_id),
                    known_authority_titles=tuple(document_titles.values()),
                )
            )
        contexts_by_parent: dict[str, list[tuple[str, int, int]]] = {}
        for (
            parent_article_id,
            reference_context,
            local_reference_start,
            local_reference_end,
        ) in referenced_parent_articles:
            contexts_by_parent.setdefault(parent_article_id, []).append(
                (
                    reference_context,
                    local_reference_start + source_offset,
                    local_reference_end + source_offset,
                )
            )
        for parent_article_id, reference_contexts in contexts_by_parent.items():
            if parent_article_id not in available_articles:
                continue
            reference_edge_id = (
                f"edge-{lower_article_id}-references-{parent_article_id}"
            )
            if reference_edge_id not in seen:
                seen.add(reference_edge_id)
                reference_occurrences = [
                    (context[:240], start, min(end, start + 240))
                    for context, start, end in reference_contexts[:4]
                ]
                edges.append(
                    {
                        "graphEdgeId": reference_edge_id,
                        "edgeType": "REFERENCES",
                        "fromGraphNodeId": lower_article_id,
                        "toGraphNodeId": parent_article_id,
                        "documentId": lower_document_id,
                        "referenceKind": REFERENCE_KIND_PARENT_LAW_REFERENCE,
                        "relationSource": "subordinate_law_parent_reference",
                        "relationConfidence": 0.95,
                        "citationText": reference_occurrences[0][0],
                        "citationTexts": [item[0] for item in reference_occurrences],
                        "sourceSpanStarts": [item[1] for item in reference_occurrences],
                        "sourceSpanEnds": [item[2] for item in reference_occurrences],
                        "targetResolutionMethod": "parent_authority_reference",
                        "publishStatus": "published",
                        "isLatest": True,
                        **_relation_seed_identity(
                            document,
                            str(document["contentUnitId"]),
                        ),
                    }
                )
            # 文言シグナルは候補の説明にだけ残す。ここでIMPLEMENTSを確定したり、
            # 弱い候補を削除したりしない。
            assessments = [
                assess_implements(
                    parent_text=article_texts.get(parent_article_id, ""),
                    child_text=reference_context,
                    child_authority_type=authority_types.get(lower_document_id),
                    same_family=True,
                )
                for reference_context, _, _ in reference_contexts
            ]
            assessment = max(
                assessments,
                key=lambda item: item.confidence,
            )
            assertion_id = (
                f"assertion-law-reference-{parent_article_id}"
                f"-implements-{lower_article_id}"
            )
            assertion = assertions.get(assertion_id)
            reference_context_values = list(
                dict.fromkeys(context[:240] for context, _, _ in reference_contexts)
            )[:4]
            if assertion is None:
                assertions[assertion_id] = {
                    "graphNodeId": assertion_id,
                    "nodeType": "RelationAssertion",
                    "assertionId": assertion_id,
                    "fromArticleId": parent_article_id,
                    "toArticleId": lower_article_id,
                    "suggestedType": "IMPLEMENTS",
                    "assertedByDocumentId": lower_document_id,
                    "sourceReferenceEdgeId": reference_edge_id,
                    "sourceText": reference_context_values[0],
                    "sourceTexts": reference_context_values,
                    "assertionSource": "law_reference_candidate_rule",
                    # 未確認候補は文言の強弱にかかわらず同じ信頼状態とする。
                    "confidence": UNVERIFIED_ASSERTION_CONFIDENCE,
                    "status": RELATION_STATUS_UNVERIFIED,
                    "sameFamily": True,
                    "delegationWordingDetected": assessment.delegation_wording_detected,
                    "specificationWordingDetected": assessment.specification_wording_detected,
                    "publishStatus": "published",
                    "isLatest": True,
                    "clearanceLevel": max(
                        article_clearance_levels.get(parent_article_id, 3),
                        article_clearance_levels.get(lower_article_id, 3),
                    ),
                    "graphSchemaVersion": GRAPH_SCHEMA_VERSION,
                }
            else:
                assertion["sourceTexts"] = list(
                    dict.fromkeys(
                        [
                            *(assertion.get("sourceTexts") or []),
                            *reference_context_values,
                        ]
                    )
                )[:4]
                assertion["delegationWordingDetected"] = bool(
                    assertion.get("delegationWordingDetected")
                    or assessment.delegation_wording_detected
                )
                assertion["specificationWordingDetected"] = bool(
                    assertion.get("specificationWordingDetected")
                    or assessment.specification_wording_detected
                )
    return list(assertions.values()), edges


def _parent_law_article_reference_ids(parent_document_id: str, text: str) -> list[str]:
    return _prefixed_article_reference_ids(
        parent_document_id,
        text,
        PARENT_LAW_ARTICLE_PATTERN,
    )


def _parent_order_article_reference_ids(
    parent_document_id: str,
    text: str,
) -> list[str]:
    return _prefixed_article_reference_ids(
        parent_document_id,
        text,
        PARENT_ORDER_ARTICLE_PATTERN,
    )


def _prefixed_article_reference_ids(
    parent_document_id: str,
    text: str,
    pattern: re.Pattern[str],
) -> list[str]:
    references = []
    for match in pattern.finditer(text):
        parts = [match.group(1), *match.group(2).removeprefix("の").split("の")]
        numbers = [_japanese_number_to_int(part) for part in parts if part]
        if not numbers or any(number is None for number in numbers):
            continue
        suffix = "_".join(str(number) for number in numbers)
        content_unit_id = f"{parent_document_id}-article-{suffix}"
        if content_unit_id not in references:
            references.append(content_unit_id)
    return references


def _prefixed_article_references_with_context(
    parent_document_id: str,
    text: str,
    pattern: re.Pattern[str],
    *,
    expected_authority_title: str | None = None,
    known_authority_titles: tuple[str, ...] = (),
) -> list[tuple[str, str, int, int]]:
    """親Article IDと、その参照を含む局所文脈を返す。

    条全体を使うと、別の項号への単純参照と委任文言を誤って結合してしまうため、
    参照位置から文末または閉じ括弧までに限定する。
    """
    references: list[tuple[str, str, int, int]] = []
    for match in pattern.finditer(text):
        if not _relative_reference_targets_expected_authority(
            text,
            match,
            expected_authority_title=expected_authority_title,
            known_authority_titles=known_authority_titles,
        ):
            continue
        parts = [
            match.group(1),
            *match.group(2).removeprefix("の").split("の"),
        ]
        numbers = [_japanese_number_to_int(part) for part in parts if part]
        if not numbers or any(number is None for number in numbers):
            continue
        suffix = "_".join(str(number) for number in numbers)
        article_id = f"{parent_document_id}-article-{suffix}"
        context_end = min(len(text), match.end() + 160)
        # 同じ括弧・文の後半に別Articleへの「規定による」が現れることがあるため、
        # 読点も境界にして現在の参照へ直接係る表現だけを判定する。
        for delimiter in ("、", "。", "\n"):
            position = text.find(delimiter, match.end(), context_end)
            if position >= 0:
                context_end = min(context_end, position + len(delimiter))
        if text[: match.start()].count("（") > text[: match.start()].count("）"):
            position = text.find("）", match.end(), context_end)
            if position >= 0:
                context_end = min(context_end, position + 1)
        references.append(
            (article_id, text[match.start():context_end], match.start(), context_end)
        )
    return references


def _relative_reference_targets_expected_authority(
    text: str,
    match: re.Match[str],
    *,
    expected_authority_title: str | None,
    known_authority_titles: tuple[str, ...],
) -> bool:
    matched = match.group(0)
    if not matched.startswith(("同法", "同令")):
        return True

    sentence_start = max(
        text.rfind(delimiter, 0, match.start()) for delimiter in ("。", "\n", "；")
    )
    prefix = text[sentence_start + 1 : match.start()]
    anchors: list[tuple[int, bool]] = []
    standalone_pattern = (
        re.compile(r"(?:当該法律|本法|(?<![一-鿏])法)第")
        if matched.startswith("同法")
        else re.compile(r"(?:当該政令|本令|(?<![一-鿏])令)第")
    )
    anchors.extend((item.start(), True) for item in standalone_pattern.finditer(prefix))

    for title in dict.fromkeys(known_authority_titles):
        if not title:
            continue
        marker = f"{title}第"
        position = prefix.rfind(marker)
        if position >= 0:
            anchors.append((position, title == expected_authority_title))

    # The referenced law may be outside the indexed family (for example,
    # 会社法). Treat such an explicit title as a non-parent anchor. Ambiguous
    # relative tokens never become a confirmed REFERENCES edge.
    suffix = "(?:法律|法)" if matched.startswith("同法") else "(?:政令|令)"
    named_pattern = re.compile(rf"(?<![一-鿏])([一-鿏々]{{1,50}}{suffix})第")
    for item in named_pattern.finditer(prefix):
        anchors.append(
            (
                item.start(),
                bool(expected_authority_title)
                and item.group(1) == expected_authority_title,
            )
        )

    if not anchors:
        return False
    nearest_position = max(position for position, _ in anchors)
    return any(
        is_expected
        for position, is_expected in anchors
        if position == nearest_position
    )


def _article_heading(value: Any) -> str | None:
    """項・号チャンクの見出しからArticle共通の見出しだけを取り出す。"""
    if value is None:
        return None
    heading = str(value).strip()
    return ARTICLE_HEADING_CHILD_SUFFIX_PATTERN.sub("", heading)


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


def _seed_minio(
    samples_dir: Path,
    documents: list[dict[str, Any]],
    external_guidance_sources: list[dict[str, Any]] | None = None,
) -> tuple[int, int]:
    client = _minio_client()
    try:
        if not client.bucket_exists(settings.minio_bucket):
            client.make_bucket(settings.minio_bucket)
    except S3Error:
        raise

    expected_vector_objects = {
        str(document["processedObjectUri"]).replace("minio://knowledge-root/", "")
        for document in documents
    }
    stale_removed = _remove_stale_vector_objects(client, expected_vector_objects)

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

    for source in external_guidance_sources or []:
        source_path = Path(source["_sourcePath"])
        object_name = f"source-documents/external-guidance/{source_path.name}"
        data = source_path.read_bytes()
        client.put_object(settings.minio_bucket, object_name, data=_Bytes(data), length=len(data), content_type="application/pdf")
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
    return count, stale_removed


def _remove_stale_vector_objects(
    client: Minio,
    expected_object_names: set[str],
) -> int:
    """現行OpenSearch投入対象から外れた派生vector文書だけを削除する。

    原本・評価データ・前処理成果物は対象にしない。seedを繰り返してもMinIOへ旧chunkが
    蓄積し続けないよう、管理対象prefixを限定して同期する。
    """
    prefix = "derived-artifacts/vector-documents/"
    stale = [
        item.object_name
        for item in client.list_objects(
            settings.minio_bucket,
            prefix=prefix,
            recursive=True,
        )
        if item.object_name not in expected_object_names
    ]
    if not stale:
        return 0
    errors = list(
        client.remove_objects(
            settings.minio_bucket,
            (DeleteObject(object_name) for object_name in stale),
        )
    )
    if errors:
        first = errors[0]
        raise RuntimeError(
            "Failed to remove stale MinIO vector object: "
            f"{getattr(first, 'object_name', '')} {getattr(first, 'message', first)}"
        )
    return len(stale)


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
