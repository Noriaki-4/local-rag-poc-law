"""S3互換オブジェクトストレージへの薄いラッパー。

ローカルではAWS_ENDPOINT_URLでMinIOを指す。AWS移行時はこの環境変数を
外すだけで本物のS3に切り替わる(コード変更不要)。
"""

import json
import os
from typing import Any


def create_client():
    import boto3

    endpoint_url = os.getenv("AWS_ENDPOINT_URL") or None
    return boto3.client("s3", endpoint_url=endpoint_url)


class ObjectStorage:
    """get/putだけを提供する最小ストレージ。テストではこのクラスを偽物に差し替える。"""

    def __init__(self, client=None):
        self._client = client or create_client()

    def get_bytes(self, bucket: str, key: str) -> bytes:
        response = self._client.get_object(Bucket=bucket, Key=key)
        return response["Body"].read()

    def put_bytes(self, bucket: str, key: str, data: bytes, content_type: str) -> None:
        self._client.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)

    def put_json(self, bucket: str, key: str, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.put_bytes(bucket, key, data, "application/json; charset=utf-8")

    def ensure_bucket(self, bucket: str) -> None:
        try:
            self._client.head_bucket(Bucket=bucket)
        except Exception:
            self._client.create_bucket(Bucket=bucket)

    def list_keys(self, bucket: str, prefix: str) -> list[str]:
        keys: list[str] = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            keys.extend(item["Key"] for item in page.get("Contents", []))
        return keys
