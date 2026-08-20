from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
BINDER = (
    REPO_ROOT
    / ".agents/skills/legal-relation-adjudicator/scripts/bind_single_occurrence_ids.py"
)


def _write_jsonl(path: Path, values: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(value) + "\n" for value in values), encoding="utf-8"
    )


def _candidate() -> dict:
    return {
        "candidateKey": "candidate-1",
        "referenceOccurrences": [{"occurrenceHash": "occurrence-1"}],
    }


def _worker() -> dict:
    return {
        "candidateKey": "candidate-1",
        "assertions": [{"referenceOccurrenceHash": "model-placeholder"}],
    }


def _run_binder(tmp_path: Path, packet_value: dict) -> dict:
    packet = tmp_path / "packet.jsonl"
    worker = tmp_path / "worker.jsonl"
    output = tmp_path / "bound.jsonl"
    _write_jsonl(packet, [packet_value])
    _write_jsonl(worker, [_worker()])
    subprocess.run(
        [
            sys.executable,
            str(BINDER),
            "--packet",
            str(packet),
            "--worker",
            str(worker),
            "--output",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(output.read_text(encoding="utf-8"))


def test_binder_accepts_original_candidate_packet(tmp_path: Path) -> None:
    bound = _run_binder(tmp_path, _candidate())
    assert bound["assertions"][0]["referenceOccurrenceHash"] == "occurrence-1"


def test_binder_accepts_revision_envelope(tmp_path: Path) -> None:
    bound = _run_binder(
        tmp_path,
        {
            "candidateKey": "candidate-1",
            "originalCandidate": _candidate(),
            "previousDecision": {},
            "reviewFeedback": {},
        },
    )
    assert bound["assertions"][0]["referenceOccurrenceHash"] == "occurrence-1"
