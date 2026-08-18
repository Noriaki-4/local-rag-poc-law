"""e-Gov法令XMLを内容ハッシュ付きの再利用可能なデータセットへ同期する。"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = (
    REPO_ROOT / "docs/requirements/samples/eval/law_registry.json"
)
DEFAULT_OUTPUT_DIR = REPO_ROOT / "datasets/lawqa_jp/egov_law_corpus"
DEFAULT_API_BASE_URL = "https://laws.e-gov.go.jp/api/1"
SCHEMA_VERSION = 1


class EgovDatasetError(RuntimeError):
    """取得結果またはローカルデータセットが契約を満たさない。"""


def _sha256_hex(data: bytes) -> str:
    return sha256(data).hexdigest()


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_hex(encoded)


def _element_text(element: ET.Element | None) -> str | None:
    if element is None:
        return None
    value = "".join(element.itertext()).strip()
    return value or None


def inspect_egov_xml(payload: bytes, *, expected_title: str) -> dict[str, Any]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise EgovDatasetError(f"e-Gov response is not valid XML: {exc}") from exc

    code = root.findtext("./Result/Code")
    if code and code != "0":
        message = root.findtext("./Result/Message") or "unknown e-Gov API error"
        raise EgovDatasetError(f"e-Gov API returned code {code}: {message}")
    law = root.find(".//Law")
    if law is None:
        raise EgovDatasetError("e-Gov response has no Law element")
    title = _element_text(law.find(".//LawTitle"))
    if not title:
        raise EgovDatasetError("e-Gov response has no LawTitle")
    if "".join(title.split()) != "".join(expected_title.split()):
        raise EgovDatasetError(
            f"law title mismatch: registry={expected_title!r}, e-Gov={title!r}"
        )
    article_count = len(law.findall(".//Article"))
    if article_count == 0:
        raise EgovDatasetError("e-Gov response contains no Article")
    return {
        "lawTitle": title,
        "lawNum": _element_text(law.find(".//LawNum")),
        "lawType": law.get("LawType"),
        "lang": law.get("Lang"),
        "era": law.get("Era"),
        "year": law.get("Year"),
        "num": law.get("Num"),
        "promulgateMonth": law.get("PromulgateMonth"),
        "promulgateDay": law.get("PromulgateDay"),
        "mainProvisionCount": len(law.findall(".//MainProvision")),
        "supplementaryProvisionCount": len(law.findall(".//SupplProvision")),
        "articleCount": article_count,
    }


def fetch_egov_xml(url: str, *, timeout_sec: int = 120) -> bytes:
    request = Request(
        url,
        headers={"User-Agent": "local-rag-poc-law/egov-dataset-sync"},
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=timeout_sec) as response:
                return response.read()
        except (HTTPError, URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(attempt + 1)
    raise EgovDatasetError(f"failed to download {url}: {last_error}")


def _load_registry(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    laws = payload.get("laws")
    if not isinstance(laws, list) or not laws:
        raise EgovDatasetError("law registry must contain a non-empty laws array")
    ids = [str(item.get("lawId") or "") for item in laws]
    if any(not law_id for law_id in ids) or len(ids) != len(set(ids)):
        raise EgovDatasetError("law registry lawId values must be non-empty and unique")
    if any(not str(item.get("title") or "") for item in laws):
        raise EgovDatasetError("every registry law requires a title")
    return laws


def _load_current_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schemaVersion") != SCHEMA_VERSION:
        raise EgovDatasetError(
            f"unsupported existing manifest schema: {manifest.get('schemaVersion')}"
        )
    return manifest


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _entry_from_payload(
    *,
    registry_entry: dict[str, Any],
    payload: bytes,
    output_dir: Path,
    source_url: str,
    retrieved_at: str,
) -> dict[str, Any]:
    law_id = str(registry_entry["lawId"])
    metadata = inspect_egov_xml(
        payload, expected_title=str(registry_entry["title"])
    )
    digest = _sha256_hex(payload)
    relative_path = Path("documents") / law_id / f"{digest}.xml"
    destination = output_dir / relative_path
    if destination.exists():
        existing = destination.read_bytes()
        if existing != payload:
            raise EgovDatasetError(f"content-addressed file mismatch: {destination}")
    else:
        _atomic_write(destination, payload)
    return {
        "lawId": law_id,
        "registryTitle": registry_entry["title"],
        "aliases": registry_entry.get("aliases", []),
        "familyRoot": registry_entry.get("familyRoot") or law_id,
        "authorityType": registry_entry.get("authorityType"),
        "authoritySource": registry_entry.get("authoritySource"),
        "seedSpec": registry_entry.get("seedSpec"),
        "sourceUrl": source_url,
        "retrievedAt": retrieved_at,
        "path": relative_path.as_posix(),
        "sha256": f"sha256:{digest}",
        "bytes": len(payload),
        **metadata,
    }


def _reuse_entry(
    *,
    registry_entry: dict[str, Any],
    existing_entry: dict[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    relative_path = Path(str(existing_entry.get("path") or ""))
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise EgovDatasetError(
            f"unsafe path in manifest for {registry_entry['lawId']}: {relative_path}"
        )
    source = output_dir / relative_path
    if not source.is_file():
        raise EgovDatasetError(f"cached XML is missing: {source}")
    payload = source.read_bytes()
    expected_hash = str(existing_entry.get("sha256") or "").removeprefix(
        "sha256:"
    )
    if _sha256_hex(payload) != expected_hash:
        raise EgovDatasetError(f"cached XML hash mismatch: {source}")
    metadata = inspect_egov_xml(
        payload, expected_title=str(registry_entry["title"])
    )
    law_id = str(registry_entry["lawId"])
    return {
        **existing_entry,
        "lawId": law_id,
        "registryTitle": registry_entry["title"],
        "aliases": registry_entry.get("aliases", []),
        "familyRoot": registry_entry.get("familyRoot") or law_id,
        "authorityType": registry_entry.get("authorityType"),
        "authoritySource": registry_entry.get("authoritySource"),
        "seedSpec": registry_entry.get("seedSpec"),
        "bytes": len(payload),
        **metadata,
    }


def sync_corpus(
    *,
    registry_path: Path,
    output_dir: Path,
    api_base_url: str,
    selected_law_ids: set[str] | None = None,
    refresh: bool = False,
    timeout_sec: int = 120,
    fetcher: Callable[..., bytes] = fetch_egov_xml,
) -> tuple[dict[str, Any], dict[str, int]]:
    registry_entries = _load_registry(registry_path)
    registry_ids = {str(item["lawId"]) for item in registry_entries}
    selected = selected_law_ids or registry_ids
    unknown = selected.difference(registry_ids)
    if unknown:
        raise EgovDatasetError(f"unknown lawId: {sorted(unknown)}")

    current_manifest_path = output_dir / "manifest.json"
    current_manifest = _load_current_manifest(current_manifest_path)
    existing_by_id = {
        str(item["lawId"]): item
        for item in (current_manifest or {}).get("laws", [])
        if item.get("lawId")
    }
    retrieved_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    laws: list[dict[str, Any]] = []
    downloaded = 0
    reused = 0
    for registry_entry in registry_entries:
        law_id = str(registry_entry["lawId"])
        existing = existing_by_id.get(law_id)
        should_fetch = law_id in selected and (refresh or existing is None)
        if should_fetch:
            source_url = f"{api_base_url.rstrip('/')}/lawdata/{law_id}"
            payload = fetcher(source_url, timeout_sec=timeout_sec)
            laws.append(
                _entry_from_payload(
                    registry_entry=registry_entry,
                    payload=payload,
                    output_dir=output_dir,
                    source_url=source_url,
                    retrieved_at=retrieved_at,
                )
            )
            downloaded += 1
        elif existing is not None:
            laws.append(
                _reuse_entry(
                    registry_entry=registry_entry,
                    existing_entry=existing,
                    output_dir=output_dir,
                )
            )
            reused += 1

    if not laws:
        raise EgovDatasetError("no laws were selected and no cached laws exist")
    registry_sha256 = _sha256_hex(registry_path.read_bytes())
    snapshot_basis = {
        "registrySha256": registry_sha256,
        "laws": [
            {"lawId": item["lawId"], "sha256": item["sha256"]}
            for item in sorted(laws, key=lambda item: item["lawId"])
        ],
    }
    dataset_snapshot_id = f"egov-law-corpus-{_stable_hash(snapshot_basis)}"
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "datasetType": "egov_law_xml",
        "datasetSnapshotId": dataset_snapshot_id,
        "createdAt": retrieved_at,
        "sourceApiBaseUrl": api_base_url.rstrip("/"),
        "registry": {
            "path": str(registry_path.resolve()),
            "sha256": f"sha256:{registry_sha256}",
        },
        "lawCount": len(laws),
        "totalBytes": sum(int(item["bytes"]) for item in laws),
        "laws": laws,
    }
    encoded = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    immutable_manifest_path = (
        output_dir / "manifests" / f"{dataset_snapshot_id}.json"
    )
    if immutable_manifest_path.exists():
        immutable_manifest = json.loads(
            immutable_manifest_path.read_text(encoding="utf-8")
        )
        manifest = immutable_manifest
        encoded = immutable_manifest_path.read_bytes()
    else:
        _atomic_write(immutable_manifest_path, encoded)
    _atomic_write(current_manifest_path, encoded)
    return manifest, {"downloaded": downloaded, "reused": reused}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--api-base-url", default=DEFAULT_API_BASE_URL)
    parser.add_argument("--law-id", action="append", default=[])
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="キャッシュ済みXMLもe-Govから再取得し、変更時は新snapshotを作る",
    )
    parser.add_argument("--timeout-sec", type=int, default=120)
    args = parser.parse_args()
    try:
        manifest, counts = sync_corpus(
            registry_path=args.registry,
            output_dir=args.output_dir,
            api_base_url=args.api_base_url,
            selected_law_ids=set(args.law_id) if args.law_id else None,
            refresh=args.refresh,
            timeout_sec=max(1, args.timeout_sec),
        )
    except (EgovDatasetError, OSError, json.JSONDecodeError) as exc:
        parser.exit(1, f"error: {exc}\n")
    print(
        json.dumps(
            {
                "datasetSnapshotId": manifest["datasetSnapshotId"],
                "lawCount": manifest["lawCount"],
                "totalBytes": manifest["totalBytes"],
                **counts,
                "manifest": str((args.output_dir / "manifest.json").resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
