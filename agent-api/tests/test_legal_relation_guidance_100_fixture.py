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
        "resolved": 73,
        "unresolved": 13,
        "not_reference": 8,
    }
    assert Counter(item["expectedSemanticStatus"] for item in legal) == {
        "codex_verified": 72,
        "needs_resolution": 1,
        "not_applicable": 21,
    }
    assert Counter(
        predicate
        for item in legal
        for predicate in (item["expectedPredicates"] or [])
    ) == {
        "IMPLEMENTS": 9,
        "USES_DEFINITION": 25,
        "INCORPORATES": 5,
        "EXCEPTION_TO": 5,
        "OVERRIDES": 2,
    }
    assert sum("-suppl-" in item["referenceSourceArticleId"] for item in legal) == 22


def test_every_resolved_pair_has_a_reviewed_semantic_status():
    legal = _load_jsonl(LEGAL_FIXTURE)
    audits = {
        item["basisEdgeId"]: item for item in _load_jsonl(AUDIT_FIXTURE)
    }

    assert set(audits) == {item["basisEdgeId"] for item in legal}
    assert {item["adjudicationSource"] for item in legal} == {
        "gpt_5_6_sol_full_manual_gold_2026_08_19",
        "gpt_5_6_sol_version_mismatch_correction_2026_08_19",
    }
    for item in legal:
        audit = audits[item["basisEdgeId"]]
        assert audit["finalGoldAudit"] == {
            "ownerModel": "gpt-5.6-sol",
            "status": "verified",
            "structureReviewed": True,
            "meaningReviewed": item["expectedResolutionStatus"] == "resolved",
        }
        semantic_status = item["expectedSemanticStatus"]
        if semantic_status == "codex_verified":
            assert item["expectedResolutionStatus"] == "resolved"
            assert item["expectedPredicates"] is not None
            assert audit["semanticDecision"]["adjudicationStatus"] == "accepted"
        elif semantic_status == "needs_resolution":
            assert item["expectedResolutionStatus"] == "resolved"
            assert item["expectedPredicates"] is None
            assert audit["semanticDecision"]["adjudicationStatus"] == "needs_resolution"
            assert audit["semanticDecision"]["assertions"] == []
        else:
            assert item["expectedResolutionStatus"] in {
                "not_reference",
                "unresolved",
            }
            assert item["expectedPredicates"] is None
            assert audit["semanticDecision"] is None

    manually_resolved = [
        audit
        for audit in audits.values()
        if audit["manualFinalAdjudication"] is not None
    ]
    assert len(manually_resolved) == 18
    disputed = [
        audit for audit in manually_resolved if audit["semanticDispute"] is not None
    ]
    assert len(disputed) == 2
    assert any(
        audit["semanticDecision"]["assertions"]
        and audit["semanticDecision"]["assertions"][0]["proposedPredicate"]
        == "INCORPORATES"
        for audit in disputed
    )
    assert any(
        audit["semanticDecision"]["adjudicationStatus"] == "needs_resolution"
        and audit["semanticDispute"]["status"] == "resolved_as_version_mismatch"
        for audit in disputed
    )

    manually_structured = [
        audit
        for audit in audits.values()
        if audit["manualStructureAdjudication"] is not None
    ]
    assert len(manually_structured) == 1
    assert manually_structured[0]["originalStructuralDecision"] is not None
    assert manually_structured[0]["structuralDecision"]["structuralStatus"] == "valid_pair"


def test_manifest_records_human_judgment_boundary():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert manifest["adjudication"] == {
        "answerKeyModel": "gpt-5.6-sol",
        "answerKeyAuditScope": "94 legal relation cases and 6 guidance cases",
        "answerKeyVerifiedCaseCount": 100,
        "semanticVerifiedPairCount": 72,
        "priorLunaArtifactsRetainedForAudit": True,
        "manualStructureCorrectionCount": 1,
        "manualSemanticCorrectionCount": 18,
        "finalAudit": "Codex full 100-case manual audit plus Article-version mismatch correction",
        "meaningJudgmentByProgram": False,
    }
    assert manifest["coverage"]["sourceLawFamilyCount"] == 13
