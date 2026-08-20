from pathlib import Path

from app import seed as seed_module
from app.graph_audit import audit_graph
from app.scenario_dataset import load_scenario_dataset


REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = (
    REPO_ROOT
    / "datasets/scenarios/public_tender_offer_three_layer_v1/manifest.json"
)


def test_scenario_seed_builds_only_allowlisted_complete_articles(monkeypatch):
    scenario = load_scenario_dataset(MANIFEST)
    monkeypatch.setattr(
        seed_module,
        "embed_texts",
        lambda texts: [[0.0] for _ in texts],
    )
    monkeypatch.setattr(
        seed_module.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("scenario seed must not use the network")
        ),
    )

    documents = seed_module._scenario_egov_documents(scenario)

    assert {seed_module._article_id(document) for document in documents} == set(
        scenario.article_ids
    )
    assert all(document["sectionKey"] == "main" for document in documents)
    assert all(document["provisionType"] == "main" for document in documents)
    assert all(
        document["sourceObjectUri"].startswith(
            "dataset://public-tender-offer-three-layer-mini-v1/"
        )
        for document in documents
    )
    assert not any("-suppl-" in document["contentUnitId"] for document in documents)


def test_scenario_seed_graph_has_expected_three_layer_references(monkeypatch):
    scenario = load_scenario_dataset(MANIFEST)
    monkeypatch.setattr(
        seed_module,
        "embed_texts",
        lambda texts: [[0.0] for _ in texts],
    )
    documents, source_snapshot_id = seed_module._with_seed_identity(
        seed_module._scenario_egov_documents(scenario)
    )

    raw_nodes, raw_edges = seed_module._graph_artifacts_from_documents(
        documents, scenario.family_roots
    )
    nodes = seed_module._dedupe_by_key(raw_nodes, "graphNodeId")
    edges = seed_module._dedupe_by_key(raw_edges, "graphEdgeId")
    report = audit_graph(nodes, edges, source_snapshot_id=source_snapshot_id)
    reference_pairs = {
        (edge["fromGraphNodeId"], edge["toGraphNodeId"])
        for edge in edges
        if edge["edgeType"] == "REFERENCES"
    }

    assert report.ok, report.violations
    assert (
        "law-340CO0000000321-article-7",
        "law-323AC0000000025-article-27_2",
    ) in reference_pairs
    assert (
        "law-402M50000040038-article-2_5",
        "law-340CO0000000321-article-7",
    ) in reference_pairs
    assert (
        "law-402M50000040038-article-10",
        "law-323AC0000000025-article-27_3",
    ) in reference_pairs
    assert {node["sourceSnapshotId"] for node in nodes} == {source_snapshot_id}
    assert {edge["sourceSnapshotId"] for edge in edges} == {source_snapshot_id}
