#!/usr/bin/env python3
"""Validate the frozen public-tender-offer three-layer scenario dataset."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "agent-api"))

from app.scenario_dataset import scenario_snapshot_id  # noqa: E402

DEFAULT_DATASET_DIR = (
    REPO_ROOT / "datasets/scenarios/public_tender_offer_three_layer_v1"
)
ALLOWED_PREDICATES = {
    "IMPLEMENTS",
    "INCORPORATES",
    "USES_DEFINITION",
    "EXCEPTION_TO",
    "OVERRIDES",
}
GOLD_ONLY_KEYS = {
    "answerPoints",
    "annotationBasis",
    "basisReferenceExpectationId",
    "expectation",
    "predicate",
    "requiredEvidenceArticleIds",
    "requiredNavigationArticleIds",
    "structuralStatus",
}


class ScenarioDatasetError(RuntimeError):
    """The frozen scenario dataset violates its declared contract."""


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ScenarioDatasetError(f"JSON object required: {path}")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ScenarioDatasetError(
                f"JSONL object required: {path}:{line_number}"
            )
        output.append(value)
    return output


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _assert_no_gold_keys(value: Any, *, location: str) -> None:
    if isinstance(value, dict):
        overlap = GOLD_ONLY_KEYS.intersection(value)
        if overlap:
            raise ScenarioDatasetError(
                f"gold-only keys leaked into {location}: {sorted(overlap)}"
            )
        for child in value.values():
            _assert_no_gold_keys(child, location=location)
    elif isinstance(value, list):
        for child in value:
            _assert_no_gold_keys(child, location=location)


def _safe_repo_path(repo_root: Path, relative_path: str) -> Path:
    path = (repo_root / relative_path).resolve()
    try:
        path.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise ScenarioDatasetError(f"path escapes repository: {relative_path}") from exc
    return path


def _law_and_main_articles(payload: bytes) -> tuple[ET.Element, dict[str, ET.Element]]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ScenarioDatasetError(f"invalid e-Gov XML: {exc}") from exc
    law = root.find(".//Law")
    if law is None:
        raise ScenarioDatasetError("e-Gov XML has no Law element")
    main = law.find("./LawBody/MainProvision")
    if main is None:
        raise ScenarioDatasetError("e-Gov XML has no MainProvision")
    by_num: dict[str, ET.Element] = {}
    duplicates: set[str] = set()
    for article in main.iter("Article"):
        article_num = str(article.get("Num") or "")
        if not article_num:
            raise ScenarioDatasetError("MainProvision Article has no Num")
        if article_num in by_num:
            duplicates.add(article_num)
        by_num[article_num] = article
    if duplicates:
        raise ScenarioDatasetError(
            f"duplicate MainProvision Article numbers: {sorted(duplicates)}"
        )
    return law, by_num


def _element_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def validate_dataset(
    dataset_dir: Path = DEFAULT_DATASET_DIR,
    *,
    repo_root: Path = REPO_ROOT,
) -> dict[str, Any]:
    dataset_dir = dataset_dir.resolve()
    manifest = _load_json(dataset_dir / "manifest.json")
    allowlist = _load_json(dataset_dir / str(manifest["articleAllowlistPath"]))
    if manifest.get("schemaVersion") != 1 or allowlist.get("schemaVersion") != 1:
        raise ScenarioDatasetError("unsupported scenario dataset schema")
    if manifest.get("datasetId") != allowlist.get("datasetId"):
        raise ScenarioDatasetError("manifest and allowlist datasetId mismatch")
    _assert_no_gold_keys(manifest, location="manifest")
    _assert_no_gold_keys(allowlist, location="article allowlist")

    expected_snapshot = scenario_snapshot_id(manifest, allowlist)
    if manifest.get("datasetSnapshotId") != expected_snapshot:
        raise ScenarioDatasetError(
            "datasetSnapshotId mismatch: "
            f"expected {expected_snapshot}, got {manifest.get('datasetSnapshotId')}"
        )

    laws = manifest.get("laws")
    if not isinstance(laws, list) or not laws:
        raise ScenarioDatasetError("manifest requires laws")
    laws_by_id: dict[str, dict[str, Any]] = {}
    main_articles_by_law: dict[str, dict[str, ET.Element]] = {}
    for law_entry in laws:
        law_id = str(law_entry.get("lawId") or "")
        if not law_id or law_id in laws_by_id:
            raise ScenarioDatasetError("lawId values must be non-empty and unique")
        source = _safe_repo_path(repo_root, str(law_entry.get("sourcePath") or ""))
        payload = source.read_bytes()
        actual_hash = f"sha256:{_sha256_hex(payload)}"
        if actual_hash != law_entry.get("sourceSha256"):
            raise ScenarioDatasetError(f"source XML hash mismatch: {law_id}")
        law, main_articles = _law_and_main_articles(payload)
        title = _element_text(law.find("./LawBody/LawTitle"))
        if title != law_entry.get("title"):
            raise ScenarioDatasetError(f"source law title mismatch: {law_id}")
        laws_by_id[law_id] = law_entry
        main_articles_by_law[law_id] = main_articles

    articles = allowlist.get("articles")
    if not isinstance(articles, list) or not articles:
        raise ScenarioDatasetError("article allowlist requires articles")
    article_ids = [str(item.get("articleId") or "") for item in articles]
    if any(not article_id for article_id in article_ids):
        raise ScenarioDatasetError("articleId must not be empty")
    if len(article_ids) != len(set(article_ids)):
        raise ScenarioDatasetError("articleId values must be unique")
    article_text_by_id: dict[str, str] = {}
    per_law = Counter()
    paragraph_count = 0
    item_count = 0
    for item in articles:
        law_id = str(item.get("lawId") or "")
        article_num = str(item.get("articleNum") or "")
        if law_id not in laws_by_id:
            raise ScenarioDatasetError(f"unknown allowlist lawId: {law_id}")
        if item.get("provisionType") != "main":
            raise ScenarioDatasetError("mini dataset accepts complete main Articles only")
        expected_id = f"law-{law_id}-article-{article_num}"
        if item.get("articleId") != expected_id:
            raise ScenarioDatasetError(
                f"article ID does not match naming contract: {item.get('articleId')}"
            )
        article = main_articles_by_law[law_id].get(article_num)
        if article is None:
            raise ScenarioDatasetError(f"MainProvision Article is missing: {expected_id}")
        text = _element_text(article)
        if not text:
            raise ScenarioDatasetError(f"Article text is empty: {expected_id}")
        article_text_by_id[expected_id] = text
        paragraph_count += sum(1 for _ in article.iter("Paragraph"))
        item_count += sum(1 for _ in article.iter("Item"))
        per_law[law_id] += 1

    eval_paths = [dataset_dir / str(path) for path in manifest["evaluationArtifacts"]]
    if any(not path.is_file() for path in eval_paths):
        raise ScenarioDatasetError("evaluation artifact is missing")
    references = _load_jsonl(dataset_dir / "eval/expected_references.jsonl")
    reference_by_id: dict[str, dict[str, Any]] = {}
    for record in references:
        expectation_id = str(record.get("expectationId") or "")
        if not expectation_id or expectation_id in reference_by_id:
            raise ScenarioDatasetError("reference expectation IDs must be unique")
        source_id = str(record.get("sourceArticleId") or "")
        target_id = str(record.get("targetArticleId") or "")
        if source_id not in article_text_by_id or target_id not in article_text_by_id:
            raise ScenarioDatasetError(
                f"reference expectation endpoint is outside dataset: {expectation_id}"
            )
        if record.get("structuralStatus") != "valid_pair":
            raise ScenarioDatasetError("expected references must be structurally verified")
        citation = str(record.get("citationContains") or "")
        minimum_count = int(record.get("minimumOccurrenceCount") or 0)
        if not citation or minimum_count < 1:
            raise ScenarioDatasetError("reference expectation requires citation and count")
        if article_text_by_id[source_id].count(citation) < minimum_count:
            raise ScenarioDatasetError(
                f"reference occurrence is missing from source Article: {expectation_id}"
            )
        reference_by_id[expectation_id] = record

    navigation = _load_jsonl(dataset_dir / "eval/navigation_expectations.jsonl")
    navigation_ids: set[str] = set()
    for record in navigation:
        expectation_id = str(record.get("expectationId") or "")
        if not expectation_id or expectation_id in navigation_ids:
            raise ScenarioDatasetError("navigation expectation IDs must be unique")
        navigation_ids.add(expectation_id)
        basis_id = str(record.get("basisReferenceExpectationId") or "")
        if basis_id not in reference_by_id:
            raise ScenarioDatasetError(
                f"navigation expectation has unknown basis: {expectation_id}"
            )
        predicate = str(record.get("predicate") or "")
        if predicate not in ALLOWED_PREDICATES:
            raise ScenarioDatasetError(f"unknown navigation predicate: {predicate}")
        if record.get("expectation") not in {"required", "forbidden"}:
            raise ScenarioDatasetError("navigation expectation must be required or forbidden")
        endpoints = {
            str(record.get("subjectArticleId") or ""),
            str(record.get("objectArticleId") or ""),
        }
        if not endpoints.issubset(article_text_by_id):
            raise ScenarioDatasetError(
                f"navigation endpoint is outside dataset: {expectation_id}"
            )
        basis = reference_by_id[basis_id]
        if endpoints != {basis["sourceArticleId"], basis["targetArticleId"]}:
            raise ScenarioDatasetError(
                f"navigation endpoints differ from physical basis pair: {expectation_id}"
            )

    questions = _load_jsonl(dataset_dir / "eval/questions.jsonl")
    question_ids: set[str] = set()
    for record in questions:
        question_id = str(record.get("questionId") or "")
        if not question_id or question_id in question_ids:
            raise ScenarioDatasetError("question IDs must be non-empty and unique")
        question_ids.add(question_id)
        if not str(record.get("question") or "").strip():
            raise ScenarioDatasetError(f"question text is missing: {question_id}")
        required_ids = [
            *list(record.get("requiredEvidenceArticleIds") or []),
            *list(record.get("requiredNavigationArticleIds") or []),
        ]
        if not set(required_ids).issubset(article_text_by_id):
            raise ScenarioDatasetError(
                f"question gold references Article outside dataset: {question_id}"
            )
        if not record.get("answerPoints"):
            raise ScenarioDatasetError(f"question answer points are missing: {question_id}")

    return {
        "datasetId": manifest["datasetId"],
        "datasetSnapshotId": manifest["datasetSnapshotId"],
        "lawCount": len(laws_by_id),
        "selectedArticleCount": len(article_text_by_id),
        "selectedArticleCountByLaw": dict(sorted(per_law.items())),
        "paragraphCount": paragraph_count,
        "itemCount": item_count,
        "articleTextChars": sum(len(text) for text in article_text_by_id.values()),
        "expectedReferenceCount": len(references),
        "requiredNavigationCount": sum(
            record["expectation"] == "required" for record in navigation
        ),
        "forbiddenNavigationCount": sum(
            record["expectation"] == "forbidden" for record in navigation
        ),
        "questionCount": len(questions),
        "goldIncludedInIngestInputs": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATASET_DIR)
    args = parser.parse_args()
    report = validate_dataset(args.dataset_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
