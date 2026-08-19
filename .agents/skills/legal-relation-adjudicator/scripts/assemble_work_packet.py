"""Assemble validated candidate packets without duplicating candidates."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def _load(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict):
            raise TypeError(f"{path}:{line_number}: record must be an object")
        basis_ids = record.get("basisEdgeIds")
        if (
            not record.get("candidateKey")
            or not isinstance(basis_ids, list)
            or not basis_ids
            or any(not isinstance(value, str) or not value for value in basis_ids)
        ):
            raise ValueError(f"{path}:{line_number}: candidate identity is missing")
        if basis_ids != sorted(set(basis_ids)):
            raise ValueError(
                f"{path}:{line_number}: basisEdgeIds must be sorted and unique"
            )
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
    parser.add_argument("--packet", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    records = [record for path in args.packet for record in _load(path)]
    candidate_keys = [str(record["candidateKey"]) for record in records]
    basis_ids = [
        str(basis_id)
        for record in records
        for basis_id in record["basisEdgeIds"]
    ]
    if len(candidate_keys) != len(set(candidate_keys)):
        raise ValueError("candidateKey values must be unique")
    if len(basis_ids) != len(set(basis_ids)):
        raise ValueError("basisEdgeIds values must be globally unique")

    _atomic_write(
        args.output,
        "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in records
        ),
    )
    print(
        json.dumps(
            {
                "candidateCount": len(records),
                "output": str(args.output.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
