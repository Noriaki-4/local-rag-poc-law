import json
import sys
from hashlib import sha256
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.handler import build_s3_event, derived_key_for, handle_s3_event


class FakeStorage:
    def __init__(self, objects: dict[tuple[str, str], bytes]):
        self.objects = dict(objects)
        self.put_calls: list[tuple[str, str, dict]] = []

    def get_bytes(self, bucket: str, key: str) -> bytes:
        return self.objects[(bucket, key)]

    def put_json(self, bucket: str, key: str, payload: dict) -> None:
        self.put_calls.append((bucket, key, payload))


def _fake_convert(data: bytes, source_sha256: str) -> dict:
    return {"schemaVersion": 1, "sourceSha256": source_sha256, "converter": "fake", "items": []}


def test_derived_key_maps_pdf_to_preprocessed_json():
    raw_key = "source-documents/external-guidance/mhlw-000761110.pdf"
    assert derived_key_for(raw_key) == "derived-artifacts/preprocessed/external-guidance/mhlw-000761110.json"


def test_handle_s3_event_converts_and_puts_derived_artifact():
    pdf_bytes = b"%PDF fake"
    raw_key = "source-documents/external-guidance/guidance.pdf"
    storage = FakeStorage({("knowledge-root", raw_key): pdf_bytes})

    processed = handle_s3_event(
        build_s3_event("knowledge-root", [raw_key]),
        storage=storage,
        convert_fn=_fake_convert,
    )

    assert processed == ["derived-artifacts/preprocessed/external-guidance/guidance.json"]
    bucket, key, payload = storage.put_calls[0]
    assert bucket == "knowledge-root"
    assert key == processed[0]
    assert payload["sourceSha256"] == f"sha256:{sha256(pdf_bytes).hexdigest()}"


def test_handle_s3_event_decodes_url_encoded_keys():
    raw_key = "source-documents/external-guidance/my guidance.pdf"
    encoded_key = "source-documents/external-guidance/my+guidance.pdf"
    storage = FakeStorage({("knowledge-root", raw_key): b"%PDF"})

    processed = handle_s3_event(
        {"Records": [{"s3": {"bucket": {"name": "knowledge-root"}, "object": {"key": encoded_key}}}]},
        storage=storage,
        convert_fn=_fake_convert,
    )

    assert processed == ["derived-artifacts/preprocessed/external-guidance/my guidance.json"]


def test_handle_s3_event_ignores_keys_outside_raw_zone_and_non_pdf():
    storage = FakeStorage({})
    event = build_s3_event(
        "knowledge-root",
        [
            "eval-data/samples/some.pdf",
            "source-documents/external-guidance/readme.txt",
        ],
    )

    assert handle_s3_event(event, storage=storage, convert_fn=_fake_convert) == []
    assert storage.put_calls == []


def test_artifact_payload_is_json_serializable():
    payload = _fake_convert(b"x", "sha256:0")
    assert json.loads(json.dumps(payload)) == payload
