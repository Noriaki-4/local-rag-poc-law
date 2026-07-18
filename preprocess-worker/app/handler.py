"""S3イベントを受けて前処理を実行するハンドラ。

イベント形式はS3 Event Notificationの標準形(Records[].s3.bucket.name /
Records[].s3.object.key)のみに依存する。ローカルではcli.pyが同形のdictを
組み立てて呼び、AWS移行時はLambdaハンドラからそのまま呼ぶ。
"""

from hashlib import sha256
from typing import Any, Callable
from urllib.parse import unquote_plus

RAW_PREFIX = "source-documents/external-guidance/"
DERIVED_PREFIX = "derived-artifacts/preprocessed/external-guidance/"


def derived_key_for(raw_key: str) -> str:
    stem = raw_key.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return f"{DERIVED_PREFIX}{stem}.json"


def build_s3_event(bucket: str, keys: list[str]) -> dict[str, Any]:
    """手動トリガー(CLI)用に、S3イベント通知と同形のdictを組み立てる。"""
    return {
        "Records": [
            {"s3": {"bucket": {"name": bucket}, "object": {"key": key}}}
            for key in keys
        ]
    }


def handle_s3_event(
    event: dict[str, Any],
    storage: Any = None,
    convert_fn: Callable[[bytes, str], dict[str, Any]] | None = None,
) -> list[str]:
    """イベント中のrawゾーンPDFを変換し、派生ゾーンへJSONをputする。

    処理した派生キーのリストを返す。rawゾーン外・PDF以外のキーは無視する。
    """
    if storage is None:
        from .storage import ObjectStorage

        storage = ObjectStorage()
    if convert_fn is None:
        from .convert import convert_pdf_bytes

        convert_fn = convert_pdf_bytes

    processed: list[str] = []
    for record in event.get("Records", []):
        s3_entry = record.get("s3") or {}
        bucket = str((s3_entry.get("bucket") or {}).get("name") or "")
        # S3イベント通知はobject.keyをURLエンコードして届けるため復号する
        key = unquote_plus(str((s3_entry.get("object") or {}).get("key") or ""))
        if not bucket or not key:
            continue
        if not key.startswith(RAW_PREFIX) or not key.lower().endswith(".pdf"):
            continue
        data = storage.get_bytes(bucket, key)
        artifact = convert_fn(data, f"sha256:{sha256(data).hexdigest()}")
        derived_key = derived_key_for(key)
        storage.put_json(bucket, derived_key, artifact)
        processed.append(derived_key)
    return processed
