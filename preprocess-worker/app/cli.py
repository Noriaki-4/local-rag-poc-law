"""前処理の手動トリガー(このリポジトリでの唯一のローカル起動経路)。

内部でS3イベント同形のdictを組み立ててhandle_s3_eventを呼ぶため、
手動実行でも本番(S3 Event Notification→Lambda)と同じコードパスを通る。

使い方:
  python -m app.cli --sync-local           # datasets/のPDFをrawゾーンへ上げてから全件処理
  python -m app.cli                        # rawゾーンにある既存PDFを全件処理
  python -m app.cli --only mhlw-000761110  # ファイル名(部分一致)で絞り込み
"""

import argparse
import os
import sys
from pathlib import Path

from .handler import RAW_PREFIX, build_s3_event, handle_s3_event
from .storage import ObjectStorage

DEFAULT_LOCAL_DOCUMENTS_DIR = "/workspace/datasets/lawqa_jp/external-guidance/documents"


def main() -> int:
    parser = argparse.ArgumentParser(description="Preprocess guidance PDFs with docling")
    parser.add_argument("--bucket", default=os.getenv("PREPROCESS_BUCKET", "knowledge-root"))
    parser.add_argument(
        "--sync-local",
        nargs="?",
        const=DEFAULT_LOCAL_DOCUMENTS_DIR,
        default=None,
        metavar="DIR",
        help="ローカルディレクトリのPDFをrawゾーンへアップロードしてから処理する",
    )
    parser.add_argument("--only", default=None, help="ファイル名の部分一致で処理対象を絞る")
    args = parser.parse_args()

    storage = ObjectStorage()
    storage.ensure_bucket(args.bucket)

    if args.sync_local:
        keys = _upload_local_pdfs(storage, args.bucket, Path(args.sync_local))
    else:
        keys = [key for key in storage.list_keys(args.bucket, RAW_PREFIX) if key.lower().endswith(".pdf")]

    if args.only:
        keys = [key for key in keys if args.only in key]
    if not keys:
        print("No PDFs to process", file=sys.stderr)
        return 1

    for key in keys:
        print(f"Processing {key} ...", flush=True)
        processed = handle_s3_event(build_s3_event(args.bucket, [key]))
        for derived_key in processed:
            print(f"  -> {derived_key}", flush=True)
    return 0


def _upload_local_pdfs(storage: ObjectStorage, bucket: str, documents_dir: Path) -> list[str]:
    if not documents_dir.is_dir():
        raise FileNotFoundError(f"Local documents directory not found: {documents_dir}")
    keys = []
    for pdf_path in sorted(documents_dir.glob("*.pdf")):
        key = f"{RAW_PREFIX}{pdf_path.name}"
        storage.put_bytes(bucket, key, pdf_path.read_bytes(), "application/pdf")
        print(f"Uploaded {pdf_path.name} -> {key}", flush=True)
        keys.append(key)
    return keys


if __name__ == "__main__":
    raise SystemExit(main())
