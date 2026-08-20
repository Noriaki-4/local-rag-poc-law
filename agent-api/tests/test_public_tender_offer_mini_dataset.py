import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts/validate_public_tender_offer_mini_dataset.py"
DATASET_DIR = (
    REPO_ROOT / "datasets/scenarios/public_tender_offer_three_layer_v1"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "validate_public_tender_offer_mini_dataset", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_public_tender_offer_dataset_is_frozen_complete_and_gold_separated():
    module = _load_module()

    report = module.validate_dataset(DATASET_DIR, repo_root=REPO_ROOT)

    assert report["lawCount"] == 3
    assert report["selectedArticleCount"] == 13
    assert report["selectedArticleCountByLaw"] == {
        "323AC0000000025": 3,
        "340CO0000000321": 4,
        "402M50000040038": 6,
    }
    assert report["paragraphCount"] >= 13
    assert report["articleTextChars"] > 10_000
    assert report["expectedReferenceCount"] == 4
    assert report["requiredNavigationCount"] == 4
    assert report["forbiddenNavigationCount"] == 1
    assert report["questionCount"] == 3
    assert report["goldIncludedInIngestInputs"] is False


def test_public_tender_offer_dataset_snapshot_is_reproducible():
    module = _load_module()
    manifest = module._load_json(DATASET_DIR / "manifest.json")
    allowlist = module._load_json(DATASET_DIR / "article_allowlist.json")

    assert module.scenario_snapshot_id(manifest, allowlist) == manifest[
        "datasetSnapshotId"
    ]
