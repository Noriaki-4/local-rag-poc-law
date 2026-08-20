"""Load a frozen, allowlisted e-Gov scenario dataset for isolated seed runs."""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ScenarioDatasetError(ValueError):
    """A scenario manifest is unsafe or internally inconsistent."""


@dataclass(frozen=True)
class ScenarioLaw:
    law_id: str
    title: str
    authority_type: str
    family_root: str
    source_path: Path
    source_sha256: str


@dataclass(frozen=True)
class ScenarioDataset:
    dataset_id: str
    dataset_snapshot_id: str
    parent_dataset_snapshot_id: str
    manifest_path: Path
    laws: tuple[ScenarioLaw, ...]
    article_ids_by_law: dict[str, tuple[str, ...]]

    @property
    def article_ids(self) -> frozenset[str]:
        return frozenset(
            article_id
            for article_ids in self.article_ids_by_law.values()
            for article_id in article_ids
        )

    @property
    def family_roots(self) -> dict[str, str]:
        return {
            f"law-{law.law_id}": f"law-{law.family_root}" for law in self.laws
        }


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ScenarioDatasetError(f"JSON object required: {path}")
    return value


def _safe_child(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ScenarioDatasetError(f"path escapes repository: {relative}") from exc
    return candidate


def _repository_root(manifest_path: Path) -> Path:
    for parent in manifest_path.parents:
        if parent.name == "datasets":
            return parent.parent
    raise ScenarioDatasetError(
        "scenario manifest must be stored under a repository datasets directory"
    )


def _stable_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def scenario_snapshot_id(
    manifest: dict[str, Any], allowlist: dict[str, Any]
) -> str:
    identity = {
        "schemaVersion": manifest.get("schemaVersion"),
        "datasetId": manifest.get("datasetId"),
        "parentDatasetSnapshotId": manifest.get("parentDatasetSnapshotId"),
        "laws": [
            {
                "lawId": item.get("lawId"),
                "sourceSha256": item.get("sourceSha256"),
            }
            for item in sorted(
                manifest.get("laws") or [], key=lambda item: str(item.get("lawId"))
            )
        ],
        "articles": [
            {
                "articleId": item.get("articleId"),
                "lawId": item.get("lawId"),
                "provisionType": item.get("provisionType"),
                "articleNum": item.get("articleNum"),
            }
            for item in sorted(
                allowlist.get("articles") or [],
                key=lambda item: str(item.get("articleId")),
            )
        ],
    }
    return f"public-tender-offer-mini-{_stable_hash(identity)}"


def load_scenario_dataset(manifest_path: Path) -> ScenarioDataset:
    manifest_path = manifest_path.resolve()
    manifest = _read_json(manifest_path)
    if manifest.get("schemaVersion") != 1:
        raise ScenarioDatasetError("unsupported scenario dataset schema")
    if manifest.get("datasetType") != "egov_article_subset_scenario":
        raise ScenarioDatasetError("invalid scenario datasetType")
    if manifest.get("sourceReusePolicy") != "frozen_local_xml_only":
        raise ScenarioDatasetError("scenario must use frozen local XML")

    dataset_id = str(manifest.get("datasetId") or "")
    if not dataset_id:
        raise ScenarioDatasetError("scenario datasetId is required")
    allowlist_path = _safe_child(
        manifest_path.parent, str(manifest.get("articleAllowlistPath") or "")
    )
    allowlist = _read_json(allowlist_path)
    if allowlist.get("schemaVersion") != 1:
        raise ScenarioDatasetError("unsupported scenario allowlist schema")
    if allowlist.get("datasetId") != dataset_id:
        raise ScenarioDatasetError("manifest and allowlist datasetId mismatch")
    if allowlist.get("selectionUnit") != "complete_main_provision_article":
        raise ScenarioDatasetError("scenario must select complete main Articles")

    expected_snapshot = scenario_snapshot_id(manifest, allowlist)
    declared_snapshot = str(manifest.get("datasetSnapshotId") or "")
    if declared_snapshot != expected_snapshot:
        raise ScenarioDatasetError(
            f"datasetSnapshotId mismatch: expected {expected_snapshot}, got {declared_snapshot}"
        )

    repo_root = _repository_root(manifest_path)
    law_entries = manifest.get("laws")
    if not isinstance(law_entries, list) or not law_entries:
        raise ScenarioDatasetError("scenario manifest requires laws")
    laws: list[ScenarioLaw] = []
    laws_by_id: dict[str, ScenarioLaw] = {}
    article_nums_in_xml: dict[str, set[str]] = {}
    for entry in law_entries:
        if not isinstance(entry, dict):
            raise ScenarioDatasetError("scenario law entries must be objects")
        law_id = str(entry.get("lawId") or "")
        if not law_id or law_id in laws_by_id:
            raise ScenarioDatasetError("scenario lawId values must be unique")
        source_path = _safe_child(repo_root, str(entry.get("sourcePath") or ""))
        if not source_path.is_file():
            raise ScenarioDatasetError(f"scenario source XML is missing: {source_path}")
        payload = source_path.read_bytes()
        actual_hash = hashlib.sha256(payload).hexdigest()
        expected_hash = str(entry.get("sourceSha256") or "").removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ScenarioDatasetError(f"scenario law requires SHA-256: {law_id}")
        if actual_hash != expected_hash:
            raise ScenarioDatasetError(f"scenario source XML hash mismatch: {law_id}")
        root = ET.fromstring(payload)
        title = "".join((root.findtext(".//LawTitle") or "").split())
        expected_title = "".join(str(entry.get("title") or "").split())
        if title != expected_title:
            raise ScenarioDatasetError(f"scenario source title mismatch: {law_id}")
        main = root.find(".//LawBody/MainProvision")
        if main is None:
            raise ScenarioDatasetError(f"scenario source has no MainProvision: {law_id}")
        article_nums = [str(article.get("Num") or "") for article in main.iter("Article")]
        if any(not value for value in article_nums) or len(article_nums) != len(
            set(article_nums)
        ):
            raise ScenarioDatasetError(
                f"scenario source MainProvision Article numbers are invalid: {law_id}"
            )
        law = ScenarioLaw(
            law_id=law_id,
            title=str(entry["title"]),
            authority_type=str(entry.get("authorityType") or ""),
            family_root=str(entry.get("familyRoot") or law_id),
            source_path=source_path,
            source_sha256=f"sha256:{expected_hash}",
        )
        if not law.authority_type:
            raise ScenarioDatasetError(f"scenario authorityType is required: {law_id}")
        laws.append(law)
        laws_by_id[law_id] = law
        article_nums_in_xml[law_id] = set(article_nums)

    article_entries = allowlist.get("articles")
    if not isinstance(article_entries, list) or not article_entries:
        raise ScenarioDatasetError("scenario allowlist requires Articles")
    article_ids_by_law: dict[str, list[str]] = {law_id: [] for law_id in laws_by_id}
    seen_article_ids: set[str] = set()
    for entry in article_entries:
        if not isinstance(entry, dict):
            raise ScenarioDatasetError("scenario Article entries must be objects")
        law_id = str(entry.get("lawId") or "")
        article_num = str(entry.get("articleNum") or "")
        article_id = str(entry.get("articleId") or "")
        if law_id not in laws_by_id:
            raise ScenarioDatasetError(f"unknown scenario lawId: {law_id}")
        if entry.get("provisionType") != "main":
            raise ScenarioDatasetError("scenario accepts main Articles only")
        if article_id != f"law-{law_id}-article-{article_num}":
            raise ScenarioDatasetError(f"invalid scenario Article ID: {article_id}")
        if article_id in seen_article_ids:
            raise ScenarioDatasetError(f"duplicate scenario Article ID: {article_id}")
        if article_num not in article_nums_in_xml[law_id]:
            raise ScenarioDatasetError(f"scenario Article is absent from XML: {article_id}")
        seen_article_ids.add(article_id)
        article_ids_by_law[law_id].append(article_id)

    return ScenarioDataset(
        dataset_id=dataset_id,
        dataset_snapshot_id=declared_snapshot,
        parent_dataset_snapshot_id=str(
            manifest.get("parentDatasetSnapshotId") or ""
        ),
        manifest_path=manifest_path,
        laws=tuple(laws),
        article_ids_by_law={
            law_id: tuple(article_ids)
            for law_id, article_ids in article_ids_by_law.items()
        },
    )


__all__ = [
    "ScenarioDataset",
    "ScenarioDatasetError",
    "ScenarioLaw",
    "load_scenario_dataset",
    "scenario_snapshot_id",
]
