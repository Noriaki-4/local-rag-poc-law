import json
import sys

import bootstrap_entrypoint


def test_entrypoint_builds_runtime_config_from_task_environment(monkeypatch, tmp_path):
    values = {
        "AWS_ACCOUNT_ID": "123456789012",
        "AWS_REGION": "ap-northeast-1",
        "BOOTSTRAP_S3_PREFIX": "bootstrap/current",
        "SEARCH_SNAPSHOT_ID": "snapshot-" + "1" * 64,
        "GRAPH_SNAPSHOT_ID": "snapshot-" + "2" * 64,
        "CLASSIFICATION_RUN_ID": "published-run",
        "KNOWLEDGE_BUCKET_NAME": "bucket-name",
        "OPENSEARCH_URL": "https://collection.example",
        "OPENSEARCH_INDEX": "legal-index",
        "EMBEDDING_MODEL": "amazon.titan-embed-text-v2:0",
        "EMBEDDING_DIMENSION": "1024",
        "EMBEDDING_MAX_CHARS": "1000",
        "NEPTUNE_GRAPH_ID": "g-123",
        "BOOTSTRAP_WORK_DIR": str(tmp_path),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        bootstrap_entrypoint,
        "_download_artifact",
        lambda _bucket, _prefix, destination: destination.mkdir(),
    )
    captured = {}

    def invoke():
        captured["argv"] = list(sys.argv)
        return 0

    monkeypatch.setattr(bootstrap_entrypoint.bootstrap_aws_data, "main", invoke)

    assert bootstrap_entrypoint.main() == 0
    config = json.loads((tmp_path / "bootstrap-config.json").read_text())
    assert config["account"] == "123456789012"
    assert config["bootstrapData"]["searchSnapshotId"] == values[
        "SEARCH_SNAPSHOT_ID"
    ]
    assert config["openSearchServerless"]["embeddingDimensions"] == 1024
    assert "--apply" in captured["argv"]
