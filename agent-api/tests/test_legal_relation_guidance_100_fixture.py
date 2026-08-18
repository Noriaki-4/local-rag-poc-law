import json
from collections import Counter
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = REPO_ROOT / "docs/requirements/samples/eval"
LEGAL_FIXTURE = EVAL_DIR / "legal_relation_94_adjudicated_fixture.jsonl"
AUDIT_FIXTURE = EVAL_DIR / "legal_relation_94_adjudication_audit.jsonl"
GUIDANCE_FIXTURE = EVAL_DIR / "guidance_navigation_fixture.jsonl"
MANIFEST = EVAL_DIR / "legal_relation_guidance_100_manifest.json"


def _load_jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_dataset_has_five_twenty_case_shards_and_two_typed_lanes():
    legal = _load_jsonl(LEGAL_FIXTURE)
    guidance = _load_jsonl(GUIDANCE_FIXTURE)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert len(legal) == 94
    assert len(guidance) == 6
    assert manifest["caseCount"] == 100
    assert manifest["shardCount"] == 5
    assert manifest["casesPerShard"] == 20
    assert [lane["caseCount"] for lane in manifest["lanes"]] == [94, 6]
    assert len({item["fixtureId"] for item in [*legal, *guidance]}) == 100
    assert len({item["basisEdgeId"] for item in legal}) == 94


def test_legal_fixture_covers_reference_structure_and_all_predicates():
    legal = _load_jsonl(LEGAL_FIXTURE)

    assert Counter(item["referenceKind"] for item in legal) == {
        "parent_law_reference": 22,
        "application": 18,
        "definition": 18,
        "exception": 18,
        "article_reference": 18,
    }
    assert Counter(item["expectedResolutionStatus"] for item in legal) == {
        "resolved": 72,
        "unresolved": 14,
        "not_reference": 8,
    }
    assert Counter(item["expectedSemanticStatus"] for item in legal) == {
        "reviewer_approved": 71,
        "unresolved_after_single_revision": 1,
        "not_applicable": 22,
    }
    assert Counter(
        predicate
        for item in legal
        for predicate in (item["expectedPredicates"] or [])
    ) == {
        "IMPLEMENTS": 9,
        "USES_DEFINITION": 12,
        "INCORPORATES": 3,
        "EXCEPTION_TO": 2,
        "OVERRIDES": 1,
    }
    assert sum("-suppl-" in item["referenceSourceArticleId"] for item in legal) == 22


def test_only_reviewer_approved_valid_pairs_have_semantic_teacher_labels():
    legal = _load_jsonl(LEGAL_FIXTURE)
    audits = {
        item["basisEdgeId"]: item for item in _load_jsonl(AUDIT_FIXTURE)
    }

    assert set(audits) == {item["basisEdgeId"] for item in legal}
    for item in legal:
        audit = audits[item["basisEdgeId"]]
        semantic_status = item["expectedSemanticStatus"]
        if semantic_status == "reviewer_approved":
            assert item["expectedResolutionStatus"] == "resolved"
            assert item["expectedPredicates"] is not None
            assert audit["semanticDecision"]["adjudicationStatus"] == "accepted"
            assert audit["semanticDispute"] is None
        elif semantic_status == "unresolved_after_single_revision":
            assert item["expectedResolutionStatus"] == "resolved"
            assert item["expectedPredicates"] is None
            assert audit["semanticDecision"] is None
            assert audit["semanticDispute"] is not None
        else:
            assert item["expectedResolutionStatus"] in {
                "not_reference",
                "unresolved",
            }
            assert item["expectedPredicates"] is None
            assert audit["semanticDecision"] is None


def test_manifest_records_human_judgment_boundary():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert manifest["adjudication"] == {
        "workerModel": "gpt-5.6-luna",
        "reviewerModel": "gpt-5.6-luna",
        "maximumRevisionCount": 1,
        "finalAudit": "Codex cross-shard manual audit",
        "meaningJudgmentByProgram": False,
    }
    assert manifest["coverage"]["sourceLawFamilyCount"] == 13
