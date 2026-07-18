import json
import re
import xml.etree.ElementTree as ET
from hashlib import sha256
from pathlib import Path
from typing import Any

from minio import Minio
from minio.error import S3Error
from pypdf import PdfReader
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


def seed_all(os_client: OpenSearchClient, graph_client: GraphClient) -> dict[str, Any]:
    samples_dir = settings.samples_dir
    mapping = _read_json(samples_dir / "metadata" / "opensearch_index_mapping.sample.json")
    os_client.recreate_index(mapping)

    external_guidance_sources = _external_guidance_sources()
    documents = _opensearch_documents(samples_dir, external_guidance_sources)
    for document in documents:
        os_client.index_document(document)
    os_client.refresh()

    nodes = _read_jsonl(samples_dir / "metadata" / "nodes.sample.jsonl")
    edges = _read_jsonl(samples_dir / "metadata" / "edges.sample.jsonl")
    egov_nodes, egov_edges = _graph_artifacts_from_documents(documents)
    guidance_nodes, guidance_edges = _guidance_graph_artifacts(documents)
    nodes = _dedupe_by_key([*nodes, *egov_nodes, *guidance_nodes], "graphNodeId")
    edges = _dedupe_by_key([*edges, *egov_edges, *guidance_edges], "graphEdgeId")
    # EXPLAINSの張り先条文は、法令の部分投入(例: 民法601-622条の2のみ)ではグラフに
    # 存在しないことがある。dangling assertでseed全体を止めず、該当EXPLAINSだけ落とす。
    edges, dropped_explains = _drop_dangling_explains_edges(edges, {node["graphNodeId"] for node in nodes})
    if dropped_explains:
        print(f"[seed] dropped {dropped_explains} EXPLAINS edge(s) whose target article is not in the graph")
    _assert_no_dangling_edges(nodes, edges)
    graph_client.clear()
    graph_client.seed_nodes(nodes)
    graph_client.seed_edges(edges)

    minio_count = _seed_minio(samples_dir, documents, external_guidance_sources)

    return {
        "opensearchDocuments": len(documents),
        "externalGuidanceDocuments": sum(1 for document in documents if document.get("docType") == "guideline"),
        "graphNodes": len(nodes),
        "graphEdges": len(edges),
        "minioObjects": minio_count,
    }


def _opensearch_documents(samples_dir: Path, external_guidance_sources: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    sample = _read_json(samples_dir / "metadata" / "opensearch_document.sample.json")
    law_fsa = dict(sample)
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


def _guidance_graph_artifacts(
    documents: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """ガイドライン文書を「羅針盤」としてグラフに載せる。

    文書内の全チャンクの relatedArticleContentUnitIds を文書単位で集約し、
    ガイドライン文書ノード -EXPLAINS-> 法令条文ノード のエッジを張る。検索時は
    上位ヒットしたガイドラインチャンクの documentId からこのエッジを辿り、
    解説対象の条文を特定する(条文本文の取得はOpenSearch側の役割)。

    条文ノードは法令投入側(_graph_artifacts_from_documents)が作るため、ここでは
    作らない。張り先が存在しないEXPLAINSは seed_all 側の dangling ドロップに委ねる。
    """
    ordered_articles_by_document: dict[str, list[str]] = {}
    representative_by_document: dict[str, dict[str, Any]] = {}
    for document in documents:
        if document.get("docType") != "guideline":
            continue
        document_id = document["documentId"]
        representative_by_document.setdefault(document_id, document)
        articles = ordered_articles_by_document.setdefault(document_id, [])
        for article_id in document.get("relatedArticleContentUnitIds") or []:
            if article_id not in articles:
                articles.append(article_id)

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for document_id, article_ids in ordered_articles_by_document.items():
        if not article_ids:
            continue  # 対応表・条文注釈が無い文書はグラフに載せない(孤立ノードを作らない)
        document = representative_by_document[document_id]
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
            }
        )
        for article_id in article_ids:
            edges.append(
                {
                    "graphEdgeId": f"edge-{document_id}-explains-{article_id}",
                    "edgeType": "EXPLAINS",
                    "fromGraphNodeId": document_id,
                    "toGraphNodeId": article_id,
                    "documentId": document_id,
                    "relationSource": "guidance_article_annotation",
                    "relationConfidence": 0.9,
                    "publishStatus": "published",
                    "isLatest": True,
                }
            )
    return nodes, edges


def _drop_dangling_explains_edges(
    edges: list[dict[str, Any]],
    node_ids: set[str],
) -> tuple[list[dict[str, Any]], int]:
    """張り先/張り元ノードがグラフに無い EXPLAINS エッジだけを除去する。
    EXPLAINS 以外の dangling は _assert_no_dangling_edges で従来どおり検出させる。"""
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


def _seed_minio(
    samples_dir: Path,
    documents: list[dict[str, Any]],
    external_guidance_sources: list[dict[str, Any]] | None = None,
) -> int:
    client = _minio_client()
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
