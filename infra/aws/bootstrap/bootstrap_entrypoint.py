#!/usr/bin/env python3
"""S3上の固定成果物を取得してbootstrap管理コマンドを起動する。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import bootstrap_aws_data


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _download_artifact(bucket: str, prefix: str, destination: Path) -> None:
    import boto3

    client = boto3.client("s3")
    artifact_prefix = f"{prefix.strip('/')}/artifact/"
    paginator = client.get_paginator("list_objects_v2")
    downloaded = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=artifact_prefix):
        for item in page.get("Contents", []):
            key = str(item["Key"])
            relative = key.removeprefix(artifact_prefix)
            if not relative or relative.startswith("/") or ".." in Path(relative).parts:
                raise ValueError(f"unsafe artifact S3 key: {key}")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(bucket, key, str(target))
            downloaded += 1
    if downloaded == 0 or not (destination / "manifest.json").is_file():
        raise ValueError("S3 artifact prefix contains no bootstrap manifest")


def main() -> int:
    work_dir = Path(os.environ.get("BOOTSTRAP_WORK_DIR", "/tmp"))
    work_dir.mkdir(parents=True, exist_ok=True)
    config_path = work_dir / "bootstrap-config.json"
    config = {
        "account": _required("AWS_ACCOUNT_ID"),
        "region": _required("AWS_REGION"),
        "bootstrapData": {
            "searchSnapshotId": _required("SEARCH_SNAPSHOT_ID"),
            "graphSnapshotId": _required("GRAPH_SNAPSHOT_ID"),
            "classificationRunId": _required("CLASSIFICATION_RUN_ID"),
            "s3Prefix": _required("BOOTSTRAP_S3_PREFIX"),
        },
        "openSearchServerless": {
            "indexName": _required("OPENSEARCH_INDEX"),
            "embeddingModelId": _required("EMBEDDING_MODEL"),
            "embeddingDimensions": int(_required("EMBEDDING_DIMENSION")),
            "embeddingMaxChars": int(_required("EMBEDDING_MAX_CHARS")),
        },
    }
    config_path.write_text(
        json.dumps(config, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    bucket = _required("KNOWLEDGE_BUCKET_NAME")
    artifact_dir = work_dir / "bootstrap-artifact"
    _download_artifact(bucket, config["bootstrapData"]["s3Prefix"], artifact_dir)
    sys.argv = [
        "bootstrap_aws_data.py",
        "--config",
        str(config_path),
        "--artifact-dir",
        str(artifact_dir),
        "--bucket",
        bucket,
        "--opensearch-endpoint",
        _required("OPENSEARCH_URL"),
        "--neptune-graph-id",
        _required("NEPTUNE_GRAPH_ID"),
        "--checkpoint",
        str(work_dir / "aws-bootstrap.checkpoint.json"),
        "--apply",
    ]
    return bootstrap_aws_data.main()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as error:
        print(f"bootstrap task failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
