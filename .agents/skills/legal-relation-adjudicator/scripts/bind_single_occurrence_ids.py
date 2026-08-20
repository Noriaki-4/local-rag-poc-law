"""Bind envelope-owned occurrence hashes without changing semantic decisions."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _atomic_write(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(
                    json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    packet_envelopes = _load_jsonl(args.packet)
    packets = []
    for envelope in packet_envelopes:
        candidate = envelope.get("originalCandidate", envelope)
        if not isinstance(candidate, dict):
            parser.error("originalCandidate must be an object")
        packets.append(candidate)
    workers = _load_jsonl(args.worker)
    if len(packets) != len(workers):
        parser.error("packet and worker counts must match")

    packet_by_key = {str(row.get("candidateKey") or ""): row for row in packets}
    if "" in packet_by_key or len(packet_by_key) != len(packets):
        parser.error("packet candidateKey values must be present and unique")

    replacement_count = 0
    errors: list[str] = []
    for worker in workers:
        candidate_key = str(worker.get("candidateKey") or "")
        packet = packet_by_key.get(candidate_key)
        if packet is None:
            errors.append(f"{candidate_key}: unknown worker candidateKey")
            continue
        occurrences = packet.get("referenceOccurrences")
        assertions = worker.get("assertions")
        if not isinstance(occurrences, list) or not occurrences:
            errors.append(f"{candidate_key}: missing referenceOccurrences")
            continue
        if not isinstance(assertions, list):
            errors.append(f"{candidate_key}: assertions must be a list")
            continue
        known_hashes = {
            str(item.get("occurrenceHash"))
            for item in occurrences
            if isinstance(item, dict) and item.get("occurrenceHash")
        }
        if len(known_hashes) != len(occurrences):
            errors.append(f"{candidate_key}: invalid or duplicate occurrence hashes")
            continue
        if len(occurrences) == 1:
            canonical_hash = next(iter(known_hashes))
            for assertion in assertions:
                if not isinstance(assertion, dict):
                    errors.append(f"{candidate_key}: assertion must be an object")
                    continue
                if assertion.get("referenceOccurrenceHash") != canonical_hash:
                    assertion["referenceOccurrenceHash"] = canonical_hash
                    replacement_count += 1
        else:
            for assertion in assertions:
                if not isinstance(assertion, dict):
                    errors.append(f"{candidate_key}: assertion must be an object")
                elif assertion.get("referenceOccurrenceHash") not in known_hashes:
                    errors.append(f"{candidate_key}: unknown occurrence hash")

    if errors:
        parser.error("; ".join(errors))
    _atomic_write(args.output, workers)
    print(
        json.dumps(
            {
                "candidateCount": len(workers),
                "singleOccurrenceHashBindings": replacement_count,
                "output": str(args.output.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
