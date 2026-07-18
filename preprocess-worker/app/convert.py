"""docling変換の中核ロジック。PDFバイト列→簡易JSON dict。

出力は自前スキーマ(schemaVersion=1)とし、読み手(agent-apiのseed.py、将来の
AWSインデクサ)にdocling依存を持ち込まない。AWS移行後もこのモジュールは
無変更でLambda/ECSから使う。
"""

import tempfile
from pathlib import Path
from typing import Any

ARTIFACT_SCHEMA_VERSION = 1


def convert_pdf_bytes(data: bytes, source_sha256: str) -> dict[str, Any]:
    """PDFバイト列をdoclingで構造分解し、簡易スキーマのdictを返す。

    itemsは読み順で、type: section_header / text / table のいずれか。
    tableはMarkdown表現(markdown)とセル平文連結(text)の両方を持つ。
    """
    from docling.document_converter import DocumentConverter
    from docling_core.types.doc import SectionHeaderItem, TableItem, TextItem

    with tempfile.TemporaryDirectory() as tmp_dir:
        pdf_path = Path(tmp_dir) / "source.pdf"
        pdf_path.write_bytes(data)
        result = DocumentConverter().convert(str(pdf_path))
        document = result.document

        items: list[dict[str, Any]] = []
        for item, _level in document.iterate_items():
            page = _item_page(item)
            if isinstance(item, TableItem):
                markdown = item.export_to_markdown(document)
                plain = _table_plain_text(item) or markdown
                items.append({"type": "table", "page": page, "text": plain, "markdown": markdown})
            elif isinstance(item, SectionHeaderItem):
                text = (item.text or "").strip()
                if text:
                    items.append({"type": "section_header", "page": page, "text": text})
            elif isinstance(item, TextItem):
                text = (item.text or "").strip()
                if text:
                    items.append({"type": "text", "page": page, "text": text})

    if not items:
        raise ValueError("docling conversion produced no extractable items")
    return {
        "schemaVersion": ARTIFACT_SCHEMA_VERSION,
        "sourceSha256": source_sha256,
        "converter": "docling",
        "items": items,
    }


def _item_page(item: Any) -> int | None:
    provenance = getattr(item, "prov", None) or []
    for entry in provenance:
        page_no = getattr(entry, "page_no", None)
        if page_no is not None:
            return int(page_no)
    return None


def _table_plain_text(item: Any) -> str:
    data = getattr(item, "data", None)
    cells = getattr(data, "table_cells", None) or []
    texts = [str(getattr(cell, "text", "") or "").strip() for cell in cells]
    return " ".join(text for text in texts if text)
