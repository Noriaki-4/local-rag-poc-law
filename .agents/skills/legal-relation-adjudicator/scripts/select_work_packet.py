"""Select work-packet records by basis IDs from a separate manifest."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict):
            raise TypeError(f"{path}:{line_number}: record must be an object")
        records.append(record)
    return records


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--selector", type=Path, required=True)
    parser.add_argument("--selector-field")
    parser.add_argument("--selector-value")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if (args.selector_field is None) != (args.selector_value is None):
        parser.error("--selector-field and --selector-value must be used together")
    packet = _load_jsonl(args.packet)
    selector = _load_jsonl(args.selector)
    if args.selector_field is not None:
        selector = [
            record
            for record in selector
            if str(record.get(args.selector_field)) == args.selector_value
        ]
    selected_ids = [str(record.get("basisEdgeId") or "") for record in selector]
    if any(not basis_id for basis_id in selected_ids):
        parser.error("every selected record must have basisEdgeId")
    if len(selected_ids) != len(set(selected_ids)):
        parser.error("selector contains duplicate basisEdgeId")
    packet_by_basis = {
        str(basis_id): record
        for record in packet
        for basis_id in record.get("basisEdgeIds", [])
    }
    missing = set(selected_ids) - set(packet_by_basis)
    if missing:
        parser.error(f"selected basis IDs are absent from packet: {sorted(missing)}")
    selected = []
    selected_candidate_keys: set[str] = set()
    for basis_id in selected_ids:
        record = packet_by_basis[basis_id]
        candidate_key = str(record.get("candidateKey") or "")
        if candidate_key not in selected_candidate_keys:
            selected.append(record)
            selected_candidate_keys.add(candidate_key)
    _atomic_write(
        args.output,
        "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in selected
        ),
    )
    print(
        json.dumps(
            {
                "packetCount": len(packet),
                "selectedCount": len(selected),
                "output": str(args.output.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
